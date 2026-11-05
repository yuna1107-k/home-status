"""各タブ（Raspberry Pi / ネットワーク / Tailscale / Nextcloud）の表示データを組み立てる。

どの関数も例外を外に出さず、次の形の dict を返す:
    {"level": "ok" | "warn" | "error", "summary": str, "items": [...], ...}
items は下部パネルに並べる (ラベル, 値, level) の一覧。
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import versions
from sampler import DISKS, INTERNET_HOST, NET_IFACE, NEXTCLOUD_COMPOSE_PROJECT, NEXTCLOUD_URL, SAMPLER

DNS_TEST_NAME = os.environ.get("DNS_TEST_NAME", "www.google.com")
# Nextcloud のデータを置いているマウント先（空なら表示しない）
NEXTCLOUD_DATA_DIR = os.environ.get("NEXTCLOUD_DATA_DIR", "")

OK, WARN, ERROR = "ok", "warn", "error"
_LEVEL_ORDER = {OK: 0, WARN: 1, ERROR: 2}


_cached_funcs: list = []


def cached(seconds: float):
    """結果を seconds 秒だけ使い回す（引数ごと）。2 秒ごとの配信で重いコマンドを叩きすぎないため。"""
    def deco(fn):
        store: dict = {}
        lock = threading.Lock()

        def wrapper(*args):
            now = time.monotonic()
            with lock:
                hit = store.get(args)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            value = fn(*args)
            with lock:
                store[args] = (now, value)
            return value

        wrapper.clear = store.clear
        _cached_funcs.append(wrapper)
        return wrapper
    return deco


def clear_caches() -> None:
    """操作の直後に最新の状態を見せるため、キャッシュを捨てる。"""
    for fn in _cached_funcs:
        fn.clear()


def _worst(*levels: str) -> str:
    return max(levels, key=lambda lv: _LEVEL_ORDER[lv], default=OK)


def _item(label: str, value: object, level: str = OK) -> dict:
    return {"label": label, "value": str(value), "level": level}


def _run(*args: str, timeout: float = 5) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{args[0]} が見つかりません"
    except subprocess.TimeoutExpired:
        return 124, "タイムアウト"
    return proc.returncode, proc.stdout


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}日 {hours}時間"
    if hours:
        return f"{hours}時間 {minutes}分"
    if minutes:
        return f"{minutes}分"
    return f"{seconds}秒"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


# ---------------------------------------------------------------- Raspberry Pi

# (id, systemd ユニット, --user か, 説明, 再起動できるか)。入っていないユニットは表示しない
SERVICES = [
    ("home-status", "home-status", True, "このステータスサイト", True),
    ("nextcloud", None, False, "Nextcloud（Docker コンテナ一式）", True),
    ("tailscaled", "tailscaled", False, "Tailscale デーモン", False),
    ("docker", "docker", False, "Docker Engine", False),
    ("cloudflared", "cloudflared", False, "Cloudflare Tunnel", False),
    ("ssh", "ssh", False, "OpenSSH サーバー", False),
]
# 追加で表示する systemd のユニット（カンマ区切り。例: EXTRA_SERVICES=nginx,cups）
SERVICES += [(u, u, False, u, False) for u in
             (x.strip() for x in os.environ.get("EXTRA_SERVICES", "").split(","))
             if re.fullmatch(r"[A-Za-z0-9@._-]+", u) and u not in {sv[0] for sv in SERVICES}]


@cached(5)
def _services() -> list[dict]:
    rows = []
    for sid, unit, user, desc, restartable in SERVICES:
        if unit is None:
            code, out = _docker_ps()
            states = [line.split("\t")[1] for line in out.splitlines() if "\t" in line] if code == 0 else []
            running = sum(st == "running" for st in states)
            rows.append({"id": sid, "name": sid, "desc": desc, "restartable": restartable,
                         "state": "running" if states and running == len(states) else "degraded" if running else "stopped",
                         "detail": f"{running} / {len(states)} 稼働", "pid": None, "memory": None})
            continue
        args = ["systemctl"] + (["--user"] if user else []) + [
            "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "MemoryCurrent"]
        _, out = _run(*args)
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            if sid == "cloudflared":
                rows.append({"id": sid, "name": sid, "desc": desc + "（未インストール）", "restartable": False,
                             "state": "absent", "detail": "未インストール", "pid": None, "memory": None})
            continue
        mem = props.get("MemoryCurrent", "")
        rows.append({
            "id": sid, "name": unit, "desc": desc, "restartable": restartable,
            "state": "running" if props.get("ActiveState") == "active" else props.get("ActiveState", "?"),
            "detail": props.get("SubState", ""),
            "pid": int(props.get("MainPID") or 0) or None,
            "memory": int(mem) if mem.isdigit() else None,
        })
    return rows


@cached(3600)
def _cpu_info() -> dict:
    _, out = _run("lscpu")
    info = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    get = lambda k: info.get(k, "").strip()  # noqa: E731
    return {
        "model": get("Model name") or "不明",
        "vendor": get("Vendor ID"),
        "max_mhz": float(get("CPU max MHz") or 0) or None,
        "min_mhz": float(get("CPU min MHz") or 0) or None,
        "sockets": get("Socket(s)") or "1",
    }


@cached(30)
def _throttled() -> tuple[int, str]:
    return _run("vcgencmd", "get_throttled")


@cached(30)
def _dns_ms() -> float | None:
    try:
        start = time.monotonic()
        socket.getaddrinfo(DNS_TEST_NAME, 443)
        return (time.monotonic() - start) * 1000
    except OSError:
        return None


@cached(10)
def neighbors() -> list[dict]:
    _, out = _run("ip", "-4", "neigh", "show", "dev", NET_IFACE)
    devices = []
    for line in out.splitlines():
        parts = line.split()
        if "lladdr" not in parts or parts[-1] in ("FAILED", "INCOMPLETE"):
            continue
        devices.append({"ip": parts[0], "mac": parts[parts.index("lladdr") + 1], "state": parts[-1]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(pool.map(lambda d: _reverse_name(d["ip"]), devices))
    for dev, name in zip(devices, names):
        dev["name"] = name
        # 2 文字目が 2/6/A/E の MAC はランダム化（ローカル管理）アドレス
        dev["random_mac"] = dev["mac"][1].lower() in "26ae"
    devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
    return devices


def collect_pi(snap: dict) -> dict:
    levels = []
    items = []
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\0\n ")
    except OSError:
        model = "不明"
    items.append(_item("機種", model))
    items.append(_item("ホスト名", socket.gethostname()))
    items.append(_item("起動時間", fmt_duration(_uptime())))

    temps = [t for t in snap["fast"]["temp"] if t is not None]
    if temps:
        temp = temps[-1]
        lv = OK if temp < 70 else WARN if temp < 80 else ERROR
        levels.append(lv)
        items.append(_item("CPU 温度", f"{temp:.1f} ℃", lv))

    load1, load5, load15 = os.getloadavg()
    items.append(_item("負荷平均", f"{load1:.2f} / {load5:.2f} / {load15:.2f}"))

    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            mem[key] = int(val.split()[0]) * 1024
    used = mem["MemTotal"] - mem["MemAvailable"]
    pct = 100 * used / mem["MemTotal"]
    lv = OK if pct < 85 else WARN if pct < 95 else ERROR
    levels.append(lv)
    memory = {
        "total": mem["MemTotal"], "used": used,
        "available": mem["MemAvailable"], "free": mem["MemFree"],
        "cached": mem.get("Cached", 0) + mem.get("Buffers", 0),
        "dirty": mem.get("Dirty", 0),
        "swap_total": mem.get("SwapTotal", 0),
        "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
        "percent": round(pct, 1), "level": lv,
    }

    io = {d["mount"]: d for d in snap.get("disks", [])}
    disks = []
    for label, path in DISKS:
        d_io = io.get(path, {})
        base = {"label": label, "path": path, "dev": d_io.get("dev"), "model": d_io.get("model", ""),
                "read": d_io.get("read"), "write": d_io.get("write"), "busy": d_io.get("busy")}
        if not os.path.ismount(path):
            levels.append(ERROR)
            disks.append({**base, "percent": None, "text": "未マウント", "level": ERROR})
            continue
        du = shutil.disk_usage(path)
        dpct = 100 * du.used / du.total
        dlv = OK if dpct < 85 else WARN if dpct < 95 else ERROR
        levels.append(dlv)
        disks.append({**base, "percent": round(dpct, 1), "used": du.used, "total": du.total,
                      "text": f"{fmt_bytes(du.used)} / {fmt_bytes(du.total)}", "level": dlv})

    code, out = _throttled()
    if code == 0 and "=" in out:
        raw = out.strip().split("=")[1]
        value = int(raw, 16)
        if value & 0xF:
            text, lv = "発生中", ERROR
        elif value:
            text, lv = "過去にあり", WARN
        else:
            text, lv = "なし", OK
        levels.append(lv)
        items.append(_item("電圧低下", f"{text} ({raw})", lv))

    level = _worst(*levels)
    return {
        "level": level,
        "summary": {OK: "正常", WARN: "注意", ERROR: "異常あり"}[level],
        "items": items,
        "cpu": {**snap["cpu"], **_cpu_info(), "uptime": _uptime()},
        "memory": memory,
        "disks": disks,
        "processes": snap["processes"],
        "history": {k: v for k, v in snap["fast"].items() if not k.startswith("net_")},
        "services": _services(),
    }


# ---------------------------------------------------------------- Network

_name_cache: dict[str, tuple[float, str]] = {}
_NAME_TTL = 300


def _reverse_name(ip: str) -> str:
    cached = _name_cache.get(ip)
    if cached and time.monotonic() - cached[0] < _NAME_TTL:
        return cached[1]
    try:
        name = socket.gethostbyaddr(ip)[0].rstrip(".")
    except OSError:
        name = ""
    _name_cache[ip] = (time.monotonic(), name)
    return name


def _iface_state(name: str) -> str | None:
    try:
        with open(f"/sys/class/net/{name}/operstate") as f:
            return f.read().strip()
    except OSError:
        return None


def collect_network(snap: dict) -> dict:
    items = []
    levels = []
    lat = snap["latency"]

    if lat["gateway"]:
        ms = lat["router"]
        lv = OK if ms is not None else ERROR
        levels.append(lv)
        items.append(_item(f"ルーター ({lat['gateway']})", f"{ms:.1f} ms" if ms is not None else "応答なし", lv))
    ms = lat["internet"]
    lv = OK if ms is not None else ERROR
    levels.append(lv)
    items.append(_item(f"インターネット ({INTERNET_HOST})", f"{ms:.1f} ms" if ms is not None else "接続できません", lv))

    dns_ms = _dns_ms()
    if dns_ms is not None:
        items.append(_item("DNS", f"{dns_ms:.0f} ms"))
    else:
        levels.append(ERROR)
        items.append(_item("DNS", "名前解決できません", ERROR))

    eth = _iface_state(NET_IFACE)
    if eth is not None:
        try:
            with open(f"/sys/class/net/{NET_IFACE}/speed") as f:
                speed = f" {int(f.read())} Mbps"
        except (OSError, ValueError):
            speed = ""
        items.append(_item(f"インターフェース ({NET_IFACE})", ("接続" + speed) if eth == "up" else eth,
                           OK if eth == "up" else WARN))
    if NET_IFACE != "wlan0":
        wlan = _iface_state("wlan0")
        if wlan is not None:
            items.append(_item("Wi-Fi (wlan0)", "接続" if wlan == "up" else "未使用"))

    devices = neighbors()

    level = _worst(*levels)
    return {
        "level": level,
        "summary": {OK: "オンライン", WARN: "注意", ERROR: "接続に問題"}[level],
        "items": items,
        "devices": devices,
        "iface": NET_IFACE,
        "gateway": lat["gateway"],
        "internet_host": INTERNET_HOST,
        "net": snap["net"],
        "history": {
            "t": snap["fast"]["t"], "net_rx": snap["fast"]["net_rx"], "net_tx": snap["fast"]["net_tx"],
            "slow_t": snap["slow"]["t"], "router_ms": snap["slow"]["router_ms"],
            "internet_ms": snap["slow"]["internet_ms"],
        },
    }


# ---------------------------------------------------------------- Tailscale

@cached(5)
def serve_mappings() -> list[dict]:
    """`tailscale serve status --json` を (https ポート, パス, 転送先) の一覧にする。"""
    code, out = _run("tailscale", "serve", "status", "--json")
    if code != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    mappings = []
    for hostport, web in (data.get("Web") or {}).items():
        host, port = hostport.rsplit(":", 1)
        port = int(port)
        for path, handler in (web.get("Handlers") or {}).items():
            mappings.append({
                "port": port,
                "path": path,
                "target": handler.get("Proxy") or handler.get("Path") or handler.get("Text") or "?",
                "url": f"https://{host}{'' if port == 443 else f':{port}'}{path}",
            })
    mappings.sort(key=lambda m: (m["port"], m["path"]))
    return mappings


def _ts_time_ago(value: str) -> str:
    if not value or value.startswith("0001"):
        return ""
    try:
        t = calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return ""
    return fmt_duration(time.time() - t) + "前"


@cached(3)
def _tailscale_status() -> tuple[int, str]:
    return _run("tailscale", "status", "--json")


def collect_tailscale(snap: dict) -> dict:
    code, out = _tailscale_status()
    if code != 0 and not out.strip().startswith("{"):
        return {"level": ERROR, "summary": "取得できません",
                "items": [_item("tailscale", out.strip() or f"exit {code}", ERROR)], "peers": [], "serve": []}
    data = json.loads(out)
    backend = data.get("BackendState", "?")
    me = data.get("Self") or {}
    running = backend == "Running"
    users = data.get("User") or {}
    login = (users.get(str(me.get("UserID"))) or {}).get("LoginName", "")
    items = [
        _item("状態", "接続中" if running else {"Stopped": "停止中"}.get(backend, backend),
              OK if running else ERROR),
        _item("このマシン", me.get("HostName", "?")),
        _item("IP", ", ".join((me.get("TailscaleIPs") or [])[:1]) or "-"),
        _item("MagicDNS 名", (me.get("DNSName") or "").rstrip(".") or "-"),
        _item("アカウント", login or "-"),
    ]

    peers = []
    for peer in (data.get("Peer") or {}).values():
        online = bool(peer.get("Online"))
        peers.append({
            "name": (peer.get("DNSName") or "").split(".")[0] or peer.get("HostName", "?"),
            "ip": (peer.get("TailscaleIPs") or [""])[0],
            "os": peer.get("OS", ""),
            "online": online,
            "last_seen": "" if online else _ts_time_ago(peer.get("LastSeen", "")),
            "rx": peer.get("RxBytes", 0),
            "tx": peer.get("TxBytes", 0),
            "relay": peer.get("Relay", ""),
            "direct": bool(peer.get("CurAddr")),
        })
    peers.sort(key=lambda p: (not p["online"], p["name"]))
    items.append(_item("オンライン", f"{sum(p['online'] for p in peers)} / {len(peers)} 台"))

    return {
        "level": OK if running else ERROR,
        "summary": "接続中" if running else "停止中",
        "running": running,
        "items": items,
        "peers": peers,
        "serve": serve_mappings(),
    }


# ---------------------------------------------------------------- Nextcloud

@cached(5)
def _nextcloud_status() -> dict:
    with urllib.request.urlopen(NEXTCLOUD_URL, timeout=5) as resp:
        return json.load(resp)


@cached(5)
def _docker_ps() -> tuple[int, str]:
    return _run(
        "docker", "ps", "-a",
        "--filter", f"label=com.docker.compose.project={NEXTCLOUD_COMPOSE_PROJECT}",
        "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}",
    )


def collect_nextcloud(snap: dict) -> dict:
    items = []
    levels = []
    maintenance = None
    try:
        st = _nextcloud_status()
        maintenance = bool(st.get("maintenance"))
        items.append(_item("Web 応答", "OK", OK))
        items.append(_item("バージョン", st.get("versionstring", "?")))
        items.append(_item("メンテナンス", "ON" if maintenance else "OFF", WARN if maintenance else OK))
        levels.append(WARN if maintenance else OK)
        if st.get("needsDbUpgrade"):
            levels.append(WARN)
            items.append(_item("DB アップグレード", "必要", WARN))
    except Exception as ex:  # noqa: BLE001
        levels.append(ERROR)
        items.append(_item("Web 応答", f"応答なし ({ex.__class__.__name__})", ERROR))

    ms = snap["latency"]["nextcloud"]
    if ms is not None:
        items.append(_item("応答時間", f"{ms:.0f} ms"))

    code, out = _docker_ps()
    containers = []
    if code == 0:
        for line in out.splitlines():
            name, state, status, image = (line.split("\t") + ["", "", ""])[:4]
            lv = OK if state == "running" and "unhealthy" not in status else ERROR
            levels.append(lv)
            stats = snap["containers"].get(name, {})
            containers.append({"name": name, "state": state, "status": status, "image": image,
                               "level": lv, **stats})
        containers.sort(key=lambda c: c["name"])
    else:
        levels.append(WARN)
        items.append(_item("コンテナ", "docker から取得できません", WARN))

    if NEXTCLOUD_DATA_DIR and os.path.ismount(NEXTCLOUD_DATA_DIR):
        du = shutil.disk_usage(NEXTCLOUD_DATA_DIR)
        items.append(_item(f"データ ({NEXTCLOUD_DATA_DIR})", f"{fmt_bytes(du.used)} / {fmt_bytes(du.total)}"))

    level = _worst(*levels)
    return {
        "level": level,
        "summary": {OK: "稼働中", WARN: "注意", ERROR: "異常あり"}[level],
        "items": items,
        "maintenance": maintenance,
        "containers": containers,
        "history": {"t": snap["slow"]["t"], "nextcloud_ms": snap["slow"]["nextcloud_ms"]},
    }


# ---------------------------------------------------------------- まとめ

COLLECTORS = {
    "pi": collect_pi,
    "network": collect_network,
    "tailscale": collect_tailscale,
    "nextcloud": collect_nextcloud,
}


def _safe(fn, snap) -> dict:
    try:
        return fn(snap)
    except Exception as ex:  # noqa: BLE001 - 1つの失敗でページ全体を落とさない
        return {"level": ERROR, "summary": "取得エラー",
                "items": [_item("エラー", f"{ex.__class__.__name__}: {ex}", ERROR)]}


def collect_all() -> dict:
    snap = SAMPLER.snapshot()
    with ThreadPoolExecutor(max_workers=len(COLLECTORS)) as pool:
        futures = {key: pool.submit(_safe, fn, snap) for key, fn in COLLECTORS.items()}
        sections = {key: fut.result() for key, fut in futures.items()}
    try:
        vers = versions.get_versions()
    except Exception:  # noqa: BLE001
        vers = {}
    for key, section in sections.items():
        section["versions"] = [{"name": n, "version": v} for n, v in vers.get(key, [])]
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "sections": sections}
