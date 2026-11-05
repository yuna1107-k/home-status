"""バックグラウンドで定期的に計測し、グラフ用の履歴を保持する。

- FAST_INTERVAL ごと: CPU（全体・プロセス別）、メモリ、温度、ネットワーク（NET_IFACE）の通信量
- SLOW_INTERVAL ごと: ルーター / インターネットの応答時間、Nextcloud の応答時間、
  Docker コンテナの CPU・メモリ
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import threading
import time
import urllib.request
from collections import deque

FAST_INTERVAL = 2.0
SLOW_INTERVAL = 10.0
HISTORY_POINTS = 180  # FAST で 6 分、SLOW で 30 分
NET_IFACE = os.environ.get("NET_IFACE", "eth0")
INTERNET_HOST = os.environ.get("INTERNET_HOST", "1.1.1.1")
NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "http://127.0.0.1:8080/status.php")
NEXTCLOUD_COMPOSE_PROJECT = os.environ.get("NEXTCLOUD_COMPOSE_PROJECT", "nextcloud")


def _parse_disks(spec: str) -> list[tuple[str, str]]:
    """DISKS="System=/,Data=/mnt/data" を [(表示名, マウント先), ...] にする。"""
    disks = []
    for part in spec.split(","):
        label, sep, path = part.strip().partition("=")
        if sep and label.strip() and path.strip().startswith("/"):
            disks.append((label.strip(), path.strip()))
    return disks or [("System", "/")]


# 監視するディスク（表示名, マウント先）
DISKS = _parse_disks(os.environ.get("DISKS", "System=/"))

_CLK_TCK = os.sysconf("SC_CLK_TCK")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def _read_cpu_times() -> list[int]:
    with open("/proc/stat") as f:
        return [int(x) for x in f.readline().split()[1:]]


def _read_core_times() -> list[list[int]]:
    cores = []
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                cores.append([int(x) for x in line.split()[1:]])
    return cores


def _busy_pct(prev: list[int], cur: list[int]) -> float:
    delta = [b - a for a, b in zip(prev, cur)]
    total = sum(delta) or 1
    return 100 * (1 - (delta[3] + delta[4]) / total)


def _read_freq_mhz() -> float | None:
    vals = []
    for i in range(os.cpu_count() or 1):
        try:
            with open(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq") as f:
                vals.append(int(f.read()) / 1000)
        except OSError:
            pass
    return sum(vals) / len(vals) if vals else None


def disk_devices() -> list[dict]:
    """マウントされているディスク（パーティションの親デバイス）と表示名。"""
    labels = {path: label for label, path in DISKS}
    found = {}
    try:
        with open("/proc/mounts") as f:
            mounts = [line.split()[:2] for line in f]
    except OSError:
        return []
    for dev, mnt in mounts:
        if not dev.startswith("/dev/") or mnt not in labels:
            continue
        part = dev[5:]
        parent = re.sub(r"p?\d+$", "", part) if not part.startswith("mmcblk") else re.sub(r"p\d+$", "", part)
        model = ""
        try:
            with open(f"/sys/block/{parent}/device/model") as f:
                model = f.read().strip()
        except OSError:
            model = "SD カード" if parent.startswith("mmcblk") else ""
        found.setdefault(parent, {"dev": parent, "label": labels[mnt], "mount": mnt, "model": model})
    return sorted(found.values(), key=lambda d: d["label"])


def _read_diskstats() -> dict[str, tuple[int, int, int]]:
    """デバイス -> (読んだセクタ, 書いたセクタ, I/O に費やした ms)。"""
    stats = {}
    with open("/proc/diskstats") as f:
        for line in f:
            parts = line.split()
            stats[parts[2]] = (int(parts[5]), int(parts[9]), int(parts[12]))
    return stats


def _read_net(iface: str) -> tuple[int, int] | None:
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(iface + ":"):
                parts = line.split(":", 1)[1].split()
                return int(parts[0]), int(parts[8])
    return None


def _read_temp() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except OSError:
        return None


def _read_mem() -> dict[str, int]:
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            mem[key] = int(val.split()[0]) * 1024
    return mem


_user_cache: dict[int, str] = {}


def _username(uid: int) -> str:
    if uid not in _user_cache:
        try:
            _user_cache[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _user_cache[uid] = str(uid)
    return _user_cache[uid]


def _read_processes() -> dict[int, dict]:
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue
        # comm は括弧付きで空白を含み得るので、最後の ')' で区切る
        rparen = stat.rfind(")")
        comm = stat[stat.find("(") + 1 : rparen]
        fields = stat[rparen + 2 :].split()
        procs[pid] = {
            "pid": pid,
            "name": comm,
            "user": _username(uid),
            "ticks": int(fields[11]) + int(fields[12]),  # utime + stime
            "threads": int(fields[17]),
            "rss": int(fields[21]) * _PAGE_SIZE,
        }
    return procs


def _ping_ms(host: str) -> float | None:
    try:
        out = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    m = re.search(r"time=([\d.]+)", out.stdout)
    return float(m.group(1)) if m else None


def _default_gateway() -> str | None:
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"default via (\S+)", out.stdout)
    return m.group(1) if m else None


def _parse_size(text: str) -> float:
    """docker stats の '99.6MiB' などをバイト数に。"""
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", text.strip())
    if not m:
        return 0.0
    num, unit = float(m.group(1)), m.group(2)
    scale = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
             "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return num * scale.get(unit, 1)


class Sampler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.fast = {k: deque(maxlen=HISTORY_POINTS) for k in (
            "t", "cpu_user", "cpu_system", "mem_used", "temp", "net_rx", "net_tx", "freq", "cores")}
        self.disks = disk_devices()
        for d in self.disks:
            for k in ("busy", "read", "write"):
                self.fast[f"disk_{d['dev']}_{k}"] = deque(maxlen=HISTORY_POINTS)
        self.disk_now: dict[str, dict] = {}
        self.slow = {k: deque(maxlen=HISTORY_POINTS) for k in (
            "t", "router_ms", "internet_ms", "nextcloud_ms")}
        self.cpu = {"user": 0.0, "system": 0.0, "idle": 100.0, "cores": os.cpu_count() or 1,
                    "freq": None, "per_core": [], "processes": 0, "threads": 0}
        self.processes: list[dict] = []
        self.net = {"rx_rate": 0.0, "tx_rate": 0.0, "rx_total": 0, "tx_total": 0}
        self.containers: dict[str, dict] = {}
        self.latency = {"router": None, "internet": None, "nextcloud": None, "gateway": None}

        self._prev_cpu = _read_cpu_times()
        self._prev_cores = _read_core_times()
        self._prev_disk = _read_diskstats()
        self._prev_procs = _read_processes()
        self._prev_net = _read_net(NET_IFACE)
        self._prev_time = time.monotonic()

    # ---- 公開

    def start(self) -> None:
        # 起動直後に「ルーター応答なし」などと誤表示しないよう、1 回目は先に測る
        try:
            self._sample_slow()
        except Exception as ex:  # noqa: BLE001
            print(f"sampler: initial sample: {ex!r}", flush=True)
        threading.Thread(target=self._loop, args=(FAST_INTERVAL, self._sample_fast), daemon=True).start()
        threading.Thread(target=self._loop, args=(SLOW_INTERVAL, self._sample_slow), daemon=True).start()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "fast": {k: list(v) for k, v in self.fast.items()},
                "slow": {k: list(v) for k, v in self.slow.items()},
                "cpu": dict(self.cpu),
                "processes": list(self.processes),
                "disks": [dict(d, **self.disk_now.get(d["dev"], {})) for d in self.disks],
                "net": dict(self.net),
                "containers": dict(self.containers),
                "latency": dict(self.latency),
            }

    # ---- 内部

    @staticmethod
    def _loop(interval: float, fn) -> None:
        while True:
            start = time.monotonic()
            try:
                fn()
            except Exception as ex:  # noqa: BLE001 - 計測の失敗でスレッドを止めない
                print(f"sampler: {fn.__name__}: {ex!r}", flush=True)
            time.sleep(max(0.5, interval - (time.monotonic() - start)))

    def _sample_fast(self) -> None:
        now = time.monotonic()
        dt = max(now - self._prev_time, 1e-3)

        cpu = _read_cpu_times()
        delta = [b - a for a, b in zip(self._prev_cpu, cpu)]
        total = sum(delta) or 1
        user = 100 * (delta[0] + delta[1]) / total            # user + nice
        system = 100 * (delta[2] + delta[5] + delta[6]) / total  # system + irq + softirq
        idle = 100 * (delta[3] + delta[4]) / total            # idle + iowait

        cores = _read_core_times()
        per_core = [round(_busy_pct(a, b), 1) for a, b in zip(self._prev_cores, cores)]
        freq = _read_freq_mhz()

        disk = _read_diskstats()
        disk_now = {}
        for d in self.disks:
            prev, cur = self._prev_disk.get(d["dev"]), disk.get(d["dev"])
            if not prev or not cur:
                continue
            disk_now[d["dev"]] = {
                "read": max(0, cur[0] - prev[0]) * 512 / dt,
                "write": max(0, cur[1] - prev[1]) * 512 / dt,
                "busy": min(100.0, max(0, cur[2] - prev[2]) / (dt * 1000) * 100),
            }

        procs = _read_processes()
        ncpu = os.cpu_count() or 1
        rows = []
        for pid, p in procs.items():
            prev = self._prev_procs.get(pid)
            ticks = p["ticks"] - prev["ticks"] if prev else 0
            # Activity Monitor と同じく 1 コア = 100%
            p_cpu = 100 * ticks / _CLK_TCK / dt
            rows.append({**{k: p[k] for k in ("pid", "name", "user", "threads", "rss")},
                         "cpu": round(min(p_cpu, 100 * ncpu), 1)})
        rows.sort(key=lambda r: (r["cpu"], r["rss"]), reverse=True)

        net = _read_net(NET_IFACE)
        rx_rate = tx_rate = 0.0
        if net and self._prev_net:
            rx_rate = max(0, net[0] - self._prev_net[0]) / dt
            tx_rate = max(0, net[1] - self._prev_net[1]) / dt

        mem = _read_mem()
        mem_used = mem["MemTotal"] - mem["MemAvailable"]
        temp = _read_temp()

        with self._lock:
            self.cpu.update(user=round(user, 1), system=round(system, 1), idle=round(idle, 1),
                            freq=freq, per_core=per_core, processes=len(procs),
                            threads=sum(p["threads"] for p in procs.values()))
            self.disk_now = disk_now
            for dev, v in disk_now.items():
                for k in ("busy", "read", "write"):
                    self.fast[f"disk_{dev}_{k}"].append(round(v[k], 1))
            self.fast["freq"].append(round(freq) if freq else None)
            self.fast["cores"].append(per_core)
            self.processes = rows[:80]
            self.net = {"rx_rate": rx_rate, "tx_rate": tx_rate,
                        "rx_total": net[0] if net else 0, "tx_total": net[1] if net else 0}
            t = time.time()
            for key, val in (("t", t), ("cpu_user", round(user, 1)), ("cpu_system", round(system, 1)),
                             ("mem_used", mem_used), ("temp", temp),
                             ("net_rx", round(rx_rate)), ("net_tx", round(tx_rate))):
                self.fast[key].append(val)

        self._prev_cpu, self._prev_procs, self._prev_net, self._prev_time = cpu, procs, net, now
        self._prev_cores, self._prev_disk = cores, disk

    def _sample_slow(self) -> None:
        gateway = _default_gateway()
        router_ms = _ping_ms(gateway) if gateway else None
        internet_ms = _ping_ms(INTERNET_HOST)

        nextcloud_ms = None
        try:
            start = time.monotonic()
            with urllib.request.urlopen(NEXTCLOUD_URL, timeout=5) as resp:
                resp.read()
            nextcloud_ms = (time.monotonic() - start) * 1000
        except Exception:  # noqa: BLE001
            pass

        containers = {}
        try:
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=15,
            )
            for line in out.stdout.splitlines():
                st = json.loads(line)
                net_in, _, net_out = st.get("NetIO", "").partition("/")
                containers[st["Name"]] = {
                    "cpu": float(st.get("CPUPerc", "0").rstrip("%") or 0),
                    "mem": _parse_size(st.get("MemUsage", "").split("/")[0]),
                    "net_in": _parse_size(net_in),
                    "net_out": _parse_size(net_out),
                    "pids": int(st.get("PIDs") or 0),
                }
        except Exception:  # noqa: BLE001
            pass

        with self._lock:
            self.latency = {"router": router_ms, "internet": internet_ms,
                            "nextcloud": nextcloud_ms, "gateway": gateway}
            if containers:
                self.containers = containers
            for key, val in (("t", time.time()), ("router_ms", router_ms), ("internet_ms", internet_ms),
                             ("nextcloud_ms", nextcloud_ms)):
                self.slow[key].append(None if val is None else round(val, 1) if isinstance(val, float) else val)


SAMPLER = Sampler()
