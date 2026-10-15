"""Persistent spool recovery/resource limits using the real shell helper.

Mount fixtures replace /proc/mounts only in disposable copies. Set
OWRT_LOG_TEST_SHELL to a BusyBox executable to exercise its actual ash/applets.
"""
import base64
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GIT_BIN = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/usr/bin'
SH = str(GIT_BIN / 'sh.exe') if os.name == 'nt' else shutil.which('sh')
BUSYBOX = os.environ.get('OWRT_LOG_TEST_SHELL', '')


class PersistentLogsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='persistent-logs-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run_dir = self.root / 'runtime'
        self.flash = self.root / 'flash'
        self.flash.mkdir()
        self.helper = self.root / 'logs.sh'
        self.mounts = self.root / 'mounts'
        self.root_mount = self.root.as_posix()
        self.flash_mount = self.flash.as_posix()
        if os.name == 'nt' and not BUSYBOX:
            # Git's bundled readlink may emit native C:/ or MSYS /c paths.
            # The fixture must use what the actual applet returns, not assume it.
            env = {**os.environ, 'PATH': str(GIT_BIN) + os.pathsep + os.environ['PATH']}
            def resolved_mount(path):
                return subprocess.check_output([SH, '-c', 'readlink -f "$1"', '--', path.as_posix()],
                                               env=env, text=True).strip()
            self.root_mount = resolved_mount(self.root)
            self.flash_mount = resolved_mount(self.flash)
        self.mounts.write_text(f'tmpfs {self.root_mount} tmpfs rw 0 0\n'
                               f'/dev/ubi0_1 {self.flash_mount} ubifs rw 0 0\n')
        source = (ROOT / 'files/usr/lib/owrt-remote/logs.sh').read_text(encoding='utf-8')
        source = source.replace('/proc/mounts', self.mounts.as_posix())
        if os.name == 'nt':
            source = source.replace('case "$path" in /*)', 'case "$path" in [a-zA-Z]:/*|/*)')
        source = source.replace('log_clock() { cut -d. -f1 /proc/uptime; }',
                                'log_clock() { echo "${TEST_UPTIME:-1000}"; }')
        source = source.replace('log_epoch() { date +%s; }',
                                'log_epoch() { echo "${TEST_EPOCH:-' + str(int(time.time())) + '}"; }')
        # Real df remains covered in integration tests. Fixtures emulate free space.
        source = source.replace('free="$(df -Pk "$LOG_DIR" | awk \'NR==2 {print $4}\')"',
                                'free="${TEST_FREE_KB:-8192}"')
        self.source = source
        self.source = self.source.replace('legacy=/tmp/owrt-remote-logs', 'legacy=' + (self.root / 'legacy').as_posix())
        self.helper.write_text(self.source, encoding='utf-8', newline='\n')
        self.env = {**os.environ, 'OWRT_REMOTE_LOG_RUNTIME': self.run_dir.as_posix(),
                    'OWRT_REMOTE_LOG_DIR': (self.flash / 'journal').as_posix()}
        self.env['PATH'] = str(GIT_BIN) + os.pathsep + self.env['PATH']
        self.shell = [BUSYBOX, 'ash'] if BUSYBOX else [SH]
        if BUSYBOX:
            self.env['BB_OVERRIDE_APPLETS'] = '1'

    def call(self, command, text='', *args, check=True, **flags):
        result = subprocess.run([*self.shell, self.helper.as_posix(), command, *args],
                                input=text, encoding='utf-8', capture_output=True,
                                env={**self.env, **flags}, timeout=25)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def status(self):
        return dict(line.split('=', 1) for line in self.call('status').stdout.splitlines())

    def flush(self, **flags):
        return self.call('flush', '', 'force', **flags)

    def export(self):
        return json.loads(self.call('export').stdout)

    def append(self, text, **flags):
        return self.call('append', text, **flags)

    def acknowledge(self, batch):
        self.call('ack', '', batch['journal_id'], str(batch['to']))

    @property
    def journal(self):
        return self.flash / 'journal'

    def reboot(self):
        shutil.rmtree(self.run_dir)

    def test_persistent_storage_selected_and_survives_runtime_loss(self):
        self.append('before reboot\n')
        self.flush()
        first = self.export()
        self.acknowledge(first)
        self.flush()
        self.reboot()
        self.assertEqual(self.status()['persistent'], '1')
        self.assertIn('before reboot', self.call('read').stdout)
        after = self.export()
        self.assertEqual(after['journal_id'], first['journal_id'])
        self.assertEqual(after['from'], first['to'])
        self.assertEqual(after['data'], '')
        self.assertFalse(any(self.journal.glob('stream.*')))
        self.assertFalse((self.journal / 'lock').exists())

    def test_missing_optional_cksum_does_not_create_empty_digest_or_drop_facts(self):
        source = self.source.replace('case "${1:-}" in', 'cksum() { return 127; }\ncase "${1:-}" in', 1)
        self.helper.write_text(source, encoding='utf-8', newline='\n')
        self.append('first event\nsecond event\n')
        self.flush()
        part = next(self.journal.glob('part.*'))
        self.assertIn('.s256-', part.name)
        self.assertIn(b'second event', base64.b64decode(self.export()['data']))
        self.reboot()
        self.assertIn(b'first event', base64.b64decode(self.export()['data']))

    def test_digest_fingerprint_cache_stays_small_at_64_entries(self):
        self.call('check')
        # Seed 63 existing fingerprints; exercise eviction with two real events
        # instead of starting thousands of Windows utility processes for a fill.
        (self.run_dir / 'facts').write_text(''.join('1000\ts256-%035d\n' % i for i in range(63)))
        self.append('distinct event 63\ndistinct event 64\n')
        self.assertEqual(len((self.run_dir / 'facts').read_text().splitlines()), 64)
        self.assertLessEqual((self.run_dir / 'facts').stat().st_size, 4096)

    def test_empty_digest_files_falsely_acked_by_old_hub_are_recovered(self):
        self.append('retained restart event\n'); self.flush()
        before = self.export()
        self.acknowledge(before); self.flush()
        part = next(self.journal.glob('part.*'))
        part.rename(part.with_name(part.name.rsplit('.', 1)[0] + '.'))
        # Upgrade during the same boot must override the former recovery marker.
        (self.run_dir / 'recovered-v2').unlink()
        (self.run_dir / 'recovered').touch()
        repaired = self.export()
        self.assertNotEqual(repaired['journal_id'], before['journal_id'])
        self.assertTrue(repaired['journal_id'].startswith('recovered-'))
        self.assertEqual(repaired['from'], 0)
        self.assertIn(b'retained restart event', base64.b64decode(repaired['data']))
        self.assertFalse(any(p.name.endswith('.') for p in self.journal.glob('part.*')))
        self.acknowledge(repaired)
        self.assertEqual(self.export()['data'], '')
        self.flush()
        # Recovery's warning was buffered after the replay chunk; deliver it too.
        self.acknowledge(self.export())
        self.flush()
        self.reboot()
        self.assertEqual(self.export()['data'], '')

    def test_checksum_failure_keeps_pending_and_does_not_advance_total(self):
        self.append('keep until checksum works\n')
        before = self.status()['committed_bytes']
        source = self.source.replace('log_checksum() {', 'log_checksum() { return 1;', 1)
        self.helper.write_text(source, encoding='utf-8', newline='\n')
        self.call('flush', '', 'force', check=False)
        self.assertFalse(list(self.journal.glob('part.*')))
        self.assertEqual(self.status()['committed_bytes'], before)
        self.assertIn('keep until checksum works', (self.run_dir / 'pending').read_text())

    def test_md5_and_legacy_numeric_checksum_formats_remain_readable(self):
        for digest in ('md5', 'crc'):
            with self.subTest(digest=digest):
                self.call('clear')
                source = self.source.replace('if command -v sha256sum >/dev/null 2>&1; then method=s256',
                                             'if false; then method=s256')
                if digest == 'crc':
                    source = source.replace('elif command -v md5sum >/dev/null 2>&1; then method=md5',
                                             'elif false; then method=md5')
                self.helper.write_text(source, encoding='utf-8', newline='\n')
                self.append('digest compatibility ' + digest + '\n'); self.flush()
                value = self.export()
                self.reboot()
                self.assertEqual(self.export(), value)
                self.assertIn(digest.encode(), base64.b64decode(value['data']))
                name = next(self.journal.glob('part.*')).name.rsplit('.', 1)[1]
                self.assertEqual(name.startswith('md5-'), digest == 'md5')

    def test_empty_export_never_claims_unreadable_chunks_as_delivered(self):
        self.append('not yet delivered\n'); self.flush()
        part = next(self.journal.glob('part.*'))
        part.rename(part.with_name(part.name.rsplit('.', 1)[0] + '.invalid'))
        empty = self.export()
        self.assertEqual((empty['from'], empty['to'], empty['data']), (0, 0, ''))

    def test_failed_or_unmounted_overlay_does_not_receive_journal(self):
        for kind, flags in [('overlay', 'rw'), ('tmpfs', 'rw'), ('ubifs', 'ro')]:
            with self.subTest(kind=kind, flags=flags):
                if self.run_dir.exists(): self.reboot()
                self.mounts.write_text(f'tmpfs {self.root_mount} tmpfs rw 0 0\n'
                                       f'fake {self.flash_mount} {kind} {flags} 0 0\n')
                self.append('RAM fallback\n')
                self.assertEqual(self.status()['persistent'], '0')
                self.assertIn('Flash недоступна', self.call('read').stdout)
                self.assertFalse(self.journal.exists())

    def test_nested_tmpfs_and_symlink_do_not_pass_flash_check(self):
        nested = self.flash / 'ram'
        nested.mkdir()
        self.mounts.write_text(self.mounts.read_text() + f'tmpfs {self.flash_mount}/ram tmpfs rw 0 0\n')
        self.env['OWRT_REMOTE_LOG_DIR'] = (nested / 'logs').as_posix()
        self.append('nested tmpfs\n')
        self.assertEqual(self.status()['persistent'], '0')
        self.assertFalse((nested / 'logs').exists())

    def test_periodic_flush_and_idle_polling_do_not_rewrite_flash(self):
        self.call('check')
        state = self.journal / 'state'
        before = state.stat().st_mtime_ns
        self.append('buffered record\n')
        self.assertEqual(list(self.journal.glob('part.*')), [])
        self.assertEqual(self.export()['data'], '')
        self.call('flush', TEST_UPTIME='1059')
        self.assertEqual(list(self.journal.glob('part.*')), [])
        self.call('flush', TEST_UPTIME='1060')
        self.assertEqual(len(list(self.journal.glob('part.*'))), 1)
        self.assertNotEqual(state.stat().st_mtime_ns, before)
        stable = {p.name: p.stat().st_mtime_ns for p in self.journal.iterdir()}
        for _ in range(3):
            self.call('flush', TEST_UPTIME='1120')
            self.export()
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.journal.iterdir()}, stable)

    def test_buffer_and_number_of_archives_are_bounded_without_dropping_unacked(self):
        self.helper.write_text(self.source.replace('LOG_LIMIT=2097152', 'LOG_LIMIT=32768')
                               .replace('LOG_PART_LIMIT=256', 'LOG_PART_LIMIT=2'),
                               encoding='utf-8', newline='\n')
        for i in range(4):
            self.append(f'event {i} ' + 'x' * 4000 + '\n')
            self.flush()
        parts = sorted(self.journal.glob('part.*'))
        self.assertEqual(len(parts), 2)
        original = {p.name: p.read_bytes() for p in parts}
        for i in range(5):
            self.append(f'overflow {i} ' + 'y' * 4000 + '\n')
        self.flush()
        self.assertEqual({p.name: p.read_bytes() for p in parts}, original)
        self.assertLessEqual((self.run_dir / 'pending').stat().st_size, 16384)
        self.assertLessEqual((self.run_dir / 'facts').stat().st_size, 4096)
        first = self.export()
        self.acknowledge(first)
        self.flush()
        self.assertFalse(parts[0].exists(), 'only acknowledged chunks make room')
        self.assertIn('пропущено новых событий', self.call('read').stdout)

    def test_ack_is_monotonic_and_does_not_delete_unconfirmed_after_one_day(self):
        self.append('not delivered\n')
        self.flush()
        batch = self.export()
        self.call('flush', '', 'force', TEST_EPOCH='2000000000')
        self.assertEqual(self.export(), batch)
        invalid = self.call('ack', '', batch['journal_id'], str(batch['to'] + 1), check=False)
        self.assertNotEqual(invalid.returncode, 0)
        self.acknowledge(batch)
        self.call('flush', '', 'force', TEST_EPOCH='2000000000')
        self.assertEqual(list(self.journal.glob('part.*')), [])
        invalid = self.call('ack', '', batch['journal_id'], '0', check=False)
        self.assertNotEqual(invalid.returncode, 0)

    def test_crash_after_chunk_rename_recovers_original_id_and_offsets(self):
        self.append('committed first\n')
        self.flush()
        first = self.export()
        previous_state = (self.journal / 'state').read_bytes()
        self.append('second chunk before state checkpoint\n')
        self.flush()
        (self.journal / 'state').write_bytes(previous_state)
        (self.journal / 'chunk.new').write_text('interrupted write')
        (self.journal / 'state.new').write_text('partial state')
        self.reboot()
        self.assertEqual(self.export()['journal_id'], first['journal_id'])
        self.acknowledge(first)
        second = self.export()
        self.assertEqual(second['from'], first['to'])
        self.assertIn(b'second chunk', base64.b64decode(second['data']))
        self.assertFalse((self.journal / 'chunk.new').exists())
        self.assertFalse((self.journal / 'state.new').exists())

    def test_corrupt_chunk_is_quarantined_and_later_chunks_still_deliver(self):
        self.append('first\n'); self.flush()
        self.append('second\n'); self.flush()
        first = sorted(self.journal.glob('part.*'))[0]
        first.write_bytes(b'partial')
        self.reboot()
        batch = self.export()
        self.assertIn(b'second', base64.b64decode(batch['data']))
        self.assertTrue((self.journal / 'damaged.log').exists())
        self.assertIn('Повреждённый фрагмент', self.call('read').stdout)

    def test_disk_full_falls_back_without_touching_router_configuration(self):
        self.append('retained on flash\n'); self.flush()
        self.append('buffered before ENOSPC\n')
        self.call('flush', '', 'force', TEST_FREE_KB='100', check=False)
        self.append('after disk full\n')
        self.assertEqual(self.status()['persistent'], '0')
        self.assertIn('after disk full', self.call('read').stdout)
        self.assertTrue(list(self.journal.glob('part.*')))

    def test_confirmed_ack_can_replay_after_crash_without_changing_cursor_identity(self):
        self.append('duplicate safe\n'); self.flush()
        batch = self.export()
        self.acknowledge(batch)
        self.assertEqual(self.export()['data'], '')
        self.reboot()  # ack not yet checkpointed: safe replay, Hub suppresses it.
        self.assertEqual(self.export(), batch)

    def test_killed_flusher_reconciles_inflight_without_duplicate_data(self):
        self.append('already renamed\n')
        self.flush()
        batch = self.export()
        part = next(self.journal.glob('part.*'))
        fields = part.name.rsplit('.', 3)
        (self.run_dir / 'pending').write_bytes(base64.b64decode(batch['data']))
        (self.run_dir / 'inflight').write_text(f'{batch["journal_id"]} 0 {batch["to"]} {fields[-1]}\n')
        self.append('after killed writer\n')
        text = self.call('read').stdout
        self.assertEqual(text.count('already renamed'), 1)
        self.assertIn('after killed writer', text)
        self.assertFalse((self.run_dir / 'inflight').exists())

    def test_corrupt_state_recovers_committed_generation_and_stale_temporaries(self):
        self.append('state recovery\n'); self.flush()
        batch = self.export()
        (self.journal / 'state').write_text('partial garbage')
        self.reboot()
        self.assertEqual(self.export(), batch)
        (self.run_dir / 'export.12345').write_text('orphan')
        self.export()
        self.assertFalse(list(self.run_dir.glob('export.*')))

    def test_parallel_writers_and_stale_lock_recovery(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda i: self.append(f'concurrent {i}\n'), range(6)))
        self.assertEqual(len(self.call('read').stdout.splitlines()), 6)
        (self.run_dir / 'lock').mkdir()
        (self.run_dir / 'lock/pid').write_text('99999999')
        self.append('recovered lock\n')
        self.assertIn('recovered lock', self.call('read').stdout)
        self.assertFalse((self.run_dir / 'lock').exists())

    def test_stale_lock_reaper_itself_recovers_after_process_crash(self):
        self.call('check')
        lock = self.run_dir / 'lock'; lock.mkdir()
        (lock / 'pid').write_text('99999999')
        (lock / 'reap').mkdir(); (lock / 'reap/pid').write_text('99999998')
        self.append('after killed reaper\n')
        self.assertIn('after killed reaper', self.call('read').stdout)
        self.assertFalse(lock.exists())

    def test_collector_failure_reaps_children_and_removes_fifo(self):
        # Actual FIFO integration is tested on POSIX; Windows BusyBox has no
        # FIFO support. Exercise its traps with mocked process/FIFO operations.
        mocks = r'''
mkfifo() { : >"$1"; }
logread() { return 0; }
sleep() { return 0; }
kill() {
    printf '%s\n' "$*" >>"$RUN_DIR/kills"
    [ "$1" != -0 ]
}
wait() { return 0; }
'''
        source = self.source.replace('case "${1:-}" in', mocks + '\ncase "${1:-}" in', 1)
        self.helper.write_text(source, encoding='utf-8', newline='\n')
        result = self.call('collect', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.run_dir.glob('stream.*')))
        self.assertFalse((self.run_dir / 'collector-lock').exists())
        lines = (self.run_dir / 'kills').read_text().splitlines()
        self.assertGreaterEqual(len([line for line in lines if not line.startswith('-0')]), 2)

    def test_legacy_migration_preserves_identity_ack_and_only_undelivered_bytes(self):
        legacy = self.root / 'legacy'
        legacy.mkdir()
        (legacy / 'current.log').write_bytes(b'legacy current\n')
        (legacy / 'previous.log').write_bytes(b'legacy previous\n')
        raw = b'legacy previous\nlegacy current\n'
        (legacy / 'total').write_text(str(len(raw)))
        (legacy / 'journal-id').write_text('legacy-journal')
        (legacy / 'sync-ack').write_text('legacy-journal 16')
        (legacy / 'stream.dead').write_text('must stay transient')
        self.call('migrate')
        self.assertEqual(self.call('read').stdout, raw.decode())
        self.assertFalse((self.journal / 'stream.dead').exists())
        self.assertFalse((legacy / 'current.log').exists())
        self.assertFalse((legacy / 'total').exists())
        self.assertTrue((legacy / 'stream.dead').exists())
        batch = self.export()
        self.assertEqual(batch['journal_id'], 'legacy-journal')
        self.assertEqual(batch['from'], 16)
        self.assertEqual(base64.b64decode(batch['data']), b'legacy current\n')

    def test_unavailable_flash_legacy_migration_does_not_keep_large_ram_archive(self):
        self.mounts.write_text(f'tmpfs {self.root_mount} tmpfs rw 0 0\n')
        legacy = self.root / 'legacy'; legacy.mkdir()
        (legacy / 'current.log').write_text('old data\n' * 10000)
        self.call('migrate')
        self.assertFalse((legacy / 'current.log').exists())
        self.assertEqual(self.status()['persistent'], '0')
        self.assertLessEqual((self.run_dir / 'pending').stat().st_size, 16384)
        self.assertIn('ограниченный хвост', self.call('read').stdout)

    def test_interrupted_legacy_migration_finishes_after_old_ram_is_gone(self):
        self.append('staged persistent data\n'); self.flush()
        batch = self.export()
        staging = self.journal / 'migrating'; staging.mkdir()
        for part in self.journal.glob('part.*'):
            part.rename(staging / part.name)
        shutil.copyfile(self.journal / 'state', staging / 'state')
        (self.journal / 'state').write_text('interrupted state replacement')
        self.reboot()
        self.assertEqual(self.export(), batch)
        self.assertFalse(staging.exists())


if __name__ == '__main__':
    unittest.main()
