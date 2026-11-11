"""共通アカウント（auth.yuna1107.com などの共通認証基盤）の D1 を読み書きする。

共通認証基盤の D1（auth-db）を、記録用の D1 と同じ API トークンで D1 の HTTP API から引く。
このサーバーは Pi の上の Python なので Worker のように D1 を直接つなげないが、記録用の D1 に
書くためのトークン（D1 編集の権限）がすでにあり、同じアカウントの D1 ならそのまま届く。
新しいトークンや中継用の Worker は要らない。

使うもの:
  - パスキー（credentials）: 共通パスキー（COMMON_PASSKEY_RP_ID）の公開鍵と、持ち主の user_id
  - ユーザー（users）と サービス権限（user_services）: ログインしてよいかの判定
書き込むのは、このサイトで作った共通パスキーを credentials に足す・消すとき（と、その
監査ログ）と、SSO の認可コードを使い切るときだけ。ユーザーは作らない（このサイトにはアカウントを
作る機能が無い。利用者は Cloudflare Access とパスキーで決まる 1 人だけ）。

設定（.env）。AUTH_D1_DATABASE_ID と COMMON_PASSKEY_RP_ID の両方があるときだけ有効:
  AUTH_D1_DATABASE_ID   共通認証基盤の D1 データベースの ID
  COMMON_PASSKEY_RP_ID  共通パスキーの RP ID（例: example.com。サイトのホスト名の親ドメイン）
  AUTH_SERVICE_ID       共通認証基盤でのこのサイトのサービス ID（既定 status）
  AUTH_SSO_URL          共通認証基盤の URL（SSO ログイン用。例: https://auth.example.com。省略可）
  AUTH_OWNER_USER_ID    このサイトを使える唯一のユーザー ID（省略可。書くとこの人以外は拒否する）
"""

from __future__ import annotations

import json
import time
import uuid

import cloud

AUTH_DB_ID = cloud._env("AUTH_D1_DATABASE_ID")
COMMON_RP_ID = cloud._env("COMMON_PASSKEY_RP_ID")
SERVICE_ID = cloud._env("AUTH_SERVICE_ID") or "status"
SSO_URL = cloud._env("AUTH_SSO_URL").rstrip("/")
# このサイトを使える共通アカウントのユーザー ID（1 人で使うサイト用。空なら権限の判定だけ）
OWNER_USER_ID = cloud._env("AUTH_OWNER_USER_ID")


class AuthDbError(Exception):
    """共通認証基盤に問い合わせできなかった（拒否ではなく、確認できなかった）。"""


def enabled() -> bool:
    return bool(AUTH_DB_ID and COMMON_RP_ID)


def _query(sql: str, params: list) -> list[dict]:
    try:
        return cloud.query(sql, params, timeout=10, database=AUTH_DB_ID)
    except cloud.CloudError as ex:
        raise AuthDbError(f"共通アカウントを確認できません（{ex}）") from None


# 利用者とこのサイトの権限をまとめて引く列
_USER_COLS = """u.id AS user_id, u.username, u.display_name, u.status AS user_status,
                u.global_role, us.is_enabled"""
_USER_JOIN = "LEFT JOIN user_services us ON us.user_id = u.id AND us.service_id = ?"


def allowed(row: dict | None) -> bool:
    """有効なユーザーで、管理者か、このサイトの利用が許可されていれば True。

    AUTH_OWNER_USER_ID があれば、そのユーザーであることも求める。
    """
    if not row:
        return False
    if OWNER_USER_ID and row.get("user_id") != OWNER_USER_ID:
        return False
    if row.get("user_status") != "active":
        return False
    return row.get("global_role") == "admin" or row.get("is_enabled") == 1


def credential(cred_id: str) -> dict | None:
    """パスキーの ID から、公開鍵（COSE のバイト列）・RP ID・持ち主と権限を引く。"""
    rows = _query(
        f"""SELECT c.id, c.rp_id, c.name, hex(c.public_key) AS public_key_hex, {_USER_COLS}
            FROM credentials c JOIN users u ON u.id = c.user_id {_USER_JOIN}
            WHERE c.id = ?""",
        [SERVICE_ID, cred_id],
    )
    if not rows:
        return None
    row = dict(rows[0])
    row["public_key"] = bytes.fromhex(row.pop("public_key_hex") or "")
    return row


def user(user_id: str) -> dict | None:
    rows = _query(f"SELECT {_USER_COLS} FROM users u {_USER_JOIN} WHERE u.id = ?", [SERVICE_ID, user_id])
    return dict(rows[0]) if rows else None


def common_credential_ids(user_id: str) -> list[str]:
    rows = _query("SELECT id FROM credentials WHERE user_id = ? AND rp_id = ?", [user_id, COMMON_RP_ID])
    return [r["id"] for r in rows]


def oauth_code(code_hash: str, redirect_uri: str) -> dict | None:
    """SSO の認可コード（未使用・期限内・このサイト宛て）の持ち主と権限。"""
    rows = _query(
        f"""SELECT {_USER_COLS}
            FROM oauth_codes c JOIN users u ON u.id = c.user_id {_USER_JOIN}
            WHERE c.code_hash = ? AND c.client_id = ? AND c.redirect_uri = ? AND c.expires_at > ?""",
        [SERVICE_ID, code_hash, SERVICE_ID, redirect_uri, int(time.time() * 1000)],
    )
    return dict(rows[0]) if rows else None


def consume_oauth_code(code_hash: str) -> None:
    _query("DELETE FROM oauth_codes WHERE code_hash = ?", [code_hash])


def add_common_credential(*, cred_id: str, user_id: str, cose: bytes, counter: int, transports: list | None,
                          name: str, flags: int, aaguid: bytes) -> None:
    """共通パスキーを共通認証基盤に登録する。公開鍵は COSE のバイト列をそのまま BLOB で保存する。"""
    if not isinstance(cose, (bytes, bytearray)) or not cose:
        raise AuthDbError("公開鍵が不正です")
    _query(
        """INSERT INTO credentials (id, user_id, public_key, counter, transports, device_type, backed_up,
                                    aaguid, name, rp_id, created_at)
           VALUES (?, ?, unhex(?), ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            cred_id, user_id, bytes(cose).hex(), counter,
            json.dumps([t for t in (transports or []) if isinstance(t, str)]),
            "multiDevice" if flags & 0x08 else "singleDevice",
            1 if flags & 0x10 else 0,
            str(uuid.UUID(bytes=bytes(aaguid))) if len(aaguid) == 16 else None,
            name, COMMON_RP_ID, int(time.time() * 1000),
        ],
    )


def delete_common_credential(cred_id: str, user_id: str) -> None:
    """共通パスキーを共通認証基盤から消す（持ち主と RP ID が一致するものだけ）。"""
    _query("DELETE FROM credentials WHERE id = ? AND user_id = ? AND rp_id = ?", [cred_id, user_id, COMMON_RP_ID])


def audit(action: str, user_id: str, details: dict, ip: str = "", user_agent: str = "") -> None:
    """共通認証基盤の監査ログ（audit_logs）に 1 行書く。"""
    _query(
        """INSERT INTO audit_logs (id, timestamp, actor_id, target_user_id, service_id, action, details, ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [str(uuid.uuid4()), int(time.time() * 1000), user_id, user_id, SERVICE_ID, action,
         json.dumps(details, ensure_ascii=False, separators=(",", ":")), ip or None, (user_agent or "")[:300] or None],
    )
