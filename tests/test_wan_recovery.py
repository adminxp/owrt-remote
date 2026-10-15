"""WAN outage/recovery using the real agent shell and a simulated procd."""
import json
import os
import subprocess
import unittest

import test_host_policy as base
from test_wan_failover import UPLINK_MOCKS, JSONFILTER


@unittest.skipUnless(base.SH, "POSIX shell required")
class WanRecoveryTests(unittest.TestCase):
    setUp = base.RouterShellTests.setUp
    config = base.RouterShellTests.config

    def shell(self, commands):
        shell = os.environ.get("OWRT_RECOVERY_TEST_SHELL", base.SH)
        args = [shell, "ash"] if "busybox" in shell.lower() else [shell]
        return subprocess.run(args, input=self.agent + "\n" + base.MOCKS + "\n" + UPLINK_MOCKS + "\n" + commands,
                              encoding="utf-8", capture_output=True, cwd=base.ROOT, env=self.env, timeout=40)

    def prepare(self, enabled="1"):
        self.config(enabled=enabled, wan_direct="1", wan_interface="wan", vps_host="192.0.2.10",
                    hub_url="https://hub.example", hub_token="secret-test-token")
        self.directory.joinpath("jsonfilter.py").write_text(JSONFILTER, encoding="utf-8")
        self.directory.joinpath("service.json").write_text(json.dumps({"owrt-remote": {"instances": {
            "heartbeat": {"running": True, "pid": 123}, "xray": {"running": False}
        }}}), encoding="utf-8")

    PROCD = r'''
xray_running() { return 1; }
ubus() {
    case "$1 $2 $3" in
        'call service list') cat "$TEST_ROOT/service.json" ;;
        'call service add')
            printf '%s\n' "$4" >>"$TEST_ROOT/add-calls"
            [ ! -e "$TEST_ROOT/add-fail" ] ;;
        *) return 1 ;;
    esac
}
jsonfilter() {
    "$TEST_PYTHON" -c 'import json,sys
state=json.load(sys.stdin)
instance="heartbeat" if ".heartbeat." in sys.argv[1] else "xray"
value=state.get("owrt-remote",{}).get("instances",{}).get(instance,{}).get("running","")
print(str(value).lower())' "$2"
}
wan_select_interface() { [ ! -e "$TEST_ROOT/wan-down" ] || return 1; printf wan; }
wan_device() { [ ! -e "$TEST_ROOT/wan-down" ] || return 1; printf eth0; }
'''

    def test_online_heartbeat_after_outage_recovers_dead_tunnel_on_same_uplink_once(self):
        self.prepare()
        result = self.shell(self.PROCD + r'''
touch "$TEST_ROOT/wan-down"
wan_reconcile_xray || true
test ! -e "$TEST_ROOT/add-calls" || exit 1
rm "$TEST_ROOT/wan-down"
wan_reconcile_xray || exit
wan_reconcile_xray
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = self.directory.joinpath("add-calls").read_text().splitlines()
        self.assertEqual(len(requests), 1)
        request = json.loads(requests[0])
        self.assertEqual(set(request["instances"]), {"xray"})
        self.assertEqual(request["instances"]["xray"]["command"], ["/usr/sbin/owrt-remote", "run"])
        self.assertEqual(request["instances"]["xray"]["respawn"], ["3600", "5", "5"])
        self.assertFalse(self.directory.joinpath("owrt-remote-xray-recovery").exists())
        self.assertNotIn("secret-test-token", result.stderr)

    def test_initial_start_waiting_for_wan_is_left_to_procd(self):
        self.prepare()
        state = self.directory / "service.json"
        data = json.loads(state.read_text())
        data["owrt-remote"]["instances"]["xray"]["running"] = True
        state.write_text(json.dumps(data))
        result = self.shell(self.PROCD + "wan_reconcile_xray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.directory.joinpath("add-calls").exists())

    def test_successful_heartbeat_recovers_dead_xray_and_still_returns_hub_response(self):
        self.prepare()
        result = self.shell(self.PROCD + r'''
wan_prepare() { return 0; }
heartbeat_json() { printf '{}'; }
heartbeat_candidate_endpoints() { printf 'https://hub.example/api/heartbeat'; }
post_json() { printf '{"ok":true}'; }
heartbeat_apply_hub_update() { return 0; }
send_heartbeat
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True})
        request = json.loads(self.directory.joinpath("add-calls").read_text())
        self.assertEqual(set(request["instances"]), {"xray"})
        self.assertIn("Heartbeat: OK", result.stderr)

    def test_healthy_xray_is_not_restarted_when_wan_returns(self):
        self.prepare()
        result = self.shell(self.PROCD + "xray_running() { return 0; }; wan_reconcile_xray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.directory.joinpath("add-calls").exists())

    def test_panel_stop_and_external_service_stop_are_respected(self):
        self.prepare(enabled="0")
        result = self.shell(self.PROCD + "wan_reconcile_xray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.directory.joinpath("add-calls").exists())
        self.prepare()
        self.directory.joinpath("service.json").write_text('{}')
        result = self.shell(self.PROCD + "wan_reconcile_xray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.directory.joinpath("add-calls").exists())

    def test_parallel_requests_and_stale_lock_produce_one_recovery(self):
        self.prepare()
        lock = self.directory / "owrt-remote-xray-recovery"
        lock.mkdir()
        lock.joinpath("pid").write_text("2147483000")
        result = self.shell(self.PROCD + "wan_reconcile_xray & wan_reconcile_xray & wait")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.directory.joinpath("add-calls").read_text().splitlines()), 1)
        self.assertFalse(lock.exists())

    def test_ubus_failure_reports_error_cleans_lock_and_honors_backoff(self):
        self.prepare()
        self.directory.joinpath("add-fail").touch()
        result = self.shell(self.PROCD + "wan_reconcile_xray; rc=$?; wan_reconcile_xray; exit $rc")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("procd", result.stderr)
        self.assertEqual(len(self.directory.joinpath("add-calls").read_text().splitlines()), 1)
        self.assertFalse(self.directory.joinpath("owrt-remote-xray-recovery").exists())

    def test_empty_lock_and_dead_reaper_do_not_block_later_recovery(self):
        self.prepare()
        lock = self.directory / "owrt-remote-xray-recovery"
        lock.mkdir()
        (lock / "reap").mkdir()
        (lock / "reap/pid").write_text("2147483000")
        result = self.shell(self.PROCD + "wan_reconcile_xray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.directory.joinpath("add-calls").read_text().splitlines()), 1)
        self.assertFalse(lock.exists())

    def test_missing_wan_does_not_cause_repeated_service_restarts(self):
        self.prepare()
        self.directory.joinpath("wan-down").touch()
        result = self.shell(self.PROCD + r'''
active_ssh_session() { return 1; }
heartbeat_self_heal; heartbeat_self_heal; heartbeat_self_heal; heartbeat_self_heal
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.directory.joinpath("owrt-remote-heartbeat.restart").exists())
        self.assertIn("IPv4 default route", result.stderr)

    def test_supervised_start_waits_beyond_six_retries_then_launches_with_valid_config(self):
        self.prepare()
        self.directory.joinpath("wan-down").touch()
        executable = self.directory / "fake-xray"
        executable.write_text('#!/bin/sh\n[ "$1" != help ] || { echo run; exit; }; printf "%s\\n" "$*" >"$TEST_ROOT/xray-called"\n', newline="\n")
        executable.chmod(0o755)
        config = self.directory / "valid-config.json"
        config.write_text('{"outbounds":[]}')
        result = self.shell(r'''
sleep() {
    local n
    n=$(cat "$TEST_ROOT/waits" 2>/dev/null || echo 0)
    n=$((n+1)); printf '%s' "$n" >"$TEST_ROOT/waits"
    [ "$n" -lt 8 ] || rm -f "$TEST_ROOT/wan-down"
}
ensure_xray_available_with_retry() { return 0; }
xray_bin_path() { printf '%s' "$TEST_ROOT/fake-xray"; }
render_client_config() { printf '%s' "$TEST_ROOT/valid-config.json"; }
run_xray
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.directory.joinpath("waits").read_text(), "8")
        self.assertEqual(self.directory.joinpath("xray-called").read_text().strip(), 'run -config ' + config.as_posix())
        self.assertEqual(result.stderr.count("конфигурация сохранена"), 1)
        self.assertIn("продолжается запуск", result.stderr)

    def test_disabling_service_cancels_wait_without_rewriting_configuration(self):
        self.prepare()
        self.directory.joinpath("wan-down").touch()
        result = self.shell(r'''
sleep() { uci set owrtremote.main.enabled=0; }
render_client_config() { touch "$TEST_ROOT/rendered"; }
run_xray
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.directory.joinpath("rendered").exists())

    def test_configuration_error_with_available_wan_is_not_retried_as_wan_outage(self):
        self.prepare()
        result = self.shell(r'''
sleep() { touch "$TEST_ROOT/slept"; }
ensure_xray_available_with_retry() { return 0; }
xray_bin_path() { printf /usr/bin/xray; }
render_client_config() { printf 'invalid configuration\n' >&2; return 7; }
run_xray
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("invalid configuration", result.stderr)
        self.assertFalse(self.directory.joinpath("slept").exists())


if __name__ == "__main__":
    unittest.main()
