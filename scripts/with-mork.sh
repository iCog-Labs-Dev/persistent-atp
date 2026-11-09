#!/usr/bin/env bash
# Run a command with the MORK FFI preloaded.
#
#   scripts/with-mork.sh .venv/bin/python scripts/alignment-mork-exp.py
#   scripts/with-mork.sh --install .venv/bin/python scripts/alignment-mork-exp.py
#   scripts/with-mork.sh --install -- .venv/bin/python -c "..."
#
# The library is the ffi/ crate of the Pequanta/MORK fork — no external
# mork_ffi clone. Resolution order:
#   1. MORK_LIBRARY (exact path)
#   2. $MORK_FORK_ROOT/target/{release,debug}/libmork_ffi.so (local checkout)
#   3. .mork-fork-src/MORK/target/... (a previous --install clone/build)
#   4. with --install: clone https://github.com/Pequanta/MORK at the recorded
#      commit and build ffi/ (set MORK_FORK_ROOT to build a local checkout
#      instead of cloning)
#
# LD_PRELOAD is required because loading MORK later can exhaust static TLS.
#
# Every run verifies the committed source/build identity and emits the loaded
# path + SHA256. Only --record-provenance changes the committed record.
set -euo pipefail

usage() {
    echo "usage: $0 [--install] [--record-provenance] [--] <command> [args...]" >&2
}

install=false
record=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install) install=true; shift ;;
        --record-provenance) record=true; shift ;;
        --) shift; break ;;
        -h|--help) usage; exit 0 ;;
        *) break ;;
    esac
done

if [ "$#" -eq 0 ]; then
    usage
    exit 64
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name=libmork_ffi.so
install_root="${MORK_INSTALL_ROOT:-$repo/.mork-fork-src}"
provenance_file="$repo/mork/FFI_PROVENANCE.json"

fork_url=https://github.com/Pequanta/MORK.git
fork_branch=persistent-atp-ffi
# Recorded build-target tuple: the fork commit the ffi/ crate builds from,
# and the PathMap commit vendored inside the fork at vendor/PathMap.
mork_commit=c065fc42cf308465a413feee3fa3c3c9b2f5804f
pathmap_commit=4c84a8b40c7b6a7ecb54e009a70f0c5abbc1b60f
ffi_source=in-tree
toolchain=nightly-2026-07-28

probe_python="${MORK_PYTHON:-}"
if [ -z "$probe_python" ]; then
    if [ -x "$repo/.venv/bin/python" ]; then probe_python="$repo/.venv/bin/python";
    else probe_python=python3; fi
fi
command -v "$probe_python" >/dev/null || {
    echo "$0: Python probe executable not found: $probe_python" >&2
    exit 69
}

