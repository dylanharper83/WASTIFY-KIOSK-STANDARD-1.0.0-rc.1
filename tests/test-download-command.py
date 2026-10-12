"""Verify the one-command downloader never executes unverified bytes."""
import io
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class DownloadCommandTests(unittest.TestCase):
    def setUp(self):
        command = shlex.split((ROOT / 'ONE-COMMAND.txt').read_text())
        self.code = command[command.index('-c') + 1]
        self.payload = (ROOT / 'INSTALL-VIA-CONNECT.txt').read_bytes()

    def run_command(self, content, child_status=0):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(content)) as download, \
                patch('subprocess.run', return_value=subprocess.CompletedProcess(['bash'], child_status)) as child:
            with self.assertRaises(SystemExit) as result:
                exec(self.code, {})
        return result.exception.code, download, child

    def test_complete_verified_file_executes_and_uses_pinned_release(self):
        code, download, child = self.run_command(self.payload)
        self.assertEqual(code, 0)
        self.assertIn('/v1.0.0-rc.2/INSTALL-VIA-CONNECT.txt', download.call_args.args[0])
        self.assertEqual(child.call_args.args[0], ['bash'])
        self.assertEqual(child.call_args.kwargs['input'], self.payload)

    def test_partial_download_never_executes(self):
        code, _, child = self.run_command(self.payload[:-20])
        self.assertEqual(code, 'Installer verification failed')
        child.assert_not_called()

    def test_changed_outer_script_never_executes(self):
        code, _, child = self.run_command(self.payload + b'\necho altered\n')
        self.assertEqual(code, 'Installer verification failed')
        child.assert_not_called()

    def test_child_failure_is_returned(self):
        code, _, _ = self.run_command(self.payload, child_status=7)
        self.assertEqual(code, 7)


if __name__ == '__main__':
    unittest.main()
