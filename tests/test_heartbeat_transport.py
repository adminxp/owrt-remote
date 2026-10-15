"""HTTPS retry policy, origin-scoped auth, timings and real curl redirects."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading
import unittest

import test_host_policy as shell_tests

ROOT, SH = shell_tests.ROOT, shell_tests.SH


@unittest.skipUnless(SH, "POSIX shell required")
class HeartbeatTransportTests(unittest.TestCase):
    setUp = shell_tests.RouterShellTests.setUp
    config = shell_tests.RouterShellTests.config
    run_shell = shell_tests.RouterShellTests.run_shell

    def policy(self, commands, **env):
        self.config(hub_url="https://primary.example", public_url="https://backup.example",
                    vps_host="192.0.2.7", hub_token="never-log-this-token")
        self.directory.joinpath("calls").write_text("")
        return self.run_shell(r'''
heartbeat_json() { printf '{"id":"test-router"}'; }
sleep() { printf 'sleep %s\n' "$*" >>"$TEST_ROOT/calls"; }
''' + commands, **env)

    def test_one_timeout_retries_https_without_http_or_setting_changes(self):
        result = self.policy(r'''
post_json() {
    printf '%s\n' "$1" >>"$TEST_ROOT/calls"
    if [ ! -f "$TEST_ROOT/tried" ]; then touch "$TEST_ROOT/tried"; return 28; fi
    printf '{"ok":true}'
}
send_heartbeat >/dev/null
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.directory.joinpath("calls").read_text().splitlines(),
                         ["https://primary.example/api/heartbeat", "sleep 1", "https://primary.example/api/heartbeat"])
        self.assertIn("retry HTTPS", result.stderr)
        self.assertNotIn("fallback;", result.stderr)
        self.assertNotIn("never-log-this-token", result.stderr)
        self.assertFalse(self.directory.joinpath("commits").exists())
        self.assertEqual(self.directory.joinpath("owrt-remote-heartbeat-last").read_text().split("reason=")[1].split(";")[0], "ok")

    def test_exhausted_https_uses_secure_backup_and_explains_reason(self):
        result = self.policy(r'''
post_json() {
    printf '%s\n' "$1" >>"$TEST_ROOT/calls"
    case "$1" in https://backup.example/*) printf '{"ok":true}' ;; *) return 28 ;; esac
}
send_heartbeat >/dev/null
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.directory.joinpath("calls").read_text()
        self.assertEqual(calls.count("https://primary.example/api/heartbeat"), 2)
        self.assertIn("https://backup.example/api/heartbeat", calls)
        self.assertNotIn("http://", calls)
        self.assertIn("fallback; from=https://primary.example:443; to=https://backup.example:443; reason=timeout", result.stderr)

    def test_https_never_uses_plain_http_even_after_all_retries(self):
        result = self.policy(r'''
post_json() { printf '%s\n' "$1" >>"$TEST_ROOT/calls"; return 28; }
send_heartbeat
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("http://", self.directory.joinpath("calls").read_text())
        self.assertIn("reason=https_required", result.stderr)

    def test_401_does_not_retry_or_change_endpoint_and_never_logs_echoed_secrets(self):
        result = self.policy(r'''
post_json() {
    printf '%s\n' "$1" >>"$TEST_ROOT/calls"
    printf 'tool=curl HTTP=401 DNS_ms=1 TCP_ms=2 TLS_ms=3\n' >"$OWRT_REMOTE_HTTP_METRICS"
    printf 'Authorization: Bearer never-log-this-token\n' >&2
    printf '{"hub_token":"never-log-this-token"}'
    return 22
}
send_heartbeat
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.directory.joinpath("calls").read_text().splitlines(), ["https://primary.example/api/heartbeat"])
        self.assertIn("reason=auth_rejected", result.stderr)
        self.assertIn("HTTP=401", result.stderr)
        self.assertNotIn("never-log-this-token", result.stdout + result.stderr)

    def test_http_only_configuration_still_works_and_origin_removes_secrets(self):
        self.config(hub_url="http://plain.example:8088", public_url="http://plain.example:8088", hub_token="test-token")
        result = self.run_shell(r'''
heartbeat_json() { printf '{}'; }
post_json() { printf '{"ok":true}'; }
send_heartbeat >/dev/null
remote_url_origin 'https://hub.example/path?token=secret#secret'
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "https://hub.example:443")
        self.assertNotIn("secret", result.stderr)

    def test_certificate_error_is_not_retried_and_never_uses_http(self):
        result = self.policy(r'''
post_json() { printf '%s\n' "$1" >>"$TEST_ROOT/calls"; return 60; }
send_heartbeat
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        calls = self.directory.joinpath("calls").read_text()
        self.assertEqual(calls.count("https://primary.example/api/heartbeat"), 1)
        self.assertNotIn("sleep", calls)
        self.assertNotIn("http://", calls)
        self.assertIn("reason=tls_verification_failed", result.stderr)

    def test_timing_deltas_and_safe_fields_only(self):
        self.directory.joinpath("stats").write_text("200 0.010 0.030 0.080 0.100 0.120 https://evil.example/?token=secret\n")
        result = self.run_shell(r'''
OWRT_REMOTE_HTTP_METRICS="$TEST_ROOT/metrics"
remote_curl_metrics "$TEST_ROOT/stats" https 15 1
cat "$TEST_ROOT/metrics"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DNS_ms=10.0 TCP_ms=20.0 TLS_ms=50.0", result.stdout)
        self.assertIn("WAN_resolve_ms=15 hop=1", result.stdout)
        self.assertNotIn("secret", result.stdout)
        self.assertNotIn("evil", result.stdout)

    def test_wan_dns_timing_distinguishes_lookup_cache_and_pinned_host(self):
        self.config(vps_host="hub.example", vps_host_mode="manual")
        self.directory.joinpath("stats").write_text("200 0.000 0.030 0.080 0.100 0.120\n")
        result = self.run_shell(r'''
OWRT_REMOTE_HTTP_METRICS="$TEST_ROOT/metrics"
OWRT_REMOTE_DNS_METRICS="$TEST_ROOT/dns-time"
wan_resolve_host hub.example >/dev/null || exit
read -r duration source <"$TEST_ROOT/dns-time"
[ "$source" = lookup ] || exit 81
wan_resolve_host hub.example >/dev/null || exit
read -r duration source <"$TEST_ROOT/dns-time"
[ "$duration $source" = '0 cache' ] || exit 82
remote_curl_metrics "$TEST_ROOT/stats" https 15 0 "$duration" "$source"
uci set owrtremote.main.vps_host=192.0.2.1
wan_endpoint_ip https://hub.example/api/heartbeat >/dev/null || exit
read -r duration source <"$TEST_ROOT/dns-time"
[ "$duration $source" = '0 pinned' ] || exit 83
remote_curl_metrics "$TEST_ROOT/stats" https 15 0 "$duration" "$source"
cat "$TEST_ROOT/metrics"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DNS_ms=0.0", result.stdout)
        self.assertIn("DNS_source=cache", result.stdout)
        self.assertIn("DNS_source=pinned", result.stdout)


@unittest.skipUnless(SH, "POSIX shell required")
class CurlRedirectTests(unittest.TestCase):
    setUp = shell_tests.RouterShellTests.setUp
    config = shell_tests.RouterShellTests.config
    run_shell = shell_tests.RouterShellTests.run_shell

    def prepare(self, secure_front=False):
        if os.name == "nt":
            bindir = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/mingw64/bin"
            self.curl = bindir / "curl.exe"
            openssl = bindir / "openssl.exe"
        else:
            self.curl = Path(shutil.which("curl") or "/nonexistent")
            openssl = Path(shutil.which("openssl") or "/nonexistent")
        if not self.curl.exists() or not openssl.exists():
            self.skipTest("real curl and OpenSSL required")
        cert = self.directory / "cert.pem"
        key = self.directory / "key.pem"
        subprocess.run([str(openssl), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        self.env.update(TEST_CURL=self.curl.as_posix(), TEST_CA_CERT=cert.as_posix())
        self.directory.joinpath("payload").write_bytes(b'{"id":"test-router"}')
        self.config(wan_direct="0")
        self.records = []

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.records.append((self.server.server_port, self.path, self.headers.get("Authorization"), body))
                if self.server is owner.front and self.path == "/api/heartbeat":
                    self.send_response(owner.redirect_code)
                    self.send_header("Location", owner.destination)
                    response = b""
                else:
                    self.send_response(200 if self.headers.get("Authorization") == "Bearer test-token" else 401)
                    response = b'{"ok":true}'
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.back = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.back.socket = context.wrap_socket(self.back.socket, server_side=True)
        self.front = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if secure_front:
            self.front.socket = context.wrap_socket(self.front.socket, server_side=True)
        for server in (self.front, self.back):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.redirect_code = 301
        self.destination = f"https://localhost:{self.back.server_port}/api/heartbeat"
        self.url = f"{'https' if secure_front else 'http'}://localhost:{self.front.server_port}/api/heartbeat"
        # shell_tests supplies a mock curl; replace only the executable binding.
        self.real = r'''
curl() { "$TEST_CURL" -q --cacert "$TEST_CA_CERT" "$@"; }
OWRT_REMOTE_HTTP_METRICS="$TEST_ROOT/metrics"
export OWRT_REMOTE_HTTP_METRICS
'''

    def test_real_curl_reproduces_401_then_upgrade_keeps_json_and_auth(self):
        self.prepare()
        old = subprocess.run([str(self.curl), "-q", "--cacert", self.env["TEST_CA_CERT"], "-fsSL", "--noproxy", "*", "--max-time", "5",
                              "--post301", "-H", "Authorization: Bearer test-token", "-H", "Content-Type: application/json",
                              "--data-binary", "@" + str(self.directory / "payload"), self.url],
                             env=self.env, capture_output=True)
        self.assertEqual(old.returncode, 22, old.stderr)
        self.assertEqual(self.records[-1][2], None, "curl drops auth across HTTP -> HTTPS")
        self.records.clear()
        result = self.run_shell(self.real + 'post_json "$TEST_URL" test-token "$TEST_ROOT/payload"', TEST_URL=self.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual([x[2] for x in self.records], ["Bearer test-token"] * 2)
        self.assertTrue(all(x[3] == b'{"id":"test-router"}' for x in self.records))
        self.assertEqual(len(self.directory.joinpath("metrics").read_text().splitlines()), 2)
        self.assertFalse(list(self.directory.glob("owrt-remote-http.*")))

    def test_rejects_cross_host_without_forwarding_token(self):
        self.prepare()
        self.destination = f"https://127.0.0.1:{self.back.server_port}/api/heartbeat?token=secret"
        result = self.run_shell(self.real + 'post_json "$TEST_URL" test-token "$TEST_ROOT/payload"', TEST_URL=self.url)
        self.assertEqual(result.returncode, 47, result.stderr)
        self.assertEqual(len(self.records), 1)
        self.assertNotIn("secret", self.directory.joinpath("metrics").read_text())

    def test_rejects_tls_downgrade_without_forwarding_token(self):
        self.prepare(secure_front=True)
        self.destination = self.url.replace("https://", "http://")
        result = self.run_shell(self.real + 'post_json "$TEST_URL" test-token "$TEST_ROOT/payload"', TEST_URL=self.url)
        self.assertEqual(result.returncode, 47, result.stderr)
        self.assertEqual(len(self.records), 1)

    def test_same_origin_relative_redirect_preserves_auth_and_post(self):
        self.prepare(secure_front=True)
        self.destination = "/redirected"
        self.redirect_code = 307
        result = self.run_shell(self.real + 'post_json "$TEST_URL" test-token "$TEST_ROOT/payload"', TEST_URL=self.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual([x[1] for x in self.records], ["/api/heartbeat", "/redirected"])
        self.assertEqual([x[2] for x in self.records], ["Bearer test-token"] * 2)

    def test_repeated_redirects_are_bounded(self):
        self.prepare()
        self.destination = self.url
        result = self.run_shell(self.real + 'post_json "$TEST_URL" test-token "$TEST_ROOT/payload"', TEST_URL=self.url)
        self.assertEqual(result.returncode, 47, result.stderr)
        self.assertEqual(len(self.records), 4)

    def test_wan_redirect_resolves_and_binds_both_hops(self):
        self.prepare()
        result = self.run_shell(self.real + r'''
wan_http_target() {
    local port
    printf '%s\n' "$1" >>"$TEST_ROOT/targets"
    port="${1#*localhost:}"; port="${port%%/*}"
    printf '127.0.0.1|localhost:%s:127.0.0.1' "$port"
}
wan_post_json "$TEST_URL" test-token "$TEST_ROOT/payload"
''', TEST_URL=self.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.directory.joinpath("targets").read_text().splitlines(), [self.url, self.destination])
        self.assertEqual([x[2] for x in self.records], ["Bearer test-token"] * 2)


if __name__ == "__main__":
    unittest.main()
