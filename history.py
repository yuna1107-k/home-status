"""長期の履歴（1 分ごとの平均を D1 に保存）と生存確認。

- 毎分: 直前 1 分の計測値の平均を metrics_1m に書く。heartbeat も更新する
  （Cloudflare 側の見張りが、これが止まったら Pi の停止として通知する）
- 毎時: metrics_1m を 1 時間ごとに集計して metrics_1h に書く（ずっと保持）
- 毎日: 31 日より古い metrics_1m を消す

画面からは query() で期間を指定して読む。D1 に届かないときは手元の 24 時間分で返す。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections import deque

import boots
import cloud
from sampler import DISKS, SAMPLER

KEEP_1M_DAYS = 31
LOCAL_POINTS = 24 * 60
COLUMNS = ("cpu", "mem_used", "temp", "load1", "net_rx", "net_tx",
           "router_ms", "internet_ms", "nextcloud_ms")

# 期間 -> (秒数, 何秒ごとにまとめるか, 使う表)
RANGES = {
    "1h": (3600, 60, "metrics_1m"),
    "6h": (6 * 3600, 120, "metrics_1m"),
    "24h": (86400, 300, "metrics_1m"),
    "7d": (7 * 86400, 1800, "metrics_1m"),
    "30d": (30 * 86400, 7200, "metrics_1h"),
    "1y": (365 * 86400, 86400, "metrics_1h"),
}

_local: deque[dict] = deque(maxlen=LOCAL_POINTS)
_lock = threading.Lock()


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def minute_row(start: float, end: float) -> dict:
    """[start, end) の計測値を平均して 1 行にする。"""
    snap = SAMPLER.snapshot()
    fast, slow = snap["fast"], snap["slow"]

    def pick(group: dict, key: str) -> list:
        return [v for t, v in zip(group["t"], group[key]) if start <= t < end]

    cpu = [u + s for u, s in zip(pick(fast, "cpu_user"), pick(fast, "cpu_system"))]
    disk = {}
    for label, path in DISKS:
        if os.path.ismount(path):
            du = shutil.disk_usage(path)
            disk[label] = round(100 * du.used / du.total, 1)
    try:
        with open("/proc/meminfo") as f:
            total = next(int(line.split()[1]) * 1024 for line in f if line.startswith("MemTotal:"))
    except (OSError, StopIteration):
        total = None
    row = {
        "ts": int(start),
        "cpu": _avg(cpu),
        "mem_used": _avg(pick(fast, "mem_used")),
        "mem_total": total,
        "temp": _avg(pick(fast, "temp")),
        "load1": os.getloadavg()[0],
        "net_rx": _avg(pick(fast, "net_rx")),
        "net_tx": _avg(pick(fast, "net_tx")),
        "router_ms": _avg(pick(slow, "router_ms")),
        "internet_ms": _avg(pick(slow, "internet_ms")),
        "nextcloud_ms": _avg(pick(slow, "nextcloud_ms")),
        "disk": disk,
    }
    for k, v in row.items():
        if isinstance(v, float):
            row[k] = round(v, 2)
    return row


def _write_minute(row: dict) -> None:
    cloud.write(
        "INSERT OR REPLACE INTO metrics_1m (host, ts, cpu, mem_used, mem_total, temp, load1, net_rx, net_tx, "
        "router_ms, internet_ms, nextcloud_ms, disk) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [cloud.HOST, row["ts"], row["cpu"], row["mem_used"], row["mem_total"], row["temp"], row["load1"],
         row["net_rx"], row["net_tx"], row["router_ms"], row["internet_ms"], row["nextcloud_ms"],
         json.dumps(row["disk"])],
        kind="metric",
    )


def heartbeat(extra: dict | None = None) -> None:
    cloud.write(
        "INSERT INTO heartbeat (host, ts, boot_id, uptime, info) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (host) DO UPDATE SET ts = excluded.ts, boot_id = excluded.boot_id, "
        "uptime = excluded.uptime, info = excluded.info WHERE excluded.ts >= heartbeat.ts",
        [cloud.HOST, int(time.time()), boots.current_boot_id(), round(_uptime()), json.dumps(extra or {}, ensure_ascii=False)],
        kind="heartbeat",
    )


def _rollup_hours(now: float) -> None:
    hour = int(now) // 3600 * 3600
    cloud.write(
        "INSERT OR REPLACE INTO metrics_1h (host, ts, cpu, cpu_max, mem_used, temp, temp_max, net_rx, net_tx, "
        "internet_ms, nextcloud_ms) "
        "SELECT host, (ts / 3600) * 3600, avg(cpu), max(cpu), avg(mem_used), avg(temp), max(temp), avg(net_rx), "
        "avg(net_tx), avg(internet_ms), avg(nextcloud_ms) FROM metrics_1m "
        "WHERE host = ? AND ts >= ? AND ts < ? GROUP BY host, (ts / 3600)",
        [cloud.HOST, hour - 3 * 3600, hour],
        kind="rollup",
    )


def _cleanup(now: float) -> None:
    cloud.write("DELETE FROM metrics_1m WHERE ts < ?", [int(now) - KEEP_1M_DAYS * 86400], kind="rollup")


def _loop() -> None:
    last_hour = last_day = 0
    while True:
        now = time.time()
        # 毎分 0 秒の少し後に、直前 1 分をまとめる
        time.sleep(60 - now % 60 + 1)
        end = time.time() // 60 * 60
        try:
            row = minute_row(end - 60, end)
            with _lock:
                _local.append(row)
            _write_minute(row)
            heartbeat({"temp": row["temp"], "cpu": row["cpu"], "pending": cloud.OUTBOX_Q.pending()})
            hour, day = int(end) // 3600, int(end) // 86400
            if hour != last_hour:
                last_hour = hour
                _rollup_hours(end)
            if day != last_day:
                last_day = day
                _cleanup(end)
        except Exception as ex:  # noqa: BLE001 - 記録のスレッドは止めない
            print(f"history: {ex!r}", flush=True)


def _preload() -> None:
    """再起動しても直近 24 時間のグラフが途切れないよう、D1 から手元に読み込む。"""
    if not cloud.configured():
        return
    try:
        rows = cloud.query(
            f"SELECT ts, {', '.join(COLUMNS)} FROM metrics_1m WHERE host = ? AND ts >= ? ORDER BY ts",
            [cloud.HOST, int(time.time()) - 86400])
    except cloud.CloudError as ex:
        print(f"history: 直近の履歴を読み込めません: {ex}", flush=True)
        return
    with _lock:
        have = {r["ts"] for r in _local}
        merged = [r for r in rows if r["ts"] not in have] + list(_local)
        merged.sort(key=lambda r: r["ts"])
        _local.clear()
        _local.extend(merged[-LOCAL_POINTS:])


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="history").start()
    threading.Thread(target=_preload, daemon=True, name="history-preload").start()


# ---------------------------------------------------------------- 読み出し

def _series(rows: list[dict], start: int, step: int, count: int, cols: tuple) -> dict:
    """バケットごとの値の配列にする。データが無いところは None（グラフが途切れる＝止まっていた）。"""
    index = {int(r["t"]): r for r in rows}
    out = {"t": [start + i * step for i in range(count)]}
    for c in cols:
        out[c] = [(lambda v: None if v is None else round(v, 2))(index.get(t, {}).get(c)) for t in out["t"]]
    return out


def query(range_key: str) -> dict:
    if range_key not in RANGES:
        raise ValueError("期間の指定が不正です")
    span, step, table = RANGES[range_key]
    now = int(time.time())
    start = (now - span) // step * step
    count = (now - start) // step + 1
    cols = COLUMNS if table == "metrics_1m" else ("cpu", "mem_used", "temp", "net_rx", "net_tx",
                                                  "internet_ms", "nextcloud_ms")
    maxes = ("cpu", "temp")
    select = ", ".join(f"avg({c}) AS {c}" for c in cols)
    if table == "metrics_1m":
        select += ", " + ", ".join(f"max({c}) AS {c}_max" for c in maxes)
    else:
        select += ", max(cpu_max) AS cpu_max, max(temp_max) AS temp_max"
    out_cols = cols + tuple(f"{c}_max" for c in maxes)
    source = "d1"
    try:
        rows = cloud.query(
            f"SELECT (ts / ?) * ? AS t, {select} FROM {table} WHERE host = ? AND ts >= ? GROUP BY 1 ORDER BY 1",
            [step, step, cloud.HOST, start], timeout=20)
    except cloud.CloudError as ex:
        # D1 に届かない: 手元の 24 時間分だけで返す
        source = f"local（{ex}）"
        buckets: dict[int, list[dict]] = {}
        with _lock:
            for r in _local:
                if r["ts"] >= start:
                    buckets.setdefault(r["ts"] // step * step, []).append(r)
        rows = []
        for t, rs in sorted(buckets.items()):
            row = {"t": t}
            for c in cols:
                row[c] = _avg([r.get(c) for r in rs])
            for c in maxes:
                vals = [r.get(c) for r in rs if r.get(c) is not None]
                row[f"{c}_max"] = max(vals) if vals else None
            rows.append(row)
    try:
        with open("/proc/meminfo") as f:
            mem_total = next(int(line.split()[1]) * 1024 for line in f if line.startswith("MemTotal:"))
    except (OSError, StopIteration):
        mem_total = None
    return {"range": range_key, "step": step, "source": source, "mem_total": mem_total,
            **_series(rows, start, step, count, out_cols)}
