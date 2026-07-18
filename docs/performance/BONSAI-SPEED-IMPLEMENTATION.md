# Bonsai Native Speed Implementation Plan

The native Bonsai demo is currently too slow for live use. The Merkle-tree demo turn generated two tokens in
`200.3s`, then spent additional time verifying and emitting the receipt. That is not a viable receipt-bound
REPL.

This document applies the lessons from `CACHING.md`, `CACHING_2.md`, and `FASTER-KERNELS.md` to the Bonsai
Qwen3/Q1_0 path. The target is faster native receipt emission without changing any committed model output.

> **Source-doc scope.** `CACHING.md`, `CACHING_2.md`, and `FASTER-KERNELS.md` are parent-repo
> (`ATLAS-Notarized-BitNet`) design notes and are **not bundled** in this extraction; they are summarized
> in §1 below for context. The Bonsai speed implementation itself **does ship and is real** — the kernels,
> KV cache, native Q1_0 path, and benchmarks described here live under `src/trinote/infer_int/` and
> `tools/`, and are exercised by `tests/test_bonsai_smoke.py` (see §13 "Implemented Status").

## 1. What the Existing Speed Docs Say

`CACHING.md`:

- Add a KV cache so decode stops recomputing the full prefix every token.
- Keep the cache bit-exact: cached decode must produce the same logits/tokens as the oracle `forward`.
- Project only the last hidden state through the LM head during decode.
- Keep receipt verification valid by preserving the same token/logit bytes.

`CACHING_2.md`:

- After KV caching, the remaining floor was another cache miss: constant weights were converted again on
  every matmul call.
- The fix was not new math. It was hoisting a pure conversion of constant weights out of the loop.
- The profile, not intuition, determined the work order.

`FASTER-KERNELS.md`:

- Every fast path must be byte-identical to the oracle.
- Use an oracle path plus a fast path gated by tests.
- Start with the smallest measured fix, then graduate to a native kernel if the NumPy strategy remains too
  slow or too memory-heavy.

## 2. Bonsai-Specific Diagnosis

Bonsai is not using the flagship BitNet kernels. Its hot path is `src/trinote/infer_int/reference_bonsai.py`.

Already done:

- `forward(..., last_only=True)` exists and `bonsai_runtime.generate_bonsai_tokens` uses it.
- Bonsai artifact loading already uses lazy digest metadata in `artifact_io_bonsai.py`.
- The identity and receipt binding are correct.

Still missing:

- `BonsaiReferenceModel` has no KV cache. Each generated token calls:
  `model.forward(seq[-ctx:], last_only=True)`.
- `q1_linear_ref` repeatedly unpacks constant Q1_0 sign bits:
  `_unpack_q1_signs(b[lo:hi]).astype(np.int64)`.
- Receipt verification repeats a teacher-forced Bonsai forward after generation. If the fast path only
  speeds up generation but not verification, the demo still blocks.

The Q1_0 cost is different from the BitNet cost. For Bonsai, the obvious constant conversion miss is not
`w_codes.astype(float64)`; it is unpacking the same packed one-bit sign weights for every layer call, every
output chunk, every generated token, and again during verification.

## 3. Constraints

- No approximate kernels. If a fast path changes a logit by one integer unit, it is a new inference engine,
  not an optimization.
- Keep the current oracle path available. `q1_linear_ref`, `_attention_ref`, `_ffn_ref`, and `forward` are
  the correctness baseline.
- Fast generation and fast verification must both be available for the demo.
- Context-window sliding must fall back to the oracle or rebuild the cache. Bonsai's common demo path is
  short prompts, so the non-sliding case is enough for the first implementation.

## 4. Phase 0: Measure the Real Bonsai Bottleneck

Add `tools/bench_bonsai_speed.py` before changing kernels.

Measure:

- artifact load time
- prompt prefill time
- one-token native generation time
- two-token native generation time
- receipt verification time
- peak RSS
- self-time profile for `_unpack_q1_signs`, `q1_linear_ref`, `np.einsum`, `fixed_point_rmsnorm`,
  `fixed_point_softmax`, and `fixed_point_matmul`

Benchmark commands:

```bash
PYTHONPATH=src .venv/bin/python tools/bench_bonsai_speed.py \
  --artifact artifacts/model/atlas-notarized-bonsai-8b.safetensors \
  --gguf models/Bonsai-8B-Q1_0.gguf \
  --prompt "What is a Merkle tree?" \
  --n-new 2
```

Record baseline numbers in this file before and after each phase.

