"""パスキー（WebAuthn）によるログインと、ログイン中のセッション管理。

- 登録・認証ともに userVerification=required（Face ID / Touch ID / PIN などの本人確認を必須にする）
- 登録したパスキーは data/passkeys.json に保存（公開鍵だけ。秘密鍵は端末から出ない）
- セッションはメモリ上にだけ持つ（サーバーを再起動したら再ログイン）
- 外部ライブラリは使わず、CBOR の読み取りと署名検証を自前で行う（署名検証は cryptography）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

import cloud

# サイトのホスト名（例: status.example.com）。パスキーはこのホスト名に結び付く
RP_ID = os.environ.get("PASSKEY_RP_ID", "localhost")
RP_NAME = "Home Status"
ORIGIN = os.environ.get("PASSKEY_ORIGIN", f"https://{RP_ID}")
KV_KEY = "passkeys"
STORE = Path(os.environ.get("PASSKEY_STORE", str(Path(__file__).resolve().parent / "data/passkeys.json")))
SESSION_SECONDS = int(os.environ.get("PASSKEY_SESSION_SECONDS", str(12 * 3600)))
CHALLENGE_SECONDS = 300
COOKIE = "hs_session"

_lock = threading.Lock()
_challenges: dict[str, tuple[str, float, str]] = {}  # challenge -> (用途, 期限, メール)
_sessions: dict[str, dict] = {}  # セッション ID -> {email, credential, expires}


class PasskeyError(Exception):
    pass


# ---------------------------------------------------------------- 共通

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"user_id": b64url(secrets.token_bytes(16)), "credentials": []}


def _save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE.parent, 0o700)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)
    # 端末が壊れても登録をやり直さずに済むよう D1 にも控える（公開鍵だけなので漏れても使えない）
    cloud.kv_put(KV_KEY, data)


def restore() -> None:
    """起動時: 手元に登録が無ければ D1 の控えから戻す。"""
    cloud.restore_later(KV_KEY, STORE, lambda v: not v.get("credentials"))


def credentials() -> list[dict]:
    with _lock:
        return [
            {k: c.get(k) for k in ("id", "name", "created_at", "last_used_at", "email")}
            for c in _load()["credentials"]
        ]


def has_credentials() -> bool:
    with _lock:
        return bool(_load()["credentials"])


def _new_challenge(purpose: str, email: str) -> str:
    now = time.time()
    challenge = b64url(secrets.token_bytes(32))
    with _lock:
        for key in [k for k, v in _challenges.items() if v[1] < now]:
            del _challenges[key]
        _challenges[challenge] = (purpose, now + CHALLENGE_SECONDS, email)
    return challenge


def _take_challenge(challenge: str, purpose: str, email: str) -> None:
    with _lock:
        entry = _challenges.pop(challenge, None)
    if entry is None or entry[0] != purpose or entry[1] < time.time() or entry[2] != email:
        raise PasskeyError("チャレンジが無効か期限切れです。もう一度やり直してください")


# ---------------------------------------------------------------- CBOR（WebAuthn で使う範囲だけ）

def _cbor(data: bytes, pos: int = 0):
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if info < 24:
        val = info
    elif info == 24:
        val = data[pos]; pos += 1
    elif info == 25:
        val = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
    elif info == 26:
        val = struct.unpack(">I", data[pos:pos + 4])[0]; pos += 4
    elif info == 27:
        val = struct.unpack(">Q", data[pos:pos + 8])[0]; pos += 8
    else:
        raise PasskeyError("対応していない CBOR です")
    if major == 0:
        return val, pos
    if major == 1:
        return -1 - val, pos
    if major == 2:
        return data[pos:pos + val], pos + val
    if major == 3:
        return data[pos:pos + val].decode(), pos + val
    if major == 4:
        items = []
        for _ in range(val):
            item, pos = _cbor(data, pos)
            items.append(item)
        return items, pos
    if major == 5:
        obj = {}
        for _ in range(val):
            key, pos = _cbor(data, pos)
            obj[key], pos = _cbor(data, pos)
        return obj, pos
    if major == 7:
        return {20: False, 21: True, 22: None}.get(info), pos
    raise PasskeyError("対応していない CBOR です")


def _cose_to_key(cose: dict):
    kty, alg = cose.get(1), cose.get(3)
    if kty == 2 and alg == -7 and cose.get(-1) == 1:  # EC2 / ES256 / P-256
        x, y = cose[-2], cose[-3]
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + x + y), alg
    if kty == 3 and alg == -257:  # RSA / RS256
        n = int.from_bytes(cose[-1], "big")
        e = int.from_bytes(cose[-2], "big")
        return rsa.RSAPublicNumbers(e, n).public_key(), alg
    if kty == 1 and alg == -8 and cose.get(-1) == 6:  # OKP / EdDSA / Ed25519
        return ed25519.Ed25519PublicKey.from_public_bytes(cose[-2]), alg
    raise PasskeyError("対応していない鍵の種類です")


def _verify_signature(pem: str, alg: int, signature: bytes, data: bytes) -> None:
    key = serialization.load_pem_public_key(pem.encode())
    try:
        if alg == -7:
            key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif alg == -257:
            key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        elif alg == -8:
            key.verify(signature, data)
        else:
            raise PasskeyError("対応していない署名方式です")
    except InvalidSignature:
        raise PasskeyError("署名が正しくありません") from None


def _parse_auth_data(auth: bytes) -> dict:
    if len(auth) < 37:
        raise PasskeyError("認証データが短すぎます")
    rp_hash, flags, count = auth[:32], auth[32], struct.unpack(">I", auth[33:37])[0]
    if rp_hash != hashlib.sha256(RP_ID.encode()).digest():
        raise PasskeyError("別のサイト向けの認証データです")
    if not flags & 0x01:
        raise PasskeyError("ユーザーの操作が確認できません")
    if not flags & 0x04:
        raise PasskeyError("本人確認（生体認証や PIN）が行われていません")
    result = {"flags": flags, "count": count}
    if flags & 0x40:  # 認証器のデータ（登録時）
        cred_len = struct.unpack(">H", auth[53:55])[0]
        result["credential_id"] = auth[55:55 + cred_len]
        result["cose"], _ = _cbor(auth, 55 + cred_len)
    return result


def _check_client_data(raw: bytes, kind: str) -> str:
    try:
        client = json.loads(raw)
    except json.JSONDecodeError:
        raise PasskeyError("clientDataJSON が不正です") from None
    if client.get("type") != kind:
        raise PasskeyError("種類が違います")
    if client.get("origin") != ORIGIN:
        raise PasskeyError(f"想定外の接続元です: {client.get('origin')}")
    if client.get("crossOrigin"):
        raise PasskeyError("別サイトからの呼び出しは受け付けません")
    return client.get("challenge", "")


# ---------------------------------------------------------------- 登録

def registration_options(email: str) -> dict:
    with _lock:
        data = _load()
        if not STORE.exists():
            _save(data)
    return {
        "challenge": _new_challenge("register", email),
        "rp": {"id": RP_ID, "name": RP_NAME},
        "user": {"id": data["user_id"], "name": email, "displayName": email},
        "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in (-7, -8, -257)],
        "authenticatorSelection": {"residentKey": "preferred", "userVerification": "required"},
        "excludeCredentials": [{"type": "public-key", "id": c["id"]} for c in data["credentials"]],
        "attestation": "none",
        "timeout": CHALLENGE_SECONDS * 1000,
    }


def register(email: str, body: dict) -> dict:
    try:
        client_raw = unb64url(body["response"]["clientDataJSON"])
        att_obj, _ = _cbor(unb64url(body["response"]["attestationObject"]))
    except (KeyError, TypeError, ValueError, IndexError):
        raise PasskeyError("登録データが不正です") from None
    _take_challenge(_check_client_data(client_raw, "webauthn.create"), "register", email)
    auth = _parse_auth_data(att_obj["authData"])
    if "credential_id" not in auth:
        raise PasskeyError("公開鍵が含まれていません")
    key, alg = _cose_to_key(auth["cose"])
    pem = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    cred_id = b64url(auth["credential_id"])
    name = str(body.get("name") or "パスキー").strip()[:40] or "パスキー"
    with _lock:
        data = _load()
        if any(c["id"] == cred_id for c in data["credentials"]):
            raise PasskeyError("このパスキーは登録済みです")
        data["credentials"].append({
            "id": cred_id, "public_key": pem, "alg": alg, "sign_count": auth["count"],
            "name": name, "email": email,
            "created_at": time.strftime("%Y-%m-%d %H:%M"), "last_used_at": None,
        })
        _save(data)
    return {"id": cred_id, "name": name}


# ---------------------------------------------------------------- ログイン

def login_options(email: str) -> dict:
    with _lock:
        creds = _load()["credentials"]
    return {
        "challenge": _new_challenge("login", email),
        "rpId": RP_ID,
        "allowCredentials": [{"type": "public-key", "id": c["id"]} for c in creds],
        "userVerification": "required",
        "timeout": CHALLENGE_SECONDS * 1000,
    }


def login(email: str, body: dict) -> str:
    """検証に成功したらセッション ID を返す。"""
    try:
        cred_id = body["id"]
        client_raw = unb64url(body["response"]["clientDataJSON"])
        auth_raw = unb64url(body["response"]["authenticatorData"])
        signature = unb64url(body["response"]["signature"])
    except (KeyError, TypeError, ValueError):
        raise PasskeyError("ログインデータが不正です") from None
    _take_challenge(_check_client_data(client_raw, "webauthn.get"), "login", email)
    auth = _parse_auth_data(auth_raw)

    with _lock:
        data = _load()
        cred = next((c for c in data["credentials"] if c["id"] == cred_id), None)
        if cred is None:
            raise PasskeyError("登録されていないパスキーです")
        _verify_signature(cred["public_key"], cred["alg"], signature,
                          auth_raw + hashlib.sha256(client_raw).digest())
        # 署名カウンタが戻っていたら複製された鍵の疑い（0 のままの認証器もあるので両方 0 は許可）
        if (auth["count"] or cred["sign_count"]) and auth["count"] <= cred["sign_count"]:
            raise PasskeyError("署名カウンタが不正です（パスキーが複製された可能性があります）")
        cred["sign_count"] = auth["count"]
        cred["last_used_at"] = time.strftime("%Y-%m-%d %H:%M")
        _save(data)

        now = time.time()
        for sid in [s for s, v in _sessions.items() if v["expires"] < now]:
            del _sessions[sid]
        sid = secrets.token_urlsafe(32)
        _sessions[sid] = {"email": email, "credential": cred["name"], "expires": now + SESSION_SECONDS}
    return sid


def session(cookie_header: str, email: str) -> dict | None:
    """有効なセッションなら {email, credential, expires} を返す。メールが違えば無効。"""
    sid = None
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            sid = value
    if not sid:
        return None
    with _lock:
        sess = _sessions.get(sid)
        if sess is None or sess["expires"] < time.time() or sess["email"] != email:
            return None
        return dict(sess)


def logout(cookie_header: str) -> None:
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            with _lock:
                _sessions.pop(value, None)


def cookie_for(sid: str) -> str:
    return f"{COOKIE}={sid}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict"


def clear_cookie() -> str:
    return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"


def remove_credential(cred_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["credentials"])
        data["credentials"] = [c for c in data["credentials"] if c["id"] != cred_id]
        if len(data["credentials"]) == before:
            return False
        if not data["credentials"]:
            # 最後の 1 つを消したら全セッションも無効にする（次は最初の登録からやり直し）
            _sessions.clear()
        _save(data)
        return True
