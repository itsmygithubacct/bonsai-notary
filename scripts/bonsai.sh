#!/usr/bin/env bash
# bonsai.sh — launch the composed notary in one of its modes (thin dispatcher over ./bonsai-notary).
#
# Each mode is a curated flag set; anything run_bonsai_cli accepts can still be appended.
#
#   scripts/bonsai.sh <mode> [PROMPT] [extra flags...]
#
# Modes:
#   json            structured JSON output {thinking,answer,bonsai,receipt,bundle} (deterministic + receipted)
#   repl            interactive REPL (omit PROMPT) — deterministic + receipted
#   deterministic   deterministic integer engine, NO receipt (fastest reproducible run)        [alias: det]
#   receipted       deterministic + local notarized receipt (byte-exact re-execution + verify) [alias: rcpt]
#   onchain         receipted + chain_c Third Entry — DRY-RUN by default (no spend/broadcast)
#   original        raw flagship GGUF via prismml.cpp (float, non-deterministic, NO receipt)    [alias: orig]
#
# Onchain: builds the Third Entry via chain_c/build/bonsai_third_entry but does NOT broadcast
#          unless you append --chain-confirm (it spends real BSV). See SECURITY.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH="$ROOT/bonsai-notary"

usage() { sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
[ $# -ge 1 ] || usage 1
mode="$1"; shift

case "$mode" in
    json)              exec "$LAUNCH" json --receipts "$@" ;;
    repl)              exec "$LAUNCH" repl --receipts "$@" ;;
    deterministic|det) exec "$LAUNCH" --no-receipt "$@" ;;
    receipted|rcpt)    exec "$LAUNCH" --receipts "$@" ;;
    onchain)
        case " $* " in
            *" --chain-confirm "*) echo "[bonsai.sh] ONCHAIN: --chain-confirm set — BROADCASTS a real BSV tx via chain_c." >&2 ;;
            *) echo "[bonsai.sh] onchain DRY-RUN (builds the Third Entry via chain_c, no broadcast). Append --chain-confirm to spend." >&2 ;;
        esac
        exec "$LAUNCH" --receipts --onchain "$@" ;;
    original|orig)     exec "$LAUNCH" --engine prismml.cpp --no-receipt "$@" ;;
    -h|--help|help)    usage 0 ;;
    *) echo "bonsai.sh: unknown mode '$mode' (try: json repl deterministic receipted onchain original)" >&2; exit 2 ;;
esac
