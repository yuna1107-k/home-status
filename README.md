# Home Status

Raspberry Pi で動かす自宅サーバーの状態を、ブラウザからリアルタイムで確認・操作するダッシュボードです。
Cloudflare Tunnel 経由で公開し、Cloudflare Access とパスキーでログインします。

*A real-time status dashboard for a Raspberry Pi home server, served through Cloudflare Tunnel with Cloudflare Access and passkey login. The UI is in Japanese.*

- 必要なもの: Linux（Raspberry Pi OS / Ubuntu など）、Python 3.10 以上と `python3-cryptography`、Cloudflare で管理しているドメイン、Tailscale
- ドキュメント: [home-status-docs](https://github.com/yuna1107-k/home-status-docs)（セットアップ・設定項目・構成）
- 脆弱性の報告: [SECURITY.md](.github/SECURITY.md)

## ライセンス

MIT License

Copyright (c) 2026 yuna1107

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
