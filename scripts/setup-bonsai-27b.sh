#!/usr/bin/env bash
# setup-bonsai-27b.sh — complete, idempotent setup for a receipt-capable Bonsai-27B notary.
#
# Run this after cloning bonsai-notary. It clones/wires the three sibling repositories, installs
# Linux build dependencies, creates the uv environment, builds chain_c + the deterministic CPU
# kernel, downloads/imports the pinned 27B artifact, and provisions identity-bound signing keys.
# Public BSV Third Entry support is opt-in. When enabled, setup performs a read-only funding check
# and exits 3 with a funding address if the wallet cannot cover the minimum. No transaction is
# broadcast unless BOTH --deploy-agent and --confirm-mainnet are explicit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_NAME="$(basename "$0")"

usage() {
  cat <<'EOF'
Usage: ./scripts/setup-bonsai-27b.sh [OPTIONS]

Fresh-host defaults are interactive. For unattended local-receipt setup:

  ./scripts/setup-bonsai-27b.sh --yes --key-mode generate --local-only

Identity / wallet:
  --key-mode MODE             generate | import-mnemonic | keyfiles | existing
  --mnemonic-file PATH        import BIP39 words from a protected file (never argv/stdout)
  --elder-key-file PATH       existing {wif,address} JSON (keyfiles mode)
  --agent-key-file PATH       existing model/Agent {wif,address} JSON (keyfiles mode)
  --counterparty-key-file P   existing counterparty {wif,address} JSON (keyfiles mode)
  --notary-home PATH          state, models, builds, wallet, and keys (default ~/.local/trinote)

Third Entry:
  --public-third-entry        configure public BSV Third Entries and require wallet funding
  --local-only                local verified receipts only (no funding required)
  --minimum-satoshis N        funding preflight threshold (default 12000)
  --deploy-agent              perform the one-time AgentTea deployment
  --confirm-mainnet           second explicit consent required with --deploy-agent (spends BSV)
  --funding-check-only        recheck an existing public setup; do not build/download anything

Installation controls:
  --python VERSION            uv-managed Python for a new venv (default: 3.12; minimum: 3.11)
  --yes                       accept safe defaults; never implies a blockchain broadcast
  --skip-system-packages      require prerequisites to be installed already
  --skip-model-download       do not download the 3.80 GB pinned GGUF
  --skip-model-import         reuse an existing validated artifact; fail if absent
  --skip-tests                skip offline C/Python tests (the dry-run wiring check still runs)
  --jobs N                    parallel build/test jobs (default: min(CPU threads, 8))
  --dry-run                   print the resolved plan without changing the machine
  -h, --help                  show this help

Public Third Entry setup is intentionally resumable: if funding is missing, fund the displayed
address and rerun the same command. Setup never prints a mnemonic or WIF.
EOF
}

say()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf '%s: %s\n' "$SCRIPT_NAME" "$2" >&2; exit "$1"; }

yes_mode=0
key_mode=""
public_mode="ask"
notary_home="${BONSAI_NOTARY_HOME:-$HOME/.local/trinote}"
mnemonic_file=""
elder_source=""
agent_source=""
counterparty_source=""
minimum_sats=12000
deploy_agent=0
confirm_mainnet=0
funding_check_only=0
skip_system_packages=0
skip_model_download=0
skip_model_import=0
skip_tests=0
dry_run=0
jobs="${JOBS:-}"
python_spec="${BONSAI_PYTHON_VERSION:-3.12}"

