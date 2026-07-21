#!/usr/bin/env bash
# bootstrap-deps.sh — fetch the three sibling dependency repos and wire the
# ./engine, ./chain_c, ./bsv_third_entry symlinks that every launcher resolves through.
#
# bonsai-notary is a thin *composition* layer; the heavy lifting lives in three
# independently-versioned repos. This clones the exact immutable revisions in
# dependencies.lock next to this checkout and links them in, so every host gets
# the same tested composition.
#
# Idempotent — safe to re-run. Existing checkouts must already match the lock;
# pass BONSAI_DEPS_UPDATE=1 to fetch/checkout the locked revision after updating
# this notary checkout. Dirty dependency trees fail closed unless the explicit
# development-only BONSAI_DEPS_ALLOW_DIRTY=1 override is set.
#
#   ./scripts/bootstrap-deps.sh
#
# Env overrides:
#   BONSAI_DEPS_ORG     GitHub org/user to clone from          (default: itsmygithubacct)
#   BONSAI_DEPS_BASE    full base URL, overrides ORG           (default: https://github.com/$ORG)
#   BONSAI_DEPS_DIR     directory the siblings are cloned into (default: the parent of this repo)
#   BONSAI_DEPS_LOCK_FILE lock manifest override              (default: ./dependencies.lock)
#   BONSAI_DEPS_UPDATE  1 = move clean clones to locked SHAs (default: 0)
#   BONSAI_DEPS_ALLOW_DIRTY 1 = allow local source edits      (development only)
set -euo pipefail

ORG="${BONSAI_DEPS_ORG:-itsmygithubacct}"
BASE="${BONSAI_DEPS_BASE:-https://github.com/$ORG}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root (bonsai-notary/)
parent="$(cd "$here/.." && pwd)"
DEPS_DIR="${BONSAI_DEPS_DIR:-$parent}"
LOCK_FILE="${BONSAI_DEPS_LOCK_FILE:-$here/dependencies.lock}"
mkdir -p "$DEPS_DIR"
[ -f "$LOCK_FILE" ] || { echo "bootstrap-deps.sh: dependency lock not found: $LOCK_FILE" >&2; exit 2; }

# repo name  ->  symlink name inside this checkout (only the engine differs)
link_name() { case "$1" in integer_inference_engine) echo engine ;; *) echo "$1" ;; esac; }
locked_revision() {
  local name="$1" revision
  revision="$(awk -v name="$name" '$1 == name { print $2 }' "$LOCK_FILE")"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || {
    echo "bootstrap-deps.sh: $LOCK_FILE has no single lowercase 40-hex commit for $name" >&2
    exit 2
  }
  printf '%s\n' "$revision"
}

for name in integer_inference_engine chain_c bsv_third_entry; do
  revision="$(locked_revision "$name")"
  dest="$DEPS_DIR/$name"
  if [ -e "$dest/.git" ]; then
    if [ "${BONSAI_DEPS_ALLOW_DIRTY:-0}" != 1 ] && [ -n "$(git -C "$dest" status --porcelain)" ]; then
      echo "bootstrap-deps.sh: $name has uncommitted changes at $dest; refusing a non-reproducible setup" >&2
      echo "  commit/stash them, or use BONSAI_DEPS_ALLOW_DIRTY=1 for development only" >&2
      exit 2
    fi
    if ! current="$(git -C "$dest" rev-parse HEAD 2>/dev/null)"; then
      # Resume a checkout whose clone/init reached .git but was interrupted
      # before the first locked commit was installed.
      echo "→ completing interrupted $name checkout at locked revision $revision"
      git -C "$dest" fetch origin
      git -C "$dest" checkout --detach "$revision"
      current="$revision"
    fi
    if [ "$current" != "$revision" ]; then
      if [ "${BONSAI_DEPS_UPDATE:-0}" != 1 ]; then
        echo "bootstrap-deps.sh: $name is at $current, expected locked revision $revision" >&2
        echo "  rerun with BONSAI_DEPS_UPDATE=1 to checkout the tested composition" >&2
        exit 2
      fi
      echo "→ syncing $name to locked revision $revision"
      git -C "$dest" fetch origin
      git -C "$dest" checkout --detach "$revision"
    else
      echo "✓ $name at locked revision $revision"
    fi
  elif [ -e "$dest" ]; then
    echo "bootstrap-deps.sh: $dest exists but is not a git checkout; refusing to link it" >&2
    exit 2
  else
    echo "→ cloning $name at locked revision $revision → $dest"
    git clone --no-checkout "$BASE/$name.git" "$dest"
    git -C "$dest" checkout --detach "$revision"
  fi

  # point the symlink at the sibling: relative (../name) when cloned into the default
  # parent dir, else an absolute path to a custom BONSAI_DEPS_DIR.
  link="$here/$(link_name "$name")"
  if [ "$DEPS_DIR" = "$parent" ]; then target="../$name"; else target="$dest"; fi
  if { [ -e "$link" ] || [ -L "$link" ]; } && [ ! -L "$link" ]; then
    echo "bootstrap-deps.sh: $link exists and is not a symlink; refusing to replace it" >&2
    exit 2
  fi
  ln -sfn "$target" "$link"
  printf '  linked %-16s -> %s\n' "$(link_name "$name")" "$(readlink "$link")"
done

echo
echo "Dependencies wired at the immutable revisions in $LOCK_FILE."
echo "Verify:  ls -l engine chain_c bsv_third_entry"
echo "Next (see INSTALL.md): build chain_c, create the engine venv + native kernel, fetch weights."
