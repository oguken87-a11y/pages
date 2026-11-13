# Waka塾 Threads 日次メディアの保存期間

調査日: 2026-11-07。対象コミット: `2a988b328b8035836429bb7dbce4e3927f4112da`。

この手順は監査と、将来条件が揃ったときの削除判定だけを扱う。この変更では本番の画像を消さない。main へマージしない。Pages の再公開やデプロイ設定の変更をしない。Git 履歴の書き換えと force push をしない。

## 結論

前日以前の日次メディアを、日付だけを理由に削除するのは安全だと確認できていない。削除自動化は有効化していない。

確認できたこと:

- Threads の公式ドキュメントは、公開の試行時にこちらが渡した URL からメディアを取得すると書いている。
- 取得後、公開済み投稿が元 URL を参照し続けるかは書かれていない。したがって「公開済みなら元 URL を消してよい」とは断定しない。
- 投稿台帳（固定、未投稿、見送り、振替、再利用、共有）は Apps Script 側にあり、このリポジトリには無い。台帳が無いファイルは残す。

## 公式ドキュメントで確認した範囲

参照したページ（2026-11-07 に取得）:

- [Threads Get Started](https://developers.facebook.com/docs/threads/get-started)
- [Threads Posts](https://developers.facebook.com/docs/threads/posts)
- [Threads API Troubleshooting](https://developers.facebook.com/docs/threads/troubleshooting)

Get Started の Public Server:

> We download media used in publishing attempts so the media must be hosted on a publicly accessible server at the time of the attempt.

Posts の `image_url` / `video_url`:

> Threads retrieves your image from the URL provided, so it must be on a public server.

> Threads retrieves your video from the URL provided, so it must be on a public server.

投稿は二段階で、`POST /{threads-user-id}/threads` でコンテナを作り、`POST /{threads-user-id}/threads_publish` で公開する。コンテナ作成時のメディアは「your public server」上の画像または動画とされている。

Troubleshooting では、動画コンテナの状態として次が返る。

- `IN_PROGRESS`: 公開処理がまだ続いている
- `FINISHED`: コンテナとメディアオブジェクトの公開準備ができた
- `PUBLISHED`: コンテナのメディアオブジェクトが公開された
- `EXPIRED`: 24 時間以内に公開されず期限切れ
- 失敗時の `FAILED_DOWNLOADING_VIDEO`

ここから言えるのは、公開試行の時点で URL が公開サーバ上にあり、Meta がそこから取得できる必要がある、ということまで。`PUBLISHED` のあと元 URL が 404 になっても投稿の表示が維持される、という記述は無い。第三者の実装メモは根拠にしない。

このリポジトリの運用は、Apps Script が `image_url` を Meta API に渡す形である。渡す時刻が予約時刻なら、その時刻まで URL が必要になる。未投稿、見送り、振替、再利用は過去日付のフォルダをまだ参照し得る。その一覧はリポジトリ内に無い。

## 容量

2026-11-07 時点の計測。削除はしていない。

| 範囲 | サイズ |
| --- | --- |
| `waka-threads/` のファイル 99 件 | 161,817,524 bytes |
| うち 2026-11-07 より前（94 件） | 155,787,031 bytes |
| 2026-11-07 当日 | 0 |
| 2026-11-08（5 件、未来側） | 6,030,493 bytes |
| `.git` を除く作業ツリー全体 | 約 345 MiB（`waka-threads` 以外は 199,743,916 bytes） |
| Git パック / GitHub `diskUsage` | 327.84 MiB / 335,682 KB |
| 履歴に残る `waka-threads` のユニーク blob | 150,769,995 bytes（93 blob） |

区別:

- 先端のファイルを消すと、次回の公開が成功したときに Pages が配るツリーからはその分が減る。上の「前日以前」は作業ツリーの約 149 MiB で、作業ツリー全体の約 4 割。
- 同じ削除コミットでは Git 履歴の容量は減らない。blob は過去コミットから辿れる。GitHub 上のリポジトリサイズも減らず、削除コミットの分だけ増えることがある。履歴を縮めるには履歴書き換えが必要で、この作業では行わない。
- したがって、今回のタイムアウトがクローンや履歴の大きさによるものなら、先端のファイル削除では解消しない。公開物のアップロード量によるものなら、次回の成功した公開でツリー分は減る。どちらが原因かは、この調査では切り分けていない。

## 2026-11-08 の公開失敗（未修正）

この調査ではデプロイの再実行も設定変更もしない。

- `main` の `waka-threads/2026-11-08/` に画像 5 件がある（コミット `2a988b3`、2026-11-07 12:02 JST）。
- `https://raw.githubusercontent.com/oguken87-a11y/pages/main/waka-threads/2026-11-08/0700_0700_kiku.png` は 200。
- `https://oguken87-a11y.github.io/pages/waka-threads/2026-11-08/0700_0700_kiku.png` は 404。
- `https://oguken87-a11y.github.io/pages/waka-threads/2026-11-06/0700_0700_seikai.png` とサイト直下は 200。応答の `Last-Modified` はどちらも `Thu, 05 Nov 2026 16:39:51 GMT`。
- Pages の builds API はこのトークンでは 404 で、失敗ログ自体は取得していない。

11/08 は基準日 2026-11-07 では未来日なので、保持対象でもある。予約を文字投稿へ落とした事実は、このリポジトリの台帳では確認できない。

## ツールの動作

```bash
python3 -m tools.waka_threads_retention \
  --repo-root . \
  --now 2026-11-07T12:00:00+09:00 \
  --output /tmp/waka-threads-retention-audit.json
```

既定は dry-run。上記の実行結果（2026-11-07 12:00 JST）:

- 走査 99 / 保持 99 / 削除候補 0 / 削除 0
- 保持 161,817,524 bytes
- 理由: `jst-today-or-future` 5 件（すべて `2026-11-08`）、`ledger-missing-fail-closed` 94 件
- 終了コード 0

`--apply` を付けても、現在は終了コード 2 で何も消さない。次の両方が閉じている。

- `config/waka-threads-retention.json` の `apply_enabled` と `published_media_source_url_independence_confirmed` は `false`
- `tools/waka_threads_retention.py` の `CLI_APPLY_ENABLED` は `False`。JSON を true に変えても CLI は削除しない

GitHub Actions は追加していない。

判定ルール:

1. 対象は `waka-threads/YYYY-MM-DD/ファイル` の通常ファイルだけ。拡張子は png, jpg, jpeg, gif, webp, mp4, mov, m4v。
2. それ以外（他フォルダ、共有置き場、未知の拡張子、隠しファイル、シンボリックリンク、ディレクトリ）は残す。
3. フォルダ日付が日本時間の当日または未来なら残す。日付の境界は `Asia/Tokyo` の 0 時。
4. 台帳が無い、読めない、形式が違う、パスに `..` や想定外の URL が混ざる場合は、過去分もすべて残す。
5. 台帳にあっても、役割が `published` だけではないものは残す。固定、未投稿、下書き、予約、見送り、振替、再利用、共有、不明、未知の役割は残す。日本語の役割名も同じ扱いにする。
6. 台帳に無い過去ファイルは残す。載っていないことを削除許可とは扱わない。
7. `published` だけの過去ファイルも、元 URL 非依存が確認されるまで残す。
8. 削除実装は、上記をすべて通過した通常ファイルだけを unlink する。リンクや、解決先が日付フォルダの外に出るパスは unlink しない。

台帳の形は `config/waka-threads-ledger.example.json`。これは例であり、読み込み対象ではない。パスはリポジトリ相対か、次の前置辞付き URL。

- `https://oguken87-a11y.github.io/pages/`
- `https://raw.githubusercontent.com/oguken87-a11y/pages/main/`

クエリ文字列付き URL は拒否する。拒否が 1 件でもある台帳は全体を不信頼とする。

## 削除を有効化する前に必要なもの

この PR では満たしていない。

1. Apps Script またはスプレッドシートから、画像・動画ごとの役割を機械出力する。固定、未投稿、見送り、振替、再利用、共有、投稿済みを区別し、`image_url` を上のパス形式にする。手作業で「全部 published」と書いた台帳は使わない。
2. 公開済み投稿が、コンテナの取得完了後に元 URL へ依存しないことが公式に確認できること。現状のドキュメントでは未確認なので、`published_media_source_url_independence_confirmed` を true にしない。
3. その確認のあとで、`CLI_APPLY_ENABLED` と `apply_enabled` を変える別の変更を出す。本番で `--apply` する前に dry-run の監査 JSON で、削除対象と保持理由を目視する。
4. 履歴容量を減らす必要が出た場合は、このツールでは行わない。履歴書き換えは別判断とし、force push をこの手順に含めない。

## テスト

```bash
python3 -m unittest discover -s tests -v
```

一時ディレクトリで、日本時間の日付境界、パストラバーサル、保護役割、未来日、シンボリックリンク、CLI のラッチを確認する。実リポジトリに対するテストは dry-run の読み取りだけで、削除件数が 0 であることを確認する。
