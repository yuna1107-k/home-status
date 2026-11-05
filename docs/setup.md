# セットアップ

## 必要なもの

- Raspberry Pi（Raspberry Pi OS / Ubuntu などの Linux。`/proc` と systemd を使う）
- Python 3.10 以上と `python3-cryptography`
- 自分のドメインを Cloudflare で管理していること（Cloudflare Tunnel と Cloudflare Access を使う）
- Tailscale（Tailscale のページと、「Tailscale がオフのときはオン/オフだけの画面にする」動作のため）
- 任意: Docker Compose で動かす Nextcloud、Cloudflare D1、ntfy、smartmontools（[任意の機能](optional.md)）

以下は、リポジトリを `~/home-status` に置き、サービスを動かすユーザーで作業する前提です。

## 1. 取得と設定

```bash
sudo apt install python3-cryptography
git clone https://github.com/yuna1107-k/home-status.git ~/home-status && cd ~/home-status
cp deploy/env.example .env
chmod 600 .env
# .env の PASSKEY_RP_ID（サイトのホスト名）・CF_ACCESS_ALLOWED_EMAILS・DISKS などを書き換える
```

`.env` と、実行中に作られる `data/` はリポジトリのルートに置かれます（どちらも git 管理外）。項目は [設定項目](configuration.md) を見てください。

## 2. Tailscale

サイトから `tailscale up` / `tailscale down` / `tailscale serve` を実行するため、サービスのユーザーを operator にします。

```bash
sudo tailscale set --operator=$USER
```

## 3. Cloudflare Tunnel と Access

1. Cloudflare Tunnel を作り、`<PASSKEY_RP_ID>` を `http://127.0.0.1:8096` に向ける
   （[Cloudflare のドキュメント](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)）
2. Cloudflare の API トークンを `~/.cloudflared/api-token` に保存する（`chmod 600`）
3. Access アプリを作り、`.env` に AUD などを書く:

```bash
python3 src/tools/cf_access_setup.py   # Access: Apps and Policies の Edit 権限が必要
```

Cloudflare のダッシュボードで手動で作る場合は、Self-hosted アプリを作り、
`CF_ACCESS_TEAM_DOMAIN`（`<team>.cloudflareaccess.com`）と `CF_ACCESS_AUD` を `.env` に書きます。
`CF_ACCESS_*` が未設定の間は、サイトはすべてのリクエストを拒否します。

うまくつながらないときは `python3 src/tools/cf_diagnose.py`（読み取りのみ）で DNS・Workers ルート・各種ルール・トンネルの状態を一覧できます。

注意: Worker のカスタムドメインで「Preview URLs」を有効にすると `*.<あなたのドメイン>` が Worker に取られ、
トンネル宛ての通信まで Worker 側に吸い込まれます（「There is nothing here yet」になる）。

## 4. サービスとして起動（systemd ユーザーサービス）

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/home-status/deploy/home-status.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now home-status
sudo loginctl enable-linger $USER   # ログインしていなくても起動時に立ち上げる

# 状態・ログ・再起動
systemctl --user status home-status
journalctl --user -u home-status -f
systemctl --user restart home-status
```

`deploy/home-status.service` は `~/home-status` に置いた前提です。別の場所に置いた場合は、ファイル内のパスを書き換えてください。

LAN（`:8090`）と Tailscale Serve 向け（`127.0.0.1:8092`）のポートは公開 URL へ転送するだけです。
Tailscale 経由でも同じ URL に誘導したい場合: `tailscale serve --bg --https=8443 http://127.0.0.1:8092`

常時稼働:

- `Restart=always` と `StartLimitIntervalSec=0` で、落ちても必ず立ち上げ直す
- linger を有効にしておけば、Pi の再起動後も自動で立ち上がる

## 次に

- 記録の保存・通知・ディスクの健康状態などは [任意の機能](optional.md)
