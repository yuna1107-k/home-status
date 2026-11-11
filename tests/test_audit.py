import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import audit


class TestAuditSecretMasking(unittest.TestCase):
    def test_ntfy_token_and_topic_masked(self):
        params = {
            "token": "tk_abcdef123456",
            "topic": "secret_topic_name",
            "server": "https://ntfy.sh",
        }
        cleaned = audit._clean(params)
        self.assertEqual(cleaned["token"], "…")
        self.assertEqual(cleaned["topic"], "…")
        self.assertEqual(cleaned["server"], "https://ntfy.sh")

    def test_webhook_url_and_auth_keys_masked(self):
        params = {
            "webhook_url": "https://example.com/webhook",
            "password": "secret_password",
            "code": "sso_code_val",
        }
        cleaned = audit._clean(params)
        self.assertEqual(cleaned["webhook_url"], "…")
        self.assertEqual(cleaned["password"], "…")
        self.assertEqual(cleaned["code"], "…")


if __name__ == "__main__":
    unittest.main()