## 5. Phase 1: Bonsai KV Cache

Mirror the proven `ReferenceModelV2` structure, but adapt it to Qwen3 Bonsai:

- Qwen3 applies attention RMSNorm, then Q/K/V projections.
- Q and K each get per-head RMSNorm before RoPE.
- K cache stores post-QK-norm, post-RoPE K.
- V cache stores raw V heads.
- Attention for a new block of `M` positions attends to cached prefix plus the new block with a causal mask
  inside the new block.

Files:

- `src/trinote/infer_int/reference_bonsai.py`
- `src/trinote/infer_int/bonsai_runtime.py`
- `src/trinote/cli/run_bonsai_cli.py`

Implementation shape:

```python
class _BonsaiKVCache:
    def __init__(self, n_layers):
        self.k = [None] * n_layers      # (n_kv_heads, t, head_dim), post-RoPE int64
        self.v = [None] * n_layers      # (n_kv_heads, t, head_dim), raw int64
        self.t = 0

    def extend(self, li, kh, vh):
        self.k[li] = kh if self.k[li] is None else np.concatenate([self.k[li], kh], axis=1)
        self.v[li] = vh if self.v[li] is None else np.concatenate([self.v[li], vh], axis=1)
```

Add:

- `_attention_with_cache_bonsai(x_fp, layer, cfg, cos, sin, frac, cache, li, start, q1=...)`
- `BonsaiReferenceModel._run_layers(new_ids, cache)`
- `BonsaiReferenceModel.prefill_logits(token_ids)`
- `BonsaiReferenceModel.generate_cached(token_ids, n_new, pick, eos=None, on_token=None)`
- `BonsaiReferenceModel.generate_greedy_cached(...)`

Update:

- `generate_bonsai_tokens` should call `model.generate_cached(...)` when available.
- Keep an exact fallback to the current uncached loop when `len(prompt)+n_new` exceeds the effective context
  window.

Expected impact:

- Makes per-token latency flat instead of increasing with answer length.
- Helps longer turns more than one-token demos.
- Does not by itself solve the Q1_0 constant unpacking floor.

Acceptance tests:

- `test_bonsai_kv_cache_is_bit_identical`
- `test_bonsai_prefill_logits_match_forward_last_only`
- `test_bonsai_kv_cache_streaming_callback_matches`
- `test_bonsai_kv_cache_context_overflow_falls_back_exactly`
- Existing receipt tests must still pass.

## 6. Phase 2: Q1_0 Sign Cache

Add a fast Q1 strategy that caches unpacked signs once.

Current hot pattern:

```python
signs = _unpack_q1_signs(b[lo:hi]).astype(np.int64)
acc = np.einsum("tbi,obi->tob", xg, signs, optimize=True)
```

Fast pattern:

```python
layer["wq_signs_i8"] = _unpack_q1_signs(layer["wq_bits"])   # once
...
signs = layer["wq_signs_i8"][lo:hi]                         # int8
acc = np.einsum("tbi,obi->tob", xg, signs, optimize=True)    # returns int64 with xg int64
```

Do not cache signs as `int64`; that would expand the 8B model to roughly 65 GB. Cache `int8` signs. The
extra memory is roughly one byte per weight, around 8-9 GB for Bonsai layer/output weights, which is large
but plausible on this demo machine. Gate it by available RAM and expose a flag.

Add:

- `q1_linear_signs_ref(x_fp, signs_i8, scale_fp, frac, out_chunk=256)`
- `_q1_bl_ref(x_fp, layer, name, frac)` for oracle packed bits
- `_q1_bl_fast(x_fp, layer, name, frac)` for cached signs
- `BonsaiReferenceModel.enable_fast(check_ram=True, cache_output=True)`

Use the same `q1` strategy in:

- cached generation
- fast teacher-forced verification
- quality-gate native forward when requested

Keep default `forward` as the packed oracle unless we intentionally choose to let `forward` dispatch to the
fast strategy after the tests prove byte identity. The safer API is:

- `forward(...)`: oracle
- `forward_fast(...)`: cached signs/native kernel
- `teacher_forced_logits(...)`: uses fast if enabled, else oracle

Then update `infer_int.verify.teacher_forced_logits` to prefer `model.teacher_forced_logits(ids)` if the
method exists.

Expected impact:

- Removes repeated Q1 sign unpacking, the Bonsai analogue of `CACHING_2.md`.
- Should reduce both generation and receipt verification.
- Must be measured on the real model before making a speed claim.

