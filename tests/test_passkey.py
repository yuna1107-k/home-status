"""passkey.py の登録・ログイン検証を、ソフトウェアで作った認証器で確かめる。"""

import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["PASSKEY_STORE"] = str(Path(tempfile.mkdtemp()) / "passkeys.json")
# テストの登録を本物の D1 に送らない
os.environ["CLOUD_OUTBOX"] = str(Path(tempfile.mkdtemp()) / "outbox.jsonl")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

import authdb  # noqa: E402
import passkey  # noqa: E402

EMAIL = "user@example.com"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def cbor(obj) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        if n < 256:
            return bytes([major << 5 | 24, n])
        if n < 65536:
            return bytes([major << 5 | 25]) + struct.pack(">H", n)
        return bytes([major << 5 | 26]) + struct.pack(">I", n)
    if isinstance(obj, int):
        return head(0, obj) if obj >= 0 else head(1, -1 - obj)
    if isinstance(obj, bytes):
        return head(2, len(obj)) + obj
    if isinstance(obj, str):
        return head(3, len(obj.encode())) + obj.encode()
    if isinstance(obj, dict):
        return head(5, len(obj)) + b"".join(cbor(k) + cbor(v) for k, v in obj.items())
    raise TypeError(obj)


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    def _auth_data(self, flags, extra=b"", rp_id=passkey.RP_ID):
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + extra

    def cose(self):
        nums = self.key.public_key().public_numbers()
        return cbor({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def create(self, challenge, origin=passkey.ORIGIN, flags=0x45, rp_id=passkey.RP_ID):
        att = b"\0" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + self.cose()
        client = json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": origin}).encode()
        obj = cbor({"fmt": "none", "attStmt": {}, "authData": self._auth_data(flags, att, rp_id=rp_id)})
        return {"id": b64(self.cred_id), "name": "テスト端末",
                "response": {"clientDataJSON": b64(client), "attestationObject": b64(obj)}}

    def get(self, challenge, origin=passkey.ORIGIN, flags=0x05, bump=True, rp_id=passkey.RP_ID):
        if bump:
            self.count += 1
        auth = self._auth_data(flags, rp_id=rp_id)
        client = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin}).encode()
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id),
                "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth), "signature": b64(sig)}}


class PasskeyTest(unittest.TestCase):
    def setUp(self):
        passkey.COMMON_RP_ID = ""  # 共通アカウントなし（.env に設定があっても使わない）
        Path(os.environ["PASSKEY_STORE"]).unlink(missing_ok=True)
        self.auth = Authenticator()
        opts = passkey.registration_options(EMAIL)
        passkey.register(EMAIL, self.auth.create(opts["challenge"]))

    def login(self, **kw):
        opts = passkey.login_options(EMAIL)
        return passkey.login(EMAIL, self.auth.get(opts["challenge"], **kw))

    def test_register_and_login(self):
        self.assertTrue(passkey.has_credentials())
        sid = self.login()
        cookie = f"other=1; {passkey.COOKIE}={sid}"
        self.assertEqual(passkey.session(cookie, EMAIL)["email"], EMAIL)
        self.assertIsNone(passkey.session(cookie, "someone@example.com"))  # 別メールでは無効
        passkey.logout(cookie)
        self.assertIsNone(passkey.session(cookie, EMAIL))

    def test_wrong_origin(self):
        with self.assertRaises(passkey.PasskeyError):
            self.login(origin="https://evil.example.com")

    def test_wrong_rp(self):
        with self.assertRaises(passkey.PasskeyError):
            self.login(rp_id="evil.example.com")

    def test_no_user_verification(self):
        with self.assertRaises(passkey.PasskeyError):
            self.login(flags=0x01)

    def test_replayed_challenge(self):
        opts = passkey.login_options(EMAIL)
        assertion = self.auth.get(opts["challenge"])
        passkey.login(EMAIL, assertion)
        with self.assertRaises(passkey.PasskeyError):
            passkey.login(EMAIL, assertion)

    def test_counter_rollback(self):
        self.login()
        self.auth.count = 0
        with self.assertRaises(passkey.PasskeyError):
            self.login(bump=False)

    def test_bad_signature(self):
        opts = passkey.login_options(EMAIL)
        assertion = self.auth.get(opts["challenge"])
        assertion["response"]["signature"] = b64(b"\x30\x06\x02\x01\x01\x02\x01\x01")
        with self.assertRaises(passkey.PasskeyError):
            passkey.login(EMAIL, assertion)

    def test_challenge_bound_to_email(self):
        opts = passkey.login_options("someone@example.com")
        with self.assertRaises(passkey.PasskeyError):
            passkey.login(EMAIL, self.auth.get(opts["challenge"]))

    def test_unknown_credential(self):
        other = Authenticator()
        opts = passkey.login_options(EMAIL)
        with self.assertRaises(passkey.PasskeyError):
            passkey.login(EMAIL, other.get(opts["challenge"]))

    def test_remove_last_clears_sessions(self):
        sid = self.login()
        cred = passkey.credentials()[0]["id"]
        self.assertTrue(passkey.remove_credential(cred))
        self.assertFalse(passkey.has_credentials())
        self.assertIsNone(passkey.session(f"{passkey.COOKIE}={sid}", EMAIL))



