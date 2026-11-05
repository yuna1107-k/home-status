# Home Status

Raspberry Pi で動かす自宅サーバーの状態を、ブラウザからリアルタイムで確認・操作するダッシュボードです。

*A self-hosted, real-time status dashboard for a Raspberry Pi home server. Python standard library + `cryptography` only, served through Cloudflare Tunnel with Cloudflare Access and passkey (WebAuthn) login. The UI is in Japanese.*

- タスクマネージャー風のレイアウトに、半透明・屈折のあるガラス調のデザイン
- 約 2 秒ごとに自動更新（Server-Sent Events）。スマホでは下部のタブバーに切り替わる
- Python 3 の標準ライブラリと [`cryptography`](https://pypi.org/project/cryptography/) だけで動く（フロントエンドも外部ライブラリなし）
- 外部とのやり取りは公式の CLI と公開 API だけ（`tailscale`・`docker`・`systemctl`・`smartctl`・`vcgencmd`、Cloudflare API、ntfy、Nextcloud の `status.php` / `occ`）

## ページ

| ページ | 内容 | 操作 |
|---|---|---|
| ホーム | 全体の状態、発生中のアラート、主な数値、各部の状態、最近の操作、D1 の同期状態 | 各ページへ移動 |
| プロセス | % CPU・メモリ（使用量が多いほど色が濃いヒート表示）・スレッド・PID | 並べ替え・検索 |
| パフォーマンス | CPU（全体 / コア別）・メモリ構成・各ディスクの使用時間と転送速度・ネットワーク・温度・インターネット応答 | グラフの表示範囲 |
| 履歴 | CPU・温度・メモリ・通信量・応答時間の 1 時間〜1 年の推移（D1 から読む） | 期間の切り替え |
| ネットワーク | ルーター・インターネット・DNS・インターフェース、LAN の端末（新しい端末に印）、通信量と応答時間のグラフ | LAN の再スキャン、端末を既知にする・名前を付ける |
| Tailscale | 接続状態、各端末（経路・通信量）、Serve の一覧 | Serve の追加・削除、ログ |
| Nextcloud | Web 応答・メンテナンス・コンテナ（% CPU・メモリ・通信量）、応答時間グラフ | メンテナンス、コンテナのログ・再起動、全体の再起動 |
| サービス | このサイト・Nextcloud・tailscaled・Docker・cloudflared など | 再起動（許可されたもの）、ログ、Pi の再起動 |
| アラート | 発生中のアラートと履歴、通知（ntfy）の設定、監視する項目としきい値 | テスト通知、設定の保存 |
| 操作の記録 | いつ・どの端末（パスキー）から・何をしたか | 検索、古い記録の読み込み |
| システム情報 | 機種・OS、アップデートの有無、ディスクの健康状態、バックアップ、起動と停止の履歴、全バージョン | アップデートの再確認 |
| 設定 | 表示（テーマ・背景の透明度・屈折・アクセントカラー・文字サイズ・グラフ範囲・最初のページ）、パスキー | 表示設定はアカウントに保存 |

Nextcloud を使っていない場合、Nextcloud のページは「応答なし」になります。アラートの「Nextcloud が応答しない」はアラートのページでオフにできます。

## 必要なもの

- Raspberry Pi（Raspberry Pi OS / Ubuntu などの Linux。`/proc` と systemd を使う）
- Python 3.10 以上と `python3-cryptography`
- 自分のドメインを Cloudflare で管理していること（Cloudflare Tunnel と Cloudflare Access を使う）
- Tailscale（Tailscale のページと、「Tailscale がオフのときはオン/オフだけの画面にする」動作のため）
- 任意: Docker Compose で動かす Nextcloud、Cloudflare D1（記録の保存先）、ntfy（スマホへの通知）、smartmontools

## 認証の流れ

入口は `https://<PASSKEY_RP_ID>/` の 1 つだけです（Cloudflare Tunnel 経由）。

1. **Cloudflare Access**: 許可したメールアドレスだけがログインできる（メールのワンタイムコードなど）。サイト側でも JWT を検証する
2. **パスキー**: Face ID / Touch ID / Windows Hello / PIN（本人確認必須）でログイン。セッションは 12 時間
3. 表示内容は Pi の Tailscale の状態で切り替わる
   - **オン**: ステータスボード（閲覧・変更）
   - **オフ**: Tailscale のオン/オフだけの画面（オンにすると自動でボードに切り替わる）

パスキー:
- 最初の 1 つは、Cloudflare Access を通過した本人がログイン画面で登録する。2 つ目以降はボード右上のアカウント表示から追加する
- 登録情報（公開鍵のみ）は `data/passkeys.json`（git 管理外）。全部消すと、次に開いたときに最初の登録からやり直し
- 端末をなくしたら、Pi で `python3 tools/passkey_admin.py list` / `remove <ID>` / `reset`

## セットアップ

以下は、リポジトリを `~/home-status` に置き、サービスを動かすユーザーで作業する前提です。

### 1. 取得と設定

```bash
sudo apt install python3-cryptography
git clone <このリポジトリの URL> ~/home-status && cd ~/home-status
cp .env.example .env
chmod 600 .env
# .env の PASSKEY_RP_ID（サイトのホスト名）・CF_ACCESS_ALLOWED_EMAILS・DISKS などを書き換える
```

### 2. Tailscale

サイトから `tailscale up` / `tailscale down` / `tailscale serve` を実行するため、サービスのユーザーを operator にします。

```bash
sudo tailscale set --operator=$USER
```

### 3. Cloudflare Tunnel と Access

1. Cloudflare Tunnel を作り、`<PASSKEY_RP_ID>` を `http://127.0.0.1:8096` に向ける
   （[公式ドキュメント](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)）
2. Cloudflare の API トークンを `~/.cloudflared/api-token` に保存する（`chmod 600`）
3. Access アプリを作り、`.env` に AUD などを書く:

```bash
python3 tools/cf_access_setup.py   # Access: Apps and Policies の Edit 権限が必要
```

Cloudflare のダッシュボードで手動で作る場合は、Self-hosted アプリを作り、
`CF_ACCESS_TEAM_DOMAIN`（`<team>.cloudflareaccess.com`）と `CF_ACCESS_AUD` を `.env` に書きます。
`CF_ACCESS_*` が未設定の間は、サイトはすべてのリクエストを拒否します。

うまくつながらないときは `python3 tools/cf_diagnose.py`（読み取りのみ）で DNS・Workers ルート・各種ルール・トンネルの状態を一覧できます。

注意: Worker のカスタムドメインで「Preview URLs」を有効にすると `*.<あなたのドメイン>` が Worker に取られ、
トンネル宛ての通信まで Worker 側に吸い込まれます（「There is nothing here yet」になる）。

### 4. サービスとして起動（systemd ユーザーサービス）

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/home-status/home-status.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now home-status
sudo loginctl enable-linger $USER   # ログインしていなくても起動時に立ち上げる

# 状態・ログ・再起動
systemctl --user status home-status
journalctl --user -u home-status -f
systemctl --user restart home-status
```

`home-status.service` は `~/home-status` に置いた前提です。別の場所に置いた場合は、ファイル内のパスを書き換えてください。

LAN（`:8090`）と Tailscale Serve 向け（`127.0.0.1:8092`）のポートは公開 URL へ転送するだけです。
Tailscale 経由でも同じ URL に誘導したい場合: `tailscale serve --bg --https=8443 http://127.0.0.1:8092`

### 5. 任意の設定

#### 記録の保存（Cloudflare D1）と Pi の停止の見張り

履歴・操作の記録・アラート・起動履歴・LAN の端末、それに設定とパスキー（公開鍵）の控えを Cloudflare D1 に保存します。
Pi 側のファイルは控えなので、SD カードが壊れても記録は残ります。

```bash
python3 tools/cloud_setup.py   # D1: Edit と Workers Scripts: Edit の権限が必要
systemctl --user restart home-status
```

- **書き込みは送信待ち（`data/outbox.jsonl`）に積んでから送る。** インターネットや D1 が止まっていても記録は消えず、つながった時点でまとめて送る
- **SD カードへの書き込みを増やさない。** 1 分ごとの計測は D1 に送るだけで、手元には 24 時間分をメモリに持つだけ
- **Pi そのものが止まったら Cloudflare 側から通知する。** Pi は 1 分ごとに D1 の `heartbeat` を更新し、
  Worker `home-status-watchdog`（`watchdog/worker.js`）が 5 分ごとにそれを確認して、10 分止まっていたら ntfy に通知する
- 保存期間: 1 分ごとの計測は 31 日、1 時間ごとの集計・操作の記録・アラート・起動履歴はずっと

Pi を作り直したときは、リポジトリを置いて `.env` と API トークンを用意し、`tools/cloud_setup.py` を実行してから起動すると、
パスキー・表示設定・通知設定が D1 から自動で戻ります。

#### 通知（ntfy）

1. スマホに [ntfy](https://ntfy.sh/) アプリを入れる
2. 「アラート」ページの「購読する URL」のトピック名を、アプリで購読する
3. 「テスト通知」で届くか確認する

トピック名を知っている人は誰でも通知を読めるので、他人に教えないでください（「作り直す」で変えられます）。
監視する項目・しきい値・静かにする時間帯は同じページで変えられます。

#### ディスクの健康状態（SMART）

`deploy/home-status-smartctl.sudoers` の `YOUR_USER` とディスク名を書き換えてから入れます。読み取り（`-a`）だけを許可します。

```bash
sudo apt install smartmontools
sudo install -m 440 deploy/home-status-smartctl.sudoers /etc/sudoers.d/home-status-smartctl
sudo visudo -c
systemctl --user restart home-status
```

#### Pi の再起動をサイトから行う

`deploy/49-home-status-reboot.rules` の `YOUR_USER` を書き換えてから `/etc/polkit-1/rules.d/` に置きます。許可するのは再起動だけです。

## 設定項目

すべて `.env` に書きます。一覧と説明は [`.env.example`](.env.example) を見てください。主なもの:

| 項目 | 既定値 | 内容 |
|---|---|---|
| `PASSKEY_RP_ID` | `localhost` | サイトのホスト名（必須） |
| `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` | なし | Cloudflare Access の検証用（必須） |
| `CF_ACCESS_ALLOWED_EMAILS` | なし | 操作を許可するメールアドレス |
| `DISKS` | `System=/` | 監視するディスク（`表示名=マウント先` をカンマ区切り） |
| `NET_IFACE` | `eth0` | 監視するネットワークインターフェース |
| `EXTRA_SERVICES` | なし | サービスのページに追加する systemd のユニット |
| `BACKUP_DIR` | なし | バックアップの置き場所（最新ファイルの時刻を表示） |
| `NEXTCLOUD_*` | Docker Compose の既定の名前 | Nextcloud の URL・コンテナ名 |

## 構成

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
watchdog/         Pi の停止を見張る Cloudflare Worker
tools/            Cloudflare の準備・診断、パスキーの管理
deploy/           sudoers・polkit の設定の雛形
static/           画面（index.html / app.js / features.js / app.css / login.html / tunnel.html）
tests/            テスト
```

## 開発

```bash
python3 -m unittest discover -s tests
```

サーバーは Linux の `/proc` を読むため、Linux 上で動かします。手元で試すとき:

```bash
python3 server.py --public "" --admin "" --tunnel 127.0.0.1:18096
```

Cloudflare Access の後ろ以外からのアクセスはすべて拒否されるため、画面を開くには Tunnel と Access の設定が必要です。

## ライセンス

[MIT](LICENSE)
