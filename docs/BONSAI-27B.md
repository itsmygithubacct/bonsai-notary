# Bonsai-27B on Linux

`bonsai-notary` exposes the same Bonsai-27B Q1 release through two deliberately different runtimes.
Choose the runtime based on whether you need interactive speed or a receipt that another machine can
re-execute.

| Path | Command | Execution | Receipt / BSV Third Entry |
|---|---|---|---|
| Regular GGUF | `./scripts/bonsai.sh bonsai27 …` | pinned PrismML llama.cpp CUDA runtime | No |
| Deterministic notary | `./bonsai-notary … --model 27b` | Trinote Qwen3.5 integer graph; resident CUDA or CPU producer | Yes, with `--receipts` |

The GGUF has Q1_0 binary language weights. That is what **1-bit model** means here. It does not mean
that every value and operation is one bit: the regular llama.cpp path dequantizes into floating-point
activations and uses hardware-dependent CUDA reductions. The deterministic path instead commits an
integer execution contract, including Q16 residuals and Q30 recurrent/pre-normalization state, so its
output tokens can be reproduced byte for byte.

## Install

The pinned regular runtime requires Linux x86-64, an NVIDIA GPU, and a CUDA 12.4-compatible driver. The
deterministic runtime also runs on CPU; CUDA is an optional producer accelerator. Start with the common
setup in [`INSTALL.md`](../INSTALL.md), then:

```bash
export BONSAI_NOTARY_HOME="${BONSAI_NOTARY_HOME:-$HOME/.local/trinote}"

# Shared checksum-verified 3.80 GB Q1 GGUF; this download does not require CUDA.
engine/bonsai/scripts/fetch_bonsai_27b_gguf.sh

# Pinned PrismML runtime (skip this line for deterministic CPU-only use).
engine/bonsai/scripts/install_bonsai_27b_gguf.sh

# Deterministic CPU producer; build the optional CUDA producer as well when nvcc is available.
engine/bonsai/tools/build_bonsai_q1_kernel.sh
engine/bonsai/tools/build_bonsai_q1_gpu.sh       # optional

# One-time, deterministic conversion to the receipt-capable Qwen3.5 artifact (~4.23 GB).
PYTHONPATH=engine/bonsai/src engine/bonsai/.venv/bin/python \
  -m trinote.cli.import_bonsai35_gguf_cli \
  --gguf "$BONSAI_NOTARY_HOME/models/Bonsai-27B-Q1_0.gguf" \
  --out "$BONSAI_NOTARY_HOME/models/Bonsai-27B-Q1_0-int-qwen35.safetensors" \
  --context-len 4096
```

The download is public and normally needs no token. For a mirror or authenticated download, set
`HF_TOKEN`, `BONSAI_TOKEN`, or `HF_TOKEN_FILE`; the fetcher never prints the secret. The engine dependency
ships the 27B model identity and its hash-bound quality-gate record. Receipt issuance fails closed if the
GGUF digest, imported artifact digest, identity, or quality gate does not match the pinned release.

## Run

Fast, regular GGUF inference:

```bash
./scripts/bonsai.sh bonsai27 "How many r's are in strawberry?" -n 128
./scripts/bonsai.sh bonsai27 repl
```

Deterministic integer inference, first without and then with a verified receipt:

```bash
./bonsai-notary "How many r's are in strawberry?" --model 27b -n 128
./bonsai-notary "How many r's are in strawberry?" --model 27b --receipts -n 128
./bonsai-notary json "Explain Merkle proofs." --model 27b --receipts -n 256
./bonsai-notary repl --model 27b --receipts
```

The REPL retains prior turns, reuses exact prefix state, and evicts only whole oldest turns when the
context fills. `/help` lists commands such as `/context`, `/history`, `/retry`, `/bundle`, and `/verify`.
Ctrl-C cancels a generation without committing its partial answer.

To construct an on-chain action without broadcasting, use the default dry-run. A live action requires a
previously deployed agent identity and the explicit confirmation switch; it spends BSV.

```bash
./bonsai-agent deploy --confirm                                      # one-time live deployment
./bonsai-notary "Notarize this." --model 27b --receipts --onchain    # dry-run
./bonsai-notary "Notarize this." --model 27b --receipts --onchain \
  --chain-confirm                                                    # live broadcast
```

## What a 27B receipt proves

The optimized integer CPU or CUDA runner is the **producer**. For issuance, the launcher requires
`--verify-mode fresh-oracle`: a separately loaded, native-disabled NumPy Qwen3.5 model re-executes the
generation. The producer is never allowed to approve its own output. The receipt commits the exact:

- model artifact and release identity;
- input and output token IDs;
- sampler configuration, seed, and integer-logit trace;
- model and counterparty signatures.

This proves provenance and reproducibility, not that the answer is factually correct. The regular GGUF
launcher cannot issue receipts because floating-point execution is not the committed integer contract.

## Resource use and speed

These are measured release values, not minimum system requirements:

| Item | Measured / fixed value |
|---|---:|
| Q1 GGUF on disk | 3,803,452,480 bytes (3.54 GiB) |
| Imported deterministic artifact on disk | about 4.23 GB |
| Regular PrismML process | about 4.17 GiB VRAM |
| Deterministic CUDA graph, observed at populated 4K context | 6,362,562,560 bytes (5.93 GiB) VRAM |
| Deterministic CUDA conservative peak proof | 6,740,049,920 bytes (6.28 GiB) VRAM |
| Deterministic CUDA decode at populated 4K context | 10.40 tokens/s median on an RTX 3070 |
| Deterministic CPU decode | about 0.35 s/token on an 8-core i7-10700F |

