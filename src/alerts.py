"""異常の検出と通知。

画面用に集めたデータ（collectors.collect_all の結果）を受け取り、settings.RULES の
規則ごとに「いま起きている異常」を洗い出す。同じ異常が規則ごとの秒数だけ続いたら
発生として通知し、消えたら復旧として通知する。発生・復旧は D1 の alerts に残す。

ほかのモジュール（ディスクの健康状態・アップデートなど）は register() で
異常を返す関数を追加できる。一度きりの出来事（知らない端末の接続など）は event() で送る。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

import cloud
import notify
import settings
from sampler import INTERNET_HOST

STATE_FILE = Path(os.environ.get("ALERT_STATE", str(cloud.DATA_DIR / "alerts_state.json")))
CLEAR_AFTER = 30         # 異常が消えてからこの秒数たったら復旧とみなす（ばたつき防止）
HISTORY_LOCAL = 200      # D1 に届かないとき用に手元に持つ履歴の件数


class Condition:
    __slots__ = ("rule", "key", "level", "title", "message")

    def __init__(self, rule: str, key: str, level: str, title: str, message: str) -> None:
        self.rule, self.key, self.level, self.title, self.message = rule, key, level, title, message


def _rule_on(conf: dict, rule: str) -> bool:
    return bool(conf["rules"].get(rule, {}).get("enabled"))


def _th(conf: dict, rule: str) -> float:
    return float(conf["rules"][rule]["threshold"])


def _item(section: dict, label: str) -> dict | None:
    return next((i for i in section.get("items") or [] if i["label"].startswith(label)), None)


def builtin_conditions(data: dict, conf: dict) -> list[Condition]:
    s = data.get("sections") or {}
    pi, net, ts, nc = (s.get(k) or {} for k in ("pi", "network", "tailscale", "nextcloud"))
    out: list[Condition] = []

    if _rule_on(conf, "temp"):
        temps = [t for t in (pi.get("history") or {}).get("temp") or [] if t is not None]
        if temps and temps[-1] >= _th(conf, "temp"):
            out.append(Condition("temp", "temp", "error", "CPU 温度が高くなっています",
                                 f"現在 {temps[-1]:.1f} ℃（しきい値 {_th(conf, 'temp'):.0f} ℃）"))

    if _rule_on(conf, "disk"):
        for d in pi.get("disks") or []:
            if d.get("percent") is None:
                out.append(Condition("disk", f"disk:{d['path']}", "error", f"{d['label']} がマウントされていません",
                                     f"{d['path']} が見えません。ケーブルや電源を確認してください"))
            elif d["percent"] >= _th(conf, "disk"):
                out.append(Condition("disk", f"disk:{d['path']}", "warn", f"{d['label']} の空きが少なくなっています",
                                     f"使用率 {d['percent']:.0f}%（{d.get('text', '')}）"))

    if _rule_on(conf, "memory"):
        mem = pi.get("memory") or {}
        if mem.get("percent") is not None and mem["percent"] >= _th(conf, "memory"):
            out.append(Condition("memory", "memory", "warn", "メモリが足りなくなっています",
                                 f"使用率 {mem['percent']:.0f}%"))

    if _rule_on(conf, "nextcloud") and nc:
        web = _item(nc, "Web 応答")
        if web and web["level"] == "error":
            out.append(Condition("nextcloud", "nextcloud", "error", "Nextcloud が応答しません", web["value"]))

    if _rule_on(conf, "service"):
        watch = set(conf.get("watch_services") or [])
        for svc in pi.get("services") or []:
            if svc["id"] in watch and svc["state"] not in ("running", "absent"):
                out.append(Condition("service", f"service:{svc['id']}", "error", f"{svc['name']} が止まっています",
                                     f"状態: {svc['state']}（{svc.get('detail') or '-'}）"))
        for c in nc.get("containers") or []:
            if c.get("level") == "error":
                out.append(Condition("service", f"container:{c['name']}", "error", f"コンテナ {c['name']} に異常があります",
                                     f"状態: {c.get('status') or c.get('state')}"))

    if _rule_on(conf, "tailscale") and ts and ts.get("running") is False:
        out.append(Condition("tailscale", "tailscale", "warn", "Pi の Tailscale が切れています",
                             "Tailscale 経由のアクセスができません。このサイトの Tailscale のページからオンにできます"))

    if _rule_on(conf, "internet"):
        it = _item(net, "インターネット")
        if it and it["level"] == "error":
            out.append(Condition("internet", "internet", "error", "インターネットにつながりません",
                                 f"Pi から {INTERNET_HOST} に届きません。ルーターや回線を確認してください"))

    if _rule_on(conf, "voltage"):
        v = _item(pi, "電圧低下")
        if v and v["level"] == "error":
            out.append(Condition("voltage", "voltage", "error", "電圧低下が起きています",
                                 f"{v['value']}。電源アダプターやケーブルを確認してください"))
    return out


class Engine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: list = []
        self.pending: dict[str, dict] = {}   # 起きているが、まだ通知するほど続いていない
        self.active: dict[str, dict] = {}    # 通知済みで続いている
        self.recent: list[dict] = []         # 手元の履歴（新しい順）
        self._load()

    # ---- 状態の保存（再起動しても同じ異常を何度も通知しないため）

    def _load(self) -> None:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.active = data.get("active") or {}
            self.recent = data.get("recent") or []
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"active": self.active, "recent": self.recent[:HISTORY_LOCAL]},
                                  ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(STATE_FILE)

    # ---- 外から使う

    def register(self, provider) -> None:
        """provider(conf) -> list[Condition] を追加する。"""
        self._providers.append(provider)

    def event(self, rule: str, title: str, message: str, level: str = "warn") -> None:
        """一度きりの出来事を記録して通知する。"""
        if not _rule_on(settings.get(), rule):
            return
        now = int(time.time())
        alert = {"id": secrets.token_hex(8), "rule": rule, "key": f"event:{rule}", "level": level,
                 "title": title, "message": message, "started_at": now, "ended_at": now}
        with self._lock:
            self.recent.insert(0, alert)
            self._save()
        self._record(alert)
        notify.send(title, message, level)

    def snapshot(self) -> dict:
        with self._lock:
            return {"active": sorted(self.active.values(), key=lambda a: -a["started_at"]),
                    "recent": list(self.recent[:50])}

    def evaluate(self, data: dict) -> None:
        conf = settings.get()
        conds = builtin_conditions(data, conf)
        for provider in self._providers:
            try:
                conds.extend(provider(conf))
            except Exception as ex:  # noqa: BLE001 - 1 つの失敗で全体を止めない
                print(f"alerts: {getattr(provider, '__name__', provider)}: {ex!r}", flush=True)
        self._apply(conds, conf)

    # ---- 内部

    def _record(self, alert: dict) -> None:
        cloud.write(
            "INSERT OR REPLACE INTO alerts (id, host, rule, level, title, message, started_at, ended_at, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            [alert["id"], cloud.HOST, alert["rule"], alert["level"], alert["title"], alert["message"],
             alert["started_at"], alert.get("ended_at")],
        )

    def _apply(self, conds: list[Condition], conf: dict) -> None:
        now = time.time()
        seen = {c.key: c for c in conds}
        fired, cleared = [], []
        with self._lock:
            for key, c in seen.items():
                hold = settings.RULES.get(c.rule, (None, None, None, None, 0))[4]
                if key in self.active:
                    a = self.active[key]
                    a["last_seen"] = now
                    a["message"] = c.message
                    if c.level == "error" and a["level"] != "error":
                        a["level"] = "error"  # 悪化したら記録だけ更新する
                    continue
                p = self.pending.setdefault(key, {"since": now})
                p["last_seen"] = now
                if now - p["since"] >= hold:
                    self.pending.pop(key, None)
                    alert = {"id": secrets.token_hex(8), "rule": c.rule, "key": key, "level": c.level,
                             "title": c.title, "message": c.message, "started_at": int(p["since"]),
                             "last_seen": now, "ended_at": None}
                    self.active[key] = alert
                    fired.append(alert)
            for key in list(self.pending):
                if key not in seen:
                    self.pending.pop(key)
            for key, a in list(self.active.items()):
                rule_off = not _rule_on(conf, a["rule"])
                if key not in seen and (rule_off or now - a.get("last_seen", 0) >= CLEAR_AFTER):
                    self.active.pop(key)
                    a["ended_at"] = int(now)
                    a["silent"] = rule_off
                    cleared.append(a)
            if fired or cleared:
                for a in fired:
                    self.recent.insert(0, {k: v for k, v in a.items() if k != "last_seen"})
                for a in cleared:
                    for r in self.recent:
                        if r["id"] == a["id"]:
                            r["ended_at"] = a["ended_at"]
                            break
                self.recent = self.recent[:HISTORY_LOCAL]
                self._save()
        for a in fired:
            self._record(a)
            notify.send(a["title"], a["message"], a["level"])
            print(f"alert: {a['title']} - {a['message']}", flush=True)
        for a in cleared:
            self._record(a)
            print(f"alert cleared: {a['title']}", flush=True)
            if conf["notify"].get("recovery") and not a.get("silent"):
                took = a["ended_at"] - a["started_at"]
                notify.send("復旧しました", f"{a['title']} → 元に戻りました（約 {max(1, round(took / 60))} 分間）", "ok")


ENGINE = Engine()


def history(limit: int = 100) -> tuple[list[dict], str]:
    """アラートの履歴（新しい順）。D1 から読み、届かなければ手元の控え。"""
    try:
        rows = cloud.query(
            "SELECT id, rule, level, title, message, started_at, ended_at FROM alerts "
            "ORDER BY started_at DESC LIMIT ?", [limit])
        return rows, "d1"
    except cloud.CloudError:
        return ENGINE.snapshot()["recent"][:limit], "local"
