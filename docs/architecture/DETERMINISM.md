# DETERMINISM.md — the bit-exact contract (the #1 engineering risk, written down)

The entire Ricardian-TEA "third entry is **trustless**, not trusted" claim rests on one property:
**anyone can re-run `M(x)` and get a byte-identical `y` and trace, regardless of machine, library,
or summation order.** Float inference cannot offer this (FP addition is non-associative; BLAS/GPU
reduction order varies). trinote buys it with a binary Q1_0 + fixed-point integer reference path.

> **Platform scope.** Byte-exact re-verification runs through the **portable pure-NumPy oracle**
> (`infer_int/reference_bonsai.py`) — it needs neither the native `.so` nor `fcntl`, so it re-verifies
> anywhere (any OS/arch, and re-implementable in other languages). The *optional* native kernel
> (`tools/bonsai_q1_kernel.c`, `__int128` + `-march`) and the local-ledger file lock (`fcntl`) are
> POSIX + x86_64/aarch64 only; they are an accelerator/lock, not part of the byte-exactness. The optional
> per-host CUDA kernel (`tools/bonsai_q1_gpu.cu`, NVIDIA-only, gitignored arch-specific `.so`) is a further
> such accelerator — a *producer* that must bit-match this oracle, declining (`None`/`rc`) to the CPU path
> rather than ever diverging. Local GPU↔oracle byte-parity is now testable in-tree via
> `tests/test_bonsai_gpu.py` (it skips without a GPU); see [GPU-INTEGER-KERNEL.md](GPU-INTEGER-KERNEL.md).
> This is narrower than the parent-repo GPU↔CPU re-verify against real trained weights noted in the contract
> table below.

> **Scope of this extraction.** The sole canonical re-executor for the SHIPPED model is
> `infer_int/reference_bonsai.py` (Qwen3-dense, binary Q1_0 weights — the `int-ref@bonsai-qwen3`
> engine in the identity JSON). There is no `infer_int/reference.py`. The only test bundled here is
> `tests/test_bonsai_smoke.py`; rows below that
> cite parent-repo (`ATLAS-Notarized-BitNet`) tests/dirs/CLIs are marked accordingly. (`torch` is **not**
> a dependency — `requirements_atlas_notarized.txt` pins only `numpy` + `safetensors` — so any
> "torch↔ref" claim is parent-repo context, not reproducible here.)

> This is a *maintained engineering property of the integer reference path*, not a law of nature.
> Guard it in CI; confess its residuals here. If a change makes the reference path non-reproducible,
> the third entry silently degrades from trustless to trusted — so determinism is a release gate.

## The contract (what must hold)

Status legend: ✅ = ships + covered by `tests/test_bonsai_smoke.py`; ◐ = parent-repo
(ATLAS-Notarized-BitNet) status, tests not bundled in this Bonsai extraction.

| # | Requirement | Where | Status |
|---|-------------|-------|--------|
| 1 | Matmul reduction is **integer** (int activations × binary Q1_0 signs → int64 accumulate) | `infer_int/matmul.py` (shared); `infer_int/reference_bonsai.py::q1_linear_ref` (Bonsai) | ✅ done + tested |
| 2 | Weight/activation quant is deterministic (binary Q1_0 signs + per-128-group scale; round-half-even pinned) | `infer_int/reference_bonsai.py` (Bonsai); `quant/ternary.py`, `quant/activation.py` (2B4T, ◐) | ✅ Bonsai done + tested |
| 3 | Scale application order is **pinned** (per-group sum × scale, then `>> frac`; avoids 1-ULP regrouping) | `infer_int/reference_bonsai.py::q1_linear_ref`; `infer_int/matmul.py::linear_int` | ✅ done + tested |
| 4 | LayerNorm/softmax computed in **fixed-point** (integer sum-of-squares + `math.isqrt`; integer softmax) | `determinism/fixedpoint.py` | ✅ done + tested |
| 4b | **Fixed-point attention** (Q@Kᵀ, probs@V via integer-sum fixed-point matmul) — decision #attn | `determinism/fixedpoint.py::fixed_point_matmul` | ✅ done + tested |
| 4c | **RoPE via committed fixed-point tables** (no live trig; table hash in the env contract) — decision #arch | `model/rope_v2.py::build_yarn_rope_tables` (YaRN, the flagship GGUF); `model/rope.py::build_rope_tables` (plain RoPE, 2B4T) | ✅ done + tested |
| 4d | **gated-SiLU** FFN (Qwen3 dense; exact integer fixed-point) | `infer_int/reference_bonsai.py::_ffn_ref`; `determinism/fixedpoint.py::fixed_point_squared_relu` (2B4T squared-ReLU, ◐) | ✅ Bonsai done + tested |
| 5 | Frozen tokenizer (`tokenizerHash` from the GGUF token metadata) — no tokenizer drift | `infer_int/gguf_tokenizer_v2.py` (qwen2-gpt2-bpe, vocab 151,669; `tokenizerHash 085fe8da…`) | ✅ done + tested |
| 6 | Committed sampler — ALL modes integer + receipt-bound (greedy argmax; seeded temp/top-k/top-p via committed fixed-point temperature + SHA-256-counter Lemire draw) | `infer_int/sampler.py` (see [SAMPLER-INTEGER.md](SAMPLER-INTEGER.md)) | ✅ greedy receipts tested; seeded modes per [SAMPLER-INTEGER.md](SAMPLER-INTEGER.md) |
| 7 | A single canonical **reference re-executor** that GPU/training kernels must bit-match | `infer_int/reference_bonsai.py` (the sole canonical re-executor) | ✅ done + tested (byte-identical re-execution in `tests/test_bonsai_smoke.py`) |
| 8 | Cross-machine CI: a produced receipt re-verifies bit-for-bit on CPU before release | `tests/test_bonsai_smoke.py` | ◐ Bonsai receipts re-verify here; full GPU↔CPU re-verify against real trained weights and the on-chain receipt verifier are parent-repo (not bundled) |

