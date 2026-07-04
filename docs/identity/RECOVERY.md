# Social recovery — rotating an AgentTea agent key with `recover`

`recover` is the **M-of-3 guardian social-recovery** path for an AgentTea identity: it lets a quorum of
guardians **rotate the agent key** without the old key and without disturbing the rest of the identity.
Use it when the agent key is lost, leaked, or being retired. It is a first-class `agentd` subcommand
(selector index 3), the sibling of `deploy`/`action`/`revoke`/`status` covered in
[`AGENT-LIFECYCLE.md`](AGENT-LIFECYCLE.md). The on-chain contract is `chain_c`'s
`src/contracts_next/agent_tea.c`; the lifecycle driver is `chain_c`'s `agentd`
(`src/scripts/agentd_lib.c`).

> **Validate on testnet first.** Unlike `deploy`/`action`/`revoke`/`executeTea`, the `recover`
> unlocking script has **no byte-exact golden vector yet**. The implementation uses only
> golden-verified push primitives in the correct ABI order and is unit-tested for framing, but the
> contract-level acceptance (guardian Rabin sigs verify *and* `hashOutputs` matches the recreated
> identity) can only be fully confirmed by a real spend. **Run `recover` on testnet (`NETWORK=test`)
> and confirm the spend before any mainnet use.** See [Caveat](#caveat--no-golden-vector-yet-testnet-first).

---

## What `recover` does on-chain

Like every AgentTea operation, `recover` **spends** the identity tip and **recreates** it at output 0 with
advanced state. It rotates the agent key and bumps the recovery nonce, carrying everything else forward:

| field | after `recover` |
| --- | --- |
| `agent` | **→ `newAgent`** (the rotated-to key from `NEW_AGENT_KEY_FILE`) |
| `recoveryCount` | **`+= 1`** (anti-replay nonce; incremented *after* the guardian-sig check) |
| `txCount`, `spentInWindow`, `windowStart`, `tier` | carried forward **unchanged** |
| `ricardianHash`, Elder, guardians, envelope | unchanged |
| identity value | unchanged (still the 1-sat identity UTXO) |

The transaction lays out three outputs:

```
output[0]  recreated identity        recreated locking script, agent = newAgent, recoveryCount+1
output[1]  OP_FALSE OP_RETURN <h>     the 32-byte recovery receipt (0 sat), see below
output[2]  change                     → the Elder address (or CHANGE_ADDRESS), funds the fee
```

### The recovery receipt (output[1])

```
recoveryReceipt = sha256(
    "AGNT_RECOVER_V1" ‖ ricardianHash(32) ‖ newAgent(33)
    ‖ int2ByteString(recoveryCount, 8) ‖ int2ByteString(txCount, 8) )
```

`recoveryCount` and `txCount` are committed at their **current (pre-increment)** values — a public,
ordered mark that *this* identity rotated to *this* new agent key at *this* point in its history.

### What the guardians sign

Authorization is **M-of-3 guardian Rabin signatures** over a separate `recoveryMsg` — note it binds the
**pre-increment** `recoveryCount` and does **not** include `txCount`:

```
recoveryMsg = "AGNT_RECOVER_V1" ‖ ricardianHash(32) ‖ newAgent(33)
            ‖ int2ByteString(recoveryCount, 8)        # the CURRENT, pre-increment count
```

The contract verifies at least `recoveryThreshold` of the 3 guardian signatures over this message before
accepting the rotation; `agentd` enforces the same threshold up front so a doomed spend never broadcasts.
`recoveryThreshold` **defaults to 2 of 3** (set at `deploy`). Because guardians sign the new agent key and
the current `recoveryCount`, a quorum's signature authorizes exactly one rotation to one named key and
cannot be replayed against a later state.

---

## Authorization — the hybrid guardian model

`recover` obtains its M-of-3 guardian signatures one of two ways. Pick the mode at **`deploy`** time.

### (a) External guardians — true social recovery

The guardians each hold **their own** Rabin key and sign **offline**. You collect their signatures into a
3-line file and point `RECOVER_SIGS_FILE` at it — one line per guardian, in guardian order:

```
<used 0|1> <s_hex|-> <paddingByteCount>
```

* `used` — `1` if this guardian contributed a signature, `0` to skip the slot.
* `s_hex` — the Rabin signature value `s` as hex (or `-` when `used` is `0`).
* `paddingByteCount` — the Rabin padding byte count for that signature.

Each signature must be over `recoveryMsg(ricardianHash, NEW agent pubkey, CURRENT recoveryCount)` — i.e.
the guardians sign the **new** agent key they are authorizing and the **pre-increment** count. At least
`recoveryThreshold` lines must have `used = 1`. This is the production trust model: no single party can
rotate the key.

### (b) Custodial opt-in — self-contained / testing only

Run **`deploy`** with `AGENTD_PERSIST_RECOVERY_KEYS=yes` and it persists the 3 guardian Rabin private
factors to `<STATE_FILE>.recovery_keys` (mode `0600`, one `p_dec:q_dec` line per guardian). `recover` then
**self-signs** the quorum from that store — no external guardians, no `RECOVER_SIGS_FILE` needed.

> **Security trade-off — read this.** In custodial mode **`agentd` holds all three guardian keys**, so a
> single operator with the state directory can rotate the agent key unilaterally. That **defeats the
> social-recovery guarantee** and is intended only for self-contained demos and testnet validation —
> **not** the production trust model. The **default `deploy` does NOT persist** the guardian keys: it
> generates the three keypairs, hands out the public moduli, and **discards the private factors**, so
> recovery genuinely requires the external guardians to sign (mode (a)).

`agentd` cross-checks each stored key against the on-chain guardian pubkey before self-signing and refuses
a stale store, so a custodial `recover` cannot silently sign with the wrong keys.

---

## Running `recover`

`recover` is a first-class subcommand of the `bonsai-agent` / `bsv_third_entry` launchers
(`bonsai-agent recover --new-agent-key-file F [--recover-sigs-file S] [--fund-key-file K] [--confirm]`),
exactly like `deploy`/`action`/`revoke`. It can also be driven **directly** at the underlying `agentd`
C CLI (built to `chain_c/build/agentd`, run from inside the chain_c checkout so it resolves the committed
AgentTea artifact) with the raw env contract below — the wrapper just sets these env vars for you.

### Environment

| variable | meaning |
| --- | --- |
| `STATE_FILE` (required) | the identity tip to recover (e.g. `$BONSAI_NOTARY_HOME/agent/identity.state.json`) |
| `NEW_AGENT_KEY_FILE` (required) | the rotated-to agent `{wif,address}` keyfile — the key the identity will carry afterward |
| `RECOVER_SIGS_FILE` | external guardian signatures (mode (a)); omit when using the custodial store (mode (b)) |
| `FUND_RECOVER_KEY_FILE` | funds the fee; **defaults to `FUND_ACTION_KEY_FILE`** if unset |
| `CHANGE_ADDRESS` | change sink; **defaults to the Elder address** |
| `NETWORK` | `main` or `test` (use `test` for the required testnet validation) |
| `CONFIRM_MAINNET_BROADCAST` | `yes` to actually broadcast; anything else ⇒ DRY-RUN |
| `AGENTD_PERSIST_RECOVERY_KEYS` | set to `yes` **at `deploy`** (not at `recover`) to enable custodial self-sign |

Keys are referenced by **path**, never passed on argv; the state file and the `.recovery_keys` store are
written `0600`. **Do not print private keys.**

### The two-key broadcast interlock

`recover` is **dry-run by default** — it prints the plan (`recoveryCount`, threshold, guardian count) and
**changes nothing**: no broadcast, no state-file mutation. A real broadcast needs **both** factors, the
same interlock the rest of the lifecycle uses (see [`AGENT-LIFECYCLE.md`](AGENT-LIFECYCLE.md) and
[`../../SECURITY.md`](../../SECURITY.md)):

1. a funded signing key (`FUND_RECOVER_KEY_FILE` / `FUND_ACTION_KEY_FILE`) **and** the guardian quorum, and
2. an explicit `CONFIRM_MAINNET_BROADCAST=yes`.

Through the `bonsai-agent` wrapper that second factor is exactly what `--confirm` sets; driving `agentd`
directly you set `CONFIRM_MAINNET_BROADCAST=yes` yourself. The launcher's auto-funding (`fund-key` →
`FUND_RECOVER_KEY_FILE`) and fresh-change (`next-change` → `CHANGE_ADDRESS`) hygiene applies to a
`--confirm` `bonsai-agent recover` just as it does to `action`; driving `agentd` directly you supply
`FUND_RECOVER_KEY_FILE`/`FUND_ACTION_KEY_FILE` and (optionally) `CHANGE_ADDRESS` yourself.

