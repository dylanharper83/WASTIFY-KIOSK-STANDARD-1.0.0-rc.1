"""The short launcher must never execute a partial or changed installer."""
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CODE = (ROOT / 'go').read_text().split("<<'WASTIFY_BOOTSTRAP'\n", 1)[1].rsplit('\nWASTIFY_BOOTSTRAP', 1)[0]


class ShortLauncherTests(unittest.TestCase):
    def setUp(self):
        self.payload = (ROOT / 'INSTALL-VIA-CONNECT.txt').read_bytes()
        namespace = {}
        # Load definitions without executing the entry point.
        exec(CODE.rsplit('\nraise SystemExit(main())', 1)[0], namespace)
        self.main = namespace['main']

    def invoke(self, payload, child_status=0, root=False):
        executed = []
        def child(command, **kwargs):
            self.assertEqual(command[0], 'bash')
            script = Path(command[1])
            self.assertEqual(script.stat().st_mode & 0o777, 0o600)
            executed.append((script, script.read_bytes()))
            return subprocess.CompletedProcess(command, child_status)
        with patch('os.geteuid', return_value=0 if root else 1000), \
                patch('urllib.request.urlopen', return_value=io.BytesIO(payload)) as download, \
                patch('subprocess.run', side_effect=child) as run, \
                patch('builtins.open', return_value=io.BytesIO()) as terminal, \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            status = self.main()
        return status, executed, download, run, terminal

    def test_success_runs_only_verified_bytes_with_terminal_and_cleans_up(self):
        status, executed, download, _, terminal = self.invoke(self.payload)
        self.assertEqual(status, 0)
        self.assertIn('/v1.0.0-rc.2/', download.call_args.args[0])
        self.assertEqual(executed[0][1], self.payload)
        self.assertFalse(executed[0][0].exists())
        terminal.assert_called_once_with('/dev/tty', 'rb')

    def test_partial_installer_never_executes(self):
        status, _, _, child, _ = self.invoke(self.payload[:-1])
        self.assertEqual(status, 1)
        child.assert_not_called()

    def test_changed_installer_never_executes(self):
        status, _, _, child, _ = self.invoke(self.payload + b'changed')
        self.assertEqual(status, 1)
        child.assert_not_called()

    def test_root_is_rejected_before_downloading(self):
        status, _, download, child, _ = self.invoke(self.payload, root=True)
        self.assertEqual(status, 1)
        download.assert_not_called()
        child.assert_not_called()

    def test_installer_failure_is_returned_and_file_removed(self):
        status, executed, _, _, _ = self.invoke(self.payload, child_status=7)
        self.assertEqual(status, 7)
        self.assertFalse(executed[0][0].exists())

    def test_truncated_launcher_before_entry_point_does_not_download_or_execute(self):
        prefix = CODE.rsplit('\nraise SystemExit(main())', 1)[0]
        with patch('urllib.request.urlopen') as download, patch('subprocess.run') as child:
            exec(prefix, {})
        download.assert_not_called()
        child.assert_not_called()

    def test_network_failure_never_executes(self):
        with patch('os.geteuid', return_value=1000), \
                patch('urllib.request.urlopen', side_effect=urllib.error.URLError('offline')), \
                patch('subprocess.run') as child, \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            self.assertEqual(self.main(), 1)
        child.assert_not_called()

    def test_outer_download_failure_returns_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            curl = Path(folder) / 'curl'
            curl.write_text('#!/bin/sh\nexit 22\n')
            curl.chmod(0o700)
            import os
            env = dict(os.environ, PATH=folder + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', '-c', (ROOT / 'INSTALL-COMMAND.txt').read_text()],
                                    env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 22)


if __name__ == '__main__':
    unittest.main()
