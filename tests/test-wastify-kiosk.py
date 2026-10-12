#!/usr/bin/env python3
"""Isolated tests: no real browser, network or service changes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import contextlib
import io
import ast
import socket
import tarfile
import hashlib
import base64
import gzip
import re
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wk', ROOT / 'wastify-kiosk/wastify_kiosk.py')
wk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wk)
REAL_CTL = wk.ctl
REAL_READY = wk.session_ready
REAL_CONFLICTS = wk.check_conflicts


def result(stdout='', code=0):
    return subprocess.CompletedProcess([], code, stdout, '')


class KioskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.layout = wk.Layout(self.temp.name)
        self.calls = []
        self.ctl = patch.object(wk, 'ctl', side_effect=self.control).start()
        self.run = patch.object(wk, 'run', return_value=result('arm64\n')).start()
        self.which = patch.object(wk.shutil, 'which', return_value='/usr/bin/chromium').start()
        self.conflicts = patch.object(wk, 'check_conflicts').start()
        self.ready = patch.object(wk, 'session_ready').start()
        patch.object(sys.stdin, 'isatty', return_value=True).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def control(self, *args, **kwargs):
        self.calls.append(args)
        return result(code=3 if args[0] == 'is-active' else 0)

    def install(self):
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/etc/os-release':
                return 'VERSION_CODENAME=trixie\n'
            if str(path) == '/proc/device-tree/model':
                return 'Raspberry Pi 5 Model B Rev 1.0\0'
            return real_read(path, *args, **kwargs)
        real_glob = Path.glob
        def glob(path, pattern):
            if str(path) == '/usr/share/wayland-sessions':
                return iter([Path('/usr/share/wayland-sessions/labwc.desktop')])
            return real_glob(path, pattern)
        with patch.object(wk.sys, 'platform', 'linux'), patch.object(Path, 'read_text', read), patch.object(Path, 'glob', glob):
            wk.install(self.layout, 'https://example.test/SITE_CODE')

    def main(self, *args):
        with patch.object(wk, 'Layout', return_value=self.layout), \
             patch.object(sys, 'argv', ['wastify-kiosk', *args]), \
             patch.object(wk.os, 'geteuid', return_value=1000):
            return wk.main()

    def test_fresh_install_is_staged_not_enabled(self):
        self.install()
        self.assertFalse(self.layout.enabled.exists())
        self.assertNotIn(('start', wk.UNIT), self.calls)
        self.assertIn('session-start', self.layout.desktop.read_text())
        self.assertIn('StartLimitBurst=3', self.layout.unit.read_text())
        self.assertIn('" launch\n', self.layout.unit.read_text())
        self.assertIn('RestartPreventExitStatus=78', self.layout.unit.read_text())
        self.assertNotIn('[Install]', self.layout.unit.read_text())

    def test_install_does_not_touch_desktop_profile(self):
        profile = self.layout.home / '.config/chromium/Default'
        profile.mkdir(parents=True)
        (profile / 'Preferences').write_text('keep')
        self.install()
        self.assertEqual((profile / 'Preferences').read_text(), 'keep')

    def test_install_refuses_unowned_command(self):
        wk.atomic(self.layout.command, 'not ours')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.layout.command.read_text(), 'not ours')

    def test_upgrade_preserves_enabled_and_restarts_running(self):
        self.install()
        wk.atomic(self.layout.enabled, 'enabled')
        with patch.object(wk, 'active', return_value=True):
            self.install()
        self.assertTrue(self.layout.enabled.exists())
        self.assertIn(('start', wk.UNIT), self.calls)

    def test_failed_upgrade_restores_previous_files(self):
        self.install()
        previous = self.layout.config.read_text()
        original = wk.atomic
        failed = False
        def fail_once(path, content, mode=0o600):
            nonlocal failed
            if path == self.layout.desktop and not failed:
                failed = True
                raise OSError('injected disk error')
            return original(path, content, mode)
        with patch.object(wk, 'atomic', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(previous, self.layout.config.read_text())
        self.assertTrue(self.layout.runtime.exists())

    def test_urls_are_data_not_shell(self):
        value = 'https://example.com/?a=1&b=$(id)'
        self.assertEqual(wk.validate_url(value), value)
        for bad in ('file:///etc/passwd', 'http://example.com', 'https://',
                    'https://a\nb', 'https://user:secret@example.com'):
            with self.assertRaises(ValueError):
                wk.validate_url(bad)

    def test_stopped_url_change_does_not_start_browser(self):
        self.install()
        self.calls.clear()
        self.main('url', 'https://example.com/')
        self.assertEqual(self.layout.read()['url'], 'https://example.com/')
        self.assertNotIn(('restart', wk.UNIT), self.calls)

    def test_running_url_change_restarts(self):
        self.install()
        with patch.object(wk, 'active', return_value=True):
            self.main('url', 'https://example.com/')
        self.assertIn(('restart', wk.UNIT), self.calls)

    def test_manual_start_does_not_enable_autostart(self):
        self.install()
        with patch.object(wk, 'prepare_from_remote'):
            self.main('start')
        self.assertFalse(self.layout.enabled.exists())
        self.assertIn(('start', wk.UNIT), self.calls)

    def test_disable_stops_and_prevents_session_start(self):
        self.install()
        wk.atomic(self.layout.enabled, 'enabled')
        self.main('disable')
        self.calls.clear()
        with patch.object(wk, 'import_session'):
            self.main('session-start')
        self.assertNotIn(('start', wk.UNIT), self.calls)

    def test_uninstall_retains_browser_config_and_backup(self):
        self.install()
        self.layout.profile.mkdir()
        (self.layout.profile / 'saved').write_text('data')
        self.main('uninstall')
        self.assertTrue(self.layout.config.exists())
        self.assertEqual((self.layout.profile / 'saved').read_text(), 'data')
        self.assertFalse(self.layout.desktop.exists())
        self.assertFalse(self.layout.unit.exists())

    def test_rollback_preserves_current_url_without_start(self):
        self.install()
        self.main('url', 'https://example.com/previous')
        self.install()
        self.calls.clear()
        self.main('rollback')
        self.assertEqual(self.layout.read()['url'], 'https://example.test/SITE_CODE')
        self.assertNotIn(('start', wk.UNIT), self.calls)

    def test_missing_session_fails_closed(self):
        self.ready.side_effect = ValueError('missing')
        self.assertEqual(wk.launch(self.layout), 78)

    def test_launch_uses_dedicated_profile_minimal_flags(self):
        self.install()
        with patch.object(wk.os, 'execv') as execute:
            wk.launch(self.layout)
        args = execute.call_args.args[1]
        self.assertIn('--user-data-dir=' + str(self.layout.profile), args)
        self.assertNotIn('--use-fake-ui-for-media-stream', args)
        self.assertNotIn('--disable-gpu', args)
        self.assertIn('--ozone-platform=wayland', args)

    def test_foreign_lock_is_not_deleted(self):
        self.install()
        self.layout.profile.mkdir()
        lock = self.layout.profile / 'SingletonLock'
        lock.symlink_to('other-host-123')
        self.assertEqual(wk.launch(self.layout), 78)
        self.assertTrue(lock.is_symlink())

    def test_stale_local_lock_is_left_for_chromium(self):
        self.install()
        self.layout.profile.mkdir()
        lock = self.layout.profile / 'SingletonLock'
        lock.symlink_to(wk.socket.gethostname() + '-999999999')
        with patch.object(wk.os, 'execv') as execute:
            wk.launch(self.layout)
        execute.assert_called_once()
        self.assertTrue(lock.is_symlink())

    def test_conflict_blocks_install_before_files(self):
        self.conflicts.side_effect = ValueError('old kiosk')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.layout.runtime.exists())



    def test_no_display_or_network_resets_in_runtime(self):
        text = (ROOT / 'wastify-kiosk/wastify_kiosk.py').read_text()
        for forbidden in ('wlr-randr', 'wlopm', 'xset', 'WLR_SCENE_',
                          'restart lightdm', 'restart NetworkManager', '--disable-gpu'):
            self.assertNotIn(forbidden, text)

    def test_live_profile_lock_prevents_launch(self):
        self.install()
        self.layout.profile.mkdir()
        (self.layout.profile / 'SingletonLock').symlink_to(wk.socket.gethostname() + '-123')
        exists = Path.exists
        with patch.object(Path, 'exists', lambda p: True if str(p) == '/proc/123' else exists(p)):
            with patch.object(wk.os, 'execv') as execute:
                self.assertEqual(wk.launch(self.layout), 78)
                execute.assert_not_called()

    def test_session_start_only_starts_when_enabled(self):
        self.install()
        wk.atomic(self.layout.enabled, 'enabled')
        self.calls.clear()
        with patch.object(wk, 'import_session') as imported:
            self.main('session-start')
        imported.assert_called_once()
        self.assertIn(('start', wk.UNIT), self.calls)

    def test_invalid_url_does_not_change_configuration(self):
        self.install()
        original = self.layout.config.read_text()
        with self.assertRaises(ValueError):
            self.main('url', 'file:///etc/passwd')
        self.assertEqual(original, self.layout.config.read_text())

    def test_snapshot_refuses_symlink(self):
        self.layout.command.parent.mkdir(parents=True)
        self.layout.command.symlink_to('/tmp/unrelated-file')
        with self.assertRaises(ValueError):
            self.layout.snapshot()


    def test_install_through_real_ctl(self):
        def system(args, **kwargs):
            if args[0] == 'dpkg':
                return result('arm64')
            if 'is-active' in args:
                return result(code=3)
            return result()
        self.run.side_effect = system
        with patch.object(wk, 'ctl', REAL_CTL):
            self.install()
            self.main('status')
        self.assertTrue(self.layout.runtime.exists())
        self.assertTrue(any(c.kwargs.get('stdout') == subprocess.DEVNULL for c in self.run.call_args_list))

    def test_ctl_forwards_subprocess_options(self):
        REAL_CTL('show-environment', stdout=subprocess.DEVNULL)
        self.run.assert_called_with(['systemctl', '--user', 'show-environment'],
                                    check=True, stdout=subprocess.DEVNULL)
        REAL_CTL('show-environment', capture_output=True)
        self.run.assert_called_with(['systemctl', '--user', 'show-environment'],
                                    check=True, capture_output=True)
        REAL_CTL('stop', wk.UNIT, check=False, stderr=subprocess.DEVNULL)
        self.run.assert_called_with(['systemctl', '--user', 'stop', wk.UNIT],
                                    check=False, stderr=subprocess.DEVNULL)

    def test_syntax_and_help(self):
        source = ROOT / 'wastify-kiosk/wastify_kiosk.py'
        ast.parse(source.read_text())
        help_result = subprocess.run([sys.executable, '-B', str(source), '--help'], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('rollback', help_result.stdout)
        syntax = subprocess.run(['bash', '-n', str(ROOT / 'wastify-kiosk/install.sh')])
        self.assertEqual(syntax.returncode, 0)

    def test_wrong_os_refused_before_files(self):
        with patch.object(wk.sys, 'platform', 'darwin'):
            with self.assertRaises(ValueError):
                wk.install(self.layout, 'https://example.com/')
        self.assertFalse(self.layout.runtime.exists())
        self.assertFalse(self.calls)

    def test_wrong_arch_refused_before_files(self):
        self.run.return_value = result('armhf')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.layout.runtime.exists())

    def test_root_refused_before_files(self):
        with patch.object(sys, 'argv', ['wk', 'install', 'https://example.com/']), patch.object(wk.os, 'geteuid', return_value=0):
            with self.assertRaises(ValueError):
                wk.main()
        self.assertFalse(self.layout.runtime.exists())

    def test_bad_ports_and_config_shapes(self):
        for url in ('https://example.com:bad/', 'https://example.com:99999/', None, 42):
            with self.assertRaises(ValueError):
                wk.validate_url(url)
        for text in ('[]', '{}', '{"url": 42}', '{broken'):
            wk.atomic(self.layout.config, text)
            with self.assertRaises(ValueError):
                self.layout.read()

    def test_actual_conflict_detector(self):
        self.run.return_value = result(code=1)
        REAL_CONFLICTS()
        self.run.return_value = result(code=0)
        with self.assertRaises(ValueError):
            REAL_CONFLICTS()
        def commands(args, **kwargs):
            if args[0] == 'pgrep':
                return result('123 /usr/bin/chromium --kiosk https://example.com/')
            return result(code=1)
        self.run.side_effect = commands
        with self.assertRaises(ValueError):
            REAL_CONFLICTS()

    def test_upgrade_refuses_replaced_integration(self):
        self.install()
        self.layout.unit.write_text('another service')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.layout.unit.read_text(), 'another service')

    def test_permissions_are_private(self):
        self.install()
        for path in (self.layout.config, self.layout.unit, self.layout.desktop):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for path in (self.layout.runtime, self.layout.command):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_atomic_write_failure_preserves_old_file(self):
        wk.atomic(self.layout.config, 'original')
        with patch.object(wk.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                wk.atomic(self.layout.config, 'replacement')
        self.assertEqual(self.layout.config.read_text(), 'original')
        self.assertFalse(list(self.layout.config.parent.glob('.wastify-*')))

    def test_all_install_write_failures_roll_back(self):
        self.install()
        before = {p: p.read_bytes() if p.exists() else None for p in self.layout.managed}
        original = wk.atomic
        for target in (self.layout.runtime, self.layout.command, self.layout.config, self.layout.unit, self.layout.desktop):
            with self.subTest(target=target.name):
                failed = False
                def write(path, content, mode=0o600):
                    nonlocal failed
                    if path == target and not failed:
                        failed = True
                        raise OSError('injected write failure')
                    return original(path, content, mode)
                with patch.object(wk, 'atomic', side_effect=write):
                    with self.assertRaises(OSError):
                        self.install()
                for path, content in before.items():
                    self.assertEqual(path.read_bytes() if path.exists() else None, content)

    def test_failed_stop_blocks_upgrade_and_uninstall_and_rollback(self):
        self.install()
        self.install()
        old = self.layout.runtime.read_bytes()
        original_control = self.control
        def failing_control(*args, **kwargs):
            if args[0] == 'stop':
                raise subprocess.CalledProcessError(1, ['systemctl', 'stop'])
            return original_control(*args, **kwargs)
        self.ctl.side_effect = failing_control
        for action in ('install', 'uninstall', 'rollback'):
            with self.subTest(action=action):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.install() if action == 'install' else self.main(action)
                self.assertEqual(self.layout.runtime.read_bytes(), old)
                self.assertTrue(self.layout.unit.exists())

    def test_truncated_backup_is_rejected_before_restore(self):
        self.install()
        backup = self.layout.snapshot()
        (backup / '3').unlink()
        before = self.layout.command.read_bytes()
        with self.assertRaises(OSError):
            self.layout.restore(backup)
        self.assertEqual(self.layout.command.read_bytes(), before)

    def test_bad_manifest_is_rejected(self):
        self.install()
        backup = self.layout.snapshot()
        (backup / 'manifest.json').write_text('{"0":true}')
        with self.assertRaises(ValueError):
            self.layout.restore(backup)
        self.assertTrue(self.layout.command.exists())

    def test_stop_keeps_autostart_enable_flag(self):
        self.install()
        wk.atomic(self.layout.enabled, 'enabled')
        self.main('stop')
        self.assertTrue(self.layout.enabled.exists())
        self.assertIn(('stop', wk.UNIT), self.calls)

    def test_enable_defers_start_until_desktop_login(self):
        self.install()
        with patch.object(wk, 'prepare_from_remote', side_effect=ValueError('no desktop')):
            self.main('enable')
        self.assertTrue(self.layout.enabled.exists())
        self.assertNotIn(('start', wk.UNIT), self.calls)

    def test_session_accepts_wayland_two_and_rejects_wrong_user(self):
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': '/run/user/1000', 'WAYLAND_DISPLAY': 'wayland-2'}), patch.object(wk.os, 'getuid', return_value=1000), patch.object(Path, 'is_socket', return_value=True):
            REAL_READY()
            os.environ['XDG_RUNTIME_DIR'] = '/run/user/2000'
            with self.assertRaises(ValueError):
                REAL_READY()

    def test_regular_files_are_not_a_desktop(self):
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': '/run/user/1000', 'WAYLAND_DISPLAY': 'wayland-0'}), patch.object(wk.os, 'getuid', return_value=1000), patch.object(Path, 'is_socket', return_value=False):
            with self.assertRaises(ValueError):
                REAL_READY()

    def test_remote_environment_is_restored(self):
        self.ctl.side_effect = None
        self.ctl.return_value = result('WAYLAND_DISPLAY=wayland-2\nXDG_RUNTIME_DIR=/run/user/1000\n')
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': '/run/user/1000'}, clear=True):
            before = dict(os.environ)
            observed = []
            self.ready.side_effect = lambda: observed.append(os.environ['WAYLAND_DISPLAY'])
            wk.prepare_from_remote()
            self.assertEqual(observed, ['wayland-2', 'wayland-2'])
            self.assertEqual(dict(os.environ), before)

    def test_desktop_environment_is_imported(self):
        with patch.dict(os.environ, {'WAYLAND_DISPLAY': 'wayland-2'}):
            wk.prepare_from_remote()
        self.assertIn(('import-environment', 'WAYLAND_DISPLAY'), self.calls)

    def test_launch_never_interprets_url_as_shell(self):
        self.install()
        url = 'https://example.com/?a=$(id)&b=1;true'
        wk.atomic(self.layout.config, json.dumps({'url': url}))
        with patch.object(wk.os, 'execv') as execute:
            wk.launch(self.layout)
        self.assertEqual(execute.call_args.args[1][-1], url)

    def test_restart_policy_is_bounded_and_stops_process_group(self):
        self.install()
        unit = self.layout.unit.read_text()
        for directive in ('StartLimitIntervalSec=300', 'StartLimitBurst=3', 'RestartSec=10',
                          'RestartPreventExitStatus=78', 'KillMode=control-group', 'TimeoutStopSec=20'):
            self.assertIn(directive, unit)
        self.assertNotIn('StartLimitIntervalSec=0', unit)

    def test_control_lock_blocks_second_process(self):
        self.layout.base.mkdir(parents=True)
        with (self.layout.base / 'control.lock').open('a') as lock:
            wk.fcntl.flock(lock, wk.fcntl.LOCK_EX | wk.fcntl.LOCK_NB)
            env = dict(os.environ, HOME=str(self.layout.home))
            proc = subprocess.run([sys.executable, '-B', str(ROOT / 'wastify-kiosk/wastify_kiosk.py'), 'status'], env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Another kiosk management command', proc.stderr)






    def test_installer_reinstall_preserves_disabled_state(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            binary = home / 'bin'
            binary.mkdir()
            calls = home / 'calls.log'
            for name in ('python3', 'chromium', 'curl'):
                path = binary / name
                path.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\n')
                path.chmod(0o700)
            command = home / '.local/bin/kiosk'
            command.parent.mkdir(parents=True)
            command.write_text('#!/bin/bash\nprintf "kiosk %s\\n" "$*" >> "$TEST_CALLS"\n')
            command.chmod(0o700)
            env = dict(os.environ, HOME=str(home), PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       TEST_CALLS=str(calls))
            first = subprocess.run(['bash', str(ROOT / 'install.sh'), 'https://example.test/'],
                                   env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn('kiosk enable', calls.read_text())
            runtime = home / '.local/share/wastify-kiosk/wastify_kiosk.py'
            runtime.parent.mkdir(parents=True)
            runtime.write_text('installed')
            calls.write_text('')
            again = subprocess.run(['bash', str(ROOT / 'install.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertNotIn('kiosk enable', calls.read_text())
            self.assertIn(' preflight', calls.read_text())
            self.assertIn(' install', calls.read_text())

    def test_connect_paste_payload_matches_release_source(self):
        paste = ROOT / 'INSTALL-VIA-CONNECT.txt'
        text = paste.read_text()
        match = re.search(r'encoded = """\n(.*?)\n"""', text, re.S)
        self.assertIsNotNone(match)
        payload = gzip.decompress(base64.b64decode(match.group(1)))
        digest = hashlib.sha256(payload).hexdigest()
        self.assertIn("hexdigest() != '" + digest + "'", text)
        files = json.loads(payload)
        self.assertEqual(set(files), {'install.sh', 'desktop_setup.py', 'wastify-kiosk/install.sh',
                                     'wastify-kiosk/wastify_kiosk.py'})
        for name, content in files.items():
            self.assertEqual(content, (ROOT / name).read_text())
        syntax = subprocess.run(['bash', '-n', str(paste)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)


if __name__ == '__main__':
    unittest.main()
