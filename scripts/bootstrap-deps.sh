#!/usr/bin/env bash
# bootstrap-deps.sh — fetch the three sibling dependency repos and wire the
# ./engine, ./chain_c, ./bsv_third_entry symlinks that every launcher resolves through.
#
# bonsai-notary is a thin *composition* layer; the heavy lifting lives in three
# independently-versioned repos. This clones them next to this checkout and links
# them in, so `./bonsai-notary`, `./bonsai-agent`, and scripts/bonsai.sh just work.
#
# Idempotent — safe to re-run. Existing checkouts are left alone (relinked only);
# pass BONSAI_DEPS_UPDATE=1 to fast-forward them.
#
#   ./scripts/bootstrap-deps.sh
#
# Env overrides:
#   BONSAI_DEPS_ORG     GitHub org/user to clone from          (default: itsmygithubacct)
#   BONSAI_DEPS_BASE    full base URL, overrides ORG           (default: https://github.com/$ORG)
#   BONSAI_DEPS_DIR     directory the siblings are cloned into (default: the parent of this repo)
#   BONSAI_DEPS_UPDATE  1 = git pull --ff-only existing clones (default: 0)
set -euo pipefail

ORG="${BONSAI_DEPS_ORG:-itsmygithubacct}"
BASE="${BONSAI_DEPS_BASE:-https://github.com/$ORG}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root (bonsai-notary/)
parent="$(cd "$here/.." && pwd)"
DEPS_DIR="${BONSAI_DEPS_DIR:-$parent}"
mkdir -p "$DEPS_DIR"

# repo name  ->  symlink name inside this checkout (only the engine differs)
link_name() { case "$1" in integer_inference_engine) echo engine ;; *) echo "$1" ;; esac; }

for name in integer_inference_engine chain_c bsv_third_entry; do
  dest="$DEPS_DIR/$name"
  if [ -e "$dest/.git" ]; then
    echo "✓ $name present at $dest"
    if [ "${BONSAI_DEPS_UPDATE:-0}" = 1 ]; then
      git -C "$dest" pull --ff-only || echo "  (skipped update for $name — not fast-forwardable)"
    fi
  elif [ -e "$dest" ]; then
    echo "!! $dest exists but is not a git checkout — leaving it untouched; linking anyway" >&2
  else
    echo "→ cloning $name → $dest"
    git clone "$BASE/$name.git" "$dest"
  fi

  # point the symlink at the sibling: relative (../name) when cloned into the default
  # parent dir, else an absolute path to a custom BONSAI_DEPS_DIR.
  link="$here/$(link_name "$name")"
  if [ "$DEPS_DIR" = "$parent" ]; then target="../$name"; else target="$dest"; fi
  ln -sfn "$target" "$link"
  printf '  linked %-16s -> %s\n' "$(link_name "$name")" "$(readlink "$link")"
done

echo
echo "Dependencies wired. Verify:  ls -l engine chain_c bsv_third_entry"
echo "Next (see INSTALL.md): build chain_c, create the engine venv + native kernel, fetch weights."
