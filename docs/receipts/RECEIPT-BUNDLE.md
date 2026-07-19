# Receipt bundles — package and verify a notarized inference

A **receipt bundle** is the self-contained, content-addressed artifact a third party needs to audit a
notarized Bonsai inference *without trusting the producer*. It packages the receipt, the off-chain
preimage, the chain artifact, and a description of where the third entry landed on BSV, under a single
manifest whose `bundleHash` commits every file.

The engine exposes one CLI with three subcommands (shown here by its console-script name; the module form
used below works directly from the composed checkout):

```
trinote-receipt-bundle pack    …            # build a bundle (directory or .tar.gz)
trinote-receipt-bundle verify  BUNDLE …     # verify it (offline; optional on-chain + re-execution)
trinote-receipt-bundle inspect BUNDLE       # print the manifest + on-chain descriptor
```

This is the consumer-facing counterpart to [`THIRD-ENTRY.md`](THIRD-ENTRY.md) (how the third entry is
produced) and [`RECEIPTS.md`](RECEIPTS.md) (the receipt lifecycle).

Receipted `./bonsai-notary` runs automatically create a local `.tar.gz` bundle under
`$BONSAI_NOTARY_HOME/bundles`; use `--no-bundle` to opt out. In the REPL, `/bundle` packages the last
receipt and `/verify` replays it. These local bundles include a plaintext transcript, so treat them as
private unless you intend to disclose the prompt and answer. They never contain model weights or private
signing keys.

---

## What a bundle contains

```
<bundle>/
  manifest.json          # {schema, kind, modelHash, receiptHash, files:{name:sha256}, bundleHash}
  receipt.json           # the on-chain-committable half: commitments + signatures + receiptHash
  preimage.json          # the off-chain half: token ids + sampler + trace (needed to re-execute)
  chain-artifact.json    # {schema, tag, modelHash, receiptHash, samplerMode, seed}
  onchain.json           # where/how the third entry landed (standalone OP_RETURN or stateful action)
  ledger-head.json       # OPTIONAL local hash-linked ledger entry for this receipt
  identity.json          # STATEFUL ONLY — the AgentTea identity the action ran under
```

Every file is written as **canonical bytes** (sorted keys, compact, UTF-8) so its digest is reproducible
by any party. `manifest.files` records each file's `sha256`; `bundleHash = sha256(canonical(manifest
without bundleHash))`. A one-byte change to any file changes its digest, which changes the `bundleHash` —
so a single 32-byte value pins the whole package.

### `onchain.json` — standalone

```json
{ "kind": "standalone", "network": "main", "tag": "trinote/r1",
  "txid": "…", "modelHash": "…", "receiptHash": "…", "rawTx": "…(optional)" }
```

When `rawTx` (the full signed transaction hex) is present, the bundle is self-contained and
re-broadcastable, and the offline layer additionally checks `txid == hash256(rawTx)`. The same `rawTx`/txid
of every third entry is also written to the off-chain **transaction log**
(`$BONSAI_NOTARY_HOME/receipts/transactions.log`, JSONL) by the notary's `--tx-log` option, alongside the
dry-run `broadcast.log`.

### `onchain.json` — stateful (AgentTea `executeAction`)

```json
{ "kind": "stateful", "network": "main", "actionTxid": "…", "receiptVout": 1,
  "receiptHashOnChain": "…",
  "action": { "amount": 1000, "txCount": 0, "lockTime": 1718000000,
              "actionHash": "<= the receiptHash>", "provenanceHash": "<= the modelHash>" } }
