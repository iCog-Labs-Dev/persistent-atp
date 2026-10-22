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
  reference|omega|persistence|lean|real-atp) ;;
  *) echo "usage: bash scripts/omegaclaw-test.sh --tier {reference|omega|persistence|lean|real-atp}" >&2; exit 2 ;;
esac

run_dir="$(mktemp -d "${TMPDIR:-/tmp}/persistent-atp-${tier}-XXXXXX")"
echo "test artifacts: $run_dir"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$run_dir/uv-cache}"
status=0
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
    *)
      echo "required $tier integration tests are not implemented in this checkout" > "$run_dir/blocker.txt"
      status=1
      ;;
  esac
fi
if [[ -f "$run_dir/pytest.log" ]]; then cat "$run_dir/pytest.log"; fi
echo "exit_code=$status" > "$run_dir/result.txt"
exit "$status"
