"""Router log delivery, offline archive, authorization and retry regressions."""
import base64
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
STATE = tempfile.TemporaryDirectory(prefix='router-log-archive-')
spec = importlib.util.spec_from_file_location('log_archive_hub', ROOT / 'vps/owrt-remote-hub.py')
hub = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hub
with mock.patch.dict(os.environ, {'OWRT_REMOTE_STATE_DIR': STATE.name}):
    spec.loader.exec_module(hub)


def batch(raw, start=0, journal='journal-1', clear_ack=''):
    return {'journal_id': journal, 'from': start, 'to': start + len(raw),
            'data': base64.b64encode(raw).decode(), 'clear_ack': clear_ack}


class QuietHandler(hub.Handler):
    def log_message(self, *args):
        pass


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=STATE.name)
        self.addCleanup(self.temp.cleanup)
        self.app = hub.App(Path(self.temp.name) / 'hub.db', 'session-test', 'agent-test', '')
        with self.app.conn() as conn:
            hub.upsert_router(conn, {'id': 'main', 'name': 'Test router', 'vps_host': 'hub.example'})
            hub.upsert_router(conn, {'id': 'other', 'name': 'Other router', 'vps_host': 'hub.example'})
        hub.write_json_private(hub.AUTH_FILE, {'username': 'owner', 'password_hash': 'unused'})
        self.server = hub.HubHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.server.app = self.app
        self.server.is_tls = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, payload=None, cookie=True, agent=False, method=None):
        headers = {}
        if cookie:
            headers['Cookie'] = 'owrt_remote_session=session-test'
        if agent:
            headers['Authorization'] = 'Bearer agent-test'
        raw = None
        if payload is not None:
            raw = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}{path}',
                                         data=raw, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read(), response.headers

    def ingest(self, value, state=None):
        payload = {'id': 'main', 'batch': value}
        if state is not None:
            payload['state'] = state
        raw, _ = self.request('/api/router-logs', payload, cookie=False, agent=True)
        return json.loads(raw)

    def snapshot(self):
        raw, _ = self.request('/api/router/main/logs')
        return json.loads(raw)

    def test_offline_router_page_download_and_archive_survive_hub_restart(self):
        raw = '[ERROR] WAN: connection refused\n[WARN] Retry\n[OK] Before disconnect\n'.encode()
        self.ingest(batch(raw))
        snapshot = self.snapshot()
        self.assertFalse(snapshot['online'])
        self.assertEqual(snapshot['text'].encode(), raw)
        page, _ = self.request('/router/main/logs')
        self.assertIn('Глобал логи'.encode(), page)
        self.assertIn('Роутер оффлайн'.encode(), page)
        downloaded, headers = self.request('/api/router/main/logs/download')
        self.assertEqual(downloaded, raw)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.server.app = hub.App(self.app.db_path, 'session-test', 'agent-test', '')
        self.assertEqual(self.snapshot()['text'].encode(), raw)
        with self.app.conn() as conn:
            router = hub.router_with_access(hub.row_to_router(hub.get_router(conn, 'main')), {'is_owner': True})
            self.assertTrue(router['permissions']['logs'])
            self.assertEqual(router['logs_url'], '/router/main/logs')
            self.assertFalse(router['online'])

    def test_lost_ack_retries_and_overlapping_batches_do_not_duplicate(self):
        first = b'[OK] first\n'
        second = b'[ERROR] next\n'
        self.ingest(batch(first))
        self.ingest(batch(first))
        self.ingest(batch(first + second))
        self.assertEqual(self.snapshot()['text'].encode(), first + second)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.ingest(batch(first + second)), range(4)))
        self.assertEqual(self.snapshot()['text'].encode(), first + second)
        self.ingest(batch(b'[OK] after reboot\n', journal='journal-2'))
        self.assertIn('новый журнал', self.snapshot()['text'])
        self.assertIn('first', self.snapshot()['text'])

    def test_empty_status_cannot_confirm_missing_records_or_invent_rotation_gap(self):
        reply = self.ingest(batch(b'', start=298))
        self.assertEqual(reply['offset'], 0)
        self.assertEqual(self.snapshot()['text'], '')
        raw = b'[OK] retained records arrive after status\n'
        self.ingest(batch(raw))
        reply = self.ingest(batch(b'', start=1000))
        self.assertEqual(reply['offset'], len(raw))
        self.assertEqual(self.snapshot()['text'].encode(), raw)

    def test_actual_gap_with_data_is_reported_once_but_retries_keep_data(self):
        value = batch(b'[OK] surviving records\n', start=50)
        self.ingest(value)
        self.ingest(value)
        text = self.snapshot()['text']
        self.assertEqual(text.count('не доставлена'), 1)
        self.assertEqual(text.count('surviving records'), 1)

    def test_empty_new_generation_cannot_erase_current_journal_cursor(self):
        raw = b'[OK] previous generation\n'
        self.ingest(batch(raw))
        self.ingest(batch(b'', start=2000, journal='new-journal'))
        self.ingest(batch(raw))
        self.assertEqual(self.snapshot()['text'].encode(), raw)

    def test_recovery_generation_replays_previously_falsely_confirmed_chunks(self):
        # Reproduce the cursor left by the former empty-batch ingestion bug.
        with self.app.conn() as conn:
            conn.execute("insert into router_logs(router_id,journal_id,cursor) values ('main','broken',298)")
        self.ingest(batch(b'[OK] retained restart event\n', journal='recovered-old'))
        self.assertIn('retained restart event', self.snapshot()['text'])

    def test_real_router_spool_recovers_bad_digest_and_delivers_after_restart(self):
        from test_persistent_logs import PersistentLogsTests
        client = PersistentLogsTests()
        client.setUp()
        self.addCleanup(client.doCleanups)
        client.append('Панель: действие=stop; начало\nСервис: остановка туннеля\n')
        client.flush()
        original = client.export()
        client.acknowledge(original); client.flush()
        with self.app.conn() as conn:
            conn.execute('insert into router_logs(router_id,journal_id,cursor) values (?,?,?)',
                         ('main', original['journal_id'], original['to']))
        part = next(client.journal.glob('part.*'))
        part.rename(part.with_name(part.name.rsplit('.', 1)[0] + '.'))
        (client.run_dir / 'recovered-v2').unlink()
        repaired = client.export()
        reply = self.ingest(repaired, {'enabled': False, 'xray': 'stopped'})
        client.call('ack', '', reply['journal_id'], str(reply['offset']))
        # A lost acknowledgment may replay, but the archive must stay unchanged.
        self.ingest(repaired)
        self.assertEqual(self.snapshot()['text'].count('Сервис: остановка туннеля'), 1)
        client.append('Сервис: запуск\n'); client.flush()
        second = client.export()
        reply = self.ingest(second, {'enabled': True, 'xray': 'running'})
        client.call('ack', '', reply['journal_id'], str(reply['offset']))
        text = self.snapshot()['text']
        self.assertIn('Сервис: запуск', text)
        self.assertNotIn('не доставлена до ротации', text)
        self.assertEqual(client.export()['data'], '')

    def test_disk_mount_diagnostic_resolves_deepest_mount_and_escaped_paths(self):
        path = self.app.db_path.resolve()
        mount = str(path.parent).replace(' ', r'\040')
        root = str(Path(path.anchor)).replace(' ', r'\040')
        mountinfo = f'1 0 8:1 / {root} rw - ext4 /dev/vda1 rw\n'
        mountinfo += f'2 1 0:1 / {mount} rw - tmpfs tmpfs rw\n'
        with mock.patch.object(hub.sys, 'platform', 'linux'), mock.patch('builtins.open', mock.mock_open(read_data=mountinfo)):
            result = hub.router_log_storage(path)
        self.assertIs(result['persistent'], False)
        self.assertEqual(result['filesystem'], 'tmpfs')
        with mock.patch.object(hub.sys, 'platform', 'linux'), mock.patch('builtins.open', mock.mock_open(read_data=mountinfo.splitlines()[0])):
            self.assertIs(hub.router_log_storage(path)['persistent'], True)

    def test_offline_clear_does_not_restore_old_logs_on_reconnection(self):
        old = b'[ERROR] old\n'
        self.ingest(batch(old))
        raw, _ = self.request('/api/router/main/logs/clear', {}, method='POST')
        self.assertTrue(json.loads(raw)['pending_clear'])
        self.assertEqual(self.snapshot()['text'], '')
        reply = self.ingest(batch(old))
        token = reply['clear_request']
        self.assertTrue(token)
        self.assertEqual(self.snapshot()['text'], '')
        self.ingest(batch(b'', journal='after-clear', clear_ack=token))
        self.assertFalse(self.snapshot()['pending_clear'])
        fresh = b'[OK] fresh\n'
        self.ingest(batch(fresh, journal='after-clear', clear_ack=token))
        self.assertEqual(self.snapshot()['text'].encode(), fresh)
        self.ingest(batch(fresh, journal='after-clear', clear_ack=token))
        self.assertEqual(self.snapshot()['text'].encode(), fresh)

    def test_credentials_are_redacted_and_retention_is_bounded(self):
        self.ingest(batch(b'[ERROR] hub_token=SECRET\n'))
        self.assertNotIn('SECRET', self.snapshot()['text'])
        self.assertTrue(self.snapshot()['text'].startswith('[ERROR]'))
        with mock.patch.object(hub, 'ROUTER_LOG_MAX_BYTES', 80):
            self.ingest(batch(b'[OK] ' + b'X' * 150 + b'\n', journal='new'))
        self.assertEqual(self.snapshot()['bytes'], 80)

    def test_malformed_batches_rejected_without_touching_archive(self):
        for value in (batch(b'abc', start=-1), {**batch(b'abc'), 'to': 9},
                      {**batch(b'abc'), 'data': 'invalid base64'}, batch(b'x' * 65537),
                      batch(b'', journal='../file')):
            with self.subTest(value=list(value)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.ingest(value)
                self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.snapshot()['text'], '')

    def test_agent_token_and_router_permission_are_required(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/router-logs', {'id': 'main', 'batch': batch(b'')}, cookie=True)
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/router/main/logs', cookie=False)
        self.assertEqual(error.exception.code, 401)
        delegated = {'username': 'viewer', 'is_owner': False, 'router_flags': {'other': ['G'], 'main': ['A']}}
        with mock.patch.object(hub.Handler, 'current_account_profile', return_value=delegated):
            for path, method in [('/router/main/logs', None), ('/api/router/main/logs', None),
                                 ('/api/router/main/logs/download', None), ('/api/router/main/logs/clear', 'POST')]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request(path, {} if method else None, method=method)
                self.assertEqual(error.exception.code, 403)

    def test_hub_records_offline_transition_once_and_delivery_does_not_mark_online(self):
        with self.app.conn() as conn:
            conn.execute('update routers set last_seen = ? where id = ?', (hub.now_ts(), 'main'))
        self.app.prime_router_state_snapshot()
        with self.app.conn() as conn:
            conn.execute('update routers set last_seen = 1 where id = ?', ('main',))
        with mock.patch.object(hub, 'notify_router_offline'):
            self.app.check_router_state_changes()
            self.app.check_router_state_changes()
        self.assertEqual(self.snapshot()['text'].count('истекло время'), 1)
        self.ingest(batch(b'[OK] live logs with no heartbeat\n'))
        self.assertFalse(self.snapshot()['online'])

    def test_stop_reason_arrives_while_offline_and_no_heartbeat_is_required(self):
        initial = b'[OK] Heartbeat: OK; Hub=https://hub.example\n'
        self.ingest(batch(initial))
        stopped = '[OK] Панель: действие=stop; начало\n[OK] Сервис: остановка туннеля\n[WARN] Панель: Сервис остановлен; результат=warn\n'.encode()
        self.ingest(batch(stopped, start=len(initial)), {'enabled': False, 'xray': 'stopped'})
        snapshot = self.snapshot()
        self.assertFalse(snapshot['online'])
        self.assertIn('Сервис остановлен', snapshot['text'])
        self.assertIn('Remote выключен', snapshot['diagnosis']['text'])
        self.assertEqual(snapshot['diagnosis']['level'], 'warn')
        # Empty periodic deliveries must still carry the state without new log lines.
        self.ingest(batch(b'', start=len(initial) + len(stopped)), {'enabled': True, 'xray': 'stopped'})
        self.assertEqual(self.snapshot()['diagnosis']['level'], 'error')
        self.assertIn('Xray остановлен', self.snapshot()['diagnosis']['text'])
        self.ingest(batch(b'', start=len(initial) + len(stopped)), {'enabled': True, 'xray': 'running'})
        self.assertNotIn('Remote выключен', self.snapshot()['diagnosis']['text'])

    def test_offline_monitor_states_known_stop_cause_instead_of_referring_to_router(self):
        with self.app.conn() as conn:
            conn.execute('update routers set last_seen = ? where id = ?', (hub.now_ts() - 1, 'main'))
        self.app.prime_router_state_snapshot()
        self.ingest(batch('[WARN] Панель: Сервис остановлен; результат=warn\n'.encode()),
                    {'enabled': False, 'xray': 'stopped'})
        with self.app.conn() as conn:
            conn.execute('update routers set last_seen = 1 where id = ?', ('main',))
        with mock.patch.object(hub, 'notify_router_offline'):
            self.app.check_router_state_changes()
        text = self.snapshot()['text']
        self.assertIn('Remote выключен', text)
        self.assertNotIn('Причина на роутере', text)

    def test_unknown_outage_is_not_reported_as_a_confirmed_cause(self):
        self.ingest(batch(b'[OK] Heartbeat: OK; Hub=https://hub.example\n'))
        self.assertIn('Причина обрыва не доставлена', self.snapshot()['diagnosis']['text'])
        raw = b'[ERROR] WAN has no IPv4 default route on wan\n'
        self.ingest(batch(raw, journal='network-outage'))
        reason = self.snapshot()['diagnosis']['text']
        self.assertIn('WAN has no IPv4 default route', reason)
        self.assertIn('Последнее доставленное', reason)

    def test_legacy_delivered_stop_reason_is_visible_without_new_agent_state(self):
        self.ingest(batch('[WARN] Панель: Сервис остановлен; результат=warn\n'.encode()))
        self.assertIn('остановлен через панель', self.snapshot()['diagnosis']['text'])
        self.ingest(batch('[OK] Сервис: запуск\n'.encode(), journal='restart'))
        self.assertNotIn('остановлен через панель', self.snapshot()['diagnosis']['text'])

    def test_invalid_state_rolls_back_batch_and_does_not_change_archive(self):
        self.ingest(batch(b'[OK] before\n'))
        original = self.snapshot()['text']
        with self.assertRaises(urllib.error.HTTPError):
            self.ingest(batch(b'[ERROR] invalid\n', journal='invalid-state'), {'enabled': 'false', 'xray': 'running'})
        self.assertEqual(self.snapshot()['text'], original)

    def test_existing_archive_gains_state_columns_without_losing_logs(self):
        raw = b'[OK] saved by the previous Hub\n'
        self.ingest(batch(raw))
        with self.app.conn() as conn:
            conn.execute('alter table router_logs drop column state_json')
            conn.execute('alter table router_logs drop column state_at')
        self.server.app = hub.App(self.app.db_path, 'session-test', 'agent-test', '')
        self.assertEqual(self.snapshot()['text'].encode(), raw)
        self.ingest(batch(b'', start=len(raw)), {'enabled': False, 'xray': 'stopped'})
        snapshot = self.snapshot()
        self.assertIn('Remote выключен', snapshot['diagnosis']['text'])
        self.assertEqual(snapshot['text'].encode(), raw)


if __name__ == '__main__':
    unittest.main()
