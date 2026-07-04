# The Integer Inference Engine — `int-ref@bonsai-qwen3`

This is the entry-point overview of the engine that runs the ATLAS-Notarized-Bonsai-8B model. For the
deeper companion docs see [`DETERMINISM.md`](DETERMINISM.md) (the bit-exactness contract),
[`SAMPLER-INTEGER.md`](SAMPLER-INTEGER.md) (the seeded integer sampler),
[`PERFORMANCE.md`](PERFORMANCE.md) and [`../performance/BONSAI-SPEED-IMPLEMENTATION.md`](../performance/BONSAI-SPEED-IMPLEMENTATION.md)
(making it fast without breaking the bits), and [`GPU-INTEGER-KERNEL.md`](GPU-INTEGER-KERNEL.md) (the
optional CUDA backend).

## 1. What it is

`BonsaiReferenceModel` (`src/trinote/infer_int/reference_bonsai.py`) is a **deterministic, integer-only
NumPy reference engine** for the Bonsai-8B Qwen3 model, whose weights are stored in **packed Q1_0** form
(one sign bit per weight + one FP16-derived fixed-point scale per 128-weight group). It is a separate
canonical path from the BitNet ternary `ReferenceModelV2`.

The engine's reason for existing is **byte-exact re-execution**: anyone can re-run `M(x)` and obtain a
bit-identical output and trace on any machine, OS, or library — the property the triple-entry receipt design
depends on (a third party *verifies by re-running*, never by trusting). Float inference cannot offer this
(FP addition is non-associative; BLAS/GPU reduction order varies), so every activation, weight scale, RoPE
table entry, RMSNorm gain, and logit is an **integer at fixed-point scale `2^frac`** (`frac` is read from the
artifact config — `16` for the shipped model, not a constant of the format). See
[`DETERMINISM.md`](DETERMINISM.md) and the invariants summary in §5 below.

The forward stack is standard Qwen3-dense: token-embedding gather → for each layer { pre-attention RMSNorm →
attention (per-head q/k-norm, NeoX RoPE, GQA) → residual → pre-FFN RMSNorm → SiLU-gated FFN → residual } →
final RMSNorm → Q1 output head → integer `argmax` with a committed lowest-index tie-break. It supports full
forward, KV-cached prefill/decode, and request-batched decode.

## 2. Four byte-identical backends

The same forward math is available through four backends. The **pure-NumPy oracle is canonical**; the other
three are *accelerators/producers* that must bit-match it. The runtime reports one of three `enginePath`
strings — `"oracle"`, `"sign-cache"`, `"native"` — the **GPU is not its own `enginePath`**: it layers on top
of `"native"` and is toggled by the `TRINOTE_GPU` environment variable.

| backend | `enginePath` | what it is | role |
|---|---|---|---|
| **NumPy oracle** | `oracle` | `q1_linear_ref` on the packed bits, pure NumPy, no `.so` / no `fcntl` | **canonical verifier**; re-runs on any OS/arch |
| **CPU native** | `native` | `tools/libbonsai_q1_kernel.so` (C, `__int128`, `-march`) via `q1_native.py` | committed, portable, byte-exact accelerator |
| **RAM-gated sign-cache** | `sign-cache` | hoists the constant sign-unpack into per-weight `int8` caches | NumPy accelerator when the native `.so` is absent |
| **GPU (CUDA)** | `native` (+`TRINOTE_GPU`) | `tools/libbonsai_q1_gpu.so` via `gpu_native.py` | **per-host opt-in producer**; see [`GPU-INTEGER-KERNEL.md`](GPU-INTEGER-KERNEL.md) |

### Selection & fallback

The fast path is opt-in (`--fast`); without it the engine stays on the oracle. When `--fast` is requested the
ladder is **native → sign-cache → oracle**:

```
engine_path = "oracle"
if fast:
    if model.enable_native():    engine_path = "native"      # also enables the per-op GPU path if TRINOTE_GPU=1
    elif model.enable_fast(...):  engine_path = "sign-cache"
    # else stays "oracle"
```

Fallback is **automatic and per-operation**, not just per-run. The native and GPU wrappers **return `None`
(they never raise) on overflow/unavailability**, so a single op degrades to the next path without aborting:
for a Q1 apply the chain is GPU (if enabled) → native C kernel → sign-cache → oracle. A silent downgrade to
the oracle is treated as a diagnosable problem and logged loudly on stderr.

### The load-bearing contract: byte-identical *or it declines*

The interchangeability claim is precise:

> **Every accelerator is bit-for-bit identical to the NumPy oracle, *or* it declines (returns `None` /
> an `rc` code) and the CPU path produces the value. A backend never silently emits a divergent result.**

That is what makes a receipt produced on the native kernel or the GPU re-executable on a CPU-only verifier:
verification recomputes commitments *from the output ids* (it does not "trust" the producing path), so a
hypothetical divergence would fail verification rather than be signed. Both GPU env levers (`TRINOTE_GPU` and
`TRINOTE_GPU_FULL`) are byte-exact and parity-tested; they differ only in *performance* (see the GPU doc).

## 3. What stays on CPU vs. what is offloaded

