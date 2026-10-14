"""Exercise the real RAM journal and authorized router CGI with temporary files."""
import concurrent.futures
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
GIT_BIN = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/usr/bin'
SH = str(GIT_BIN / 'sh.exe') if os.name == 'nt' else shutil.which('sh')


@unittest.skipUnless(SH, 'POSIX shell required')
class GlobalLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='global-log-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_dir = self.root / 'journal'
        self.helper = self.root / 'logs.sh'
        source = (ROOT / 'files/usr/lib/owrt-remote/logs.sh').read_text(encoding='utf-8')
        source = source.replace('log_clock() { cut -d. -f1 /proc/uptime; }',
                                'log_clock() { echo "${TEST_UPTIME:-1000}"; }')
        self.helper_source = source
        source = source.replace('log_ram_storage() {', 'log_ram_storage() { return 0;', 1)
        self.helper.write_text(source, encoding='utf-8', newline='\n')
        self.cgi = self.root / 'cgi'
        self.cgi.write_text((ROOT / 'files/www/cgi-bin/owrt-remote').read_text(encoding='utf-8'),
                            encoding='utf-8', newline='\n')
        self.key = self.root / 'key'
        self.key.write_text('test-key', encoding='utf-8')
        self.env = {**os.environ, 'OWRT_REMOTE_LOG_DIR': self.log_dir.as_posix(),
                    'OWRT_REMOTE_LOG_HELPER': self.helper.as_posix(), 'TMPDIR': self.root.as_posix(),
                    'KEY_FILE': self.key.as_posix(), 'TEST_UPTIME': '1000'}
        if os.name == 'nt':
            self.env['PATH'] = str(GIT_BIN) + os.pathsep + self.env['PATH']

    def helper_call(self, command, text='', *args, env=None):
        result = subprocess.run([SH, self.helper.as_posix(), command, *args], input=text,
                                encoding='utf-8', capture_output=True, env=env or self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def cgi_call(self, action, key='test-key', method='GET'):
        query = urlencode({'action': action, 'key': key})
        result = subprocess.run([SH, self.cgi.as_posix()], input='', encoding='utf-8',
                                capture_output=True, timeout=20,
                                env={**self.env, 'QUERY_STRING': query, 'REQUEST_METHOD': method})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_download_is_attachment_and_live_is_bounded_plain_text(self):
        self.helper_call('append', 'Xray connected\n' + 'x' * 1024 + '\n')
        downloaded = self.cgi_call('logs_download')
        self.assertIn('Content-Disposition: attachment; filename="owrt-remote-global-', downloaded)
        self.assertIn('Xray connected', downloaded)
        self.assertIn('Cache-Control: no-store', downloaded)
        # The bounded read is independent of append filtering; supply an existing large journal.
        with (self.log_dir / 'current.log').open('a', encoding='utf-8') as journal:
            journal.write(''.join(f'{i}: ' + 'y' * 1024 + '\n' for i in range(70)))
        live = self.cgi_call('logs_live').split('\n\n', 1)[1]
        # Git for Windows can translate CRLF inside native pipes.
        self.assertLessEqual(len(live.encode()), 65536)
        self.assertGreater(len(live.encode()), 64000)
        self.assertNotIn('Content-Disposition', self.cgi_call('logs_live'))

    def test_connection_noise_is_omitted_and_duplicate_sources_are_one_fact(self):
        self.helper_call('append',
            'Tue Oct 13 05:00:00 2026 daemon.err owrt-remote[3173]: Xray: запуск туннеля\n'
            'Tue Oct 13 05:00:01 2026 daemon.info owrt-remote[3173]: [Info] transport/internet: opening connection\n'
            'Tue Oct 13 05:00:01 2026 daemon.info owrt-remote[3173]: from tcp:1.2.3.4:80 accepted tcp:localhost:80\n'
            'Tue Oct 13 05:00:01 2026 daemon.info owrt-remote[3173]: [Debug] connection buffer\n'
            'Tue Oct 13 05:00:02 2026 user.notice owrt-remote: Another event\n'
            'Tue Oct 13 05:00:03 2026 user.notice owrt-remote: Xray: запуск туннеля\n'
            'Tue Oct 13 05:00:04 2026 daemon.info owrt-remote[3173]: [Warning] core: Xray 26.3.27 started\n')
        lines = self.helper_call('read').splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith('[OK]') for line in lines))
        self.assertEqual(sum('запуск туннеля' in line for line in lines), 1)
        self.assertFalse(any('opening connection' in line or 'accepted' in line for line in lines))

    def test_managed_agent_uses_one_system_log_sink_and_cli_keeps_its_output(self):
        agent = (ROOT / 'files/usr/sbin/owrt-remote').read_text(encoding='utf-8').split('cmd="${1:-help}"')[0]
        script = agent + '\nlogger() { printf "LOGGER=%s\\n" "$3"; }\nlog "event"\n'
        result = subprocess.run([SH], input=script, encoding='utf-8', capture_output=True,
                                env={**self.env, 'OWRT_REMOTE_SERVICE_LOG': '1'}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'LOGGER=event\n')
        self.assertEqual(result.stderr, '')
        result = subprocess.run([SH], input=script, encoding='utf-8', capture_output=True,
                                env={**self.env, 'OWRT_REMOTE_SERVICE_LOG': '0'}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, 'event\n')

    def test_heartbeat_records_changes_failures_and_recovery_instead_of_each_interval(self):
        good = 'Heartbeat: OK; Hub=https://hub.example; WAN_direct=1; WAN_active=wan\n'
        self.helper_call('append', good * 3)
        self.helper_call('append', 'Other action\n')
        self.helper_call('append', good, env={**self.env, 'TEST_UPTIME': '2000'})
        self.assertEqual(self.helper_call('read', env={**self.env, 'TEST_UPTIME': '2000'}).count('Heartbeat: OK;'), 1)
        failed = 'Heartbeat: ошибка; endpoint=https://hub.example; rc=7; connection refused\n'
        self.helper_call('append', failed * 3, env={**self.env, 'TEST_UPTIME': '2001'})
        self.helper_call('append', good, env={**self.env, 'TEST_UPTIME': '2002'})
        self.helper_call('append', failed, env={**self.env, 'TEST_UPTIME': '2003'})
        self.helper_call('append', good.replace('WAN_active=wan', 'WAN_active=wwan'),
                         env={**self.env, 'TEST_UPTIME': '2004'})
        stored = self.helper_call('read', env={**self.env, 'TEST_UPTIME': '2004'})
        self.assertEqual(stored.count('[ERROR] Heartbeat:'), 2, 'same failure after recovery must appear')
        self.assertEqual(stored.count('[OK] Heartbeat:'), 3, 'first, recovery and another WAN')

    def test_recurring_failure_is_recorded_again_after_a_quiet_period(self):
        failure = 'owrt-remote: cannot download Xray\n'
        self.helper_call('append', failure)
        self.helper_call('append', failure, env={**self.env, 'TEST_UPTIME': '1050'})
        self.helper_call('append', failure, env={**self.env, 'TEST_UPTIME': '1169'})
        self.helper_call('append', failure, env={**self.env, 'TEST_UPTIME': '1290'})
        self.assertEqual(self.helper_call('read', env={**self.env, 'TEST_UPTIME': '1290'}).count('cannot download'), 2)
        self.helper_call('append', 'Xray is not ready yet, retry in 5s (1/18)\n'
                         'Xray is not ready yet, retry in 5s (2/18)\n')
        self.assertEqual(self.helper_call('read').count('retry in 5s'), 1)

    def test_panel_failure_classifies_the_body_and_preserves_redaction(self):
        self.helper_call('event', 'rc=255\nhub_token=SECRET_TOKEN\n', 'Панель: Xray; результат=bad', 'bad')
        lines = self.helper_call('read').splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith('[ERROR]') for line in lines))
        self.assertNotIn('SECRET_TOKEN', '\n'.join(lines))
        self.helper_call('event', 'retry later\n', 'Панель: WAN; результат=warn', 'warn')
        self.assertTrue(self.helper_call('read').endswith('[WARN] retry later\n'))

    def test_wan_failure_and_recoverable_dns_fallback_have_correct_severity(self):
        self.helper_call('append', 'WAN has no IPv4 default route on wan\n'
                         'RTNETLINK answers: Network is unreachable\n'
                         'Heartbeat: Hub ответил, но обновление настроек не применилось\n'
                         'WAN DNS unavailable for hub.example; using last successful WAN address 192.0.2.1\n')
        lines = self.helper_call('read').splitlines()
        self.assertEqual([line.split(']', 1)[0] for line in lines], ['[ERROR', '[ERROR', '[ERROR', '[WARN'])

    def test_persistent_storage_is_rejected_before_any_file_is_written(self):
        mounts = self.root / 'mounts'
        mounts.write_text('/dev/root / overlay rw 0 0\n', encoding='utf-8')
        source = self.helper_source.replace('/proc/mounts', mounts.as_posix())
        self.helper.write_text(source, encoding='utf-8', newline='\n')
        env = self.env
        result = subprocess.run([SH, self.helper.as_posix(), 'append'], input='must not be written\n',
                                encoding='utf-8', capture_output=True, env=env, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing persistent storage', result.stderr)
        self.assertFalse(self.log_dir.exists())
        self.env = env
        self.assertIn('503 Service Unavailable', self.cgi_call('logs_live'))
        self.assertFalse(self.log_dir.exists())
        # The same location is permitted only when its containing mount is tmpfs.
        mounts.write_text('/dev/root / overlay rw 0 0\ntmpfs ' + self.root.as_posix() + ' tmpfs rw 0 0\n', encoding='utf-8')
        self.helper_call('append', 'RAM fact\n')
        self.assertEqual(self.helper_call('read'), '[OK] RAM fact\n')
        mounts.write_text('/dev/root / overlay rw 0 0\ntmpfs ' + self.root.as_posix()
                          + ' tmpfs rw 0 0\n/dev/flash ' + self.log_dir.as_posix()
                          + ' overlay rw 0 0\n', encoding='utf-8')
        result = subprocess.run([SH, self.helper.as_posix(), 'append'], input='must not enter nested Flash mount\n',
                                encoding='utf-8', capture_output=True, env=env, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.log_dir / 'current.log').read_text(), '[OK] RAM fact\n')

    def test_clear_requires_post_and_correct_key(self):
        self.helper_call('append', 'retain this event\n')
        self.assertIn('Доступ запрещен', self.cgi_call('logs_download', key='wrong'))
        self.assertIn('Доступ запрещен', self.cgi_call('logs_clear', key='wrong', method='POST'))
        self.assertIn('405 Method Not Allowed', self.cgi_call('logs_clear'))
        self.assertIn('retain this event', self.helper_call('read'))
        self.assertIn('Логи Remote очищены', self.cgi_call('logs_clear', method='POST'))
        self.assertEqual(self.helper_call('read'), '')
        self.helper_call('append', 'new event\n')
        self.assertEqual(self.helper_call('read'), '[OK] new event\n')

    def test_retention_uses_uptime_and_clears_on_reboot(self):
        self.helper_call('append', 'old event\n')
        self.assertIn('old event', self.helper_call('read', env={**self.env, 'TEST_UPTIME': '87399'}))
        self.assertEqual(self.helper_call('read', env={**self.env, 'TEST_UPTIME': '87400'}), '')
        self.helper_call('append', 'after daily clear\n', env={**self.env, 'TEST_UPTIME': '87401'})
        self.assertEqual(self.helper_call('read', env={**self.env, 'TEST_UPTIME': '20'}), '')

    def test_export_acknowledges_only_received_bytes_and_remote_clear_is_idempotent(self):
        self.helper_call('append', 'First event\n')
        first = json.loads(self.helper_call('export'))
        raw = base64.b64decode(first['data'])
        self.assertEqual(raw, b'[OK] First event\n')
        self.assertEqual(first['to'] - first['from'], len(raw))
        self.assertEqual(json.loads(self.helper_call('export')), first, 'lost reply retries same bytes')
        self.helper_call('ack', '', first['journal_id'], str(first['to']))
        self.assertEqual(json.loads(self.helper_call('export'))['data'], '')
        self.helper_call('append', 'Second event\n')
        second = json.loads(self.helper_call('export'))
        self.assertEqual(second['from'], first['to'])
        self.assertEqual(base64.b64decode(second['data']), b'[OK] Second event\n')
        self.helper_call('ack', '', second['journal_id'], str(second['to']), 'clear-123')
        cleared = json.loads(self.helper_call('export'))
        self.assertNotEqual(cleared['journal_id'], first['journal_id'])
        self.assertEqual(cleared['clear_ack'], 'clear-123')
        self.assertEqual(cleared['data'], '')
        self.helper_call('append', 'After clear\n')
        self.helper_call('ack', '', second['journal_id'], str(second['to']), 'clear-123')
        self.assertIn('After clear', self.helper_call('read'), 'retrying clear must not clear new events')

    def test_export_catches_up_in_bounded_batches_after_rotation(self):
        self.helper_call('clear')
        raw = b'[OK] ' + b'x' * 100000 + b'\n'
        (self.log_dir / 'current.log').write_bytes(raw)
        first = json.loads(self.helper_call('export'))
        first_raw = base64.b64decode(first['data'])
        self.assertLessEqual(len(first_raw), 65536)
        self.helper_call('ack', '', first['journal_id'], str(first['to']))
        second = json.loads(self.helper_call('export'))
        self.assertEqual(first_raw + base64.b64decode(second['data']), raw)
        # Bytes dropped by local rotation are detected by the server from the gap.
        (self.log_dir / 'current.log').write_bytes(b'[OK] Latest\n')
        (self.log_dir / 'total').write_text('200000')
        late = json.loads(self.helper_call('export'))
        self.assertEqual(late['from'], 200000 - len(b'[OK] Latest\n'))
        self.assertEqual(base64.b64decode(late['data']), b'[OK] Latest\n')

    def test_rotation_retains_recent_log_and_clear_removes_both(self):
        self.helper_call('clear')
        current = self.log_dir / 'current.log'
        current.write_text('A' * 1048576, encoding='utf-8')
        self.helper_call('append', 'second file\n')
        self.assertTrue((self.log_dir / 'previous.log').exists())
        self.assertEqual(current.read_text(), '[OK] second file\n')
        current.write_text('B' * 1048576, encoding='utf-8')
        self.helper_call('append', 'third file\n')
        output = self.helper_call('read')
        self.assertNotIn('A', output)
        self.assertTrue(output.endswith('third file\n'))
        self.helper_call('clear')
        self.assertFalse((self.log_dir / 'previous.log').exists())
        self.assertEqual(current.read_text(), '')

    def test_secrets_are_redacted_before_storage_and_html_is_literal(self):
        text = ('Heartbeat OK\nuci set owrtremote.main.hub_token=SECRET_TOKEN\n'
                'Authorization: Bearer SECRET_AUTH\nhttps://hub.test/?key=SECRET_KEY\n'
                '{"uuid":"SECRET_UUID"}\nabcdef12-abcd-1234-abcd-abcdef123456\n<script>alert(1)</script>\n')
        self.helper_call('append', text)
        stored = self.helper_call('read')
        self.assertNotIn('SECRET_', stored)
        self.assertNotIn('abcdef12-abcd-1234-abcd-abcdef123456', stored)
        self.assertIn('Heartbeat OK', stored)
        self.assertIn('<script>alert(1)</script>', stored)
        self.assertIn('Content-Type: text/plain', self.cgi_call('logs_live'))

    def test_parallel_events_do_not_corrupt_or_drop_lines(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.helper_call('append', f'event-{i}\n'), range(12)))
        self.assertEqual(set(self.helper_call('read').splitlines()), {f'[OK] event-{i}' for i in range(12)})
        self.assertFalse((self.log_dir / 'lock').exists())

    def test_stale_lock_recovers(self):
        self.helper_call('clear')
        lock = self.log_dir / 'lock'
        lock.mkdir()
        (lock / 'pid').write_text('99999999')
        self.helper_call('append', 'recovered\n')
        self.assertEqual(self.helper_call('read'), '[OK] recovered\n')

    def test_missing_collector_is_reported(self):
        self.helper.unlink()
        self.assertIn('503 Service Unavailable', self.cgi_call('logs_live'))

    def test_empty_lock_left_by_killed_writer_recovers(self):
        self.helper_call('clear')
        (self.log_dir / 'lock').mkdir()
        self.helper_call('append', 'after empty lock\n')
        self.assertEqual(self.helper_call('read'), '[OK] after empty lock\n')

    def test_collector_failure_cleans_both_children_and_fifo(self):
        source = self.helper.read_text(encoding='utf-8')
        mocks = '''
log_init() { return 0; }
mkfifo() { return 0; }
logread() { return 0; }
log_lock() { return 1; }
kill() { printf 'KILL=%s\\n' "$*"; }
wait() { return 0; }
rm() { printf 'REMOVE=%s\\n' "$*"; }
'''
        self.helper.write_text(source.replace('case "${1:-}" in', mocks + '\ncase "${1:-}" in', 1),
                               encoding='utf-8', newline='\n')
        result = subprocess.run([SH, self.helper.as_posix(), 'collect'], capture_output=True,
                                encoding='utf-8', env=self.env, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count('KILL='), 2)
        self.assertIn('REMOVE=-f ' + self.log_dir.as_posix() + '/stream.', result.stdout)


if __name__ == '__main__':
    unittest.main()
