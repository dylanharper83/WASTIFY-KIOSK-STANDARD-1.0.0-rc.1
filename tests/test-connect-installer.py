#!/usr/bin/env python3
"""Run the complete Connect paste block in an isolated, simulated Pi shell.

Only the embedded release extractor runs real Python. Controller, desktop,
dependency and reboot operations are shadowed before the installer runs. No
system service, network, privilege or machine configuration is touched.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / 'INSTALL-VIA-CONNECT.txt'
PYTHON = sys.executable
BASH = shutil.which('bash')


@unittest.skipIf(os.geteuid() == 0, 'The real shell installer rejects root by design.')
class ConnectInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wastify-connect-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.bin = self.base / 'bin'
        self.tmp = self.base / 'tmp'
        for directory in (self.home, self.bin, self.tmp):
            directory.mkdir()
        self.events_file = self.base / 'events.jsonl'
        self.runtime = self.home / '.local/share/wastify-kiosk/wastify_kiosk.py'
        self.config = self.home / '.config/wastify-kiosk/config.json'
        self.environment = dict(os.environ)
        self.environment.update({
            'HOME': str(self.home),
            'PATH': str(self.bin) + os.pathsep + '/usr/bin:/bin:/usr/sbin:/sbin',
            'TMPDIR': str(self.tmp),
            'WASTIFY_TEST_EVENTS': str(self.events_file),
            'WASTIFY_TEST_FAIL': '',
        })
        self.write_executable(self.bin / 'python3', '''
import json, os, pathlib, sys
args = sys.argv[1:]
if args and args[0] == '-':
    os.execv(REAL_PYTHON, [REAL_PYTHON, *args])
name = pathlib.Path(args[0]).name
if name == 'wastify_kiosk.py':
    kind, stage = 'core', args[1]
elif name == 'desktop_setup.py':
    kind = 'desktop'
    stage = 'desktop-check' if args[1:] == ['--check'] else 'desktop-apply'
else:
    raise SystemExit('Unexpected Python script: ' + repr(args))
with open(os.environ['WASTIFY_TEST_EVENTS'], 'a') as stream:
    stream.write(json.dumps({'kind': kind, 'args': args[1:]}) + '\\n')
if os.environ.get('WASTIFY_TEST_FAIL') == stage:
    print('Simulated failure: ' + stage, file=sys.stderr)
    raise SystemExit(37)
if kind == 'core' and stage == 'install':
    runtime = pathlib.Path.home() / '.local/share/wastify-kiosk/wastify_kiosk.py'
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text('# simulated installed controller\\n')
''')
        self.write_executable(self.bin / 'sudo', '''
import json, os, sys
with open(os.environ['WASTIFY_TEST_EVENTS'], 'a') as stream:
    stream.write(json.dumps({'kind': 'sudo', 'args': sys.argv[1:]}) + '\\n')
if sys.argv[1:] != ['/usr/bin/systemctl', 'reboot']:
    raise SystemExit('Unexpected privileged command; test refuses it.')
''')
        self.write_executable(self.home / '.local/bin/kiosk', '''
import json, os, sys
with open(os.environ['WASTIFY_TEST_EVENTS'], 'a') as stream:
    stream.write(json.dumps({'kind': 'kiosk', 'args': sys.argv[1:]}) + '\\n')
if os.environ.get('WASTIFY_TEST_FAIL') == sys.argv[1]:
    print('Simulated failure: ' + sys.argv[1], file=sys.stderr)
    raise SystemExit(37)
''')
        for command in ('chromium', 'curl', 'apt-get'):
            self.write_executable(self.bin / command, '''
raise SystemExit('This command must only be discovered, never executed.')
''')

    def write_executable(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!' + PYTHON + '\nREAL_PYTHON = ' + repr(PYTHON) + '\n' + source)
        path.chmod(0o700)

    def run_bootstrap(self, failure='', existing=False):
        self.environment['WASTIFY_TEST_FAIL'] = failure
        if existing:
            self.runtime.parent.mkdir(parents=True, exist_ok=True)
            self.runtime.write_text('# existing controller\n')
            self.config.parent.mkdir(parents=True, exist_ok=True)
            self.config.write_text('{"url":"https://existing.example.test/unit","enabled":false}\n')
        master, slave = pty.openpty()

        def own_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            [BASH, str(BOOTSTRAP)], stdin=slave, stdout=slave, stderr=slave,
            env=self.environment, cwd=self.base, preexec_fn=own_terminal,
        )
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 10
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    self.fail('Connect paste block waited for input or hung:\n' + output.decode(errors='replace'))
                ready, _, _ = select.select([master], [], [], min(remaining, .1))
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                elif process.poll() is not None:
                    break
            process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        events = [json.loads(line) for line in self.events_file.read_text().splitlines()] if self.events_file.exists() else []
        return process.returncode, output.decode(errors='replace'), events

    def test_fresh_install_configures_enables_and_reboots_without_input(self):
        code, output, events = self.run_bootstrap()
        self.assertEqual(code, 0, output)
        self.assertIn('Verified Wastify Kiosk ', output)
        self.assertNotIn('Unit HTTPS URL', output)
        self.assertEqual(events, [
            {'kind': 'core', 'args': ['preflight']},
            {'kind': 'desktop', 'args': ['--check']},
            {'kind': 'desktop', 'args': []},
            {'kind': 'core', 'args': ['install']},
            {'kind': 'kiosk', 'args': ['enable']},
            {'kind': 'kiosk', 'args': ['status']},
            {'kind': 'sudo', 'args': ['/usr/bin/systemctl', 'reboot']},
        ])
        self.assertTrue(self.runtime.is_file())
        self.assertEqual(list(self.tmp.iterdir()), [], 'Extracted release must be cleaned up.')

    def test_existing_install_omits_url_and_preserves_disabled_state_without_reboot(self):
        code, output, events = self.run_bootstrap(existing=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(events, [
            {'kind': 'core', 'args': ['preflight']},
            {'kind': 'core', 'args': ['install']},
            {'kind': 'kiosk', 'args': ['status']},
        ])
        self.assertEqual(self.config.read_text(), '{"url":"https://existing.example.test/unit","enabled":false}\n')
        self.assertNotIn('Rebooting now', output)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_failed_preflight_aborts_before_desktop_install_or_privileged_commands(self):
        code, output, events = self.run_bootstrap('preflight')
        self.assertEqual(code, 37, output)
        self.assertEqual(events, [{'kind': 'core', 'args': ['preflight']}])
        self.assertFalse(self.runtime.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_failed_desktop_check_aborts_before_apply_install_or_reboot(self):
        code, output, events = self.run_bootstrap('desktop-check')
        self.assertEqual(code, 37, output)
        self.assertEqual(events, [
            {'kind': 'core', 'args': ['preflight']},
            {'kind': 'desktop', 'args': ['--check']},
        ])
        self.assertFalse(self.runtime.exists())

    def test_failed_desktop_apply_aborts_before_install_or_reboot(self):
        code, output, events = self.run_bootstrap('desktop-apply')
        self.assertEqual(code, 37, output)
        self.assertEqual(events, [
            {'kind': 'core', 'args': ['preflight']},
            {'kind': 'desktop', 'args': ['--check']},
            {'kind': 'desktop', 'args': []},
        ])
        self.assertFalse(self.runtime.exists())

    def test_failed_controller_install_aborts_before_enable_or_reboot(self):
        code, output, events = self.run_bootstrap('install')
        self.assertEqual(code, 37, output)
        self.assertEqual(events[-1], {'kind': 'core', 'args': ['install']})
        self.assertFalse(any(event['kind'] in ('kiosk', 'sudo') for event in events))
        self.assertFalse(self.runtime.exists())

    def test_failed_enable_reports_error_and_never_reboots(self):
        code, output, events = self.run_bootstrap('enable')
        self.assertEqual(code, 1, output)
        self.assertIn('activation failed', output)
        self.assertEqual(events[-1], {'kind': 'kiosk', 'args': ['enable']})
        self.assertFalse(any(event['kind'] == 'sudo' for event in events))

    def test_failed_status_prevents_automatic_reboot(self):
        code, output, events = self.run_bootstrap('status')
        self.assertEqual(code, 37, output)
        self.assertEqual(events[-1], {'kind': 'kiosk', 'args': ['status']})
        self.assertFalse(any(event['kind'] == 'sudo' for event in events))


if __name__ == '__main__':
    unittest.main()
