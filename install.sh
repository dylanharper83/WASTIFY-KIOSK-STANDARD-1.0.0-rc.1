#!/usr/bin/env bash
# Wastify Kiosk 1.0.0-rc.1
# Usage: bash install.sh [--deps] [--no-start] [HTTPS_URL]
# Run as the desktop user from Raspberry Pi Connect, without sudo.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="$ROOT/wastify-kiosk/wastify_kiosk.py"
KIOSK_COMMAND="$HOME/.local/bin/kiosk"
INSTALL_DEPS=0
ACTIVATE=1
URL_ARGS=()

usage() {
  cat <<'HELP'
Usage: bash install.sh [--deps] [--no-start] [HTTPS_URL]

  --deps      Install missing Chromium and curl packages only.
  --no-start  Stage a new installation without enabling it. Updates retain
              the existing running and desktop-autostart state.
  HTTPS_URL   Required on first install; omitted on updates to retain the URL.

Run as the same normal user who logs into the Pi desktop, without sudo.
First installs start automatically unless --no-start is supplied.
Updates always retain the existing running and desktop-autostart state.
HELP
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deps) INSTALL_DEPS=1 ;;
    --no-start) ACTIVATE=0 ;;
    -h|--help) usage; exit 0 ;;
    --*) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ ${#URL_ARGS[@]} -ne 0 ]]; then
        printf 'Supply at most one HTTPS URL.\n' >&2
        exit 2
      fi
      URL_ARGS=("$1")
      ;;
  esac
  shift
done

if [[ "$EUID" -eq 0 ]]; then
  printf 'Run this installer as the Pi desktop user, without sudo.\n' >&2
  exit 1
fi
command -v python3 >/dev/null || {
  printf 'Python 3 is required; use Raspberry Pi OS Trixie Desktop 64-bit.\n' >&2
  exit 1
}
[[ -f "$CORE" ]] || {
  printf 'Incomplete release: missing %s\n' "$CORE" >&2
  exit 1
}

# Check the supported Pi/OS, user bus, URL, legacy kiosks and managed-file
# ownership before sudo, apt or any installation changes.
invoke_core() {
  if [[ ${#URL_ARGS[@]} -eq 1 ]]; then
    python3 "$CORE" "$1" "${URL_ARGS[0]}"
  else
    python3 "$CORE" "$1"
  fi
}
invoke_core preflight

# Reinstalling must never re-enable a deliberately stopped/disabled unit.
# The core installer preserves an existing installation's state.
if [[ -e "$HOME/.local/share/wastify-kiosk/wastify_kiosk.py" ]]; then
  ACTIVATE=0
fi

MISSING_PACKAGES=()
command -v chromium >/dev/null || MISSING_PACKAGES+=(chromium)
command -v curl >/dev/null || MISSING_PACKAGES+=(curl)
if [[ ${#MISSING_PACKAGES[@]} -gt 0 ]]; then
  if [[ "$INSTALL_DEPS" -ne 1 ]]; then
    printf 'Missing packages: %s\n' "${MISSING_PACKAGES[*]}" >&2
    printf 'Re-run this command with --deps to install them.\n' >&2
    exit 1
  fi
  command -v sudo >/dev/null || { printf 'sudo is required for --deps.\n' >&2; exit 1; }
  command -v apt-get >/dev/null || { printf 'apt-get is required for --deps.\n' >&2; exit 1; }
  printf 'Installing missing packages: %s\n' "${MISSING_PACKAGES[*]}"
  sudo apt-get -o DPkg::Lock::Timeout=300 update
  sudo apt-get -o DPkg::Lock::Timeout=300 --no-remove install -y "${MISSING_PACKAGES[@]}"
fi

invoke_core install
if [[ "$ACTIVATE" -eq 1 ]]; then
  if ! "$KIOSK_COMMAND" enable; then
    printf 'Files installed, but activation failed. Inspect kiosk status and logs.\n' >&2
    printf 'After the desktop is logged in, retry: %s enable\n' "$KIOSK_COMMAND" >&2
    exit 1
  fi
else
  printf 'Installation complete; existing running and desktop-autostart state retained.\n'
fi

cat <<'NEXT'

For commands in this Connect shell, run:
  export PATH="$HOME/.local/bin:$PATH"

The next desktop login or reboot normally includes ~/.local/bin automatically.
Until then, new terminals may also need that export or ~/.local/bin/kiosk.
Check the unit with: kiosk status
Commission the physical screen, touch, camera and scale before site use.
NEXT
