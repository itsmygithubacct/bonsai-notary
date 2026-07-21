# INSTALL — bonsai-notary (composed)

`bonsai-notary` is a thin composition of four projects (see `README.md` and
`docs/architecture/COMPOSED-ARCHITECTURE.md`). Installing it means: get the three sibling checkouts
in place, build the on-chain C software, create the engine's Python venv, build the native kernel,
and fetch the model weights **into the state home**. None of that touches the repo tree — all
generated state and weights live under `$BONSAI_NOTARY_HOME` (default `~/.local/trinote`).

> The state/secrets home (`~/.local/trinote`) lives OUTSIDE this repo — never copy or ship it.

For a fresh receipt-capable **Bonsai-27B agent**, the recommended path is the idempotent all-in-one
installer rather than the manual steps below:

```bash
./scripts/setup-bonsai-27b.sh
```

It also provisions or imports signing identities and fails loudly when public Third Entry mode is requested
without wallet funds. See [`docs/SETUP-BONSAI-27B.md`](docs/SETUP-BONSAI-27B.md) for required information,
unattended examples, funding, and the separately confirmed one-time AgentTea deployment.

## 0. Prerequisites

| Need | For |
|---|---|
| `git`, `bash` | clone + launchers |
| `uv` | the engine's Python venv (never plain `pip`) |
| `cmake` ≥ 3.16, a C11 compiler, `pkg-config` | build chain_c |
| `libsecp256k1-dev`, `libssl-dev`, `libcurl4-openssl-dev` | chain_c crypto/HTTP (`sudo apt install …`) |
| a C/C++ compiler with OpenMP (`libgomp`) | the native packed-Q1 inference kernel |
| `curl` or `wget`, `sha256sum` | fetch + verify weights |
| (optional) CUDA `nvcc` | the per-host GPU kernel; absent → CPU fallback |
| (27B regular path) Linux x86-64, NVIDIA GPU, CUDA 12.4-compatible driver | pinned PrismML llama.cpp runtime |

## 1. The three sibling checkouts

`bonsai-notary` references them at `./engine`, `./chain_c`, `./bsv_third_entry`. The easiest way to
get them is the bootstrap script — it clones all three from GitHub and wires the symlinks:

```bash
./scripts/bootstrap-deps.sh                        # clone siblings + link engine/ chain_c/ bsv_third_entry/
BONSAI_DEPS_UPDATE=1 ./scripts/bootstrap-deps.sh   # (later) fast-forward the sibling checkouts
```

Prefer your own checkouts? Point the env vars (`BONSAI_ENGINE_DIR`, `BONSAI_CHAIN_C_DIR`,
`BONSAI_BSV_TE_DIR`) at them, symlink them by hand, or add them as git submodules at those paths —
the launchers resolve through `./engine`, `./chain_c`, `./bsv_third_entry` either way. Confirm they
resolve:

```bash
ls -l engine chain_c bsv_third_entry        # must resolve:
#   engine          -> the inference-engine checkout (trinote lives under <dir>/bonsai/src)
#   chain_c         -> the chain_c checkout         (CLIs build under <dir>/build)
#   bsv_third_entry -> the on-chain orchestration checkout
```

## 2. Build the on-chain software (C)

```bash
(bash chain_c/build_chain_c.sh)
# produces chain_c/build/{bonsai_third_entry,agentd,deploy,cpfp,woc,verify_ricardian}
ctest --test-dir chain_c/build --output-on-failure -LE net      # optional: run the offline suite
```

## 3. The engine venv + native kernel

```bash
# Python runtime for the inference engine (numpy + safetensors + ecdsa); the notary glue is stdlib.
(cd engine/bonsai && uv venv)
uv pip install --python engine/bonsai/.venv/bin/python -r requirements_notary.txt

# Native packed-Q1 CPU kernel (byte-identical accelerator); built under $BONSAI_NOTARY_HOME/bin.
engine/bonsai/tools/build_bonsai_q1_kernel.sh
# Exact Qwen tokenizer required by deterministic inference (CPU-only; CUDA is not required).
./scripts/install-llama-tokenizer.sh
# Optional per-host CUDA kernel (needs nvcc); --gpu falls back to the CPU oracle when absent.
engine/bonsai/tools/build_bonsai_q1_gpu.sh        # optional
```

## 4. Fetch Bonsai-8B into the state home

