#!/usr/bin/env bash
set -euo pipefail

tier=""
while (($#)); do
  case "$1" in
    --tier) tier="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$tier" in
  reference|omega|coordinator|container|persistence|formal-contract|lean|real-atp) ;;
  *) echo "usage: bash scripts/omegaclaw-test.sh --tier {reference|omega|coordinator|container|persistence|formal-contract|lean|real-atp}" >&2; exit 2 ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export TMPDIR="${TMPDIR:-$repo_root/tmp/test-runs}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$repo_root/tmp/uv-cache}"
export OMEGA_PINNED_ROOT="${OMEGA_PINNED_ROOT:-$repo_root/tmp/repos/Omega-ba185cd}"
export PETTA_PYTHON="${PETTA_PYTHON:-$repo_root/.venv/bin/python}"
export PETTA_PINNED_ROOT="${PETTA_PINNED_ROOT:-$repo_root/tmp/repos/PeTTa}"
mkdir -p "$TMPDIR" "$UV_CACHE_DIR"
run_dir="$(mktemp -d "$TMPDIR/persistent-atp-${tier}-XXXXXX")"
echo "test artifacts: $run_dir"
status=0
if [[ "$tier" == coordinator ]]; then
  export OMEGA_RUNTIME_REQUIRED=1
  uv run persistent-atp-omega doctor --profile omega --require > "$run_dir/doctor.json" 2>&1 || status=$?
  if ((status == 0)); then
    uv run pytest src/omegaclaw/tests/test_task_workflow.py src/omegaclaw/tests/test_receiver_v2.py \
      tests/omegaclaw_runtime/test_omega_process.py \
      -q > "$run_dir/pytest.log" 2>&1 || status=$?
    cat "$run_dir/pytest.log"
  else
    cat "$run_dir/doctor.json"
  fi
  echo "exit_code=$status" > "$run_dir/result.txt"
  exit "$status"
fi
if [[ "$tier" == formal-contract ]]; then
  # Scripted search against the paired provider contract; replay tests launch
  # an installed Lean process. This does not establish trained-model readiness.
  export OMEGA_FORMAL_CONTRACT_REQUIRED=1
  uv run --extra formal-tests pytest src/omegaclaw/tests/test_formal_v2_receiver.py -q > "$run_dir/pytest.log" 2>&1 || status=$?
  cat "$run_dir/pytest.log"
  echo "exit_code=$status" > "$run_dir/result.txt"
  exit "$status"
fi
if [[ "$tier" == container ]]; then
  export OMEGA_CONTAINER_REQUIRED=1
  uv run pytest tests/omegaclaw_runtime/test_omega_container.py -q > "$run_dir/pytest.log" 2>&1 || status=$?
  cat "$run_dir/pytest.log"
  echo "exit_code=$status" > "$run_dir/result.txt"
  exit "$status"
fi
if ! uv run persistent-atp-omega doctor --profile "$tier" --require > "$run_dir/doctor.json" 2>&1; then
  cat "$run_dir/doctor.json"
  status=1
else
  case "$tier" in
    reference)
      uv run pytest src/omegaclaw/tests -q > "$run_dir/pytest.log" 2>&1 || status=$?
      ;;
    omega)
      uv run pytest tests/omegaclaw_runtime src/omegaclaw/tests -q > "$run_dir/pytest.log" 2>&1 || status=$?
      ;;
    persistence)
      export OMEGA_PERSISTENCE_REQUIRED=1
      uv run pytest src/omegaclaw/tests/test_persistent_receiver.py -q > "$run_dir/pytest.log" 2>&1 || status=$?
      ;;
    *)
      echo "required $tier integration tests are not implemented in this checkout" > "$run_dir/blocker.txt"
      status=1
      ;;
  esac
fi
if [[ -f "$run_dir/pytest.log" ]]; then cat "$run_dir/pytest.log"; fi
echo "exit_code=$status" > "$run_dir/result.txt"
exit "$status"
