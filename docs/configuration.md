# 設定項目

すべてリポジトリのルートの `.env` に書きます。雛形は [`deploy/env.example`](../deploy/env.example) です。

## サイトと認証

| 項目 | 既定値 | 内容 |
|---|---|---|
| `PASSKEY_RP_ID` | `localhost` | サイトのホスト名（必須）。パスキーはこのホスト名に結び付く |
| `PASSKEY_SESSION_SECONDS` | `43200` | パスキーでログインしたセッションの長さ（秒） |
| `CF_ACCESS_TEAM_DOMAIN` | なし | Zero Trust のチームドメイン（必須。`src/tools/cf_access_setup.py` が書く） |
| `CF_ACCESS_AUD` | なし | Access アプリの Application Audience (AUD) Tag（必須。同上） |
| `CF_ACCESS_ALLOWED_EMAILS` | なし | 操作を許可するメールアドレス（カンマ区切り） |

## 共通アカウント（省略可）

複数のサイトで共通のパスキーとアカウントを使う認証基盤（D1 にユーザー・パスキー・サービス権限を持つもの）があるときの設定です。
`AUTH_D1_DATABASE_ID` と `COMMON_PASSKEY_RP_ID` の両方を書くと有効になり、次のように動きます。

- ログインはまず共通パスキー、無ければこのサイトのパスキーを使う
- ログイン時と、ログイン中も 1 分ごとに、共通アカウントで利用が許可されているか（有効なユーザーで、管理者か、このサイトの利用が有効）を確かめ、取り消されていればログアウトする
- このサイトのパスキーでログインしたら、共通パスキーへの切り替え（共通アカウントへのパスキーの追加）を勧める
- このサイトで追加するパスキーはすべて共通パスキーになり、共通アカウントに保存される（このサイト専用のパスキーは新しく作らない。作ってあるものは引き続き使える）
- 共通パスキーをこのサイトで削除すると、共通アカウントからも削除される（すべてのサービスで使えなくなる）
- このサイトにはアカウントを作る機能は無い（共通アカウントのユーザーは作らない）

認証基盤の D1 には、下の「Cloudflare D1」と同じ API トークンで D1 の HTTP API から接続します（同じアカウントの D1 に対する編集の権限が必要。パスキーの追加・削除と監査ログで書き込む）。

| 項目 | 既定値 | 内容 |
|---|---|---|
| `AUTH_D1_DATABASE_ID` | なし | 認証基盤の D1 データベースの ID |
| `COMMON_PASSKEY_RP_ID` | なし | 共通パスキーの RP ID（サイトのホスト名の親ドメイン。例: `example.com`） |
| `AUTH_SERVICE_ID` | `status` | 認証基盤でのこのサイトのサービス ID |
| `AUTH_SSO_URL` | なし | 認証基盤の URL（例: `https://auth.example.com`）。書くと `/auth/sso/login` から SSO でログインできる |

## Cloudflare D1

| 項目 | 既定値 | 内容 |
|---|---|---|
| `CF_ACCOUNT_ID` | なし | アカウント ID（`src/tools/cloud_setup.py` が書く） |
| `D1_DATABASE_ID` | なし | D1 データベースの ID（同上） |
| `CF_API_TOKEN_FILE` | `~/.cloudflared/api-token` | API トークンのファイル |
| `D1_LOCATION_HINT` | なし | D1 を作るときの場所のヒント（`apac` / `weur` / `enam` など） |
| `TIME_ZONE` | `Asia/Tokyo` | 見張りの Worker が通知に書く時刻のタイムゾーン |

## 監視する対象

| 項目 | 既定値 | 内容 |
|---|---|---|
| `DISKS` | `System=/` | 監視するディスク（`表示名=マウント先` をカンマ区切り。例: `System=/,Data=/mnt/data`） |
| `NET_IFACE` | `eth0` | 監視するネットワークインターフェース |
| `INTERNET_HOST` | `1.1.1.1` | インターネットの疎通確認に ping する相手 |
| `DNS_TEST_NAME` | `www.google.com` | 名前解決の確認に使うホスト名 |
| `EXTRA_SERVICES` | なし | サービスのページに追加で表示する systemd のユニット（カンマ区切り） |
| `SMART_DISKS` | `sda` | SMART を読むディスク（`deploy/home-status-smartctl.sudoers` と揃える） |
| `BACKUP_DIR` | なし | バックアップを置いているディレクトリ（一番新しいファイルの時刻を表示） |

## Nextcloud（Docker Compose）

| 項目 | 既定値 | 内容 |
|---|---|---|
| `NEXTCLOUD_URL` | `http://127.0.0.1:8080/status.php` | 応答を確認する URL |
| `NEXTCLOUD_COMPOSE_PROJECT` | `nextcloud` | Docker Compose のプロジェクト名 |
| `NEXTCLOUD_APP_CONTAINER` | `nextcloud-app-1` | アプリのコンテナ名（`occ` を実行する） |
| `NEXTCLOUD_DB_CONTAINER` | `nextcloud-db-1` | DB のコンテナ名（バージョン表示用） |
| `NEXTCLOUD_REDIS_CONTAINER` | `nextcloud-redis-1` | Redis のコンテナ名（バージョン表示用） |
| `NEXTCLOUD_DATA_DIR` | なし | データを置いているマウント先（使用量を表示） |

## 保存先（通常は変更不要）

| 項目 | 既定値 |
|---|---|
| `PASSKEY_STORE` | `data/passkeys.json` |
| `PREFS_STORE` | `data/prefs.json` |
| `SETTINGS_STORE` | `data/settings.json` |
| `ALERT_STATE` | `data/alerts_state.json` |
| `CLOUD_OUTBOX` | `data/outbox.jsonl` |
| `LAN_CACHE` | `data/lan_devices.json` |
