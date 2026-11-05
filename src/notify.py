"""ntfy でスマホに通知する。

インターネットが切れている間に起きた通知は手元にためておき、つながった時点で
発生時刻を添えて送る（最大 1 日分）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import settings

# 通知をタップしたときに開く URL
CLICK_URL = f"https://{os.environ.get('PASSKEY_RP_ID', 'localhost')}/"
MAX_AGE = 86400          # これより古い未送信の通知は捨てる
PRIORITY = {"error": 4, "warn": 3, "info": 2, "ok": 2}
TAGS = {"error": ["rotating_light"], "warn": ["warning"], "info": ["information_source"], "ok": ["white_check_mark"]}


class NotifyError(Exception):
    pass


def _in_quiet(quiet: str, now: time.struct_time) -> bool:
    if not quiet:
        return False
    start, _, end = quiet.partition("-")
    cur = now.tm_hour * 60 + now.tm_min
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    return s <= cur < e if s <= e else cur >= s or cur < e


def post(title: str, message: str, level: str = "info", page: str = "", conf: dict | None = None) -> None:
    """すぐに 1 件送る（失敗すると NotifyError）。"""
    n = conf or settings.get()["notify"]
    body = {
        "topic": n["topic"],
        "title": title,
        "message": message,
        "priority": PRIORITY.get(level, 3),
        "tags": TAGS.get(level, []),
        "click": CLICK_URL + (f"#{page}" if page else ""),
    }
    headers = {"Content-Type": "application/json"}
    if n.get("token"):
        headers["Authorization"] = f"Bearer {n['token']}"
    req = urllib.request.Request(n["server"].rstrip("/"), data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as ex:
        try:
            detail = json.load(ex).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise NotifyError(f"ntfy がエラーを返しました ({ex.code}{': ' + detail if detail else ''})") from None
    except OSError as ex:
        raise NotifyError(f"ntfy に接続できません ({ex.__class__.__name__})") from None


class Notifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[dict] = []
        self._wake = threading.Event()
        self.last_error: str | None = None
        self.last_sent: float | None = None

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="notify").start()

    def send(self, title: str, message: str, level: str = "info", page: str = "alerts") -> None:
        """送信待ちに積む（すぐ戻る）。設定で通知がオフなら何もしない。"""
        n = settings.get()["notify"]
        if not n["enabled"]:
            return
        # 静かにする時間帯は「異常」以外を送らない
        if level != "error" and _in_quiet(n.get("quiet", ""), time.localtime()):
            return
        with self._lock:
            self._queue.append({"title": title, "message": message, "level": level, "page": page, "at": time.time()})
        self._wake.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def _loop(self) -> None:
        backoff = 5.0
        while True:
            self._wake.wait(timeout=backoff)
            self._wake.clear()
            while True:
                with self._lock:
                    self._queue = [i for i in self._queue if time.time() - i["at"] < MAX_AGE]
                    item = self._queue[0] if self._queue else None
                if item is None:
                    backoff = 60.0
                    break
                message = item["message"]
                late = time.time() - item["at"]
                if late > 90:
                    message += f"\n（{time.strftime('%m/%d %H:%M', time.localtime(item['at']))} に発生。通信できなかったため遅れて通知）"
                try:
                    post(item["title"], message, item["level"], item["page"])
                except NotifyError as ex:
                    if self.last_error != str(ex):
                        print(f"notify: {ex}（{self.pending()} 件を保持）", flush=True)
                    self.last_error = str(ex)
                    backoff = min(300.0, max(5.0, backoff * 2))
                    break
                with self._lock:
                    if self._queue and self._queue[0] is item:
                        self._queue.pop(0)
                self.last_error, self.last_sent = None, time.time()
                backoff = 5.0


NOTIFIER = Notifier()


def send(title: str, message: str, level: str = "info", page: str = "alerts") -> None:
    NOTIFIER.send(title, message, level, page)


def status() -> dict:
    return {"pending": NOTIFIER.pending(), "last_error": NOTIFIER.last_error, "last_sent": NOTIFIER.last_sent}
