# 任意の機能

## 記録の保存（Cloudflare D1）と Pi の停止の見張り

履歴・操作の記録・アラート・起動履歴・LAN の端末、それに設定とパスキー（公開鍵）の控えを Cloudflare D1 に保存します。
Pi 側のファイルは控えなので、SD カードが壊れても記録は残ります。

```bash
python3 src/tools/cloud_setup.py   # D1: Edit と Workers Scripts: Edit の権限が必要
systemctl --user restart home-status
```

このスクリプトは何度実行しても同じ結果になります。行うこと:

1. D1 データベース `home-status` が無ければ作る
2. `.env` に `CF_ACCOUNT_ID` / `D1_DATABASE_ID` を書く
3. テーブルを作る
4. 見張りの Worker `home-status-watchdog` を配置し、5 分ごとに動かす（workers.dev の URL は無効にする）

動き:

- **書き込みは送信待ち（`data/outbox.jsonl`）に積んでから送る。** インターネットや D1 が止まっていても記録は消えず、つながった時点でまとめて送る。同じ記録を 2 回送っても重複しない
- **SD カードへの書き込みを増やさない。** 1 分ごとの計測は D1 に送るだけで、手元には 24 時間分をメモリに持つだけ
- **Pi そのものが止まったら Cloudflare 側から通知する。** Pi は 1 分ごとに D1 の `heartbeat` を更新し、
  Worker が 5 分ごとにそれを確認して、10 分止まっていたら ntfy に「応答がありません」、戻ったら「復旧しました」を送る
- **D1 自体は 30 日前までの任意の時点に戻せる**（Cloudflare の Time Travel）
- 保存期間: 1 分ごとの計測は 31 日、1 時間ごとの集計・操作の記録・アラート・起動履歴はずっと

Pi を作り直したときは、リポジトリを置いて `.env` と API トークンを用意し、`src/tools/cloud_setup.py` を実行してから起動すると、
パスキー・表示設定・通知設定が D1 から自動で戻ります。パスキーの登録や通知アプリの購読をやり直す必要はありません。

## 通知（ntfy）

1. スマホに [ntfy](https://ntfy.sh/) アプリを入れる
2. 「アラート」ページの「購読する URL」のトピック名を、アプリで購読する
3. 「テスト通知」で届くか確認する

トピック名を知っている人は誰でも通知を読めるので、他人に教えないでください（「作り直す」で変えられます）。
監視する項目・しきい値・静かにする時間帯は同じページで変えられます。

見張りの Worker から ntfy.sh に送る場合は、ntfy のアクセストークン（`tk_…`）を設定してください。
ntfy.sh は送信元 IP ごとに 1 日の上限があり、Workers の IP は共有のため上限に当たることがあります。

主な監視項目: CPU 温度、ディスクの空き・未マウント、メモリ、Nextcloud の応答、サービス・コンテナの停止、Tailscale の切断、
インターネットの切断、電圧低下、ディスクの健康状態、LAN に知らない端末がつながったとき。
バックアップの古さとセキュリティ更新は、既定ではオフです。

## ディスクの健康状態（SMART）

SMART は root でしか読めないため、読み取り（`-a`）だけを許可する設定を入れます。
`deploy/home-status-smartctl.sudoers` の `YOUR_USER` とディスク名（`.env` の `SMART_DISKS` と揃える）を書き換えてから:

```bash
sudo apt install smartmontools
sudo install -m 440 deploy/home-status-smartctl.sudoers /etc/sudoers.d/home-status-smartctl
sudo visudo -c
systemctl --user restart home-status
```

入れていない間は「未設定」と表示し、ファイルシステムのエラー数とカーネルの読み書きエラーだけを見ます。SD カードには SMART がありません。

## Pi の再起動をサイトから行う

`deploy/49-home-status-reboot.rules` の `YOUR_USER` を書き換えてから `/etc/polkit-1/rules.d/` に置きます。許可するのは再起動だけです（シャットダウンなどは含めない）。

## バックアップの表示

`.env` の `BACKUP_DIR` に指定したディレクトリの中で、一番新しいファイルの時刻を「最後のバックアップ」として表示します。
未設定なら「バックアップは設定されていません」と表示します。