while (($#)); do
  case "$1" in
    --yes) yes_mode=1 ;;
    --key-mode) (($# >= 2)) || die 2 "--key-mode needs a value"; key_mode="$2"; shift ;;
    --key-mode=*) key_mode="${1#*=}" ;;
    --mnemonic-file) (($# >= 2)) || die 2 "--mnemonic-file needs a path"; mnemonic_file="$2"; shift ;;
    --elder-key-file) (($# >= 2)) || die 2 "--elder-key-file needs a path"; elder_source="$2"; shift ;;
    --agent-key-file) (($# >= 2)) || die 2 "--agent-key-file needs a path"; agent_source="$2"; shift ;;
    --counterparty-key-file) (($# >= 2)) || die 2 "--counterparty-key-file needs a path"; counterparty_source="$2"; shift ;;
    --notary-home) (($# >= 2)) || die 2 "--notary-home needs a path"; notary_home="$2"; shift ;;
    --public-third-entry)
      [ "$public_mode" != "local" ] || die 2 "--public-third-entry conflicts with --local-only"
      public_mode="public" ;;
    --local-only|--no-public-third-entry)
      [ "$public_mode" != "public" ] || die 2 "--local-only conflicts with --public-third-entry"
      public_mode="local" ;;
    --minimum-satoshis) (($# >= 2)) || die 2 "--minimum-satoshis needs an integer"; minimum_sats="$2"; shift ;;
    --deploy-agent) deploy_agent=1 ;;
    --confirm-mainnet) confirm_mainnet=1 ;;
    --funding-check-only) funding_check_only=1 ;;
    --python) (($# >= 2)) || die 2 "--python needs a version"; python_spec="$2"; shift ;;
    --python=*) python_spec="${1#*=}" ;;
    --skip-system-packages) skip_system_packages=1 ;;
    --skip-model-download) skip_model_download=1 ;;
    --skip-model-import) skip_model_import=1 ;;
    --skip-tests) skip_tests=1 ;;
    --jobs) (($# >= 2)) || die 2 "--jobs needs an integer"; jobs="$2"; shift ;;
    --dry-run) dry_run=1 ;;
    -h|--help|help) usage; exit 0 ;;
    *) die 2 "unknown option: $1" ;;
  esac
  shift
done

[[ "$minimum_sats" =~ ^[1-9][0-9]*$ ]] || die 2 "--minimum-satoshis must be a positive integer"
[ -n "$python_spec" ] || die 2 "--python must not be empty"
if [ -n "$jobs" ]; then
  [[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die 2 "--jobs must be a positive integer"
else
  jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf 4)"
  ((jobs > 8)) && jobs=8
fi
case "$key_mode" in ""|generate|import-mnemonic|keyfiles|existing) ;; *)
  die 2 "--key-mode must be generate, import-mnemonic, keyfiles, or existing" ;;
esac
[ "$deploy_agent" = 0 ] || [ "$public_mode" != "local" ] || die 2 "--deploy-agent requires --public-third-entry"
[ "$confirm_mainnet" = 0 ] || [ "$deploy_agent" = 1 ] || die 2 "--confirm-mainnet requires --deploy-agent"
[ "$(uname -s)" = Linux ] || die 2 "Bonsai-27B setup currently supports Linux only"

ask_yes_no() {
  local prompt="$1" default="${2:-no}" answer
  if [ "$yes_mode" = 1 ]; then [ "$default" = yes ]; return; fi
  if [ ! -r /dev/tty ]; then die 2 "$prompt requires a TTY or an explicit command-line option"; fi
  if [ "$default" = yes ]; then
    read -r -p "$prompt [Y/n] " answer </dev/tty
    case "$answer" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
  else
    read -r -p "$prompt [y/N] " answer </dev/tty
    case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
  fi
}

if [ "$public_mode" = ask ]; then
  if [ "$yes_mode" = 1 ]; then
    public_mode="local"
  elif ask_yes_no "Enable public BSV Third Entries? This requires a funded wallet" no; then
    public_mode="public"
  else
    public_mode="local"
  fi
fi

role_dir="$notary_home/agent/keys"
if [ -z "$key_mode" ]; then
  if [ -f "$role_dir/elder.key.json" ] && [ -f "$role_dir/agent.key.json" ] &&
     [ -f "$role_dir/counterparty.key.json" ]; then
    key_mode="existing"
  elif [ "$yes_mode" = 1 ]; then
    key_mode="generate"
  else
    printf '\nSigning identity:\n  g) generate a new BIP39 wallet (recommended)\n' >/dev/tty
    printf '  m) import an existing BIP39 mnemonic (hidden input)\n' >/dev/tty
    printf '  k) import three existing {wif,address} keyfiles\n' >/dev/tty
    read -r -p 'Choose [g/m/k]: ' choice </dev/tty
    case "$choice" in ""|g|G) key_mode=generate ;; m|M) key_mode=import-mnemonic ;; k|K) key_mode=keyfiles ;;
      *) die 2 "unknown signing-identity choice" ;;
    esac
  fi
fi

if [ "$key_mode" = keyfiles ] && [ "$yes_mode" = 0 ]; then
  [ -n "$elder_source" ] || read -r -p 'Elder {wif,address} keyfile path: ' elder_source </dev/tty
  [ -n "$agent_source" ] || read -r -p 'Agent/model {wif,address} keyfile path: ' agent_source </dev/tty
  [ -n "$counterparty_source" ] || read -r -p 'Counterparty {wif,address} keyfile path: ' counterparty_source </dev/tty
