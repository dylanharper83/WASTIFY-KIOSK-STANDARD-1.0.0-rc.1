#!/usr/bin/env python3
"""Prepare the stock Pi desktop on a fresh kiosk install.

The outer installer decides whether this is a fresh installation and whether to
reboot afterwards. This helper never restarts a display, Connect, or the Pi.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class DesktopUser:
    name: str
    uid: int
    gid: int
    home: Path


@dataclass(frozen=True)
class Paths:
    base: Path = Path('/')
    root_uid: int = 0

    def at(self, name):
        return self.base / name.lstrip('/')

    @property
    def raspi_config(self):
        return self.at('/usr/bin/raspi-config')

    @property
    def lightdm_conf(self):
        return self.at('/etc/lightdm/lightdm.conf')

    @property
    def global_autostart(self):
        return self.at('/etc/xdg/labwc/autostart')

    @property
    def backup_root(self):
        return self.at('/var/backups/wastify-kiosk')


@dataclass(frozen=True)
class Plan:
    user: DesktopUser
    commands: tuple
    session: str
    autostart_before: bytes | None
    autostart_after: bytes | None
    autostart_mode: int


def guard_directory_chain(directory, stop, owner):
    """Reject redirected, incorrectly owned, or writable managed directories."""
    directory, stop = Path(directory), Path(stop)
    if not directory.is_relative_to(stop):
        raise SetupError(f'Path is outside its managed root: {directory}')
    chain = [stop]
    current = stop
    for component in directory.relative_to(stop).parts:
        current = current / component
        chain.append(current)
    for current in chain:
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
            raise SetupError(f'Unsafe directory ownership or type: {current}')
        if info.st_mode & 0o022:
            raise SetupError(f'Managed directory is writable by other users: {current}')


def guard_file(path, owner, required=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise SetupError(f'Required desktop file is missing: {path}')
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
        raise SetupError(f'Unsafe file ownership or type: {path}')
    if info.st_mode & 0o022:
        raise SetupError(f'Managed file is writable by other users: {path}')
    return info


def guarded_os_file(paths, name, required=False):
    path = paths.at(name)
    guard_directory_chain(path.parent, paths.base, paths.root_uid)
    return guard_file(path, paths.root_uid, required)


def desktop_user(name=None):
    account = pwd.getpwnam(name) if name else pwd.getpwuid(os.getuid())
    user = DesktopUser(account.pw_name, account.pw_uid, account.pw_gid,
                       Path(account.pw_dir))
    if user.uid == 0 or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', user.name):
        raise SetupError('Run desktop setup as the normal Pi desktop user.')
    if not user.home.is_absolute():
        raise SetupError('The desktop account must have an absolute home directory.')
    return user


def boot_commands(source):
    def has(name):
        return re.search(r'^\s*' + re.escape(name) + r'\s*\(\s*\)\s*\{',
                         source, re.MULTILINE) is not None
    if has('do_boot_target') and has('do_autologin'):
        return (('do_boot_target', 'B2'), ('do_autologin', '2'))
    if has('do_boot_behaviour'):
        return (('do_boot_behaviour', 'B4'),)
    raise SetupError('This raspi-config does not support desktop autologin setup.')


def wayland_command(source):
    """Select Labwc by its case body; W2/W3 changed between Pi OS releases."""
    function = re.search(r'^\s*do_wayland\s*\(\s*\)\s*\{(.*?)^\}',
                         source, re.MULTILINE | re.DOTALL)
    if function:
        for option, body in re.findall(r'^\s*(W[23])\*\)(.*?)^\s*;;',
                                       function.group(1), re.MULTILINE | re.DOTALL):
            if re.search(r'STATUS=[\'\"]Labwc on Wayland[\'\"]', body):
                return ('do_wayland', option)
    raise SetupError('This raspi-config cannot select the supported Labwc desktop.')


def without_stock_blanking(content):
    """Remove complete stock single-line blanking commands, preserve other jobs.

    Custom or multiline blanking is refused rather than partially rewritten.
    The normal Pi command optionally has a full path for swayidle or wlopm.
    """
    result = []
    for line in content.splitlines(keepends=True):
        text = line.decode('utf-8', errors='strict').strip()
        if not text or text.startswith('#'):
            result.append(line)
            continue
        if not re.search(r'\bwlopm\s+--off\b', text):
            result.append(line)
            continue
        pattern = (r'(?:/usr/bin/)?swayidle\s+-w\s+timeout\s+\d+\s+'
                   r"'?(?:/usr/bin/)?wlopm --off \\?\*'?\s+resume\s+"
                   r"'?(?:/usr/bin/)?wlopm --on \\?\*'?\s*&")
        if not re.fullmatch(pattern, text):
            raise SetupError('Custom swayidle blanking was found; refusing to rewrite it.')
    return b''.join(result)


def preflight(user, paths=Paths(), privileged=False):
    guard_directory_chain(user.home, user.home, user.uid)
    if not user.home.is_dir():
        raise SetupError('The desktop home directory is missing.')
    for name in ('/usr/bin/raspi-config', '/etc/lightdm/lightdm.conf',
                 '/etc/init.d/lightdm', '/usr/sbin/lightdm'):
        guarded_os_file(paths, name, required=True)
    if not os.access(paths.raspi_config, os.X_OK):
        raise SetupError('raspi-config is not executable.')
    source = paths.raspi_config.read_text(encoding='utf-8')
    commands = (wayland_command(source),) + boot_commands(source)
    modern_session = (paths.at('/usr/share/xsessions/rpd-x.desktop').exists()
                      or paths.at('/usr/share/wayland-sessions/rpd-labwc.desktop').exists())
    session = 'rpd-labwc' if modern_session else 'LXDE-pi-labwc'
    guarded_os_file(paths, f'/usr/share/wayland-sessions/{session}.desktop', required=True)
    guarded_os_file(paths, '/usr/bin/labwc', required=True)
    config = paths.lightdm_conf.read_text(encoding='utf-8')
    # These are the exact forms that the official utility can update.
    if not re.search(r'^#?autologin-user=.*$', config, re.MULTILINE):
        raise SetupError('LightDM has no standard autologin-user setting.')
    for name in ('user-session', 'autologin-session', 'greeter-session'):
        if not re.search(r'^#?' + name + r'=.*$', config, re.MULTILINE):
            raise SetupError(f'LightDM has no standard {name} setting.')
    existing = re.findall(r'^autologin-user=(.*)$', config, re.MULTILINE)
    if any(value.strip() not in ('', user.name) for value in existing):
        raise SetupError('LightDM already auto-logs in a different desktop user.')
    guarded_os_file(paths, '/etc/systemd/system/getty@tty1.service.d/autologin.conf')
    if privileged:
        # AccountsService's users directory is deliberately unreadable to
        # normal accounts. The sudo child validates it before all mutations.
        guarded_os_file(paths, f'/var/lib/AccountsService/users/{user.name}')
    guard_file(user.home / '.dmrc', user.uid)
    # systemctl set-default manages this intentional system symlink.
    default_target = paths.at('/etc/systemd/system/default.target')
    guard_directory_chain(default_target.parent, paths.base, paths.root_uid)
    if default_target.is_symlink():
        if default_target.lstat().st_uid != paths.root_uid:
            raise SetupError('Unsafe default systemd target ownership.')
    elif default_target.exists():
        raise SetupError('The default systemd target is not a managed symlink.')
    autostart = user.home / '.config/labwc/autostart'
    guard_directory_chain(autostart.parent, user.home, user.uid)
    info = guard_file(autostart, user.uid)
    before = autostart.read_bytes() if info else None
    mode = stat.S_IMODE(info.st_mode) if info else 0o644
    global_info = guarded_os_file(paths, '/etc/xdg/labwc/autostart')
    global_content = paths.global_autostart.read_bytes() if global_info else b''
    if without_stock_blanking(global_content) != global_content:
        # Pi launches labwc with merged configuration: a user copy cannot
        # suppress an idle process started by the system autostart file.
        raise SetupError('System labwc autostart contains custom screen blanking; '
                         'the stock Pi desktop stores blanking in the user file.')
    if before is not None:
        cleaned = without_stock_blanking(before)
        after = cleaned if cleaned != before else None
    else:
        after = None
    return Plan(user, commands, session, before, after, mode)


def backup(paths, plan):
    guard_directory_chain(paths.backup_root, paths.base, paths.root_uid)
    paths.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = paths.backup_root / f'{stamp}-desktop-{uuid.uuid4().hex[:8]}'
    destination.mkdir(mode=0o700)
    records = []
    targets = [paths.lightdm_conf, paths.at('/etc/systemd/system/default.target'),
               paths.at('/etc/systemd/system/getty@tty1.service.d/autologin.conf'),
               plan.user.home / '.config/labwc/autostart',
               paths.at(f'/var/lib/AccountsService/users/{plan.user.name}'),
               plan.user.home / '.dmrc']
    for index, path in enumerate(targets):
        record = {'path': str(path), 'exists': False}
        if path.exists() or path.is_symlink():
            info = path.lstat()
            record.update(exists=True, uid=info.st_uid, gid=info.st_gid,
                          mode=stat.S_IMODE(info.st_mode))
            if stat.S_ISLNK(info.st_mode):
                record['symlink_target'] = os.readlink(path)
            else:
                filename = f'{index}-{path.name}'
                (destination / filename).write_bytes(path.read_bytes())
                (destination / filename).chmod(0o600)
                record['backup'] = filename
        records.append(record)
    manifest = destination / 'manifest.json'
    manifest.write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
    manifest.chmod(0o600)
    return destination


def write_autostart(plan):
    if plan.autostart_after is None:
        return
    target = plan.user.home / '.config/labwc/autostart'
    guard_directory_chain(target.parent, plan.user.home, plan.user.uid)
    info = guard_file(target, plan.user.uid)
    current = target.read_bytes() if info else None
    if current != plan.autostart_before:
        raise SetupError('The desktop autostart file changed during setup.')
    # Missing directories are created with the desktop user's ownership.
    for directory in (plan.user.home / '.config', target.parent):
        if not directory.exists():
            directory.mkdir(mode=0o700)
            os.chown(directory, plan.user.uid, plan.user.gid)
    descriptor, temporary = tempfile.mkstemp(prefix='.wastify-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(plan.autostart_after)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, plan.autostart_mode)
        os.chown(temporary, plan.user.uid, plan.user.gid)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def apply(user, paths=Paths(), run=subprocess.run):
    plan = preflight(user, paths, privileged=True)
    destination = backup(paths, plan)
    print(f'Desktop settings backup: {destination}', flush=True)
    try:
        for command in plan.commands:
            run([str(paths.raspi_config), 'nonint', *command], check=True)
        # Verify the utility changed the intended user's setting.
        config = paths.lightdm_conf.read_text(encoding='utf-8')
        if not re.search(r'^autologin-user=' + re.escape(user.name) + r'$',
                         config, re.MULTILINE):
            raise SetupError('LightDM desktop autologin could not be verified.')
        for name in ('user-session', 'autologin-session'):
            if not re.search(r'^' + name + '=' + re.escape(plan.session) + r'$',
                             config, re.MULTILINE):
                raise SetupError('LightDM Labwc session selection could not be verified.')
        write_autostart(plan)
    except Exception:
        print(f'Desktop setup did not complete. Original settings are in {destination}.',
              file=sys.stderr)
        raise
    print('Labwc desktop, desktop boot, desktop autologin and persistent screen blanking configured.')
    print('These settings take effect on the next reboot; no services were restarted.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read-only desktop preflight.')
    parser.add_argument('--apply', metavar='USER', help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        if arguments.apply:
            user = desktop_user(arguments.apply)
            if (os.geteuid() != 0 or os.environ.get('SUDO_USER') != user.name
                    or os.environ.get('SUDO_UID') != str(user.uid)):
                raise SetupError('Privileged setup must be started by the normal desktop user.')
            apply(user)
        else:
            if os.geteuid() == 0:
                raise SetupError('Run this installer as the desktop user, without sudo.')
            user = desktop_user()
            if Path(os.environ.get('HOME', '')) != user.home:
                raise SetupError('HOME does not match the logged-in desktop account.')
            preflight(user)
            if arguments.check:
                print('Desktop setup preflight passed.')
                return 0
            if shutil.which('sudo') is None:
                raise SetupError('sudo is required to prepare desktop startup.')
            script = Path(__file__).absolute()
            guard_file(script, user.uid, required=True)
            print('Preparing desktop startup automatically; sudo may ask for your password.', flush=True)
            subprocess.run(['sudo', sys.executable, str(script), '--apply', user.name], check=True)
        return 0
    except (SetupError, OSError, UnicodeError, subprocess.CalledProcessError) as error:
        print(f'Desktop setup: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
