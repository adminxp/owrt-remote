"""Password changes end the current Hub session; only loopback and temporary state."""
import http.client
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock


SOURCE = Path(os.environ.get("OWRT_PASSWORD_LOGOUT_HUB_SOURCE", Path(__file__).resolve().parents[1] / "vps/owrt-remote-hub.py"))
STATE = tempfile.TemporaryDirectory(prefix="owrt-password-logout-tests-")
spec = importlib.util.spec_from_file_location("password_logout_hub", SOURCE)
hub = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hub
with mock.patch.dict(os.environ, {"OWRT_REMOTE_STATE_DIR": STATE.name}):
    spec.loader.exec_module(hub)


class QuietHandler(hub.Handler):
    def log_message(self, *args):
        pass


class PasswordLogoutTests(unittest.TestCase):
    def setUp(self):
        hub.save_auth_state({"username": "test-owner", "password": hub.password_digest("old-password")})
        hub.save_sessions([])
        hub.atomic_write_text(hub.SESSION_TOKEN_FILE, "legacy-session\n", mode=0o600)
        self.token, _ = hub.make_hub_session("test-owner", "127.0.0.1", "test-browser")
        self.server = hub.HubHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.server.app = hub.App(Path(STATE.name) / "hub.db", "legacy-session", "test-agent", "")
        self.server.is_tls = False
        self.push_patch = mock.patch.object(hub, "queue_web_push_payload")
        self.push_patch.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.push_patch.stop()

    def request(self, path, payload=None, token=None, secure=False):
        headers = {"Cookie": f"{hub.SESSION_COOKIE}={self.token if token is None else token}"}
        if secure:
            headers["X-Forwarded-Proto"] = "https"
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(payload)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request("POST" if payload is not None else "GET", path, body=body, headers=headers)
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    def change_payload(self, **patch):
        return {"username": "test-owner", "current_password": "old-password", "password": "new-password", "password_confirm": "new-password", **patch}

    def test_success_clears_cookie_revokes_current_session_and_accepts_new_password(self):
        status, headers, body = self.request("/api/auth", self.change_payload(), secure=True)
        self.assertEqual(status, 200)
        self.assertIn("Доступ к Hub обновлен", body.decode())
        self.assertEqual(headers["X-Hub-Reauth"], "1")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIsNone(hub.verify_hub_session(self.token, touch=False))
        self.assertEqual(self.request("/api/sessions")[0], 401)
        self.assertFalse(hub.verify_login("test-owner", "old-password"))
        self.assertTrue(hub.verify_login("test-owner", "new-password"))
        code, challenge = hub.captcha_challenge()
        status, headers, _ = self.request("/login", {
            "username": "test-owner", "password": "new-password",
            "captcha_token": challenge, "captcha_answer": code,
        }, token="")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        self.assertNotIn("Max-Age=0", headers["Set-Cookie"])

    def test_rejected_password_changes_keep_password_and_session(self):
        for patch, expected in (({"current_password": "wrong"}, 403), ({"password_confirm": "different"}, 400), ({"password": "x", "password_confirm": "x"}, 400)):
            with self.subTest(patch=patch):
                status, headers, _ = self.request("/api/auth", self.change_payload(**patch))
                self.assertEqual(status, expected)
                self.assertNotIn("X-Hub-Reauth", headers)
                self.assertNotIn("Set-Cookie", headers)
                self.assertTrue(hub.verify_login("test-owner", "old-password"))
                self.assertIsNotNone(hub.verify_hub_session(self.token, touch=False))
                self.assertEqual(self.request("/api/sessions")[0], 200)

    def test_captcha_settings_without_new_password_keep_current_session(self):
        status, headers, _ = self.request("/api/auth", {"current_password": "old-password", "captcha_mode": "digits", "password": ""})
        self.assertEqual(status, 200)
        self.assertNotIn("X-Hub-Reauth", headers)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIsNotNone(hub.verify_hub_session(self.token, touch=False))
        self.assertEqual(self.request("/api/sessions")[0], 200)

    def test_legacy_cookie_is_invalid_after_password_change_and_restart(self):
        status, headers, _ = self.request("/api/auth", self.change_payload(), token="legacy-session")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Hub-Reauth"], "1")
        self.assertNotEqual(self.server.app.session_token, "legacy-session")
        self.assertEqual(hub.session_token(), self.server.app.session_token)
        self.assertEqual(self.request("/api/sessions", token="legacy-session")[0], 401)
        # A new process reads the rotated token, so the old cookie stays invalid.
        self.server.app.session_token = hub.session_token()
        self.assertEqual(self.request("/api/sessions", token="legacy-session")[0], 401)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--export-js":
        page = hub.dashboard_html([], "test-owner", auth_bootstrap={"is_owner": True})
        output = Path(sys.argv[2])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(re.findall(r"<script>(.*?)</script>", page, re.S)), encoding="utf-8")
        print(f"JavaScript exported to {output}")
    else:
        unittest.main()