fi
if [ "$key_mode" = keyfiles ]; then
  if [ -z "$elder_source" ] || [ -z "$agent_source" ] || [ -z "$counterparty_source" ]; then
    die 2 "keyfiles mode requires --elder-key-file, --agent-key-file, and --counterparty-key-file"
  fi
fi
if [ "$public_mode" = public ] && [ "$key_mode" = keyfiles ]; then
  die 2 "automatic public Third Entry funding requires a BIP39 wallet; use generate/import-mnemonic (or provision role keys + mnemonic first and rerun with --key-mode existing)"
fi
if [ "$funding_check_only" = 1 ] && [ "$public_mode" != public ]; then
  die 2 "--funding-check-only requires --public-third-entry"
fi

if [ "$dry_run" = 1 ]; then
  say "Resolved setup plan (dry run)"
  info "repository: $ROOT"
  info "state home: $notary_home"
  info "keys: $key_mode"
  info "Third Entry: $public_mode"
  info "system packages: $([ "$skip_system_packages" = 1 ] && echo skip || echo install/verify)"
  info "Python: $python_spec (uv-managed; downloaded if absent)"
  info "model GGUF: $([ "$skip_model_download" = 1 ] && echo skip || echo download+checksum)"
  info "integer artifact: $([ "$skip_model_import" = 1 ] && echo skip || echo import)"
  info "tests: $([ "$skip_tests" = 1 ] && echo skip || echo offline suites)"
  info "blockchain broadcast: $([ "$deploy_agent" = 1 ] && [ "$confirm_mainnet" = 1 ] && echo 'one AgentTea deploy' || echo none)"
  exit 0
fi

mkdir -p "$notary_home"
chmod 700 "$notary_home" 2>/dev/null || true
export BONSAI_NOTARY_HOME="$notary_home"
export BONSAI_ENGINE_DIR="$ROOT/engine"
export BONSAI_CHAIN_C_DIR="$ROOT/chain_c"
export BONSAI_BSV_TE_DIR="$ROOT/bsv_third_entry"
export JOBS="$jobs"

run_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

install_system_dependencies() {
  [ "$skip_system_packages" = 0 ] || { say "Skipping system packages (operator requested)"; return; }
  say "Installing/verifying Linux build dependencies"
  if command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update
    run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
      ca-certificates curl git python3 build-essential cmake pkg-config \
      libsecp256k1-dev libssl-dev libcurl4-openssl-dev
  elif command -v dnf >/dev/null 2>&1; then
    run_root dnf install -y \
      ca-certificates curl git python3 gcc gcc-c++ make cmake pkgconf-pkg-config \
      libsecp256k1-devel openssl-devel libcurl-devel
  else
    warn "unsupported package manager; verifying preinstalled tools only"
  fi
  local tool
  for tool in curl git python3 cmake pkg-config cc; do
    command -v "$tool" >/dev/null 2>&1 || die 2 "required tool not found after package setup: $tool"
  done
}

uv_bin=""
install_uv() {
  local tools_bin="$notary_home/tools/bin" version="${BONSAI_UV_VERSION:-0.11.30}" installer
  if command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
  elif [ -x "$tools_bin/uv" ]; then
    uv_bin="$tools_bin/uv"
  else
    say "Installing pinned uv $version (no shell-profile changes)"
    mkdir -p "$tools_bin"
    installer="$(mktemp)"
    if ! curl -LsSf "https://astral.sh/uv/$version/install.sh" -o "$installer"; then
      rm -f "$installer"
      die 2 "could not download the official uv installer"
    fi
    env UV_INSTALL_DIR="$tools_bin" UV_NO_MODIFY_PATH=1 sh "$installer"
    rm -f "$installer"
    uv_bin="$tools_bin/uv"
  fi
  [ -x "$uv_bin" ] || die 2 "uv installation did not produce an executable"
  info "uv: $($uv_bin --version) ($uv_bin)"
}

wallet_py="$ROOT/wallet/notary_wallet.py"
engine_venv="$ROOT/engine/bonsai/.venv"
engine_py="$engine_venv/bin/python"

backup_engine_venv() {
  local reason="$1" version backup suffix=0
  version="$($engine_py -c 'import platform; print(platform.python_version())' 2>/dev/null || printf unknown)"
  backup="$engine_venv.$reason-python-$version"
  while [ -e "$backup" ] || [ -L "$backup" ]; do
    suffix=$((suffix + 1))
    backup="$engine_venv.$reason-python-$version.$suffix"
  done
  mv "$engine_venv" "$backup"
  warn "preserved incompatible engine environment at $backup"
}

