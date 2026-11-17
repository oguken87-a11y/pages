"""Safety tests for the waka-threads retention planner.

These tests use temporary directories. They do not delete the repository's
published media. The one real-repo test is a read-only dry-run.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.waka_threads_retention import (
    CLI_APPLY_ENABLED,
    LedgerInvalid,
    SafetyConfig,
    apply_deletions,
    build_plan,
    load_config,
    load_ledger,
    main,
    normalize_media_path,
    run,
)

JST = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")
REPO = Path(__file__).resolve().parents[1]
PAST = "waka-threads/2026-11-06/0700_keep.png"
TODAY = "waka-threads/2026-11-07/0800_today.png"
FUTURE = "waka-threads/2026-11-08/0900_future.png"


def _write(root: Path, rel: str, payload: bytes = b"img") -> Path:
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _ledger(root: Path, entries: list[dict[str, object]]) -> Path:
    path = root / "ledger.json"
    path.write_text(
        json.dumps({"version": 1, "source": "test", "entries": entries}),
        encoding="utf-8",
    )
    return path


def _open_config() -> SafetyConfig:
    return SafetyConfig(
        apply_enabled=True,
        published_media_source_url_independence_confirmed=True,
    )


class NormalizeTests(unittest.TestCase):
    def test_accepts_daily_media_and_known_urls(self) -> None:
        self.assertEqual(
            normalize_media_path("waka-threads/2026-11-06/0700_a.png"),
            "waka-threads/2026-11-06/0700_a.png",
        )
        encoded = (
            "https://oguken87-a11y.github.io/pages/waka-threads/"
            "2026-11-06/%E3%82%B9%E3%83%9A%E3%83%83%E3%82%AF.png"
        )
        self.assertEqual(
            normalize_media_path(encoded),
            "waka-threads/2026-11-06/スペック.png",
        )
        raw = (
            "https://raw.githubusercontent.com/oguken87-a11y/pages/main/"
            "waka-threads/2026-11-06/a.mp4"
        )
        self.assertEqual(
            normalize_media_path(raw),
            "waka-threads/2026-11-06/a.mp4",
        )

    def test_rejects_traversal_absolute_and_foreign_urls(self) -> None:
        rejected = [
            "waka-threads/2026-11-06/../../README.md",
            "waka-threads/2026-11-06/../2026-11-06/a.png",
            "/etc/passwd",
            "waka-threads/2026-11-06/..\\..\\README.md",
            "https://evil.example/waka-threads/2026-11-06/a.png",
            "https://oguken87-a11y.github.io/pages/waka-threads/2026-11-06/a.png?raw=1",
            "https://oguken87-a11y.github.io/pages/waka-threads/2026-11-06/%2e%2e/%2e%2e/README.md",
            "waka-threads/2026-11-06/nested/a.png",
            "waka-threads/2026-13-40/a.png",
            "waka-threads/2026-11-06/note.txt",
            "waka-threads/2026-11-06/.hidden.png",
            "index.html",
        ]
        for raw in rejected:
            with self.subTest(raw=raw):
                with self.assertRaises(Exception):
                    normalize_media_path(raw)


class BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self._tempdir())
        _write(self.root, PAST)
        _write(self.root, TODAY)
        _write(self.root, FUTURE)

    def _tempdir(self) -> str:
        import tempfile

        self.addCleanup(lambda: None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

    def _reason(self, now: datetime, rel: str, **kwargs: object) -> str:
        plan = build_plan(self.root, now, None, **kwargs)  # type: ignore[arg-type]
        match = [item for item in plan.decisions if item.path == rel]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0].action, "keep")
        return match[0].reasons[0]

    def test_jst_midnight_keeps_today_and_future(self) -> None:
        just_before = datetime(2026, 11, 6, 23, 59, 59, tzinfo=JST)
        at_midnight = datetime(2026, 11, 7, 0, 0, 0, tzinfo=JST)
        self.assertEqual(
            self._reason(just_before, PAST),
            "jst-today-or-future",
        )
        self.assertEqual(
            self._reason(at_midnight, PAST),
            "ledger-missing-fail-closed",
        )
        self.assertEqual(
            self._reason(at_midnight, TODAY),
            "jst-today-or-future",
        )
        self.assertEqual(
            self._reason(at_midnight, FUTURE),
            "jst-today-or-future",
        )

    def test_utc_evening_is_next_jst_date(self) -> None:
        still_sixth = datetime(2026, 11, 6, 14, 59, 59, tzinfo=UTC)
        seventh = datetime(2026, 11, 6, 15, 0, 0, tzinfo=UTC)
        self.assertEqual(self._reason(still_sixth, PAST), "jst-today-or-future")
        self.assertEqual(self._reason(seventh, PAST), "ledger-missing-fail-closed")
        self.assertEqual(self._reason(seventh, FUTURE), "jst-today-or-future")


class ProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=JST)

    def test_protected_roles_and_future_stay_when_gates_open(self) -> None:
        files = {
            "waka-threads/2026-11-06/pinned.png": ["pinned"],
            "waka-threads/2026-11-06/unpublished.png": ["unpublished"],
            "waka-threads/2026-11-06/skipped.png": ["見送り"],
            "waka-threads/2026-11-06/rescheduled.png": ["振替"],
            "waka-threads/2026-11-06/reused.png": ["再利用"],
            "waka-threads/2026-11-06/shared.png": ["共有資産"],
            "waka-threads/2026-11-06/draft.png": ["draft"],
            "waka-threads/2026-11-06/scheduled.png": ["予約"],
            "waka-threads/2026-11-06/unknown.png": ["unknown"],
            "waka-threads/2026-11-06/both.png": ["published", "固定"],
            "waka-threads/2026-11-06/published.png": ["published"],
            "waka-threads/2026-11-06/omitted.png": None,
            "waka-threads/2026-11-08/future_published.png": ["published"],
        }
        entries = []
        for rel, roles in files.items():
            _write(self.root, rel, rel.encode())
            if roles is not None:
                entries.append({"path": rel, "roles": roles})
        _write(self.root, "README.md", b"site")
        _write(self.root, "waka-threads/2026-11-06/notes.txt", b"nope")
        _write(self.root, "waka-threads/shared/logo.png", b"logo")
        ledger = load_ledger(_ledger(self.root, entries))
        plan = build_plan(
            self.root,
            self.now,
            ledger,
            independence_confirmed=True,
        )
        by_path = {item.path: item for item in plan.decisions}
        self.assertEqual(by_path["waka-threads/2026-11-06/published.png"].action, "delete")
        for rel in files:
            if rel.endswith("published.png") and "/2026-11-06/" in rel:
                continue
            self.assertEqual(by_path[rel].action, "keep", rel)
        self.assertIn(
            "protected-reference:",
            by_path["waka-threads/2026-11-06/both.png"].reasons[0],
        )
        self.assertIn("pinned", by_path["waka-threads/2026-11-06/both.png"].reasons[0])
        self.assertEqual(
            by_path["waka-threads/2026-11-06/omitted.png"].reasons,
            ("not-in-ledger-fail-closed",),
        )
        self.assertEqual(
            by_path["waka-threads/2026-11-08/future_published.png"].reasons,
            ("jst-today-or-future",),
        )
        self.assertEqual(
            by_path["waka-threads/2026-11-06/notes.txt"].reasons,
            ("outside-daily-media-pattern",),
        )
        self.assertEqual(
            by_path["waka-threads/shared/logo.png"].reasons,
            ("outside-daily-media-pattern",),
        )
        self.assertNotIn("README.md", by_path)

    def test_invalid_ledger_deletes_nothing(self) -> None:
        _write(self.root, "waka-threads/2026-11-06/published.png")
        _write(self.root, "README.md", b"keep")
        path = _ledger(
            self.root,
            [
                {
                    "path": "waka-threads/2026-11-06/../../README.md",
                    "roles": ["published"],
                }
            ],
        )
        with self.assertRaises(LedgerInvalid):
            load_ledger(path)
        plan = build_plan(
            self.root,
            self.now,
            None,
            ledger_error="entry 0 path rejected: path must be waka-threads/YYYY-MM-DD/filename",
            independence_confirmed=True,
        )
        self.assertEqual(plan.delete_candidates, [])
        self.assertEqual(plan.decisions[0].reasons, ("ledger-invalid-fail-closed",))
        deleted, _ = apply_deletions(self.root, plan, allowed=True)
        self.assertEqual(deleted, [])
        self.assertTrue((self.root / "README.md").is_file())

    def test_unconfirmed_published_posts_are_kept(self) -> None:
        rel = "waka-threads/2026-11-06/published.png"
        _write(self.root, rel)
        ledger = load_ledger(_ledger(self.root, [{"path": rel, "roles": ["published"]}]))
        plan = build_plan(
            self.root,
            self.now,
            ledger,
            independence_confirmed=False,
        )
        self.assertEqual(plan.decisions[0].action, "keep")
        self.assertEqual(
            plan.decisions[0].reasons,
            ("published-source-url-unconfirmed",),
        )


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = datetime(2026, 11, 7, 9, 0, tzinfo=JST)
        self.past = "waka-threads/2026-11-01/only.png"
        self.future = "waka-threads/2026-11-08/later.png"
        self.pinned = "waka-threads/2026-10-16/pin.png"
        _write(self.root, self.past, b"past")
        _write(self.root, self.future, b"future")
        _write(self.root, self.pinned, b"pin")
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside")
        link = self.root / "waka-threads/2026-11-01/escape.png"
        link.symlink_to(outside)
        self.ledger_path = _ledger(
            self.root,
            [
                {"path": self.past, "roles": ["published"]},
                {"path": self.future, "roles": ["published"]},
                {"path": self.pinned, "roles": ["pinned"]},
                {
                    "path": "https://oguken87-a11y.github.io/pages/" + self.past,
                    "roles": ["published"],
                },
            ],
        )

    def test_default_config_apply_removes_nothing(self) -> None:
        audit, code = run(
            self.root,
            now=self.now,
            config=SafetyConfig(),
            ledger_path=self.ledger_path,
            apply_requested=True,
        )
        self.assertEqual(code, 2)
        self.assertFalse(audit["deletion_allowed"])
        self.assertEqual(audit["summary"]["deleted"], 0)
        self.assertEqual(audit["summary"]["delete_candidates"], 0)
        self.assertTrue((self.root / self.past).is_file())
        self.assertTrue((self.root / "outside.png").is_file())
        self.assertTrue((self.root / "waka-threads/2026-11-01/escape.png").is_symlink())

    def test_open_gates_delete_only_eligible_regular_file(self) -> None:
        audit, code = run(
            self.root,
            now=self.now,
            config=_open_config(),
            ledger_path=self.ledger_path,
            apply_requested=True,
        )
        self.assertEqual(code, 0, audit)
        self.assertTrue(audit["deletion_allowed"])
        self.assertEqual(audit["summary"]["deleted"], 1)
        self.assertFalse((self.root / self.past).exists())
        self.assertTrue((self.root / "waka-threads/2026-11-01").is_dir())
        self.assertTrue((self.root / self.future).is_file())
        self.assertTrue((self.root / self.pinned).is_file())
        self.assertTrue((self.root / "outside.png").is_file())
        kept = {
            item["path"]: item["reasons"]
            for item in audit["items"]
            if item["result"] == "kept"
        }
        self.assertEqual(kept[self.future], ["jst-today-or-future"])
        self.assertEqual(kept[self.pinned], ["protected-reference:pinned"])
        self.assertEqual(kept["waka-threads/2026-11-01/escape.png"], ["symlink-not-eligible"])

    def test_symlink_swap_is_not_unlinked(self) -> None:
        ledger = load_ledger(self.ledger_path)
        plan = build_plan(
            self.root,
            self.now,
            ledger,
            independence_confirmed=True,
        )
        target = self.root / self.past
        target.unlink()
        outside = self.root / "outside.png"
        target.symlink_to(outside)
        deleted, problems = apply_deletions(self.root, plan, allowed=True)
        self.assertEqual(deleted, [])
        self.assertTrue(target.is_symlink())
        self.assertTrue(outside.is_file())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(problems[0]["result"], "refused")

    def test_empty_date_directory_is_removed_after_real_delete(self) -> None:
        root_file = "waka-threads/2026-10-22/solo.png"
        _write(self.root, root_file, b"solo")
        audit, code = run(
            self.root,
            now=self.now,
            config=_open_config(),
            ledger_path=_ledger(
                self.root,
                [{"path": root_file, "roles": ["投稿済"]}],
            ),
            apply_requested=True,
        )
        self.assertEqual(code, 0, audit)
        self.assertFalse((self.root / root_file).exists())
        self.assertFalse((self.root / "waka-threads/2026-10-22").exists())
        self.assertTrue((self.root / self.past).is_file())


class CliAndRepoTests(unittest.TestCase):
    def test_committed_config_is_fail_closed(self) -> None:
        config = load_config(REPO / "config" / "waka-threads-retention.json")
        self.assertFalse(config.apply_enabled)
        self.assertFalse(config.published_media_source_url_independence_confirmed)
        self.assertFalse(CLI_APPLY_ENABLED)

    def test_cli_latch_ignores_config_that_would_allow_deletion(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            rel = "waka-threads/2026-11-01/a.png"
            _write(root, rel, b"keep-me")
            config = root / "open.json"
            config.write_text(
                json.dumps(
                    {
                        "apply_enabled": True,
                        "published_media_source_url_independence_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            ledger = _ledger(root, [{"path": rel, "roles": ["published"]}])
            code = main(
                [
                    "--repo-root",
                    str(root),
                    "--config",
                    str(config),
                    "--ledger",
                    str(ledger),
                    "--now",
                    "2026-11-07T12:00:00+09:00",
                    "--apply",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual((root / rel).read_bytes(), b"keep-me")

    def test_example_ledger_loads_and_does_not_name_real_files(self) -> None:
        ledger = load_ledger(REPO / "config" / "waka-threads-ledger.example.json")
        assert ledger is not None
        for rel in ledger.roles_by_path:
            self.assertTrue(rel.startswith("waka-threads/2099-01-01/"))
            self.assertFalse((REPO / rel).exists())

    def test_cli_apply_on_temp_repo_exits_2(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write(root, "waka-threads/2026-11-01/a.png", b"x")
            config = root / "config.json"
            config.write_text(
                (REPO / "config" / "waka-threads-retention.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            code = main(
                [
                    "--repo-root",
                    str(root),
                    "--config",
                    str(config),
                    "--now",
                    "2026-11-07T12:00:00+09:00",
                    "--apply",
                ]
            )
            self.assertEqual(code, 2)
            self.assertTrue((root / "waka-threads/2026-11-01/a.png").is_file())

    def test_real_repo_dry_run_keeps_everything(self) -> None:
        media = REPO / "waka-threads"
        if not media.is_dir():
            self.skipTest("waka-threads is not present")
        on_disk = [
            path
            for path in media.rglob("*")
            if path.is_file() or path.is_symlink()
        ]
        audit, code = run(
            REPO,
            now=datetime(2026, 11, 7, 12, 0, tzinfo=JST),
            config=load_config(REPO / "config" / "waka-threads-retention.json"),
            ledger_path=None,
            apply_requested=False,
        )
        self.assertEqual(code, 0)
        self.assertFalse(audit["deletion_allowed"])
        summary = audit["summary"]
        self.assertEqual(summary["seen"], len(on_disk))
        self.assertEqual(summary["keep"], len(on_disk))
        self.assertEqual(summary["delete_candidates"], 0)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["bytes_deleted"], 0)
        reasons = {item["path"]: item["reasons"] for item in audit["items"]}
        for path in on_disk:
            rel = path.relative_to(REPO).as_posix()
            folder = rel.split("/")[1]
            self.assertIn(rel, reasons)
            if folder >= "2026-11-07":
                self.assertEqual(reasons[rel], ["jst-today-or-future"])
            else:
                self.assertEqual(reasons[rel], ["ledger-missing-fail-closed"])
            self.assertTrue(path.exists())
        self.assertGreater(summary["bytes_keep"], 0)

    def test_module_help_does_not_delete(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "tools.waka_threads_retention", "--help"],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("deletes nothing", " ".join(completed.stdout.split()))


class ModeBitsTests(unittest.TestCase):
    def test_directory_named_like_a_file_is_not_removed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            folder = root / "waka-threads/2026-11-01/only.png"
            folder.mkdir(parents=True)
            (folder / "inner.png").write_bytes(b"no")
            ledger = load_ledger(
                _ledger(
                    root,
                    [
                        {
                            "path": "waka-threads/2026-11-01/only.png",
                            "roles": ["published"],
                        }
                    ],
                )
            )
            plan = build_plan(
                root,
                datetime(2026, 11, 7, tzinfo=JST),
                ledger,
                independence_confirmed=True,
            )
            self.assertTrue(all(item.action == "keep" for item in plan.decisions))
            deleted, _ = apply_deletions(root, plan, allowed=True)
            self.assertEqual(deleted, [])
            self.assertTrue((folder / "inner.png").is_file())
            self.assertFalse(stat.S_ISLNK(folder.lstat().st_mode))


if __name__ == "__main__":
    unittest.main()
