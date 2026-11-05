#!/usr/bin/env python3
"""Cloudflare 側の準備をまとめて行う（何度実行しても同じ結果になる）。

  1. D1 データベース「home-status」が無ければ作る
  2. .env に CF_ACCOUNT_ID / D1_DATABASE_ID を書く
  3. テーブルを作る
  4. 見張りの Worker「home-status-watchdog」を配置し、5 分ごとに動かす

使うトークン: ~/.cloudflared/api-token（CF_API_TOKEN_FILE で変更可）
必要な権限: アカウントの「D1: 編集」「Workers Scripts: 編集」
（.env に CF_ACCOUNT_ID が無く、トークンが複数のアカウントを見られるときは CF_ACCOUNT_ID を書いておく）

Pi を作り直したとき: リポジトリを置き、トークンを保存してからこれを実行し、
サービスを起動すると、パスキー・表示設定・通知設定が D1 から自動で戻る。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cloud import _env  # noqa: E402

API = "https://api.cloudflare.com/client/v4"
DB_NAME = "home-status"
WORKER_NAME = "home-status-watchdog"
WORKER_FILE = ROOT / "watchdog/worker.js"
CRON = "*/5 * * * *"
ENV_FILE = ROOT / ".env"


def token() -> str:
    path = Path(_env("CF_API_TOKEN_FILE") or str(Path.home() / ".cloudflared/api-token"))
    try:
        return path.read_text().strip()
    except OSError:
        sys.exit(f"{path} がありません。先に API トークンを保存してください")


def call(method: str, path: str, body: object = None, *, raw: bytes | None = None, ctype: str = "application/json") -> dict:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token()}", "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as ex:
        try:
            errors = json.load(ex).get("errors")
        except Exception:  # noqa: BLE001
            errors = None
        sys.exit(f"{method} {path} が失敗しました ({ex.code}): {errors}")


def set_env(key: str, value: str) -> None:
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)


def main() -> int:
    account = _env("CF_ACCOUNT_ID")
    if not account:
        accounts = call("GET", "/accounts")["result"] or []
        if len(accounts) != 1:
            sys.exit("アカウントを 1 つに決められません。.env の CF_ACCOUNT_ID にアカウント ID を書いてください")
        account = accounts[0]["id"]

    dbs = call("GET", f"/accounts/{account}/d1/database?name={DB_NAME}")["result"] or []
    db = next((d for d in dbs if d["name"] == DB_NAME), None)
    if db is None:
        body = {"name": DB_NAME}
        if hint := _env("D1_LOCATION_HINT"):  # 例: apac / weur / enam
            body["primary_location_hint"] = hint
        db = call("POST", f"/accounts/{account}/d1/database", body)["result"]
        print(f"D1 を作成しました: {DB_NAME}")
    db_id = db["uuid"]
    print(f"D1: {DB_NAME} ({db_id})")

    set_env("CF_ACCOUNT_ID", account)
    set_env("D1_DATABASE_ID", db_id)
    os.environ["CF_ACCOUNT_ID"], os.environ["D1_DATABASE_ID"] = account, db_id
    print(".env を更新しました")

    import cloud  # .env を書いた後に読む
    cloud.OUTBOX_Q._ensure_schema()
    print("テーブルを作成しました")

    boundary = uuid.uuid4().hex
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": "2026-10-01",
        "bindings": [
            {"type": "d1", "name": "DB", "id": db_id},
            # 通知をタップしたときに開く URL と、通知に書く時刻のタイムゾーン
            {"type": "plain_text", "name": "PUBLIC_URL", "text": f"https://{_env('PASSKEY_RP_ID') or 'localhost'}/"},
            {"type": "plain_text", "name": "TIME_ZONE", "text": _env("TIME_ZONE") or "Asia/Tokyo"},
        ],
    }
    parts = [
        ("metadata", "metadata.json", "application/json", json.dumps(metadata).encode()),
        ("worker.js", "worker.js", "application/javascript+module", WORKER_FILE.read_bytes()),
    ]
    body = b""
    for name, filename, ctype, content in parts:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: {ctype}\r\n\r\n").encode() + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    call("PUT", f"/accounts/{account}/workers/scripts/{WORKER_NAME}", raw=body,
         ctype=f"multipart/form-data; boundary={boundary}")
    call("PUT", f"/accounts/{account}/workers/scripts/{WORKER_NAME}/schedules", [{"cron": CRON}])
    # 外から呼べる URL（workers.dev）は使わない
    call("POST", f"/accounts/{account}/workers/scripts/{WORKER_NAME}/subdomain", {"enabled": False, "previews_enabled": False})
    print(f"Worker {WORKER_NAME} を配置しました（{CRON} に実行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