engine_python_supported() {
  "$engine_py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    >/dev/null 2>&1
}

ensure_engine_environment() {
  if { [ -e "$engine_venv" ] || [ -L "$engine_venv" ]; } && [ ! -x "$engine_py" ]; then
    backup_engine_venv incomplete
  elif [ -x "$engine_py" ] && ! engine_python_supported; then
    backup_engine_venv unsupported
  fi
  if [ ! -x "$engine_py" ]; then
    env UV_PYTHON_INSTALL_DIR="$notary_home/tools/python" \
      "$uv_bin" venv --managed-python --python "$python_spec" "$engine_venv"
  fi
  engine_python_supported || die 2 \
    "the resolved engine Python is unsupported; Bonsai pins require Python >=3.11 (requested $python_spec)"
  info "engine Python: $($engine_py -c 'import platform; print(platform.python_version())') ($engine_py)"
}

validate_role_keys() {
  local file
  for file in "$role_dir/elder.key.json" "$role_dir/agent.key.json" "$role_dir/counterparty.key.json"; do
    [ -f "$file" ] || die 2 "missing role key: $file"
    "$engine_py" "$wallet_py" validate-keyfile --path "$file" >/dev/null
    chmod 600 "$file"
  done
  chmod 700 "$role_dir"
}

install_secret_key() {
  local source="$1" destination="$2" tmp
  [ -f "$source" ] || die 2 "keyfile not found: $source"
  "$engine_py" "$wallet_py" validate-keyfile --path "$source" >/dev/null
  mkdir -p "$(dirname "$destination")"
  chmod 700 "$(dirname "$destination")" 2>/dev/null || true
  if [ -e "$destination" ]; then
    if cmp -s "$source" "$destination"; then return; fi
    die 2 "refusing to replace a different existing signing key: $destination"
  fi
  tmp="$(mktemp "$(dirname "$destination")/.key.XXXXXX")"
  chmod 600 "$tmp"
  cp "$source" "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$destination"
}

provision_keys() {
  local source
  say "Provisioning identity-bound receipt and AgentTea signing keys"
  mkdir -p "$role_dir"; chmod 700 "$role_dir"
  case "$key_mode" in
    generate)
      if [ -f "$notary_home/wallet/master_mnemonic.txt" ]; then
        info "reusing existing protected mnemonic"
      else
        "$engine_py" "$wallet_py" gen-mnemonic
      fi
      ;;
    import-mnemonic)
      if [ -n "$mnemonic_file" ]; then
        "$engine_py" "$wallet_py" import-mnemonic --file "$mnemonic_file"
      else
        "$engine_py" "$wallet_py" import-mnemonic
      fi
      ;;
    keyfiles)
      install_secret_key "$elder_source" "$role_dir/elder.key.json"
      install_secret_key "$agent_source" "$role_dir/agent.key.json"
      install_secret_key "$counterparty_source" "$role_dir/counterparty.key.json"
      validate_role_keys
      return
      ;;
    existing)
      validate_role_keys
      return
      ;;
  esac

  for role in elder agent counterparty; do
    source="$("$engine_py" "$wallet_py" keyfile --role "$role" --label "$role")"
    install_secret_key "$source" "$role_dir/$role.key.json"
  done
  validate_role_keys
  warn "Back up $notary_home/wallet/master_mnemonic.txt securely; setup never prints it."
}

funding_preflight() {
  local output status deposit funded_addr funded_sats
  say "Checking public Third Entry wallet funding (read-only)"
  set +e
  output="$("$engine_py" "$wallet_py" funding-status --need "$minimum_sats" --allow-unconfirmed 2>&1)"
  status=$?
  set -e
  if [ "$status" = 3 ]; then
    deposit="$(printf '%s' "$output" | "$engine_py" -c 'import json,sys; print(json.load(sys.stdin)["depositAddress"])' 2>/dev/null || true)"
    printf '\nPUBLIC THIRD ENTRY IS ENABLED, BUT THE WALLET IS NOT FUNDED.\n' >&2
    printf 'Required: one wallet-derived address with at least %s satoshis.\n' "$minimum_sats" >&2
    [ -z "$deposit" ] || printf 'Fund this wallet-owned address: %s\n' "$deposit" >&2
    printf 'No transaction was built or broadcast. Fund it, then rerun the same setup command.\n' >&2
    return 3
  fi
  [ "$status" = 0 ] || die 2 "funding check failed (network/API error): $output"
  funded_addr="$(printf '%s' "$output" | "$engine_py" -c 'import json,sys; print(json.load(sys.stdin)["address"])')"
  funded_sats="$(printf '%s' "$output" | "$engine_py" -c 'import json,sys; print(json.load(sys.stdin)["satoshis"])')"
  info "funded wallet address: $funded_addr ($funded_sats satoshis available)"
}