COMMON = "example.com"
USER_ID = "00000000-0000-4000-8000-000000000001"


class FakeAuthDb:
    """authdb の問い合わせを差し替える（共通認証基盤の D1 の代わり）。"""

    def __init__(self):
        self.users = {USER_ID: {"user_id": USER_ID, "username": "owner", "display_name": "Owner",
                                "user_status": "active", "global_role": "admin", "is_enabled": None}}
        self.emails = {EMAIL: USER_ID}
        self.creds = {}  # id -> {rp_id, user_id, public_key, name}
        self.added = []
        self.down = False

    def _check(self):
        if self.down:
            raise authdb.AuthDbError("共通アカウントを確認できません（テスト）")

    def credential(self, cred_id):
        self._check()
        c = self.creds.get(cred_id)
        return {**c, **self.users[c["user_id"]], "id": cred_id} if c else None

    def user(self, user_id):
        self._check()
        u = self.users.get(user_id)
        return dict(u) if u else None

    def user_by_email(self, email):
        self._check()
        uid = self.emails.get(email)
        return self.user(uid) if uid else None

    def common_credential_ids(self, user_id):
        self._check()
        return [i for i, c in self.creds.items() if c["user_id"] == user_id and c["rp_id"] == COMMON]

    def add_common_credential(self, **kw):
        self._check()
        self.added.append(kw)
        self.creds[kw["cred_id"]] = {"rp_id": COMMON, "user_id": kw["user_id"], "public_key": kw["cose"],
                                     "name": kw["name"]}


