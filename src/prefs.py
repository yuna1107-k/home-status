"""表示設定をアカウント（メールアドレス）ごとに保存し、どの端末でも同じ表示にする。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import cloud

KV_KEY = "prefs"
STORE = Path(os.environ.get("PREFS_STORE", str(cloud.DATA_DIR / "prefs.json")))
_lock = threading.Lock()

# 受け付ける項目と値（これ以外は保存しない）
ALLOWED = {
    "wall": {"black", "purple", "blue", "green", "gold"},
    "theme": {"auto", "light", "dark"},
    "clear": set(range(0, 101)),
    "refract": {True, False},
    "accent": {"", "#0a64d6", "#7c3aed", "#0f9d58", "#e8710a", "#db2777", "#0891b2"},
    "range": {30, 90, 180},
    "size": {"small", "normal", "large"},
    "start": {"home", "processes", "performance", "history", "network", "tailscale", "nextcloud", "services",
              "alerts", "audit", "system", "settings"},
}


def _load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def get(email: str) -> dict:
    with _lock:
        return dict(_load().get(email, {}))


def save(email: str, values: object) -> dict:
    if not isinstance(values, dict):
        raise ValueError("設定の形式が不正です")
    clean = {k: v for k, v in values.items()
             if k in ALLOWED and not (isinstance(v, bool) and k in ("range", "clear")) and v in ALLOWED[k]}
    with _lock:
        data = _load()
        data[email] = {**data.get(email, {}), **clean}
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(STORE)
        cloud.kv_put(KV_KEY, data)
        return dict(data[email])


def restore() -> None:
    """起動時: 手元に表示設定が無ければ D1 の控えから戻す。"""
    cloud.restore_later(KV_KEY, STORE, lambda v: not v)