write_manifest() {
  local ready="$1"
  SETUP_ROOT="$ROOT" SETUP_PUBLIC="$public_mode" SETUP_READY="$ready" \
  SETUP_ROLE_DIR="$role_dir" "$engine_py" - <<'PY'
import json, os, tempfile
from pathlib import Path

home = Path(os.environ["BONSAI_NOTARY_HOME"])
path = home / "setup" / "bonsai-27b.json"
path.parent.mkdir(parents=True, exist_ok=True)
data = {
    "schema": "bonsai-27b-setup/v1",
    "model": "27b",
    "repoRoot": os.environ["SETUP_ROOT"],
    "publicThirdEntry": os.environ["SETUP_PUBLIC"] == "public",
    "ready": os.environ["SETUP_READY"] == "yes",
    "roleKeys": {
        "elder": str(Path(os.environ["SETUP_ROLE_DIR"]) / "elder.key.json"),
        "agent": str(Path(os.environ["SETUP_ROLE_DIR"]) / "agent.key.json"),
        "counterparty": str(Path(os.environ["SETUP_ROLE_DIR"]) / "counterparty.key.json"),
    },
}
payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as manifest:
        fd = -1
        manifest.write(payload)
        manifest.flush()
        os.fsync(manifest.fileno())
    os.replace(tmp, path)
    tmp = ""
    dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
finally:
    if fd >= 0:
        os.close(fd)
    if tmp:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
PY
}

if [ "$funding_check_only" = 1 ]; then
  [ -x "$engine_py" ] || die 2 "existing engine environment not found: $engine_py"
  validate_role_keys
  [ -f "$notary_home/wallet/master_mnemonic.txt" ] ||
    die 2 "public funding preflight needs the wallet mnemonic at $notary_home/wallet/master_mnemonic.txt"
  if ! funding_preflight; then write_manifest no; exit 3; fi
  write_manifest yes
  say "Public Third Entry funding preflight passed"
  exit 0
fi

install_system_dependencies
install_uv

say "Cloning/wiring the three dependency repositories"
"$ROOT/scripts/bootstrap-deps.sh"

say "Creating the deterministic inference environment"
ensure_engine_environment
"$uv_bin" pip install --python "$engine_py" \
  -r "$ROOT/requirements_notary.txt" -r "$ROOT/requirements_wallet.txt" -r "$ROOT/requirements_test.txt"

say "Building chain_c outside the checkout"
BONSAI_NOTARY_HOME="$notary_home" JOBS="$jobs" bash "$ROOT/chain_c/build_chain_c.sh"
if [ "$skip_tests" = 0 ]; then
  ctest --test-dir "$notary_home/chain_c/build" --output-on-failure -j"$jobs" -LE net
fi

say "Building the byte-exact native CPU kernel"
BONSAI_NOTARY_HOME="$notary_home" "$ROOT/engine/bonsai/tools/build_bonsai_q1_kernel.sh"

say "Building the pinned CPU tokenizer required by deterministic inference"
BONSAI_NOTARY_HOME="$notary_home" JOBS="$jobs" "$ROOT/scripts/install-llama-tokenizer.sh"

if command -v nvcc >/dev/null 2>&1; then
  say "Building the optional deterministic CUDA producer"
  if ! BONSAI_NOTARY_HOME="$notary_home" "$ROOT/engine/bonsai/tools/build_bonsai_q1_gpu.sh"; then
    warn "CUDA producer build failed; CPU producer remains available"
  fi
else
  info "nvcc not present; installing the receipt-capable CPU producer (GPU is optional)"
fi

provision_keys

models_dir="${BONSAI_MODELS_DIR:-$notary_home/models}"
mkdir -p "$models_dir"
gguf="$models_dir/Bonsai-27B-Q1_0.gguf"
artifact="$models_dir/Bonsai-27B-Q1_0-int-qwen35.safetensors"
release_identity="$ROOT/engine/bonsai/artifacts/atlas-notarized-bonsai-27b.identity.json"

