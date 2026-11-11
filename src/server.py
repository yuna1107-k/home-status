#!/usr/bin/env python3
"""自宅サーバーのステータスページ。

入口は https://<PUBLIC_HOST>/（.env の PASSKEY_RP_ID）に 1 本化する:

  app     127.0.0.1:8096   Cloudflare Tunnel 経由。Cloudflare Access → パスキーの順に認証
                           Pi の Tailscale がオン: ステータスボード（閲覧・変更）
                           Pi の Tailscale がオフ: Tailscale のオン/オフだけ
  public  0.0.0.0:8090     LAN 向け。公開 URL へ転送するだけ
  admin   127.0.0.1:8092   Tailscale Serve（:8443）向け。公開 URL へ転送するだけ
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import actions
import alerts
import audit
import auth
import boots
import cloud
import collectors
import health
import history
import lan
import notify
import passkey
import prefs
import settings
from sampler import SAMPLER

STATIC_DIR = Path(__file__).resolve().parent / "static"
PUBLIC_URL = f"https://{passkey.RP_ID}"
ASSETS = {
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/features.js": ("features.js", "text/javascript; charset=utf-8"),
    "/wall.js": ("wall.js", "text/javascript; charset=utf-8"),
    "/favicon.ico": ("favicon-32.png", "image/png"),
    "/favicon-32.png": ("favicon-32.png", "image/png"),
    "/favicon-192.png": ("favicon-192.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}
PUSH_INTERVAL = 2.0
IDLE_REFRESH = 10.0  # 誰も見ていないときの更新間隔
MAX_BODY = 64 * 1024


class Hub:
    """全項目を定期的に集め、接続中のブラウザへ配る。"""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.version = 0
        self.payload: bytes | None = None
        self.updated_at = 0.0
        self.clients = 0
        self.wake = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _collect(self) -> None:
        data = collectors.collect_all()
        try:
            alerts.ENGINE.evaluate(data)
        except Exception as ex:  # noqa: BLE001 - 判定の失敗で配信を止めない
            print(f"alerts: {ex!r}", flush=True)
        data["alerts"] = alerts.ENGINE.snapshot()["active"]
        data["extra"] = extra_status()
        body = json.dumps(data, ensure_ascii=False).encode()
        with self.cond:
            self.payload = body
            self.version += 1
            self.updated_at = time.monotonic()
            self.cond.notify_all()

    def _loop(self) -> None:
        while True:
            woke = self.wake.wait(timeout=PUSH_INTERVAL)
            self.wake.clear()
            idle = self.clients == 0 and time.monotonic() - self.updated_at < IDLE_REFRESH
            if idle and not woke:
                continue
            try:
                self._collect()
            except Exception as ex:  # noqa: BLE001 - 配信ループは止めない
                print(f"hub: {ex!r}", flush=True)

    def refresh_now(self) -> None:
        collectors.clear_caches()
        self.wake.set()

    def latest(self) -> bytes:
        if self.payload is None or time.monotonic() - self.updated_at > IDLE_REFRESH:
            self._collect()
        return self.payload


HUB = Hub()


def extra_status() -> dict:
    """新しいページ（ホーム・システム情報など）で使う、変化のゆっくりな情報。"""
    devices = lan.devices()
    return {
        "health": health.snapshot(),
        "cloud": cloud.status(),
        "notify": notify.status(),
        "lan": {mac: {k: d.get(k) for k in ("known", "label", "first_seen")} for mac, d in devices.items()},
    }

_ts_cache: tuple[float, bool] = (0.0, False)
_ts_lock = threading.Lock()


def tailscale_running() -> bool:
    """Pi の Tailscale が起動していて、ネットワークにもつながっているか（2 秒キャッシュ）。"""
    global _ts_cache
    with _ts_lock:
        if time.monotonic() - _ts_cache[0] > 2:
            _ts_cache = (time.monotonic(), actions.tailscale_state().get("connected", False))
        return _ts_cache[1]


def forget_tailscale_state() -> None:
    global _ts_cache
    with _ts_lock:
        _ts_cache = (0.0, False)


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "HomeStatus/3.0"
    protocol_version = "HTTP/1.1"
    role = "base"

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "publickey-credentials-get=(self), publickey-credentials-create=(self)")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: object, extra: dict | None = None) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _file(self, name: str, ctype: str) -> None:
        self._send(200, (STATIC_DIR / name).read_bytes(), ctype)

    def _redirect(self, location: str) -> None:
        self._send(302, b"", "text/plain; charset=utf-8", {"Location": location})

    def log_message(self, fmt: str, *args) -> None:
        pass  # アクセスログは出さない（操作とログインだけ個別に記録する）


class RedirectHandler(BaseHandler):
    """LAN・Tailscale の旧 URL は公開 URL に転送する。"""

    role = "redirect"

    def do_GET(self) -> None:  # noqa: N802
        self._redirect(PUBLIC_URL + "/")

    do_HEAD = do_GET
    do_POST = do_GET


class AppHandler(BaseHandler):
    """公開 URL の本体。Cloudflare Access の後ろで、さらにパスキーを求める。"""

    role = "app"

    # ---- 認証

    def _cf(self) -> tuple[bool, str]:
        return auth.check_cloudflare(self.headers)

    def _session(self, email: str) -> dict | None:
        return passkey.session(self.headers.get("Cookie", ""), email)

    def _ip(self) -> str:
        return self.headers.get("CF-Connecting-IP", "") or self.client_address[0]

    def _audit(self, action: str, email: str, sess: dict | None, **kw) -> None:
        audit.record(action, who=email, credential=(sess or {}).get("credential", ""), ip=self._ip(), **kw)

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlsplit
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _read_json(self) -> dict | None:
        """CSRF 対策として JSON + 独自ヘッダー付きのリクエストだけ受け付ける。"""
        if self.headers.get("X-Requested-With") != "home-status":
            self._json(403, {"error": "不正なリクエストです"})
            return None
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(415, {"error": "JSON で送信してください"})
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._json(413, {"error": "リクエストが大きすぎます"})
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "JSON が不正です"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "JSON が不正です"})
            return None
        return data

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True
        with HUB.cond:
            HUB.clients += 1
            last, payload = HUB.version, HUB.payload
        HUB.wake.set()
        try:
            if payload is not None:
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
            while True:
                with HUB.cond:
                    HUB.cond.wait_for(lambda: HUB.version != last, timeout=15)
                    version, payload = HUB.version, HUB.payload
                if not tailscale_running():
                    # Tailscale がオフになったらボードの配信をやめる（画面側はオン/オフ画面に切り替わる）
                    self.wfile.write(b"event: tailscale-off\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                if version != last and payload is not None:
                    last = version
                    self.wfile.write(b"data: " + payload + b"\n\n")
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            with HUB.cond:
                HUB.clients -= 1

    # ---- GET

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ASSETS:
            self._file(*ASSETS[path])
            return

        ok, who = self._cf()
        if not ok:
            if path.startswith("/api/"):
                self._json(403, {"error": who})
            else:
                self._send(403, f"アクセスできません: {who}".encode(), "text/plain; charset=utf-8")
            return
        email = who
        sess = self._session(email)

        if path == "/auth/sso/login":
            redirect_uri = urllib.parse.quote("https://status.yuna1107.com/auth/sso/callback", safe="")
            self._redirect(f"https://auth.yuna1107.com/oauth/authorize?client_id=status&redirect_uri={redirect_uri}&response_type=code")
            return
        if path == "/auth/sso/callback":
            q = self._query()
            code = q.get("code")
            if not code:
                self._redirect("/login?error=missing_code")
                return
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            now_ms = int(time.time() * 1000)
            try:
                rows = cloud.query_auth_db(
                    """SELECT c.user_id, u.username, u.display_name, u.status as user_status, p.is_enabled
                       FROM oauth_codes c
                       JOIN users u ON c.user_id = u.id
                       LEFT JOIN user_services p ON u.id = p.user_id AND p.service_id = 'status'
                       WHERE c.code_hash = ? AND c.client_id = 'status' AND c.expires_at > ?""",
                    [code_hash, now_ms],
                )
                if rows:
                    r = rows[0]
                    if r.get("user_status") == "active" and (r.get("username") == "yuna" or r.get("is_enabled") == 1):
                        cloud.query_auth_db("DELETE FROM oauth_codes WHERE code_hash = ?", [code_hash])
                        sid = secrets.token_urlsafe(32)
                        with passkey._lock:
                            passkey._sessions[sid] = {
                                "email": email or r.get("username"),
                                "credential": f"SSO ({r.get('username')})",
                                "expires": time.time() + passkey.SESSION_SECONDS,
                            }
                        self._audit("sso.login", email, {"credential": "SSO"}, message=f"SSOでログインしました ({r.get('username')})")
                        self._send(302, b"", "text/plain", {"Location": "/", "Set-Cookie": passkey.cookie_for(sid)})
                        return
            except Exception as ex:
                print(f"[sso] callback error: {ex}", flush=True)
            self._redirect("/login?error=sso_unauthorized")
            return

        if path == "/login":
            if sess:
                self._redirect("/")
            else:
                self._file("login.html", "text/html; charset=utf-8")
            return
        if path == "/api/passkey/state":
            self._json(200, {"email": email, "registered": passkey.has_credentials(), "logged_in": bool(sess)})
            return
        if not sess:
            if path.startswith("/api/"):
                self._json(401, {"error": "パスキーでログインしてください", "login": "/login"})
            else:
                self._redirect("/login")
            return

        running = tailscale_running()
        if path in ("/", "/index.html"):
            self._file("index.html" if running else "tunnel.html", "text/html; charset=utf-8")
        elif path == "/api/whoami":
            self._json(200, {"can_edit": running, "who": email, "credential": sess["credential"],
                             "role": self.role, "tailscale_running": running,
                             "reason": "" if running else "Pi の Tailscale がオフのため、ボードは使えません"})
        elif path == "/api/tailscale":
            self._json(200, {"authorized": True, "who": email, **actions.tailscale_state()})
        elif path == "/api/passkeys":
            self._json(200, {"passkeys": passkey.credentials()})
        elif path == "/api/prefs":
            self._json(200, {"prefs": prefs.get(email)})
        elif path == "/api/settings":
            self._json(200, site_settings())
        elif path == "/api/alerts":
            rows, source = alerts.history(int(self._query().get("limit", 100) or 100))
            self._json(200, {"history": rows, "source": source, "active": alerts.ENGINE.snapshot()["active"]})
        elif path == "/api/audit":
            q = self._query()
            try:
                before = int(q.get("before") or 0) or None
                limit = int(q.get("limit") or 100)
            except ValueError:
                self._json(400, {"error": "指定が不正です"})
                return
            rows, source = audit.recent(limit, before, q.get("q", "")[:100])
            self._json(200, {"rows": rows, "source": source})
        elif path == "/api/history":
            try:
                self._json(200, history.query(self._query().get("range", "24h")))
            except ValueError as ex:
                self._json(400, {"error": str(ex)})
        elif path == "/api/boots":
            rows, source = boots.history()
            self._json(200, {"rows": rows, "source": source})
        elif not running and path in ("/api/status", "/api/stream"):
            self._json(409, {"error": "Pi の Tailscale がオフです", "tailscale_running": False})
        elif path == "/api/status":
            self._send(200, HUB.latest(), "application/json; charset=utf-8")
        elif path == "/api/stream":
            self._stream()
        else:
            self._json(404, {"error": "not found"})

    # ---- POST

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        ok, who = self._cf()
        if not ok:
            self._json(403, {"error": who})
            return
        email = who
        data = self._read_json()
        if data is None:
            return
        sess = self._session(email)

        try:
            # ---- パスキー（ログイン前でも使うもの）
            if path == "/api/passkey/login/options":
                common = data.get("common", True) if isinstance(data, dict) else True
                if not common and not passkey.has_credentials():
                    self._json(409, {"error": "パスキーがまだ登録されていません"})
                    return
                self._json(200, passkey.login_options(email, common=common))
                return
            if path == "/api/passkey/login":
                try:
                    sid = passkey.login(email, data)
                except passkey.PasskeyError as ex:
                    self._audit("passkey.login", email, None, ok=False, message=str(ex))
                    raise
                print(f"passkey login: {email}", flush=True)
                new = self._session_for(sid, email)
                self._audit("passkey.login", email, new, message="ログインしました")
                self._json(200, {"ok": True}, {"Set-Cookie": passkey.cookie_for(sid)})
                return
            if path in ("/api/passkey/register/options", "/api/passkey/register"):
                # 最初の 1 つだけは Cloudflare Access を通過した本人が登録できる。2 つ目以降はログイン中のみ
                if passkey.has_credentials() and not sess:
                    self._json(403, {"error": "パスキーを追加するには、登録済みのパスキーでログインしてください"})
                    return
                if path.endswith("/options"):
                    self._json(200, passkey.registration_options(email))
                else:
                    cred = passkey.register(email, data)
                    print(f"passkey registered: {email} ({cred['name']})", flush=True)
                    self._audit("passkey.register", email, sess, params={"name": cred["name"]},
                                message=f"パスキー「{cred['name']}」を登録しました")
                    self._json(200, cred)
                return
            if path == "/api/passkey/logout":
                if sess:
                    self._audit("passkey.logout", email, sess, message="ログアウトしました")
                passkey.logout(self.headers.get("Cookie", ""))
                self._json(200, {"ok": True}, {"Set-Cookie": passkey.clear_cookie()})
                return

            # ---- ここから先はログイン必須
            if not sess:
                self._json(401, {"error": "パスキーでログインしてください", "login": "/login"})
                return
            if path == "/api/passkey/remove":
                cred_id = str(data.get("id", ""))
                if not passkey.remove_credential(cred_id):
                    self._json(404, {"error": "そのパスキーは見つかりません"})
                    return
                print(f"passkey removed by {email}: {cred_id[:12]}…", flush=True)
                self._audit("passkey.remove", email, sess, params={"id": cred_id[:12]}, message="パスキーを削除しました")
                self._json(200, {"message": "パスキーを削除しました"})
                return
            if path == "/api/tailscale":
                self._tailscale(email, sess, data)
                return
            if path == "/api/settings":
                try:
                    settings.update(data.get("changes"))
                except ValueError as ex:
                    self._json(400, {"error": str(ex)})
                    return
                self._audit("settings.update", email, sess, params=data.get("changes"), message="設定を変更しました")
                HUB.refresh_now()
                self._json(200, {"message": "保存しました", **site_settings()})
                return
            if path == "/api/notify/test":
                try:
                    notify.post("テスト通知", "Home Status からのテスト通知です。届いていれば設定は正しく動いています。", "info", "alerts")
                except notify.NotifyError as ex:
                    self._json(502, {"error": str(ex)})
                    return
                self._json(200, {"message": "テスト通知を送りました"})
                return
            if path == "/api/prefs":
                try:
                    self._json(200, {"prefs": prefs.save(email, data.get("prefs"))})
                except ValueError as ex:
                    self._json(400, {"error": str(ex)})
                return
            if path == "/api/action":
                self._action(email, sess, data)
                return
            self._json(404, {"error": "not found"})
        except passkey.PasskeyError as ex:
            print(f"passkey error ({email}): {ex}", flush=True)
            self._json(400, {"error": str(ex)})

    def _session_for(self, sid: str, email: str) -> dict | None:
        return passkey.session(f"{passkey.COOKIE}={sid}", email)

    def _tailscale(self, email: str, sess: dict | None, data: dict) -> None:
        print(f"tailscale by {email}: running={data.get('running')}", flush=True)
        params = {"running": data.get("running")}
        try:
            message = actions.tailscale_set_running(params)
        except actions.ActionError as ex:
            print(f"  -> error: {ex}", flush=True)
            self._audit("tailscale.set_running", email, sess, params=params, ok=False, message=str(ex))
            self._json(400, {"error": str(ex)})
            return
        print(f"  -> {message}", flush=True)
        self._audit("tailscale.set_running", email, sess, params=params, message=message)
        forget_tailscale_state()
        HUB.refresh_now()
        self._json(200, {"message": message, **actions.tailscale_state()})

    def _action(self, email: str, sess: dict | None, data: dict) -> None:
        if not tailscale_running():
            self._json(409, {"error": "Pi の Tailscale がオフのため、ボードの操作はできません"})
            return
        name = str(data.get("action", ""))
        params = data.get("params") or {}
        print(f"action by {email}: {name} {json.dumps(params, ensure_ascii=False)}", flush=True)
        log = name not in audit.READ_ONLY
        try:
            if name == "tailscale.set_running":
                result = {"message": actions.tailscale_set_running(params if isinstance(params, dict) else {})}
                forget_tailscale_state()
            else:
                result = actions.run_action(name, params)
        except actions.ActionError as ex:
            print(f"  -> error: {ex}", flush=True)
            if log:
                self._audit(name, email, sess, params=params, ok=False, message=str(ex))
            self._json(400, {"error": str(ex)})
            return
        except Exception as ex:  # noqa: BLE001
            print(f"  -> failed: {ex!r}", flush=True)
            if log:
                self._audit(name, email, sess, params=params, ok=False, message=f"{ex!r}")
            self._json(500, {"error": f"失敗しました: {ex}"})
            return
        print(f"  -> {result['message']}", flush=True)
        if log:
            self._audit(name, email, sess, params=params, message=result["message"])
        if name != "logs.view":
            HUB.refresh_now()
        self._json(200, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def site_settings() -> dict:
    conf = settings.get()
    try:
        watchdog = cloud.kv_get("watchdog") if cloud.configured() else None
    except cloud.CloudError:
        watchdog = None
    # トークンは画面に返さない（設定済みかどうかだけ伝える）
    if conf["notify"].get("token"):
        conf["notify"]["token"] = settings.MASK
    return {
        "watchdog": watchdog,
        "settings": conf,
        "rules": [{"id": rid, "label": r[0], "unit": r[1], "hold": r[4]} for rid, r in settings.RULES.items()],
        "subscribe_url": f"{conf['notify']['server']}/{conf['notify']['topic']}",
    }


def _serve(host: str, port: int, handler) -> Server:
    httpd = Server((host, port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"{handler.role:8s} http://{host}:{port}/", flush=True)
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--public", default="0.0.0.0:8090", help="LAN 用の転送。空で無効")
    parser.add_argument("--admin", default="127.0.0.1:8092", help="Tailscale Serve 用の転送。空で無効")
    parser.add_argument("--tunnel", default="127.0.0.1:8096", help="本体（Cloudflare Tunnel 経由）")
    args = parser.parse_args()

    # 記録の送信を先に始め、手元に無い設定・パスキーを D1 の控えから戻す
    cloud.start()
    settings.restore()
    passkey.restore()
    prefs.restore()
    notify.NOTIFIER.start()

    SAMPLER.start()
    HUB.start()
    history.start()
    health.start()
    boots.start()
    lan.start(collectors.neighbors)
    for spec, handler in ((args.tunnel, AppHandler), (args.public, RedirectHandler), (args.admin, RedirectHandler)):
        if spec:
            host, _, port = spec.rpartition(":")
            _serve(host, int(port), handler)
    print(f"公開 URL: {PUBLIC_URL}/（パスキーの RP ID: {passkey.RP_ID}）", flush=True)
    print(f"Cloudflare Access: {'設定済み' if auth.cloudflare_configured() else '未設定（すべて拒否）'}", flush=True)
    print(f"登録済みパスキー: {len(passkey.credentials())} 件", flush=True)
    print(f"D1: {'設定済み' if cloud.configured() else '未設定（記録は手元にためておく）'}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