### Example — testnet validation, then mainnet

Through the wrapper (recommended — it sets the env, and on `--confirm` self-funds + rotates change):

```bash
# 1) DRY RUN — print the plan, broadcast nothing (no --confirm)
bonsai-agent recover \
  --state-file "$BONSAI_NOTARY_HOME/agent/identity.state.json" \
  --new-agent-key-file "$BONSAI_NOTARY_HOME/wallet/keys/<new-agent>.json" \
  --recover-sigs-file ./guardian-sigs.txt

# 2) LIVE on TESTNET — required before any mainnet recover
bonsai-agent recover --network test \
  --state-file "…/identity.state.json" \
  --new-agent-key-file "…/<new-agent>.json" \
  --recover-sigs-file ./guardian-sigs.txt \
  --confirm                   # → CONFIRM_MAINNET_BROADCAST=yes; prints "RECOVER broadcast: <txid>"
```

Equivalently, at the raw `agentd` layer (you set the env + funding yourself):

```bash
cd "$BONSAI_CHAIN_C_DIR"     # the chain_c checkout, so agentd finds the committed AgentTea artifact
STATE_FILE="…/identity.state.json" \
NEW_AGENT_KEY_FILE="…/<new-agent>.json" \
RECOVER_SIGS_FILE=./guardian-sigs.txt \
FUND_RECOVER_KEY_FILE="…/<funder>.json" \
NETWORK=test \
CONFIRM_MAINNET_BROADCAST=yes \
  build/agentd recover
```

