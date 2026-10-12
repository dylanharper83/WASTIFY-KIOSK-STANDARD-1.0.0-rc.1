#!/usr/bin/env bash
# Backward-compatible entry point for the standard release installer.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$ROOT/install.sh" "$@"
