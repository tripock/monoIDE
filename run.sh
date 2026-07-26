#!/usr/bin/env bash
# Launcher. No venv, no pip: standard library only.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${MONOIDE_PORT:-4321}"
WORKSPACE="${1:-$PWD}"

exec python3 main.py "$WORKSPACE" --port "$PORT" "${@:2}"
