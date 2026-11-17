#!/usr/bin/env python3
"""Fail-closed retention planner for waka-threads daily media.

Default behavior is a dry-run audit. Files are unlinked only when every
safety gate is open, and the gates committed in this repo are closed.

The Apps Script ledger that passes image_url to the Threads API is not in
this repository. A missing or untrusted ledger never authorizes deletion.
A folder date before today in Asia/Tokyo is not, by itself, a reason to
delete.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import unquote
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
MEDIA_ROOT = "waka-threads"

# CLI latch. JSON flags alone cannot delete. This stays false until a later
# reviewed change, made only after the conditions in
# docs/waka-threads-media-retention.md are actually met.
CLI_APPLY_ENABLED = False
DATE_RE_PARTS = 3
MEDIA_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".mov",
    ".m4v",
}

# Roles that always keep a file. "published" is intentionally absent:
# official Threads docs do not say a live post survives source URL removal.
PROTECTED_ROLES = frozenset(
    {
        "pinned",
        "unpublished",
        "draft",
        "scheduled",
        "skipped",
        "rescheduled",
        "postponed",
        "reused",
        "shared",
        "unknown",
    }
)
DELETE_ELIGIBLE_ROLES = frozenset({"published"})

ROLE_ALIASES = {
    "pinned": "pinned",
    "固定": "pinned",
    "固定投稿": "pinned",
    "unpublished": "unpublished",
    "未投稿": "unpublished",
    "draft": "draft",
    "下書き": "draft",
    "scheduled": "scheduled",
    "予約": "scheduled",
    "未来予約": "scheduled",
    "skipped": "skipped",
    "見送り": "skipped",
    "rescheduled": "rescheduled",
    "postponed": "postponed",
    "振替": "rescheduled",
    "再掲待ち": "rescheduled",
    "reused": "reused",
    "再利用": "reused",
    "shared": "shared",
    "共有": "shared",
    "共有資産": "shared",
    "unknown": "unknown",
    "不明": "unknown",
    "published": "published",
    "公開済": "published",
    "投稿済": "published",
}

URL_PREFIXES = (
    "https://oguken87-a11y.github.io/pages/",
    "http://oguken87-a11y.github.io/pages/",
    "https://raw.githubusercontent.com/oguken87-a11y/pages/main/",
    "https://raw.githubusercontent.com/oguken87-a11y/pages/master/",
    "https://github.com/oguken87-a11y/pages/blob/main/",
    "https://github.com/oguken87-a11y/pages/blob/master/",
)


class LedgerInvalid(Exception):
    """The reference ledger cannot be trusted. Deletion stays closed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsafePath(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SafetyConfig:
    apply_enabled: bool = False
    published_media_source_url_independence_confirmed: bool = False
    media_root: str = MEDIA_ROOT
    timezone: str = "Asia/Tokyo"

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "SafetyConfig":
        # Missing or unexpected values fail closed.
        apply_enabled = data.get("apply_enabled") is True
        confirmed = data.get("published_media_source_url_independence_confirmed") is True
        media_root = data.get("media_root", MEDIA_ROOT)
        timezone = data.get("timezone", "Asia/Tokyo")
        if media_root != MEDIA_ROOT or timezone != "Asia/Tokyo":
            apply_enabled = False
            confirmed = False
        return cls(
            apply_enabled=apply_enabled,
            published_media_source_url_independence_confirmed=confirmed,
            media_root=MEDIA_ROOT,
            timezone="Asia/Tokyo",
        )


@dataclass(frozen=True)
class Ledger:
    roles_by_path: Mapping[str, frozenset[str]]
    source: str | None = None
    generated_at: str | None = None

    def roles(self, rel: str) -> frozenset[str] | None:
        return self.roles_by_path.get(rel)


@dataclass(frozen=True)
class Decision:
    path: str
    size: int
    folder_date: str | None
    action: str
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes": self.size,
            "folder_date": self.folder_date,
            "action": self.action,
            "reasons": list(self.reasons),
        }


@dataclass
class Plan:
    today_jst: date
    now_jst: datetime
    decisions: list[Decision] = field(default_factory=list)
    ledger_status: str = "missing"
    ledger_error: str | None = None

    @property
    def delete_candidates(self) -> list[Decision]:
        return [item for item in self.decisions if item.action == "delete"]