The 8B weight pair (~2.6 GB total, gitignored) is **not** in any repo. `scripts/fetch_weights.sh` puts
it under `$BONSAI_NOTARY_HOME/models` and sha256-verifies it against the identity record:

```bash
# Default: download the GGUF from HuggingFace (prism-ml/Bonsai-8B-gguf, public/Apache-2.0) and BUILD
# the int-ref safetensors from it by import — no HF_REPO override and no local checkout needed:
./scripts/fetch_weights.sh

# Override the source repo (e.g. a private mirror; HF_TOKEN for private repos):
HF_REPO=<org>/Bonsai-8B-gguf HF_TOKEN=hf_xxx ./scripts/fetch_weights.sh

# (OPT-IN) reuse a verified local GGUF instead of downloading — OFF by default:
BONSAI_WEIGHTS_REPO=~/some/checkout ./scripts/fetch_weights.sh   #  --copy · --from DIR · --dry-run
```

## 5. Optional: install Bonsai-27B

The 27B release has two runtimes: a fast PrismML llama.cpp CUDA path without receipts, and a deterministic
integer path that can issue receipts. Both start from the same pinned, checksum-verified 3.80 GB Q1 GGUF.

```bash
export BONSAI_NOTARY_HOME="${BONSAI_NOTARY_HOME:-$HOME/.local/trinote}"

# Shared pinned model download. It works anonymously and does not require CUDA.
engine/bonsai/scripts/fetch_bonsai_27b_gguf.sh

# Regular GGUF runtime (Linux x86-64/NVIDIA only; skip for deterministic CPU-only use).
engine/bonsai/scripts/install_bonsai_27b_gguf.sh

# One-time conversion for deterministic inference and notarization.
PYTHONPATH=engine/bonsai/src engine/bonsai/.venv/bin/python \
  -m trinote.cli.import_bonsai35_gguf_cli \
  --gguf "$BONSAI_NOTARY_HOME/models/Bonsai-27B-Q1_0.gguf" \
  --out "$BONSAI_NOTARY_HOME/models/Bonsai-27B-Q1_0-int-qwen35.safetensors" \
  --context-len 4096

# Optional deterministic CUDA producer; receipt verification still uses a fresh CPU oracle.
engine/bonsai/tools/build_bonsai_q1_gpu.sh
```

The imported artifact is about 4.23 GB. The engine checkout supplies its distinct 27B model identity and
hash-bound quality gate; the notary validates both and fails closed on any mismatch. The full installation,
resource table, context/output controls, and receipt workflow are in [`docs/BONSAI-27B.md`](docs/BONSAI-27B.md).

## 6. Optional: the BSV wallet + a resumable on-chain identity

```bash
# Wallet deps (only if you fund/sign in Python rather than letting chain_c hold keys):
uv pip install -r requirements_wallet.txt

# The --onchain path is a RESUMABLE agentd action under a persisted identity. Deploy it once:
./bonsai-agent deploy --confirm                 # DRY-RUN without --confirm; --confirm spends BSV
```

## 7. Smoke test

```bash
./bonsai-notary "What is a tensor?"                          # deterministic generation, model output
./bonsai-notary "Explain Merkle proofs." --receipts -n 128   # + verified local receipt
./bonsai-notary "Notarize this." --receipts --onchain        # + chain_c Third Entry (DRY-RUN)

# 27B: regular GGUF, deterministic integer, then receipt wiring without loading the model
./scripts/bonsai.sh bonsai27 "Count the letters in strawberry." -n 64
./bonsai-notary "Count the letters in strawberry." --model 27b -n 64
BONSAI_DRYRUN=1 ./bonsai-notary "Count the letters in strawberry." --model 27b --receipts -n 64

# run the test suites
( cd bsv_third_entry && PYTHONPATH=. ../engine/bonsai/.venv/bin/python -m pytest tests/ -q )
PYTHONPATH=engine/bonsai/src:bsv_third_entry engine/bonsai/.venv/bin/python -m pytest tests/ -q
```

## State & secrets — outside the repo

Everything generated (receipt ledger, signing keys, bundles, sessions) and all chain_c key files live
under `$BONSAI_NOTARY_HOME` (default `~/.local/trinote`); the model weights live in
`$BONSAI_NOTARY_HOME/models`. Nothing is written into a repo tree, and that home is never copied or
shipped. See `SECURITY.md`.
