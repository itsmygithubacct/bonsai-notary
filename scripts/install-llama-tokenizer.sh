#!/usr/bin/env bash
# install-llama-tokenizer.sh — build the pinned CPU-only llama-tokenize required by deterministic inference.
#
# The receipt-capable integer engine does not use llama.cpp for generation, but it deliberately delegates
# tokenization to the exact pinned implementation instead of reimplementing Qwen's BPE. This small CPU-only
# build works without NVIDIA/CUDA and lands at the engine's default $BONSAI_LLAMA_DIR/build/bin path.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/install-llama-tokenizer.sh [--force] [--dry-run]

Environment:
  BONSAI_NOTARY_HOME       state/build home (default ~/.local/trinote)
  BONSAI_LLAMA_DIR         tokenizer source/build root (default $HOME/.local/trinote/vendor/llama.cpp)
  BONSAI_TOKENIZER_JOBS    parallel jobs (default: JOBS or 4)

Builds PrismML-Eng/llama.cpp commit 62061f9 as CPU-only/static and installs:
  $BONSAI_LLAMA_DIR/build/bin/llama-tokenize
EOF
}

REPO="https://github.com/PrismML-Eng/llama.cpp.git"
COMMIT="62061f91088281e65071cc38c5f69ee95c39f14e"
NOTARY_HOME="${BONSAI_NOTARY_HOME:-$HOME/.local/trinote}"
LLAMA_DIR="${BONSAI_LLAMA_DIR:-$NOTARY_HOME/vendor/llama.cpp}"
SOURCE_DIR="$LLAMA_DIR/source"
BUILD_DIR="$LLAMA_DIR/build"
BIN="$BUILD_DIR/bin/llama-tokenize"
MARKER="$BUILD_DIR/.tokenizer-commit"
JOBS_COUNT="${BONSAI_TOKENIZER_JOBS:-${JOBS:-4}}"
force=0
dry_run=0

while (($#)); do
  case "$1" in
    --force) force=1 ;;
    --dry-run) dry_run=1 ;;
    -h|--help|help) usage; exit 0 ;;
    *) printf 'install-llama-tokenizer.sh: unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$dry_run" = 1 ]; then
  printf '[dry-run] source: %s @ %s\n' "$REPO" "$COMMIT"
  printf '[dry-run] build: %s (CPU-only, static)\n' "$BUILD_DIR"
  printf '[dry-run] binary: %s\n' "$BIN"
  exit 0
fi

for tool in git cmake c++; do
  command -v "$tool" >/dev/null 2>&1 || {
    printf 'install-llama-tokenizer.sh: required tool not found: %s\n' "$tool" >&2
    exit 2
  }
done
[[ "$JOBS_COUNT" =~ ^[1-9][0-9]*$ ]] || {
  printf 'install-llama-tokenizer.sh: BONSAI_TOKENIZER_JOBS/JOBS must be a positive integer\n' >&2
  exit 2
}

if [ "$force" = 0 ] && [ -x "$BIN" ] && [ -f "$MARKER" ] &&
   [ "$(sed -n '1p' "$MARKER")" = "$COMMIT" ]; then
  "$BIN" --help >/dev/null
  printf '[tokenizer] pinned CPU tokenizer already installed: %s\n' "$BIN"
  exit 0
fi

mkdir -p "$LLAMA_DIR"
if [ -e "$SOURCE_DIR" ] && [ ! -d "$SOURCE_DIR/.git" ]; then
  printf 'install-llama-tokenizer.sh: refusing non-Git source path: %s\n' "$SOURCE_DIR" >&2
  exit 1
fi
if [ ! -d "$SOURCE_DIR/.git" ]; then
  git init "$SOURCE_DIR"
  git -C "$SOURCE_DIR" remote add origin "$REPO"
fi
if [ -n "$(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null)" ]; then
  printf 'install-llama-tokenizer.sh: source checkout has local changes; refusing to overwrite: %s\n' "$SOURCE_DIR" >&2
  exit 1
fi
git -C "$SOURCE_DIR" -c fetch.fsck.badTimezone=ignore fetch --depth=1 origin "$COMMIT"
git -C "$SOURCE_DIR" checkout --detach --force FETCH_HEAD

cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DGGML_CUDA=OFF \
  -DGGML_NATIVE=OFF \
  -DGGML_OPENMP=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_SERVER=OFF \
  -DLLAMA_CURL=OFF
cmake --build "$BUILD_DIR" --target llama-tokenize -j"$JOBS_COUNT"
[ -x "$BIN" ] || {
  printf 'install-llama-tokenizer.sh: build completed without %s\n' "$BIN" >&2
  exit 1
}
"$BIN" --help >/dev/null
printf '%s\n%s\n' "$COMMIT" "$REPO" > "$MARKER"
printf '[tokenizer] installed pinned CPU tokenizer: %s\n' "$BIN"