def load_config(path: Path | None) -> SafetyConfig:
    if path is None or not path.is_file():
        return SafetyConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SafetyConfig()
    if not isinstance(data, dict):
        return SafetyConfig()
    return SafetyConfig.from_mapping(data)


def today_in_jst(now: datetime) -> date:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(JST).date()


def _valid_calendar_date(text: str) -> date | None:
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    if parsed.isoformat() != text:
        return None
    return parsed


def _suffix_ok(filename: str) -> bool:
    if filename.startswith("."):
        return False
    dot = filename.rfind(".")
    if dot <= 0:
        return False
    return filename[dot:].lower() in MEDIA_SUFFIXES


def normalize_media_path(raw: object) -> str:
    """Return a repo-relative daily media path, or raise UnsafePath.

    Rejects absolute paths, traversal, unknown URL hosts, query strings,
    and anything that is not waka-threads/YYYY-MM-DD/<mediafile>.
    """
    if not isinstance(raw, str) or raw == "" or "\x00" in raw:
        raise UnsafePath("path must be a non-empty string")
    text = raw.strip()
    if "\\" in text:
        raise UnsafePath("backslash is not allowed")
    if text.startswith("//"):
        raise UnsafePath("network path is not allowed")

    if "://" in text:
        for prefix in URL_PREFIXES:
            if text.startswith(prefix):
                remainder = text[len(prefix) :]
                break
        else:
            raise UnsafePath("url host is not a known pages or raw prefix")
        if "?" in remainder or "#" in remainder:
            raise UnsafePath("url query or fragment is not allowed")
        text = unquote(remainder)
        if "\\" in text or "\x00" in text:
            raise UnsafePath("decoded url is not a safe path")
    elif text.startswith("/"):
        raise UnsafePath("absolute path is not allowed")

    parts = text.split("/")
    if len(parts) != DATE_RE_PARTS:
        raise UnsafePath("path must be waka-threads/YYYY-MM-DD/filename")
    root, folder, filename = parts
    if root != MEDIA_ROOT:
        raise UnsafePath("path is outside waka-threads")
    if folder in {"", ".", ".."} or filename in {"", ".", ".."}:
        raise UnsafePath("path segment is empty or traversal")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafePath("path traversal")
    if _valid_calendar_date(folder) is None:
        raise UnsafePath("folder date is not a calendar date")
    if "/" in filename or filename.startswith("."):
        raise UnsafePath("filename is not a daily media file")
    if not _suffix_ok(filename):
        raise UnsafePath("filename suffix is not daily media")
    return "/".join(parts)


