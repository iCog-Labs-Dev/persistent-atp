#!/usr/bin/env bash
# Run inside the digest-pinned official image, with this repository mounted.
# Installs test dependencies only; no model keys or inference calls are used.
set -euo pipefail
export OMEGA_RUNTIME_REQUIRED=1
export OMEGA_PINNED_ROOT=/tmp/coordinator-omega
export PETTA_PINNED_ROOT=/PeTTa
export PETTA_PYTHON=python3
test "$(git -C /PeTTa rev-parse HEAD)" = 7037f4c2ad378c52fc328004fe216d5118b674f0
if ! python3 -m pip --version >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends python3-pip python3-dev build-essential
fi
python3 -m pip install --break-system-packages --disable-pip-version-check \
  -e packages/persistent-atp-client -e '.[dev,omega-playground]' janus-swi
git init "$OMEGA_PINNED_ROOT"
git -C "$OMEGA_PINNED_ROOT" fetch --depth=1 https://github.com/singnet/Omega.git \
  ba185cd359d9fe231b904110b988af39fafa5014
git -C "$OMEGA_PINNED_ROOT" checkout --detach FETCH_HEAD
python3 -m pytest -q src/omegaclaw/tests/test_task_workflow.py \
  src/omegaclaw/tests/test_bounded_tasks.py \
  src/omegaclaw/tests/test_bounded_ledger.py \
  src/omegaclaw/tests/test_runtime_overlay.py \
  src/omegaclaw/tests/test_intake_metadata.py \
  src/omegaclaw/tests/test_receiver_v2.py \
  tests/omegaclaw_runtime/test_omega_process.py -k 'not sdk-provider'