Acceptance tests:

- `test_bonsai_q1_sign_cache_matches_oracle`
- `test_bonsai_fast_forward_matches_oracle_logits`
- `test_bonsai_fast_kv_cache_is_bit_identical`
- `test_bonsai_fast_receipt_reexecutes`

Worst-case kernel test:

- random signs/scales
- all-ones and alternating sign groups
- large fixed-point activations near the expected safe range
- shapes covering Q/K/V/O, FFN up/down, and output head dimensions

## 7. Phase 3: Fast Full-Turn Verification

The demo currently waits for generation and then waits again for receipt verification. We need to keep
verification, but it should use the same bit-exact fast kernels.

Change:

```python
def teacher_forced_logits(model, ids):
    if hasattr(model, "teacher_forced_logits"):
        return model.teacher_forced_logits(list(ids))
    return model.forward(list(ids))
```

In `BonsaiReferenceModel.teacher_forced_logits`:

- if fast signs/native kernel is enabled, use the fast teacher-forced path
- otherwise call `forward`

This does not change receipt semantics. A receipt commits token IDs, sampler settings, and hashes; it does
not commit whether the verifier used the slow packed path or a byte-identical fast path.

Acceptance:

- A receipt generated with fast cached decode verifies with:
  - slow oracle verification
  - fast teacher-forced verification
- Both produce the same `logitsDigest` values for every checked position.

## 8. Phase 4: Native Q1_0 Kernel

If Phase 2 is still too slow or too memory-heavy, implement the Q1_0 group-sum kernel natively.

Target operation:

```text
for output row o:
  y[o] = sum_blocks ((sum_i x[block, i] * sign[o, block, i]) * scale[o, block]) >> frac
```

Native options:

- C shared library loaded by `ctypes`
- C++ extension built by a small script under `tools/`
- Numba JIT if adding a runtime dependency is acceptable

The C path can read packed Q1 bits directly, avoiding the 8-9 GB sign cache. It should specialize the
decode case `T == 1` first, then support `T > 1` for prefill/verification.

Implementation notes:

- Use int64 accumulation to match the oracle.
- Keep right-shift timing identical: multiply each block sum by its block scale, shift by `frac`, then sum
  blocks.
- Parallelize over output rows.
- Keep output chunking so memory remains bounded.
- Preserve NumPy oracle fallback.

Acceptance:

- Native kernel equals `q1_linear_ref` byte-for-byte on synthetic cases.
- Native full Bonsai logits equal `forward` byte-for-byte on small artifacts.
- Real Bonsai smoke receipt verifies.
- `trinote-quality-gate-bonsai` remains `PASS`.

## 9. CLI and Demo Changes

Add flags to `trinote-run-bonsai`:

```text
--fast              enable RAM-gated fast Bonsai kernels
--no-fast           force oracle packed path
--fast-required     fail instead of falling back when fast kernels cannot be enabled
--bench             print per-stage timing: tokenize, load, prefill, per-token, verify, emit
```

Default recommendation for the demo:

```bash
./cli/trinote-run-bonsai --engine native --receipt --sampler greedy -n 8 --fast --fast-required --bench
```

The command should print:

- whether fast signs/native kernel was enabled
- estimated extra RAM
- generation time per token
- verification time
- ledger index and receipt hash

## 10. Rollout Order

1. Add `tools/bench_bonsai_speed.py` and record baseline.
2. Add Bonsai KV cache and tests.
3. Wire `bonsai_runtime.generate_bonsai_tokens` to `generate_cached`.
4. Add Q1 sign cache and tests.
5. Add fast teacher-forced verification hook.
6. Add `--fast`, `--fast-required`, and `--bench` CLI flags.
7. Re-run:
   - `PYTHONPATH=src .venv/bin/python -m pytest tests/test_bonsai_smoke.py -q`
   - `PYTHONPATH=src .venv/bin/python -m pytest -q`
   - `cli/trinote-quality-gate-bonsai --n-new 1 --ctx-size 512 --threads 16`
   - one real native receipt demo prompt
8. If per-token remains above the demo target, implement the native Q1 kernel.

## 11. Success Criteria

Minimum acceptable:

- Cached/fast Bonsai outputs are byte-identical to oracle outputs.
- Full receipts still verify.
- The Merkle-tree demo prompt completes generation and verification fast enough for live presentation.
- Right-side tmux panes continue to show full hashes and the new ledger row.

Stretch:

