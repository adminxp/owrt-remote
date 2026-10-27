"""Reboot recovery, safe installation and bounded Xray downloads; no router required.

OWRT_TEST_BUSYBOX selects real BusyBox ash and its built-in applets.
"""
import gzip
import hashlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import unittest

import test_host_policy as base


class XrayBootstrapTests(unittest.TestCase):
    config = base.RouterShellTests.config

    def setUp(self):
        base.RouterShellTests.setUp(self)
        self.runtime = self.directory / 'runtime'
        self.runtime.mkdir()
        self.store = self.directory / 'flash'
        self.binary = self.runtime / 'xray'
        self.config(enabled='1', wan_direct='0', xray_bin=self.binary.as_posix())
        self.env['OWRT_REMOTE_XRAY_RUNTIME'] = self.runtime.as_posix()
        self.env['PATH'] = str(Path(base.SH).parent) + os.pathsep + os.environ['PATH']
        self.mocks = r'''
xray_cache_dir() { printf '%s' "$TEST_ROOT/flash"; }
xray_release_asset() { printf openwrt-xray_26.10.21-1_aarch64_cortex-a53.ipk; }
xray_arch() { printf aarch64; }
xray_package_extract_binary() { cp "$TEST_ROOT/release.pkg" "$2"; }
ensure_ca_certificates() { return 0; }
ensure_fetch_tool() { return 0; }
'''
        if os.name == 'nt':
            # Windows shells cannot impose Linux RLIMIT_FSIZE. Assert the
            # requested ceiling while testing downloader/watchdog separately.
            self.mocks += r'''
ulimit() { [ "$1 $2" = '-f 32768' ]; }
'''
        if os.environ.get('OWRT_TEST_BUSYBOX') and os.name == 'nt':
            # busybox-w32 has no Linux /proc. Real Linux process identities are
            # exercised separately by MSYS, including two concurrent installers.
            self.mocks += r'''
xray_process_identity() { XRAY_SELF_PID=$$; XRAY_SELF_BIRTH=1; }
xray_process_start() { [ "$1" != "$$" ] || printf 1; }
'''

    def shell(self, commands, mocks='', **env):
        shell = os.environ.get('OWRT_TEST_BUSYBOX', base.SH)
        args = [shell, 'ash'] if 'busybox' in shell.lower() else [shell]
        script = self.directory / 'run.sh'
        script.write_text(self.agent + '\n' + base.MOCKS + '\n' + self.mocks + '\n' + mocks + '\n' + commands,
                          encoding='utf-8', newline='\n')
        return subprocess.run(args + [script.as_posix()], capture_output=True, encoding='utf-8',
                              env={**self.env, **env}, timeout=40)

    def raw_shell(self, commands, mocks='', **env):
        shell = os.environ.get('OWRT_TEST_BUSYBOX', base.SH)
        args = [shell, 'ash'] if 'busybox' in shell.lower() else [shell]
        script = self.directory / 'raw-run.sh'
        script.write_text(self.agent + '\n' + base.MOCKS + '\n' + mocks + '\n' + commands,
                          encoding='utf-8', newline='\n')
        return subprocess.run(args + [script.as_posix()], capture_output=True, encoding='utf-8',
                              env={**self.env, **env}, timeout=40)

    def make_binary(self, text='Xray test version'):
        data = f'#!/bin/sh\ncase "$1" in version) echo "{text}" ;; *) exit 0 ;; esac\n'.encode()
        self.binary.write_bytes(data)
        self.binary.chmod(0o755)
        return data

    def test_yichya_asset_uses_openwrt_opkg_architecture(self):
        result = self.raw_shell('xray_release_asset', mocks=r'''
opkg() {
    [ "$1" = print-architecture ] || return 1
    printf '%s\n' 'arch all 1' 'arch noarch 1' 'arch aarch64_cortex-a53 10'
}
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1],
                         'openwrt-xray_26.10.21-1_aarch64_cortex-a53.ipk')

    def test_yichya_ipk_extracts_only_usr_bin_xray(self):
        binary = self.make_binary('package')
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode='w') as archive:
            entry = tarfile.TarInfo('./usr/bin/xray')
            entry.mode = 0o755
            entry.size = len(binary)
            archive.addfile(entry, io.BytesIO(binary))
        data = gzip.compress(inner.getvalue())
        outer = io.BytesIO()
        with tarfile.open(fileobj=outer, mode='w') as archive:
            entry = tarfile.TarInfo('./data.tar.gz')
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        (self.directory / 'release.pkg').write_bytes(gzip.compress(outer.getvalue()))
        result = self.raw_shell('xray_package_extract_binary "$TEST_ROOT/release.pkg" "$TEST_ROOT/extracted"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'extracted').read_bytes(), binary)

    def cache(self, data=None):
        data = data or self.make_binary()
        self.store.mkdir(exist_ok=True)
        path = self.store / ('openwrt-xray_26.10.21-1_aarch64_cortex-a53.ipk.' + hashlib.sha256(data).hexdigest() + '.gz')
        path.write_bytes(gzip.compress(data))
        return path

    def assert_clean(self):
        self.assertFalse((self.runtime / 'bootstrap-lock').exists())
        self.assertFalse(list(self.runtime.glob('*.new')))
        self.assertFalse(list(self.runtime.glob('.restore.*')))

    def test_reboot_restores_verified_cache_without_network_or_flash_rewrite(self):
        data = self.make_binary()
        result = self.shell('prepare_xray')
        self.assertEqual(result.returncode, 0, result.stderr)
        cached = list(self.store.glob('*.gz'))
        self.assertEqual(len(cached), 1)
        before = cached[0].stat().st_mtime_ns
        self.binary.unlink()  # /tmp disappears on reboot; Flash survives.
        result = self.shell('ensure_xray_available', mocks='fetch_file() { echo forbidden-network >&2; return 99; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.binary.read_bytes(), data)
        self.assertEqual(cached[0].stat().st_mtime_ns, before)
        self.assertIn('без загрузки из интернета', result.stderr)
        self.assert_clean()

    def test_unchanged_binary_does_not_rewrite_flash(self):
        self.make_binary()
        self.assertEqual(self.shell('prepare_xray').returncode, 0)
        cached = next(self.store.glob('*.gz'))
        before = cached.stat().st_mtime_ns
        result = self.shell('prepare_xray')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cached.stat().st_mtime_ns, before)
        self.assertNotIn('копия сохранена', result.stderr)
        self.assert_clean()

    def test_corrupt_or_wrong_digest_cache_is_never_executed(self):
        cached = self.cache()
        self.binary.unlink()
        cached.write_bytes(b'broken gzip')
        result = self.shell('ensure_xray_available', mocks='fetch_file() { return 6; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.binary.exists())
        cached.write_bytes(gzip.compress(b'#!/bin/sh\ntouch "$TEST_ROOT/executed"\n'))
        result = self.shell('ensure_xray_available', mocks='fetch_file() { return 6; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / 'executed').exists())
        self.assert_clean()

    def test_download_failure_preserves_working_binary_and_cache(self):
        data = self.make_binary()
        cached = self.cache(data)
        result = self.shell('install_xray_tmp', mocks='fetch_file() { echo partial >"$2"; return 6; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.binary.read_bytes(), data)
        self.assertEqual(gzip.decompress(cached.read_bytes()), data)
        self.assert_clean()

    def test_invalid_package_preserves_working_binary_and_cache(self):
        data = self.make_binary()
        cached = self.cache(data)
        result = self.shell('install_xray_tmp', mocks='fetch_file() { echo invalid >"$2"; }; xray_package_extract_binary() { return 1; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.binary.read_bytes(), data)
        self.assertTrue(cached.exists())
        self.assert_clean()

    def test_successful_install_validates_then_caches_one_copy(self):
        old = self.make_binary('old')
        cached = self.cache(old)
        new = self.make_binary('new')
        self.binary.write_bytes(old)
        (self.directory / 'release.pkg').write_bytes(new)
        result = self.shell('install_xray_tmp', mocks='fetch_file() { cp "$TEST_ROOT/release.pkg" "$2"; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.binary.read_bytes(), new)
        self.assertFalse(cached.exists())
        self.assertEqual(len(list(self.store.glob('*.gz'))), 1)
        self.assert_clean()

    def test_full_or_unmounted_flash_never_breaks_existing_xray(self):
        self.make_binary()
        for mock in ('xray_cache_dir() { return 1; }',
                     'df() { printf "Filesystem blocks used available capacity mount\\nflash 100 99 1 99%% /overlay\\n"; }'):
            result = self.shell('prepare_xray', mocks=mock)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.binary.exists())
            self.assertFalse(list(self.store.glob('*.gz')))
        self.assert_clean()

    def test_mount_detection_rejects_tmpfs_readonly_and_unmounted_overlay(self):
        # Call the real selector, without creating any system directories.
        selector = self.agent.split('xray_cache_dir() {', 1)[1].split('\nxray_digest()', 1)[0]
        selector = 'xray_cache_dir() {' + selector
        for mount, fs, flags, expected in (
            ('/overlay', 'ubifs', 'rw,relatime', '/overlay/owrt-remote/xray-cache'),
            ('/overlay', 'tmpfs', 'rw', None),
            ('/overlay', 'ubifs', 'ro', None),
            ('/', 'overlay', 'rw', None),
            ('/', 'ext4', 'rw', '/etc/owrt-remote/xray-cache'),
        ):
            result = self.shell('xray_cache_dir', mocks=selector + '\nxray_cache_mount() { printf "%s\\n" ' +
                                shlex.quote(f'{mount} {fs} {flags}') + '; }')
            self.assertEqual(result.returncode == 0, expected is not None, result.stderr)
            if expected: self.assertEqual(result.stdout, expected)

    def test_managed_bootstrap_survives_attempt_limit_but_stops_when_disabled(self):
        mocks = r'''
ensure_xray_available() { tries=$((tries+1)); [ "$tries" -ge 4 ]; }
wait_for_selected_wan() { return 0; }
sleep() { printf '%s\n' "$1" >>"$TEST_ROOT/delays"; }
'''
        result = self.shell('tries=0; ensure_xray_available_with_retry 2 10; echo tries=$tries', mocks=mocks,
                            OWRT_REMOTE_SERVICE_LOG='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tries=4', result.stdout)
        self.assertEqual((self.directory / 'delays').read_text().splitlines(), ['10', '60', '60'])
        self.config(enabled='0')
        result = self.shell('ensure_xray_available_with_retry', mocks=mocks, OWRT_REMOTE_SERVICE_LOG='1')
        self.assertNotEqual(result.returncode, 0)

    def test_manual_attempt_limit_is_finite(self):
        result = self.shell('tries=0; ensure_xray_available_with_retry 2 0; rc=$?; echo tries=$tries; exit "$rc"',
                            mocks='ensure_xray_available() { tries=$((tries+1)); return 6; }; sleep() { :; }',
                            OWRT_REMOTE_SERVICE_LOG='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('tries=2', result.stdout)

    def test_stale_lock_and_abandoned_reaper_are_recovered(self):
        lock = self.runtime / 'bootstrap-lock'
        (lock / 'reap').mkdir(parents=True)
        (lock / 'owner').write_text('999999 123\n')
        (lock / 'reap/owner').write_text('999998 456\n')
        self.cache()
        self.binary.unlink()
        result = self.shell('ensure_xray_available')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.binary.exists())
        self.assert_clean()

    @unittest.skipIf(os.name == 'nt' and bool(os.environ.get('OWRT_TEST_BUSYBOX')),
                     'busybox-w32 has no /proc process identities; exercised with MSYS')
    def test_two_concurrent_preparations_download_once_and_release_lock(self):
        data = self.make_binary()
        self.binary.unlink()
        (self.directory / 'release.pkg').write_bytes(data)
        script = self.directory / 'parallel.sh'
        script.write_text(self.agent + '\n' + base.MOCKS + '\n' + self.mocks + r'''
fetch_file() { echo download >>"$TEST_ROOT/downloads"; sleep 2; cp "$TEST_ROOT/release.pkg" "$2"; }
ensure_xray_available
''', encoding='utf-8', newline='\n')
        processes = [subprocess.Popen([base.SH, script.as_posix()], env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, encoding='utf-8') for _ in range(2)]
        try:
            for process in processes:
                out, err = process.communicate(timeout=25)
                self.assertEqual(process.returncode, 0, err)
        finally:
            for process in processes:
                if process.poll() is None: process.kill(); process.wait()
        self.assertEqual((self.directory / 'downloads').read_text().splitlines(), ['download'])
        self.assertEqual(self.binary.read_bytes(), data)
        self.assert_clean()

    CURL_MOCK = r'''
remote_clock_ms() { printf 1000; }
wan_device() { printf wan; }
wan_resolve_host() { echo "$1" >>"$TEST_ROOT/resolved"; printf 192.0.2.42; }
curl() {
    local output previous arg url
    printf '%s\n' "$*" >>"$TEST_ROOT/fetches"
    [ "${http_proxy-unset}|${https_proxy-unset}" = 'unset|unset' ] || return 90
    previous=''
    for arg in "$@"; do [ "$previous" != -o ] || output="$arg"; previous="$arg"; url="$arg"; done
    case "$url" in
        https://github.com/*)
            echo redirect-body >"$output"
            printf '302 %s\n' "${TEST_REDIRECT:-https://release-assets.githubusercontent.com/test.zip}" ;;
        *) echo archive >"$output"; printf '200 \n' ;;
    esac
}
'''

    def test_each_download_redirect_uses_wan_dns_and_keeps_tls_verification(self):
        self.config(wan_direct='1')
        result = self.shell('fetch_file https://github.com/yichya/openwrt-xray/releases/download/v26.10.21/openwrt-xray_26.10.21-1_aarch64_cortex-a53.ipk "$TEST_ROOT/download"',
                            mocks=self.CURL_MOCK, http_proxy='', https_proxy='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'resolved').read_text().splitlines(),
                         ['github.com', 'release-assets.githubusercontent.com'])
        calls = (self.directory / 'fetches').read_text().splitlines()
        for call, host in zip(calls, ('github.com', 'release-assets.githubusercontent.com')):
            self.assertIn('--connect-timeout 10', call)
            self.assertIn('--max-time 120.000', call)
            self.assertIn('--resolve ' + host + ':443:192.0.2.42', call)
            self.assertIn('--interface wan', call)
            self.assertNotIn('--insecure', call)
            self.assertNotIn(' -L ', call)
        self.assertFalse(list(self.directory.glob('owrt-xray-http.*')))

    def test_download_rejects_insecure_redirect_and_cleans_partial_body(self):
        result = self.shell('fetch_file https://github.com/test "$TEST_ROOT/download"', mocks=self.CURL_MOCK,
                            TEST_REDIRECT='http://release-assets.githubusercontent.com/test.zip')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / 'download').exists())
        self.assertEqual(len((self.directory / 'fetches').read_text().splitlines()), 1)

    def test_download_timeout_removes_partial_data_and_preserves_error(self):
        result = self.shell('fetch_file https://github.com/test "$TEST_ROOT/download"', mocks=r'''
