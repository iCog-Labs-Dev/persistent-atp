#!/usr/bin/env bash
# Dedicated provider contract test: synthetic search, actual API and Lean replay.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MATHS_AI_BACKEND_ROOT="${MATHS_AI_BACKEND_ROOT:-$repo/tmp/review/maths-ai-atp-main-20261111}"
export MATHS_AI_BACKEND_PYTHON="${MATHS_AI_BACKEND_PYTHON:-$MATHS_AI_BACKEND_ROOT/.venv/bin/python}"
export MATHS_AI_TEST_LEAN_TOOLCHAIN="${MATHS_AI_TEST_LEAN_TOOLCHAIN:-leanprover/lean4:v4.33.0}"
if [ ! -f "$MATHS_AI_BACKEND_ROOT/maths_ai/api/provider_contract.py" ] || [ ! -x "$MATHS_AI_BACKEND_PYTHON" ]; then
    echo "Set MATHS_AI_BACKEND_ROOT/PYTHON to the companion integration branch and its test environment" >&2
    exit 69
fi
export MORK_REQUIRE_NATIVE=1
exec "$repo/scripts/test-omega-production.sh" "$repo/src/omegaclaw/tests/test_maths_ai_production.py" "$@"
