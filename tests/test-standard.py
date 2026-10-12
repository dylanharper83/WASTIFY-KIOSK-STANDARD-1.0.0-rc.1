#!/usr/bin/env python3
"""Release-standard regression tests; never touch real services or networks.

The simulated Pi OS and desktop live only in mocks. File writes are confined to
TemporaryDirectory; even installer dependency-order tests shadow sudo and apt.
Run with: python3 tests/test-standard.py
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('standard_kiosk', ROOT / 'wastify-kiosk/wastify_kiosk.py')
wk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wk)


def result(stdout='', code=0):
    return subprocess.CompletedProcess([], code, stdout, '')


def tree(path):
    """File contents and modes expose even temporary preflight mutations."""
    return {str(p.relative_to(path)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in Path(path).rglob('*') if p.is_file()}


class StandardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = wk.Layout(self.temp.name)
        self.control_calls = []
        self.run_calls = []
        self.environment_imports = []
        self.manager_env = ''
        self.uid = os.getuid()
        self.runtime = Path(f'/run/user/{self.uid}')
        self.sockets = {self.runtime / 'bus'}
        self.socket_owner = self.uid
        self.model = 'Raspberry Pi 5 Model B Rev 1.0\0'
        self.os_release = 'ID=raspbian\nVERSION_CODENAME=trixie\n'
        self.arch = 'arm64'
        self.desktop = True
        self.real_read = Path.read_text
        self.real_glob = Path.glob
        self.real_stat = Path.stat
        self.real_is_socket = Path.is_socket
        self.real_read_bytes = Path.read_bytes
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(wk, 'ctl', side_effect=self.control).start()
        patch.object(wk, 'run', side_effect=self.command).start()
        patch.object(wk.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name).start()
        patch.object(wk.sys, 'platform', 'linux').start()
        patch.object(wk.os, 'geteuid', return_value=1000).start()
        patch.object(Path, 'read_text', lambda path, *a, **kw: self.read_text(path, *a, **kw)).start()
        patch.object(Path, 'read_bytes', lambda path, *a, **kw: self.read_bytes(path, *a, **kw)).start()
        patch.object(Path, 'glob', lambda path, pattern: self.glob(path, pattern)).start()
        patch.object(Path, 'stat', lambda path, *a, **kw: self.path_stat(path, *a, **kw)).start()
        patch.object(Path, 'is_socket', lambda path: self.is_socket(path)).start()
        patch.dict(os.environ, {'HOME': self.temp.name}, clear=False).start()
        patch.object(Path, 'home', return_value=self.layout.home).start()

    def read_text(self, path, *args, **kwargs):
        if str(path) == '/etc/os-release':
            return self.os_release
        if str(path) == '/proc/device-tree/model':
            return self.model
        return self.real_read(path, *args, **kwargs)

    def read_bytes(self, path, *args, **kwargs):
        if str(path) == '/proc/device-tree/model':
            return self.model.encode()
        return self.real_read_bytes(path, *args, **kwargs)

    def glob(self, path, pattern):
        if str(path) == '/usr/share/wayland-sessions':
            return iter([path / 'labwc.desktop'] if self.desktop else [])
        if path == self.runtime:
            return iter(sorted(p for p in self.sockets if p.name.startswith('wayland-') and not p.name.endswith('.lock')))
        return self.real_glob(path, pattern)

    def path_stat(self, path, *args, **kwargs):
        if path in self.sockets or path == self.runtime:
            return types.SimpleNamespace(st_uid=self.socket_owner,
                                         st_mode=(stat.S_IFDIR if path == self.runtime else stat.S_IFSOCK) | 0o700)
        return self.real_stat(path, *args, **kwargs)

    def is_socket(self, path):
        if str(path).startswith('/run/user/'):
            return path in self.sockets
        return self.real_is_socket(path)

    def control(self, *args, **kwargs):
        self.control_calls.append(args)
        if args[0] == 'show-environment':
            return result(self.manager_env)
        if args[0] == 'import-environment':
            self.environment_imports.append(dict(os.environ))
        return result(code=3 if args[0] == 'is-active' else 0)

    def command(self, args, **kwargs):
        self.run_calls.append((args, kwargs))
        if args[:2] == ['dpkg', '--print-architecture']:
            return result(self.arch + '\n')
        if args[0] == 'systemctl':
            return result(code=3)
        if args[0] == 'pgrep':
            return result(code=1)
        return result()

    def main(self, *args):
        with patch.object(wk, 'Layout', return_value=self.layout), \
                patch.object(sys, 'argv', ['kiosk', *args]), \
                contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return wk.main()

    def install(self, url='https://touch.example.test/UnitA'):
        with contextlib.redirect_stdout(self.stdout):
            wk.install(self.layout, url)

    def desktop_environment(self, manager=False, displays=('wayland-0',)):
        self.sockets.update(self.runtime / value for value in displays)
        fields = {'XDG_RUNTIME_DIR': str(self.runtime), 'WAYLAND_DISPLAY': displays[0],
                  'DBUS_SESSION_BUS_ADDRESS': f'unix:path={self.runtime}/bus',
                  'XDG_SESSION_TYPE': 'wayland'}
        if manager:
            self.manager_env = ''.join(f'{key}={value}\n' for key, value in fields.items())
        return fields

    def remote_shell(self):
        values = dict(os.environ)
        for name in ('WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
                     'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP'):
            values.pop(name, None)
        values['SSH_CONNECTION'] = '192.0.2.9 1234 192.0.2.10 22'
        return patch.dict(os.environ, values, clear=True)

    def test_pi5_and_cm4_are_supported(self):
        for model in ('Raspberry Pi 5 Model B Rev 1.0\0',
                      'Raspberry Pi Compute Module 4 Rev 1.1\0'):
            with self.subTest(model=model):
                self.model = model
                self.assertEqual(wk.preflight(self.layout, 'https://example.test/a'), 'https://example.test/a')

    def test_other_models_fail_before_any_write(self):
        for model in ('Raspberry Pi 4 Model B Rev 1.5\0',
                      'Raspberry Pi Compute Module 4S Rev 1.0\0',
                      'Raspberry Pi Compute Module 5 Rev 1.0\0', 'unrecognised\0'):
            with self.subTest(model=model):
                self.model = model
                before = tree(self.layout.home)
                with self.assertRaises(ValueError):
                    wk.preflight(self.layout, 'https://example.test/a')
                self.assertEqual(tree(self.layout.home), before)

    def test_preflight_is_read_only(self):
        before = tree(self.layout.home)
        with patch.object(wk, 'atomic') as write, patch.object(self.layout, 'snapshot') as snapshot:
            self.assertEqual(wk.preflight(self.layout, 'https://example.test/a'), 'https://example.test/a')
        write.assert_not_called()
        snapshot.assert_not_called()
        self.assertEqual(tree(self.layout.home), before)
        self.assertFalse(any(args[0] in ('sudo', 'apt', 'apt-get', 'nmcli') for args, _ in self.run_calls))
        self.assertFalse(any(call[0] in ('start', 'stop', 'restart', 'enable', 'disable',
                                        'import-environment', 'daemon-reload') for call in self.control_calls))

    def test_wrong_os_arch_or_lite_are_rejected(self):
        for field, value in (('os_release', 'VERSION_CODENAME=bookworm\n'),
                             ('arch', 'armhf'), ('desktop', False)):
            with self.subTest(field=field):
                original = getattr(self, field)
                setattr(self, field, value)
                with self.assertRaises(ValueError):
                    wk.preflight(self.layout, 'https://example.test/a')
                self.assertEqual(tree(self.layout.home), {})
                setattr(self, field, original)

    def test_missing_user_bus_aborts_preflight(self):
        with patch.object(wk, 'ctl', side_effect=subprocess.CalledProcessError(1, ['systemctl'])):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                wk.preflight(self.layout, 'https://example.test/a')
        self.assertEqual(tree(self.layout.home), {})

    def test_preflight_requires_https_and_ownership(self):
        wk.atomic(self.layout.command, '#!/bin/sh\n# Some other kiosk\n')
        before = tree(self.layout.home)
        with self.assertRaises(ValueError):
            wk.preflight(self.layout, 'https://example.test/a')
        self.assertEqual(tree(self.layout.home), before)
        self.layout.command.unlink()
        for url in ('http://example.test/a', 'file:///etc/passwd',
                    'https://user:pass@example.test/a', 'https://example.test/a\n'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                wk.preflight(self.layout, url)

    def test_managed_leaf_symlink_is_rejected_without_touching_target(self):
        target = self.layout.home / 'existing-script'
        target.write_text('#!/bin/sh\n' + wk.MARKER + '\n')
        self.layout.command.parent.mkdir(parents=True)
        self.layout.command.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.layout.check_owned()
        self.assertTrue(self.layout.command.is_symlink())
        self.assertEqual(target.read_text(), '#!/bin/sh\n' + wk.MARKER + '\n')

    def test_managed_parent_symlink_is_rejected_without_writing_through_it(self):
        target = self.layout.home / 'existing-local'
        target.mkdir()
        marker = target / 'keep'
        marker.write_text('unchanged')
        (self.layout.home / '.local').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.layout.check_owned()
        self.assertTrue((self.layout.home / '.local').is_symlink())
        self.assertEqual(list(target.iterdir()), [marker])
        self.assertEqual(marker.read_text(), 'unchanged')

    def test_managed_leaf_parent_and_home_must_belong_to_current_user(self):
        self.install()
        original_stat = Path.stat
        for foreign_path in (self.layout.runtime, self.layout.runtime.parent, self.layout.home):
            with self.subTest(path=foreign_path):
                def stat_with_foreign_owner(path, *args, **kwargs):
                    value = original_stat(path, *args, **kwargs)
                    if path == foreign_path:
                        fields = list(value)
                        fields[4] = self.uid + 1
                        return os.stat_result(fields)
                    return value
                with patch.object(Path, 'stat', stat_with_foreign_owner):
                    with self.assertRaisesRegex(ValueError, 'another user'):
                        self.layout.check_owned()

    def test_installed_aliases_and_direct_browser_unit(self):
        self.install()
        for command in (self.layout.command, self.layout.compat_command):
            self.assertTrue(command.exists())
            self.assertIn(wk.MARKER, command.read_text())
            self.assertTrue(os.access(command, os.X_OK))
        unit = self.layout.unit.read_text()
        self.assertIn('" launch\n', unit)
        self.assertNotIn('supervise', unit)
        self.assertIn('RestartPreventExitStatus=78', unit)
        self.assertIn('StartLimitBurst=3', unit)
        self.assertFalse(self.layout.enabled.exists())

    def test_enable_from_first_connect_shell_stages_without_desktop(self):
        self.install()
        self.control_calls.clear()
        with self.remote_shell():
            self.assertEqual(self.main('enable'), 0)
        self.assertTrue(self.layout.enabled.exists())
        self.assertNotIn(('start', wk.UNIT), self.control_calls)
        self.assertIn('desktop', self.stdout.getvalue().lower())

    def test_start_from_connect_without_desktop_fails_closed(self):
        self.install()
        self.control_calls.clear()
        with self.remote_shell(), self.assertRaises(ValueError):
            self.main('start')
        self.assertNotIn(('start', wk.UNIT), self.control_calls)
        self.assertFalse(self.layout.enabled.exists())

    def test_remote_uses_valid_manager_display_and_restores_shell(self):
        fields = self.desktop_environment(manager=True)
        with self.remote_shell():
            saved = dict(os.environ)
            wk.prepare_from_remote()
            self.assertEqual(dict(os.environ), saved)
        self.assertTrue(self.environment_imports)
        for name in ('WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
            self.assertEqual(self.environment_imports[-1][name], fields[name])

    def test_first_connect_shell_discovers_one_same_user_socket(self):
        self.desktop_environment()
        with self.remote_shell():
            saved = dict(os.environ)
            wk.prepare_from_remote()
            self.assertEqual(dict(os.environ), saved)
        self.assertTrue(self.environment_imports)
        self.assertEqual(self.environment_imports[-1]['WAYLAND_DISPLAY'], 'wayland-0')

    def test_ambiguous_desktop_is_rejected_and_shell_restored(self):
        self.desktop_environment(displays=('wayland-0', 'wayland-1'))
        with self.remote_shell():
            saved = dict(os.environ)
            with self.assertRaises(ValueError):
                wk.prepare_from_remote()
            self.assertEqual(dict(os.environ), saved)
        self.assertEqual(self.environment_imports, [])

    def test_foreign_socket_is_rejected(self):
        self.desktop_environment()
        self.socket_owner = self.uid + 1
        with self.remote_shell(), self.assertRaises(ValueError):
            wk.prepare_from_remote()
        self.assertEqual(self.environment_imports, [])

    def test_foreign_runtime_in_shell_is_rejected(self):
        fields = self.desktop_environment()
        fields['XDG_RUNTIME_DIR'] = f'/run/user/{self.uid + 1}'
        with patch.dict(os.environ, fields), self.assertRaises(ValueError):
            wk.prepare_from_remote()
        self.assertEqual(self.environment_imports, [])

    def test_stop_keeps_autostart_but_disable_removes_it(self):
        self.install()
        wk.atomic(self.layout.enabled, 'enabled\n')
        self.control_calls.clear()
        self.main('stop')
        self.assertTrue(self.layout.enabled.exists())
        self.assertIn(('stop', wk.UNIT), self.control_calls)
        self.main('disable')
        self.assertFalse(self.layout.enabled.exists())
        self.control_calls.clear()
        with patch.object(wk, 'import_session'):
            self.main('session-start')
        self.assertNotIn(('start', wk.UNIT), self.control_calls)

    def test_rollback_preserves_current_site_and_browser_data(self):
        self.install('https://example.test/old-site')
        self.layout.profile.mkdir(parents=True)
        saved = self.layout.profile / 'pending-record'
        saved.write_text('pending offline record')
        self.install()
        self.main('url', 'https://example.test/current-site?token=keep')
        self.control_calls.clear()
        self.main('rollback')
        self.assertEqual(self.layout.read()['url'], 'https://example.test/current-site?token=keep')
        self.assertEqual(saved.read_text(), 'pending offline record')
        self.assertNotIn(('start', wk.UNIT), self.control_calls)

    def test_first_install_rollback_refuses_before_stopping_or_removing_itself(self):
        self.install()
        (self.layout.base / 'control.lock').touch()
        before = tree(self.layout.home)
        self.control_calls.clear()
        with self.assertRaises(ValueError):
            self.main('rollback')
        self.assertEqual(tree(self.layout.home), before)
        self.assertNotIn(('stop', wk.UNIT), self.control_calls)
        self.assertTrue(self.layout.command.exists())
        self.assertTrue(self.layout.runtime.exists())

    def test_corrupt_backup_is_rejected_before_stopping_or_changing_files(self):
        self.install()
        self.install()
        (self.layout.base / 'control.lock').touch()
        manifest = sorted((self.layout.base / 'backups').glob('*/manifest.json'))[-1]
        manifest.write_text('{"broken": true}')
        before = tree(self.layout.home)
        self.control_calls.clear()
        with self.assertRaises(ValueError):
            self.main('rollback')
        self.assertEqual(tree(self.layout.home), before)
        self.assertNotIn(('stop', wk.UNIT), self.control_calls)

    def test_update_rejects_missing_release_without_changing_files(self):
        self.install()
        before = tree(self.layout.home)
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.main('update', str(self.layout.home / 'missing-release'))
        self.assertEqual(tree(self.layout.home), before)

    def test_update_uses_explicit_local_package_and_preserves_state(self):
        self.install()
        wk.atomic(self.layout.enabled, 'enabled\n')
        before = tree(self.layout.home)
        self.run_calls.clear()
        with patch.object(wk.os, 'execv') as execute:
            self.main('update', str(ROOT))
        execute.assert_called_once_with('/bin/bash', ['bash', str(ROOT / 'install.sh'), '--no-start'])
        self.assertEqual(tree(self.layout.home), before)
        self.assertEqual(self.run_calls, [])

    def test_help_and_version_need_no_service_or_state(self):
        self.main('help')
        self.main('version')
        output = self.stdout.getvalue()
        for command in ('status', 'start', 'stop', 'restart', 'enable', 'disable',
                        'url', 'wifi', 'check', 'logs', 'support', 'update', 'rollback'):
            self.assertIn(command, output)
        self.assertIn(wk.VERSION, output)
        self.assertEqual(tree(self.layout.home), {})
        self.assertEqual(self.control_calls, [])
        self.assertEqual(self.run_calls, [])

    def test_check_is_read_only_even_before_install(self):
        before = tree(self.layout.home)
        self.main('check')
        self.assertEqual(tree(self.layout.home), before)
        self.assertFalse(any(call[0] in ('start', 'stop', 'restart', 'daemon-reload')
                             for call in self.control_calls))

    def test_diagnostics_report_invalid_configuration_without_curl_or_mutation(self):
        self.install()
        for contents in ('{broken-json', '{}', '{"url": "http://example.test/private-token"}'):
            with self.subTest(contents=contents):
                self.layout.config.write_text(contents)
                before = tree(self.layout.home)
                self.run_calls.clear()
                report = wk.diagnostic_report(self.layout)
                self.assertEqual(report['site_http'], 'invalid or unreadable configuration')
                self.assertNotIn('private-token', json.dumps(report))
                self.assertFalse(any(args[0] == 'curl' for args, _ in self.run_calls))
                self.assertEqual(tree(self.layout.home), before)

    def test_diagnostics_report_unreadable_configuration_without_raising_or_leaking(self):
        self.install()
        self.run_calls.clear()
        before = tree(self.layout.home)
        with patch.object(self.layout, 'read', side_effect=OSError('private-path-or-secret')):
            report = wk.diagnostic_report(self.layout)
        self.assertEqual(report['site_http'], 'invalid or unreadable configuration')
        self.assertNotIn('private-path-or-secret', json.dumps(report))
        self.assertFalse(any(args[0] == 'curl' for args, _ in self.run_calls))
        self.assertEqual(tree(self.layout.home), before)

    def test_wifi_requires_interactive_terminal_before_sudo(self):
        with patch.object(sys.stdin, 'isatty', return_value=False), self.assertRaises(ValueError):
            self.main('wifi')
        self.assertFalse(any(args[0] in ('sudo', 'nmcli', 'nmtui') for args, _ in self.run_calls))

    def test_wifi_uses_timed_networkmanager_checkpoint_without_shell(self):
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(self.stdout, 'isatty', return_value=True):
            self.main('wifi')
        command = next((args, kwargs) for args, kwargs in self.run_calls if args[0] == 'sudo')
        self.assertEqual(command[0], ['sudo', 'nmcli', 'device', 'checkpoint', '--timeout', '180', '--', 'timeout', '--foreground', '150', 'nmtui'])
        self.assertFalse(command[1].get('shell', False))
        self.assertFalse(any(args[0] in ('bash', 'sh') for args, _ in self.run_calls))

    def test_support_does_not_collect_site_tokens_ssid_ips_or_journal(self):
        self.install('https://example.test/private/unit-secret?token=site-token-900')
        secret = 'Office-Private-SSID-900 192.0.2.222 2001:db8::900 session-token-900'
        self.manager_env = 'WAYLAND_DISPLAY=wayland-0\nPRIVATE=' + secret + '\n'
        before = tree(self.layout.home)
        queries = []
        def diagnostics(args, **kwargs):
            queries.append(args)
            if args == ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status']:
                return result('wlan0:wifi:connected\n')
            if args[0] in ('nmcli', 'journalctl', 'ip', 'hostname', 'loginctl'):
                return result(secret)
            if args[0] == 'curl':
                return result('200')
            return self.command(args, **kwargs)
        real_temporary = tempfile.NamedTemporaryFile
        def temporary_report(*args, **kwargs):
            kwargs['dir'] = self.temp.name
            return real_temporary(*args, **kwargs)
        with patch.object(wk, 'run', side_effect=diagnostics), \
                patch.object(wk.tempfile, 'NamedTemporaryFile', side_effect=temporary_report):
            self.main('support')
        after = tree(self.layout.home)
        new_files = set(after) - set(before)
        self.assertEqual(len(new_files), 1)
        exported = self.stdout.getvalue() + self.stderr.getvalue()
        for name in new_files:
            exported += after[name][0].decode('utf-8', errors='replace')
            self.assertEqual(after[name][1], 0o600)
            report = json.loads(after[name][0])
            self.assertEqual(report['site_http'], '200')
            self.assertEqual(report['application'], 'unverified')
            self.assertEqual(report['physical_display'], 'unverified')
        self.assertEqual({name: contents for name, contents in after.items() if name not in new_files}, before)
        for sensitive in ('unit-secret', 'site-token-900', 'Office-Private-SSID-900',
                          '192.0.2.222', '2001:db8::900', 'session-token-900'):
            self.assertNotIn(sensitive, exported)
        self.assertFalse(any(args[0] in ('journalctl', 'ip', 'hostname', 'loginctl') for args in queries))
        network_queries = [args for args in queries if args[0] == 'nmcli']
        self.assertEqual(network_queries, [['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status']])


class InstallerOrderingTests(unittest.TestCase):
    def test_deps_never_reaches_sudo_or_apt_after_failed_preflight(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            fake_bin = folder / 'bin'
            fake_bin.mkdir()
            trace = folder / 'calls'
            (fake_bin / 'python3').write_text('#!/bin/sh\nprintf "python %s\\n" "$*" >> "$TEST_TRACE"\nexit 17\n')
            for name in ('sudo', 'apt-get'):
                (fake_bin / name).write_text('#!/bin/sh\nprintf "PRIVILEGED %s\\n" "$*" >> "$TEST_TRACE"\nexit 99\n')
            for executable in fake_bin.iterdir():
                executable.chmod(0o700)
            env = dict(os.environ, HOME=str(folder), TEST_TRACE=str(trace),
                       PATH=str(fake_bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'))
            completed = subprocess.run(['/bin/bash', str(ROOT / 'install.sh'), '--deps',
                                        'https://example.test/site'], env=env, capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0)
            calls = trace.read_text()
            self.assertIn('preflight', calls)
            self.assertNotIn('PRIVILEGED', calls)
            self.assertNotIn(' install ', calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
