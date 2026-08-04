#!/usr/bin/env bash
# Installs voice-to-clipboard as a systemd user service tied to your
# graphical session (starts on login, restarts on failure).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/.venv/bin/voice-to-clipboard"

if [[ ! -x "$BIN" ]]; then
    echo "Virtualenv not found — running uv sync first..."
    (cd "$DIR" && uv sync)
fi

exec "$BIN" --install-service
