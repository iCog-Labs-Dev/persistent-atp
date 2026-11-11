#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
if [ "${1:-}" = "replay" ]; then
    shift
    exec "$repo/scripts/with-mork.sh" -- "$repo/.venv/bin/python" -m omegaclaw.verify "$@"
fi
exec "$repo/.venv/bin/python" -m omegaclaw.playground "$@"
