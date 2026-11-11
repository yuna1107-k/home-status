import io
import unittest
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# On macOS /proc does not exist; mock sampler before importing actions/server
mock_sampler = MagicMock()
mock_sampler.NEXTCLOUD_COMPOSE_PROJECT = "nextcloud"
mock_sampler.SAMPLER = MagicMock()
mock_sampler.NET_IFACE = "eth0"
sys.modules["sampler"] = mock_sampler

import actions
import authdb
import server
import settings


class TestSecurityFixes(unittest.TestCase):
    def test_handler_timeouts(self):
        self.assertEqual(server.BaseHandler.timeout, 15)
        self.assertEqual(server.RedirectHandler.timeout, 15)
        self.assertEqual(server.AppHandler.timeout, 15)

    def test_read_json_content_length_validation(self):
        handler = server.AppHandler.__new__(server.AppHandler)
        handler.headers = {"X-Requested-With": "home-status", "Content-Type": "application/json"}
        sent_json = []
        handler._json = lambda code, data, **kw: sent_json.append((code, data))

        # Negative Content-Length
        handler.headers["Content-Length"] = "-1"
        self.assertIsNone(handler._read_json())
        self.assertEqual(sent_json[-1][0], 400)

        # Non-numeric Content-Length
        handler.headers["Content-Length"] = "invalid"
        self.assertIsNone(handler._read_json())
        self.assertEqual(sent_json[-1][0], 400)

        # Exceeding MAX_BODY
        handler.headers["Content-Length"] = str(server.MAX_BODY + 1)
        self.assertIsNone(handler._read_json())
        self.assertEqual(sent_json[-1][0], 413)

        # Valid Content-Length with JSON
        payload = b'{"hello": "world"}'
        handler.headers["Content-Length"] = str(len(payload))
        handler.rfile = io.BytesIO(payload)
        res = handler._read_json()
        self.assertEqual(res, {"hello": "world"})

    def test_tailscale_serve_allowed_ports(self):
        # Default allowed ports in settings
        self.assertEqual(settings.get()["allowed_serve_ports"], [8080, 8092])

        # Disallow exposing arbitrary ports like 22 or 3306
        with self.assertRaises(actions.ActionError) as ctx:
            actions.tailscale_serve_add({"port": 9000, "target": 22})
        self.assertIn("許可されていません", str(ctx.exception))

        # Allowed port (8080 or 8092) should pass check and call _run
        with patch.object(actions, "_run") as mock_run:
            res = actions.tailscale_serve_add({"port": 9000, "target": 8080})
            self.assertIn("転送するようにしました", res)
            mock_run.assert_called_once_with("tailscale", "serve", "--bg", "--https=9000", "http://127.0.0.1:8080")

    def test_consume_oauth_code_atomic(self):
        code_hash = "abc123hash"
        redirect_uri = "https://example.com/auth/sso/callback"

        # When code exists and is deleted returning user_id
        with patch.object(authdb, "_query") as mock_q:
            mock_q.side_effect = [
                [{"user_id": "usr_test"}],  # DELETE ... RETURNING user_id
                [{"user_id": "usr_test", "username": "yuna", "user_status": "active", "is_enabled": 1}],  # user lookup
            ]
            user = authdb.consume_oauth_code(code_hash, redirect_uri)
            self.assertIsNotNone(user)
            self.assertEqual(user["username"], "yuna")
            first_sql = mock_q.call_args_list[0][0][0]
            self.assertIn("DELETE FROM oauth_codes", first_sql)
            self.assertIn("RETURNING user_id", first_sql)

        # When code is already consumed or expired
        with patch.object(authdb, "_query", return_value=[]):
            user = authdb.consume_oauth_code(code_hash, redirect_uri)
            self.assertIsNone(user)


if __name__ == "__main__":
    unittest.main()
