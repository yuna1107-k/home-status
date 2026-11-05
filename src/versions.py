"""各タブに表示するバージョン情報。めったに変わらないので 10 分キャッシュする。"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

CACHE_SECONDS = 600
NEXTCLOUD_APP = os.environ.get("NEXTCLOUD_APP_CONTAINER", "nextcloud-app-1")
NEXTCLOUD_DB = os.environ.get("NEXTCLOUD_DB_CONTAINER", "nextcloud-db-1")
NEXTCLOUD_REDIS = os.environ.get("NEXTCLOUD_REDIS_CONTAINER", "nextcloud-redis-1")
STATUS_DIR = Path(__file__).resolve().parent.parent

_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def _out(*args: str, timeout: float = 10) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or proc.stderr).strip()


def _first(pattern: str, text: str, default: str = "不明") -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else default


def _git(path: Path) -> str:
    info = _out("git", "-C", str(path), "log", "-1", "--format=%h (%cd)", "--date=format:%Y-%m-%d %H:%M")
    if not info:
        return "不明"
    dirty = _out("git", "-C", str(path), "status", "--porcelain", "--untracked-files=no")
    return info + (" + 未コミットの変更" if dirty else "")


def _os_name() -> str:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return "不明"
    return _first(r'PRETTY_NAME="([^"]+)"', text)


def _container_version(container: str, *cmd: str, pattern: str) -> str:
    return _first(pattern, _out("docker", "exec", container, *cmd))


def _collect() -> dict:
    firmware = _out("vcgencmd", "version").splitlines()
    eeprom = _out("vcgencmd", "bootloader_version").splitlines()

    ts = _out("tailscale", "version").splitlines()
    try:
        ts_status = json.loads(_out("tailscale", "status", "--json") or "{}")
    except json.JSONDecodeError:
        ts_status = {}

    nc_occ = _out("docker", "exec", "-u", "www-data", NEXTCLOUD_APP, "php", "occ", "-V")

    cloudflared = _out("cloudflared", "--version")

    return {
        "pi": [
            ("OS", _os_name()),
            ("カーネル", platform.release()),
            ("ファームウェア", firmware[0] if firmware else "不明"),
            ("ブートローダー", eeprom[0] if eeprom else "不明"),
            ("Python", platform.python_version()),
            ("Docker Engine", _out("docker", "version", "--format", "{{.Server.Version}}") or "不明"),
            ("ステータスサイト", _git(STATUS_DIR)),
        ],
        "network": [
            ("iproute2", _first(r"iproute2-v?([\d.]*\d)", _out("ip", "-V"))),
            ("iputils (ping)", _first(r"iputils[- ](\S+)", _out("ping", "-V"))),
            ("cloudflared", _first(r"version (\S+)", cloudflared, "未インストール")),
        ],
        "tailscale": [
            ("クライアント", ts[0] if ts else "不明"),
            ("tailscaled", (ts_status.get("Version") or "不明").split("-")[0]),
        ],
        "nextcloud": [
            ("Nextcloud", _first(r"Nextcloud (\S+)", nc_occ)),
            ("PHP", _container_version(NEXTCLOUD_APP, "php", "-r", "echo PHP_VERSION;", pattern=r"(\S+)")),
            ("Apache", _container_version(NEXTCLOUD_APP, "apache2", "-v", pattern=r"Apache/(\S+)")),
            ("MariaDB", _container_version(NEXTCLOUD_DB, "mariadb", "--version", pattern=r"Distrib ([\d.]+)")),
            ("Redis", _container_version(NEXTCLOUD_REDIS, "redis-server", "--version", pattern=r"v=(\S+)")),
        ],
    }


def get_versions() -> dict:
    """{タブ: [(名前, バージョン), ...]} を返す（10 分キャッシュ）。"""
    global _cache
    with _lock:
        if _cache is None or time.monotonic() - _cache[0] > CACHE_SECONDS:
            _cache = (time.monotonic(), _collect())
        return _cache[1]


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None
