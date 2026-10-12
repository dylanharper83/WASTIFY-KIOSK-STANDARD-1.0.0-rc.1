#!/usr/bin/env bash
# Stable Connect launcher. Only the complete verified release can be executed.
python3 -I <<'WASTIFY_BOOTSTRAP'
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

RELEASE = '1.0.0-rc.2'
URL = ('https://raw.githubusercontent.com/dylanharper83/'
       'WASTIFY-KIOSK-STANDARD-1.0.0-rc.1/v' + RELEASE + '/INSTALL-VIA-CONNECT.txt')
SHA256 = '9727cbd8f22d755b0c314f85226c5b9171a64e9cb03f27b2d4f269926372f2bc'


def main():
    if os.geteuid() == 0:
        print('Paste the command as the normal Pi desktop user, without sudo.', file=sys.stderr)
        return 1
    try:
        print('Downloading Wastify Kiosk ' + RELEASE + '...', flush=True)
        with urllib.request.urlopen(URL, timeout=60) as response:
            payload = response.read(1024 * 1024 + 1)
        if hashlib.sha256(payload).hexdigest() != SHA256:
            print('Installer verification failed. Nothing was installed.', file=sys.stderr)
            return 1
        with tempfile.NamedTemporaryFile(prefix='wastify-install-', suffix='.sh') as script:
            script.write(payload)
            script.flush()
            with open('/dev/tty', 'rb') as terminal:
                return subprocess.run(['bash', script.name], stdin=terminal).returncode
    except (OSError, urllib.error.URLError) as error:
        print('Could not run the installer: ' + str(error), file=sys.stderr)
        return 1


raise SystemExit(main())
WASTIFY_BOOTSTRAP
