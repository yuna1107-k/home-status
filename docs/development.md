# 開発

## テスト

```bash
python3 -m unittest discover -s tests
```

`cryptography` が必要です（`sudo apt install python3-cryptography` か `pip install cryptography`）。

## 手元での起動

サーバーは Linux の `/proc` を読むため、Linux 上で動かします。

```bash
python3 src/server.py --public "" --admin "" --tunnel 127.0.0.1:18096
```

`--public` / `--admin` に空文字を渡すと、その待ち受けを無効にします。

Cloudflare Access の後ろ以外からのアクセスはすべて拒否されるため、画面を開くには Tunnel と Access の設定が必要です
（[セットアップ](setup.md)）。

## 方針

- 依存は Python の標準ライブラリと `cryptography` だけにする。フロントエンドも外部ライブラリを使わない
- 外部とのやり取りは公式の CLI と公開 API だけにする（非公開のエンドポイントや非公式のライブラリは使わない）
- 新しい操作は `src/actions.py` の `ACTIONS` に追加する。シェルは使わず、引数はすべて検証してからコマンドに渡す
