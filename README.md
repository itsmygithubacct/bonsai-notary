# bonsai-notary (composed)

A deterministic-inference **notary**: it runs a Bonsai/BitNet model through a byte-exact integer
inference engine, emits a cryptographic triple-entry **receipt** for every generation, and anchors
the **Third Entry** on Bitcoin SV — so a third party can re-execute the run, get the same bytes, and
verify what the model produced.

This repository is the **composition layer**. It is deliberately thin: the heavy lifting lives in
three independently-versioned projects that it wires together at run time.

```
              ┌─────────────────────────── bonsai-notary (this repo) ───────────────────────────┐
              │  wallet/ · launchers (bonsai-notary, bonsai-agent, scripts/bonsai.sh) · docs/    │
              │  artifacts/identity · requirements · .env                                        │
              └──────────────┬──────────────────┬───────────────────────┬──────────────────────┘
                  symlink     │      symlink      │        symlink         │
            ┌─────────────────▼───┐  ┌────────────▼─────────┐  ┌──────────▼───────────────┐
            │ engine/             │  │ chain_c/             │  │ bsv_third_entry/         │
            │ ~/integer_inference │  │ ~/chain_c            │  │ ~/bsv_third_entry        │
            │  _engine            │  │                      │  │                          │
            │ deterministic int   │  │ byte-exact C port of │  │ Python on-chain orch:    │
            │ inference (trinote) │  │ the BSV chain layer  │  │ drives chain_c CLIs as   │
            │ + receipts          │  │ (bonsai_third_entry, │  │ the receipt's Third      │
            │                     │  │  agentd, woc, …)     │  │ Entry / agent lifecycle  │
            └─────────────────────┘  └──────────────────────┘  └──────────────────────────┘
```

| Piece | Repo | Role | In here as |
|---|---|---|---|
| **Inference engine** | `~/integer_inference_engine` | byte-exact integer generation (`trinote`) + builds/verifies the receipt | `engine/` (symlink) |
| **On-chain software** | `~/chain_c` | the C CLIs that build/sign/broadcast BSV txs (`bonsai_third_entry`, `agentd`, `woc`) | `chain_c/` (symlink) |
| **On-chain orchestration** | `~/bsv_third_entry` | the Python layer that drives chain_c to publish the Third Entry / run the agent lifecycle | `bsv_third_entry/` (symlink) |
| **Notary glue** | *this repo* | wallet, launchers, docs, model identity | — |

## Get it

```bash
git clone https://github.com/itsmygithubacct/bonsai-notary.git
cd bonsai-notary
./scripts/bootstrap-deps.sh        # clone the 3 sibling repos next to this one + wire the symlinks
```

