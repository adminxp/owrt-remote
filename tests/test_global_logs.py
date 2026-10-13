"""Exercise the real RAM journal and authorized router CGI with temporary files."""
import concurrent.futures
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
        for _ in range(70):
            self.helper_call('append', 'y' * 1024 + '\n')
        live = self.cgi_call('logs_live').split('\n\n', 1)[1]
        self.assertLessEqual(len(live.encode()), 65536)
        self.assertNotIn('Content-Disposition', self.cgi_call('logs_live'))

    def test_clear_requires_post_and_correct_key(self):
        self.helper_call('append', 'retain this event\n')
        self.assertIn('Доступ запрещен', self.cgi_call('logs_download', key='wrong'))
        self.assertIn('Доступ запрещен', self.cgi_call('logs_clear', key='wrong', method='POST'))
        self.assertIn('405 Method Not Allowed', self.cgi_call('logs_clear'))
        self.assertIn('retain this event', self.helper_call('read'))
        self.assertIn('Логи Remote очищены', self.cgi_call('logs_clear', method='POST'))
        self.assertEqual(self.helper_call('read'), '')
        self.helper_call('append', 'new event\n')
        self.assertEqual(self.helper_call('read'), 'new event\n')

    def test_retention_uses_uptime_and_clears_on_reboot(self):
        self.helper_call('append', 'old event\n')
        self.assertIn('old event', self.helper_call('read', env={**self.env, 'TEST_UPTIME': '87399'}))
        self.assertEqual(self.helper_call('read', env={**self.env, 'TEST_UPTIME': '87400'}), '')
        self.helper_call('append', 'after daily clear\n', env={**self.env, 'TEST_UPTIME': '87401'})
        self.assertEqual(self.helper_call('read', env={**self.env, 'TEST_UPTIME': '20'}), '')

    def test_rotation_retains_recent_log_and_clear_removes_both(self):
        self.helper_call('clear')
        current = self.log_dir / 'current.log'
        current.write_text('A' * 1048576, encoding='utf-8')
        self.helper_call('append', 'second file\n')
        self.assertTrue((self.log_dir / 'previous.log').exists())
        self.assertEqual(current.read_text(), 'second file\n')
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
        self.assertEqual(set(self.helper_call('read').splitlines()), {f'event-{i}' for i in range(12)})
        self.assertFalse((self.log_dir / 'lock').exists())

    def test_stale_lock_recovers(self):
        self.helper_call('clear')
        lock = self.log_dir / 'lock'
        lock.mkdir()
        (lock / 'pid').write_text('99999999')
        self.helper_call('append', 'recovered\n')
        self.assertEqual(self.helper_call('read'), 'recovered\n')

    def test_missing_collector_is_reported(self):
        self.helper.unlink()
        self.assertIn('503 Service Unavailable', self.cgi_call('logs_live'))

    def test_empty_lock_left_by_killed_writer_recovers(self):
        self.helper_call('clear')
        (self.log_dir / 'lock').mkdir()
        self.helper_call('append', 'after empty lock\n')
        self.assertEqual(self.helper_call('read'), 'after empty lock\n')

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
