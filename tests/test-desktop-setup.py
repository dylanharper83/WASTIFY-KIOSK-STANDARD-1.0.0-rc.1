#!/usr/bin/env python3
"""Fresh desktop provisioning tests; no real sudo, services, or OS writes."""
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
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'desktop_setup', Path(__file__).resolve().parents[1] / 'desktop_setup.py')
ds = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ds
SPEC.loader.exec_module(ds)


WAYLAND = '''do_wayland() {
case "$RET" in
 W2*)
 STATUS="Labwc on Wayland"
 ;;
esac
}
'''
MODERN = '#!/bin/sh\ndo_boot_target(){ :; }\ndo_autologin() { :; }\n' + WAYLAND
LEGACY = '#!/bin/sh\ndo_boot_behaviour() { :; }\n' + WAYLAND
CONFIG = (b'[Seat:*]\n#autologin-user=\n#user-session=\n'
          b'#autologin-session=\n#greeter-session=\n')
BLANKING = b"swayidle -w timeout 600 'wlopm --off \\*' resume 'wlopm --on \\*' &\n"


class DesktopSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if sys.platform == 'darwin' else None)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.uid = os.getuid()
        self.paths = ds.Paths(self.base, self.uid)
        self.home = self.base / 'home/pi'
        self.home.mkdir(parents=True)
        self.user = ds.DesktopUser('pi', self.uid, os.getgid(), self.home)
        self.put('/usr/bin/raspi-config', MODERN.encode(), 0o755)
        self.put('/usr/sbin/lightdm', b'fixture', 0o755)
        self.put('/etc/init.d/lightdm', b'fixture', 0o755)
        self.put('/etc/lightdm/lightdm.conf', CONFIG)
        self.put('/usr/bin/labwc', b'fixture', 0o755)
        self.put('/usr/share/wayland-sessions/rpd-labwc.desktop', b'[Desktop Entry]\nExec=labwc-pi\n')
        self.put('/etc/xdg/labwc/autostart', b'/usr/bin/lxsession-xdg-autostart\n')
        self.paths.at('/etc/systemd/system').mkdir(parents=True)
        self.paths.at('/etc/systemd/system/default.target').symlink_to('/lib/systemd/system/multi-user.target')
        self.calls = []

    def put(self, name, content, mode=0o644):
        destination = self.paths.at(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(mode)
        return destination

    def user_autostart(self, content):
        destination = self.home / '.config/labwc/autostart'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def command(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[2] in ('do_autologin', 'do_boot_behaviour'):
            config = self.paths.lightdm_conf.read_text().replace('#autologin-user=', 'autologin-user=pi')
            self.paths.lightdm_conf.write_text(config)
        if command[2] == 'do_wayland':
            session = ('rpd-labwc' if self.paths.at('/usr/share/wayland-sessions/rpd-labwc.desktop').exists()
                       else 'LXDE-pi-labwc')
            config = self.paths.lightdm_conf.read_text()
            for name in ('user-session', 'autologin-session'):
                config = config.replace(f'#{name}=', f'{name}={session}')
            self.paths.lightdm_conf.write_text(config)
        return subprocess.CompletedProcess(command, 0)

    def apply(self, runner=None):
        with contextlib.redirect_stdout(io.StringIO()):
            ds.apply(self.user, self.paths, self.command if runner is None else runner)

    def snapshot(self):
        return {str(path.relative_to(self.base)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in self.base.rglob('*') if path.is_file()}

    def test_modern_and_legacy_function_detection(self):
        self.assertEqual(ds.boot_commands(MODERN),
                         (('do_boot_target', 'B2'), ('do_autologin', '2')))
        self.assertEqual(ds.boot_commands(LEGACY), (('do_boot_behaviour', 'B4'),))
        with self.assertRaises(ds.SetupError):
            ds.boot_commands('# do_boot_behaviour() { :; }\n')

    def test_labwc_option_detection_never_selects_wayfire(self):
        self.assertEqual(ds.wayland_command(MODERN), ('do_wayland', 'W2'))
        old_options = WAYLAND.replace('W2*)', 'W3*)')
        old_options = old_options.replace('case "$RET" in',
                                          'case "$RET" in\n W2*)\n STATUS="Wayfire on Wayland"\n ;;')
        self.assertEqual(ds.wayland_command(old_options), ('do_wayland', 'W3'))
        with self.assertRaises(ds.SetupError):
            ds.wayland_command('do_wayland() {\n W2*)\n STATUS="Wayfire on Wayland"\n ;;\n}\n')

    def test_missing_labwc_session_is_rejected_before_changes(self):
        self.paths.at('/usr/share/wayland-sessions/rpd-labwc.desktop').unlink()
        before = self.snapshot()
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)
        self.assertEqual(before, self.snapshot())

    def test_older_lxde_labwc_session_is_selected(self):
        self.paths.at('/usr/share/wayland-sessions/rpd-labwc.desktop').unlink()
        self.put('/usr/share/wayland-sessions/LXDE-pi-labwc.desktop', b'[Desktop Entry]\nExec=labwc-pi\n')
        self.assertEqual(ds.preflight(self.user, self.paths).session, 'LXDE-pi-labwc')
        self.apply()
        self.assertIn('autologin-session=LXDE-pi-labwc', self.paths.lightdm_conf.read_text())

    def test_preflight_is_read_only_and_does_not_run_commands(self):
        before = self.snapshot()
        with patch.object(ds.subprocess, 'run') as run:
            plan = ds.preflight(self.user, self.paths)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(plan.autostart_after)
        run.assert_not_called()
        self.assertFalse((self.home / '.config').exists())

    def test_missing_desktop_components_fail_without_changes(self):
        for component in ('/usr/sbin/lightdm', '/etc/init.d/lightdm',
                          '/usr/bin/raspi-config', '/etc/lightdm/lightdm.conf'):
            with self.subTest(component=component):
                path = self.paths.at(component)
                saved, mode = path.read_bytes(), path.stat().st_mode
                path.unlink()
                before = self.snapshot()
                with self.assertRaises(ds.SetupError):
                    ds.preflight(self.user, self.paths)
                self.assertEqual(before, self.snapshot())
                self.put(component, saved, stat.S_IMODE(mode))

    def test_symlinked_os_config_and_user_directories_are_rejected(self):
        lightdm = self.paths.lightdm_conf
        original = lightdm.read_bytes()
        lightdm.unlink()
        other = self.put('/etc/lightdm/original.conf', original)
        lightdm.symlink_to(other)
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)
        lightdm.unlink()
        self.put('/etc/lightdm/lightdm.conf', original)
        (self.home / '.config').symlink_to(self.base / 'elsewhere')
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)

    def test_writable_files_and_wrong_owner_are_rejected(self):
        target = self.user_autostart(BLANKING)
        target.chmod(0o666)
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)
        target.chmod(0o644)
        real_lstat = Path.lstat
        def wrong_owner(path):
            info = real_lstat(path)
            if path == target:
                values = list(info)
                values[4] = self.uid + 1
                return os.stat_result(values)
            return info
        with patch.object(Path, 'lstat', wrong_owner), self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)

    def test_preserves_unrelated_autostart_content_and_permissions(self):
        keep = (b'# Keep this comment\n/usr/bin/lxsession-xdg-autostart\n'
                b"swayidle -w timeout 10 'custom-cursor-command' &\n")
        target = self.user_autostart(keep + BLANKING)
        target.chmod(0o600)
        self.apply()
        self.assertEqual(target.read_bytes(), keep)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_uid, self.uid)
        self.assertEqual([command[2:] for command, _ in self.calls],
                         [['do_wayland', 'W2'], ['do_boot_target', 'B2'], ['do_autologin', '2']])
        self.assertTrue(all(command[0] == str(self.paths.raspi_config) for command, _ in self.calls))

    def test_no_empty_autostart_is_created_for_stock_global_defaults(self):
        self.apply()
        self.assertFalse((self.home / '.config/labwc/autostart').exists())

    def test_global_blanking_is_refused_for_merged_labwc_configuration(self):
        keep = b'/usr/bin/lxsession-xdg-autostart\n/usr/bin/kanshi &\n'
        self.paths.global_autostart.write_bytes(keep + BLANKING)
        before = self.snapshot()
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)
        self.assertEqual(self.paths.global_autostart.read_bytes(), keep + BLANKING)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.home / '.config/labwc/autostart').exists())

    def test_custom_blanking_is_refused_before_any_changes(self):
        target = self.user_autostart(BLANKING + b"swayidle -w timeout 300 'wlopm --off HDMI-A-1' &\n")
        before = self.snapshot()
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)
        self.assertEqual(before, self.snapshot())
        self.assertIn(b'HDMI-A-1', target.read_bytes())

    def test_multiline_blanking_is_refused_without_partial_rewrite(self):
        target = self.user_autostart(b"swayidle -w \\\n timeout 600 'wlopm --off \\*' \\\n resume 'wlopm --on \\*' &\n")
        before = target.read_bytes()
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths)
        self.assertEqual(before, target.read_bytes())

    def test_backup_precedes_first_privileged_command(self):
        target = self.user_autostart(BLANKING)
        original = self.paths.lightdm_conf.read_bytes()
        def checked_run(command, **kwargs):
            backups = list(self.paths.backup_root.glob('*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / '0-lightdm.conf').read_bytes(), original)
            self.assertEqual((backups[0] / '3-autostart').read_bytes(), BLANKING)
            manifest = json.loads((backups[0] / 'manifest.json').read_text())
            self.assertEqual(manifest[1]['symlink_target'], '/lib/systemd/system/multi-user.target')
            self.assertFalse((backups[0] / '1-default.target').exists())
            return self.command(command, **kwargs)
        self.apply(checked_run)
        self.assertEqual(target.read_bytes(), b'')

    def test_legacy_fallback_is_supported(self):
        self.paths.raspi_config.write_text(LEGACY)
        self.apply()
        self.assertEqual([command[2:] for command, _ in self.calls],
                         [['do_wayland', 'W2'], ['do_boot_behaviour', 'B4']])

    def test_accounts_service_and_dmrc_backups_are_saved(self):
        original = b'[User]\nXSession=rpd-x\n'
        self.put('/var/lib/AccountsService/users/pi', original, 0o600)
        dmrc = self.home / '.dmrc'
        dmrc.write_bytes(b'[Desktop]\nSession=rpd-x\n')
        self.apply()
        saved = next(self.paths.backup_root.iterdir())
        self.assertEqual((saved / '4-pi').read_bytes(), original)
        self.assertEqual((saved / '5-.dmrc').read_bytes(), dmrc.read_bytes())
        account = self.paths.at('/var/lib/AccountsService/users/pi')
        account.unlink()
        account.symlink_to(dmrc)
        with self.assertRaises(ds.SetupError):
            ds.preflight(self.user, self.paths, privileged=True)

    def test_session_selection_must_be_verified_after_official_command(self):
        target = self.user_autostart(BLANKING)
        def no_session_change(command, **kwargs):
            if command[2] != 'do_wayland':
                return self.command(command, **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ds.SetupError):
            self.apply(no_session_change)
        self.assertEqual(target.read_bytes(), BLANKING)

    def test_unverified_autologin_fails_without_touching_user_autostart(self):
        target = self.user_autostart(BLANKING)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ds.SetupError):
            self.apply(lambda *args, **kwargs: subprocess.CompletedProcess([], 0))
        self.assertEqual(target.read_bytes(), BLANKING)
        self.assertEqual(len(list(self.paths.backup_root.glob('*'))), 1)

    def test_another_autologin_user_or_nonstandard_config_is_rejected(self):
        for content in (b'[Seat:*]\nautologin-user=someone_else\n', b'[Seat:*]\n'):
            self.paths.lightdm_conf.write_bytes(content)
            with self.assertRaises(ds.SetupError):
                ds.preflight(self.user, self.paths)

    def test_change_between_preflight_and_write_is_rejected(self):
        target = self.user_autostart(BLANKING)
        plan = ds.preflight(self.user, self.paths)
        target.write_bytes(BLANKING + b'other-command &\n')
        with self.assertRaises(ds.SetupError):
            ds.write_autostart(plan)
        self.assertIn(b'other-command', target.read_bytes())

    def test_privileged_entry_requires_matching_sudo_identity(self):
        with patch.object(ds, 'desktop_user', return_value=self.user), \
                patch.object(ds.os, 'geteuid', return_value=0), \
                patch.dict(os.environ, {'SUDO_USER': 'wrong', 'SUDO_UID': str(self.uid)}), \
                patch.object(ds, 'apply') as apply, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ds.main(['--apply', 'pi']), 1)
        apply.assert_not_called()

    def test_read_only_entry_does_not_request_sudo(self):
        with patch.object(ds, 'desktop_user', return_value=self.user), \
                patch.object(ds.os, 'geteuid', return_value=1000), \
                patch.dict(os.environ, {'HOME': str(self.home)}), \
                patch.object(ds, 'preflight') as preflight, \
                patch.object(ds.subprocess, 'run') as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ds.main(['--check']), 0)
        preflight.assert_called_once_with(self.user)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