## What is already proven (here, in `tests/test_bonsai_smoke.py`)

The bundled smoke suite exercises the integer reference path end-to-end for the shipped Bonsai model:

- **Byte-identical re-execution** — a greedy Bonsai receipt re-derives token-for-token
  (`test_bonsai_receipt_reexecutes`), and KV-cached decode is bit-identical to the uncached oracle
  (`test_bonsai_kv_cache_is_bit_identical`, `test_bonsai_prefill_logits_match_forward_last_only`).
- **Fast/native parity** — the Q1_0 sign-cache and native packed-Q1 kernel match the packed oracle
  byte-for-byte (`test_bonsai_q1_sign_cache_matches_oracle`, `test_bonsai_fast_forward_matches_oracle_logits`,
  the `*_native_q1_*` cases), including at the overflow boundary, so no fast path is a new engine.
- **YaRN table constants** — `model/rope_v2.py::build_yarn_rope_tables` matches the PrismML/llama.cpp
  YaRN constants (`test_bonsai_yarn_tables_match_prismml_llamacpp_constants`).
- **Fixed-point RMSNorm / softmax** — deterministic and order-free; reciprocal RMS via `math.isqrt`;
  rows sum to ~`2^frac` in fixed-point, argmax-preserving.

> Parent-repo (`ATLAS-Notarized-BitNet`) coverage — reduction-order-invariance and float-contrast
> demonstrations in `tests/determinism/test_bitexact.py` + `tests/golden/test_matmul_golden.py` — is
> **not bundled** in this extraction. The integer property is identical (no library/GPU reduction order
> can change an integer sum); only those specific test files live upstream.

## Who runs the reference path today

The canonical reference engine is not only a CI oracle — `cli/trinote-run-bonsai` (and `bonsai_notary.sh`)
runs it as the live, interactive inference path (numpy-only, no torch to *run*;
`infer_int/{reference_bonsai,sampler}.py`). Every exposed int-ref sampler is receipt-bound by exactly
this contract: `greedy` is an integer `argmax` over fixed-point logits, and `temp`/`top_k`/`top_p`
replay a committed seed through the integer SHA-256-counter draw described in
[`SAMPLER-INTEGER.md`](SAMPLER-INTEGER.md). Each receipt commits the sampler block in the trace and
verification replays the committed settings, not mutable preimage metadata.

## Honest residuals (what is NOT yet guaranteed)

- Full **forward-pass** bit-exactness (attention + residual + embedding lookups end-to-end) is
  **built and proven byte-identical** for the shipped Bonsai model (`infer_int/reference_bonsai.py`;
  re-execution + fast/native parity in `tests/test_bonsai_smoke.py`). What remains is the cross-machine
  **GPU↔CPU re-verify against real trained weights**, which is parent-repo work (not bundled here; `torch`
  is not a dependency, so no torch↔ref correlation is asserted in this extraction).
- The fixed-point `exp`/softmax is a **modest cubic approximation**; determinism is exact, numerical
  accuracy is approximate. If accuracy proves insufficient for quality, improve the polynomial — but
  never trade determinism for a libm call on the reference path.
- Determinism covers **inference only**. Training may be (and will be) stochastic; the committed
  artifact is the trained weights, and the reference path re-executes inference over them.
- A reproducible inference proves *what the model did*, never that the output is *correct*
  (honest scope, carried from `priscilla_bsv`).
- **Re-import is not hash-stable across platforms.** The committed RoPE cos/sin tables are built with
  libm trig — for the shipped flagship GGUF that is `model/rope_v2.py::build_yarn_rope_tables` (YaRN;
  `model/rope.py::build_rope_tables` is the plain-RoPE 2B4T builder) — whose last-ULP results can differ
  by platform, so re-importing the GGUF elsewhere may yield a slightly different artifact and `modelHash`.
  *Inference*
  stays bit-exact everywhere (the tables are read from the committed artifact, never recomputed); only
  *regeneration* is affected. The shipped, hashed artifact is canonical — re-import is a fresh build to
  be re-validated by the quality gate, not a guaranteed bit-identical reproduction.
