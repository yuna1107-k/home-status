# 構成

## ディレクトリ

```
.github/SECURITY.md   脆弱性の報告方法
docs/                 ドキュメント
deploy/               systemd のサービス、.env の雛形、sudoers・polkit の設定の雛形
src/                  本体
tests/                テスト
```

`.env` と `data/`（実行中に作られる）はリポジトリのルートに置かれ、どちらも git 管理外です。

## src/

```
server.py         HTTP サーバー（本体 8096 / 転送 8090・8092）と配信
passkey.py        パスキー（WebAuthn）の登録・検証とセッション
auth.py           Cloudflare Access の JWT の検証
sampler.py        2 秒・10 秒ごとの計測とグラフ用の履歴
collectors.py     各ページの表示データ
versions.py       バージョン情報（10 分キャッシュ）
actions.py        操作（ここに書いたものだけ実行できる）
cloud.py          D1 への保存（送信待ち・再送）と、設定・パスキーの控えの復元
settings.py       通知・アラートの設定（D1 にも控える）
prefs.py          表示設定（アカウントごと）
alerts.py         異常の検出（続いたら通知、消えたら復旧）
notify.py         ntfy への通知（つながらない間はためておく）
history.py        1 分ごとの計測の保存・集計と生存確認（heartbeat）
audit.py          操作の記録
health.py         アップデート・ディスクの健康状態・バックアップ
lan.py            LAN の端末の記録と、知らない端末の検出
boots.py          起動と停止の履歴（停止の仕方の推定）
static/           画面（index.html / app.js / features.js / wall.js / app.css / login.html / tunnel.html）
tools/            Cloudflare の準備・診断、パスキーの管理
watchdog/         Pi の停止を見張る Cloudflare Worker
```

## ポート

| 待ち受け | 役割 |
|---|---|
| `127.0.0.1:8096` | 本体。Cloudflare Tunnel から届く。Cloudflare Access → パスキーの順に認証 |
| `0.0.0.0:8090` | LAN 向け。公開 URL へ転送するだけ |
| `127.0.0.1:8092` | Tailscale Serve 向け。公開 URL へ転送するだけ |

## データの保存先

| データ | 手元（`data/`） | Cloudflare D1 |
|---|---|---|
| 1 分ごとの計測 | メモリに 24 時間分 | `metrics_1m`（31 日） |
| 1 時間ごとの集計 | なし | `metrics_1h`（ずっと） |
| 生存確認 | なし | `heartbeat` |
| 操作の記録 | なし | `audit` |
| アラート | `alerts_state.json`（直近 200 件） | `alerts` |
| 起動と停止の履歴 | なし | `boots` |
| LAN の端末 | `lan_devices.json` | `lan_devices` |
| 設定・表示設定・パスキー（公開鍵） | `settings.json` / `prefs.json` / `passkeys.json` | `kv`（控え） |
| 送信待ち | `outbox.jsonl` | — |
