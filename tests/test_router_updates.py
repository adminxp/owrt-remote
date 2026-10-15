"""Sequential updates: isolated Hub, fake SSH, and real shell task execution."""
import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
STATE = tempfile.TemporaryDirectory(prefix='owrt-router-update-tests-')
spec = importlib.util.spec_from_file_location('router_update_hub', ROOT / 'vps/owrt-remote-hub.py')
hub = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hub
with mock.patch.dict(os.environ, {'OWRT_REMOTE_STATE_DIR': STATE.name}):
    spec.loader.exec_module(hub)


def b64(value):
    return base64.b64encode(value.encode()).decode()


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=STATE.name)
        self.addCleanup(self.temp.cleanup)
        self.app = hub.App(Path(self.temp.name) / 'hub.db', 'owner-session', 'agent', '')
        self.manager = self.app.router_updates
        self.manager.POLL_SECONDS = .001
        self.manager.ROUTER_TIMEOUT = .02
        hub.save_auth_state({'username': 'owner', 'password': hub.password_digest('hub-password')})
        hub.save_sessions([])
        with self.app.conn() as conn:
            for index, name in enumerate(['Old name', 'Second router']):
                row = hub.upsert_router(conn, {'id': ['main', 'node-2'][index], 'name': name,
                                             'entry_port': 18080 + index, 'ssh_entry_port': 19080 + index})
                conn.execute('update routers set status_json=? where id=?',
                             (json.dumps({'name': 'Советская 30' if index == 0 else name}), row['id']))
        self.addCleanup(self.manager.stop)

    def finish(self):
        self.manager.thread.join(timeout=3)
        self.assertFalse(self.manager.thread.is_alive())
        return self.manager.snapshot()

    def test_sequential_names_failures_and_no_password_in_snapshot(self):
        calls = []
        def ssh(row, script, password):
            if 'OWRT_UPDATE_READY=1' in script: return 'OWRT_UPDATE_READY=1\n'
            calls.append((row['id'], script, password))
            if 'exec setsid /bin/sh' in script:
                return 'OWRT_UPDATE_STARTED=1\nOWRT_UPDATE_NAME=' + b64('Советская 30' if row['id'] == 'main' else 'node-2')
            if 'OWRT_UPDATE_OUTPUT=' in script:
                return 'OWRT_UPDATE_STATE=done\nOWRT_UPDATE_RC=' + ('0' if row['id'] == 'main' else '5') + '\nOWRT_UPDATE_OUTPUT=' + b64('ssh-secret\nhub_token=hidden\nCompleted')
            return ''
        with mock.patch.object(self.manager, '_ssh', side_effect=ssh):
            self.manager.start('wget -O - "https://repo.example/install.sh" | sh', 'ssh-secret', 'request-1234567890')
            job = self.finish()
        self.assertEqual([x[0] for x in calls], ['main'] * 3 + ['node-2'] * 3)
        self.assertTrue(all(x[2] == 'ssh-secret' for x in calls))
        self.assertEqual(job['items'][0]['name'], 'Советская 30')
        self.assertEqual([x['state'] for x in job['items']], ['success', 'error'])
        self.assertNotIn('ssh-secret', json.dumps(job))
        self.assertNotIn('hub_token=hidden', json.dumps(job))
        self.assertNotIn('command', job)
        self.assertIsNone(getattr(self.manager.thread, '_target', None))

    def test_launch_disconnect_polls_existing_task_without_reexecution(self):
        launches = []
        def ssh(row, script, password):
            if 'OWRT_UPDATE_READY=1' in script: return 'OWRT_UPDATE_READY=1\n'
            if 'exec setsid /bin/sh' in script:
                launches.append(row['id'])
                raise RuntimeError('connection closed after acceptance')
            return 'OWRT_UPDATE_STATE=done\nOWRT_UPDATE_RC=0\n'
        with mock.patch.object(self.manager, '_ssh', side_effect=ssh):
            self.manager.start('echo install', '', 'request-1234567891')
            job = self.finish()
        self.assertEqual(launches, ['main', 'node-2'])
        self.assertTrue(all(x['state'] == 'success' for x in job['items']))

    def test_unknown_result_stops_queue_without_false_success_or_overlap(self):
        launches = []
        def ssh(row, script, password):
            if 'OWRT_UPDATE_READY=1' in script: return 'OWRT_UPDATE_READY=1\n'
            if 'exec setsid /bin/sh' in script:
                launches.append(row['id'])
                return 'OWRT_UPDATE_STARTED=1\n'
            raise RuntimeError('temporary disconnect')
        with mock.patch.object(self.manager, '_ssh', side_effect=ssh):
            self.manager.start('echo install', '', 'request-1234567892')
            job = self.finish()
        self.assertEqual(launches, ['main'])
        self.assertEqual([x['state'] for x in job['items']], ['unknown', 'skipped'])

    def test_duplicate_click_and_cancel_dont_launch_again_or_interrupt_current(self):
        entered, release = threading.Event(), threading.Event()
        def ssh(row, script, password):
            if 'OWRT_UPDATE_READY=1' in script: return 'OWRT_UPDATE_READY=1\n'
            if 'exec setsid /bin/sh' in script:
                entered.set(); release.wait(2)
                return 'OWRT_UPDATE_STARTED=1\n'
            return 'OWRT_UPDATE_STATE=done\nOWRT_UPDATE_RC=0\n'
        with mock.patch.object(self.manager, '_ssh', side_effect=ssh):
            task = self.manager.start('echo install', '', 'request-1234567893')
            self.assertTrue(entered.wait(2))
            self.assertEqual(task, self.manager.start('echo install', '', 'request-1234567893'))
            with self.assertRaises(RuntimeError):
                self.manager.start('echo install', '', 'request-1234567894')
            self.manager.cancel(task)
            release.set()
            job = self.finish()
        self.assertEqual([x['state'] for x in job['items']], ['success', 'skipped'])

    def test_missing_tunnel_and_invalid_input(self):
        with self.app.conn() as conn:
            conn.execute('update routers set ssh_entry_port=0, entry_port=0')
        with mock.patch.object(self.manager, '_ssh') as ssh:
            self.manager.start('echo install', '', 'request-1234567895')
            job = self.finish()
            ssh.assert_not_called()
        self.assertTrue(all(x['state'] == 'skipped' for x in job['items']))
        for value in ['', 'a' * 8193, '\x00', None]:
            with self.assertRaises(ValueError):
                self.manager.start(value, '', 'request-1234567896')

    def test_failed_authentication_continues_without_sending_installer(self):
        calls = []
        def ssh(row, script, password):
            calls.append(script)
            raise RuntimeError('Permission denied, please try again.\nroot@127.0.0.1: Permission denied (publickey,password).')
        with mock.patch.object(self.manager, '_ssh', side_effect=ssh):
            self.manager.start('echo install', 'wrong-password', 'request-1234567888')
            job = self.finish()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('OWRT_UPDATE_READY' in script for script in calls))
        self.assertTrue(all(item['state'] == 'error' for item in job['items']))
        self.assertTrue(all('Общий SSH-пароль не принят' in item['message'] for item in job['items']))
        self.assertNotIn('строка с ключом или паролем скрыта', json.dumps(job, ensure_ascii=False))
        self.assertNotIn('wrong-password', json.dumps(job))

    def test_wrong_common_password_skips_installer_but_next_router_succeeds(self):
        launches = []
        def ssh(row, script, password):
            if row['id'] == 'main':
                raise RuntimeError('root@127.0.0.1: Permission denied (publickey,password,keyboard-interactive).')
            if 'OWRT_UPDATE_READY=1' in script: return 'OWRT_UPDATE_READY=1\n'
            if 'exec setsid /bin/sh' in script:
                launches.append(row['id'])
                return 'OWRT_UPDATE_STARTED=1\n'
            return 'OWRT_UPDATE_STATE=done\nOWRT_UPDATE_RC=0\n'
        with mock.patch.object(self.manager, '_ssh', side_effect=ssh):
            self.manager.start('echo install', 'common-secret', 'request-1234567889')
            job = self.finish()
        self.assertEqual(launches, ['node-2'])
        self.assertEqual([item['state'] for item in job['items']], ['error', 'success'])
        self.assertEqual(job['items'][0]['name'], 'Советская 30')
        self.assertIn('отличается от общего', job['items'][0]['message'])
        self.assertNotIn('common-secret', json.dumps(job))

    def test_authentication_diagnosis_preserves_other_errors_and_secret_filter(self):
        for response in ['Permission denied', 'Permission denied (password).', 'Authentication failed.']:
            with self.subTest(response=response):
                self.assertIn('Общий SSH-пароль не принят', self.manager._ssh_error(response, 'secret'))
        self.assertIn('SSH-ключ', self.manager._ssh_error('Permission denied (publickey).', ''))
        self.assertIn('не предлагает вход по паролю', self.manager._ssh_error('Permission denied (publickey).', 'secret'))
        for response in ['ssh command timeout after 20s', 'Connection refused', '/root/file: Permission denied']:
            with self.subTest(response=response):
                self.assertEqual(self.manager._ssh_error(response, 'secret'), response)
        safe = self.manager._ssh_error('Failed: password=secret\nConnection closed', 'secret')
        self.assertNotIn('secret', safe)
        self.assertIn('строка с ключом или паролем скрыта', safe)
        self.assertIn('Connection closed', safe)

    def test_owner_only_api_csrf_and_page_default_hosting(self):
        class Quiet(hub.Handler):
            def log_message(self, *args): pass
        server = hub.HubHTTPServer(('127.0.0.1', 0), Quiet)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None, cookie='owner-session', csrf=None, origin=None):
            headers = {'Cookie': hub.SESSION_COOKIE + '=' + cookie}
            if body is not None: headers['Content-Type'] = 'application/json'
            if csrf is not None: headers['X-Router-Update-CSRF'] = csrf
            if origin is not None: headers['Origin'] = origin
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                conn.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, headers)
                response = conn.getresponse()
                return response.status, response.read().decode()
            finally: conn.close()
        try:
            self.assertEqual(request('/api/router-updates', cookie='')[0], 401)
            with mock.patch.object(hub.Handler, 'current_account_profile', return_value={'is_owner': False}):
                self.assertEqual(request('/api/router-updates')[0], 403)
                self.assertEqual(request('/router-updates/')[0], 403)
            status, page = request('/router-updates/')
            self.assertEqual(status, 200)
            self.assertIn(hub.RAW_REPO_BASE + '/install.sh', page)
            self.assertIn(self.manager.csrf, page)
            payload = {'command': 'echo install', 'ssh_password': '', 'request_id': 'request-1234567897'}
            self.assertEqual(request('/api/router-updates/start', payload)[0], 403)
            self.assertEqual(request('/api/router-updates/start', payload, csrf=self.manager.csrf, origin='https://attacker.example')[0], 403)
            def ssh(row, script, password):
                if 'OWRT_UPDATE_READY=1' in script: return 'OWRT_UPDATE_READY=1\n'
                return 'OWRT_UPDATE_STATE=done\nOWRT_UPDATE_RC=0\n'
            with mock.patch.object(self.manager, '_ssh', side_effect=ssh):
                self.assertEqual(request('/api/router-updates/start', payload, csrf=self.manager.csrf)[0], 200)
                self.assertTrue(all(item['state'] == 'success' for item in self.finish()['items']))
            _, body = request('/api/router-updates')
            self.assertNotIn('ssh_password', body)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


class RouterTaskTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'fixture uses bundled Windows POSIX shell')
    def test_real_detached_task_failure_pipefail_bounded_output_and_cleanup(self):
        shell = r'C:\Program Files\Git\usr\bin\sh.exe'
        with tempfile.TemporaryDirectory(prefix='owrt-update-shell-') as folder:
            root = Path(folder).as_posix()
            task_dir = root + '/task'
            env = {**os.environ, 'PATH': str(Path(shell).parent) + os.pathsep + os.environ['PATH']}
            def run(script):
                file = Path(folder) / 'invoke.sh'
                file.write_text(script.replace('/tmp/owrt-remote-update-fixture', task_dir), encoding='utf-8', newline='\n')
                return subprocess.run([shell, file.as_posix()], capture_output=True, text=True, env=env, timeout=8)
            launch = hub.router_update_launch_script('fixture', 'i=0; while [ "$i" -lt 1200 ]; do echo "A line of output to keep in a bounded tail only"; i=$((i+1)); done; false | cat')
            # MSYS has FIFO/process support but no setsid applet. Linux OpenWrt
            # provides setsid; only that platform-specific syscall is adapted.
            result = run(launch.replace('command -v setsid', 'command -v sh').replace('exec setsid /bin/sh', 'exec /bin/sh'))
            self.assertEqual(result.returncode, 0, result.stderr)
            deadline = time.monotonic() + 5
            while not (Path(task_dir)/'result').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            polled = run(hub.router_update_poll_script('fixture'))
            fields = hub.router_update_fields(polled.stdout)
            self.assertEqual(fields['STATE'], 'done', polled.stderr + polled.stdout)
            self.assertNotEqual(fields['RC'], '0', 'wget failure must not be hidden by the succeeding shell')
            self.assertLessEqual((Path(task_dir)/'output').stat().st_size, 32768)
            self.assertLessEqual(len(fields['OUTPUT'].encode()), 8192)
            self.assertFalse((Path(task_dir)/'stream').exists())
            self.assertEqual(run(hub.router_update_poll_script('fixture', cleanup=True)).returncode, 0)
            self.assertFalse(Path(task_dir).exists())
            # A successful command yields rc=0 only after completion, and a
            # replay of the same task cannot execute that command twice.
            good = hub.router_update_launch_script('fixture', 'echo run >>"' + root + '/runs"; echo installed')
            adapted = good.replace('command -v setsid', 'command -v sh').replace('exec setsid /bin/sh', 'exec /bin/sh')
            self.assertEqual(run(adapted).returncode, 0)
            self.assertEqual(run(adapted).returncode, 0)
            deadline = time.monotonic() + 5
            while not (Path(task_dir)/'result').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            fields = hub.router_update_fields(run(hub.router_update_poll_script('fixture')).stdout)
            self.assertEqual(fields['RC'], '0')
            self.assertEqual((Path(folder)/'runs').read_text().splitlines(), ['run'])
            self.assertEqual(run(hub.router_update_poll_script('fixture', cleanup=True)).returncode, 0)
            # All generated shell scripts must also parse under real BusyBox ash.
            busybox = ROOT / '.test-output/busybox/busybox64.exe'
            script = Path(folder)/'syntax.sh'
            script.write_text(launch, encoding='utf-8', newline='\n')
            self.assertEqual(subprocess.run([str(busybox), 'ash', '-n', str(script)], capture_output=True).returncode, 0)
            runner = launch.split("<<'OWRT_UPDATE_RUNNER'\n", 1)[1].split('\nOWRT_UPDATE_RUNNER', 1)[0]
            script.write_text(runner, encoding='utf-8', newline='\n')
            self.assertEqual(subprocess.run([str(busybox), 'ash', '-n', str(script)], capture_output=True).returncode, 0)


if __name__ == '__main__': unittest.main()
