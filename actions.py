"""ステータスページから実行できる操作。

ここに定義した操作だけを受け付ける。シェルは使わず、引数はすべて検証してから
コマンドに渡す。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import versions
from sampler import NET_IFACE, NEXTCLOUD_COMPOSE_PROJECT

NEXTCLOUD_APP = os.environ.get("NEXTCLOUD_APP_CONTAINER", "nextcloud-app-1")
# このサイト自身を HTTPS で公開している Tailscale Serve のポート（消すと操作画面に入れなくなる）
STATUS_HTTPS_PORT = 8443
STATUS_LOCAL_PORT = 8092


class ActionError(Exception):
    pass


def _run(*args: str, timeout: float = 60) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ActionError(f"タイムアウトしました: {args[0]}") from None
    except FileNotFoundError:
        raise ActionError(f"{args[0]} が見つかりません") from None
    if proc.returncode != 0:
        raise ActionError((proc.stderr or proc.stdout).strip() or f"{args[0]} が失敗しました")
    return proc.stdout.strip()


def _background(cmd: list[str], title: str, after=None) -> None:
    """時間のかかるコマンドを裏で動かす（画面への応答を待たせない）。結果はログに残す。"""
    def work() -> None:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            ok = proc.returncode == 0
            print(f"  {title}: {'完了' if ok else '失敗 ' + (proc.stderr or proc.stdout).strip()[:300]}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"  {title}: タイムアウト", flush=True)
        if after:
            after()
    threading.Thread(target=work, daemon=True).start()


def _nextcloud_containers() -> list[str]:
    out = _run("docker", "ps", "-a", "--filter",
               f"label=com.docker.compose.project={NEXTCLOUD_COMPOSE_PROJECT}",
               "--format", "{{.Names}}")
    return [n for n in out.splitlines() if n]


# ---------------------------------------------------------------- Raspberry Pi（サービス）

def service_restart(params: dict) -> str:
    service = params.get("id")
    if service == "nextcloud":
        names = _nextcloud_containers()
        _background(["docker", "restart", *names], "Nextcloud 全体の再起動")
        return f"Nextcloud のコンテナ {len(names)} 個の再起動を始めました。1 分ほどで戻ります"
    if service == "home-status":
        # 応答を返してから自分自身を再起動する
        threading.Timer(1.0, lambda: subprocess.run(
            ["systemctl", "--user", "restart", "home-status"], check=False)).start()
        return "このサイトを再起動します。数秒後に自動で再接続します"
    raise ActionError("不明なサービスです")


def _can_reboot() -> str:
    try:
        out = _run("busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                   "org.freedesktop.login1.Manager", "CanReboot", timeout=10)
    except ActionError:
        return "unknown"
    return out.split()[-1].strip('"') if out else "unknown"


def pi_reboot(params: dict) -> str:
    """Pi 本体を再起動する（polkit でこのサービスのユーザーに再起動だけ許可している前提）。"""
    can = _can_reboot()
    if can != "yes":
        raise ActionError("再起動の権限がありません。deploy/49-home-status-reboot.rules を "
                          "/etc/polkit-1/rules.d/ に置いてください")
    print("pi.reboot requested", flush=True)
    # 応答を返してから再起動する
    threading.Timer(2.0, lambda: subprocess.run(["systemctl", "reboot"], check=False)).start()
    return "Pi を再起動します。1〜2 分で自動的に再接続します"


# ---------------------------------------------------------------- Nextcloud

def nextcloud_maintenance(params: dict) -> str:
    on = params.get("on")
    if not isinstance(on, bool):
        raise ActionError("on は true / false で指定してください")
    _run("docker", "exec", "-u", "www-data", NEXTCLOUD_APP, "php", "occ",
         "maintenance:mode", "--on" if on else "--off")
    return f"メンテナンスモードを {'ON' if on else 'OFF'} にしました"


def nextcloud_restart(params: dict) -> str:
    name = params.get("container")
    if name not in _nextcloud_containers():
        raise ActionError("Nextcloud のコンテナではありません")
    _background(["docker", "restart", name], f"{name} の再起動", after=versions.invalidate)
    return f"{name} の再起動を始めました。数十秒で戻ります"


# ---------------------------------------------------------------- Tailscale

def _port(value: object, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ActionError(f"{name} は数値で指定してください") from None
    if not 1 <= port <= 65535:
        raise ActionError(f"{name} は 1〜65535 で指定してください")
    return port


def tailscale_serve_add(params: dict) -> str:
    https_port = _port(params.get("port"), "公開ポート")
    target = _port(params.get("target"), "転送先ポート")
    if https_port == STATUS_HTTPS_PORT:
        raise ActionError(f"{STATUS_HTTPS_PORT} はこのサイト用なので変更できません")
    _run("tailscale", "serve", "--bg", f"--https={https_port}", f"http://127.0.0.1:{target}")
    return f"https の {https_port} 番を 127.0.0.1:{target} に転送するようにしました"


def tailscale_serve_remove(params: dict) -> str:
    https_port = _port(params.get("port"), "公開ポート")
    if https_port == STATUS_HTTPS_PORT:
        raise ActionError(f"{STATUS_HTTPS_PORT} はこのサイト用なので削除できません")
    _run("tailscale", "serve", f"--https={https_port}", "off")
    return f"https の {https_port} 番の公開をやめました"


def tailscale_set_running(params: dict) -> str:
    """Tailscale の接続をオン/オフする（公式 CLI の `tailscale up` / `tailscale down`）。

    サービスのユーザーを operator にしておく必要がある: `sudo tailscale set --operator=$USER`
    """
    running = params.get("running")
    if not isinstance(running, bool):
        raise ActionError("running は true / false で指定してください")
    _run("tailscale", "up" if running else "down", timeout=30)
    return "Tailscale をオンにしました" if running else "Tailscale をオフにしました"


def tailscale_state() -> dict:
    try:
        data = json.loads(_run("tailscale", "status", "--json", timeout=10))
    except (ActionError, json.JSONDecodeError):
        # 停止中は終了コード 1 でも JSON を返すので、もう一度そのまま読む
        proc = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"backend": "不明", "running": False, "online": False, "connected": False}
    me = data.get("Self") or {}
    running = data.get("BackendState") == "Running"
    online = bool(me.get("Online"))
    return {
        "backend": data.get("BackendState", "?"),
        "running": running,
        # 起動しているだけでなく、Tailscale のネットワークに実際につながっているか
        "online": online,
        "connected": running and online,
        "host": me.get("HostName", ""),
        "ip": (me.get("TailscaleIPs") or [""])[0],
    }


# ---------------------------------------------------------------- ログ表示・ネットワーク

# 表示できるログ: id -> (表示名, コマンド)。docker のコンテナは nextcloud のものだけ別途許可
LOG_SOURCES = {
    "home-status": ("home-status", ["journalctl", "--user", "-u", "home-status", "--no-pager", "-o", "short-iso"]),
    "cloudflared": ("cloudflared", ["journalctl", "-u", "cloudflared", "--no-pager", "-o", "short-iso"]),
    "tailscaled": ("tailscaled", ["journalctl", "-u", "tailscaled", "--no-pager", "-o", "short-iso"]),
    "docker": ("Docker Engine", ["journalctl", "-u", "docker", "--no-pager", "-o", "short-iso"]),
}


def logs_view(params: dict) -> dict:
    source = str(params.get("source", ""))
    try:
        lines = max(20, min(400, int(params.get("lines", 120))))
    except (TypeError, ValueError):
        lines = 120
    if source == "nextcloud":
        source = NEXTCLOUD_APP
    if source in LOG_SOURCES:
        title, cmd = LOG_SOURCES[source]
        cmd = cmd + ["-n", str(lines)]
    elif source in _nextcloud_containers():
        title, cmd = source, ["docker", "logs", "--tail", str(lines), "--timestamps", source]
    else:
        raise ActionError("表示できないログです")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise ActionError("ログの取得がタイムアウトしました") from None
    text = (proc.stdout + proc.stderr).strip() or "（ログはありません）"
    return {"message": f"{title} のログ（直近 {lines} 行）", "data": {"title": title, "text": text[-200_000:]}}


def network_scan(params: dict) -> dict:
    """同じ LAN（NET_IFACE の /24 まで）に ping を送り、近くの端末一覧を最新にする。"""
    out = _run("ip", "-4", "-o", "addr", "show", "dev", NET_IFACE)
    m = re.search(r"inet (\S+)", out)
    if not m:
        raise ActionError(f"{NET_IFACE} の IP アドレスが分かりません")
    net = ipaddress.ip_interface(m.group(1)).network
    if net.prefixlen < 24:
        net = ipaddress.ip_interface(f"{m.group(1).split('/')[0]}/24").network

    def ping(ip: str) -> bool:
        return subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True).returncode == 0

    with ThreadPoolExecutor(max_workers=64) as pool:
        alive = sum(pool.map(ping, [str(h) for h in net.hosts()]))
    return {"message": f"{net} をスキャンしました（応答 {alive} 台）"}


# ---------------------------------------------------------------- LAN の端末・アップデート

def lan_device(params: dict) -> str:
    import lan
    mac = str(params.get("mac", ""))
    known = params.get("known")
    label = params.get("label")
    if known is not None and not isinstance(known, bool):
        raise ActionError("known は true / false で指定してください")
    if label is not None and not isinstance(label, str):
        raise ActionError("名前は文字列で指定してください")
    try:
        d = lan.set_device(mac, known, label)
    except KeyError:
        raise ActionError("その端末はまだ記録されていません") from None
    name = d.get("label") or d.get("name") or d["mac"]
    if known is True:
        return f"{name} を既知の端末にしました"
    if known is False:
        return f"{name} を未確認に戻しました"
    return f"{name} の名前を保存しました"


def lan_known_all(params: dict) -> str:
    import lan
    return f"{lan.known_all()} 台を既知の端末にしました"


def health_refresh(params: dict) -> str:
    import health
    health.refresh_updates()
    return "アップデートを確認しました"


# ---------------------------------------------------------------- 登録

# メイン画面（Tailscale 経由）から実行できる操作。
# Tailscale のオン/オフは、オフにすると自分の接続も切れるため Cloudflare 側の画面だけに置く。
ACTIONS = {
    "service.restart": service_restart,
    "pi.reboot": pi_reboot,
    "nextcloud.maintenance": nextcloud_maintenance,
    "nextcloud.restart": nextcloud_restart,
    "tailscale.serve.add": tailscale_serve_add,
    "tailscale.serve.remove": tailscale_serve_remove,
    "logs.view": logs_view,
    "network.scan": network_scan,
    "lan.device": lan_device,
    "lan.known_all": lan_known_all,
    "health.refresh": health_refresh,
}


def run_action(name: str, params: dict) -> dict:
    """{"message": str, "data"?: ...} を返す。"""
    fn = ACTIONS.get(name)
    if fn is None:
        raise ActionError(f"不明な操作です: {name}")
    result = fn(params if isinstance(params, dict) else {})
    return result if isinstance(result, dict) else {"message": result}
