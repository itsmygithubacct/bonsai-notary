# Stateful agent lifecycle — running inference *under* an on-chain identity

The live demo (`tools/launch_bonsai_live.sh`) publishes a **standalone** third entry: each inference lands
an `OP_FALSE OP_RETURN trinote/r1 ‖ modelHash ‖ receiptHash` — a public mark with no identity to carry
forward. This document covers the **stateful** path: a persistent on-chain **AgentTea** identity that is
deployed once and then accrues a tamper-evident, ordered history, with each inference's `receiptHash`
committed as a state-advancing `executeAction`. The inference is then provably done *by this identity*, in
order, counted by `txCount`.

```
bonsai-agent  deploy  --ricardian-hash H [--elder-key-file …] [--confirm]   # deploy the identity ONCE
bonsai-notary "prompt" --receipts --onchain [--chain-confirm]               # inference → receipt → action under it
bonsai-agent  action  --action-hash R --provenance-hash M [--amount N] [--confirm]   # bind already-computed hashes
bonsai-agent  recover --new-agent-key-file F [--recover-sigs-file S] [--confirm]      # M-of-3 social recovery: rotate the agent key (see RECOVERY.md)
bonsai-agent  status
bonsai-agent  revoke  [--confirm]
```

The resumable *tip* is a state file at `--state-file` (default
`$BONSAI_NOTARY_HOME/agent/identity.state.json`). Theory and the standalone form:
[`THIRD-ENTRY.md`](../receipts/THIRD-ENTRY.md). The on-chain contract: `chain_c`'s
`src/contracts_next/agent_tea.c`. The resumable lifecycle driver: `chain_c`'s `agentd` CLI
(`src/scripts/agentd_lib.c`), driven by `bsv_third_entry`.

---

## The identity and its state

An AgentTea identity is a 1-sat UTXO whose locking script binds the **charter** (`ricardianHash =
H(prose ‖ params)`), the **Elder** key (the human kill-switch), the **agent** key (First Entry signer),
the spend envelope (`perTxLimit`, `dailyLimit`, `windowDuration`), and the mutable state (`txCount`,
`spentInWindow`, `windowStart`, `tier`, `recoveryCount`). Every action **spends** the identity UTXO and
**recreates** it at output 0 with advanced state — so the identity *is* its reconciled on-chain history.

`bonsai-agent`/`agentd` keep a small **state file** (`--state-file`) — the resumable *tip*: the latest raw
tx + outpoint, plus the charter/keys/params for auditing. It contains **no private keys**. Each operation
reconstructs the live contract instance from the tip (`chain_c`'s `agentd` faithfully ports
`AgentTea.fromTx(rawTx, vout)`), so a single identity can be actioned across many separate invocations.

---

## `run` — inference under the identity

`./bonsai-notary "<prompt>" --receipts --onchain` (after the identity is deployed once):

1. runs the Bonsai inference locally and builds + verifies its receipt (identical to a standalone run,
   chain emission off);
2. binds **`actionHash = receiptHash`** and **`provenanceHash = modelHash`** into an `executeAction`,
   which advances the identity (`txCount++`, window metering) and commits the Third Entry;
3. packages a **stateful** receipt bundle (via the engine's `trinote-receipt-bundle pack`) you can hand to
   anyone for offline + on-chain verification (see [`RECEIPT-BUNDLE.md`](../receipts/RECEIPT-BUNDLE.md)). The action's full raw transaction
   is also appended to the off-chain transaction log (`--tx-log`, default `artifacts/receipts/transactions.log`)
   and carried in the bundle (`onchain.rawTx`), so the third entry is re-broadcastable and offline-checkable
   (`txid == hash256(rawTx)`).

### The on-chain Third Entry (stateful)

The `executeAction` OP_RETURN commits a single 32-byte hash over the action's eight fields — **not** the
raw `(modelHash, receiptHash)`:

```
receiptHash_onchain = sha256(
    ricardianHash(32) ‖ agent(33) ‖ counterparty(33) ‖ int2ByteString(amount, 8)
    ‖ actionHash(32) ‖ provenanceHash(32) ‖ int2ByteString(txCount, 8) ‖ int2ByteString(now, 4) )
```

`txCount` is the **pre-increment** counter. `actionHash`/`provenanceHash` are the inference's
`receiptHash`/`modelHash`. The verifier recomputes this hash from the bundle's recorded fields and matches
it to the on-chain OP_RETURN, then asserts the two bindings — tying the public mark to the exact inference.
The Python recompute (`trinote.bundle.agent_action_receipt_hash`) is checked byte-for-byte against the
scrypt-ts encoding by a golden-vector test.

---

## Safety — the two-key broadcast interlock

Every operation is **dry-run by default**. A real mainnet broadcast needs **both**:

1. a funded signing key (Elder / agent / counterparty / funding keyfiles), and
2. an explicit `--confirm` (→ `CONFIRM_MAINNET_BROADCAST=yes` for `agentd`).

Without `--confirm`, `deploy`/`action`/`revoke` print the plan and **change nothing** — no broadcast, no
state-file mutation. This is the same interlock the wallet and chain scripts use (see
[`../../SECURITY.md`](../../SECURITY.md)). Keys live under `$BONSAI_NOTARY_HOME` and are referenced by
**path** (env vars), never passed on argv; the state file is written `0600` and holds no secrets.

---

## A full lifecycle (mainnet — real BSV; omit `--confirm` to rehearse)

```bash
# materialize role keys (written under $BONSAI_NOTARY_HOME, never the repo)
.venv_wallet/bin/python wallet/notary_wallet.py keyfile --role elder
# … fund the Elder/funding addresses, then:

bonsai-agent  deploy --elder-key-file ~/.local/trinote/wallet/keys/<elder>.json \
    --ricardian-hash <charter hash> --confirm
bonsai-notary "summarize the changelog" --receipts --onchain --chain-confirm   # inference → executeAction under the identity
bonsai-agent  status
bonsai-agent  revoke --confirm
```

These use the default state file (`$BONSAI_NOTARY_HOME/agent/identity.state.json`); pass `--state-file`
to the `bonsai-agent` subcommands to keep several identities side by side. Each on-chain run appends one
`executeAction` to the identity's history and emits a verifiable stateful bundle.
`revoke` is the Elder kill-switch: it dissolves the identity, after which no further actions are possible.

### Status, recovery, and the rest of the contract

`status` reports the persisted tip (`txCount`, `genesisTxid`, `ricardianHash`). Beyond the core
`deploy`/`action`/`revoke`/`status` loop, `agentd` now also wires **`recover`** (selector 3) — **M-of-3
guardian social recovery** that rotates the agent key: it spends the identity tip and recreates it with
`agent → newAgent`, `recoveryCount += 1`, and `txCount`/`spentInWindow`/`windowStart`/`tier` carried
forward unchanged. A guardian quorum (default `recoveryThreshold = 2` of 3) authorizes the rotation by
signing the new agent key and the current `recoveryCount`. See [`RECOVERY.md`](RECOVERY.md) for the full
flow, the hybrid guardian-signature model (external offline guardians vs. the custodial opt-in), the env
contract, and the **testnet-first** caveat (the `recover` script has no byte-exact golden vector yet).

The `bonsai-agent` / `bsv_third_entry` wrappers expose `deploy`/`action`/`revoke`/`status`; `recover` is
driven directly through the `agentd` binary they compose (env contract in [`RECOVERY.md`](RECOVERY.md)).
The remaining contract methods (`stake`, `slashValidator`) are covered by the adversarial suite in
`chain_c/tests/test_agent_tea.c` and reachable through the contract layer.
