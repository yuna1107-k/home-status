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

    def create(self, challenge, origin=passkey.ORIGIN, flags=0x45):
        nums = self.key.public_key().public_numbers()
        cose = cbor({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        att = b"\0" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose
        client = json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": origin}).encode()
        obj = cbor({"fmt": "none", "attStmt": {}, "authData": self._auth_data(flags, att)})
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
