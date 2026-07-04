# Receipt bundles — package and verify a notarized inference

A **receipt bundle** is the self-contained, content-addressed artifact a third party needs to audit a
notarized Bonsai inference *without trusting the producer*. It packages the receipt, the off-chain
preimage, the chain artifact, and a description of where the third entry landed on BSV, under a single
manifest whose `bundleHash` commits every file.

Two CLIs:

```
trinote-receipt-bundle pack    …            # build a bundle (directory or .tar.gz)
trinote-receipt-bundle verify  BUNDLE …     # verify it (offline; optional on-chain + re-execution)
trinote-receipt-bundle inspect BUNDLE       # print the manifest + on-chain descriptor
```

This is the consumer-facing counterpart to [`THIRD-ENTRY.md`](THIRD-ENTRY.md) (how the third entry is
produced) and [`RECEIPTS.md`](RECEIPTS.md) (the receipt lifecycle).

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
of every third entry is also written to the off-chain **transaction log** (`artifacts/receipts/transactions.log`,
JSONL) by `trinote-run-bonsai --tx-log` / `trinote-agent run --tx-log`, alongside the artifact `broadcast.log`.

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

### Standalone

```bash
# 1. run an inference and save its {receipt,preimage}+emission. --onchain builds the third entry; add
#    --chain-confirm to actually broadcast it (DRY-RUN otherwise — the two-key interlock).
trinote-run-bonsai -p "…" --onchain --chain-confirm --save-bundle artifacts/bundles/in/
# 2. package it (use the saved emission once the receipt was actually broadcast)
trinote-receipt-bundle pack \
    --receipt-bundle artifacts/bundles/in/receipt-<rh>.json \
    --from-emission  artifacts/bundles/in/emission-<rh>.json \
    -o artifacts/bundles/<rh> [--tar]
```

`pack` also accepts `--txid <txid>` (build a standalone descriptor from a known txid) or `--onchain
<file>` (an explicit descriptor).

### Stateful

`trinote-agent run` (see [`AGENT-LIFECYCLE.md`](../identity/AGENT-LIFECYCLE.md)) runs the inference, commits it
as an `executeAction` under the identity, and packs the stateful bundle in one step (`--bundle-out`).

---

## Verifying a bundle

```bash
trinote-receipt-bundle verify artifacts/bundles/<rh>                       # offline only (no deps, no network)
trinote-receipt-bundle verify artifacts/bundles/<rh> --onchain            # + confirm on WhatsOnChain
trinote-receipt-bundle verify artifacts/bundles/<rh> --onchain \
    --reexec --artifact artifacts/model/atlas-notarized-bonsai-8b.safetensors   # + bit-exact re-run
```

`verify` exits `0` when `VERIFIED`, non-zero otherwise. `--json` emits the full per-check result for
machine consumers. The offline + on-chain layers need only the Python standard library (`urllib` for the
WhatsOnChain fetch); `--reexec` is the only layer that loads the model.