class CommonAuthTest(unittest.TestCase):
    def setUp(self):
        Path(os.environ["PASSKEY_STORE"]).unlink(missing_ok=True)
        passkey._sessions.clear()
        self.db = FakeAuthDb()
        self._saved = {n: getattr(authdb, n) for n in
                       ("credential", "user", "user_by_email", "common_credential_ids", "add_common_credential")}
        for name in self._saved:
            setattr(authdb, name, getattr(self.db, name))
        passkey.COMMON_RP_ID = COMMON

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(authdb, name, fn)
        passkey.COMMON_RP_ID = ""

    def common_auth(self):
        a = Authenticator()
        self.db.creds[b64(a.cred_id)] = {"rp_id": COMMON, "user_id": USER_ID, "public_key": a.cose(), "name": "共通"}
        return a

    def legacy_auth(self):
        a = Authenticator()
        opts = passkey.registration_options(EMAIL)
        passkey.register(EMAIL, a.create(opts["challenge"]))
        return a

    def login(self, a, common=True, **kw):
        opts = passkey.login_options(EMAIL, common=common)
        kw.setdefault("rp_id", opts["rpId"])
        return passkey.login(EMAIL, a.get(opts["challenge"], **kw))

    def sess(self, sid):
        return passkey.session(f"{passkey.COOKIE}={sid}", EMAIL)

    def test_common_login(self):
        a = self.common_auth()
        opts = passkey.login_options(EMAIL)
        self.assertEqual((opts["rpId"], opts["common"], opts["allowCredentials"]), (COMMON, True, []))
        sess = self.sess(self.login(a))
        self.assertEqual(sess["auth_user_id"], USER_ID)
        self.assertFalse(sess["needs_migration"])
        local = passkey.credentials()
        self.assertEqual(len(local), 1)
        self.assertTrue(local[0]["common"])

    def test_common_rp_must_match_challenge(self):
        a = self.common_auth()
        with self.assertRaises(passkey.PasskeyError):
            self.login(a, rp_id=passkey.RP_ID)  # 共通のチャレンジにこのサイトの RP ID
        b = self.legacy_auth()
        with self.assertRaises(passkey.PasskeyError):
            self.login(b, common=False, rp_id=COMMON)  # このサイトのチャレンジに共通の RP ID

    def test_common_credential_removed_from_auth_db(self):
        a = self.common_auth()
        self.login(a)
        del self.db.creds[b64(a.cred_id)]
        with self.assertRaises(passkey.PasskeyError):
            self.login(a)

    def test_access_rules(self):
        a = self.common_auth()
        user = self.db.users[USER_ID]
        user.update(global_role="member", is_enabled=0)
        with self.assertRaises(passkey.PasskeyError):
            self.login(a)
        user.update(is_enabled=1)
        self.login(a)
        user.update(user_status="suspended")
        with self.assertRaises(passkey.PasskeyError):
            self.login(a)
        user.update(user_status="active", global_role="admin", is_enabled=None)
        self.login(a)

    def test_auth_db_down_at_login(self):
        a = self.common_auth()
        self.db.down = True
        with self.assertRaises(passkey.PasskeyError):
            self.login(a)

    def test_revocation_ends_session(self):
        sid = self.login(self.common_auth())
        self.assertIsNotNone(self.sess(sid))
        self.db.users[USER_ID].update(global_role="member", is_enabled=0)
        self.assertIsNotNone(self.sess(sid))  # 確かめ直すまでは前回の結果
        passkey._sessions[sid]["checked_at"] -= passkey.AUTH_RECHECK_SECONDS + 1
        self.assertIsNone(self.sess(sid))
        self.assertNotIn(sid, passkey._sessions)

    def test_auth_db_down_grace(self):
        sid = self.login(self.common_auth())
        self.db.down = True
        passkey._sessions[sid]["checked_at"] -= passkey.AUTH_RECHECK_SECONDS + 1
        self.assertIsNotNone(self.sess(sid))
        passkey._sessions[sid]["checked_at"] -= passkey.AUTH_RECHECK_SECONDS + 1
        passkey._sessions[sid]["ok_at"] -= passkey.AUTH_GRACE_SECONDS + 1
        self.assertIsNone(self.sess(sid))

    def test_legacy_login_via_auth_db_credential(self):
        a = self.legacy_auth()
        self.db.emails.clear()
        self.db.creds[b64(a.cred_id)] = {"rp_id": passkey.RP_ID, "user_id": USER_ID, "public_key": a.cose(),
                                         "name": "旧"}
        sess = self.sess(self.login(a, common=False))
        self.assertEqual(sess["auth_user_id"], USER_ID)
        self.assertTrue(sess["needs_migration"])

    def test_legacy_login_unmapped(self):
        a = self.legacy_auth()
        self.db.emails.clear()
        with self.assertRaises(passkey.PasskeyError):
            self.login(a, common=False)

    def test_legacy_mapping_is_pinned_by_id(self):
        a = self.legacy_auth()
        self.login(a, common=False)  # メールで見つけ、ID を覚える
        self.db.emails.clear()
        sess = self.sess(self.login(a, common=False))
        self.assertEqual(sess["auth_user_id"], USER_ID)

    def test_migrate_to_common(self):
        a = self.legacy_auth()
        sid = self.login(a, common=False)
        sess = self.sess(sid)
        self.assertTrue(sess["needs_migration"])
        opts = passkey.common_registration_options(EMAIL, sess)
        self.assertEqual(opts["rp"]["id"], COMMON)
        self.assertEqual(passkey.unb64url(opts["user"]["id"]).decode(), USER_ID)
        new = Authenticator()
        body = new.create(opts["challenge"], rp_id=COMMON)
        body["response"]["transports"] = ["internal", "hybrid"]
        passkey.register_common(EMAIL, sess, body)
        added = self.db.added[0]
        # 公開鍵は COSE のバイト列そのもの（余分なバイトを含まない）
        self.assertEqual(added["cose"], new.cose())
        self.assertEqual((added["user_id"], added["transports"]), (USER_ID, ["internal", "hybrid"]))
        self.assertFalse(self.sess(sid)["needs_migration"])
        self.assertEqual(sum(c["common"] for c in passkey.credentials()), 1)
        # 作った共通パスキーでログインできる
        self.assertEqual(self.sess(self.login(new))["auth_user_id"], USER_ID)

    def test_migrate_not_saved_locally_when_auth_db_fails(self):
        a = self.legacy_auth()
        sess = self.sess(self.login(a, common=False))
        opts = passkey.common_registration_options(EMAIL, sess)
        self.db.down = True
        with self.assertRaises(passkey.PasskeyError):
            passkey.register_common(EMAIL, sess, Authenticator().create(opts["challenge"], rp_id=COMMON))
        self.assertEqual(len(passkey.credentials()), 1)

    def test_migrate_rejects_wrong_rp(self):
        a = self.legacy_auth()
        sess = self.sess(self.login(a, common=False))
        opts = passkey.common_registration_options(EMAIL, sess)
        with self.assertRaises(passkey.PasskeyError):
            passkey.register_common(EMAIL, sess, Authenticator().create(opts["challenge"]))


