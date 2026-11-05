#!/usr/bin/env python3
"""サイト用の Cloudflare Access アプリを作り、サイトの .env に AUD を書き込む。

- 対象: .env の PASSKEY_RP_ID（サイトのホスト名）
- 許可するメール: .env の CF_ACCESS_ALLOWED_EMAILS（カンマ区切り）
- アプリ: Self-hosted / <ホスト名> / セッション 24 時間
- ポリシー: 許可するメールアドレスだけ Allow（それ以外は全員拒否）
- 既に同じホスト名のアプリがあれば作らず、その AUD を使う
- トークンは ~/.cloudflared/api-token（Access: Apps and Policies の Edit 権限が必要）
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from cloud import ROOT, _env  # noqa: E402

HOST = _env("PASSKEY_RP_ID")
APP_NAME = "Home Status"
ALLOWED_EMAILS = [e.strip() for e in _env("CF_ACCESS_ALLOWED_EMAILS").split(",") if e.strip()]
SESSION = "24h"
TOKEN_FILE = Path(_env("CF_API_TOKEN_FILE") or str(Path.home() / ".cloudflared/api-token"))
ENV_FILE = ROOT / ".env"
API = "https://api.cloudflare.com/client/v4"


def call(token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as ex:
        try:
            data = json.load(ex)
        except Exception:  # noqa: BLE001
            data = {}
        return {"success": False, "status": ex.code, "errors": data.get("errors")}


def find_zone(zones: list[dict], host: str) -> dict | None:
    """host を含むゾーン（一番長く一致するもの）。"""
    hits = [z for z in zones if host == z["name"] or host.endswith("." + z["name"])]
    return max(hits, key=lambda z: len(z["name"]), default=None)


def set_env(values: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items()]
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)


def main() -> int:
    if not HOST or HOST == "localhost":
        print(".env の PASSKEY_RP_ID にサイトのホスト名（例: status.example.com）を書いてください")
        return 1
    if not ALLOWED_EMAILS:
        print(".env の CF_ACCESS_ALLOWED_EMAILS に許可するメールアドレスを書いてください")
        return 1
    token = TOKEN_FILE.read_text().strip()
    zone = find_zone(call(token, "GET", "/zones?per_page=50").get("result") or [], HOST)
    if zone is None:
        print(f"{HOST} を含むゾーンが見つかりません（トークンにゾーンの読み取り権限が必要）")
        return 1
    acct = zone["account"]["id"]

    org = call(token, "GET", f"/accounts/{acct}/access/organizations")
    team = org["result"]["auth_domain"]
    print(f"チームドメイン: {team}")

    apps = call(token, "GET", f"/accounts/{acct}/access/apps")
    existing = [a for a in apps.get("result", []) if a.get("domain") == HOST
                or any(d.get("uri") == HOST for d in a.get("destinations") or [])]
    if existing:
        app = existing[0]
        print(f"既存のアプリを使います: {app['name']}")
    else:
        policy = call(token, "POST", f"/accounts/{acct}/access/policies", {
            "name": f"{APP_NAME}: 許可メールのみ",
            "decision": "allow",
            "include": [{"email": {"email": e}} for e in ALLOWED_EMAILS],
        })
        if not policy.get("success"):
            print(f"ポリシーを作れません（トークンに Access: Apps and Policies の Edit 権限が必要）: "
                  f"{policy.get('status')} {policy.get('errors')}")
            return 1
        created = call(token, "POST", f"/accounts/{acct}/access/apps", {
            "name": APP_NAME,
            "type": "self_hosted",
            "domain": HOST,
            "destinations": [{"type": "public", "uri": HOST}],
            "session_duration": SESSION,
            "app_launcher_visible": False,
            "policies": [{"id": policy["result"]["id"], "precedence": 1}],
        })
        if not created.get("success"):
            print(f"アプリを作れません: {created.get('status')} {created.get('errors')}")
            return 1
        app = created["result"]
        print(f"アプリを作成しました: {app['name']}（{HOST}、セッション {SESSION}）")
        print(f"許可するメール: {', '.join(ALLOWED_EMAILS)}")

    set_env({
        "CF_ACCESS_TEAM_DOMAIN": team,
        "CF_ACCESS_AUD": app["aud"],
        "CF_ACCESS_ALLOWED_EMAILS": ",".join(ALLOWED_EMAILS),
    })
    print(f".env に CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUD / CF_ACCESS_ALLOWED_EMAILS を書き込みました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