# Resume-aware free-space guard. A pre-existing GGUF still needs room for the
# artifact; checking only when the download is absent can fill the filesystem
# during the much larger import output.
required_kib=0
if [ "$skip_model_download" = 0 ] && [ ! -f "$gguf" ]; then
  required_kib=$((required_kib + 5000000))
fi
if [ "$skip_model_import" = 0 ] && [ ! -f "$artifact" ]; then
  required_kib=$((required_kib + 6000000))
fi
if ((required_kib > 0)); then
  free_kib="$(df -Pk "$models_dir" | awk 'NR==2 {print $4}')"
  ((free_kib >= required_kib)) ||
    die 2 "less than $required_kib KiB free under $notary_home for the missing 27B model outputs"
fi

if [ "$skip_model_download" = 0 ]; then
  say "Downloading and checksum-verifying the pinned 3.80 GB Bonsai-27B GGUF"
  BONSAI_NOTARY_HOME="$notary_home" "$ROOT/engine/bonsai/scripts/fetch_bonsai_27b_gguf.sh"
fi

if [ "$skip_model_import" = 0 ]; then
  [ -f "$gguf" ] || die 2 "27B GGUF missing at $gguf (cannot import; remove --skip-model-download)"
  if [ -f "$artifact" ]; then
    info "deterministic 27B artifact already present: $artifact"
  else
    say "Importing the receipt-capable deterministic 27B artifact (~4.23 GB)"
    PYTHONPATH="$ROOT/engine/bonsai/src" "$engine_py" \
      -m trinote.cli.import_bonsai35_gguf_cli \
      --gguf "$gguf" --out "$artifact" --context-len 4096
  fi
elif [ ! -f "$artifact" ]; then
  die 2 "27B artifact missing at $artifact; --skip-model-import is valid only when a completed artifact is already present"
fi

say "Validating the complete 27B artifact, release identity, and quality gate"
PYTHONPATH="$ROOT/engine/bonsai/src" "$engine_py" \
  -m trinote.cli.validate_bonsai_artifact_cli \
  --artifact "$artifact" --architecture qwen35 --identity "$release_identity"

if [ "$skip_tests" = 0 ]; then
  say "Running offline composition and Third Entry tests"
  (cd "$ROOT/bsv_third_entry" && PYTHONPATH=. "$engine_py" -m pytest tests/ -q)
  PYTHONPATH="$ROOT/engine/bonsai/src:$ROOT/bsv_third_entry" \
    "$engine_py" -m pytest "$ROOT/tests" -q
fi

say "Checking the resolved Bonsai-27B receipt command"
smoke_args=("setup smoke" --model 27b --receipts -n 1)
[ "$public_mode" = public ] && smoke_args+=(--onchain)
BONSAI_DRYRUN=1 BONSAI_GPU=0 "$ROOT/bonsai-notary" "${smoke_args[@]}" >/dev/null

if [ "$public_mode" = public ]; then
  [ -f "$notary_home/wallet/master_mnemonic.txt" ] ||
    die 2 "public Third Entry setup requires a generated/imported wallet mnemonic"
  if ! funding_preflight; then write_manifest no; exit 3; fi

  # Validate the role/funding/contract path without broadcasting first.
  "$ROOT/bonsai-agent" deploy >/dev/null
  if [ "$deploy_agent" = 1 ]; then
    [ "$confirm_mainnet" = 1 ] || die 2 "--deploy-agent requires --confirm-mainnet because deployment spends real BSV"
    if [ -f "$notary_home/agent/identity.state.json" ]; then
      info "AgentTea identity is already deployed; refusing to deploy a second one"
    else
      say "Deploying the AgentTea identity (explicit mainnet confirmation supplied)"
      "$ROOT/bonsai-agent" deploy --confirm
    fi
  else
    warn "Public mode is funded but the identity is not broadcast. Deploy when ready with: ./bonsai-agent deploy --confirm"
  fi
fi

write_manifest yes
say "Bonsai-27B notary setup complete"
info "state/secrets: $notary_home (never copy or commit this directory)"
info "local receipt: ./bonsai-notary 'Your prompt' --model 27b --receipts"
if [ "$public_mode" = public ]; then
  info "public Third Entry: ./bonsai-notary 'Your prompt' --model 27b --receipts --onchain --chain-confirm"
else
  info "public Third Entry is off; rerun with --public-third-entry when you want funding/deployment checks"
fi
