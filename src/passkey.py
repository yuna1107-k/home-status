"""パスキー（WebAuthn）によるログインと、ログイン中のセッション管理。

- 登録・認証ともに userVerification=required（Face ID / Touch ID / PIN などの本人確認を必須にする）
- 登録したパスキーは data/passkeys.json に保存（公開鍵だけ。秘密鍵は端末から出ない）
- セッションはメモリ上にだけ持つ（サーバーを再起動したら再ログイン）
- 共通アカウント（authdb.py）が有効なら:
  - ログインはまず共通パスキー（COMMON_PASSKEY_RP_ID）、無ければこのサイトのパスキー（RP_ID）
  - ログイン時とセッション利用時に、共通アカウントでこのサイトの利用が許可されているかを確かめる
  - このサイトのパスキーでログインしたら、共通パスキーへの切り替えを勧める
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

import authdb
import cloud

# サイトのホスト名（例: status.example.com）。パスキーはこのホスト名に結び付く
RP_ID = os.environ.get("PASSKEY_RP_ID", "localhost")
RP_NAME = "Home Status"
ORIGIN = os.environ.get("PASSKEY_ORIGIN", f"https://{RP_ID}")
KV_KEY = "passkeys"
STORE = Path(os.environ.get("PASSKEY_STORE", str(cloud.DATA_DIR / "passkeys.json")))
SESSION_SECONDS = int(os.environ.get("PASSKEY_SESSION_SECONDS", str(12 * 3600)))
CHALLENGE_SECONDS = 300
COOKIE = "hs_session"
# 共通アカウントの権限を確かめ直す間隔と、確かめられないときに前回の結果を使い続ける長さ（秒）
AUTH_RECHECK_SECONDS = 60
AUTH_GRACE_SECONDS = 600

_lock = threading.Lock()
_challenges: dict[str, tuple[str, float, str, str]] = {}  # challenge -> (用途, 期限, メール, RP ID)
_sessions: dict[str, dict] = {}  # セッション ID -> {email, credential, expires, auth_user_id, ...}


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
            {**{k: c.get(k) for k in ("id", "name", "created_at", "last_used_at", "email")},
             "common": bool(COMMON_RP_ID) and c.get("rp_id") == COMMON_RP_ID}
            for c in _load()["credentials"]
        ]


def has_credentials() -> bool:
    with _lock:
        return bool(_load()["credentials"])


# 共通パスキーの RP ID（共通アカウントが未設定なら空）
COMMON_RP_ID = authdb.COMMON_RP_ID if authdb.enabled() else ""


def common_enabled() -> bool:
    return bool(COMMON_RP_ID)


def _new_challenge(purpose: str, email: str, rp_id: str = RP_ID) -> str:
    now = time.time()
    challenge = b64url(secrets.token_bytes(32))
    with _lock:
        for key in [k for k, v in _challenges.items() if v[1] < now]:
            del _challenges[key]
        _challenges[challenge] = (purpose, now + CHALLENGE_SECONDS, email, rp_id)
    return challenge


def _take_challenge(challenge: str, purpose: str, email: str) -> str:
    """チャレンジを使い切り、発行したときの RP ID を返す。"""
    with _lock:
        entry = _challenges.pop(challenge, None)
    if entry is None or entry[0] != purpose or entry[1] < time.time() or entry[2] != email:
        raise PasskeyError("チャレンジが無効か期限切れです。もう一度やり直してください")
    return entry[3]


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


def _cose_to_pem(cose: dict) -> tuple[str, int]:
    key, alg = _cose_to_key(cose)
    pem = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pem, alg


def _parse_auth_data(auth: bytes, rp_id: str = RP_ID) -> dict:
    if len(auth) < 37:
        raise PasskeyError("認証データが短すぎます")
    rp_hash, flags, count = auth[:32], auth[32], struct.unpack(">I", auth[33:37])[0]
    # チャレンジを発行したときの RP ID とだけ照合する（共通とこのサイトのどちらでもよい、にはしない）
    if rp_hash != hashlib.sha256(rp_id.encode()).digest():
        raise PasskeyError("別のサイト向けの認証データです")
    if not flags & 0x01:
        raise PasskeyError("ユーザーの操作が確認できません")
    if not flags & 0x04:
        raise PasskeyError("本人確認（生体認証や PIN）が行われていません")
    result = {"flags": flags, "count": count}
    if flags & 0x40:  # 認証器のデータ（登録時）
        cred_len = struct.unpack(">H", auth[53:55])[0]
        result["aaguid"] = auth[37:53]
        result["credential_id"] = auth[55:55 + cred_len]
        result["cose"], end = _cbor(auth, 55 + cred_len)
        # 公開鍵（COSE）の元のバイト列。共通アカウントにはこれをそのまま保存する
        result["cose_raw"] = bytes(auth[55 + cred_len:end])
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
    rp_id = _take_challenge(_check_client_data(client_raw, "webauthn.create"), "register", email)
    auth = _parse_auth_data(att_obj["authData"], rp_id)
    if "credential_id" not in auth:
        raise PasskeyError("公開鍵が含まれていません")
    pem, alg = _cose_to_pem(auth["cose"])
    cred_id = b64url(auth["credential_id"])
    name = str(body.get("name") or "パスキー").strip()[:40] or "パスキー"
    with _lock:
        data = _load()
        if any(c["id"] == cred_id for c in data["credentials"]):
            raise PasskeyError("このパスキーは登録済みです")
        data["credentials"].append({
            "id": cred_id, "public_key": pem, "alg": alg, "sign_count": auth["count"],
            "name": name, "email": email, "rp_id": RP_ID,
            "created_at": time.strftime("%Y-%m-%d %H:%M"), "last_used_at": None,
        })
        _save(data)
    return {"id": cred_id, "name": name}


# ---------------------------------------------------------------- 共通パスキーへの切り替え

def common_registration_options(email: str, sess: dict) -> dict:
    """ログイン中の人の共通アカウントに、共通パスキーを作るためのオプション。"""
    if not common_enabled():
        raise PasskeyError("共通アカウントが設定されていません")
    user_id = sess.get("auth_user_id")
    try:
        user = authdb.user(user_id) if user_id else None
        existing = authdb.common_credential_ids(user_id) if user else []
    except authdb.AuthDbError as ex:
        raise PasskeyError(str(ex)) from None
    if not authdb.allowed(user):
        raise PasskeyError("共通アカウントでこのサイトの利用が許可されていません")
    return {
        "challenge": _new_challenge("register-common", email, COMMON_RP_ID),
        "rp": {"id": COMMON_RP_ID, "name": f"{COMMON_RP_ID} 共通アカウント"},
        # 共通認証基盤と同じく、ユーザー ID（UUID の文字列）をそのままユーザーハンドルにする
        "user": {"id": b64url(user_id.encode()), "name": user["username"],
                 "displayName": user.get("display_name") or user["username"]},
        "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in (-7, -8, -257)],
        "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                   "userVerification": "required"},
        "excludeCredentials": [{"type": "public-key", "id": c} for c in existing],
        "attestation": "none",
        "timeout": CHALLENGE_SECONDS * 1000,
    }


def register_common(email: str, sess: dict, body: dict, ip: str = "", user_agent: str = "") -> dict:
    """共通パスキーを検証し、共通アカウント（auth-db）と手元の両方に保存する。"""
    if not common_enabled():
        raise PasskeyError("共通アカウントが設定されていません")
    user_id = sess.get("auth_user_id")
    if not user_id:
        raise PasskeyError("共通アカウントに紐付いていません。もう一度ログインしてください")
    try:
        client_raw = unb64url(body["response"]["clientDataJSON"])
        att_obj, _ = _cbor(unb64url(body["response"]["attestationObject"]))
    except (KeyError, TypeError, ValueError, IndexError):
        raise PasskeyError("登録データが不正です") from None
    rp_id = _take_challenge(_check_client_data(client_raw, "webauthn.create"), "register-common", email)
    if rp_id != COMMON_RP_ID:
        raise PasskeyError("チャレンジが無効か期限切れです。もう一度やり直してください")
    auth = _parse_auth_data(att_obj["authData"], COMMON_RP_ID)
    if "credential_id" not in auth:
        raise PasskeyError("公開鍵が含まれていません")
    pem, alg = _cose_to_pem(auth["cose"])
    cred_id = b64url(auth["credential_id"])
    name = str(body.get("name") or "").strip()[:40] or "共通パスキー"
    transports = (body.get("response") or {}).get("transports")
    with _lock:
        if any(c["id"] == cred_id for c in _load()["credentials"]):
            raise PasskeyError("このパスキーは登録済みです")
    # 先に共通アカウントへ保存する（失敗したら手元にも残さない）
    try:
        authdb.add_common_credential(
            cred_id=cred_id, user_id=user_id, cose=auth["cose_raw"], counter=auth["count"],
            transports=transports if isinstance(transports, list) else None,
            name=name, flags=auth["flags"], aaguid=auth["aaguid"],
        )
    except authdb.AuthDbError as ex:
        raise PasskeyError(f"共通アカウントに保存できませんでした: {ex}") from None
    try:
        authdb.audit("passkey.register", user_id, {"via": "status"}, ip, user_agent)
    except authdb.AuthDbError as ex:  # 監査ログが書けなくても登録は済んでいる
        print(f"passkey: 共通アカウントの監査ログを書けませんでした: {ex}", flush=True)
    with _lock:
        data = _load()
        data["credentials"].append({
            "id": cred_id, "public_key": pem, "alg": alg, "sign_count": auth["count"],
            "name": name, "email": email, "rp_id": COMMON_RP_ID, "auth_user_id": user_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M"), "last_used_at": None,
        })
        _save(data)
        for s in _sessions.values():
            if s.get("auth_user_id") == user_id:
                s["needs_migration"] = False
    return {"id": cred_id, "name": name}


# ---------------------------------------------------------------- ログイン

def login_options(email: str, common: bool = True) -> dict:
    """common=True なら共通パスキー（無効ならこのサイトのパスキー）でのログイン用。"""
    if common and common_enabled():
        # 共通パスキーは端末に保存されたもの（discoverable）から選んでもらう
        return {
            "challenge": _new_challenge("login", email, COMMON_RP_ID),
            "rpId": COMMON_RP_ID,
            "common": True,
            "allowCredentials": [],
            "userVerification": "required",
            "timeout": CHALLENGE_SECONDS * 1000,
        }
    with _lock:
        creds = [c for c in _load()["credentials"] if c.get("rp_id", RP_ID) == RP_ID]
    return {
        "challenge": _new_challenge("login", email, RP_ID),
        "rpId": RP_ID,
        "common": False,
        "allowCredentials": [{"type": "public-key", "id": c["id"]} for c in creds],
        "userVerification": "required",
        "timeout": CHALLENGE_SECONDS * 1000,
    }


def _resolve_auth_user(cred: dict, email: str) -> dict | None:
    """このサイトのパスキーの持ち主を共通アカウントから探す（ID → パスキー → メールの順）。"""
    if cred.get("auth_user_id"):
        return authdb.user(cred["auth_user_id"])
    row = authdb.credential(cred["id"])
    if row:
        return row
    return authdb.user_by_email(email)


def login(email: str, body: dict) -> str:
    """検証に成功したらセッション ID を返す。"""
    try:
        cred_id = str(body["id"])
        client_raw = unb64url(body["response"]["clientDataJSON"])
        auth_raw = unb64url(body["response"]["authenticatorData"])
        signature = unb64url(body["response"]["signature"])
    except (KeyError, TypeError, ValueError):
        raise PasskeyError("ログインデータが不正です") from None
    rp_id = _take_challenge(_check_client_data(client_raw, "webauthn.get"), "login", email)
    auth = _parse_auth_data(auth_raw, rp_id)
    common = bool(COMMON_RP_ID) and rp_id == COMMON_RP_ID

    remote = None
    if common:
        # 共通パスキーは共通アカウントが正本。消されていたら使えない
        try:
            remote = authdb.credential(cred_id)
        except authdb.AuthDbError as ex:
            raise PasskeyError(str(ex)) from None
        if remote is None or remote.get("rp_id") != COMMON_RP_ID:
            raise PasskeyError("登録されていないパスキーです")
        try:
            remote_pem, remote_alg = _cose_to_pem(_cbor(remote["public_key"])[0])
        except (PasskeyError, IndexError, KeyError, TypeError, ValueError):
            raise PasskeyError("共通アカウントのパスキーの公開鍵を読み取れません") from None

    with _lock:
        data = _load()
        cred = next((c for c in data["credentials"] if c["id"] == cred_id), None)
        if common:
            if cred is None:
                # 手元に控えを作る（一覧の表示と署名カウンタの確認に使う）
                cred = {
                    "id": cred_id, "sign_count": 0, "email": email,
                    "name": remote.get("name") or "共通パスキー",
                    "created_at": time.strftime("%Y-%m-%d %H:%M"), "last_used_at": None,
                }
                data["credentials"].append(cred)
            cred.update({"public_key": remote_pem, "alg": remote_alg, "rp_id": COMMON_RP_ID,
                         "auth_user_id": remote["user_id"]})
        elif cred is None or cred.get("rp_id", RP_ID) != RP_ID:
            raise PasskeyError("登録されていないパスキーです")
        _verify_signature(cred["public_key"], cred["alg"], signature,
                          auth_raw + hashlib.sha256(client_raw).digest())
        # 署名カウンタが戻っていたら複製された鍵の疑い（0 のままの認証器もあるので両方 0 は許可）
        if (auth["count"] or cred["sign_count"]) and auth["count"] <= cred["sign_count"]:
            raise PasskeyError("署名カウンタが不正です（パスキーが複製された可能性があります）")
        cred["sign_count"] = auth["count"]
        cred["last_used_at"] = time.strftime("%Y-%m-%d %H:%M")
        _save(data)
        cred = dict(cred)

    # 共通アカウントで、このサイトの利用が許可されているか
    auth_user_id = None
    needs_migration = False
    if common_enabled():
        try:
            row = remote if common else _resolve_auth_user(cred, email)
            if row and not common and not authdb.common_credential_ids(row["user_id"]):
                needs_migration = True
        except authdb.AuthDbError as ex:
            raise PasskeyError(str(ex)) from None
        if row is None:
            raise PasskeyError("このパスキーは共通アカウントに紐付いていません")
        if not authdb.allowed(row):
            raise PasskeyError("共通アカウントでこのサイトの利用が許可されていません")
        auth_user_id = row["user_id"]

    with _lock:
        if auth_user_id and not common:
            # 一度見つけた持ち主は ID で覚えておく（以降はメールが変わっても同じ人として扱う）
            data = _load()
            for c in data["credentials"]:
                if c["id"] == cred_id and c.get("auth_user_id") != auth_user_id:
                    c["auth_user_id"] = auth_user_id
                    _save(data)
        return _new_session(email, cred["name"], auth_user_id, needs_migration)


def _new_session(email: str, credential: str, auth_user_id: str | None, needs_migration: bool = False) -> str:
    """セッションを作る（_lock を持った状態で呼ぶ）。"""
    now = time.time()
    for sid in [s for s, v in _sessions.items() if v["expires"] < now]:
        del _sessions[sid]
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {"email": email, "credential": credential, "expires": now + SESSION_SECONDS,
                      "auth_user_id": auth_user_id, "checked_at": now, "ok_at": now,
                      "needs_migration": needs_migration}
    return sid


def sso_session(email: str, row: dict) -> str:
    """共通認証基盤の SSO で確かめた人のセッションを作る。"""
    with _lock:
        return _new_session(email, f"SSO ({row.get('username')})", row["user_id"])


def _still_allowed(sid: str, sess: dict) -> bool:
    """共通アカウントの権限を確かめ直す（AUTH_RECHECK_SECONDS ごと）。取り消されていたら False。"""
    now = time.time()
    user_id = sess.get("auth_user_id")
    if not user_id:
        return False
    with _lock:
        live = _sessions.get(sid)
        if live is None:
            return False
        if now - live["checked_at"] < AUTH_RECHECK_SECONDS:
            return True
        live["checked_at"] = now  # 同時に来たリクエストで何度も問い合わせない
    try:
        ok = authdb.allowed(authdb.user(user_id))
    except authdb.AuthDbError as ex:
        # 一時的につながらないだけなら、しばらくは前回の結果を使う
        print(f"passkey: {ex}", flush=True)
        return now - sess["ok_at"] < AUTH_GRACE_SECONDS
    with _lock:
        if not ok:
            _sessions.pop(sid, None)
            return False
        if sid in _sessions:
            _sessions[sid]["ok_at"] = now
    return True


def session(cookie_header: str, email: str) -> dict | None:
    """有効なセッションなら {email, credential, expires, ...} を返す。メールが違えば無効。"""
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
        sess = dict(sess)
    if common_enabled() and not _still_allowed(sid, sess):
        return None
    return sess


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


def remove_credential(cred_id: str, auth_user_id: str | None = None, ip: str = "", user_agent: str = "") -> bool:
    """パスキーを消す。共通パスキーは共通アカウント（auth-db）からも消す（すべてのサービスで使えなくなる）。

    auth_user_id を渡すと、その人の共通パスキーだけを消せる（ログイン中の人が他人の分を消さないように）。
    """
    with _lock:
        cred = next((c for c in _load()["credentials"] if c["id"] == cred_id), None)
    if cred is None:
        return False
    if COMMON_RP_ID and cred.get("rp_id") == COMMON_RP_ID:
        owner = cred.get("auth_user_id")
        if not owner or (auth_user_id and owner != auth_user_id):
            raise PasskeyError("このパスキーは削除できません（共通アカウントの持ち主が違います）")
        try:
            authdb.delete_common_credential(cred_id, owner)
        except authdb.AuthDbError as ex:
            raise PasskeyError(f"共通アカウントから削除できませんでした: {ex}") from None
        try:
            authdb.audit("passkey.delete", owner, {"via": "status", "credId": cred_id}, ip, user_agent)
        except authdb.AuthDbError as ex:
            print(f"passkey: 共通アカウントの監査ログを書けませんでした: {ex}", flush=True)
    with _lock:
        data = _load()
        before = len(data["credentials"])
        data["credentials"] = [c for c in data["credentials"] if c["id"] != cred_id]
        if len(data["credentials"]) == before:
            return False
        if not data["credentials"] and not COMMON_RP_ID:
            # 最後の 1 つを消したら全セッションも無効にする（次は最初の登録からやり直し）
            _sessions.clear()
        _save(data)
        return True
