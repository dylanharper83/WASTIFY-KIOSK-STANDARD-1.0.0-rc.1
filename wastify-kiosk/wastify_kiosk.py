#!/usr/bin/env python3
# Managed by Wastify Kiosk
"""Wastify Kiosk: desktop-session startup, bounded recovery, no display controls."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

VERSION = '1.0.0-rc.1'
MARKER = '# Managed by Wastify Kiosk'
UNIT = 'wastify-field-kiosk.service'
LEGACY = ('wastify-kiosk.service', 'wastify-simple-kiosk.service', 'kiosk.service',
          'wastify-wifi-panel.service', 'thinkgreen-kiosk.service',
          'wastify-network-recovery.service', 'wastify-maintenance.timer',
          'wastify-maintenance.service', 'wastify-scale-autoconnect.timer',
          'wastify-scale-autoconnect.service')


def run(args, check=True, **kwargs):
    kwargs.setdefault('timeout', 30)
    return subprocess.run(args, check=check, text=True, **kwargs)


def ctl(*args, check=True, **kwargs):
    # Connect shells can omit the desktop variables. Use only this user's bus.
    if sys.platform == 'linux' and not os.environ.get('XDG_RUNTIME_DIR'):
        runtime = Path(f'/run/user/{os.getuid()}')
        if runtime.is_dir() and runtime.stat().st_uid == os.getuid() and (runtime / 'bus').is_socket():
            env = dict(os.environ)
            env['XDG_RUNTIME_DIR'] = str(runtime)
            env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={runtime}/bus'
            kwargs.setdefault('env', env)
    return run(['systemctl', '--user', *args], check=check, **kwargs)


def active():
    return ctl('is-active', '--quiet', UNIT, check=False).returncode == 0


def validate_url(value):
    if not isinstance(value, str):
        raise ValueError('URL must be a string.')
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('URL must not contain spaces or control characters.')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use an HTTPS URL without embedded credentials.')
    # Accessing port also validates out-of-range or non-numeric values.
    _ = parsed.port
    if '\\' in parsed.netloc or not re.fullmatch(r'[A-Za-z0-9.:[\]-]+', parsed.netloc):
        raise ValueError('Use an ordinary HTTPS hostname, IP address or bracketed IPv6 address.')
    return value


def atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.wastify-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Layout:
    def __init__(self, home):
        self.home = Path(home)
        self.base = self.home / '.local/share/wastify-kiosk'
        self.config = self.home / '.config/wastify-kiosk/config.json'
        self.runtime = self.base / 'wastify_kiosk.py'
        self.command = self.home / '.local/bin/kiosk'
        self.compat_command = self.home / '.local/bin/wastify-kiosk'
        self.unit = self.home / '.config/systemd/user' / UNIT
        self.desktop = self.home / '.config/autostart/wastify-kiosk.desktop'
        self.enabled = self.base / 'enabled'
        self.profile = self.base / 'browser-profile'
        self.managed = (self.runtime, self.command, self.unit, self.desktop,
                        self.config, self.enabled, self.compat_command)

    def read(self):
        config = json.loads(self.config.read_text())
        if not isinstance(config, dict) or 'url' not in config:
            raise ValueError('Invalid kiosk configuration; restore a backup before starting.')
        validate_url(config['url'])
        return config

    def check_owned(self):
        for path in self.managed:
            for item in (path, *path.parents):
                if item.is_symlink():
                    raise ValueError(f'Refusing managed symlink: {item}')
                if item.exists() and item.stat().st_uid != os.getuid():
                    raise ValueError(f'Managed path belongs to another user: {item}')
                if item == self.home:
                    break
        for path in (self.runtime, self.command, self.compat_command, self.unit, self.desktop):
            if path.exists() and MARKER not in path.read_text().splitlines():
                raise ValueError(f'Existing file is not ours: {path}')

    def snapshot(self):
        folder = self.base / 'backups' / str(time.time_ns())
        folder.mkdir(parents=True, mode=0o700)
        manifest = {}
        for index, path in enumerate(self.managed):
            if path.is_symlink():
                raise ValueError(f'Refusing managed symlink: {path}')
            manifest[str(index)] = path.exists()
            if path.exists():
                shutil.copy2(path, folder / str(index))
        atomic(folder / 'manifest.json', json.dumps(manifest))
        return folder

    def snapshot_payload(self, folder):
        manifest = json.loads((folder / 'manifest.json').read_text())
        if not isinstance(manifest, dict) or set(manifest) != {str(i) for i in range(len(self.managed))}:
            raise ValueError('Invalid backup manifest; no files restored.')
        payload = []
        for index, path in enumerate(self.managed):
            if type(manifest[str(index)]) is not bool:
                raise ValueError('Invalid backup manifest value.')
            if manifest[str(index)]:
                source = folder / str(index)
                if source.is_symlink():
                    raise ValueError('Backup contains a symlink; refusing restore.')
                payload.append((path, source.read_text(), source.stat().st_mode & 0o777))
            else:
                payload.append((path, None, None))
        return payload

    def restore(self, folder, preserve=()):
        # Read every backup before making changes, so a truncated backup fails closed.
        payload = self.snapshot_payload(folder)
        for path, content, mode in payload:
            if path in preserve:
                continue
            if content is not None:
                atomic(path, content, mode)
            else:
                path.unlink(missing_ok=True)


def check_conflicts():
    for unit in LEGACY:
        for scope in ([], ['--user']):
            for state in ('is-active', 'is-enabled'):
                result = run(['systemctl', *scope, state, '--quiet', unit], check=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    raise ValueError(f'{unit} is active or enabled. Disable the old kiosk first; nothing changed.')
    # Also refuse unmanaged manual kiosks; leave regular desktop Chromium alone.
    result = run(['pgrep', '-a', '-u', str(os.getuid()), '-f', 'chromium'], check=False,
                 capture_output=True)
    if result.returncode not in (0, 1):
        raise ValueError('Could not inspect running browsers; refusing to start another one.')
    own_profile = '--user-data-dir=' + str(Path.home() / '.local/share/wastify-kiosk/browser-profile')
    for line in result.stdout.splitlines():
        words = line.split()
        if '--kiosk' in words and own_profile not in words:
            raise ValueError('A different Chromium kiosk is running. Stop that test before installing/starting this kiosk.')


def session_ready():
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    display = os.environ.get('WAYLAND_DISPLAY', '')
    if runtime != Path(f'/run/user/{os.getuid()}'):
        raise ValueError('Desktop runtime directory belongs to a different user.')
    if not re.fullmatch(r'wayland-[0-9]+', display):
        raise ValueError('No desktop session environment. Log into the Pi desktop first.')
    if not (runtime / display).is_socket() or not (runtime / 'bus').is_socket():
        raise ValueError('Desktop or session bus is unavailable. Do not restart Connect or the display.')


def preflight(layout, url):
    """Read-only checks run before the installer changes any packages or files."""
    if sys.platform != 'linux':
        raise ValueError('Use Raspberry Pi OS Trixie Desktop 64-bit on Pi 5 or CM4.')
    os_info = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                   if '=' in line and not line.startswith('#'))
    if os_info.get('VERSION_CODENAME', '').strip('"\'') != 'trixie':
        raise ValueError('Use Raspberry Pi OS Trixie Desktop 64-bit. This does not upgrade the OS.')
    if run(['dpkg', '--print-architecture'], capture_output=True).stdout.strip() != 'arm64':
        raise ValueError('A 64-bit (arm64) installation is required.')
    model = Path('/proc/device-tree/model').read_text().rstrip('\0\n')
    if not (model.startswith('Raspberry Pi 5 Model ') or
            re.match(r'^Raspberry Pi Compute Module 4(?: Rev|$)', model)):
        raise ValueError(f'Unsupported model: {model}. This standard supports Pi 5 and CM4.')
    if not any(Path('/usr/share/wayland-sessions').glob('*.desktop')):
        raise ValueError('A Wayland desktop is required. Raspberry Pi OS Lite is not supported.')
    ctl('show-environment', stdout=subprocess.DEVNULL)
    check_conflicts()
    layout.check_owned()
    if url is None:
        url = layout.read()['url'] if layout.config.exists() else None
    if not url:
        raise ValueError('Supply this unit\'s HTTPS site URL on first installation.')
    return validate_url(url)


def install(layout, url):
    url = preflight(layout, url)
    if not shutil.which('chromium'):
        raise ValueError('Chromium is missing. Install it using: sudo apt-get install chromium')
    if not shutil.which('curl'):
        raise ValueError('curl is missing. Install it using: sudo apt-get install curl')
    backup = layout.snapshot()
    was_active = active()
    source = Path(__file__).read_text()
    if layout.unit.exists():
        # A failed stop must abort before replacing a live runtime.
        ctl('stop', UNIT)
    try:
        atomic(layout.runtime, source, 0o700)
        # Paths are restricted below, so both desktop Exec and systemd quoting are safe.
        atomic(layout.command, f'#!/bin/sh\n{MARKER}\nexec /usr/bin/python3 "{layout.runtime}" "$@"\n', 0o700)
        atomic(layout.compat_command, layout.command.read_text(), 0o700)
        atomic(layout.config, json.dumps({'url': url, 'version': VERSION}, indent=2) + '\n')
        atomic(layout.unit, f'''# Managed by Wastify Kiosk
[Unit]
Description=Wastify Kiosk
PartOf=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=simple
ExecStart=/usr/bin/python3 "{layout.runtime}" launch
Restart=always
RestartSec=10
RestartPreventExitStatus=78
TimeoutStopSec=20
KillMode=control-group
UMask=0077
''')
        atomic(layout.desktop, f'''{MARKER}
[Desktop Entry]
Type=Application
Name=Wastify Kiosk
Exec=/usr/bin/python3 "{layout.runtime}" session-start
Terminal=false
''')
        # New installs are staged, not activated. Updating preserves enabled state.
        ctl('daemon-reload')
        if was_active:
            ctl('reset-failed', UNIT, check=False)
            ctl('start', UNIT)
    except BaseException:
        layout.restore(backup)
        ctl('daemon-reload', check=False)
        if was_active:
            ctl('start', UNIT, check=False)
        raise
    print(f'Wastify Kiosk {VERSION} installed. Backup: {backup}')
    print(f'Activate from Connect remote shell: {layout.command} enable')
    print('Fresh profile: grant camera and scale access on first use. Existing browser data was not changed.')
    print('If the command is not found, open a new shell or use the full path above.')


def launch(layout):
    try:
        session_ready()
        check_conflicts()
        url = validate_url(layout.read()['url'])
        layout.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Chromium owns its singleton lock. Never delete it or risk reusing another instance.
        singleton = layout.profile / 'SingletonLock'
        if os.path.lexists(singleton):
            # Chromium can reclaim a stale lock after a local crash. Refuse live or
            # foreign ownership, but never remove Chromium's lock ourselves.
            owner = os.readlink(singleton)
            host, _, pid = owner.rpartition('-')
            if host != socket.gethostname() or not pid.isdigit() or Path('/proc', pid).exists():
                raise ValueError('Browser profile is in use or has an unfamiliar lock. No lock was deleted.')
        binary = shutil.which('chromium')
        if not binary:
            raise ValueError('Chromium is missing.')
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'Kiosk refused to launch: {exc}', file=sys.stderr, flush=True)
        return 78
    args = [binary, '--ozone-platform=wayland', '--kiosk', '--no-first-run',
            '--no-default-browser-check', f'--user-data-dir={layout.profile}', url]
    print('Starting Chromium on the existing desktop; camera/serial permissions remain site-controlled.', flush=True)
    os.execv(binary, args)


def import_session():
    # Called only by the desktop, never guesses a display socket from the remote shell.
    session_ready()
    for name in ('XDG_RUNTIME_DIR', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS'):
        if name in os.environ:
            ctl('import-environment', name)


def prepare_from_remote():
    if os.environ.get('WAYLAND_DISPLAY'):
        import_session()
        return
    # Prefer the environment the real desktop already imported into its user manager.
    output = ctl('show-environment', capture_output=True).stdout
    values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    saved = os.environ.copy()
    try:
        for name in ('WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR'):
            os.environ.pop(name, None)
            if name in values:
                os.environ[name] = values[name]
        try:
            session_ready()
        except ValueError:
            # A first Connect shell may predate autostart. Use only an unambiguous
            # socket owned by this desktop user, never another user's desktop.
            runtime = Path(f'/run/user/{os.getuid()}')
            candidates = [p for p in runtime.glob('wayland-*')
                          if re.fullmatch(r'wayland-[0-9]+', p.name) and p.is_socket()
                          and p.stat().st_uid == os.getuid()]
            if len(candidates) != 1:
                raise ValueError('No unambiguous desktop session. Desktop login is required.') from None
            os.environ['XDG_RUNTIME_DIR'] = str(runtime)
            os.environ['WAYLAND_DISPLAY'] = candidates[0].name
            session_ready()
        os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + os.environ.get(
            'XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}') + '/bus'
        import_session()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def wifi():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Run kiosk wifi in an interactive terminal, or use the desktop network menu.')
    if not all(shutil.which(name) for name in ('nmcli', 'nmtui', 'timeout')):
        raise ValueError('NetworkManager text menu is unavailable; use the desktop network menu.')
    print('Wi-Fi menu closes after 150 seconds; confirm the connection within 180 seconds.\n'
          'NetworkManager will attempt rollback if it is not confirmed.', flush=True)
    run(['sudo', 'nmcli', 'device', 'checkpoint', '--timeout', '180', '--',
         'timeout', '--foreground', '150', 'nmtui'], timeout=None)


def diagnostic_report(layout):
    """Inspect availability only; never infer a successful weight, upload or panel output."""
    report = {'version': VERSION, 'application': 'unverified', 'physical_display': 'unverified'}
    model = Path('/proc/device-tree/model')
    report['model'] = model.read_text().rstrip('\0\n') if model.exists() else 'unavailable'
    report['autostart'] = layout.enabled.exists()
    report['browser_service'] = 'active' if active() else 'inactive'
    report['free_storage_mib'] = shutil.disk_usage(layout.home).free // (1024 * 1024)
    report['camera_nodes'] = [p.name for p in Path('/dev').glob('video*') if p.is_char_device()]
    serial = list(Path('/dev').glob('ttyUSB*')) + list(Path('/dev').glob('ttyACM*'))
    report['serial_nodes'] = [{'name': p.name, 'read_write_access': os.access(p, os.R_OK | os.W_OK)}
                              for p in serial if p.is_char_device()]
    # No URL, network names, addresses, credentials or browser/journal contents in support output.
    if shutil.which('nmcli'):
        state = run(['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device', 'status'],
                    check=False, capture_output=True)
        report['network_devices'] = state.stdout.splitlines() if state.returncode == 0 else ['unavailable']
    else:
        report['network_devices'] = ['unavailable']
    if layout.config.exists() and shutil.which('curl'):
        try:
            result = run(['curl', '--silent', '--location', '--proto', '=https',
                          '--proto-redir', '=https', '--connect-timeout', '5', '--max-time', '10',
                          '--output', '/dev/null', '--write-out', '%{http_code}',
                          '--url', layout.read()['url']], check=False, capture_output=True, timeout=12)
            report['site_http'] = result.stdout.strip() if result.returncode == 0 else 'unreachable'
        except subprocess.TimeoutExpired:
            report['site_http'] = 'timed out'
        except (ValueError, OSError):
            report['site_http'] = 'invalid or unreadable configuration'
    else:
        report['site_http'] = 'unavailable'
    return report


def support(layout):
    report = diagnostic_report(layout)
    with tempfile.NamedTemporaryFile(mode='w', prefix='wastify-kiosk-support-', suffix='.json',
                                     encoding='utf-8', delete=False) as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
        path = stream.name
    print(f'Support report: {path}')
    print('Detection does not verify camera capture, scale readings, uploads or physical display.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='help', choices=[
        'help', 'preflight', 'install', 'enable', 'disable', 'start', 'stop', 'restart',
        'status', 'url', 'logs', 'check', 'support', 'wifi', 'version', 'update',
        'rollback', 'uninstall', 'session-start', 'launch'])
    parser.add_argument('value', nargs='?')
    args = parser.parse_args()
    if args.action == 'help':
        print('Wastify Kiosk ' + VERSION + '\n'
              'kiosk status | start | stop | restart | enable | disable\n'
              'kiosk url [HTTPS_URL]       Show or change this unit\'s site\n'
              'kiosk wifi                 NetworkManager text menu (keyboard)\n'
              'kiosk check | logs | support | version\n'
              'kiosk update PACKAGE_DIR   Apply a downloaded, verified release\n'
              'kiosk rollback             Restore previous code; keep current site and browser data\n'
              'stop exits to the desktop; disable also turns off boot startup.\n'
              'Finish weighing before a restart, URL change or update.')
        return 0
    if args.action == 'version':
        print(VERSION)
        return 0
    if os.geteuid() == 0:
        raise ValueError('Run as the desktop user, without sudo. Only Wi-Fi or dependency setup requests sudo.')
    home = str(Path.home())
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', home):
        raise ValueError('Home path contains unsupported characters.')
    layout = Layout(home)
    if args.value is not None and args.action not in ('preflight', 'install', 'url', 'update'):
        raise ValueError('This command takes no additional argument.')
    if args.action == 'preflight':
        preflight(layout, args.value)
        print('Preflight passed: Trixie Desktop arm64, Pi 5/CM4, no conflicting kiosk.')
        return 0
    if args.action == 'check':
        print(json.dumps(diagnostic_report(layout), indent=2))
        print('Device detection and HTTP status do not establish application or physical-display health.')
        return 0
    if args.action == 'support':
        support(layout)
        return 0
    if args.action == 'update':
        if args.value is None:
            raise ValueError('Download the approved release, then use: kiosk update /path/to/package')
        package = Path(args.value).resolve()
        installer = package / 'install.sh'
        if not installer.is_file() or not (package / 'wastify-kiosk/wastify_kiosk.py').is_file():
            raise ValueError('Not a Wastify Kiosk package directory.')
        os.execv('/bin/bash', ['bash', str(installer), '--no-start'])
        return 0
    if args.action == 'launch':
        return launch(layout)
    if args.action == 'install':
        preflight(layout, args.value)  # Do not create state for an unsupported device.
    layout.base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (layout.base / 'control.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another kiosk management command is running. Try again shortly.') from None
        if args.action == 'install':
            install(layout, args.value)
        elif args.action == 'session-start':
            import_session()
            if layout.enabled.exists():
                check_conflicts()
                ctl('stop', UNIT)
                ctl('reset-failed', UNIT, check=False)
                ctl('start', UNIT)
        elif args.action in ('start', 'restart', 'enable'):
            check_conflicts()
            if args.action == 'enable':
                atomic(layout.enabled, 'enabled\n')
            try:
                prepare_from_remote()
            except ValueError as exc:
                if args.action != 'enable':
                    raise
                print(f'Enabled for the next desktop login. Current start deferred: {exc}')
                return 0
            ctl('reset-failed', UNIT, check=False)
            ctl('restart' if args.action == 'restart' else 'start', UNIT)
            print('Start requested. Use status and confirm the physical screen, camera and scale.')
        elif args.action in ('stop', 'disable'):
            if args.action == 'disable':
                layout.enabled.unlink(missing_ok=True)
            ctl('stop', UNIT)
            print('Kiosk stopped. ' + ('Desktop autostart disabled.' if args.action == 'disable' else 'Desktop autostart unchanged.'))
        elif args.action == 'status':
            print(f'Wastify Kiosk {VERSION}; autostart: {layout.enabled.exists()}')
            print('URL:', layout.read()['url'])
            ctl('--no-pager', '--full', 'status', UNIT, check=False)
            print('Running does not prove physical-screen health. See logs if startup failed.')
        elif args.action == 'logs':
            run(['journalctl', '--user', '-u', UNIT, '-n', '100', '--no-pager'], check=False)
        elif args.action == 'url':
            config = layout.read()
            if args.value is not None:
                config['url'] = validate_url(args.value)
                atomic(layout.config, json.dumps(config, indent=2) + '\n')
                if active():
                    ctl('restart', UNIT)
            print(config['url'])
        elif args.action == 'wifi':
            wifi()
        elif args.action == 'rollback':
            backups = sorted((layout.base / 'backups').glob('*/manifest.json'))
            if not backups:
                raise ValueError('No backup available.')
            layout.check_owned()
            previous = backups[-1].parent
            payload = layout.snapshot_payload(previous)
            if any(content is None for path, content, mode in payload
                   if path in (layout.runtime, layout.unit, layout.command)):
                raise ValueError('No previous installed release. Use kiosk uninstall to remove this first installation.')
            if layout.unit.exists():
                ctl('stop', UNIT)
            layout.restore(previous, preserve=(layout.config,))
            ctl('daemon-reload')
            print('Previous files restored. Kiosk left stopped for inspection; browser data retained.')
        elif args.action == 'uninstall':
            layout.check_owned()
            layout.snapshot()
            if layout.unit.exists():
                ctl('stop', UNIT)
            for path in (layout.unit, layout.desktop, layout.command, layout.compat_command,
                         layout.runtime, layout.enabled):
                path.unlink(missing_ok=True)
            ctl('daemon-reload')
            print('Kiosk removed. Configuration, browser data and backups retained. Wi-Fi profiles unchanged.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'Wastify Kiosk: {error}', file=sys.stderr)
        sys.exit(1)
