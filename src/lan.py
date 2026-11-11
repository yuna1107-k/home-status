"""LAN に新しくつながった端末の検出。

LAN で見えた端末（MAC アドレス）を D1 の lan_devices に記録し、初めて見た端末を
「知らない端末」として通知する。画面で「既知にする」と以後は通知しない。
最初の 1 回は、その時点で見えている端末をすべて既知として登録する（いきなり大量に通知しない）。

スマホの「ランダム MAC（プライベートアドレス）」は時々変わるため、変わるたびに
新しい端末として見える。通知にはその旨を添える。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import alerts
import cloud

CACHE = Path(os.environ.get("LAN_CACHE", str(cloud.DATA_DIR / "lan_devices.json")))
CHECK_INTERVAL = 60
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

_lock = threading.Lock()
_devices: dict[str, dict] = {}
_loaded = False


def _save_cache() -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_devices, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CACHE)


def _upsert(d: dict) -> None:
    cloud.write(
        "INSERT INTO lan_devices (mac, ip, name, label, known, first_seen, last_seen, random_mac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (mac) DO UPDATE SET ip = excluded.ip, name = excluded.name, "
        "label = excluded.label, known = excluded.known, last_seen = max(lan_devices.last_seen, excluded.last_seen), "
        "first_seen = min(lan_devices.first_seen, excluded.first_seen), random_mac = excluded.random_mac",
        [d["mac"], d.get("ip"), d.get("name"), d.get("label", ""), 1 if d.get("known") else 0,
         d["first_seen"], d["last_seen"], 1 if d.get("random_mac") else 0],
        kind="lan",
    )


def _load() -> bool:
    """手元の控え → D1 の順に読む。どちらも無ければ False（初回）。"""
    global _devices, _loaded
    found = False
    try:
        _devices = json.loads(CACHE.read_text(encoding="utf-8"))
        found = True
    except (OSError, json.JSONDecodeError):
        _devices = {}
    if cloud.configured():
        try:
            rows = cloud.query("SELECT mac, ip, name, label, known, first_seen, last_seen, random_mac FROM lan_devices")
            for r in rows:
                cur = _devices.get(r["mac"], {})
                _devices[r["mac"]] = {**r, "known": bool(r["known"] or cur.get("known")),
                                      "random_mac": bool(r["random_mac"]),
                                      "label": cur.get("label") or r.get("label") or ""}
            found = found or bool(rows)
        except cloud.CloudError as ex:
            print(f"lan: D1 から読めません: {ex}", flush=True)
            if not found:
                return None  # 判断できない（次回やり直す）
    _loaded = True
    return found


def observe(neighbors: list[dict]) -> None:
    with _lock:
        if not _loaded:
            had = _load()
            if had is None:
                return
            baseline = not had
        else:
            baseline = False
        now = int(time.time())
        new = []
        for n in neighbors:
            mac = (n.get("mac") or "").lower()
            if not _MAC_RE.match(mac):
                continue
            d = _devices.get(mac)
            if d is None:
                d = {"mac": mac, "ip": n["ip"], "name": n.get("name") or "", "label": "", "known": baseline,
                     "first_seen": now, "last_seen": now, "random_mac": bool(n.get("random_mac"))}
                _devices[mac] = d
                _upsert(d)
                if not baseline:
                    new.append(d)
                continue
            changed = d.get("ip") != n["ip"] or (n.get("name") and d.get("name") != n.get("name"))
            d.update(ip=n["ip"], name=n.get("name") or d.get("name", ""))
            # last_seen は 10 分に 1 回だけ D1 に送る（書き込み回数を抑える）
            if changed or now - d.get("last_seen", 0) >= 600:
                d["last_seen"] = now
                _upsert(d)
        _save_cache()
    if baseline:
        print(f"lan: 最初の登録として {len(_devices)} 台を既知にしました", flush=True)
    for d in new:
        note = "\n（ランダム MAC のため、知っているスマホでもアドレスが変わると新しい端末に見えます）" if d["random_mac"] else ""
        alerts.ENGINE.event("new_device", "LAN に知らない端末がつながりました",
                            f"{d['name'] or '(名前なし)'}  {d['ip']}  {d['mac']}{note}")


def devices() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in _devices.items()}


def set_device(mac: str, known: bool | None = None, label: str | None = None) -> dict:
    mac = mac.lower()
    with _lock:
        d = _devices.get(mac)
        if d is None:
            raise KeyError(mac)
        if known is not None:
            d["known"] = known
        if label is not None:
            d["label"] = label.strip()[:40]
        _upsert(d)
        _save_cache()
        return dict(d)


def known_all() -> int:
    with _lock:
        n = 0
        for d in _devices.values():
            if not d.get("known"):
                d["known"] = True
                _upsert(d)
                n += 1
        _save_cache()
        return n


def _loop(neighbors_fn) -> None:
    while True:
        try:
            observe(neighbors_fn())
        except Exception as ex:  # noqa: BLE001
            print(f"lan: {ex!r}", flush=True)
        time.sleep(CHECK_INTERVAL)


def start(neighbors_fn) -> None:
    threading.Thread(target=_loop, args=(neighbors_fn,), daemon=True, name="lan").start()