For the **custodial** self-signed flow (deploy was run with `AGENTD_PERSIST_RECOVERY_KEYS=yes`), drop
`RECOVER_SIGS_FILE`/`--recover-sigs-file` — `agentd` self-signs the quorum from `<STATE_FILE>.recovery_keys`.

On a successful broadcast `agentd` records the txid to the broadcast journal, then rotates the state file:
`agent → newAgent`, `recoveryCount += 1`, new tip outpoint, and a `recover` history entry.

---

## After recovery — use the new agent key

The identity now expects the **new** agent key. For every subsequent `action`, point
`AGENT_KEY_FILE` at the keyfile you passed as `NEW_AGENT_KEY_FILE`:

```bash
bonsai-agent action --action-hash R --provenance-hash M \
    --agent-key-file "$BONSAI_NOTARY_HOME/wallet/keys/<new-agent>.json" --confirm
```

The old agent key can no longer sign actions for this identity. The Elder kill-switch (`revoke`) and the
charter (`ricardianHash`) are unchanged by recovery.

---

## Caveat — no golden vector yet; testnet first

The byte-exact golden vectors cover `deploy`/`action`/`revoke`/`executeTea`, **not** `recover`. The
`recover` unlocking script is built from golden-verified push primitives in the correct ABI order and is
unit-tested for framing, but **contract acceptance is not yet pinned by a golden vector** — only a real
spend exercises "guardian Rabin sigs verify **and** `hashOutputs` matches the recreated identity" end to
end. **Always validate a `recover` on testnet (`NETWORK=test`) and see it confirm before running it on
mainnet.** A failed mainnet `recover` wastes the fee and leaves the identity on its old agent key; a
testnet dry run + confirmed spend de-risks both.
