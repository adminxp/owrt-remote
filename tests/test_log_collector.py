"""Real FIFO lifecycle tests; MSYS provides FIFOs on Windows, BusyBox tests the applets separately."""
import os
from pathlib import Path
import subprocess
import time
import unittest

import test_persistent_logs as spool


class CollectorTests(unittest.TestCase):
    setUp = spool.PersistentLogsTests.setUp
    call = spool.PersistentLogsTests.call
    status = spool.PersistentLogsTests.status
    flush = spool.PersistentLogsTests.flush
    journal = spool.PersistentLogsTests.journal
    # Reuse filesystem fixtures without inheriting the persistent spool test cases.
    def start(self, extra=""):
        self.input = self.root / 'input'
        self.env['TEST_INPUT'] = self.input.as_posix()
        self.env['TEST_CLOCK_FILE'] = (self.root / 'clock').as_posix()
        (self.root / 'clock').write_text('1000\n')
        mocks = r'''
log_clock() { cat "$TEST_CLOCK_FILE"; }
logread() {
    exec 4<>"$TEST_INPUT"
    while :; do if IFS= read -r -t 1 item <&4; then printf '%s\n' "$item"; fi; done
}
'''
        source = self.source.replace('case "${1:-}" in', mocks + extra + '\ncase "${1:-}" in', 1)
        self.helper.write_text(source, encoding='utf-8', newline='\n')
        if not getattr(self, 'input_created', False):
            result = subprocess.run([*self.shell, '-c', 'mkfifo "$TEST_INPUT"'], env=self.env,
                                    capture_output=True, timeout=10)
            if result.returncode:
                self.skipTest('This shell has no FIFO support')
            self.input_created = True
        output = (self.root / ('process-%d.log' % len(getattr(self, 'processes', [])))).open('wb')
        proc = subprocess.Popen([*self.shell, self.helper.as_posix(), 'collect'], env=self.env,
                                stdout=output, stderr=output)
        if not hasattr(self, 'processes'):
            self.processes = []
            self.addCleanup(self.stop_processes)
        self.processes.append((proc, output))
        return proc

    def wait_for(self, predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.15)
        self.fail('Collector did not reach expected state')

    def ready(self):
        return self.wait_for(lambda: (self.run_dir / 'collector-lock/reader').exists())

    def signal(self, pid, signal='TERM'):
        return subprocess.run([*self.shell, '-c', 'kill -"$1" "$2"', '--', signal, str(pid)],
                              env=self.env, capture_output=True, timeout=10)

    def stop_processes(self):
        lock = self.run_dir / 'collector-lock/pid'
        if lock.exists():
            self.signal(lock.read_text().strip())
        for proc, output in self.processes:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            output.close()

    def send(self, *lines):
        result = subprocess.run([*self.shell, '-c', 'cat >"$TEST_INPUT"'], input='\n'.join(lines) + '\n',
                                encoding='utf-8', env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_fifo_heartbeat_freshness_sample_and_batch_flush(self):
        proc = self.start(); self.ready()
        good = 'Thu Oct 15 22:52:36 2026 user.notice owrt-remote: Heartbeat: OK; Hub=https://hub.example; WAN_direct=1; WAN_active=wan'
        self.send(good)
        self.wait_for(lambda: self.status()['accepted_events'] == '1')
        self.send(good.replace('22:52:36', '22:53:08'))
        self.wait_for(lambda: self.status()['suppressed_events'] == '1')
        state = self.status()
        self.assertEqual(state['collector_state'], 'receiving')
        self.assertEqual(state['received_lines'], '2')
        self.assertGreater(int(state['last_input_epoch']), 0)
        self.assertEqual(state['committed_bytes'], '0')
        (self.root / 'clock').write_text('1301\n')
        self.send(good.replace('22:52:36', '22:57:38'), 'owrt-remote: panel action after heartbeat')
        self.wait_for(lambda: self.status()['accepted_events'] == '3')
        read = self.call('read').stdout
        self.assertEqual(read.count('Heartbeat: OK;'), 2)
        self.assertIn('panel action after heartbeat', read)
        self.flush()
        self.assertEqual(self.status()['pending_bytes'], '0')
        self.assertIn(b'panel action after heartbeat', b''.join(p.read_bytes() for p in self.journal.glob('part.*')))
        self.assertIsNone(proc.poll())

    def test_duplicate_collect_does_not_remove_owner_fifo(self):
        proc = self.start(); self.ready()
        owner = (self.run_dir / 'collector-lock/pid').read_text()
        fifo = list(self.run_dir.glob('stream.*'))
        other = self.start(); self.assertEqual(other.wait(timeout=15), 0)
        self.assertEqual((self.run_dir / 'collector-lock/pid').read_text(), owner)
        self.assertEqual(list(self.run_dir.glob('stream.*')), fifo)
        self.send('owrt-remote: owner still receives')
        self.wait_for(lambda: self.status()['accepted_events'] == '1')
        self.assertIsNone(proc.poll())

    def test_reader_death_exits_nonzero_and_retains_journal_for_restart(self):
        proc = self.start(); self.ready()
        self.send('owrt-remote: before logread failure')
        self.wait_for(lambda: self.status()['accepted_events'] == '1')
        reader = (self.run_dir / 'collector-lock/reader').read_text().split()[0]
        self.signal(reader, 'KILL')
        self.assertNotEqual(proc.wait(timeout=20), 0)
        self.assertFalse((self.run_dir / 'collector-lock').exists())
        self.assertFalse(list(self.run_dir.glob('stream.*')))
        self.assertIn('reader-exited', self.call('diagnostics').stdout)
        self.assertIn('before logread failure', self.call('read').stdout)
        new = self.start(); self.ready()
        self.send('owrt-remote: after reader recovery')
        self.wait_for(lambda: self.status()['accepted_events'] == '2')
        self.assertIsNone(new.poll())

    def test_sigkill_owner_reaps_identified_reader_and_stale_fifo(self):
        proc = self.start(); self.ready()
        owner = (self.run_dir / 'collector-lock/pid').read_text().strip()
        self.send('owrt-remote: retained before killed collector')
        self.wait_for(lambda: self.status()['accepted_events'] == '1')
        old_reader = (self.run_dir / 'collector-lock/reader').read_text().split()[0]
        self.signal(owner, 'KILL'); proc.wait(timeout=10)
        new = self.start()
        self.wait_for(lambda: (self.run_dir / 'collector-lock/pid').exists() and
                      (self.run_dir / 'collector-lock/pid').read_text().strip() != owner)
        self.wait_for(lambda: (self.run_dir / 'collector-lock/reader').exists() and
                      (self.run_dir / 'collector-lock/reader').read_text().split()[0] != old_reader)
        self.assertEqual(len(list(self.run_dir.glob('stream.*'))), 1)
        self.send('owrt-remote: recovered collector')
        self.wait_for(lambda: self.status()['accepted_events'] == '2')
        text = self.call('read').stdout
        self.assertIn('retained before killed collector', text)
        self.assertIn('recovered collector', text)
        self.assertIsNone(new.poll())

    def test_append_failure_is_not_silently_ignored(self):
        proc = self.start('\nlog_collect_line() { return 1; }\n'); self.ready()
        self.send('owrt-remote: rejected input')
        self.assertNotEqual(proc.wait(timeout=20), 0)
        self.assertIn('collector-append-failed', self.call('diagnostics').stdout)
        self.assertEqual(self.status()['append_errors'], '1')
        self.assertFalse((self.run_dir / 'collector-lock').exists())

    def test_term_flushes_and_cleans_children_fifo_and_owner_lock(self):
        proc = self.start(); self.ready()
        self.send('owrt-remote: shutdown flush')
        self.wait_for(lambda: self.status()['accepted_events'] == '1')
        owner = (self.run_dir / 'collector-lock/pid').read_text().strip()
        self.signal(owner)
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(self.status()['pending_bytes'], '0')
        self.assertIn('shutdown flush', self.call('read').stdout)
        self.assertFalse(list(self.run_dir.glob('stream.*')))
        self.assertFalse((self.run_dir / 'collector-lock').exists())


if __name__ == '__main__':
    unittest.main()