- Native receipt-bound Bonsai generation under 10 seconds/token.
- Full generation plus verification under 30 seconds for a short 4-token demo answer.
- Native Q1 kernel available without the 8-9 GB sign-cache RAM overhead.

## 12. Non-Goals

- Do not use PrismML `llama-cli` for receipt emission. It remains the fast raw GGUF path, but not the
  receipt-bound path.
- Do not change Q1_0 scales, fixed-point precision, RoPE tables, sampler behavior, or receipt schema.
- Do not mark an approximate or probabilistic verifier as `VERIFIED`.
- Do not remove the slow oracle path; it is the fallback and the correctness reference.

## 13. Implemented Status

Implemented:

- Bonsai KV-cached decode: `BonsaiReferenceModel.generate_cached`, `generate_greedy_cached`, and
  `prefill_logits`.
- Q1_0 sign-cache fast path: `enable_fast(check_ram=True)`, `forward_fast`, and `teacher_forced_logits`.
- Native packed-Q1_0 C kernel: `tools/bonsai_q1_kernel.c`, built by `tools/build_bonsai_q1_kernel.sh` into
  `tools/libbonsai_q1_kernel.so`; enabled by `BonsaiReferenceModel.enable_native`.
- Native packed-Q1 workspace reuse and same-input prepared-LUT reuse for QKV and FFN gate/up projections.
- Native greedy LM-head argmax for unpenalized greedy decode and cached receipt replay, avoiding materializing
  the full logits row.
- Experimental grouped prepared multi-projection kernel, gated behind `TRINOTE_Q1_PREPARED_MULTI=1` because it
  was neutral/slower on the local demo benchmark.
- Receipt verification hook: `infer_int.verify.teacher_forced_logits` uses a model-provided
  `teacher_forced_logits` method when present.
- Runtime/CLI wiring: `generate_bonsai_tokens` uses cached decode, and `trinote-run-bonsai --fast` prefers the
  native packed-Q1 kernel, falling back to the RAM-gated sign cache.
- Benchmarking: `tools/bench_bonsai_speed.py`.
- Optional narrow **int32 Q1 scale cache** (opt-in via `TRINOTE_Q1_SCALE_CACHE=1`, default off):
  `BonsaiReferenceModel.enable_scale_cache()` builds a native-only int32 cache of every Q1 scale array when
  all values fit int32 (all-or-nothing range guard); the committed int64 artifact is unchanged and the int64
  oracle stays the fallback. All Q1 kernels in `tools/bonsai_q1_kernel.c` now share one macro per-element
  helper (`q1_element_s64`/`q1_element_s32`), so the `*_scale32` kernels are byte-identical to the int64 path
  for in-range scales. This narrows scale **storage** only — the accumulator stays `uint64` and each scale is
  promoted to a 64-bit operand before the multiply, consistent with the "no int32 partial sums" rule in
  `docs/architecture/PERFORMANCE.md`. Byte-exact parity, the int128 RMSNorm envelope, thread-invariance,
  LUT-wrap, and native-verifier receipt replay are covered in `tests/test_bonsai_smoke.py`.

Measured on the real Bonsai artifact, prompt `Hi`:

| path | generation | verification/emission | peak RSS |
|---|---:|---:|---:|
| NumPy sign-cache fast path | ~33.0s/token | ~32.8s | ~10.0GB |
| Native packed-Q1, benchmark | ~20.6s/token | ~22.2s | ~3.1GB |
| Native packed-Q1, `trinote-run-bonsai --threads 8 --bench` | ~16.5s/token | ~19.3s | not reported |
| Native workspace + argmax, 64-token receipt | 28.495s total, 0.445s/token | 31.240s | ~3.1GB |
| Native prepared-LUT default, in-process 8-token A/B | 3.714s total vs 3.765s with prepared reuse disabled | n/a | ~3.1GB |
| Native prepared-LUT default, 64-token receipt | 29.347s total, 0.459s/token | 30.738s | ~3.1GB |
| Opt-in grouped prepared multi-projection | 29.887s total, 0.467s/token | 30.943s | ~3.1GB |
| int32 scale cache A/B, 8 threads, 4x24-tok interleaved | steady 348.0 ms/tok (off) vs 346.4 ms/tok (on), ~1.005x | n/a | n/a |
| native M=1 attention A/B, 8 threads, interleaved | L~109: 422 ms/tok (off) vs 330 ms/tok (on), 1.28x (grows w/ context) | n/a | n/a |