- **Always CPU (never offloaded):** token-embedding gather, the single per-token `inv_sqrt_fp` float scalar,
  RoPE table construction, residual adds, the sampler / repetition penalty / argmax tie-break, and receipt
  build·verify·emit. The NumPy oracle is the canonical verifier and the CPU native kernel its fast analogue.
- **CPU native kernel covers the most:** Q1 linear, fused vocab-head argmax, a prepared activation-LUT reused
  across same-input projections (QKV, gate/up), integer RMSNorm, fixed-point SiLU, and decode/prefill/batched
  attention. Each sub-feature is independently default-on with an `TRINOTE_NATIVE_*` env opt-out for oracle
  comparison; two further reproducers (`TRINOTE_Q1_SCALE_CACHE`, `TRINOTE_Q1_LUT32`) are opt-in and documented
  byte-identical to the int64 path.
- **Offloaded to GPU only under `TRINOTE_GPU=1` (+ `--fast`):** the Q1 linear applies (all 7 per-layer
  projections + the output head) via resident weights, plus the M3 resident-monolith prefill. Under the
  additional `TRINOTE_GPU_FULL=1`, RMSNorm and prefill-attention are also offloaded. The GPU reads the
  committed int64 scales — the CPU int32 scale-cache / LUT32 are CPU-bandwidth tricks irrelevant on device.

## 4. Receipts ride on re-execution

Generations are notarized by *re-running* them: `emit_and_verify_bonsai_receipt` builds the receipt,
re-executes to verify, and the JSON path compares this run's `outputCommit` against the first recorded run
for the same key — flagging any mismatch as a determinism violation. Verification runs either on the same
fast/native/GPU model (`--verify-mode fast-local`, the default) or on a freshly loaded pure-NumPy oracle
(`--verify-mode fresh-oracle`). Re-execution on the same GPU only proves reproducibility *on that GPU*; the
trustless cross-machine guarantee comes from the portable NumPy oracle, which any third party can run.

## 5. The determinism invariants (summary)

Full detail and citations live in [`DETERMINISM.md`](DETERMINISM.md); the engine pins:

- **Q1_0 linear — floor-then-sum, wrap-mod-2⁶⁴.** Per block: signed 128-wide integer sum → multiply by that
  block's own int64 scale (**I3**) → **arithmetic** right-shift by `frac` (floor toward −∞, **I4**) → *then*
  sum across blocks. Floor-then-sum (not sum-then-shift) avoids a 1-ULP regrouping disagreement. Integer adds
  are exactly associative, so any reduction order is bit-identical (**I5**). This one path **deliberately
  wraps mod 2⁶⁴** rather than raising, and the NumPy oracle and the C/CUDA kernels wrap *bit-identically*, so
  producer and verifier agree even at overflow.
- **Two overflow policies, never conflated.** The Q1 linear *wraps* (above). Attention `Q@Kᵀ`/`probs@V`,
  softmax, SiLU, RoPE, and the RMSNorm gain multiply instead **fail loud** (raise / decline to the oracle),
  because they have no kernel-parity wrap contract. Note also that attention matmul is **sum-then-shift**
  (one `>> frac` after a full int64 contraction), the opposite order from the Q1 linear's floor-then-sum.
- **The one wide accumulator: RMSNorm.** The cross-layer residual stream is unbounded, so sum-of-squares
  cannot fit int64. The NumPy oracle uses **arbitrary-precision Python big-ints** (`object` dtype) +
  `math.isqrt` + exact integer floor-division; the C/CUDA kernels use a bounded **`__int128`** fast path and
  **decline (`rc=4`) to the big-int oracle** when 128 bits is insufficient. (`__int128` also appears in the
  kernels' attention overflow *guards*, which never alter a computed value.)
- **The only floats** in the forward pass are the *committed, read-only* RoPE cos/sin tables (recomputed only
  on artifact re-import, never at inference) and `inv_sqrt_fp` — a single correctly-rounded, **data-independent**
  IEEE op (depends only on the constant `head_dim`) feeding `round()`, with no float reduction. Everything
  downstream is pure integer.

## 6. Where the code lives

- `src/trinote/infer_int/reference_bonsai.py` — the engine (`BonsaiReferenceModel`, `q1_linear_ref`, forward,
  KV cache, the native/GPU dispatch).
- `src/trinote/infer_int/q1_native.py` — CPU native `.so` wrapper; `tools/bonsai_q1_kernel.c` builds
  `tools/libbonsai_q1_kernel.so` (`tools/build_bonsai_q1_kernel.sh`).
- `src/trinote/infer_int/gpu_native.py` — GPU `.so` wrapper; `tools/bonsai_q1_gpu.cu` builds
  `tools/libbonsai_q1_gpu.so` (`tools/build_bonsai_q1_gpu.sh`). See [`GPU-INTEGER-KERNEL.md`](GPU-INTEGER-KERNEL.md).
- `src/trinote/infer_int/sampler.py` — the seeded integer sampler ([`SAMPLER-INTEGER.md`](SAMPLER-INTEGER.md)).
- `src/trinote/infer_int/artifact_io_bonsai.py`, `import_bonsai_gguf.py` — artifact load / GGUF import.
- Fixed-point primitives: `src/trinote/determinism/fixedpoint.py`.
