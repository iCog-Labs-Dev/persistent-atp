#!/usr/bin/env bash
# Run a command with MORK loaded.
#
# The MORK library cannot be loaded by ctypes after Python has started: it needs
# thread-local storage that must be reserved at process start, and a plain
# CDLL fails with "cannot allocate memory in static TLS block". LD_PRELOAD is
# read by the loader before the process exists, so it has to be set out here.
#
#   scripts/with-mork.sh python -m commit_gate status p1
#   scripts/with-mork.sh python -m pytest src/commit_gate/tests
#
# MORK_LIBRARY names the library; unset, it is searched for by name -- along
# LD_LIBRARY_PATH, in a Cargo build in this repo or a checkout beside it, then
# in the system library directories. No layout is assumed beyond "somewhere
# near this repo", so set MORK_LIBRARY if yours lives elsewhere.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 64
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name=libmork_ffi.so

if [ -z "${MORK_LIBRARY:-}" ]; then
    candidates=()

    # Wherever the loader is already told to look.
    IFS=: read -r -a ld_dirs <<<"${LD_LIBRARY_PATH:-}"
    for dir in ${ld_dirs[@]+"${ld_dirs[@]}"}; do
        [ -n "$dir" ] && candidates+=("$dir/$name")
    done

    # A Cargo build in this repo, in a crate inside it, or in a sibling
    # checkout. Unmatched globs stay literal and fail the -f test below.
    for profile in release debug; do
        candidates+=(
            "$repo"/target/"$profile"/"$name"
            "$repo"/*/target/"$profile"/"$name"
            "$repo"/../*/target/"$profile"/"$name"
            "$repo"/../*/*/target/"$profile"/"$name"
        )
    done

    candidates+=(/usr/local/lib/"$name" /usr/lib/"$name")

    for candidate in "${candidates[@]}"; do
        if [ -f "$candidate" ]; then
            MORK_LIBRARY="$(cd "$(dirname "$candidate")" && pwd)/$(basename "$candidate")"
            break
        fi
    done
fi

if [ -z "${MORK_LIBRARY:-}" ]; then
    echo "$0: no $name found near $repo; set MORK_LIBRARY to its path" >&2
    echo "  (morklib.so is the Prolog wrapper and exports no rust_mork)" >&2
    exit 69
fi

if [ ! -f "$MORK_LIBRARY" ]; then
    echo "$0: MORK_LIBRARY=$MORK_LIBRARY does not exist" >&2
    exit 69
fi

export MORK_LIBRARY
export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$MORK_LIBRARY"
exec "$@"
