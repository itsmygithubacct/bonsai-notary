# bonsai-notary (composed)

A deterministic-inference **notary**: it runs a Bonsai/BitNet model through a byte-exact integer
inference engine, can emit a cryptographic triple-entry **receipt**, and can anchor the **Third Entry**
on Bitcoin SV — so a third party can re-execute a notarized run, get the same bytes, and verify what
the model produced. Receipts are explicit (`--receipts`); network publication is opt-in and dry-runs
unless separately confirmed.

This repository is the **composition layer**. It is deliberately thin: the heavy lifting lives in
three independently-versioned projects that it wires together at run time.

```
                            bonsai-notary
                                  |
              +-------------------+-------------------+
              |                   |                   |
          engine/             chain_c/        bsv_third_entry/
       integer inference    BSV transaction      Third Entry
       + receipt proofs       construction       orchestration
```

| Piece | Repo | Role | In here as |
|---|---|---|---|
| **Inference engine** | [`integer_inference_engine`](https://github.com/itsmygithubacct/integer_inference_engine) | byte-exact integer generation (`trinote`) + builds/verifies the receipt | `engine/` (symlink) |
| **On-chain software** | [`chain_c`](https://github.com/itsmygithubacct/chain_c) | the C CLIs that build/sign/broadcast BSV txs (`bonsai_third_entry`, `agentd`, `woc`) | `chain_c/` (symlink) |
| **On-chain orchestration** | [`bsv_third_entry`](https://github.com/itsmygithubacct/bsv_third_entry) | the Python layer that drives chain_c to publish the Third Entry / run the agent lifecycle | `bsv_third_entry/` (symlink) |
| **Notary glue** | *this repo* | wallet, launchers, docs, model identity | — |

## Get it

For a receipt-capable Bonsai-27B agent on a fresh Linux host, use the all-in-one setup:

```bash
git clone https://github.com/itsmygithubacct/bonsai-notary.git
cd bonsai-notary
./scripts/setup-bonsai-27b.sh       # interactive; no blockchain broadcast by default
```

The [Bonsai-27B setup guide](docs/SETUP-BONSAI-27B.md) lists the required hardware and information,
key or mnemonic choices, public-Third-Entry funding check, unattended flags, and post-install commands.

For a manual or non-27B composition install, wire only the sibling repositories first:

```bash
./scripts/bootstrap-deps.sh        # clone the 3 sibling repos next to this one + wire the symlinks
```

`bootstrap-deps.sh` clones [`integer_inference_engine`](https://github.com/itsmygithubacct/integer_inference_engine),
[`chain_c`](https://github.com/itsmygithubacct/chain_c), and
[`bsv_third_entry`](https://github.com/itsmygithubacct/bsv_third_entry) into the parent directory and
links them in as `engine/`, `chain_c/`, `bsv_third_entry/`. Every clone is checked out at the full commit
recorded in [`dependencies.lock`](dependencies.lock), so separate hosts install the same tested composition.
It is idempotent; after updating this notary checkout, `BONSAI_DEPS_UPDATE=1 ./scripts/bootstrap-deps.sh`
moves clean dependency trees to the newly locked commits. Dirty or mismatched trees fail closed. Already have them?
Point `BONSAI_ENGINE_DIR` / `BONSAI_CHAIN_C_DIR` / `BONSAI_BSV_TE_DIR` at your own checkouts instead.
Then follow **`INSTALL.md`** (build chain_c, create the engine venv + native kernel, fetch weights).

## How `--onchain` is wired

The engine's `--onchain` publish step constructs a `trinote` `WalletThirdEntryBackend` (Python BSV
wallet) by default. Here, `bsv_third_entry.engine_run` rebinds that one name to
`ChainCThirdEntryBackend`, so `--onchain` publishes through `chain_c/build/bonsai_third_entry` —
**with no change to the engine source**.

## Run

Prereqs (see `INSTALL.md`): a uv venv for the engine with `requirements_notary.txt` installed, a
built `chain_c` (`bash chain_c/build_chain_c.sh`), the CPU kernel built,
and the model weights fetched **into the state home** with `./scripts/fetch_weights.sh` (it downloads
into `$BONSAI_NOTARY_HOME/models`, or reuses a verified local checkout via `BONSAI_WEIGHTS_REPO`).

```bash
# one-shot completion (deterministic integer engine, model output only)
./bonsai-notary "What is a tensor?"

# if engine options must precede the prompt, make the prompt explicit
./bonsai-notary --model 27b --context-size 4096 --prompt "What is a tensor?" -n 64

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

# fail-closed release gate (no chain broadcast; use contracted, not host-visible, CPU cores)
./scripts/accept-gpu.py --cpu-threads 20 \
  --record-dir "$BONSAI_NOTARY_HOME/acceptance/run-001"

# resumable on-chain agent identity (driven through chain_c/build/agentd)
./bonsai-agent status
./bonsai-agent deploy --ricardian-hash <64hex>                          # DRY-RUN unless --confirm
./bonsai-agent action --action-hash <receiptHash> --provenance-hash <modelHash>
```

### Bonsai-27B

The Linux 27B release has two front doors. Both use the same 1-bit Q1_0 language weights, but only the
integer path has a byte-exact execution contract:

| Need | Command | Receipt-capable | Measured GPU use |
|---|---|---:|---:|
| Fast interactive GGUF inference | `./scripts/bonsai.sh bonsai27 …` | No | about 4.17 GiB |
| Deterministic inference / notarization | `./bonsai-notary … --model 27b` | With `--receipts` | about 5.93 GiB observed; 6.28 GiB conservative bound |

```bash
# install the pinned runtime and checksum-verified 3.80 GB GGUF
engine/bonsai/scripts/install_bonsai_27b_gguf.sh
engine/bonsai/scripts/fetch_bonsai_27b_gguf.sh

# regular PrismML llama.cpp path: fast CUDA execution, no receipt
./scripts/bonsai.sh bonsai27 "Explain Merkle proofs." -n 256

# deterministic path: optimized integer producer + separately loaded fresh CPU receipt oracle
./bonsai-notary "Explain Merkle proofs." --model 27b --receipts -n 384
./bonsai-notary "Notarize this." --model 27b --receipts --onchain                  # dry-run
./bonsai-notary "Notarize this." --model 27b --receipts --onchain --chain-confirm # real BSV
```

"1-bit" describes the binary language weights, not every runtime value. The regular runner uses
floating-point activations and CUDA reductions, so it cannot make a portable receipt. The deterministic
runner uses the imported Qwen3.5 integer graph and refuses 27B receipt issuance unless its distinct model
identity, hash-bound quality gate, and fresh native-disabled oracle all verify. See the complete
[`Bonsai-27B guide`](docs/BONSAI-27B.md) for installation, artifact import, measured speed/memory, token
limits, receipt bundles, Ricardian contracts, and Third Entries.

The native 8B and 27B profiles share a contextual REPL: prior turns are retained, exact native prefix state is
reused, and only whole oldest turns are evicted when the token budget fills. Line editing handles arrow keys;
mouse/type-ahead bytes are hidden and flushed while inference or receipt replay is running. `/help` lists session
commands (`/context`, `/system`, `/think`, `/retry`, `/history`, `/paste`, `/clear`, `/bundle`, `/verify`). Ctrl-C
cancels the active generation without committing its partial response. Defaults are model-aware—8B prefers 16,384
tokens when host memory permits, while the current deterministic 27B artifact caps the complete prompt + history +
output at 4,096. Override with `--context-size N`, `BONSAI_CONTEXT_SIZE=N`, or the engine's `bonsai.toml`; use
`/context` to inspect the live budget.

The 27B receipt profile fails closed unless its artifact-bound Qwen3.5 identity and a separately loaded
fresh CPU oracle are available; the optimized producer is never allowed to verify itself. Seeded integer
sampling remains byte-exact and receipt-verifiable. Use `--no-think` for the vendor's non-thinking chat prefix,
or leave thinking enabled for better answer quality. The profile defaults to a 1,024-token generation budget;
an explicit `-n N` / `--max-new N` is appended later and wins.

## State & secrets — outside the repo

Nothing a run produces is written into the checked-out tree. The receipt ledger, signing keys,
packaged bundles, AND the chain_c key files all live under **one shared home**,
`$BONSAI_NOTARY_HOME` (default `~/.local/trinote`). The launchers export it so the engine and chain_c
agree on one home and reuse the same keys. **Never copy or ship that directory.**

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
| `BONSAI_MODEL` | default notary model profile (`8b` or `27b`) | `8b` |
| `BONSAI_CONTEXT_SIZE` | context tokens for either native profile (`auto` also accepted) | model/artifact-aware auto |
| `BONSAI_CPU_THREADS` | cap OpenMP and common BLAS runtimes to the actual CPU entitlement | runtime default |
| `BONSAI_WEIGHTS_REPO` | opt-in local checkout `scripts/fetch_weights.sh` reuses weights from | unset (download) |
| `BONSAI_GPU` | `1` use GPU producer, `0` force CPU | `1` |
| `BONSAI_DRYRUN` | print the resolved command, don't run | `0` |

## Composition (how the three siblings are wired)

`engine`, `chain_c`, and `bsv_third_entry` are **symlinks** to sibling checkouts of the three
dependency repos — they're `.gitignore`d and never committed. `./scripts/bootstrap-deps.sh` creates
them for you by cloning the immutable revisions in `dependencies.lock` from GitHub. Every launcher resolves through `./engine`,
`./chain_c`, `./bsv_third_entry`, and each is env-overridable (`BONSAI_ENGINE_DIR`,
`BONSAI_CHAIN_C_DIR`, `BONSAI_BSV_TE_DIR`) — so you can instead point at your own checkouts, or add
them as **git submodules** at the same paths, with nothing else to change.

## Docs

* `docs/architecture/COMPOSED-ARCHITECTURE.md` — how the four pieces fit and where the seams are.
* `docs/operations/GPU-ACCEPTANCE.md` — fail-closed CUDA receipt/replay acceptance and evidence.
* `operations/README.md` — provider-neutral, state-first acceptance-node lifecycle protocol.
* `docs/SETUP-BONSAI-27B.md` — fresh-host setup, signing keys/mnemonic, funding, and deployment.
* `docs/BONSAI-27B.md` — the two 27B runtimes, install, resource use, receipts, and limits.
* `INSTALL.md` — prerequisites and setup.
* `SECURITY.md` — the security model, secret handling, and the broadcast interlock.
* `docs/receipts/` — the triple-entry receipt + bundle design (from the engine).
