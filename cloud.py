"""Cloudflare D1 に記録を保存する。

Pi の SD カードが壊れても記録が残るよう、正本は D1 に置く。Pi 側のファイルは控え。

- 書き込みは送信待ち（data/outbox.jsonl）に積んでから、別スレッドでまとめて送る。
  インターネットや D1 が止まっていても記録は失われず、つながった時点で送られる。
- 同じ文を 2 回送っても結果が変わらないよう、書き込みはすべて
  INSERT OR REPLACE / ON CONFLICT で書く（送信結果が届かず再送しても重複しない）。
- 設定・パスキーなどの JSON は kv テーブルにも保存し、Pi を作り直したときに
  自動で書き戻す（restore_later）。

設定（.env）:
  CF_ACCOUNT_ID       Cloudflare のアカウント ID
  D1_DATABASE_ID      D1 データベースの ID
  CF_API_TOKEN_FILE   API トークンのファイル（既定 ~/.cloudflared/api-token。D1 編集の権限が必要）
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
OUTBOX = Path(os.environ.get("CLOUD_OUTBOX", str(DATA_DIR / "outbox.jsonl")))
API = "https://api.cloudflare.com/client/v4"
HOST = socket.gethostname()

BATCH_SIZE = 50          # 1 回の送信にまとめる文の数
FLUSH_INTERVAL = 10.0    # 送信待ちを確認する間隔（秒）
MAX_BACKOFF = 300.0      # 送れないときの再試行の最長間隔（秒）
MAX_PENDING = 50_000     # 送信待ちの上限（超えたら古い計測値から捨てる）

SCHEMA = [
    # 1 分ごとの計測値（30 日保持）
    """CREATE TABLE IF NOT EXISTS metrics_1m (
        host TEXT NOT NULL, ts INTEGER NOT NULL,
        cpu REAL, mem_used INTEGER, mem_total INTEGER, temp REAL, load1 REAL,
        net_rx REAL, net_tx REAL, router_ms REAL, internet_ms REAL, nextcloud_ms REAL,
        disk TEXT,
        PRIMARY KEY (host, ts))""",
    # 1 時間ごとの集計（ずっと保持）
    """CREATE TABLE IF NOT EXISTS metrics_1h (
        host TEXT NOT NULL, ts INTEGER NOT NULL,
        cpu REAL, cpu_max REAL, mem_used INTEGER, temp REAL, temp_max REAL,
        net_rx REAL, net_tx REAL, internet_ms REAL, nextcloud_ms REAL,
        PRIMARY KEY (host, ts))""",
    # 生存確認（Cloudflare 側の見張りが読む）
    """CREATE TABLE IF NOT EXISTS heartbeat (
        host TEXT PRIMARY KEY, ts INTEGER NOT NULL, boot_id TEXT, uptime REAL, info TEXT)""",
    # 操作の記録
    """CREATE TABLE IF NOT EXISTS audit (
        id TEXT PRIMARY KEY, ts INTEGER NOT NULL, host TEXT, who TEXT, credential TEXT,
        action TEXT, params TEXT, ok INTEGER, message TEXT, ip TEXT, source TEXT)""",
    "CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts)",
    # アラート（発生と復旧）
    """CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY, host TEXT, rule TEXT NOT NULL, level TEXT, title TEXT, message TEXT,
        started_at INTEGER NOT NULL, ended_at INTEGER, notified INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS alerts_started ON alerts (started_at)",
    # 起動と停止の履歴
    """CREATE TABLE IF NOT EXISTS boots (
        boot_id TEXT PRIMARY KEY, host TEXT, booted_at INTEGER NOT NULL, last_seen INTEGER,
        reason TEXT, detail TEXT)""",
    # LAN で見た端末
    """CREATE TABLE IF NOT EXISTS lan_devices (
        mac TEXT PRIMARY KEY, ip TEXT, name TEXT, label TEXT, known INTEGER DEFAULT 0,
        first_seen INTEGER, last_seen INTEGER, random_mac INTEGER DEFAULT 0)""",
    # 設定・パスキーなどの JSON（Pi を作り直したときに書き戻す）
    """CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)""",
]


class CloudError(Exception):
    pass


def _env(name: str) -> str:
    """環境変数。tools/ から直接実行したとき用に、無ければ .env からも読む。"""
    value = os.environ.get(name)
    if value is None:
        try:
            for line in (Path(__file__).resolve().parent / ".env").read_text(encoding="utf-8").splitlines():
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == name and not line.lstrip().startswith("#"):
                    value = val.strip().strip('"').strip("'")
        except OSError:
            pass
    return (value or "").strip()


