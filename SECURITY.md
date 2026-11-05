# セキュリティ

## 脆弱性の報告

脆弱性を見つけた場合は、公開の Issue には書かず、GitHub の
[Private vulnerability reporting](../../security/advisories/new)（Security タブ →「Report a vulnerability」）から報告してください。

## 設計上の前提

- サイトは Cloudflare Tunnel 経由でだけ公開し、入口で Cloudflare Access による認証を必須にします。
  サイト側でも Access の JWT（署名・aud・発行元・期限・メール）を検証し、`.env` の `CF_ACCESS_*` が未設定ならすべて拒否します。
- Access を通過したうえで、さらにパスキー（WebAuthn、本人確認必須）でログインします。
- 操作 API は JSON と独自ヘッダー（`X-Requested-With: home-status`）付きのリクエストだけ受け付けます（CSRF 対策）。
  Cookie は `Secure; HttpOnly; SameSite=Strict` です。
- 実行できる操作は `actions.py` に定義したものだけです。シェルは使わず、引数はすべて検証してからコマンドに渡します。
- `.env` と `data/`（パスキーの公開鍵、通知のトピック名、設定）はコミットしないでください。
