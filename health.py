"""アップデートの有無・ディスクの健康状態・バックアップの状態。

どれも変化がゆっくりなので、裏のスレッドで定期的に調べて結果を持っておく
（画面の 2 秒ごとの更新では、持っている結果を返すだけ）。

SMART（ディスク自体の自己診断）は root 権限が要るため、
deploy/home-status-smartctl.sudoers を入れたときだけ読む。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import alerts
import cloud

UPDATE_INTERVAL = 3600       # アップデート確認の間隔（秒）
DISK_INTERVAL = 1800         # ディスクの健康状態の確認間隔（秒）
# バックアップを置いているディレクトリ（空なら「未設定」と表示）
BACKUP_DIR = Path(os.environ["BACKUP_DIR"]) if os.environ.get("BACKUP_DIR", "").strip() else None
NEXTCLOUD_APP = os.environ.get("NEXTCLOUD_APP_CONTAINER", "nextcloud-app-1")
# SMART を読むディスク（カンマ区切り。deploy/home-status-smartctl.sudoers と揃える）
SMART_DISKS = tuple(d.strip() for d in os.environ.get("SMART_DISKS", "sda").split(",")
                    if re.fullmatch(r"[a-z]+[0-9]*", d.strip()))

_lock = threading.Lock()
_state: dict = {"updates": None, "disks": None, "checked": {}}


def _run(*args: str, timeout: float = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------- アップデート

def check_updates() -> dict:
    code, out = _run("apt", "list", "--upgradable", timeout=60)
    packages = []
    for line in out.splitlines():
        m = re.match(r"^([^/\s]+)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]", line)
        if m:
            packages.append({"name": m.group(1), "source": m.group(2), "version": m.group(3),
                             "current": m.group(4), "security": "-security" in m.group(2)})
    try:
        apt_checked = os.path.getmtime("/var/lib/apt/periodic/update-success-stamp")
    except OSError:
        apt_checked = None

    _, nc = _run("docker", "exec", "-u", "www-data", NEXTCLOUD_APP, "php", "occ", "update:check", timeout=90)
    nc_updates = [line.strip().split(". Get more")[0] for line in nc.splitlines() if " is available" in line]

    return {
        "checked_at": time.time(),
        "apt": {"count": len(packages), "security": sum(p["security"] for p in packages),
                "packages": packages[:100], "checked_at": apt_checked, "ok": code == 0},
        "reboot_required": os.path.exists("/var/run/reboot-required"),
        "nextcloud": nc_updates,
    }


# ---------------------------------------------------------------- ディスク

_smart_denied = False


def _smart(dev: str) -> dict:
    global _smart_denied
    unset = {"available": False, "reason": "未設定（README の「ディスクの健康状態（SMART）」を参照）"}
    # 入っていない・許可がないときは試さない（sudo が認証ログに失敗を残すため）
    if _smart_denied or not os.path.exists("/usr/sbin/smartctl"):
        return unset
    code, out = _run("sudo", "-n", "/usr/sbin/smartctl", "-j", "-a", "-d", "sat", f"/dev/{dev}", timeout=30)
    if code in (124, 127) or "password" in out:
        _smart_denied = True  # sudoers を入れたらサービスを再起動する
        return unset
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"available": False, "reason": "SMART を読めませんでした"}
    if not data.get("smart_status") and not data.get("ata_smart_attributes"):
        msgs = "; ".join(m.get("string", "") for m in (data.get("smartctl") or {}).get("messages") or [])
        return {"available": False, "reason": msgs or "このディスク（USB 変換）は SMART に対応していません"}
    attrs = {a["id"]: a for a in (data.get("ata_smart_attributes") or {}).get("table") or []}

    def raw(i: int) -> int | None:
        a = attrs.get(i)
        return a["raw"]["value"] if a else None

    life = None
    for i in (231, 169, 202, 233):  # 製品ごとに違う「残り寿命」の属性
        if i in attrs:
            life = attrs[i].get("value")
            break
    passed = (data.get("smart_status") or {}).get("passed")
    reallocated, pending = raw(5), raw(197)
    level = "ok"
    if passed is False or (pending or 0) > 0:
        level = "error"
    elif (reallocated or 0) > 0 or (life is not None and life < 10):
        level = "warn"
    return {
        "available": True,
        "model": data.get("model_name", ""),
        "passed": passed,
        "temp": (data.get("temperature") or {}).get("current"),
        "power_on_hours": (data.get("power_on_time") or {}).get("hours"),
        "life_left": life,
        "reallocated": reallocated,
        "pending": pending,
        "level": level,
    }


def _fs_errors() -> dict[str, int]:
    out = {}
    for d in Path("/sys/fs/ext4").glob("*/errors_count"):
        try:
            out[d.parent.name] = int(d.read_text().strip())
        except (OSError, ValueError):
            pass
    return out


def _kernel_disk_errors() -> list[str]:
    """この起動以降の、ディスク関係のカーネルのエラー。"""
    _, out = _run("journalctl", "-k", "-b", "-p", "err", "--no-pager", "-o", "short-iso",
                  "-g", r"I/O error|EXT4-fs error|mmc[0-9].*(error|timeout)|Buffer I/O|blk_update_request|uas_eh",
                  timeout=20)
    return [line for line in out.splitlines() if line and not line.startswith("-- ")][-20:]


def check_disks() -> dict:
    disks = {dev: _smart(dev) for dev in SMART_DISKS if os.path.exists(f"/dev/{dev}")}
    # SD カードは SMART が無いので、起動以降に書いた量だけ示す
    try:
        with open("/sys/block/mmcblk0/stat") as f:
            written = int(f.read().split()[6]) * 512
    except (OSError, ValueError, IndexError):
        written = None
    return {
        "checked_at": time.time(),
        "smart": disks,
        "fs_errors": _fs_errors(),
        "kernel_errors": _kernel_disk_errors(),
        "sd_written": written,
    }


# ---------------------------------------------------------------- バックアップ

def _newest(path: Path, depth: int = 2) -> tuple[float, str] | None:
    best = None
    try:
        for entry in os.scandir(path):
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, entry.path)
            if depth > 1 and entry.is_dir(follow_symlinks=False):
                sub = _newest(Path(entry.path), depth - 1)
                if sub and sub[0] > best[0]:
                    best = sub
    except OSError:
        return None
    return best


def backup_status() -> dict:
    """D1（このサイトの記録）と Nextcloud のバックアップの状態。"""
    cs = cloud.status()
    d1 = {
        "label": "このサイトの記録（Cloudflare D1）",
        "configured": cs["configured"],
        "last": cs["last_ok"],
        "pending": cs["pending"],
        "error": cs["last_error"],
        "note": "D1 は Cloudflare 側で 30 日前までの任意の時点に戻せます（Time Travel）",
    }
    nc = {"label": f"Nextcloud（{BACKUP_DIR or '未設定'}）", "path": str(BACKUP_DIR or "")}
    if BACKUP_DIR is None or not BACKUP_DIR.exists():
        nc.update(configured=False, last=None, note="バックアップは設定されていません")
    else:
        newest = _newest(BACKUP_DIR)
        nc.update(configured=True, last=newest[0] if newest else None,
                  latest=newest[1] if newest else None, free=shutil.disk_usage(BACKUP_DIR).free)
    return {"d1": d1, "nextcloud": nc}


# ---------------------------------------------------------------- まとめ

def snapshot() -> dict:
    with _lock:
        return {"updates": _state["updates"], "disks": _state["disks"], "backup": backup_status()}


def _loop() -> None:
    next_updates = next_disks = 0.0
    while True:
        now = time.time()
        try:
            if now >= next_disks:
                disks = check_disks()
                with _lock:
                    _state["disks"] = disks
                next_disks = now + DISK_INTERVAL
            if now >= next_updates:
                updates = check_updates()
                with _lock:
                    _state["updates"] = updates
                next_updates = now + UPDATE_INTERVAL
        except Exception as ex:  # noqa: BLE001
            print(f"health: {ex!r}", flush=True)
        time.sleep(60)


def refresh_updates() -> None:
    updates = check_updates()
    with _lock:
        _state["updates"] = updates


def conditions(conf: dict) -> list[alerts.Condition]:
    out = []
    rules = conf["rules"]
    snap = snapshot()
    disks = snap["disks"] or {}
    if rules["smart"]["enabled"]:
        for dev, s in (disks.get("smart") or {}).items():
            if s.get("available") and s["level"] != "ok":
                out.append(alerts.Condition("smart", f"smart:{dev}", s["level"], f"ディスク {dev} の健康状態が悪化しています",
                                            f"{s.get('model', '')} 代替処理済み {s.get('reallocated')} / 保留 {s.get('pending')}"
                                            f" / 残り寿命 {s.get('life_left') if s.get('life_left') is not None else '-'}%"))
        for fs, n in (disks.get("fs_errors") or {}).items():
            if n > 0:
                out.append(alerts.Condition("smart", f"fs:{fs}", "warn", f"{fs} のファイルシステムにエラーがあります",
                                            f"エラー {n} 件。fsck で確認してください"))
        if disks.get("kernel_errors"):
            out.append(alerts.Condition("smart", "kernel-io", "warn", "ディスクの読み書きでエラーが出ています",
                                        disks["kernel_errors"][-1][:200]))
    if rules["backup"]["enabled"]:
        nc = snap["backup"]["nextcloud"]
        hours = float(rules["backup"]["threshold"])
        if not nc["configured"]:
            out.append(alerts.Condition("backup", "backup:nextcloud", "warn", "Nextcloud のバックアップがありません",
                                        f"{nc['path']} が見つかりません"))
        elif not nc.get("last") or time.time() - nc["last"] > hours * 3600:
            out.append(alerts.Condition("backup", "backup:nextcloud", "warn", "Nextcloud のバックアップが古くなっています",
                                        f"最後のバックアップから {hours:.0f} 時間以上たっています"))
    if rules["updates"]["enabled"]:
        up = snap["updates"] or {}
        sec = (up.get("apt") or {}).get("security", 0)
        if sec:
            out.append(alerts.Condition("updates", "updates:security", "warn", "セキュリティ更新があります",
                                        f"{sec} 個のパッケージ。sudo apt upgrade で更新できます"))
    return out


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="health").start()
    alerts.ENGINE.register(conditions)