def _config() -> tuple[str, str, str] | None:
    account = _env("CF_ACCOUNT_ID")
    database = _env("D1_DATABASE_ID")
    token_file = Path(_env("CF_API_TOKEN_FILE") or str(Path.home() / ".cloudflared/api-token"))
    if not account or not database:
        return None
    try:
        token = token_file.read_text().strip()
    except OSError:
        return None
    return (account, database, token) if token else None


def configured() -> bool:
    return _config() is not None


def _request(body: dict, timeout: float = 20) -> list[dict]:
    cfg = _config()
    if cfg is None:
        raise CloudError("D1 が未設定です（.env の CF_ACCOUNT_ID / D1_DATABASE_ID とトークン）")
    account, database, token = cfg
    req = urllib.request.Request(
        f"{API}/accounts/{account}/d1/database/{database}/query",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as ex:
        try:
            errors = json.load(ex).get("errors") or []
        except Exception:  # noqa: BLE001
            errors = []
        msg = "; ".join(str(e.get("message")) for e in errors) or f"HTTP {ex.code}"
        err = CloudError(f"D1: {msg}")
        err.status = ex.code  # type: ignore[attr-defined]
        raise err from None
    except (OSError, ValueError) as ex:
        raise CloudError(f"D1 に接続できません: {ex.__class__.__name__}") from None
    if not data.get("success"):
        raise CloudError(f"D1: {data.get('errors')}")
    return data.get("result") or []


def query(sql: str, params: list | tuple = (), timeout: float = 15) -> list[dict]:
    """1 文を実行して行の一覧を返す（読み取り用。失敗すると CloudError）。"""
    result = _request({"sql": sql, "params": list(params)}, timeout=timeout)
    return (result[0].get("results") if result else []) or []


# ---------------------------------------------------------------- 送信待ち

class Outbox:
    """送信待ちの書き込み。正本は OUTBOX ファイルで、プロセスをまたいで共有する。

    サーバー本体と tools/ のスクリプトが同時に書いても消えないよう、読み書きは
    すべてファイルロック（flock）の中で行い、メモリには件数しか持たない。
    """

    def __init__(self) -> None:
        self._wake = threading.Event()
        self._schema_ready = False
        self._pending = 0
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.sent = 0

    @contextlib.contextmanager
    def _locked(self):
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTBOX.with_suffix(".lock"), "a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _read() -> list[dict]:
        try:
            lines = OUTBOX.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        items = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # 書きかけで電源が落ちた行は捨てる
            if isinstance(item, dict) and "sql" in item:
                items.append(item)
        return items

    @staticmethod
    def _rewrite(items: list[dict]) -> None:
        if not items:
            OUTBOX.unlink(missing_ok=True)
            return
        tmp = OUTBOX.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(OUTBOX)

    def add(self, sql: str, params: list | tuple = (), kind: str = "") -> None:
        item = {"sql": sql, "params": list(params)}
        if kind:
            item["kind"] = kind
        line = json.dumps(item, ensure_ascii=False) + "\n"
        with self._locked():
            with open(OUTBOX, "a", encoding="utf-8") as f:
                f.write(line)
            os.chmod(OUTBOX, 0o600)
            self._pending += 1
            if self._pending > MAX_PENDING:
                items = self._read()
                if len(items) > MAX_PENDING:
                    # 古い計測値から捨てる（操作の記録や設定は残す）
                    drop = len(items) - MAX_PENDING
                    kept = []
                    for it in items:
                        if drop and it.get("kind") == "metric":
                            drop -= 1
                            continue
                        kept.append(it)
                    items = kept[-MAX_PENDING:]
                    self._rewrite(items)
                self._pending = len(items)

    def pending(self) -> int:
        return self._pending

    def flush_now(self) -> None:
        self._wake.set()

    def start(self) -> None:
        with self._locked():
            self._pending = len(self._read())
        threading.Thread(target=self._loop, daemon=True, name="d1-outbox").start()

    def _ensure_schema(self) -> None:
        if not self._schema_ready:
            _request({"batch": [{"sql": s} for s in SCHEMA]})
            self._schema_ready = True

    @staticmethod
    def _retryable(ex: CloudError) -> bool:
        status = getattr(ex, "status", None)
        return status is None or status in (401, 403, 429) or status >= 500

    def _send(self, batch: list[dict]) -> None:
        try:
            _request({"batch": [{"sql": i["sql"], "params": i["params"]} for i in batch]})
        except CloudError as ex:
            if self._retryable(ex):
                raise  # 接続・認証・混雑は後で丸ごと再送
            # 文の誤りなど: 1 文ずつ送り、受け付けられない文だけ捨てる（他を巻き込まない）
            for item in batch:
                try:
                    _request({"sql": item["sql"], "params": item["params"]})
                except CloudError as one:
                    if self._retryable(one):
                        raise
                    print(f"cloud: 送れない記録を捨てました: {one} :: {item['sql'][:80]}", flush=True)

    def flush(self) -> None:
        """送信待ちをすべて送る（失敗すると CloudError。送れた分は消える）。"""
        self._ensure_schema()
        while True:
            with self._locked():
                batch = self._read()[:BATCH_SIZE]
            if not batch:
                self._pending = 0
                return
            self._send(batch)
            with self._locked():
                items = self._read()
                # 送ったものが先頭に残っていれば消す（他のプロセスが追記した分は残す）
                if items[:len(batch)] == batch:
                    items = items[len(batch):]
                self._rewrite(items)
                self._pending = len(items)
            self.sent += len(batch)

    def _loop(self) -> None:
        backoff = FLUSH_INTERVAL
        while True:
            self._wake.wait(timeout=backoff)
            self._wake.clear()
            if not configured():
                backoff = 60.0
                continue
            try:
                self.flush()
                self.last_ok, self.last_error = time.time(), None
                backoff = FLUSH_INTERVAL
            except CloudError as ex:
                if self.last_error != str(ex):
                    print(f"cloud: {ex}（{self.pending()} 件を送信待ちで保持）", flush=True)
                self.last_error = str(ex)
                backoff = min(MAX_BACKOFF, max(FLUSH_INTERVAL, backoff * 2))
            except Exception as ex:  # noqa: BLE001 - 送信スレッドは止めない
                print(f"cloud: {ex!r}", flush=True)
                backoff = MAX_BACKOFF


OUTBOX_Q = Outbox()


def write(sql: str, params: list | tuple = (), kind: str = "") -> None:
    """D1 への書き込みを送信待ちに積む（すぐ戻る）。"""
    OUTBOX_Q.add(sql, params, kind)


def flush_quietly() -> bool:
    """tools/ のスクリプト向け: 終了前に送信待ちを送ってみる。送れなければ次にサーバーが送る。"""
    if not configured():
        return False
    try:
        OUTBOX_Q.flush()
        return True
    except CloudError:
        return False


def status() -> dict:
    return {
        "configured": configured(),
        "pending": OUTBOX_Q.pending(),
        "last_ok": OUTBOX_Q.last_ok,
        "last_error": OUTBOX_Q.last_error,
        "sent": OUTBOX_Q.sent,
    }


# ---------------------------------------------------------------- JSON の控え（kv）

def kv_put(key: str, value: object) -> None:
    write(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at "
        "WHERE excluded.updated_at >= kv.updated_at",
        [key, json.dumps(value, ensure_ascii=False), int(time.time() * 1000)],
        kind="kv",
    )


def kv_get(key: str) -> object | None:
    rows = query("SELECT value FROM kv WHERE key = ?", [key])
    return json.loads(rows[0]["value"]) if rows else None


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def restore_later(key: str, path: Path, is_empty, on_restored=None, on_absent=None) -> None:
    """起動時に手元のファイルと D1 の控えを合わせる。

    - 手元が無い（空）で D1 にある → D1 から書き戻す（Pi を作り直したとき）
    - D1 に無い → 手元の内容を D1 に送る（on_absent があればそれを呼ぶ）
    - 両方ある → 何もしない（手元で変えたものは保存のたびに D1 へ送っている）
    D1 に届かなければ 10 分ほど裏で再試行する。
    """
    def attempt() -> bool | None:
        try:
            local = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            local = None
        has_local = local is not None and not is_empty(local)
        if not configured():
            return False
        try:
            value = kv_get(key)
        except CloudError:
            return None  # 届かない: 後で再試行
        if value is None or is_empty(value):
            if on_absent:
                on_absent()
            elif has_local:
                kv_put(key, local)
            return False
        if has_local:
            return False
        _write_json(path, value)
        print(f"cloud: {path.name} を D1 の控えから復元しました", flush=True)
        if on_restored:
            on_restored()
        return True

    if attempt() is None:
        def retry() -> None:
            for _ in range(20):
                time.sleep(30)
                if attempt() is not None:
                    return
        threading.Thread(target=retry, daemon=True).start()


def start() -> None:
    OUTBOX_Q.start()
    OUTBOX_Q.flush_now()
