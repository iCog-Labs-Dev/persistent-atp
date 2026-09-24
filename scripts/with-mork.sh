#!/usr/bin/env bash
# Run a command with the real MORK FFI preloaded.
#
#   scripts/with-mork.sh .venv/bin/python scripts/alignment-mork-exp.py
#   scripts/with-mork.sh --install .venv/bin/python scripts/alignment-mork-exp.py
#
# --install clones pinned MORK sources into .mork-ffi-src and builds them only
# when no existing library can be found. MORK_LIBRARY overrides discovery.
# LD_PRELOAD is required because loading MORK later can exhaust static TLS.
set -euo pipefail

usage() {
    echo "usage: $0 [--install] <command> [args...]" >&2
}

install=false
case "${1:-}" in
    --install)
        install=true
        shift
        ;;
    -h|--help)
        usage
        exit 0
        ;;
esac

if [ "$#" -eq 0 ]; then
    usage
    exit 64
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name=libmork_ffi.so
install_root="${MORK_INSTALL_ROOT:-$repo/.mork-ffi-src}"

ffi_url=https://github.com/patham9/mork_ffi.git
ffi_commit=f6553aa4559142895c5547f56e490d29e29469f1
mork_url=https://github.com/trueagi-io/MORK.git
mork_commit=45bdb9ad94e4a99876d64058d2922be1566bc372
pathmap_url=https://github.com/Adam-Vandervorst/PathMap.git
pathmap_commit=4c84a8b40c7b6a7ecb54e009a70f0c5abbc1b60f

clone_at() {
    local label=$1 url=$2 commit=$3 destination=$4

    if [ -e "$destination" ] && [ ! -d "$destination/.git" ]; then
        echo "$0: $destination exists but is not a Git checkout" >&2
        exit 73
    fi

    if [ ! -d "$destination/.git" ]; then
        mkdir -p "$(dirname "$destination")"
        git init --quiet "$destination"
        git -C "$destination" remote add origin "$url"
    elif [ -n "$(git -C "$destination" status --porcelain)" ]; then
        echo "$0: refusing to replace modified $label checkout at $destination" >&2
        exit 73
    fi

    if [ "$(git -C "$destination" rev-parse HEAD 2>/dev/null || true)" != "$commit" ]; then
        echo "$0: fetching pinned $label source"
        git -C "$destination" fetch --quiet --depth 1 origin "$commit"
        git -C "$destination" checkout --quiet --detach FETCH_HEAD
    fi
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

    local ffi_dir="$install_root/PeTTa/mork_ffi"
    clone_at MORK "$mork_url" "$mork_commit" "$install_root/MORK"
    clone_at PathMap "$pathmap_url" "$pathmap_commit" "$install_root/PathMap"
    clone_at mork_ffi "$ffi_url" "$ffi_commit" "$ffi_dir"

    local -a cargo_command
    if command -v rustup >/dev/null && rustup run nightly rustc --version >/dev/null 2>&1; then
        cargo_command=(cargo +nightly)
    elif rustc --version 2>/dev/null | grep -q nightly; then
        cargo_command=(cargo)
    else
        echo "$0: a nightly Rust toolchain is required; run 'rustup toolchain install nightly'" >&2
        exit 69
    fi

    echo "$0: building pinned mork_ffi source"
    (
        cd "$ffi_dir"
        RUSTFLAGS="-C target-cpu=native -Awarnings" "${cargo_command[@]}" build --release
    )
    MORK_LIBRARY="$ffi_dir/target/release/$name"
}

if [ -z "${MORK_LIBRARY:-}" ]; then
    candidates=()

    IFS=: read -r -a ld_dirs <<<"${LD_LIBRARY_PATH:-}"
    for dir in ${ld_dirs[@]+"${ld_dirs[@]}"}; do
        [ -n "$dir" ] && candidates+=("$dir/$name")
    done

    for profile in release debug; do
        candidates+=(
            "$install_root"/PeTTa/mork_ffi/target/"$profile"/"$name"
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

if [ -z "${MORK_LIBRARY:-}" ] && [ "$install" = true ]; then
    install_mork
fi

if [ -z "${MORK_LIBRARY:-}" ]; then
    echo "$0: no $name found near $repo; set MORK_LIBRARY or pass --install" >&2
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
