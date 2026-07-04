# PERFORMANCE.md — making deterministic Bonsai inference fast without breaking bit-exactness

bonsai-notarized-bitnet runs **bit-exact integer inference** so a receipt can be re-executed and verified
through the portable pure-NumPy oracle on any machine — no `.so`, no `fcntl` needed (the optional native
kernel + local-ledger lock are POSIX + x86_64/aarch64; see [DETERMINISM.md](DETERMINISM.md) "Platform scope",
[../receipts/THIRD-ENTRY.md](../receipts/THIRD-ENTRY.md)). That
constraint shapes every performance decision: an optimization is only admissible if the **committed bits do
not change** — not on this CPU, and not on the arbitrary CPU that later re-runs the receipt.

## TL;DR — run it fast
```bash
OMP_NUM_THREADS=8 trinote-run-bonsai --fast --fast-required ...     # or: tools/launch_bonsai_live.sh
```
- **`--fast` is required to engage the native kernel.** The CLI defaults to `fast=False`
  (`run_bonsai_cli.py`), which runs the **single-threaded numpy oracle** — one core, the slowest path
  (the observed demo baseline was ~100 s/tok; see the measured table in
  [../performance/BONSAI-SPEED-IMPLEMENTATION.md](../performance/BONSAI-SPEED-IMPLEMENTATION.md) §13).
  Without `--fast` you get correct receipts at the slowest possible speed.
- **`OMP_NUM_THREADS=8`** — clamp to *physical* cores. See the SMT trap below.

## The bottleneck, and the trap that hides it
The hot path is the Q1_0 (1-bit weight) linear in every layer. There are two implementations, and they are
**proven byte-identical** by `tests/test_bonsai_smoke.py::test_bonsai_native_q1_kernel_matches_oracle_if_present`:

| Path | Where | Threads | Role |
|---|---|---|---|
| **numpy oracle** `q1_linear_ref` | `reference_bonsai.py` | 1 (scalar) | the **source of truth** — what a verifier re-executes |
| **native packed-Q1** `q1_linear_native` | `q1_native.py` → `tools/libbonsai_q1_kernel.so` (from `bonsai_q1_kernel.c`) | OpenMP `collapse(2)` over `(token, out_feature)` + AVX2 LUT | the **fast reproducer**, enabled by `enable_native()` under `--fast` |

A live session was observed pinned at **100 % CPU = exactly one of 16 logical cores**. Root cause: the launch
omitted `--fast`, so `enable_native()` was never called and generation fell to the numpy oracle. The native
C/OpenMP kernel — which *already* combines threading and SIMD — was never loaded. Fix: `--fast --fast-required`
(fail loudly rather than silently fall back) + `OMP_NUM_THREADS`.

### The SMT trap (this is an 8-physical / 16-logical i7-class box)
Integer GEMM is ALU-bound, so SMT (hyperthreads) buys ~nothing, and over-subscription *hurts*. Measured on a
1-token decode call: **16 threads = 15.95 ms vs 1 thread = 3.04 ms — 5× slower** (SMT contention +
fork-join). **Always clamp `OMP_NUM_THREADS` to physical cores (8), never the logical count (16),** and
serialize tiny single-token decode steps. The realized gain over the single-core oracle is large but
*not* a single clean multiplier (it depends on context length and which kernel dominates) — see the
measured per-stage tables in
[../performance/BONSAI-SPEED-IMPLEMENTATION.md](../performance/BONSAI-SPEED-IMPLEMENTATION.md) §13
(e.g. native M=1 attention ~1.28× at context L~109 and growing with context; native SiLU ~10.96× on
its bucket). It is never 16×, and any single illustrative multiplier here should be read against that
measured doc, not as a guarantee.

## The optimization decision: threading vs vectorization (and "both")
They are *orthogonal* and **both already ship in the kernel**: OpenMP threading across independent outputs
(A) × AVX2-LUT vectorization within each dot product (B). The verdict (code-grounded, adversarially checked):

**A (threading) is the lever to tune; B (vectorization) is banked.** The decisive reason is *how each stays
bit-exact*:

