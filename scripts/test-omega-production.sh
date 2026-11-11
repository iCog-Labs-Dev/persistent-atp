#!/usr/bin/env bash
# Disposable PostgreSQL + native MORK integration; never uses an existing DB.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo/.venv/bin/python"
pg_bin="${OMEGACLAW_TEST_POSTGRES_BIN:-/usr/lib/postgresql/18/bin}"
for tool in initdb pg_ctl createdb; do
    if [ ! -x "$pg_bin/$tool" ]; then
        echo "Set OMEGACLAW_TEST_POSTGRES_BIN to a PostgreSQL server bin directory" >&2
        exit 69
    fi
done
state="$(mktemp -d /tmp/omega-production-test.XXXXXX)"
port="$("$python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
cleanup() {
    "$pg_bin/pg_ctl" -D "$state/data" -m immediate -w stop >/dev/null 2>&1 || true
}
trap cleanup EXIT
"$pg_bin/initdb" -D "$state/data" --auth=trust --no-locale -U omega_test >"$state/init.log"
"$pg_bin/pg_ctl" -D "$state/data" -l "$state/postgres.log" \
    -o "-h 127.0.0.1 -p $port -k $state" -w start
"$pg_bin/createdb" -h 127.0.0.1 -p "$port" -U omega_test omega_receiver_test
export OMEGACLAW_PRODUCTION_TEST_DB_URL="postgresql://omega_test@127.0.0.1:$port/omega_receiver_test"
export ARTIFACT_TEST_DB_URL="$OMEGACLAW_PRODUCTION_TEST_DB_URL"
export MORK_PYTHON="$python" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
cd "$repo"
selection=(src/omegaclaw/tests/test_production.py)
if [ "${1:-}" = "--all" ]; then
    shift
    selection=()
fi
scripts/with-mork.sh -- "$python" -m pytest -q "${selection[@]}" "$@"