The int32 scale-cache A/B (2026-06-23) is determinism-safe — greedy output IDs are byte-identical cache on
vs off on the real 8B artifact — but the speedup is within noise (~0.5%). Q1 scale bandwidth is not the M=1
decode bottleneck (the packed bits are ~2x the scale bytes and the activation-LUT gather is 16 lookups per
scale read), so the uint16 tightening is deprioritized; the cache stays opt-in and off by default.

The native-side microprofile `tools/profile_bonsai_decode.py` (2026-06-23, M=1 decode, ~352 ms/tok) splits
the per-token cost: **Q1 apply (bit-gather + accumulate) 67%**, attention softmax 8%, attention matmul 8%,
output argmax 5%, SiLU 4%, RMSNorm 3%, **Q1 LUT build only ~1%**, RoPE 0.5%. Q1 apply is dominated by the FFN
projections (`w1`/`wu`/`w2` ≈ 80% of apply). This rules out a `tokens==1` LUT-avoidance kernel (LUT build is
~1%). Two follow-up gate probes then showed the Q1 apply gather itself is near-optimal for the portable
`x86-64-v2` build: narrowing the LUT entries `uint64`→`int32` gives ~1.0x on the biggest projection (`w2`,
0.999x) and only ~1.1x on smaller ones (the gather is **L3-latency-bound, not bandwidth-bound**), and a
direct branchless signed-dot is 1.5–3.3x **slower** (128 ops/block, SSE2-only at this ISA, lose to 16 L3
gathers). So no int32-LUT kernel was built. Remaining Q1 speedup needs a wider-ISA gather (a per-host opt-in
AVX2/AVX-512 build — determinism-safe but a distribution change), or a different strategy.

**Native M=1 cached-decode attention** (`bonsai_attention_decode_i64`, default-on under `enable_native`,
`TRINOTE_NATIVE_ATTN=0` opt-out) is the biggest realized decode win. It ports the integer softmax byte-exactly,
reads the KV cache in place (per-kv stride, no per-token copy), preserves the fail-loud overflow contract,
and is used only for the M=1 step (mask all-false); prefill and `forward()` keep the NumPy path. Measured on
the real 8B (thermal-robust interleaved A/B, byte-identical IDs): **~1.28x decode at context L~109, growing
with context** (1.20x@L37 → 1.34x@L181; the isolated attention math is 10-51x, so the end-to-end win keeps
climbing — ~1.7x@L512, ~2.5x@L2048 extrapolated). Adversarially verified (5 agents); the pass also caught and
fixed a latent fail-loud bound-check overflow (division-form fix, regression-tested).

Three further kernels followed (design docs in `~/research/bonsai-notarized/optimization-scopes/`), all
byte-exact and adversarially verified:
- **Native SiLU** (`bonsai_silu_i64`, `TRINOTE_NATIVE_SILU` default-on): reuses the integer-softmax helpers;
  **10.96× on the SiLU bucket → ~3.4% of decode**. The second realized decode win.
- **int32 activation-LUT entries** (`TRINOTE_Q1_LUT32`, opt-in/default-off): halves the Q1 gather data, with
  a per-lane range guard + uint64-LUT fallback. ~flat on this CPU (the apply gather is L3-latency-bound);
  best on the vocab head. The output **argmax** routes through the int32-LUT argmax kernel under the same flag.
- **AVX2/AVX-512 gather**: gate-probed and NOT merged — AVX2 `VPGATHERQQ` is byte-exact but ~0.84–0.90× (a
  regression) on Comet Lake; AVX-512 untestable here. Design kept build-ready for AVX-512 hardware.
- **GPU (CUDA) Q1 kernel** (`TRINOTE_GPU`, per-host opt-in / default-off): the "different strategy" the
  closer below anticipates — the Q1 applies plus a resident-monolith prefill run on the GPU, byte-identical
  to the CPU oracle (a *producer*; the CPU oracle stays the canonical verifier). Working-tree, arch-specific,
  gitignored `.so`. See [../architecture/GPU-INTEGER-KERNEL.md](../architecture/GPU-INTEGER-KERNEL.md).

This is a large improvement over the observed demo baseline of about `100s/token`, and it avoids the
RAM-heavy sign cache. It is still a CPU reference verifier, not a fast serving engine. The latest profile
shows prepared-LUT reuse is only a small win and grouped calls do not move the floor. Further speed now needs
deeper Q1 apply work: a specialized `tokens == 1` decode kernel, SIMD over packed bits, or a different
block-sum strategy. Output argmax is not a material bottleneck for greedy decode.
