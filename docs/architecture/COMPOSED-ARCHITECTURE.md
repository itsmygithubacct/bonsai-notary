# Composed architecture

`bonsai-notary` is a **composition of four projects**. This note explains the seams: what each piece
owns, how data flows through a notarized generation, and exactly where the on-chain backend is swapped
in so the engine source is never forked.

## The four pieces

| # | Piece | Repo / path | Language | Owns |
|---|---|---|---|---|
| 1 | Inference engine | `~/integer_inference_engine` → `engine/` | Python (NumPy) + C/CUDA kernels | byte-exact integer generation (`trinote`), receipt build/verify, bundle pack/verify |
| 2 | On-chain software | `~/chain_c` → `chain_c/` | C | build/sign/broadcast BSV txs; AgentTea/RicardianTea contracts; WhatsOnChain client |
| 3 | On-chain orchestration | `~/bsv_third_entry` → `bsv_third_entry/` | Python (stdlib) | drive the chain_c CLIs to publish the Third Entry + run the agent lifecycle |
| 4 | Notary glue | this repo | bash + Python | wallet, launchers, model identity, docs, shared-home wiring |

Pieces 1–3 are independently versioned and consumed by reference (symlinks today; git submodules /
fetch later). Piece 4 is the only thing unique to this repo.

## Data flow — one `--onchain` generation

```
  prompt
    │
    ▼  (4) bonsai-notary launcher: unify $BONSAI_NOTARY_HOME, resolve weights, set PYTHONPATH
    ▼      exec: python -m bsv_third_entry.engine_run --fast --receipt --onchain ...
    │
    ▼  (3) engine_run imports trinote.cli.run_bonsai_cli and REBINDS
    │        run_bonsai_cli.WalletThirdEntryBackend  ->  ChainCThirdEntryBackend
    │
    ▼  (1) run_bonsai_cli: tokenize → integer forward (engine kernels) → sample
    │        → build receipt (modelHash, inputCommit, outputCommit, traceCommit)
    │        → re-execute on the pure-int oracle and VERIFY (byte-exact)
    │        → emit_receipt(..., enable_chain=True, chain_backend=<the rebound backend>)
    │
    ▼  (3) ChainCThirdEntryBackend.broadcast(chain_artifact)  [resumable, default]:
    │        ACTION_HASH=receiptHash, PROVENANCE_HASH=modelHash, AMOUNT, STATE_FILE,
    │        CONFIRM_MAINNET_BROADCAST=yes  (only if --chain-confirm)
    │        exec ↓   (one metered action under the pre-deployed identity)
    ▼  (2) chain_c/build/agentd action  (cwd=chain_c):
    │        read STATE_FILE → build executeAction spend under the identity
    │        DRY-RUN: print the action plan   |   LIVE: fund → sign → broadcast → update STATE_FILE
    │        (mode="oneshot" instead execs bonsai_third_entry: a self-contained deploy→action→revoke)
    │
    ▼  back up the stack: status (dry-run|broadcast) + txid → receipt ledger + tx log → bundle
    ▼  $BONSAI_NOTARY_HOME/{receipts,bundles}   (never inside any repo)
```

## The on-chain seam (why the engine is never forked)

`trinote.cli.run_bonsai_cli` constructs the `--onchain` publisher inline:

```python
chain_backend = (WalletThirdEntryBackend(...) if args.onchain else None)   # engine source
```

The name `WalletThirdEntryBackend` is a module global of `run_bonsai_cli`. `bsv_third_entry.engine_run`
imports the module and rebinds that one attribute **before** calling `main()`:

```python
import trinote.cli.run_bonsai_cli as rbc
rbc.WalletThirdEntryBackend = ChainCThirdEntryBackend   # redirect --onchain to chain_c
rbc.main()
```

`ChainCThirdEntryBackend` is a true drop-in: it accepts the same constructor kwargs the engine passes
(`source_index`, `sat_per_kb`, `confirm`, `change_to_source`, `allow_unconfirmed`) and implements the
same `broadcast(artifact, ts=...) -> dict` contract — only the *transport* changes (chain_c CLI instead
of the Python BSV wallet). Because the seam is a single module attribute, an engine
upgrade that keeps that name needs no change here; one that renames it fails loudly in `engine_run`.

## The shared state home

Both subsystems are pointed at one `$BONSAI_NOTARY_HOME` (default `~/.local/trinote`):

* the **engine** writes the receipt ledger, signing keys, sessions, and bundles there;
* **chain_c** reads its key files from `$BONSAI_NOTARY_HOME/chain/*.json` (Elder / funding / agent keys).

The launchers export `BONSAI_NOTARY_HOME` so the engine's own default (`~/.local/integer_inference_engine/…`)
does not split state away from where the keys live. Nothing is ever written into a repo tree, so an
`rsync` of any repo never drags receipts, bundles, or secrets along.

## On-chain flavours — resumable by default

| Flavour | Engine / launcher path | bsv_third_entry | chain_c CLI |
|---|---|---|---|
| **Resumable Third Entry** (default) — one metered action under a persisted identity | `run_bonsai_cli --onchain` | `ChainCThirdEntryBackend` (resumable) | `agentd action` |
| Deploy / manage that identity | `bonsai-agent {deploy,action,revoke,status}` | `ChainCAgentd` | `agentd` |
| One-shot Third Entry (self-contained, ephemeral keys) | `--onchain` with `BONSAI_THIRD_ENTRY_MODE=oneshot` | `ChainCThirdEntryBackend(mode="oneshot")` | `bonsai_third_entry` |

All DRY-RUN by default; all share the two-key interlock. **Resumable requires a one-time deploy**
(`bonsai-agent deploy --confirm`) — until then a resumable `--onchain` DRY-RUN reports `identity:
absent` with a deploy hint, and a real broadcast fails closed rather than spending.

## Migration: symlinks → GitHub

`engine`, `chain_c`, `bsv_third_entry` are `.gitignore`d symlinks to absolute home paths. To publish:

1. Push each sibling to its own GitHub repo.
2. Replace each symlink with a git submodule (or an `INSTALL.md` clone step) at the **same path**.
3. Nothing else changes — every launcher resolves through `./engine`, `./chain_c`, `./bsv_third_entry`
   (each with an env override), and the engine/chain are pinned by their own versions.
