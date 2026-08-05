#!/usr/bin/env bash
# provision-counterparty.sh — set up this host to hold the counterparty key.
#
# Run it ON the machine that will be the counterparty. It prints the public key the
# producer must pin, and nothing that is a secret.
#
#   ./scripts/provision-counterparty.sh                    # mint if absent, install
#   ./scripts/provision-counterparty.sh --print-identity   # what to pin, no changes
#
# ## What a counterparty is for
#
# A receipt carries two signatures. The model signature says an engine produced this
# output; the counterparty signature says someone other than that engine agrees. The
# second is the whole reason a receipt beats a log line, and it is worth nothing if both
# keys live on the producer — one party signing twice, in bytes indistinguishable from
# genuine two-party attestation.
#
# So this runs somewhere else. Somewhere small is fine: signing needs `ecdsa` and
# nothing else. Verification re-executes the model and needs the whole engine, but a
# counterparty never verifies. A Raspberry Pi is a perfectly good counterparty and a
# hopeless verifier, and that asymmetry is the point.
#
# ## What it does not do
#
# It does not open a listening port. The producer runs this host's signer over SSH, so
# the connection is outbound from the producer and there is no inbound service to
# defend. It also does not copy a key from anywhere — the secret is generated here and
# stays here, and only the public half is ever printed.
set -euo pipefail

HOME_DIR="${BONSAI_NOTARY_HOME:-$HOME/.local/trinote}"
KEY_PATH="$HOME_DIR/keys/counterparty.key.json"
ENGINE_SRC="${TRINOTE_ENGINE_SRC:-$HOME_DIR/engine}"
ENGINE_REPO="https://github.com/itsmygithubacct/integer_inference_engine.git"
WRAPPER="$HOME/.local/bin/trinote-counterparty-sign"
NOTARY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRINT_ONLY=0
ALLOW_UNBOUND=0

while [ $# -gt 0 ]; do
  case "$1" in
    --print-identity) PRINT_ONLY=1 ;;
    --allow-unbound)  ALLOW_UNBOUND=1 ;;
    --key)            KEY_PATH="$2"; shift ;;
    --engine-src)     ENGINE_SRC="$2"; shift ;;
    -h|--help)        sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '  %-22s %s\n' "$1" "$2"; }

# ---------------------------------------------------------------- python + ecdsa
# System python is used when it already has ecdsa — which on a small box it often does,
# and a venv for one pure-python dependency is a maintenance burden with no payoff.
PY=python3
if ! $PY -c 'import ecdsa' 2>/dev/null; then
  VENV="$HOME_DIR/venv"
  if [ ! -x "$VENV/bin/python" ]; then
    echo "==> ecdsa not present; creating a venv at $VENV"
    $PY -m venv "$VENV"
    "$VENV/bin/pip" install --quiet ecdsa
  fi
  PY="$VENV/bin/python"
fi
$PY -c 'import ecdsa' 2>/dev/null || { echo "FATAL: ecdsa unavailable" >&2; exit 1; }

# ------------------------------------------------------------------- engine source
# Only the receipt key format and signer are used from it. Shallow, because history is
# of no interest to a machine whose whole job is to hold one key.
if [ ! -d "$ENGINE_SRC/bonsai/src/trinote" ]; then
  echo "==> fetching the receipt signer sources into $ENGINE_SRC"
  mkdir -p "$(dirname "$ENGINE_SRC")"
  git clone --quiet --depth 1 "$ENGINE_REPO" "$ENGINE_SRC"
fi
ENGINE_PY="$ENGINE_SRC/bonsai/src"

# -------------------------------------------------------------------------- the key
mkdir -p "$(dirname "$KEY_PATH")"
chmod 700 "$(dirname "$KEY_PATH")"

if [ ! -f "$KEY_PATH" ]; then
  if [ "$PRINT_ONLY" = "1" ]; then
    echo "no counterparty key at $KEY_PATH (run without --print-identity to mint one)" >&2
    exit 1
  fi
  echo "==> minting a counterparty key"
  # Provisioning is the ONE moment a counterparty key may be created. The signing
  # service itself refuses to generate: a key invented on first contact would make the
  # producer's pin meaningless, because whatever answered would become the counterparty.
  PYTHONPATH="$ENGINE_PY" "$PY" - "$KEY_PATH" <<'PYEOF'
