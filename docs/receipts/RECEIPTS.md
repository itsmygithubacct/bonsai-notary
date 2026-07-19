# RECEIPTS.md — per-inference TEA receipts: build, record, verify, broadcast (all local)

> **Scope.** The local receipt core—build, secp256k1-sign, append to a hash-linked ledger, and verify by
> re-execution—works without a network or blockchain. The optional BSV layer is provided by
> [`chain_c`](https://github.com/itsmygithubacct/chain_c), driven by
> [`bsv_third_entry`](https://github.com/itsmygithubacct/bsv_third_entry). It is default-off and dry-runs
> unless separately confirmed. `./bonsai-notary … --receipts` is the local path;
> `./bonsai-notary … --receipts --onchain` and `./bonsai-agent` expose the composed Third Entry path.
> The standalone wallet backend remains available for deployments that choose it.

## The triple-entry receipt

A served inference emits a TEA receipt
(`Receipt = σ_Ledger( σ_Model(input, output, trace) + σ_Counterparty(input, output) )`). All three
entries are produced locally:

| Entry | DESIGN §5.3 | Here |
|---|---|---|
| 1st `σ_Model` | model signature | `secp256k1-ecdsa@v1` model signature by default; labeled `local-hmac@v1` only in demo mode |
| 2nd `σ_Counterparty` | caller signature | `secp256k1-ecdsa@v1` counterparty signature by default; labeled `local-hmac@v1` only in demo mode |
| 3rd `σ_Ledger` | `OP_RETURN` on BSV | local hash-linked ledger **+** (gated) a real BSV `OP_RETURN` |

`build_receipt` returns a **bundle** `{"receipt": …, "preimage": …}` (DESIGN §5.3 on/off-chain split):
the `receipt` (`trinote.receipt/v1`) carries commitments + signatures + `receiptHash` and **no raw
text**; the `preimage` carries the off-chain token IDs, sampler, and trace a verifier needs.

## The trustless core (no key, no chain)

`verify_receipt` (DESIGN §5.4) **recomputes** rather than believes:
1. recompute `inputCommit`/`outputCommit` from the preimage ids → must equal the receipt
2. recompute `receiptHash` from the receipt body → must equal the committed value
3. re-run the bit-exact forward and committed integer sampler on the reference engine
   (`infer_int/verify.py`) → the output must re-derive token-for-token
4. verify the default secp256k1 signatures from their committed public keys; legacy HMAC demo receipts
   instead require the shared secret

Steps 1–3 need no key and no chain. This is what makes the third entry *trustless*.

## The publish gate (the 3rd entry's on-chain leg)

`emit_receipt` always records the receipt to the local hash-linked ledger, then **publishes** the
chain artifact (`OP_FALSE OP_RETURN <"trinote/r1"> <modelHash> <receiptHash>`, DESIGN §5.3) by a
two-key interlock:

> **Chain artifact v2 (`trinote.chain-receipt/v2`).** The artifact also carries the committed sampler
> `mode` and `seed` (the draw nonce), so a *randomized* seed is notarized in the third entry itself, not
> only off-chain in the preimage (it is already bound transitively via `receiptHash`). See
> [SAMPLER-INTEGER.md](../architecture/SAMPLER-INTEGER.md). To land `seed` in the literal OP_RETURN *bytes*, the `chain_c`
> builder (see 'Scope') must encode the field; the default log backend already
> records the full v2 artifact.

| `enable_chain` | `broadcast_to_log` | `confirm` | Result |
|---|---|---|---|
| `False` (default) | `True` (default) | — | **`logged`** — dry-run "broadcast" to a local JSONL log; `txid="log:…"`. No network. |
| `False` | `False` | — | `disabled` — local ledger only, no publish |
| `True` | — | `False` (default) | `dry-run` — the `chain_c` broadcaster **builds + signs** the tx, returns its txid, does NOT send |
| `True` | — | `True` | **`broadcast`** — real mainnet `OP_RETURN` via WhatsOnChain |

WhatsOnChain is **mainnet-only (real BSV)**, so a real send requires BOTH `enable_chain=True` AND
`confirm=True` (→ the `chain_c` gate `CONFIRM_MAINNET_BROADCAST=yes`). The default publishes nothing
to the network.

## The on-chain leg — `chain_c`, driven by `bsv_third_entry`

The BSV broadcaster is the byte-exact C port
[`chain_c`](https://github.com/itsmygithubacct/chain_c) (the C reimplementation of the chain layer). The
Python orchestration [`bsv_third_entry`](https://github.com/itsmygithubacct/bsv_third_entry)
drives its CLIs locally — no remote host:

- `chain_c`'s `bonsai_third_entry` CLI — resolves the receipt's `ACTION_HASH` (its `receiptHash`) and
  `PROVENANCE_HASH` (its `modelHash`), funds + builds + signs the §5.3 `OP_RETURN` against WhatsOnChain,
  and prints the plan + txid. DRY-RUN unless `CONFIRM_MAINNET_BROADCAST=yes`.
- `bsv_third_entry.chain_backends::ChainCThirdEntryBackend` — the Python side: it validates the 32-byte
  commitment hashes (fail-closed), invokes the `chain_c` CLI, and parses its stdout. `confirm=False`
  (DRY-RUN) by default, and it sets `CONFIRM_MAINNET_BROADCAST=yes` only when confirmed.

**Build the chain layer once** (no keys, no broadcast):

```bash
bash chain_c/build_chain_c.sh   # builds the chain_c CLIs under chain_c/build
```

## Two on-chain shapes for the third entry

The third entry can be published two ways. They are **additive** — the lightweight one stays the right
tool for cheap, parallel, identity-less timestamps; the stateful one is the richer binding.

| | Standalone `OP_RETURN` | Stateful `AgentTea` |
|---|---|---|
| Driver | `chain_c` `bonsai_third_entry` (one-shot) / `wallet/notary_wallet.py third-entry` | `chain_c` `agentd action` (resumable identity) |
| Shape | `OP_FALSE OP_RETURN <tag> <modelHash> <receiptHash>` | `AgentTea.executeAction` §5.3 state transition |
| Binds to | nothing — an anonymous public mark | a deployed identity: the agent co-signs in-script, `txCount` reputation accrues, window metering / Elder revoke all live |
| Needs | one funded P2PKH key | a deployed identity UTXO + Elder/agent/funding keys + the persisted state tip |
| Cost | ~250 bytes | the `AgentTea` contract carried in the spend (tens of KB) |
| Parallel? | yes | no — each action spends the prior identity output[0] |

The stateful path is documented end-to-end in
[`../identity/AGENT-LIFECYCLE.md`](../identity/AGENT-LIFECYCLE.md); both shapes have live mainnet examples
in [`THIRD-ENTRY.md`](THIRD-ENTRY.md) §6.

## The stateful path (`AgentTea.executeAction`)

The stateful third entry binds the receipt to a **persistent on-chain `AgentTea` identity** (deployed
once, then advanced by metered actions) instead of an anonymous mark. Each action binds the inference's
hashes into the contract:

- `actionHash` := the trinote `receiptHash` (the inference receipt itself)
- `provenanceHash` := the trinote `modelHash` (which model produced it)

`AgentTea.executeAction` hashes these (with the charter/agent/counterparty/amount/txCount/locktime
fields) into **its own** on-chain `receiptHash`, which is what the action's `OP_RETURN` pins — so the
third entry transitively commits the trinote receipt and stamps it onto the identity's `txCount`
reputation chain. A verifier recomputes the same hash from the recorded fields — no node, no chain. The
exact eight-field preimage and its golden-pinned Python recompute are documented in
[`../identity/AGENT-LIFECYCLE.md`](../identity/AGENT-LIFECYCLE.md).

This path runs through `chain_c`'s `agentd` CLI (driven by `bsv_third_entry`, surfaced as `./bonsai-agent`
and as `./bonsai-notary … --onchain`). It is **DRY-RUN by default** and only broadcasts under the two-key
interlock (`--confirm` / `--chain-confirm` → `CONFIRM_MAINNET_BROADCAST=yes`); the full deploy →
`executeAction` → revoke lifecycle has been exercised live on mainnet (see [`THIRD-ENTRY.md`](THIRD-ENTRY.md)
§6 for the txids):

```bash
# deploy the resumable identity ONCE (DRY-RUN unless --confirm; --confirm spends BSV):
./bonsai-agent deploy --ricardian-hash <64hex>

# thereafter, every receipted inference's third entry is a cheap agentd action under that identity:
./bonsai-notary "Notarize this." --receipts --onchain                 # DRY-RUN (build + sign, no send)
./bonsai-notary "Notarize this." --receipts --onchain --chain-confirm # real broadcast (spends BSV)

# or drive the action directly from already-computed hashes:
./bonsai-agent action --action-hash <receiptHash> --provenance-hash <modelHash>
```

## CLI — `./bonsai-notary`

In the composed repo the receipt flow is driven by the **`./bonsai-notary`** launcher (infer → receipt →
verify, via `bsv_third_entry.engine_run` over the integer engine); `./scripts/bonsai.sh` is a thin
mode dispatcher over it. These replace the pre-composition `cli/trinote-run-bonsai` / `bonsai_notary.sh`
entrypoints.

```bash
# the shipped receipt path: byte-exact int-ref@bonsai-qwen3, committed sampler + re-execution
./bonsai-notary "What is a Merkle tree?" --receipts -n 8
./scripts/bonsai.sh receipted "What is a tensor?"     # mode dispatcher over the same path

# Bonsai-27B: distinct Qwen3.5 identity + quality gate + separately loaded fresh CPU oracle
./bonsai-notary "How many r's are in strawberry?" --model 27b --receipts -n 128
```

The regular `./scripts/bonsai.sh bonsai27` launcher is intentionally **not** a receipt path: its
floating-point PrismML llama.cpp execution is outside the integer determinism contract. In contrast,
`./bonsai-notary … --model 27b --receipts` loads the imported Qwen3.5 artifact, validates its distinct
identity and hash-bound quality gate, and re-executes with a fresh native-disabled CPU oracle. The optimized
CPU/GPU producer cannot verify itself. See [`../BONSAI-27B.md`](../BONSAI-27B.md).

The launcher emits + verifies receipts (build + record + re-execute) and exposes the primitives in the
engine's `trinote.receipts` library:

```bash
# build + record + DRY-RUN log-broadcast (mainnet OFF), then verify by re-execution
./bonsai-notary "What is a Merkle tree?" --receipts -n 8
```

Receipted runs package a portable local bundle under `$BONSAI_NOTARY_HOME/bundles` by default. In the REPL,
`/bundle` packages the last receipt and `/verify` replays it. The engine's
`trinote.cli.receipt_bundle_cli` module provides offline, on-chain, and re-execution verification for
third-party consumers; see [`RECEIPT-BUNDLE.md`](RECEIPT-BUNDLE.md).


```python
from trinote.receipts import keygen, build_receipt, verify_receipt, LocalLedger

mk, ck = keygen(label="model"), keygen(label="counterparty")        # local signing keys (hold SECRETS)
bundle = build_receipt(model_hash=mh, input_ids=ids, output_ids=out,
                       sampler={"mode": "greedy"}, model_key=mk, counterparty_key=ck)
verify_receipt(bundle, model=model, model_digest=mh)                 # recompute + re-execute

# ledger verification is a LIBRARY call (no `trinote-receipt ledger --verify` CLI exists):
from trinote.notary_paths import ledger_default
LocalLedger(ledger_default()).verify_chain()       # → {"ok": True/False, "brokenAt": …}
```

> **Ledger caveat.** `verify_chain()` checks the hash-links of the local ledger, so it catches an
> *in-place* edit, but the local ledger is tamper-evident only **relative to an externally-trusted head**:
> a tail-truncation or full rewrite is not caught without an external anchor. The durable anchor is the
> on-chain `OP_RETURN` third entry (default-OFF; see the scope banner above).

With no key flags, `build_receipt`/`keygen` can mint **ephemeral** keys (sigs recorded but not later
re-verifiable — the trustless checks don't need them). `modelHash` defaults to the artifact's
`artifactDigest`; pass an explicit model hash for a released model's full
`H(ricardianHash ‖ datasetRoot ‖ weightsRoot)` — that hash logic lives in `src/trinote/charter.py`
(`ModelConfig.as_params_block` + charter compare) and `src/trinote/hashing/sha.py`.

## Library

```python
from trinote.receipts import (keygen, build_receipt, emit_receipt, verify_receipt,
                              LocalLedger, WalletThirdEntryBackend)

mk, ck = keygen(label="model"), keygen(label="counterparty")
bundle = build_receipt(model_hash=mh, input_ids=ids, output_ids=out,
                       sampler={"mode": "greedy"}, model_key=mk, counterparty_key=ck)

# default: dry-run log-broadcast
rec = emit_receipt(bundle["receipt"], ledger=LocalLedger("ledger.jsonl"))   # rec["onchain"]["status"] == "logged"

# real broadcast (two-key interlock): enable_chain + a confirm=True backend (the project's own BSV wallet)
backend = WalletThirdEntryBackend(source_index=21, confirm=True)
rec = emit_receipt(bundle["receipt"], ledger=LocalLedger("ledger.jsonl"),
                   enable_chain=True, chain_backend=backend)               # broadcasts mainnet

res = verify_receipt(bundle, model=ref_model, model_key=mk, counterparty_key=ck)  # res["ok"]
```

## Honest scope (the point of the discipline)

- **Binds provenance/liability/auditability, not correctness** (CHARTER §1, DESIGN §5.10). A valid,
  broadcast receipt says "*this* model produced *this* output, recorded undeniably" — not that the
  output is right.
- **`local-hmac` is a plumbing vouch**, symmetric, labeled so it is never mistaken for the on-chain
  Rabin scheme. The v1 counterparty may be a self-counterparty (DESIGN §6.6 #9): proves the wiring, not
  yet adversarially meaningful.
- **The local ledger detects tampering, doesn't prevent it.** A broadcast `OP_RETURN` (when you turn it
  on) is what adds non-repudiation against the operator — a local file's holder can still truncate it.
- **The stateful path's `amount` is dust, not a price.** Wiring an inference through a settlement
  contract pays a token dust amount to a (possibly self-) counterparty: it proves the receipt threads
  through the identity lifecycle and accrues `txCount`, not that the inference was a paid transaction —
  the same labeled-plumbing discipline as the `local-hmac` vouch.
- **Mainnet costs real money.** The two-key interlock + DRY-RUN default exist so a broadcast is always
  deliberate. The trustless re-verification (commitments + bit-exact re-execution) needs no chain at
  all — the chain only adds public, undeniable timestamping of the receipt hash.
