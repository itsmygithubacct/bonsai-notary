# THIRD-ENTRY.md — what the third entry is, and how bonsai-notary lands it on BSV

This document explains the **third entry** of a bonsai-notary receipt: the theory it comes from, what it
commits, the two forms it takes in this project (a local hash-linked ledger and a public BSV `OP_RETURN`),
how it is produced from the project's own self-managed wallet, and how anyone can verify it.

---

## 1. Where "third entry" comes from (triple-entry accounting)

Classical double-entry bookkeeping records every transaction twice — once in each party's books. Ian Grigg's
**triple-entry accounting** (Grigg, 2005, <https://iang.org/papers/triple_entry.html>) adds a **third
entry**: a single, **digitally signed, shared receipt** that *is* the transaction. Where double entry leaves
two private, separately-mutable copies, the third entry is one record that both parties — and any third party
— can see and cannot unilaterally alter. Its trust comes from being **signed** (any change breaks the
signature) **and shared/published** (a record no single party can silently rewrite). Bitcoin/BSV removes the
need for a trusted intermediary to hold that shared record: the public ledger *is* the neutral third place.

Sgantzos, Al Hemairy, Tzavaras & Stelios (2023), *"Triple-Entry Accounting as a Means of Auditing Large
Language Models"* (JRFM 16(9):383, <https://www.mdpi.com/1911-8074/16/9/383>) applies exactly this to AI: each
model interaction becomes a signed-receipt third entry stored on a **publicly accessed DLT**, so the operation
is auditable and the output's provenance is fixed.

bonsai-notary implements this for a **deterministic** LLM. A served inference produces a receipt whose three
entries are:

| Entry | What it is | In the code |
|---|---|---|
| **1st** | the **model** signs `(modelHash, inputCommit, outputCommit, traceCommit)` | `receipt.sigModel` (`receipts/receipt.py`) |
| **2nd** | the **counterparty** co-signs `(modelHash, inputCommit, outputCommit)` | `receipt.sigCounterparty` |
| **3rd** | the **shared, published record** committing the receipt | the ledger entry **and** the on-chain `OP_RETURN` (this doc) |

(For real deployments the 1st/2nd entries are third-party-verifiable **secp256k1** signatures —
`receipts/signing_ec.py`; the legacy `local-hmac@v1` is a symmetric demo vouch. See
[`RECEIPTS.md`](RECEIPTS.md).)

Two identities meet in a stateful publication but should not be confused. The **model identity** binds the
artifact, inference contract, source-weight digest, and quality gate. The **Ricardian contract** binds
human-readable agent policy to the exact deployment parameters of an on-chain identity. An action joins
them by setting `actionHash` to the inference `receiptHash` and `provenanceHash` to its `modelHash`.

---

## 2. What the third entry commits

The third entry commits the **`receiptHash`** — `sha256(canonical_bytes(receipt body))`, which transitively
binds `modelHash`, `inputCommit`, `outputCommit`, the committed sampler block, and the two signatures. So a
single 32-byte value on-chain pins *which model produced which output under which sampler, vouched by whom*.

On BSV the third entry is a bare, provably-unspendable data output:

```
OP_FALSE OP_RETURN <tag> <modelHash> <receiptHash>          ── on the wire: 006a 0a <tag> 20 <32B> 20 <32B>
                   │       │           └ the inference receipt commitment (the audit anchor)
                   │       └ which model (identity provenance)
                   └ "trinote/r1" — the protocol tag (CHAIN_TAG, receipts/emit.py)
```

`receipts/emit.py::chain_artifact(receipt)` builds the artifact `{schema, tag, modelHash, receiptHash,
samplerMode, seed}`; the wallet turns `(tag, modelHash, receiptHash)` into the `OP_RETURN` script
(`OpReturn().lock([...])`, matching `006a…` — see [`RECEIPTS.md`](RECEIPTS.md) §"on-chain shape").

The wire format is model-agnostic. Bonsai-8B and the deterministic Bonsai-27B Qwen3.5 path publish the
same pair of commitments with different `modelHash` values. The regular `bonsai27` GGUF runner cannot
publish one because its floating-point execution cannot issue a receipt. See
[`../BONSAI-27B.md`](../BONSAI-27B.md).

---

## 3. The two forms of the third entry

The third entry exists in two complementary forms; **both** are produced by `emit_receipt`.

### 3a. Local hash-linked ledger — *tamper-evident*
`receipts/ledger.py` appends each receipt as `entryHash = commit({index, prevHash, receiptHash, modelHash,
ts})`, chained by `prevHash`. `verify_chain` recomputes the chain and localizes any break. This is **always**
written. **Honest limit:** it is a single-holder log — it makes interior tampering *detectable*, not
*impossible* (the holder can re-link the whole chain, or withhold the file), and the symmetric-HMAC variant
gives no third-party proof. It is the third entry's *evidentiary* form, not its *public* form.

### 3b. Public BSV `OP_RETURN` — *shared, immutable* (the canonical third entry)
A 0-sat `OP_FALSE OP_RETURN` output on **BSV mainnet**, committing the `receiptHash`. Once mined it is a
public, miner-ordered record **no single party can rewrite or suppress** — the property the theory makes
definitional of a third entry. This is what turns "we kept a log" into "the receipt is notarized."

> The earlier theory-fidelity review flagged that, by default, the third entry existed *only* as the local
> ledger — so the *notarization* claim was not yet realized. Form 3b closes that gap.

---

## 4. How bonsai-notary produces the public third entry

Emissions come from **this project's own self-managed BSV HD wallet** (`wallet/notary_wallet.py`,
`bsv-sdk` via the uv-managed `.venv_wallet`) — no remote execution, no shared keys:

