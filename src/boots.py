"""起動と停止の履歴。

起動のたびに journal から過去の起動を読み、それぞれの停止の仕方を推定して D1 の boots に書く:
  - 最後のログに reboot.target   → 再起動（このサイトから操作した記録があればそう書く）
  - 最後のログに poweroff.target → シャットダウン
  - どちらも無い                  → 予期しない停止（電源断・フリーズなど）
起動中は数分ごとに last_seen を更新する。SD カードごと壊れて journal が消えても、
D1 側に残っている「稼働中」のまま終わった起動は「予期しない停止」として扱う。

Pi には時計用の電池が無く、起動直後の時刻はずれていることがあるため、起動時刻は
「最後のログの時刻 − 起動からの経過時間」で計算する。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import cloud

TOUCH_INTERVAL = 300
SCAN_BOOTS = 15
RUNNING = "稼働中"


def _run(*args: str, timeout: float = 20) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def current_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip().replace("-", "")
    except OSError:
        return ""


def _btime() -> int:
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return int(line.split()[1])
    return int(time.time())


def _last_entry(boot_id: str) -> tuple[int, int] | None:
    """(最後のログの時刻, その起動の起動時刻) を返す。"""
    out = _run("journalctl", "-b", boot_id, "-n", "1", "-o", "json", "--no-pager",
               "--output-fields=__REALTIME_TIMESTAMP,__MONOTONIC_TIMESTAMP")
    try:
        entry = json.loads(out.strip().splitlines()[-1])
        real = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
        mono = int(entry["__MONOTONIC_TIMESTAMP"]) / 1e6
    except (IndexError, KeyError, ValueError, json.JSONDecodeError):
        return None
    return int(real), int(real - mono)


def _reason(boot_id: str, ended: int, reboots_from_site: list[int]) -> tuple[str, str]:
    tail = _run("journalctl", "-b", boot_id, "-n", "60", "-o", "cat", "--no-pager")
    details = []
    under = _run("journalctl", "-k", "-b", boot_id, "-o", "cat", "--no-pager", "-q",
                 "-g", "Undervoltage|Under-voltage|under-voltage")
    if under.strip():
        details.append(f"電圧低下 {len(under.strip().splitlines())} 回")
    if "reboot.target" in tail:
        if any(0 <= ended - t <= 600 for t in reboots_from_site):
            reason = "再起動（このサイトから）"
        else:
            reason = "再起動"
    elif "poweroff.target" in tail or "halt.target" in tail:
        reason = "シャットダウン"
    else:
        reason = "予期しない停止"
        details.append("電源断・フリーズ・ケーブルの抜けなどの可能性")
    return reason, "、".join(details)


def _site_reboots() -> list[int]:
    try:
        rows = cloud.query("SELECT ts FROM audit WHERE action = 'pi.reboot' AND ok = 1 ORDER BY ts DESC LIMIT 50")
        return [r["ts"] for r in rows]
    except cloud.CloudError:
        return []


def _write(boot_id: str, booted: int, last_seen: int | None, reason: str, detail: str) -> None:
    cloud.write(
        "INSERT INTO boots (boot_id, host, booted_at, last_seen, reason, detail) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (boot_id) DO UPDATE SET booted_at = excluded.booted_at, "
        "last_seen = max(coalesce(boots.last_seen, 0), coalesce(excluded.last_seen, 0)), "
        "reason = excluded.reason, detail = excluded.detail",
        [boot_id, cloud.HOST, booted, last_seen, reason, detail],
        kind="boot",
    )


def scan() -> list[dict]:
    """journal の過去の起動を調べて D1 に書き、新しい順の一覧を返す。"""
    try:
        boots = json.loads(_run("journalctl", "--list-boots", "-o", "json", "--no-pager") or "[]")
    except json.JSONDecodeError:
        boots = []
    cur = current_boot_id()
    site = _site_reboots() if cloud.configured() else []
    rows = []
    for b in boots[-SCAN_BOOTS:]:
        bid = b["boot_id"]
        if bid == cur:
            continue
        last = _last_entry(bid)
        if not last:
            continue
        ended, booted = last
        reason, detail = _reason(bid, ended, site)
        rows.append({"boot_id": bid, "booted_at": booted, "last_seen": ended, "reason": reason, "detail": detail})
        _write(bid, booted, ended, reason, detail)
    # D1 に「稼働中」のまま残っている古い起動（journal ごと消えた場合など）
    if cloud.configured():
        try:
            stale = cloud.query("SELECT boot_id FROM boots WHERE host = ? AND reason = ? AND boot_id != ?",
                                [cloud.HOST, RUNNING, cur])
            known = {r["boot_id"] for r in rows}
            for r in stale:
                if r["boot_id"] not in known:
                    cloud.write("UPDATE boots SET reason = ?, detail = ? WHERE boot_id = ? AND reason = ?",
                                ["予期しない停止", "記録が途中で途切れています（SD カードの交換などで journal が無い）",
                                 r["boot_id"], RUNNING], kind="boot")
        except cloud.CloudError:
            pass
    return rows[::-1]


def touch() -> None:
    _write(current_boot_id(), _btime(), int(time.time()), RUNNING, "")


def history(limit: int = 30) -> tuple[list[dict], str]:
    """新しい順。止まっていた時間（次の起動まで）も付ける。"""
    try:
        rows = cloud.query("SELECT boot_id, booted_at, last_seen, reason, detail FROM boots WHERE host = ? "
                           "ORDER BY booted_at DESC LIMIT ?", [cloud.HOST, limit])
        source = "d1"
    except cloud.CloudError:
        rows = [{"boot_id": current_boot_id(), "booted_at": _btime(), "last_seen": int(time.time()),
                 "reason": RUNNING, "detail": ""}] + _local_scan()
        source = "local"
    cur = current_boot_id()
    for i, r in enumerate(rows):
        if r["boot_id"] == cur:
            r["reason"], r["last_seen"] = RUNNING, int(time.time())
        nxt = rows[i - 1] if i > 0 else None
        r["down_seconds"] = (nxt["booted_at"] - r["last_seen"]) if nxt and r.get("last_seen") else None
    return rows, source


_scanned: list[dict] = []


def _local_scan() -> list[dict]:
    return list(_scanned)


def _uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def _announce(prev: dict | None) -> None:
    """Pi が起動した直後（サイトだけの再起動ではない）に、前回の止まり方を添えて通知する。"""
    import notify
    if prev is None:
        notify.send("Pi が起動しました", "前回の停止の記録はありません", "info", page="system")
        return
    unexpected = prev["reason"] == "予期しない停止"
    down = time.time() - _uptime() - prev["last_seen"]
    text = f"前回: {prev['reason']}（{time.strftime('%m/%d %H:%M', time.localtime(prev['last_seen']))} に停止"
    text += f"、約 {max(1, round(down / 60))} 分止まっていました）" if down > 0 else "）"
    if prev.get("detail"):
        text += f"\n{prev['detail']}"
    notify.send("Pi が予期せず止まり、再起動しました" if unexpected else "Pi が起動しました", text,
                "warn" if unexpected else "info", page="system")


def _loop() -> None:
    global _scanned
    try:
        touch()
        _scanned = scan()
        if _uptime() < 600:
            _announce(_scanned[0] if _scanned else None)
    except Exception as ex:  # noqa: BLE001
        print(f"boots: {ex!r}", flush=True)
    while True:
        time.sleep(TOUCH_INTERVAL)
        try:
            touch()
        except Exception as ex:  # noqa: BLE001
            print(f"boots: {ex!r}", flush=True)


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="boots").start()
