"""操作の記録（いつ・誰が・どの端末（パスキー）から・何をしたか）。

正本は D1 の audit。D1 に届かないときのために直近の分を手元にも持つ。
journal（標準出力）にも従来どおり 1 行ずつ出す。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque

import cloud

_recent: deque[dict] = deque(maxlen=300)
_lock = threading.Lock()

# 記録に残さない（ログの閲覧など、状態を変えない）操作
READ_ONLY = {"logs.view"}
# パラメータのうち記録しないもの
SECRET_KEYS = {"response", "clientDataJSON", "attestationObject", "signature", "authenticatorData",
               "token", "topic", "password", "otp", "code", "credential", "key_a", "key_b", "paste", "webhook_url"}


def _clean(params: object) -> object:
    if isinstance(params, dict):
        return {k: ("…" if k in SECRET_KEYS else _clean(v)) for k, v in params.items()}
    if isinstance(params, list):
        return [_clean(v) for v in params[:50]]
    if isinstance(params, str):
        return params[:300]
    return params


def record(action: str, *, who: str = "", credential: str = "", params: object = None, ok: bool = True,
           message: str = "", ip: str = "", source: str = "web") -> None:
    row = {
        "id": f"{int(time.time() * 1000):x}-{secrets.token_hex(4)}",
        "ts": int(time.time()),
        "who": who,
        "credential": credential,
        "action": action,
        "params": json.dumps(_clean(params or {}), ensure_ascii=False),
        "ok": 1 if ok else 0,
        "message": (message or "")[:500],
        "ip": ip,
        "source": source,
    }
    with _lock:
        _recent.appendleft(row)
    cloud.write(
        "INSERT OR REPLACE INTO audit (id, ts, host, who, credential, action, params, ok, message, ip, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [row["id"], row["ts"], cloud.HOST, who, credential, action, row["params"], row["ok"],
         row["message"], ip, source],
        kind="audit",
    )


def recent(limit: int = 100, before: int | None = None, query: str = "") -> tuple[list[dict], str]:
    """新しい順。D1 から読み、届かなければ手元の控え（このプロセスの起動以降の分）。"""
    limit = max(1, min(500, limit))
    try:
        sql = "SELECT id, ts, who, credential, action, params, ok, message, ip, source FROM audit WHERE 1=1"
        args: list = []
        if before:
            sql += " AND ts < ?"
            args.append(before)
        if query:
            sql += " AND (action LIKE ? OR message LIKE ? OR credential LIKE ? OR params LIKE ?)"
            args += [f"%{query}%"] * 4
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return cloud.query(sql, args), "d1"
    except cloud.CloudError:
        with _lock:
            rows = [r for r in _recent if (not before or r["ts"] < before)
                    and (not query or query in r["action"] or query in r["message"] or query in r["params"])]
        return rows[:limit], "local"