The GGUF and artifact figures are file sizes, not process RAM. Host RAM depends on the selected producer,
memory mapping, prompt length, and verification phase; no publication-grade peak host-RAM figure is claimed
yet. A receipt run also loads a fresh CPU oracle, so it needs materially more host memory and time than an
ordinary generation.

The regular and deterministic CUDA runners do not fit together on an 8 GiB GPU. If both REPLs must remain
open, force the deterministic one to CPU with `BONSAI_GPU=0`; otherwise stop the regular process before
starting the deterministic CUDA path. The integer launcher checks the complete allocation plan before
uploading model tensors and falls back to CPU instead of leaving a partial multi-gigabyte upload.

Ordinary `--gpu` runs retain that documented fallback. Release/production gates use `--require-gpu`, which
fails on availability, residency, architecture/memory refusal, or a runtime range guard instead. The supported
composition gate is `scripts/accept-gpu.py`; give it `--cpu-threads N` so OpenMP and each supported BLAS runtime
use the provider's contracted CPU cores rather than host-visible threads.

Prompt prefill is currently sequential and can dominate time for long prompts. The 10.40 tokens/s figure is
populated-cache decode throughput, not time to first token and not fresh-oracle receipt replay time.

## Context and output limits

There is no hard 256-token output ceiling. Some examples use `-n 256` only to keep demonstrations short.

- The imported deterministic artifact currently has a 4,096-token total context. Input, retained chat
  history, thinking tokens, and generated output all share it.
- The notary's 27B profile defaults to at most 1,024 new tokens. `-n N` or `--max-new N` overrides that
  budget, subject to the remaining 4,096-token context.
- The regular GGUF launcher also defaults to a 4,096-token context so it fits an 8 GiB card. Change it with
  `BONSAI_27B_CTX_SIZE`; larger contexts need more VRAM.
- Use `--context-size N` or `BONSAI_CONTEXT_SIZE=N` for the deterministic path. It cannot exceed the
  artifact's imported cap without rebuilding a different artifact and identity.

The notary preset uses deterministic seeded sampling and a 4-token no-repeat n-gram guard. This prevents
exact short loops without imposing a small output cap. Explicit sampler and generation flags appear later
on the command line and take precedence.

## Receipt bundle, Ricardian contract, and Third Entry

A **receipt bundle** is the portable audit package: canonical `receipt.json` and `preimage.json`, the chain
artifact and on-chain descriptor, optional identity/ledger evidence, and a manifest whose `bundleHash`
commits every file. It contains the data needed to check hashes, signatures, and token commitments; with the
artifact it can also re-execute the model. It does not contain private keys or the 4.23 GB model artifact.

In a stateful publication, the **Ricardian contract** is the human-readable agent charter plus its exact
machine-readable deployment binding. Its hash is fixed in the on-chain AgentTea identity, so the prose and
enforced policy cannot silently diverge. This agent-policy identity is separate from the 27B model identity:
the action binds them by using the inference `receiptHash` as `actionHash` and the model `modelHash` as
`provenanceHash`.

The **Third Entry** is the public shared commitment. The two receipt signers form the first and second
entries; the local hash-linked ledger and, when requested, the BSV transaction form the shared third entry.
Only commitments and public transaction data go on chain—not the prompt, answer text, model weights, or
private keys. Anyone with the bundle can recompute the action commitment and look up its transaction on
WhatsOnChain.

Verify a bundle offline, then optionally check BSV and re-execute the 27B artifact:

```bash
PYTHONPATH=engine/bonsai/src engine/bonsai/.venv/bin/python \
  -m trinote.cli.receipt_bundle_cli verify "$BUNDLE"

PYTHONPATH=engine/bonsai/src engine/bonsai/.venv/bin/python \
  -m trinote.cli.receipt_bundle_cli verify "$BUNDLE" --onchain --reexec \
  --artifact "$BONSAI_NOTARY_HOME/models/Bonsai-27B-Q1_0-int-qwen35.safetensors"
```

Add `--oracle` to force the slow pure-NumPy verifier instead of the byte-identical native re-execution
accelerator. See [`RECEIPT-BUNDLE.md`](receipts/RECEIPT-BUNDLE.md),
[`THIRD-ENTRY.md`](receipts/THIRD-ENTRY.md), and the engine's
[27B implementation guide](https://github.com/itsmygithubacct/integer_inference_engine/blob/main/bonsai/BONSAI-27B-GGUF.md)
for the complete formats and evidence.

## Troubleshooting

- **No 27B weights:** run the pinned fetcher, then import the deterministic artifact if using the notary.
- **Receipt rejected:** do not substitute another identity or artifact. Check their digests and rebuild from
  the pinned GGUF; the gate is intentionally fail-closed.
- **Integer runner unexpectedly uses CPU:** stop other GPU model processes, verify `nvcc` built the resident
  CUDA library, and retry. Use `--verbose` for backend diagnostics.
- **Repeated output:** keep the default no-repeat n-gram guard, shorten retained history, use `/retry`, or
  start a clean turn with `/clear`. Raising the token budget does not itself cure a repetition loop.
- **On-chain command does not broadcast:** that is the safe default. A live action requires both the launcher
  confirmation and the lower-level mainnet interlock described in [`SECURITY.md`](../SECURITY.md).