- **A is bit-exact by geometry.** Threads partition the orthogonal `(token, out_feature)` axis — no thread
  ever owns a partial slice of a reduction, so every cross-block `+=` happens inside one thread. The result is
  independent of thread count, schedule, compiler, **and ISA**. Structurally unbreakable.
- **B is bit-exact only because the accumulator is an exact modular-2⁶⁴ integer ring** — `uint64` add/mul, no
  saturation (`hasSaturation = false`, the declared wrap-on-overflow contract in `reference_bonsai.py`), the
  per-128-group scale applied as integer `multiply-then-arithmetic-shift` *before* the cross-block sum, and **no
  float / no FMA** anywhere in the Q1 path. Integer add/mul mod 2⁶⁴ are associative+commutative, so lane-width
  reassociation (SSE/AVX2/AVX-512/NEON) yields the same residue. This is correct *today* but contingent.

So A is robust; B is correct-but-fragile. On the property the project rests on — portable committed bits — A
wins, and "both" is the existing reality, not a new build.

## Invariants the bit-exact gate depends on (do not break these)
These are load-bearing; changing any one can silently produce **ISA-dependent receipts** (a cross-machine
verification failure, i.e. a correctness/security bug, not a perf bug):

1. **Threading stays on the `(token, out_feature)` axis.** Never parallelize the contraction / `n_blocks`
   reduction with a non-deterministic combiner.
2. **The accumulator stays exact + non-narrowing** — `uint64`, no int32 partials, **no saturating int8 VNNI
   (`vpdpbusd`)**, no clamp.
3. **The group scale stays integer** — `multiply-then-arshift_i64`; no float dequant, no FMA, `-ffp-contract`
   off.
4. **The build stays portable** — `-march=x86-64-v2` (`build_bonsai_q1_kernel.sh`); **never `-march=native`,
   never `-ffast-math`**.
5. **The argmax tie-break stays lowest-index** (`bonsai_q1_kernel.c`) — it is part of the committed contract.
6. **The numpy oracle remains the source of truth**; the native kernel is contractually a *reproducer*, and
   verification always cross-checks against the oracle.

The guard is `tests/test_bonsai_smoke.py` (the `native` tests prove byte-identity to the oracle across thread
counts and at the int64 overflow boundary). Keep them green; they need `ecdsa` in the venv (already present).

## Future work: a gated re-vectorization
A genuinely *new* aggressive vectorization (e.g. saturating VNNI int8) is viable as a second step ONLY under:
(1) a declared determinism scope (oracle stays canonical); (2) a proof of an exact, non-narrowing accumulator
with pinned reduction; and (3) a **per-ISA parity CI matrix** (x86-64-v2/v3 + ARM-NEON, gcc + clang) that
*proves* modular invariance per target rather than asserting it. Until then, the lever is `OMP_NUM_THREADS`
and engaging the existing kernel via `--fast`.

A GPU is the device analogue of this CPU native path under the same byte-exact discipline (a *producer*; the
CPU oracle stays the canonical verifier) — see [GPU-INTEGER-KERNEL.md](GPU-INTEGER-KERNEL.md), and
[INFERENCE-ENGINE.md](INFERENCE-ENGINE.md) for the full backend map.

## References
- This repo: [INFERENCE-ENGINE.md](INFERENCE-ENGINE.md) · [DETERMINISM.md](DETERMINISM.md) ·
  [GPU-INTEGER-KERNEL.md](GPU-INTEGER-KERNEL.md) · `src/trinote/infer_int/reference_bonsai.py` ·
  `src/trinote/infer_int/q1_native.py` · `tools/bonsai_q1_kernel.c` · `tools/build_bonsai_q1_kernel.sh` ·
  `src/trinote/infer_int/gpu_native.py` · `tools/bonsai_q1_gpu.cu` · `tools/build_bonsai_q1_gpu.sh` ·
  `tests/test_bonsai_smoke.py` · `tests/test_bonsai_gpu.py` · `tools/launch_bonsai_live.sh`
- Field context: `~/research/bonsai-notarized/deterministic-inference.md` (why reduction order × FP
  non-associativity is the root cause integer arithmetic sidesteps).
