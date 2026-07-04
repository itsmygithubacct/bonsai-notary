# INSTALL — bonsai-notary (composed)

`bonsai-notary` is a thin composition of four projects (see `README.md` and
`docs/architecture/COMPOSED-ARCHITECTURE.md`). Installing it means: get the three sibling checkouts
in place, build the on-chain C software, create the engine's Python venv, build the native kernel,
and fetch the model weights **into the state home**. None of that touches the repo tree — all
generated state and weights live under `$BONSAI_NOTARY_HOME` (default `~/.local/trinote`).

> The state/secrets home (`~/.local/trinote`) lives OUTSIDE this repo — never copy or ship it.

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

## 1. The three sibling checkouts

`bonsai-notary` references them at `./engine`, `./chain_c`, `./bsv_third_entry`. Today these are
symlinks to local checkouts; for a fresh clone, either symlink your checkouts or (future) add them as
git submodules at those paths. Each path is env-overridable
(`BONSAI_ENGINE_DIR`, `BONSAI_CHAIN_C_DIR`, `BONSAI_BSV_TE_DIR`).

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
(cd engine/bonsai && uv venv && uv pip install -r ~/bonsai-notary/requirements_notary.txt)

# Native packed-Q1 CPU kernel (byte-identical accelerator); built under $BONSAI_NOTARY_HOME/bin.
engine/bonsai/tools/build_bonsai_q1_kernel.sh
# Optional per-host CUDA kernel (needs nvcc); --gpu falls back to the CPU oracle when absent.
engine/bonsai/tools/build_bonsai_q1_gpu.sh        # optional
```

## 4. Fetch the model weights into the state home

Weights (~2.6 GB, gitignored) are **not** in any repo. `scripts/fetch_weights.sh` puts them under
`$BONSAI_NOTARY_HOME/models` and sha256-verifies them against the identity record:

```bash
# Default: download the GGUF from HuggingFace (prism-ml/Bonsai-8B-gguf, public/Apache-2.0) and BUILD
# the int-ref safetensors from it by import — no HF_REPO override and no local checkout needed:
./scripts/fetch_weights.sh

# Override the source repo (e.g. a private mirror; HF_TOKEN for private repos):
HF_REPO=<org>/Bonsai-8B-gguf HF_TOKEN=hf_xxx ./scripts/fetch_weights.sh

# (OPT-IN) reuse a verified local GGUF instead of downloading — OFF by default:
BONSAI_WEIGHTS_REPO=~/some/checkout ./scripts/fetch_weights.sh   #  --copy · --from DIR · --dry-run
```

## 5. Optional: the BSV wallet + a resumable on-chain identity

```bash
# Wallet deps (only if you fund/sign in Python rather than letting chain_c hold keys):
uv pip install -r requirements_wallet.txt

# The --onchain path is a RESUMABLE agentd action under a persisted identity. Deploy it once:
./bonsai-agent deploy --confirm                 # DRY-RUN without --confirm; --confirm spends BSV
```

## 6. Smoke test

```bash
./bonsai-notary "What is a tensor?"                          # deterministic generation, model output
./bonsai-notary "Explain Merkle proofs." --receipts -n 128   # + verified local receipt
./bonsai-notary "Notarize this." --receipts --onchain        # + chain_c Third Entry (DRY-RUN)

# run the test suites
( cd bsv_third_entry && PYTHONPATH=. ../engine/bonsai/.venv/bin/python -m pytest tests/ -q )
PYTHONPATH=engine/bonsai/src:bsv_third_entry engine/bonsai/.venv/bin/python -m pytest tests/ -q
```

## State & secrets — outside the repo

Everything generated (receipt ledger, signing keys, bundles, sessions) and all chain_c key files live
under `$BONSAI_NOTARY_HOME` (default `~/.local/trinote`); the model weights live in
`$BONSAI_NOTARY_HOME/models`. Nothing is written into a repo tree, and that home is never copied or
shipped. See `SECURITY.md`.
