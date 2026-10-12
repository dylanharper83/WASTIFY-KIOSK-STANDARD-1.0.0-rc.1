# Local validation — 13 October 2026

This is a release candidate for the common Pi 5 / CM4 Trixie Desktop arm64 baseline.
It has not been installed on either physical hardware family during this task.

Validation commands:

    python3 -B tests/test-wastify-kiosk.py
    python3 -B tests/test-standard.py
    python3 -B tests/test-desktop-setup.py
    python3 -B tests/test-connect-installer.py
    python3 -B tests/test-download-command.py
    bash -n install.sh
    bash -n wastify-kiosk/install.sh
    bash -n INSTALL-VIA-CONNECT.txt

Tests exercise temporary files, locks and failure injection with mocked systemd,
Chromium, NetworkManager, device metadata and HTTP responses. See the test run
results for this release; no test changes a real desktop, network or service.
All 119 local tests pass: 48 core, 37 standard, 22 desktop setup,
8 Connect installer and 4 verified-download checks.

Covered: platform rejection before dependency changes, user-owned paths, Connect
desktop selection, service stop/disable behaviour, failed installs, persistent
profile, current URL retention on rollback, invalid backups, first-install rollback
refusal, explicit Wi-Fi checkpoint command, private diagnostic output, and local
release updates. The paste installer payload is decoded and compared byte-for-byte
with the supplied source files. Its embedded hash checks transfer integrity; it is
not an independent release signature.

Additional checks cover URL-free installation, the local waiting screen, setting
the site afterwards, no HTTP requests before a site is configured, and automatic
desktop setup with mocked privileged commands. New units reboot after success;
updates preserve existing desktop settings and do not reboot.

Pi 5 and CM4 field tests remain required: physical display/touch, first-boot/startup,
crash recovery, weighing and photo upload, network-loss behaviour, reboot, rollback
and an overnight observation. The software does not certify board-specific drivers.