```

with a sibling `identity.json`:

```json
{ "ricardianHash": "…", "genesisTxid": "…", "agentPubKey": "…", "counterpartyPubKey": "…" }
```

The stateful OP_RETURN does **not** carry the raw `(modelHash, receiptHash)`. It carries a single 32-byte
hash over the action's eight committed fields (see [`AGENT-LIFECYCLE.md`](../identity/AGENT-LIFECYCLE.md)).
The bundle records those fields so the verifier can recompute that hash and bind it back to the inference:
`actionHash == receiptHash` and `provenanceHash == modelHash`.

---

## Verification — three independent layers

`trinote-receipt-bundle verify` runs up to three layers; a bundle is `VERIFIED` iff every requested layer
passes. Each is reported separately, so a consumer chooses how much trust they need.

| Layer | Flag | Needs | Proves |
|---|---|---|---|
| **offline** | (always) | stdlib only | the bundle is internally consistent: file digests, `bundleHash`, `receiptHash`, `inputCommit`/`outputCommit`/`traceCommit`, the chain artifact, (stateful) the recomputed AgentTea action hash, and — when the bundle carries the raw tx — that `txid == hash256(rawTx)` |
| **on-chain** | `--onchain` | network (WhatsOnChain) | the third entry is **published and immutable** on BSV: the tx exists, its OP_RETURN matches the receipt (stateful: matches the recomputed action hash and the action chains back to the genesis identity tx) |
| **re-exec** | `--reexec --artifact A.safetensors` | the model weights | the model **actually produced** the output: a bit-exact re-run of the reference engine reproduces the committed output ids |

The offline + re-exec layers are the *trustless core* — no key, no chain. The on-chain layer adds *public
ordering and non-suppressibility*. None of them prove the output is **correct** — a receipt proves
provenance, not quality (see [`THIRD-ENTRY.md`](THIRD-ENTRY.md) §"Honest scope").

---

## Producing a bundle

### Local bundle (automatic)

```bash
./bonsai-notary "What is a Merkle tree?" --receipts -n 128
# stderr prints: [bundle] $BONSAI_NOTARY_HOME/bundles/bonsai-<receiptHash>.tar.gz
```

This form records the local ledger as its Third Entry and is independently re-executable, but it does not
claim a BSV transaction. To package a broadcast transaction descriptor, save the raw pack inputs as shown
next.

### Broadcast standalone or stateful bundle

```bash
export BONSAI_NOTARY_HOME="${BONSAI_NOTARY_HOME:-$HOME/.local/trinote}"
INPUTS="$BONSAI_NOTARY_HOME/bundle-inputs"

# --chain-confirm is required here because --from-emission rejects a dry-run txid.
./bonsai-notary "Notarize this." --receipts --onchain --chain-confirm \
  --save-bundle "$INPUTS"

# Substitute the receiptHash printed by the run for <rh>. A stateful agentd action record is detected
# automatically; otherwise this builds a standalone descriptor.
PYTHONPATH=engine/bonsai/src engine/bonsai/.venv/bin/python \
  -m trinote.cli.receipt_bundle_cli pack \
  --receipt-bundle "$INPUTS/receipt-<rh>.json" \
  --from-emission "$INPUTS/emission-<rh>.json" \
  -o "$BONSAI_NOTARY_HOME/bundles/bonsai-<rh>.tar.gz" --tar
```

`pack` also accepts `--txid <txid>` (build a standalone descriptor from a known txid) or `--onchain
<file>` (an explicit descriptor).

For a stateful bundle, `--from-emission` derives both `onchain.json` and `identity.json` from the complete
AgentTea action record and rejects incomplete or disagreeing identity data. See
[`AGENT-LIFECYCLE.md`](../identity/AGENT-LIFECYCLE.md).

---

## Verifying a bundle

```bash
export BONSAI_NOTARY_HOME="${BONSAI_NOTARY_HOME:-$HOME/.local/trinote}"
BUNDLE="$BONSAI_NOTARY_HOME/bundles/bonsai-<rh>.tar.gz"
BUNDLE_CLI=(engine/bonsai/.venv/bin/python -m trinote.cli.receipt_bundle_cli)
export PYTHONPATH=engine/bonsai/src

"${BUNDLE_CLI[@]}" verify "$BUNDLE"                         # offline hashes/signatures
"${BUNDLE_CLI[@]}" verify "$BUNDLE" --onchain               # + WhatsOnChain lookup
"${BUNDLE_CLI[@]}" verify "$BUNDLE" --onchain --reexec \
  --artifact "$BONSAI_NOTARY_HOME/models/Bonsai-27B-Q1_0-int-qwen35.safetensors"
```

`verify` exits `0` when `VERIFIED`, non-zero otherwise. `--json` emits the full per-check result for
machine consumers. The offline + on-chain layers need only the Python standard library (`urllib` for the
WhatsOnChain fetch); `--reexec` is the only layer that loads the model. The loader selects the Qwen3 or
Qwen3.5 integer graph from the artifact metadata. Add `--oracle` to force the slow pure-NumPy path; the
default native re-execution accelerator is required to remain byte-identical.
