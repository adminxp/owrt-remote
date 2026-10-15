"""WAN A-record parsing and bounded fallback; no router/network mutations.

Set OWRT_TEST_BUSYBOX to a BusyBox executable to run this exact suite in ash
with its built-in awk/grep/date applets (busybox-w32 is supported).
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUSYBOX = os.environ.get("OWRT_TEST_BUSYBOX")
SH = shutil.which("sh")
if not SH and os.name == "nt":
    candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/sh.exe"
    SH = str(candidate) if candidate.exists() else None

HEADER = "Server:\t\t8.8.8.8\nAddress:\t8.8.8.8:53\n\n"
GOOD = HEADER + "Non-authoritative answer:\nName:\tbronsvn.developer.li\nAddress: 132.243.19.232\n"
MOCKS = r'''
safe_id() { printf '%s' "$1"; }
wan_dns_server() { printf '%s' "${TEST_DNS:-8.8.8.8}"; }
log() { printf '%s\n' "$*" >&2; }
date() { printf 1800000000; }
remote_clock_ms() { printf 1000; }
nslookup() {
    printf '%s\n' "$*" >>"$TEST_ROOT/queries"
    cat "$TEST_ROOT/reply"
    cat "$TEST_ROOT/reply-stderr" >&2
    return "${TEST_RC:-0}"
}
# Resolution must not change network state or use another resolver.
ip() { printf forbidden-ip >&2; exit 90; }
nft() { printf forbidden-nft >&2; exit 91; }
uci() { printf forbidden-uci >&2; exit 92; }
'''


@unittest.skipUnless(BUSYBOX or SH, "POSIX shell required")
class WanDnsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wan-dns-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.cache = self.state / "resolve-bronsvn.developer.li-8.8.8.8"
        self.source = (ROOT / "files/usr/lib/owrt-remote/wan.sh").read_text(encoding="utf-8")

    def run_shell(self, reply=GOOD, rc=0, stderr="", commands="wan_resolve_host bronsvn.developer.li", **env):
        self.root.joinpath("reply").write_text(reply, encoding="utf-8", newline="\n")
        self.root.joinpath("reply-stderr").write_text(stderr, encoding="utf-8", newline="\n")
        script = self.root / "run.sh"
        script.write_text(self.source + '\nWAN_STATE="$TEST_ROOT/state"\n' + MOCKS + "\n" + commands + "\n",
                          encoding="utf-8", newline="\n")
        args = [BUSYBOX, "ash"] if BUSYBOX else [SH]
        process_env = dict(os.environ)
        if not BUSYBOX and os.name == "nt":
            process_env["PATH"] = str(Path(SH).parent) + os.pathsep + process_env["PATH"]
        return subprocess.run(args + [script.as_posix()], capture_output=True, encoding="utf-8",
                              env={**process_env, "TEST_ROOT": self.root.as_posix(), "TEST_RC": str(rc), **env}, timeout=15)

    def save_cache(self, timestamp=1799999699, ip="132.243.19.231"):
        self.cache.write_text(f"{timestamp} {ip}\n", encoding="ascii", newline="\n")

    def test_explicit_a_query_for_both_reported_resolvers_and_no_false_warning(self):
        for dns in ("8.8.8.8", "8.8.4.4"):
            with self.subTest(dns=dns):
                result = self.run_shell(TEST_DNS=dns)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "132.243.19.232")
                self.assertEqual(result.stderr, "")
        self.assertEqual(self.root.joinpath("queries").read_text().splitlines(),
                         [f"-type=A -timeout=3 -retry=1 bronsvn.developer.li {dns}" for dns in ("8.8.8.8", "8.8.4.4")])

    def test_busybox_and_numbered_address_formats(self):
        for address in ("Address: 132.243.19.232", "Address 1: 132.243.19.232", "Address 1: 132.243.19.232 bronsvn.developer.li"):
            with self.subTest(address=address):
                self.cache.unlink(missing_ok=True)
                result = self.run_shell(HEADER + "Name: bronsvn.developer.li\n" + address + "\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "132.243.19.232")

    def test_cname_chain_case_and_trailing_dot(self):
        reply = HEADER + "BrOnSvN.Developer.Li. canonical name = edge.example.\nedge.example canonical name = origin.example.\nName: origin.example\nAddress: 132.243.19.232\n"
        result = self.run_shell(reply)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "132.243.19.232")

    def test_host_containing_diagnostic_words_is_not_an_error(self):
        host = "timeout-servfail-refused.example"
        result = self.run_shell(GOOD.replace("bronsvn.developer.li", host),
                                commands="wan_resolve_host " + host)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "132.243.19.232")
        self.assertEqual(result.stderr, "")

    def test_unrelated_name_and_unscoped_address_are_not_answers(self):
        for reply in (HEADER + "Address: 132.243.19.232\n", HEADER + "Name: other.example\nAddress: 132.243.19.232\n"):
            with self.subTest(reply=reply):
                result = self.run_shell(reply)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.cache.exists())

    def test_no_a_record_is_distinct_from_timeout_and_parse_error(self):
        replies = ((HEADER, 0),
                   (HEADER + "Non-authoritative answer:\n", 0),
                   (HEADER + "*** Can't find bronsvn.developer.li: No answer\n", 0),
                   (HEADER + "** server can't find bronsvn.developer.li: NXDOMAIN\n", 1),
                   (HEADER + "Name: bronsvn.developer.li\nAddress: 2001:db8::1\n", 0))
        for reply, rc in replies:
            with self.subTest(reply=reply):
                result = self.run_shell(reply, rc)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("reason=no_a_record", result.stderr)

    def test_dns_timeout_stderr_and_original_return_code_are_preserved(self):
        result = self.run_shell("", 1, ";; connection timed out; no servers could be reached\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=timeout rc=1", result.stderr)

    def test_malformed_empty_and_invalid_ipv4_are_parse_errors(self):
        for reply in ("", "unexpected output\n", HEADER + "*** Can't find bronsvn.developer.li: Parse error\n",
                      *(HEADER + f"Name: bronsvn.developer.li\nAddress: {ip}\n" for ip in
                        ("999.243.19.232", "132.243.19", "132.243.19.232.evil", "0132.243.19.232", "", "-1.2.3.4"))):
            with self.subTest(reply=reply):
                result = self.run_shell(reply)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("reason=parse_error", result.stderr)

    def test_nonzero_nslookup_is_not_success_even_with_valid_looking_answer(self):
        result = self.run_shell(GOOD, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("reason=resolver_error", result.stderr)
        self.assertFalse(self.cache.exists())

    def test_servfail_is_resolver_error(self):
        result = self.run_shell(HEADER + "** server can't find bronsvn.developer.li: SERVFAIL\n", 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=resolver_error", result.stderr)

    def test_fake_ip_is_rejected_and_later_valid_a_is_used(self):
        for ip in ("198.18.0.1", "198.19.255.255"):
            result = self.run_shell(GOOD.replace("132.243.19.232", ip))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reason=rejected_address", result.stderr)
        result = self.run_shell(GOOD.replace("132.243.19.232", "198.18.0.1") + "Address: 132.243.19.232\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "132.243.19.232")
        self.assertEqual(result.stderr, "")

    def test_success_refreshes_stale_cache_without_warning(self):
        self.save_cache()
        result = self.run_shell()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.cache.read_text(), "1800000000 132.243.19.232\n")

    def test_recent_valid_cache_skips_lookup(self):
        self.save_cache(1799999701)
        result = self.run_shell("", 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "132.243.19.231")
        self.assertFalse(self.root.joinpath("queries").exists())

    def test_last_success_fallback_preserves_age_and_logs_exact_failure(self):
        for reply, rc, reason in ((";; connection timed out\n", 1, "timeout"),
                                  (HEADER, 0, "no_a_record"), ("broken reply", 0, "parse_error")):
            with self.subTest(reason=reason):
                self.save_cache(1799996401)
                before = self.cache.read_bytes()
                result = self.run_shell(reply, rc)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "132.243.19.231")
                self.assertIn("WAN DNS unavailable", result.stderr)
                self.assertIn("reason=" + reason, result.stderr)
                self.assertEqual(self.cache.read_bytes(), before)

    def test_expired_corrupt_future_and_ipv6_cache_are_never_fallback(self):
        for timestamp, ip in ((1799996400, "132.243.19.231"), (1800000001, "132.243.19.231"),
                              ("invalid", "132.243.19.231"), ("08", "132.243.19.231"),
                              ("999999999999999999999999", "132.243.19.231"),
                              (1799999999, "2001:db8::1"), (1799999999, "999.1.1.1"),
                              (1799999999, "198.18.0.1"), (1799999999, "198.19.255.255")):
            with self.subTest(timestamp=timestamp, ip=ip):
                self.save_cache(timestamp, ip)
                result = self.run_shell(HEADER, 0)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("reason=no_a_record", result.stderr)

    def test_cache_metrics_still_distinguish_actual_lookup(self):
        result = self.run_shell(commands=r'''
OWRT_REMOTE_DNS_METRICS="$TEST_ROOT/metrics"
wan_resolve_host bronsvn.developer.li || exit
cat "$TEST_ROOT/metrics"
wan_resolve_host bronsvn.developer.li || exit
cat "$TEST_ROOT/metrics"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 lookup", result.stdout)
        self.assertIn("0 cache", result.stdout)

    def test_shell_syntax(self):
        script = self.root / "wan.sh"
        script.write_text(self.source, encoding="utf-8", newline="\n")
        args = [BUSYBOX, "ash"] if BUSYBOX else [SH]
        result = subprocess.run(args + ["-n", script.as_posix()], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
