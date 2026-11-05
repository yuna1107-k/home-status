"""通知・アラートなどのサイト全体の設定。

data/settings.json に保存し、D1 にも控えを置く（Pi を作り直しても戻る）。
受け付ける値は DEFAULTS と同じ形のものだけ。
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
from pathlib import Path

import cloud

STORE = Path(os.environ.get("SETTINGS_STORE", str(cloud.DATA_DIR / "settings.json")))
KV_KEY = "settings"
_lock = threading.Lock()

# アラートの規則: id -> (表示名, しきい値の単位, 既定のしきい値, 既定で有効か, 続いたら通知する秒数)
RULES = {
    "temp":        ("CPU 温度が高い", "℃", 80, True, 60),
    "disk":        ("ディスクの空きが少ない", "%", 90, True, 0),
    "memory":      ("メモリが足りない", "%", 95, True, 120),
    "nextcloud":   ("Nextcloud が応答しない", None, None, True, 60),
    "service":     ("サービスが止まった", None, None, True, 60),
    "tailscale":   ("Tailscale が切れた", None, None, True, 60),
    "internet":    ("インターネットにつながらない", None, None, True, 120),
    "voltage":     ("電圧低下が発生している", None, None, True, 0),
    "smart":       ("ディスクの健康状態が悪い", None, None, True, 0),
    "backup":      ("バックアップが古い", "時間", 48, False, 0),
    "updates":     ("セキュリティ更新がある", None, None, False, 0),
    "new_device":  ("LAN に知らない端末がつながった", None, None, True, 0),
}

MASK = "********"  # 画面に返すときのトークンの代わり
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _defaults() -> dict:
    return {
        "notify": {
            "enabled": True,
            "server": "https://ntfy.sh",
            "topic": "hs-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y"),
            "recovery": True,       # 復旧したときも通知する
            "quiet": "",            # 静かにする時間帯 "23:00-07:00"（警告だけ抑える。異常は常に通知）
            # ntfy のアクセストークン（tk_…）。Cloudflare の見張りから送るには必要
            # （ntfy.sh は送信元 IP ごとに 1 日の上限があり、Workers の IP は共有のため上限に当たる）
            "token": "",
        },
        "rules": {rid: {"enabled": r[3], "threshold": r[2]} for rid, r in RULES.items()},
        # 監視するサービス（collectors.SERVICES の id）
        "watch_services": ["cloudflared", "tailscaled", "docker", "nextcloud"],
    }


def _load_raw() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


_cache: dict | None = None


def get() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            raw = _load_raw()
            _cache = _merge(_defaults(), raw)
            if not raw:
                # 初回: トピック名を固定するため手元にだけ保存する。D1 の控えは上書きしない
                # （Pi を作り直した直後に D1 へ届かなかった場合でも、元の設定を消さないため）
                _write({**_cache, "_fresh": True}, push=False)
        return copy.deepcopy(_cache)


def _write(data: dict, push: bool = True) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STORE)
    if push:
        cloud.kv_put(KV_KEY, data)


def update(changes: object) -> dict:
    """画面から届いた変更を検証して保存し、保存後の設定を返す。"""
    global _cache
    if not isinstance(changes, dict):
        raise ValueError("設定の形式が不正です")
    cur = get()
    if "notify" in changes:
        n = changes["notify"]
        if not isinstance(n, dict):
            raise ValueError("通知の設定が不正です")
        if "enabled" in n:
            cur["notify"]["enabled"] = bool(n["enabled"])
        if "recovery" in n:
            cur["notify"]["recovery"] = bool(n["recovery"])
        if "server" in n:
            server = str(n["server"]).strip().rstrip("/")
            if not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?$", server):
                raise ValueError("ntfy のサーバーは https://ホスト名 で指定してください")
            cur["notify"]["server"] = server
        if "topic" in n:
            topic = str(n["topic"]).strip()
            if not _TOPIC_RE.match(topic):
                raise ValueError("トピック名は英数字・-・_ の 8〜64 文字にしてください（推測されにくい名前に）")
            cur["notify"]["topic"] = topic
        if "token" in n and n["token"] != MASK:
            tok = str(n["token"]).strip()
            if tok and not re.match(r"^tk_[A-Za-z0-9]{20,64}$", tok):
                raise ValueError("アクセストークンは tk_ で始まる文字列です")
            cur["notify"]["token"] = tok
        if "quiet" in n:
            quiet = str(n["quiet"]).strip()
            if quiet and not re.match(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$", quiet):
                raise ValueError("静かにする時間帯は 23:00-07:00 の形で指定してください")
            cur["notify"]["quiet"] = quiet
    if "rules" in changes:
        rules = changes["rules"]
        if not isinstance(rules, dict):
            raise ValueError("アラートの設定が不正です")
        for rid, val in rules.items():
            if rid not in RULES or not isinstance(val, dict):
                continue
            if "enabled" in val:
                cur["rules"][rid]["enabled"] = bool(val["enabled"])
            if "threshold" in val and RULES[rid][1] is not None:
                try:
                    th = float(val["threshold"])
                except (TypeError, ValueError):
                    raise ValueError(f"{RULES[rid][0]} のしきい値は数値で指定してください") from None
                if not 1 <= th <= 1000:
                    raise ValueError(f"{RULES[rid][0]} のしきい値が範囲外です")
                cur["rules"][rid]["threshold"] = th
    with _lock:
        _cache = cur
        _write(cur)
    return copy.deepcopy(cur)


def _forget() -> None:
    global _cache
    with _lock:
        _cache = None


def restore() -> None:
    """起動時: 手元に設定が無い（初期値のまま）なら D1 の控えから戻す。"""
    cloud.restore_later(KV_KEY, STORE, lambda v: not v or bool(v.get("_fresh")), _forget, _publish)


def _publish() -> None:
    """D1 に設定が無いと分かったら、手元の初期値を D1 に置く（見張りの Worker が通知先を読めるように）。"""
    global _cache
    cur = get()
    with _lock:
        _cache = cur
        _write(cur)
