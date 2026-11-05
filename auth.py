"""操作を許可するかどうかの判定。

- メイン画面（127.0.0.1:8092、Tailscale Serve の HTTPS 経由でだけ届く）:
  Tailscale Serve が付ける `Tailscale-User-Login` ヘッダーが許可リストにあれば操作可。
  Serve はクライアントが送った同名ヘッダーを上書きするので偽装できない。
- Cloudflare トンネル画面（127.0.0.1:8096）:
  Cloudflare Access が付ける JWT（`Cf-Access-Jwt-Assertion`）の署名・aud・期限・メールを検証する。
  Access 側の設定ミスで認証なしに届いても、ここで弾く（設定が無ければ常に拒否）。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# ---------------------------------------------------------------- Tailscale


def _owner_login() -> str | None:
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout)
        uid = str((data.get("Self") or {}).get("UserID"))
        return (data.get("User") or {}).get(uid, {}).get("LoginName")
    except Exception:  # noqa: BLE001
        return None


_ts_allowed: set[str] | None = None


def tailscale_allowed_logins() -> set[str]:
    """操作を許可する Tailscale アカウント。未指定ならこのマシンの所有者。"""
    global _ts_allowed
    if _ts_allowed is None:
        env = os.environ.get("ALLOWED_TAILSCALE_LOGINS", "")
        logins = {x.strip() for x in env.split(",") if x.strip()}
        if not logins and (owner := _owner_login()):
            logins = {owner}
        if logins:
            _ts_allowed = logins
    return _ts_allowed or set()


def check_tailscale(headers) -> tuple[bool, str]:
    login = headers.get("Tailscale-User-Login", "")
    if not login:
        return False, "Tailscale の HTTPS 経由ではないため閲覧のみです"
    if login not in tailscale_allowed_logins():
        return False, f"{login} には操作権限がありません"
    return True, login


# ---------------------------------------------------------------- Cloudflare Access

CF_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
CF_ALLOWED_EMAILS = {
    x.strip().lower()
    for x in os.environ.get("CF_ACCESS_ALLOWED_EMAILS", "").split(",")
    if x.strip()
}
_CERTS_TTL = 3600

_certs_lock = threading.Lock()
_certs: tuple[float, dict] | None = None


def cloudflare_configured() -> bool:
    return bool(CF_TEAM_DOMAIN and CF_AUD and CF_ALLOWED_EMAILS)


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _fetch_keys(force: bool = False) -> dict:
    global _certs
    with _certs_lock:
        if force or _certs is None or time.monotonic() - _certs[0] > _CERTS_TTL:
            url = f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.load(resp)
            keys = {}
            for jwk in data.get("keys", []):
                if jwk.get("kty") != "RSA":
                    continue
                n = int.from_bytes(_b64url(jwk["n"]), "big")
                e = int.from_bytes(_b64url(jwk["e"]), "big")
                keys[jwk["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
            _certs = (time.monotonic(), keys)
        return _certs[1]


def _token_from(headers) -> str:
    token = headers.get("Cf-Access-Jwt-Assertion", "")
    if token:
        return token
    for part in headers.get("Cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "CF_Authorization":
            return value
    return ""


def check_cloudflare(headers) -> tuple[bool, str]:
    if not cloudflare_configured():
        return False, "Cloudflare Access の設定（チームドメイン・AUD・許可メール）が未設定です"
    token = _token_from(headers)
    if not token:
        return False, "Cloudflare Access の認証情報がありません"
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url(header_b64))
        payload = json.loads(_b64url(payload_b64))
        signature = _b64url(sig_b64)
    except ValueError:
        return False, "認証トークンの形式が不正です"
    if header.get("alg") != "RS256":
        return False, "認証トークンの署名方式が不正です"

    try:
        keys = _fetch_keys()
        key = keys.get(header.get("kid"))
        if key is None:
            key = _fetch_keys(force=True).get(header.get("kid"))
    except OSError as ex:
        return False, f"Cloudflare の公開鍵を取得できません: {ex}"
    if key is None:
        return False, "認証トークンの鍵が不明です"
    try:
        key.verify(signature, f"{header_b64}.{payload_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False, "認証トークンの署名が不正です"

    now = time.time()
    aud = payload.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if CF_AUD not in auds:
        return False, "認証トークンの対象（aud）が違います"
    if payload.get("iss") != f"https://{CF_TEAM_DOMAIN}":
        return False, "認証トークンの発行元が違います"
    if payload.get("exp", 0) < now - 30 or payload.get("nbf", 0) > now + 30:
        return False, "認証トークンの期限が切れています"
    email = str(payload.get("email", "")).lower()
    if email not in CF_ALLOWED_EMAILS:
        return False, f"{email or '(不明)'} には操作権限がありません"
    return True, email