def load_ledger(path: Path | None) -> Ledger | None:
    """Load a ledger. None means the caller did not supply one.

    A supplied file that cannot be trusted raises LedgerInvalid. Callers
    must treat that as fail-closed, not as an empty allow-list.
    """
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerInvalid(f"unreadable: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict):
        raise LedgerInvalid("ledger must be an object")
    if data.get("version") != 1:
        raise LedgerInvalid("ledger version must be 1")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise LedgerInvalid("ledger entries must be a non-empty list")
    roles_by_path: dict[str, set[str]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise LedgerInvalid(f"entry {index} is not an object")
        try:
            rel = normalize_media_path(entry.get("path"))
        except UnsafePath as exc:
            raise LedgerInvalid(f"entry {index} path rejected: {exc.reason}") from exc
        raw_roles = entry.get("roles")
        if not isinstance(raw_roles, list) or not raw_roles:
            raise LedgerInvalid(f"entry {index} roles must be a non-empty list")
        normalized: set[str] = set()
        for role in raw_roles:
            if not isinstance(role, str) or role.strip() == "":
                raise LedgerInvalid(f"entry {index} has an empty role")
            canonical = ROLE_ALIASES.get(role.strip(), role.strip())
            if canonical != role.strip() and "/" in canonical:
                raise LedgerInvalid(f"entry {index} role is not safe")
            if any(ch in canonical for ch in "/\\\x00"):
                raise LedgerInvalid(f"entry {index} role is not safe")
            normalized.add(canonical)
        roles_by_path.setdefault(rel, set()).update(normalized)
    source = data.get("source")
    generated_at = data.get("generated_at")
    return Ledger(
        roles_by_path={key: frozenset(value) for key, value in roles_by_path.items()},
        source=source if isinstance(source, str) else None,
        generated_at=generated_at if isinstance(generated_at, str) else None,
    )


def _iter_files(media_root: Path) -> Iterable[Path]:
    if not media_root.is_dir() or media_root.is_symlink():
        return
    for dirpath, dirnames, filenames in os.walk(media_root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames if not (current / name).is_symlink()
        )
        for name in sorted(filenames):
            yield current / name


def _relative_posix(repo_root: Path, path: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _classify_on_disk(repo_root: Path, path: Path) -> tuple[str | None, str | None, str | None]:
    """Return (rel, folder_date, reject_reason).

    rel is set only when the on-disk path is a regular file at the daily
    media layout and its resolved path stays inside that directory.
    """
    if path.is_symlink():
        return None, None, "symlink-not-eligible"
    try:
        rel = _relative_posix(repo_root, path)
    except ValueError:
        return None, None, "outside-repo"
    try:
        normalized = normalize_media_path(rel)
    except UnsafePath:
        return rel, None, "outside-daily-media-pattern"
    try:
        real = path.resolve(strict=True)
        media_root = (repo_root / MEDIA_ROOT).resolve()
        real.relative_to(media_root)
    except (OSError, ValueError):
        return rel, None, "path-escapes-media-root"
    expected = (repo_root / normalized).resolve()
    if real != expected:
        return rel, None, "path-escapes-media-root"
    if not stat.S_ISREG(path.stat().st_mode):
        return rel, None, "not-regular-file"
    return normalized, normalized.split("/")[1], None


def _keep(path: str, size: int, folder_date: str | None, *reasons: str) -> Decision:
    return Decision(path, size, folder_date, "keep", tuple(reasons))


def build_plan(
    repo_root: Path,
    now: datetime,
    ledger: Ledger | None,
    *,
    ledger_error: str | None = None,
    independence_confirmed: bool = False,
) -> Plan:
    today = today_in_jst(now)
    now_jst = now.astimezone(JST)
    if ledger_error:
        ledger_status = "invalid"
    elif ledger is None:
        ledger_status = "missing"
    else:
        ledger_status = "loaded"
    plan = Plan(
        today_jst=today,
        now_jst=now_jst,
        ledger_status=ledger_status,
        ledger_error=ledger_error,
    )
    media_root = repo_root / MEDIA_ROOT
    if not media_root.exists():
        return plan

    for path in _iter_files(media_root):
        size = path.lstat().st_size if path.is_symlink() else path.stat().st_size
        rel, folder_date, reject = _classify_on_disk(repo_root, path)
        display = rel or _relative_posix(repo_root, path)
        if reject is not None:
            plan.decisions.append(_keep(display, size, folder_date, reject))
            continue
        assert rel is not None and folder_date is not None
        folder = date.fromisoformat(folder_date)
        if folder >= today:
            plan.decisions.append(_keep(rel, size, folder_date, "jst-today-or-future"))
            continue
        if ledger_error:
            plan.decisions.append(
                _keep(rel, size, folder_date, "ledger-invalid-fail-closed")
            )
            continue
        if ledger is None:
            plan.decisions.append(
                _keep(rel, size, folder_date, "ledger-missing-fail-closed")
            )
            continue
        roles = ledger.roles(rel)
        if roles is None:
            plan.decisions.append(
                _keep(rel, size, folder_date, "not-in-ledger-fail-closed")
            )
            continue
        if not roles or not roles <= DELETE_ELIGIBLE_ROLES:
            listed = "+".join(sorted(roles))
            plan.decisions.append(
                _keep(rel, size, folder_date, f"protected-reference:{listed}")
            )
            continue
        if not independence_confirmed:
            plan.decisions.append(
                _keep(rel, size, folder_date, "published-source-url-unconfirmed")
            )
            continue
        plan.decisions.append(
            Decision(
                rel,
                size,
                folder_date,
                "delete",
                ("published-only-before-today",),
            )
        )
    return plan


def deletion_blockers(
    *,
    apply_requested: bool,
    config: SafetyConfig,
    plan: Plan,
) -> list[str]:
    blockers: list[str] = []
    if not apply_requested:
        blockers.append("apply-not-requested")
    if not config.apply_enabled:
        blockers.append("apply-disabled")
    if not config.published_media_source_url_independence_confirmed:
        blockers.append("published-source-url-independence-unconfirmed")
    if plan.ledger_status == "missing":
        blockers.append("ledger-missing")
    elif plan.ledger_status == "invalid":
        blockers.append("ledger-invalid")
    return blockers


def _unlink_daily_media(repo_root: Path, rel: str) -> None:
    """Unlink one regular file inside a daily media folder.

    Re-checks the path at unlink time. Symlinks and paths that resolve
    outside waka-threads/YYYY-MM-DD are refused.
    """
    normalized = normalize_media_path(rel)
    if normalized != rel:
        raise UnsafePath("path changed during normalization")
    path = repo_root.joinpath(*normalized.split("/"))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.EAGAIN}:
            raise UnsafePath("symlink-not-eligible") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath("not-regular-file")
        proc = Path(f"/proc/self/fd/{fd}")
        real = Path(os.readlink(proc))
        if not real.is_absolute():
            raise UnsafePath("unresolved file descriptor path")
        media_root = (repo_root / MEDIA_ROOT).resolve()
        try:
            inside = real.resolve().relative_to(media_root)
        except ValueError as exc:
            raise UnsafePath("path-escapes-media-root") from exc
        if inside.as_posix() != "/".join(normalized.split("/")[1:]):
            raise UnsafePath("path-escapes-media-root")
    finally:
        os.close(fd)
    os.unlink(path)


def apply_deletions(repo_root: Path, plan: Plan, *, allowed: bool) -> tuple[list[str], list[dict[str, str]]]:
    """Delete planned candidates only when allowed is true.

    Returns (deleted relative paths, refused-or-failed records).
    """
    if not allowed:
        return [], []
    deleted: list[str] = []
    problems: list[dict[str, str]] = []
    touched_dates: set[str] = set()
    for decision in plan.delete_candidates:
        try:
            _unlink_daily_media(repo_root, decision.path)
        except UnsafePath as exc:
            problems.append(
                {"path": decision.path, "result": "refused", "reason": exc.reason}
            )
            continue
        except OSError as exc:
            problems.append(
                {"path": decision.path, "result": "error", "reason": exc.strerror or str(exc)}
            )
            continue
        deleted.append(decision.path)
        if decision.folder_date:
            touched_dates.add(decision.folder_date)
    for folder_date in sorted(touched_dates):
        folder = repo_root / MEDIA_ROOT / folder_date
        try:
            if (
                folder.is_dir()
                and not folder.is_symlink()
                and folder.resolve().parent == (repo_root / MEDIA_ROOT).resolve()
                and not any(folder.iterdir())
            ):
                folder.rmdir()
        except OSError:
            continue
    return deleted, problems


def render_audit(
    plan: Plan,
    *,
    mode: str,
    blockers: list[str],
    deleted: list[str],
    problems: list[dict[str, str]],
) -> dict[str, object]:
    deleted_set = set(deleted)
    problem_by_path = {item["path"]: item for item in problems}
    items = []
    for decision in plan.decisions:
        payload = decision.to_json()
        if decision.path in deleted_set:
            payload["result"] = "deleted"
        elif decision.path in problem_by_path:
            payload["result"] = problem_by_path[decision.path]["result"]
            payload["result_reason"] = problem_by_path[decision.path]["reason"]
        else:
            payload["result"] = "kept"
        items.append(payload)
    keep_bytes = sum(item.size for item in plan.decisions if item.action == "keep")
    delete_bytes = sum(item.size for item in plan.delete_candidates)
    deleted_bytes = sum(
        item.size for item in plan.delete_candidates if item.path in deleted_set
    )
    reason_counts: dict[str, int] = {}
    for item in plan.decisions:
        for reason in item.reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "mode": mode,
        "today_jst": plan.today_jst.isoformat(),
        "now_jst": plan.now_jst.isoformat(),
        "deletion_allowed": not blockers and mode == "apply",
        "blockers": blockers,
        "ledger_status": plan.ledger_status,
        "ledger_error": plan.ledger_error,
        "summary": {
            "seen": len(plan.decisions),
            "keep": sum(1 for item in plan.decisions if item.action == "keep"),
            "delete_candidates": len(plan.delete_candidates),
            "deleted": len(deleted),
            "refused_or_failed": len(problems),
            "bytes_keep": keep_bytes,
            "bytes_delete_candidates": delete_bytes,
            "bytes_deleted": deleted_bytes,
            "reason_counts": reason_counts,
        },
        "problems": problems,
        "items": items,
    }


def format_summary(audit: Mapping[str, object]) -> str:
    summary = audit["summary"]
    assert isinstance(summary, dict)
    blockers = audit["blockers"]
    assert isinstance(blockers, list)
    lines = [
        f"モード: {audit['mode']}",
        f"基準日(JST): {audit['today_jst']}",
        f"削除実施: {'しない' if blockers else 'する'}",
        "阻害: " + ("なし" if not blockers else ", ".join(str(item) for item in blockers)),
        (
            "台帳: "
            + str(audit["ledger_status"])
            + (f" ({audit['ledger_error']})" if audit.get("ledger_error") else "")
        ),
        (
            "件数: 走査 {seen} / 保持 {keep} / 削除候補 {delete_candidates} / "
            "削除 {deleted}"
        ).format(**summary),
        (
            "容量: 保持 {bytes_keep} bytes / 削除候補 {bytes_delete_candidates} bytes / "
            "削除 {bytes_deleted} bytes"
        ).format(**summary),
        "理由内訳: "
        + ", ".join(
            f"{key}={value}"
            for key, value in sorted(summary["reason_counts"].items())
        ),
    ]
    return "\n".join(lines)


def run(
    repo_root: Path,
    *,
    now: datetime,
    config: SafetyConfig,
    ledger_path: Path | None,
    apply_requested: bool,
) -> tuple[dict[str, object], int]:
    ledger: Ledger | None = None
    ledger_error: str | None = None
    try:
        ledger = load_ledger(ledger_path)
    except LedgerInvalid as exc:
        ledger_error = exc.reason
    confirmed = config.published_media_source_url_independence_confirmed
    plan = build_plan(
        repo_root,
        now,
        ledger,
        ledger_error=ledger_error,
        independence_confirmed=confirmed,
    )
    blockers = deletion_blockers(
        apply_requested=apply_requested,
        config=config,
        plan=plan,
    )
    allowed = apply_requested and not blockers
    deleted, problems = apply_deletions(repo_root, plan, allowed=allowed)
    mode = "apply" if apply_requested else "dry-run"
    audit = render_audit(
        plan,
        mode=mode,
        blockers=blockers,
        deleted=deleted,
        problems=problems,
    )
    exit_code = 0
    if apply_requested and blockers:
        exit_code = 2
    elif any(item.get("result") == "error" for item in problems):
        exit_code = 1
    return audit, exit_code


def _parse_now(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("now must include a timezone offset")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit waka-threads daily media retention. Dry-run is the default "
            "and deletes nothing."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--now", type=_parse_now, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Request deletion. Refused while CLI_APPLY_ENABLED is false, "
            "even if the JSON flags are true."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config
    if config_path is None:
        default = repo_root / "config" / "waka-threads-retention.json"
        config_path = default if default.is_file() else None
    config = load_config(config_path)
    if not CLI_APPLY_ENABLED:
        # Ignore a flipped JSON file. Library callers used by tests can still
        # pass an open SafetyConfig to exercise the filters on temporary trees.
        config = SafetyConfig(
            apply_enabled=False,
            published_media_source_url_independence_confirmed=False,
        )
    now = args.now or datetime.now(JST)
    audit, exit_code = run(
        repo_root,
        now=now,
        config=config,
        ledger_path=args.ledger,
        apply_requested=args.apply,
    )
    rendered = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        sys.stdout.write(format_summary(audit) + "\n")
        sys.stdout.write(f"監査JSON: {args.output}\n")
    else:
        sys.stdout.write(format_summary(audit) + "\n\n")
        sys.stdout.write(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