import json, pathlib, sys
from trinote.receipts.signing_ec import ECKey
path = pathlib.Path(sys.argv[1])
key = ECKey.generate(label="counterparty")
key.save(path)
PYEOF
  chmod 600 "$KEY_PATH"
fi

IDENTITY="$(PYTHONPATH="$ENGINE_PY" "$PY" - "$KEY_PATH" <<'PYEOF'
import json, pathlib, sys
from trinote.receipts.signing_ec import ECKey
key = ECKey.from_json(json.loads(pathlib.Path(sys.argv[1]).read_text()))
print(json.dumps({"publicKey": key.public_hex, "keyId": key.key_id}))
PYEOF
)"
PUBKEY="$(printf '%s' "$IDENTITY" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["publicKey"])')"
KEYID="$(printf '%s' "$IDENTITY" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["keyId"])')"

if [ "$PRINT_ONLY" = "1" ]; then
  printf '%s\n' "$IDENTITY"
  exit 0
fi

# ---------------------------------------------------------------------- the wrapper
# An absolute path everywhere, because a non-interactive SSH command does not read the
# shell profile — ~/.local/bin is not on PATH for `ssh host trinote-counterparty-sign`
# unless it is spelled out. That is a real failure mode and it looks like a transport
# error when it happens.
mkdir -p "$(dirname "$WRAPPER")"
POLICY=""
[ "$ALLOW_UNBOUND" = "1" ] && POLICY=" --allow-unbound"
cat > "$WRAPPER" <<EOF
#!/bin/sh
# The counterparty half of a two-party Trinote receipt. One request on stdin, one
# response on stdout. Generated by provision-counterparty.sh — edit the source, not this.
exec env PYTHONPATH="$ENGINE_PY" "$PY" \\
    "$NOTARY_ROOT/scripts/counterparty-sign.py" \\
    --key "$KEY_PATH"$POLICY "\$@"
EOF
chmod +x "$WRAPPER"

# ------------------------------------------------------------------------- verify it
# Provisioning that ends without exercising the thing it provisioned is a guess. Sign a
# throwaway message end to end through the wrapper and check the answer verifies.
PROBE="$(printf '{"message":{"contextCommit":"%s","inputCommit":"%s","modelHash":"%s","outputCommit":"%s"},"schema":"trinote.counterparty-signing-request/v1"}' \
  "$(printf 'c%.0s' $(seq 64))" "$(printf 'b%.0s' $(seq 64))" \
  "$(printf 'a%.0s' $(seq 64))" "$(printf 'd%.0s' $(seq 64))" | "$WRAPPER")"

# The probe travels as an argument, not on stdin: this python reads its program from a
# heredoc, and a heredoc and a pipe cannot both be stdin — the heredoc wins, and
# json.load(sys.stdin) quietly parses the script's own text.
PYTHONPATH="$ENGINE_PY" "$PY" - "$PUBKEY" "$PROBE" <<'PYEOF'
import json, sys
from trinote.receipts.signing import verify_signature
resp = json.loads(sys.argv[2])
if not resp.get("ok"):
    sys.exit(f"FATAL: the signer refused a well-formed probe: {resp.get('code')}")
msg = {"contextCommit": "c"*64, "inputCommit": "b"*64, "modelHash": "a"*64, "outputCommit": "d"*64}
payload = json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
if not verify_signature(payload, resp["signature"], expected_pubkey=sys.argv[1]):
    sys.exit("FATAL: the signature does not verify against this host's own public key")
print("  probe                  signed and verified end to end")
PYEOF

echo
echo "counterparty ready on $(hostname)"
say "key" "$KEY_PATH (mode $(stat -c %a "$KEY_PATH"))"
say "signer" "$WRAPPER"
say "python" "$PY"
say "keyId" "$KEYID"
echo
echo "On the PRODUCER, pin this host — the public key, never the secret:"
echo
echo "  export TRINOTE_COUNTERPARTY_COMMAND=\"ssh -o BatchMode=yes $(hostname) $WRAPPER\""
echo "  export TRINOTE_COUNTERPARTY_PUBKEY=\"$PUBKEY\""
echo
echo "The producer needs key-based SSH to this host. Nothing here listens on a port."
