#!/usr/bin/env python3
"""サイト（.env の PASSKEY_RP_ID）がトンネルに届かない原因を調べる（読み取り専用）。

~/.cloudflared/api-token（CF_API_TOKEN_FILE で変更可）の API トークン（Read all resources 推奨）で、
ゾーンの DNS・Workers ルート・各種ルール・Worker のカスタムドメインを一覧する。
設定は一切変更しない。トークンの中身は表示しない。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloud import _env  # noqa: E402

HOST = _env("PASSKEY_RP_ID")
TOKEN_FILE = Path(_env("CF_API_TOKEN_FILE") or str(Path.home() / ".cloudflared/api-token"))
API = "https://api.cloudflare.com/client/v4"


def get(token: str, path: str) -> dict:
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as ex:
        try:
            body = json.load(ex)
        except Exception:  # noqa: BLE001
            body = {}
        return {"success": False, "status": ex.code, "errors": body.get("errors")}


def find_zone(zones: list[dict], host: str) -> dict | None:
    """host を含むゾーン（一番長く一致するもの）。"""
    hits = [z for z in zones if host == z["name"] or host.endswith("." + z["name"])]
    return max(hits, key=lambda z: len(z["name"]), default=None)


def show(title: str, data: dict, fmt) -> None:
    print(f"\n== {title}")
    if not data.get("success"):
        print(f"  （取得できません: {data.get('status')} {data.get('errors')}）")
        return
    rows = data.get("result") or []
    if isinstance(rows, dict):
        rows = rows.get("rules") or []
    if not rows:
        print("  なし")
    for row in rows:
        print("  " + fmt(row))


def main() -> int:
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        print(f"{TOKEN_FILE} がありません。先にトークンを保存してください。")
        return 1

    zones = get(token, "/zones?per_page=50")
    zone = find_zone(zones.get("result") or [], HOST) if zones.get("success") else None
    if zone is None:
        print(f"{HOST} を含むゾーンを取得できません: {zones.get('status')} {zones.get('errors')}")
        return 1
    zid, aid = zone["id"], zone["account"]["id"]
    print(f"ゾーン: {zone['name']}（状態: {zone['status']}）")

    show("DNS レコード", get(token, f"/zones/{zid}/dns_records?per_page=200"),
         lambda r: f"{r['type']:6} {r['name']:34} -> {str(r['content'])[:70]}  {'proxied' if r.get('proxied') else 'DNS only'}")
    show("Workers ルート（ゾーン）", get(token, f"/zones/{zid}/workers/routes"),
         lambda r: f"{r['pattern']:40} -> {r.get('script') or 'None'}")
    show("Worker のカスタムドメイン（アカウント）", get(token, f"/accounts/{aid}/workers/domains"),
         lambda r: f"{r['hostname']:40} -> {r.get('service')} ({r.get('environment')})")
    for phase, label in (
        ("http_request_origin", "Origin Rules（接続先の上書き）"),
        ("http_config_settings", "Configuration Rules"),
        ("http_request_dynamic_redirect", "Redirect Rules"),
        ("http_request_transform", "Transform Rules（URL 書き換え）"),
        ("http_request_late_transform", "Transform Rules（ヘッダー）"),
    ):
        show(label, get(token, f"/zones/{zid}/rulesets/phases/{phase}/entrypoint"),
             lambda r: f"[{'有効' if r.get('enabled', True) else '無効'}] {r.get('description') or ''} "
                       f"if {r.get('expression')} -> {r.get('action')} {json.dumps(r.get('action_parameters'), ensure_ascii=False)[:160]}")
    show("Page Rules", get(token, f"/zones/{zid}/pagerules"),
         lambda r: f"{[t['constraint']['value'] for t in r['targets']]} -> {[(a['id'], a.get('value')) for a in r['actions']]} ({r['status']})")
    show("Snippets のルール", get(token, f"/zones/{zid}/snippets/snippet_rules"),
         lambda r: f"{r.get('snippet_name')} if {r.get('expression')} ({'有効' if r.get('enabled') else '無効'})")

    tunnels = get(token, f"/accounts/{aid}/cfd_tunnel?is_deleted=false")
    show("トンネル", tunnels, lambda t: f"{t['name']:16} {t['id']}  状態: {t.get('status')}  接続数: {len(t.get('connections') or [])}")
    print(f"\n確認対象: {HOST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