curl() { local arg previous; previous=''; for arg in "$@"; do [ "$previous" != -o ] || echo partial >"$arg"; previous="$arg"; done; return 28; }
''')
        self.assertEqual(result.returncode, 28, result.stderr)
        self.assertFalse((self.directory / 'download').exists())
        self.assertFalse(list(self.directory.glob('owrt-xray-http.*')))

    def test_native_fetch_works_without_optional_timeout_applet(self):
        mocks = r'''
command() {
    if [ "$1" = -v ]; then
        case "$2" in curl|timeout) return 1 ;; uclient-fetch) return 0 ;; esac
    fi
    builtin command "$@"
}
uclient-fetch() {
    local previous arg
    previous=''
    for arg in "$@"; do [ "$previous" != -O ] || printf archive >"$arg"; previous="$arg"; done
}
'''
        result = self.shell('fetch_file https://github.com/test "$TEST_ROOT/download"', mocks=mocks)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'download').read_text(), 'archive')

    def test_native_deadline_kills_client_and_removes_partial_file(self):
        result = self.shell('xray_native_fetch "$TEST_ROOT/partial" slow_client', mocks=r'''
slow_client() { while :; do :; done; }
sleep() { return 0; }
''')
        self.assertEqual(result.returncode, 28, result.stderr)
        self.assertFalse((self.directory / 'partial').exists())

    @unittest.skipIf(os.name == 'nt' and bool(os.environ.get('OWRT_TEST_BUSYBOX')),
                     'busybox-w32 has no /proc; watchdog child identities tested in MSYS')
    def test_native_client_is_reaped_after_success_or_failure(self):
        result = self.shell(r'''
client() {
    read -r pid rest </proc/self/stat
    echo "$pid" >>"$TEST_ROOT/clients"
    command sleep 1
    printf data >"$TEST_ROOT/download"
    return "${TEST_CLIENT_RC:-0}"
}
xray_native_fetch "$TEST_ROOT/download" client
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('clients',):
            pids = (self.directory / name).read_text().splitlines()
            check = self.shell('\n'.join('if kill -0 ' + pid + ' 2>/dev/null; then exit 1; fi' for pid in pids) + '\ntrue')
            self.assertEqual(check.returncode, 0, name + ' still alive: ' + check.stderr)
        result = self.shell('xray_native_fetch "$TEST_ROOT/partial" false')
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__': unittest.main()
