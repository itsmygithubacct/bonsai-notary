#!/usr/bin/env bash
# Exercise the real dependency/bootstrap portion of setup on an Ubuntu 22.04
# class host. No model is downloaded, no signing key is provisioned, and no
# blockchain/provider operation is reachable from --environment-only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
host_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 - "$host_version" <<'PY'
import sys
major, minor = map(int, sys.argv[1].split("."))
if (major, minor) > (3, 10):
    raise SystemExit(f"container contract requires host Python <=3.10, found {major}.{minor}")
PY

acceptance_tmp="$(mktemp -d)"
cleanup() { rm -rf -- "$acceptance_tmp"; }
trap cleanup EXIT INT TERM

git clone --quiet --no-hardlinks "$ROOT" "$acceptance_tmp/notary"
acceptance_root="$acceptance_tmp/notary"
export BONSAI_NOTARY_HOME="$acceptance_tmp/state"
export BONSAI_DEPS_DIR="$acceptance_tmp/deps"
"$acceptance_root/scripts/bootstrap-deps.sh"

engine_venv="$BONSAI_DEPS_DIR/integer_inference_engine/bonsai/.venv"
mkdir -p "$engine_venv/bin"
printf 'preserve me\n' > "$engine_venv/partial-marker"

setup_args=(--yes --local-only --skip-system-packages --environment-only \
            --notary-home "$BONSAI_NOTARY_HOME" --python 3.12)
"$acceptance_root/scripts/setup-bonsai-27b.sh" "${setup_args[@]}"

resolved="$("$engine_venv/bin/python" -c 'import platform; print(platform.python_version())')"
case "$resolved" in 3.12.*) ;; *) echo "expected Python 3.12, found $resolved" >&2; exit 1 ;; esac

backup_dir="$BONSAI_NOTARY_HOME/setup/venv-backups"
preserved="$(find "$backup_dir" -maxdepth 1 -type d \
  -name 'engine.incomplete-python-*' -print -quit)"
if [ -z "$preserved" ] || [ ! -f "$preserved/partial-marker" ]; then
  echo "incompatible partial venv was not preserved" >&2
  exit 1
fi

# A second pass must reuse the supported environment and preserve the same
# interpreter selection instead of creating another backup.
before_count="$(find "$backup_dir" -maxdepth 1 -type d \
  -name 'engine.incomplete-python-*' | wc -l)"
"$acceptance_root/scripts/setup-bonsai-27b.sh" "${setup_args[@]}"
after_count="$(find "$backup_dir" -maxdepth 1 -type d \
  -name 'engine.incomplete-python-*' | wc -l)"
[ "$before_count" = "$after_count" ] || { echo "idempotent rerun created a new backup" >&2; exit 1; }
"$engine_venv/bin/python" -m pytest --version >/dev/null
printf 'container install acceptance passed (host Python %s, managed Python %s)\n' \
  "$host_version" "$resolved"