`bootstrap-deps.sh` clones [`integer_inference_engine`](https://github.com/itsmygithubacct/integer_inference_engine),
[`chain_c`](https://github.com/itsmygithubacct/chain_c), and
[`bsv_third_entry`](https://github.com/itsmygithubacct/bsv_third_entry) into the parent directory and
links them in as `engine/`, `chain_c/`, `bsv_third_entry/`. It is idempotent — re-run any time, and
`BONSAI_DEPS_UPDATE=1 ./scripts/bootstrap-deps.sh` fast-forwards the checkouts. Already have them?
Point `BONSAI_ENGINE_DIR` / `BONSAI_CHAIN_C_DIR` / `BONSAI_BSV_TE_DIR` at your own checkouts instead.
Then follow **`INSTALL.md`** (build chain_c, create the engine venv + native kernel, fetch weights).

## What changed from `bonsai-notarized-bitnet`

This is an extraction/recomposition of `bonsai-notarized-bitnet`, with two substitutions:

* **inference** is the standalone `~/integer_inference_engine` (not the in-repo `src/trinote`), and
* **on-chain** is `~/chain_c` driven by `~/bsv_third_entry` (not the vendored TypeScript `chain/`).

The engine's `--onchain` publish step normally constructs an `trinote` `WalletThirdEntryBackend`
(Python BSV wallet) or shells the TS `chain/`. Here, `bsv_third_entry.engine_run` rebinds that one
name to `ChainCThirdEntryBackend`, so `--onchain` publishes through `chain_c/build/bonsai_third_entry`
— **with no change to the engine source**.

## Run

Prereqs (see `INSTALL.md`): a uv venv for the engine with `requirements_notary.txt` installed, a
built `chain_c` (`bash chain_c/build_chain_c.sh`), the CPU kernel built,
and the model weights fetched **into the state home** with `./scripts/fetch_weights.sh` (it downloads
into `$BONSAI_NOTARY_HOME/models`, or reuses a verified local checkout via `BONSAI_WEIGHTS_REPO`).

```bash
# one-shot completion (deterministic integer engine, model output only)
./bonsai-notary "What is a tensor?"

# notarized: emit + verify a local receipt
./bonsai-notary "Explain Merkle proofs." --receipts -n 128

# notarized + on-chain Third Entry via chain_c — DRY-RUN (builds the tx, does not broadcast)
#   the on-chain step is a RESUMABLE agentd action under a persisted identity: deploy it once first
./bonsai-agent deploy --confirm                                         # one-time (spends BSV)
./bonsai-notary "Notarize this." --receipts --onchain

# …and actually broadcast the Third Entry (spends real BSV; needs the deployed identity)
./bonsai-notary "Notarize this." --receipts --onchain --chain-confirm

# curated modes
./scripts/bonsai.sh receipted "What is the capital of France?" -n 64
./scripts/bonsai.sh onchain   "Notarize this."                          # dry-run unless --chain-confirm
./scripts/bonsai.sh repl

# resumable on-chain agent identity (driven through chain_c/build/agentd)
./bonsai-agent status
./bonsai-agent deploy --ricardian-hash <64hex>                          # DRY-RUN unless --confirm
./bonsai-agent action --action-hash <receiptHash> --provenance-hash <modelHash>
```

## State & secrets — outside the repo

Nothing a run produces is written into the checked-out tree. The receipt ledger, signing keys,
packaged bundles, AND the chain_c key files all live under **one shared home**,
`$BONSAI_NOTARY_HOME` (default `~/.local/trinote`). The launchers export it so the engine and chain_c
agree on one home and reuse the same keys. **Never copy or ship that directory.**

> Note: the state/secrets home (`~/.local/trinote`) lives OUTSIDE this repo — never copy or ship it.

## The two-key interlock

On-chain broadcasts are **DRY-RUN by default**. A real spend needs **both** `--chain-confirm` /
`--confirm` at the launcher **and** the chain_c binary's own `CONFIRM_MAINNET_BROADCAST=yes` gate
(`bsv_third_entry` sets the latter only when the former is given). Commitment hashes are validated as
32-byte hex before they reach the chain. See `SECURITY.md`.

## Configuration (env)

| Var | Meaning | Default |
|---|---|---|
| `BONSAI_NOTARY_HOME` | shared state/secrets home | `~/.local/trinote` |
| `BONSAI_ENGINE_DIR` | inference-engine checkout | `./engine` |
| `BONSAI_CHAIN_C_DIR` | chain_c checkout | `./chain_c` |
| `BONSAI_BSV_TE_DIR` | bsv_third_entry checkout | `./bsv_third_entry` |
| `BONSAI_MODELS_DIR` | weights location (in the state home) | `$BONSAI_NOTARY_HOME/models` |
| `BONSAI_WEIGHTS_REPO` | local checkout `scripts/fetch_weights.sh` reuses weights from | `~/bonsai-notarized-bitnet` |
| `BONSAI_GPU` | `1` use GPU producer, `0` force CPU | `1` |
| `BONSAI_DRYRUN` | print the resolved command, don't run | `0` |

## Composition (how the three siblings are wired)

`engine`, `chain_c`, and `bsv_third_entry` are **symlinks** to sibling checkouts of the three
dependency repos — they're `.gitignore`d and never committed. `./scripts/bootstrap-deps.sh` creates
them for you by cloning the siblings from GitHub. Every launcher resolves through `./engine`,
`./chain_c`, `./bsv_third_entry`, and each is env-overridable (`BONSAI_ENGINE_DIR`,
`BONSAI_CHAIN_C_DIR`, `BONSAI_BSV_TE_DIR`) — so you can instead point at your own checkouts, or add
them as **git submodules** at the same paths, with nothing else to change.

## Docs

* `docs/architecture/COMPOSED-ARCHITECTURE.md` — how the four pieces fit and where the seams are.
* `INSTALL.md` — prerequisites and setup.
* `SECURITY.md` — the security model, secret handling, and the broadcast interlock.
* `docs/receipts/` — the triple-entry receipt + bundle design (from the engine).