class AuthDbSqlTest(unittest.TestCase):
    """auth-db への SQL と、公開鍵のバイト列の受け渡し。"""

    def setUp(self):
        self.calls = []
        self._query = authdb.cloud.query
        authdb.cloud.query = self.fake_query
        self.rows = []

    def tearDown(self):
        authdb.cloud.query = self._query

    def fake_query(self, sql, params, timeout=15, database=None):
        self.calls.append((sql, params, database))
        return self.rows

    def test_add_common_credential_stores_exact_bytes(self):
        cose = Authenticator().cose()
        authdb.add_common_credential(cred_id="abc", user_id=USER_ID, cose=cose, counter=0,
                                     transports=["internal"], name="Mac", flags=0x5D, aaguid=b"\x01" * 16)
        sql, params, database = self.calls[0]
        self.assertIn("unhex(?)", sql)
        self.assertNotIn("INSERT INTO users", sql)
        self.assertEqual(bytes.fromhex(params[2]), cose)
        self.assertEqual(database, authdb.AUTH_DB_ID)
        self.assertEqual(params[5:7], ["multiDevice", 1])

    def test_credential_decodes_hex_blob(self):
        cose = Authenticator().cose()
        self.rows = [{"id": "abc", "rp_id": COMMON, "name": "x", "public_key_hex": cose.hex().upper(),
                      "user_id": USER_ID, "user_status": "active", "global_role": "admin", "is_enabled": None}]
        self.assertEqual(authdb.credential("abc")["public_key"], cose)

    def test_allowed(self):
        self.assertFalse(authdb.allowed(None))
        self.assertTrue(authdb.allowed({"user_status": "active", "global_role": "admin", "is_enabled": 0}))
        self.assertTrue(authdb.allowed({"user_status": "active", "global_role": "member", "is_enabled": 1}))
        self.assertFalse(authdb.allowed({"user_status": "active", "global_role": "member", "is_enabled": None}))
        self.assertFalse(authdb.allowed({"user_status": "suspended", "global_role": "admin", "is_enabled": 1}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
