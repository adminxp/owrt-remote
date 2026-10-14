"""Exercise the real router CGI POST with LF/CRLF and a strict POSIX shell."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
STATE = tempfile.TemporaryDirectory(prefix="owrt-xray-apply-")
spec = importlib.util.spec_from_file_location("xray_apply_hub", ROOT / "vps/owrt-remote-hub.py")
hub = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hub
with mock.patch.dict(os.environ, {"OWRT_REMOTE_STATE_DIR": STATE.name}):
    spec.loader.exec_module(hub)

SH = shutil.which("sh")
STRICT_SH = shutil.which("dash") or SH
if os.name == "nt":
    git_bin = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin"
    SH = str(git_bin / "sh.exe") if (git_bin / "sh.exe").exists() else SH
    STRICT_SH = str(git_bin / "dash.exe") if (git_bin / "dash.exe").exists() else STRICT_SH
else:
    git_bin = None


@unittest.skipUnless(SH and STRICT_SH, "POSIX sh and a strict parser are required")
class XrayApplyPostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=STATE.name)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.directory.joinpath("key").write_text("test-web-key", encoding="utf-8")
        tools = self.directory / "bin"
        tools.mkdir()
        tools.joinpath("owrt-remote").write_text(
            '#!/bin/sh\n[ "$1" != render-client ] || [ "${RENDER_FAIL:-0}" = 0 ] || exit 7\n'
            'printf "%s\\n" "$*" >>"$TEST_ROOT/operations"\n', encoding="utf-8", newline="\n")
        tools.joinpath("owrt-remote").chmod(0o755)
        tools.joinpath("uci").write_text(
            '#!/bin/sh\n[ "$1" != -q ] || shift\n'
            '[ "$#" = 2 ] || exit 255\n'
            'case "$1" in\n'
            '  get) exit 1 ;;\n'
            '  set) printf "%s\\n" "$2" >>"$TEST_ROOT/uci-values" ;;\n'
            '  commit) printf "commit\\n" >>"$TEST_ROOT/operations" ;;\n'
            '  *) exit 255 ;;\nesac\n', encoding="utf-8", newline="\n")
        tools.joinpath("uci").chmod(0o755)
        self.env = {**os.environ, "TMPDIR": self.directory.as_posix(),
                    "TEST_ROOT": self.directory.as_posix(), "KEY_FILE": (self.directory / "key").as_posix(),
                    "TEST_STRICT_SH": Path(STRICT_SH).as_posix()}
        if git_bin:
            self.env["PATH"] = str(git_bin) + os.pathsep + self.env["PATH"]
        self.env["PATH"] = str(tools) + os.pathsep + self.env["PATH"]
        cgi = (ROOT / "files/www/cgi-bin/owrt-remote").read_text(encoding="utf-8")
        mocks = r'''
sh() {
    if [ "$1" = -n ]; then
        cp "$2" "$TEST_ROOT/checked.sh"
        printf 'check\n' >>"$TEST_ROOT/calls"
    else
        cp "$2" "$TEST_ROOT/executed.sh"
        printf 'execute\n' >>"$TEST_ROOT/calls"
    fi
    "$TEST_STRICT_SH" "$@"
}
render_page() { printf 'KIND=%s\nTITLE=%s\nBODY=%s\nDRAFT=%s\n' "$RESULT_KIND" "$RESULT_TITLE" "$RESULT_BODY" "$XRAY_SETTINGS_DRAFT"; }
'''
        self.cgi = self.directory / "cgi"
        self.cgi.write_text(cgi.replace("\nread_request\n", mocks + "\nread_request\n", 1), encoding="utf-8", newline="\n")
        self.render_cgi = self.directory / "render-cgi"
        template = cgi.split('\tif [ "$SHOW_XRAY_SETTINGS" = "1" ]; then\n', 1)[1].split('\n\tfi\n', 1)[0]
        render = '\nrender_page() {\nkey="$(param key)"\n' + template + '\n}\n'
        self.render_cgi.write_text(cgi.replace("\nread_request\n", render + "\nread_request\n", 1), encoding="utf-8", newline="\n")

    def post(self, script, key="test-web-key", render=False, action="apply_xray_settings"):
        body = urlencode({"key": key, "action": action, "xray_settings_script": script}).encode("ascii")
        result = subprocess.run([SH, (self.render_cgi if render else self.cgi).as_posix()], input=body, capture_output=True,
                                env={**self.env, "REQUEST_METHOD": "POST", "CONTENT_TYPE": "application/x-www-form-urlencoded",
                                     "CONTENT_LENGTH": str(len(body))}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertFalse(list(self.directory.glob("owrt-remote-*")), "CGI temporary files must be removed")
        return result.stdout.decode("utf-8")

    def test_mobile_smart_quotes_reproduce_uci_255_and_are_repaired(self):
        script = 'uci set owrtremote.main.router_name=‘Дом у моря’\nuci commit owrtremote\n'
        # UCI returns 255 for too many arguments; shell syntax validation alone
        # accepts these typography quotes and splits the name into three words.
        broken = self.directory / "broken.sh"
        broken.write_text(script, encoding="utf-8", newline="\n")
        result = subprocess.run([STRICT_SH, "-e", broken.as_posix()], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 255)
        response = self.post(script.replace("\n", "\r\n"))
        self.assertIn("KIND=ok", response)
        self.assertEqual(self.directory.joinpath("uci-values").read_text(encoding="utf-8"),
                         "owrtremote.main.router_name=Дом у моря\n")

    def test_mobile_quotes_bom_and_nonbreaking_separators(self):
        script = '\ufeffuci\u00a0-q\u202fset\u00a0owrtremote.main.router_name=“Папа\'s $HOME `id` & дом”\r\n'
        self.assertIn("KIND=ok", self.post(script))
        self.assertEqual(self.directory.joinpath("uci-values").read_text(encoding="utf-8"),
                         "owrtremote.main.router_name=Папа's $HOME `id` & дом\n")

    def test_ascii_shell_quotes_preserve_unicode_values(self):
        value = "Дом ‘у моря’\u00a0— “офис”\u202f+ 100% & $HOME `id`"
        script = "uci set owrtremote.main.router_name='" + value + "'\n"
        self.assertIn("KIND=ok", self.post(script))
        self.assertEqual(self.directory.joinpath("uci-values").read_text(encoding="utf-8"),
                         "owrtremote.main.router_name=" + value + "\n")
        self.assertEqual(self.directory.joinpath("executed.sh").read_text(encoding="utf-8"),
                         "#!/bin/sh\n" + script)

    def test_genuine_uci_error_is_not_reported_as_success_and_keeps_draft(self):
        script = 'uci set owrtremote.main.router_name=broken name\nuci commit owrtremote\n'
        response = self.post(script)
        self.assertIn("KIND=bad", response)
        self.assertIn("rc=255", response)
        self.assertIn("DRAFT=" + script.rstrip("\n"), response)
        self.assertFalse(self.directory.joinpath("operations").exists())

    def test_mobile_form_disables_text_rewriting_and_escapes_failed_draft(self):
        # Render the actual CGI textarea template on an unsuccessful POST.
        script = "printf '%s' '</textarea><script>bad</script>&'\nexit 255\n"
        response = self.post(script, render=True)
        self.assertIn('autocorrect="off" autocapitalize="off" autocomplete="off"', response)
        self.assertIn('wrap="soft" spellcheck="false"', response)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;bad&lt;/script&gt;&amp;", response)
        self.assertNotIn("</textarea><script>bad</script>", response)
        cleared = self.post(script, render=True, action="show_xray_settings")
        self.assertNotIn("&lt;script&gt;bad&lt;/script&gt;", cleared)

    def test_uci_looking_lines_in_multiline_values_and_heredocs_stay_literal(self):
        value = 'before\nuci set owrtremote.main.router_name=‘Дом у моря’\nafter\n'
        for script in ("printf '%s' '" + value + "' >\"$TEST_ROOT/literal\"\n",
                       'cat >"$TEST_ROOT/literal" <<\'EOF\'\n' + value + 'EOF\n'):
            with self.subTest(script=script):
                self.assertIn("KIND=ok", self.post(script))
                self.assertEqual(self.directory.joinpath("executed.sh").read_bytes(),
                                 ("#!/bin/sh\n" + script).encode("utf-8"))
                self.assertFalse(self.directory.joinpath("uci-values").exists())

    def test_form_crlf_is_normalized_before_strict_shell_parsing(self):
        script = "if [ x = x ]; then\n  printf '%s' 'name + percent% & quote\"'\nfi\n"
        response = self.post(script.replace("\n", "\r\n"))
        self.assertIn("KIND=ok", response)
        self.assertIn('BODY=name + percent% & quote"', response)
        self.assertNotIn(b"\r", self.directory.joinpath("checked.sh").read_bytes())
        self.assertNotIn(b"\r", self.directory.joinpath("executed.sh").read_bytes())
        self.assertEqual(self.directory.joinpath("calls").read_text(), "check\nexecute\n")

    def test_lf_and_crlf_heredoc_keep_lines_and_blank_lines(self):
        script = 'cat >"$TEST_ROOT/applied" <<\'EOF\'\nfirst\n\nlast + % &\nEOF\n'
        for ending in ("\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                self.assertIn("KIND=ok", self.post(script.replace("\n", ending)))
                self.assertEqual(self.directory.joinpath("applied").read_bytes(), b"first\n\nlast + % &\n")

    def test_incomplete_script_is_rejected_before_any_changes(self):
        response = self.post('printf changed >"$TEST_ROOT/applied"\nif [ x = x ]; then\n')
        self.assertIn("KIND=bad", response)
        self.assertIn("rc=2", response)
        self.assertIn("Настройки не изменены", response)
        self.assertFalse(self.directory.joinpath("applied").exists())
        self.assertFalse(self.directory.joinpath("executed.sh").exists())

    def test_failed_render_stops_before_later_restart_or_heartbeat(self):
        self.env["RENDER_FAIL"] = "1"
        script = 'owrt-remote render-client\nprintf restarted >"$TEST_ROOT/restarted"\n'
        response = self.post(script)
        self.assertIn("KIND=bad", response)
        self.assertIn("rc=7", response)
        self.assertFalse(self.directory.joinpath("restarted").exists())

    def test_runtime_rc_one_does_not_blame_mobile_command_format(self):
        response = self.post("printf '%s\\n' 'Real command failure' >&2\nexit 1\n")
        self.assertIn("KIND=bad", response)
        self.assertIn("rc=1", response)
        self.assertIn("Real command failure", response)
        self.assertNotIn("каждая команда должна быть на отдельной строке", response)

    def test_xray_and_heartbeat_failures_have_distinct_stages_and_keep_draft(self):
        for stage in ("render-client", "heartbeat"):
            with self.subTest(stage=stage):
                script = ('printf "%s\\n" "' + stage + '" >>"$OWRT_REMOTE_APPLY_TRACE"\n'
                          "printf '%s\\n' 'Failed to allocate uclient context' >&2\nexit 1\n")
                response = self.post(script.replace("\n", "\r\n"))
                self.assertIn("KIND=bad", response)
                self.assertIn("rc=1", response)
                self.assertIn("Failed to allocate uclient context", response)
                self.assertIn("DRAFT=" + script.rstrip("\n"), response)
                self.assertNotIn("каждая команда должна быть на отдельной строке", response)
                if stage == "heartbeat":
                    self.assertIn("Ошибка отправки Heartbeat после применения настроек", response)
                    self.assertNotIn("Ошибка проверки конфигурации Xray", response)
                else:
                    self.assertIn("Ошибка проверки конфигурации Xray", response)
                    self.assertIn("Рабочий файл конфигурации сохранён", response)

    def test_real_agent_heartbeat_failure_is_labeled_without_blaming_xray(self):
        agent = (ROOT / "files/usr/sbin/owrt-remote").read_text(encoding="utf-8")
        mocks = r'''
uci_get() {
    case "$1" in hub_url) printf https://hub.example ;; hub_token) printf test-token ;; vps_host) printf hub.example ;; *) printf '%s' "$2" ;; esac
}
heartbeat_json() { printf '{"id":"test-router"}'; }
post_json() { echo 'Failed to allocate uclient context' >&2; return 1; }
logger() { :; }
'''
        tools = self.directory / "bin"
        tools.joinpath("owrt-remote").write_text(agent.replace('cmd="${1:-help}"', mocks + '\ncmd="${1:-help}"', 1),
                                               encoding="utf-8", newline="\n")
        tools.joinpath("owrt-remote").chmod(0o755)
        script = 'printf restarted >"$TEST_ROOT/restarted"\nowrt-remote heartbeat\n'
        response = self.post(script.replace("\n", "\r\n"))
        self.assertIn("KIND=bad", response)
        self.assertIn("Ошибка отправки Heartbeat после применения настроек", response)
        self.assertIn("Failed to allocate uclient context", response)
        self.assertNotIn("каждая команда должна быть на отдельной строке", response)
        self.assertTrue(self.directory.joinpath("restarted").exists())

    @unittest.skipUnless(os.environ.get("OWRT_TEST_XRAY_BIN"), "Set OWRT_TEST_XRAY_BIN to exercise real Xray")
    def test_mobile_post_shows_real_xray_error_before_restart_and_keeps_json(self):
        target = self.directory / "working.json"
        target.write_text('{"previous":true}\n', encoding="utf-8")
        self.env["TEST_XRAY_BIN"] = Path(os.environ["OWRT_TEST_XRAY_BIN"]).as_posix()
        agent = (ROOT / "files/usr/sbin/owrt-remote").read_text(encoding="utf-8")
        mocks = r'''
uci_get() {
    case "$1" in
        vps_host) printf hub.example ;; vless_uuid) printf 11111111-1111-4111-8111-111111111111 ;;
        ssh_vless_uuid) printf 22222222-2222-4222-8222-222222222222 ;;
        xray_config) printf '%s/working.json' "$TEST_ROOT" ;; xray_bin) printf '%s' "$TEST_XRAY_BIN" ;;
        vless_flow) printf not-supported ;; *) printf '%s' "$2" ;;
    esac
}
ensure_xray_available() { return 0; }
logger() { :; }
'''
        executable = self.directory / "bin/owrt-remote"
        executable.write_text(agent.replace('cmd="${1:-help}"', mocks + '\ncmd="${1:-help}"', 1),
                              encoding="utf-8", newline="\n")
        executable.chmod(0o755)
        script = 'owrt-remote render-client\nprintf restarted >"$TEST_ROOT/restarted"\n'
        response = self.post(script.replace("\n", "\r\n"))
        self.assertIn("KIND=bad", response)
        self.assertIn("Ошибка проверки конфигурации Xray", response)
        self.assertIn('VLESS users: "flow"', response)
        self.assertIn("not-supported", response)
        self.assertIn("rc=23", response)
        self.assertFalse(self.directory.joinpath("restarted").exists())
        self.assertEqual(target.read_text(), '{"previous":true}\n')
        self.assertFalse(list(self.directory.glob(".owrt-remote-client.*")))

    def test_empty_and_unauthorized_form_do_not_execute_commands(self):
        self.assertIn("KIND=warn", self.post(" \r\n\t\r\n"))
        self.assertFalse(self.directory.joinpath("calls").exists())
        response = self.post('printf changed >"$TEST_ROOT/applied"', key="wrong-key")
        self.assertIn("Доступ запрещен", response)
        self.assertFalse(self.directory.joinpath("applied").exists())
        self.assertFalse(self.directory.joinpath("calls").exists())

    @unittest.skipUnless(os.environ.get("OWRT_TEST_XRAY_BIN"), "Set OWRT_TEST_XRAY_BIN to exercise real Xray")
    def test_full_mobile_hub_config_with_classless_openwrt_tr_and_real_xray(self):
        from test_xray_validation import ADMIN_UUID, SSH_UUID, CLASSLESS_TR

        self.env["TEST_XRAY_BIN"] = Path(os.environ["OWRT_TEST_XRAY_BIN"]).as_posix()
        uci_function = r'''
uci() {
    [ "$1" != -q ] || shift
    case "$1" in
        get)
            [ "$2" != owrtremote.main ] || { echo remote; return; }
            awk -F= -v key="$2" '$1 == key { sub(/^[^=]*=/, ""); print; found=1; exit } END { if(!found) exit 1 }' "$TEST_ROOT/uci" ;;
        set)
            key="${2%%=*}"; value="${2#*=}"
            awk -F= -v key="$key" '$1 != key' "$TEST_ROOT/uci" >"$TEST_ROOT/uci.new"
            printf '%s=%s\n' "$key" "$value" >>"$TEST_ROOT/uci.new"
            mv "$TEST_ROOT/uci.new" "$TEST_ROOT/uci" ;;
        commit) printf 'commit\n' >>"$TEST_ROOT/operations" ;;
    esac
}
'''
        self.directory.joinpath("uci").write_text("", encoding="utf-8")
        self.directory.joinpath("bin/uci").write_text("#!/bin/sh\n" + uci_function + '\nuci "$@"\n',
                                                  encoding="utf-8", newline="\n")
        # Both CGI and its separate agent process see a BusyBox-like tr.
        self.directory.joinpath("bin/tr").write_text("#!/bin/sh\n" + CLASSLESS_TR.replace("command tr", "command /usr/bin/tr") + '\ntr "$@"\n',
                                                 encoding="utf-8", newline="\n")
        self.directory.joinpath("bin/tr").chmod(0o755)
        agent = (ROOT / "files/usr/sbin/owrt-remote").read_text(encoding="utf-8")
        mocks = r'''
uci_get_original() {
__UCI_GET_BODY__
}
uci_get() {
    case "$1" in
        xray_config) printf '%s/working.json' "$TEST_ROOT" ;;
        xray_bin) printf '%s' "$TEST_XRAY_BIN" ;;
        *) uci_get_original "$@" ;;
    esac
}
ensure_xray_available() { return 0; }
send_heartbeat() { printf 'heartbeat\n' >>"$TEST_ROOT/operations"; }
logger() { :; }
'''.replace("__UCI_GET_BODY__", agent.split("uci_get() {\n", 1)[1].split("\n}\n", 1)[0])
        executable = self.directory / "bin/owrt-remote"
        executable.write_text(agent.replace('cmd="${1:-help}"', CLASSLESS_TR + mocks + '\ncmd="${1:-help}"', 1),
                              encoding="utf-8", newline="\n")
        executable.chmod(0o755)
        self.directory.joinpath("working.json").write_text('{"previous":true}\n', encoding="utf-8")
        row = {"id": "main", "name": "test", "vps_host": "hub.example", "reverse_tag": "reverse-in",
               "ssh_reverse_tag": "reverse-in-ssh", "vless_uuid": ADMIN_UUID, "ssh_vless_uuid": SSH_UUID}
        with mock.patch.object(hub, "agent_token", return_value="test-agent-token"):
            generated = hub.make_openwrt_config(row, "https://hub.example")
        generated = generated.replace("/etc/init.d/owrt-remote", "test_service")
        response = self.post(('test_service() { printf "service %s\\n" "$*" >>"$TEST_ROOT/operations"; }\n' + generated).replace("\n", "\r\n"))
        self.assertIn("KIND=ok", response)
        self.assertIn("Configuration OK", response)
        config = json.loads(self.directory.joinpath("working.json").read_text())
        self.assertEqual(config["outbounds"][2]["settings"]["reverse"]["tag"], "reverse-in")
        self.assertEqual(config["outbounds"][3]["settings"]["reverse"]["tag"], "reverse-in-ssh")
        self.assertEqual(config["outbounds"][2]["settings"]["address"], "hub.example")
        self.assertEqual(self.directory.joinpath("operations").read_text(), "commit\nservice enable\nservice restart\nheartbeat\n")
        self.assertFalse(list(self.directory.glob(".owrt-remote-client.*")))
        self.directory.joinpath("operations").unlink()
        self.assertIn("KIND=warn", self.post(" \r\n\t\v\f\r\n"))
        self.assertFalse(self.directory.joinpath("operations").exists())

    def test_full_hub_config_through_form_preserves_manual_host_and_wan(self):
        self.directory.joinpath("uci").write_text("owrtremote.main.vps_host=192.0.2.25\nowrtremote.main.wan_interface=wwan\n", encoding="utf-8")
        mocks = r'''
uci() {
    [ "$1" != -q ] || shift
    case "$1" in
        get)
            [ "$2" != owrtremote.main ] || { echo remote; return; }
            awk -F= -v key="$2" '$1 == key { sub(/^[^=]*=/, ""); print; found=1; exit } END { if(!found) exit 1 }' "$TEST_ROOT/uci" ;;
        set)
            key="${2%%=*}"; value="${2#*=}"
            awk -F= -v key="$key" '$1 != key' "$TEST_ROOT/uci" >"$TEST_ROOT/uci.new"
            printf '%s=%s\n' "$key" "$value" >>"$TEST_ROOT/uci.new"
            mv "$TEST_ROOT/uci.new" "$TEST_ROOT/uci" ;;
        commit) printf 'commit\n' >>"$TEST_ROOT/operations" ;;
    esac
}
test_service() { printf 'service %s\n' "$*" >>"$TEST_ROOT/operations"; }
'''
        row = {"id": "main", "name": "test + quote's & name", "vps_host": "hub.example", "reverse_tag": "reverse-in"}
        with mock.patch.object(hub, "agent_token", return_value="test-agent-token"):
            generated = hub.make_openwrt_config(row, "https://hub.example")
        generated = generated.replace("/etc/init.d/owrt-remote", "test_service")
        response = self.post((mocks + generated).replace("\n", "\r\n"))
        self.assertIn("KIND=ok", response)
        state = self.directory.joinpath("uci").read_bytes()
        self.assertNotIn(b"\r", state)
        self.assertIn(b"owrtremote.main.vps_host=192.0.2.25\n", state)
        self.assertIn(b"owrtremote.main.wan_interface=wwan\n", state)
        self.assertIn(b"owrtremote.main.router_name=test + quote's & name\n", state)
        self.assertEqual(self.directory.joinpath("operations").read_text(),
                         "commit\nrender-client\nservice enable\nservice restart\nheartbeat\n")


if __name__ == "__main__":
    unittest.main()