- **Own mnemonic + deterministic keys** — BIP44 `m/44'/236'/0'`; Elder `…/0/0`, agent `…/0/1`, counterparty
  `…/0/2`, change on the `…/1/*` path (change never reuses the spending key).
- **Pre-split funding** — `notary_wallet.py fanout` fans one funding UTXO into several equal UTXOs at the
  wallet's own receive addresses, so each third entry spends a **distinct, already-confirmed** UTXO and never
  waits on chained-change confirmations.
- **The third entry** — `notary_wallet.py third-entry --model-hash … --receipt-hash …` spends one pre-split
  UTXO into the `OP_RETURN` + change to a derived address, at an **exact 100 sat/KB** fee (computed over an
  upper-bound size, so the realized rate is at/above target — the SDK fee model is *not* trusted).

### Wired into the notary emit path
In the composition the default `--onchain` Third Entry is produced by the byte-exact C port
[`chain_c`](https://github.com/itsmygithubacct/chain_c), driven by the
[`bsv_third_entry`](https://github.com/itsmygithubacct/bsv_third_entry) orchestration
(`ChainCThirdEntryBackend`) and surfaced as `./bonsai-notary … --onchain` / `./bonsai-agent` (see
[`RECEIPTS.md`](RECEIPTS.md)). The standalone `OP_RETURN` can also be landed by this project's own BSV
wallet: `receipts/broadcast.py::WalletThirdEntryBackend` plugs that wallet into `emit_receipt` as a chain
backend. It **subprocesses** the wallet's own venv (so the numpy-only notary runtime never imports
`bsv-sdk`) and returns `{txid, broadcast, status, fee, satPerKb, …}`. Two-key interlock — a real mainnet
send needs **both**:

```python
from trinote.receipts import WalletThirdEntryBackend
from trinote.infer_int.bonsai_runtime import emit_and_verify_bonsai_receipt

bundle, verification, emission = emit_and_verify_bonsai_receipt(
    model, input_ids=ids, output_ids=out, model_digest=digest, sampler=cfg,
    enable_chain=True,                                   # key 1: turn on real publish
    chain_backend=WalletThirdEntryBackend(source_index=21, confirm=True),  # key 2: actually broadcast
)
# emission["chainArtifact"] = {tag, modelHash, receiptHash, …}
# emission["onchain"]       = {txid, broadcast: True, status: "broadcast", …}
```

With `confirm=False` (or `enable_chain=False`) it is a **dry-run** — the tx is built and the txid computed,
but nothing is broadcast. The default emit path remains the network-free local log (`LogBroadcastBackend`).

---

## 5. Verifying a third entry

Anyone, with no secret and no cooperation from the operator, can verify a published third entry:

1. **Read it on-chain.** Fetch the tx; its `OP_RETURN` (`006a…`) decodes to `tag ‖ modelHash ‖ receiptHash`.
2. **Re-derive the receipt.** From the off-chain receipt bundle, recompute `receiptHash =
   sha256(canonical_bytes(body))` and `inputCommit/outputCommit = token_commit(ids)`; they must equal the
   committed values (`receipts/verify.py`, driven by
   `infer_int/bonsai_runtime.py::emit_and_verify_bonsai_receipt`; the composed
   `./bonsai-notary … --receipts` flow performs exactly this re-derivation).
3. **Re-execute.** Re-run the bit-exact integer model over the committed input ids and confirm the output ids
   re-derive (the deterministic-inference contract, [`../architecture/DETERMINISM.md`](../architecture/DETERMINISM.md)).
   Re-execution proves *what the model did*; the on-chain entry proves *that it was recorded publicly and
   cannot be silently removed*.

These three steps are exactly what the engine's **`trinote-receipt-bundle verify`**
(`trinote.cli.receipt_bundle_cli`) automates over a portable bundle: offline (steps 1-2 minus the
network), `--onchain` (step 1), `--reexec` (step 3). Package one with the engine's `trinote-receipt-bundle
pack` from a `./bonsai-notary … --receipts --onchain` emission — standalone, or stateful when anchored
under a deployed `./bonsai-agent` identity. See
[`RECEIPT-BUNDLE.md`](RECEIPT-BUNDLE.md), and [`../identity/AGENT-LIFECYCLE.md`](../identity/AGENT-LIFECYCLE.md)
for running inference under a stateful identity.

---

## 6. Worked example (live on mainnet)

| Step | Txid | What it is |
|---|---|---|
| Fan-out | [`25f2a0fd…`](https://whatsonchain.com/tx/25f2a0fddec19049ca80cc41dbdb55c1c4eb6e311b9135c891070140da7b9a83) | a <1 BSV key fanned into 8 × 50,000-sat UTXOs at bonsai's own derived addresses (idx 20-27) |
| **Third entry** | [`2096e14b…`](https://whatsonchain.com/tx/2096e14b7cbbb623557e0db60cbe594e36cd5c22d33809c7d00a1c1d9df21ebb) | `OP_FALSE OP_RETURN open_lm/r1 ‖ 3dd65635…(modelHash) ‖ 0d3236…(receiptHash)` — a Bonsai-8B inference receipt, notarized publicly |

> ⚠️ **Pre-rename anchor.** This tx predates the `open_lm → trinote` rename, so its immutable OP_RETURN
> carries the old `open_lm/r1` tag and is not re-verifiable against the current `trinote` artifact. A fresh
> third entry carries `trinote/r1 ‖ <modelHash> ‖ <receiptHash>`.

The committed `receiptHash 0d3236…` *was* a genuine Bonsai-8B inference re-executable from the
pre-rename artifacts. Re-running today via `./bonsai-notary --receipts` produces a fresh receipt
bound to the current `e5ae7bd1…` artifact, not this historical one.

### Stateful form (also live on mainnet)

The full `AgentTea` lifecycle, funded from the notary's own Elder key:

| Step | Txid | What it is |
|---|---|---|
| Deploy | [`b17058e1…`](https://whatsonchain.com/tx/b17058e1e739f9c81478f100efc7ce6f7fce9194b6f27a4af432c7a503e92d4f) | the reputation-bearing identity UTXO (commits `ricardianHash`) |
| **executeAction** | [`75755dd6…`](https://whatsonchain.com/tx/75755dd6b8d2494a2c61b81fcaa9694bf0a94133a2aa9ede44fcea6c66b171ee) | the **stateful Third Entry** — receipt `OP_RETURN 006a20 d30b53c1…` |
| Revoke | [`1ae1379b…`](https://whatsonchain.com/tx/1ae1379b80bf1de95851e8da73d7d3890b66f46a7579ce53cedff052721d3777) | Elder kill switch — dissolves the identity |

Here the third entry is a **state transition**, not a bare timestamp. Its OP_RETURN commits
`receiptHash = sha256( ricardianHash ‖ agentPk ‖ counterpartyPk ‖ amount ‖ actionHash ‖ provenanceHash ‖ txCount ‖ lockTime )`,
where `actionHash` is the bonsai inference `receiptHash` and `provenanceHash` is its `modelHash`. This was
**verified by recomputation**: the SHA-256 of those fields equals the on-chain `d30b53c1…caa03` exactly — so the
entry binds the genuine inference receipt into a reputation-bearing identity (here `txCount` 0→1), re-derivable
by anyone holding the receipt fields.

---

## 7. Honest scope

- **What ships and works:** the **standalone** public third entry — a 0-sat `OP_RETURN` committing the
  `receiptHash`, from this project's own wallet, at a controlled fee. This delivers the core theory property
  (a public, immutable, shared third entry that no single party can rewrite).
- **The stateful form is also live** (§6): the **`RicardianTea`/`AgentTea`** lifecycle, where the third entry
  is a state transition of a reputation-bearing **on-chain agent identity** (an identity UTXO with `txCount`,
  in-script **Rabin** attestation, and an Elder-key revocation) — `chain_c` driven by `bsv_third_entry` + [`RECEIPTS.md`](RECEIPTS.md). It
  binds an *identity and reputation* to the receipts, not just a public timestamp; the action's commitment was
  verified by recomputation against mainnet.
- **Trust boundary:** re-execution + the public `OP_RETURN` prove *what the model did and that it was recorded
  publicly*. They do **not** prove the output is *correct*, nor (for the standalone form) that the operator
  recorded *every* inference — that completeness/non-repudiation property is what the stateful identity
  (txCount) adds.
- **Privacy boundary:** the transaction carries commitments and public identity/transaction data, not the
  prompt, output text, receipt preimage, model weights, or private keys. Sharing a receipt bundle can disclose
  its optional plaintext transcript, so inspect it before publication.

## References
- Grigg, *Triple Entry Accounting* (2005) — <https://iang.org/papers/triple_entry.html>
- Grigg, *The Ricardian Contract* (2004) — <https://iang.org/papers/ricardian_contract.html>
- Sgantzos, Al Hemairy, Tzavaras & Stelios, *Triple-Entry Accounting as a Means of Auditing LLMs* (JRFM 2023) — <https://www.mdpi.com/1911-8074/16/9/383>
- This repo: [`RECEIPTS.md`](RECEIPTS.md) · [`../architecture/DETERMINISM.md`](../architecture/DETERMINISM.md) · [`chain_c`](https://github.com/itsmygithubacct/chain_c) · [`bsv_third_entry`](https://github.com/itsmygithubacct/bsv_third_entry) · `wallet/notary_wallet.py` · `receipts/broadcast.py`