install_mork() {
    command -v git >/dev/null || {
        echo "$0: git is required for --install" >&2
        exit 69
    }
    command -v cargo >/dev/null || {
        echo "$0: cargo is required for --install" >&2
        exit 69
    }

    # D5/D6: fixed PoC build inputs; no optional wasm or ambient flag override.
    if [ -n "${CARGO_ENCODED_RUSTFLAGS:-}" ]; then
        echo "$0: configured PoC build does not accept CARGO_ENCODED_RUSTFLAGS" >&2
        exit 64
    fi
    if [ -n "${RUSTFLAGS:-}" ] && [ "$RUSTFLAGS" != "-C target-cpu=native" ]; then
        echo "$0: configured PoC build requires RUSTFLAGS='-C target-cpu=native'" >&2
        exit 64
    fi
    local src
    if [ -n "${MORK_FORK_ROOT:-}" ] && [ -d "$MORK_FORK_ROOT/.git" ]; then
        # Build only a clean local checkout at the recorded commit.
        src="$MORK_FORK_ROOT"
    else
        # Clone the fork at the recorded commit (requires the branch pushed).
        src="$install_root/MORK"
        if [ -e "$src" ] && [ ! -d "$src/.git" ]; then
            echo "$0: $src exists but is not a Git checkout" >&2
            exit 73
        fi
        if [ ! -d "$src/.git" ]; then
            mkdir -p "$(dirname "$src")"
            git clone --quiet --branch "$fork_branch" "$fork_url" "$src"
        elif [ -n "$(git -C "$src" status --porcelain)" ]; then
            echo "$0: refusing to touch modified fork checkout at $src" >&2
            exit 73
        fi
        if [ "$(git -C "$src" rev-parse HEAD 2>/dev/null || true)" != "$mork_commit" ]; then
            echo "$0: checking out recorded fork commit $mork_commit"
            git -C "$src" fetch --quiet origin "$fork_branch"
            git -C "$src" checkout --quiet --detach "$mork_commit"
        fi
    fi

    local build_toolchain="$toolchain"
    if ! rustup run "$build_toolchain" rustc --version >/dev/null 2>&1; then
        # An already-installed nightly is usable only if it is the exact
        # compiler in the committed provenance record.
        expected_rustc="$("$probe_python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["toolchain"])' "$provenance_file")"
        if [ "$(rustc +nightly --version)" != "$expected_rustc" ]; then
            echo "$0: install the pinned $toolchain toolchain" >&2
            exit 69
        fi
        build_toolchain=nightly
    fi
    local head
    head="$(git -C "$src" rev-parse HEAD)"
    if [ "$head" != "$mork_commit" ] || [ -n "$(git -C "$src" status --porcelain)" ]; then
        echo "$0: fork must be clean at recorded commit $mork_commit" >&2
        exit 73
    fi
    echo "$0: building ffi/ from $src @ $head"
    (
        cd "$src"
        export RUSTFLAGS="-C target-cpu=native"
        patch_file="$repo/mork/fork-patches/ffi-query-lifetime.patch"
        git apply --check "$patch_file"
        git apply "$patch_file"
        trap 'git apply --reverse "$patch_file"' EXIT
        export MORK_FFI_PATCH_SHA256
        MORK_FFI_PATCH_SHA256="$(sha256sum "$patch_file" | awk '{print $1}')"
        cargo +"$build_toolchain" build --locked --release -p mork-ffi
        if [ "${MORK_TEST_FORK:-0}" = 1 ]; then
            cargo +"$build_toolchain" test --locked -p mork -p mork-ffi
        fi
    )
    MORK_LIBRARY="$src/target/release/$name"
}

if [ -z "${MORK_LIBRARY:-}" ]; then
    candidates=()

    if [ -n "${MORK_FORK_ROOT:-}" ]; then
        for profile in release debug; do
            candidates+=("$MORK_FORK_ROOT"/target/"$profile"/"$name")
        done
    fi

    for profile in release debug; do
        candidates+=(
            "$install_root"/MORK/target/"$profile"/"$name"
            "$repo"/target/"$profile"/"$name"
            "$repo"/*/target/"$profile"/"$name"
            "$repo"/../*/target/"$profile"/"$name"
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

if [ "$install" = true ]; then
    install_mork
fi

if [ -z "${MORK_LIBRARY:-}" ]; then
    echo "$0: no $name found; set MORK_LIBRARY, MORK_FORK_ROOT, or pass --install" >&2
    exit 69
fi

if [ ! -f "$MORK_LIBRARY" ]; then
    echo "$0: MORK_LIBRARY=$MORK_LIBRARY does not exist" >&2
    exit 69
fi

lib_sha256="$(sha256sum "$MORK_LIBRARY" | awk '{print $1}')"

echo "$0: library: $MORK_LIBRARY" >&2
echo "$0: sha256:  $lib_sha256" >&2
export MORK_LIBRARY
export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$MORK_LIBRARY"
# Run the probe in its own preloaded process; verification never rewrites the
# committed record. PYTHONPATH supplies the package without requiring install.
probe_args=()
if [ "$record" = true ]; then probe_args+=(--record); fi
PYTHONPATH="$repo:$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$probe_python" -m mork.provenance "${probe_args[@]}"
exec "$@"
