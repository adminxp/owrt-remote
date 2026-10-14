"""Exercise router delivery through the real helper and agent HTTP selection."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_global_logs as journal_tests

ROOT, SH = journal_tests.ROOT, journal_tests.SH


@unittest.skipUnless(SH, 'POSIX shell required')
class RouterLogTransportTests(unittest.TestCase):
    def setUp(self):
        self.journal = journal_tests.GlobalLogTests()
        self.journal.setUp()
        self.addCleanup(self.journal.doCleanups)
        self.journal.helper_call('append', 'Before disconnect\n')
        self.source = (ROOT / 'files/usr/sbin/owrt-remote').read_text(encoding='utf-8').split('cmd="${1:-help}"')[0]
        self.env = {**self.journal.env, 'OWRT_REMOTE_WAN_HELPER': (ROOT / 'files/usr/lib/owrt-remote/wan.sh').as_posix(),
                    'TEST_ROOT': self.journal.root.as_posix(), 'TEST_PYTHON': Path(sys.executable).as_posix()}
        self.mocks = r'''
uci_get() {
    case "$1" in
        enabled) printf '%s' "${TEST_ENABLED:-1}" ;; hub_token) printf test-token ;; router_id) printf main ;;
        wan_direct) printf '%s' "${TEST_DIRECT:-0}" ;; *) printf '%s' "$2" ;;
    esac
}
heartbeat_candidate_endpoints() { printf 'http://unavailable/api/heartbeat\nhttps://available/api/heartbeat\n'; }
wan_prepare() { echo prepare >>"$TEST_ROOT/requests"; [ "${TEST_WAN_FAIL:-0}" != 1 ]; }
xray_running() { [ "${TEST_XRAY_RUNNING:-0}" = 1 ]; }
wan_post_json() { echo direct >>"$TEST_ROOT/requests"; fake_post "$1" "$3"; }
curl() {
    local file arg next url
    next=0; file=''
    for arg in "$@"; do
        [ "$next" != 1 ] || { file="${arg#@}"; next=0; }
        [ "$arg" != --data-binary ] || next=1
        url="$arg"
    done
    fake_post "$url" "$file"
}
fake_post() {
    echo "$1" >>"$TEST_ROOT/requests"
    [ "${TEST_HTTP_FAIL:-0}" != 1 ] || return 22
    case "$1" in http://unavailable/*) return 22 ;; esac
    cp "$2" "$TEST_ROOT/delivered.json"
    "$TEST_PYTHON" -c 'import json,sys; b=json.load(open(sys.argv[1]))["batch"]; print(json.dumps({"ok":True,"journal_id":b["journal_id"],"offset":b["to"],"clear_request":""}))' "$2"
}
jsonfilter() {
    "$TEST_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2][2:],""))' "$2" "$4"
}
'''

    def sync(self, **flags):
        script = self.source + '\n' + self.mocks + '\nsync_global_logs\n'
        return subprocess.run([SH], input=script, encoding='utf-8', capture_output=True,
                              env={**self.env, **flags}, timeout=25)

    def test_fallback_and_direct_wan_ack_only_after_successful_delivery(self):
        for direct in ('0', '1'):
            with self.subTest(direct=direct):
                self.journal.helper_call('clear')
                self.journal.helper_call('append', 'Before disconnect\n')
                (self.journal.root / 'requests').write_text('')
                result = self.sync(TEST_DIRECT=direct)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads((self.journal.root / 'delivered.json').read_text())
                self.assertEqual(payload['id'], 'main')
                self.assertEqual(base64.b64decode(payload['batch']['data']), b'[OK] Before disconnect\n')
                requests = (self.journal.root / 'requests').read_text()
                self.assertIn('https://available/api/router-logs', requests)
                self.assertEqual('direct' in requests, direct == '1')
                self.assertEqual('prepare' in requests, direct == '1')
                self.assertEqual(json.loads(self.journal.helper_call('export'))['data'], '')
                self.assertFalse(list(self.journal.log_dir.glob('sync.*')))

    def test_failed_delivery_preserves_unsent_records_and_removes_payload_files(self):
        before = json.loads(self.journal.helper_call('export'))
        result = self.sync(TEST_HTTP_FAIL='1')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(self.journal.helper_call('export')), before)
        self.assertFalse(list(self.journal.log_dir.glob('sync.*')))

    def test_disabled_remote_still_delivers_stop_reason_and_state_without_restart(self):
        self.journal.helper_call('append', 'Сервис: остановка туннеля\n')
        for direct in ('0', '1'):
            with self.subTest(direct=direct):
                result = self.sync(TEST_ENABLED='0', TEST_DIRECT=direct)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads((self.journal.root / 'delivered.json').read_text())
                self.assertEqual(payload['state'], {'enabled': False, 'xray': 'stopped'})
                self.assertFalse(list(self.journal.log_dir.glob('sync.*')))
        self.assertEqual(json.loads(self.journal.helper_call('export'))['data'], '')

    def test_overlapping_delivery_does_not_start_second_sender(self):
        lock = self.journal.log_dir / 'sync-lock'
        lock.mkdir()
        # The shell's own PID is alive for the duration of sync_global_logs.
        script = self.source + '\n' + self.mocks + '\nprintf "%s" "$$" >"$OWRT_REMOTE_LOG_DIR/sync-lock/pid"\nsync_global_logs\n'
        result = subprocess.run([SH], input=script, encoding='utf-8', capture_output=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.journal.root / 'requests').exists())
        self.assertNotEqual(json.loads(self.journal.helper_call('export'))['data'], '')

    def test_wan_failure_releases_delivery_lock_for_next_attempt(self):
        result = self.sync(TEST_DIRECT='1', TEST_WAN_FAIL='1', TEST_ENABLED='0')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.journal.log_dir / 'sync-lock').exists())
        self.assertNotEqual(json.loads(self.journal.helper_call('export'))['data'], '')
        self.assertEqual(self.sync(TEST_DIRECT='1', TEST_ENABLED='0').returncode, 0)

    def test_abandoned_empty_delivery_lock_is_recovered(self):
        (self.journal.log_dir / 'sync-lock').mkdir()
        result = self.sync(TEST_ENABLED='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.journal.log_dir / 'sync-lock').exists())
        self.assertTrue((self.journal.root / 'delivered.json').exists())

    def test_panel_stop_sends_completed_result_without_waiting_for_restart(self):
        # Exercise the CGI action and real journal; replace only router services.
        source = (ROOT / 'files/www/cgi-bin/owrt-remote').read_text(encoding='utf-8').split('\nread_request\n')[0]
        source = source.replace('/etc/init.d/owrt-remote', 'mock_service')
        agent = self.journal.root / 'mock-agent'
        agent.write_text('''#!/bin/sh
if [ "$1" = logs-sync ]; then
    sh "$OWRT_REMOTE_LOG_HELPER" read >"$TEST_ROOT/stop-delivery"
    cat "$TEST_ROOT/enabled" >>"$TEST_ROOT/stop-delivery"
fi
''', encoding='utf-8', newline='\n')
        agent.chmod(0o755)
        source += r'''
uci_set() { [ "$1" != enabled ] || printf '%s' "$2" >"$TEST_ROOT/enabled"; }
uci() { return 0; }
mock_service() { return 0; }
run_action stop
wait
'''
        result = subprocess.run([SH], input=source, encoding='utf-8', capture_output=True,
                                env={**self.env, 'AGENT': agent.as_posix()}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        delivered = (self.journal.root / 'stop-delivery').read_text(encoding='utf-8')
        self.assertIn('Сервис остановлен; результат=warn', delivered)
        self.assertTrue(delivered.endswith('0'), 'send runs after enabled=0 and the stop result is journaled')


if __name__ == '__main__':
    unittest.main()
