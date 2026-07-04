# GPU Integer Kernel (per-host opt-in)

The Bonsai engine has an optional CUDA backend for the Q1_0 path. It is a **producer accelerator only**: the
int64 CPU oracle (`reference_bonsai.q1_linear_ref` / `tools/bonsai_q1_kernel.c`) remains the canonical
**verifier**, and a GPU-produced receipt is required to re-execute bit-for-bit on a CPU-only host. This doc
is the device analogue of [`PERFORMANCE.md`](PERFORMANCE.md); see [`INFERENCE-ENGINE.md`](INFERENCE-ENGINE.md)
for how it fits among the four backends and [`DETERMINISM.md`](DETERMINISM.md) for the bit-exactness contract.

> **Status — working-tree, not the committed default.** The GPU support (`tools/bonsai_q1_gpu.cu`,
> `tools/build_bonsai_q1_gpu.sh`, `src/trinote/infer_int/gpu_native.py`, `tests/test_bonsai_gpu.py`, and the
> GPU dispatch in `reference_bonsai.py`) currently lives in the working tree. The built
> `tools/libbonsai_q1_gpu.so` is **`.gitignore`d** because it is arch-specific and non-portable. A clean
> clone therefore has no `.so`: `gpu_available()` is `False` and `--gpu` runs on the CPU oracle unchanged.
> The committed runtime stays pure NumPy + safetensors (no torch, no GPU dependency); this kernel is built
> per host with `nvcc`, not pulled in by pip.

## 1. What runs on the GPU

Two environment toggles, both **default OFF** (parsed as `{1,true,yes,on}`):

- **`TRINOTE_GPU=1`** (`_gpu_enabled()`) — routes the **Q1 linear applies** (every per-layer projection
  wq/wk/wv/wo/w1/wu/w2 and the output head) through the GPU, and enables the **M3 true-residency monolith**
  for prefill. This is the **proven applies-only win**.
- **`TRINOTE_GPU_FULL=1`** (`_gpu_full_enabled()`) — *additionally* routes RMSNorm and prefill-attention to
  the GPU via per-op dispatch. Both toggles are **byte-exact** (the difference is performance, not
  correctness). FULL is off by default because the per-op host↔device transfers **regress** prefill (a
  recorded ~7.8 s vs ~4.9 s @ T=64) until the activation stays device-resident; it exists for the
  end-to-end byte-exact gate and as the residency foundation. **Use `TRINOTE_GPU`, not `_FULL`.**

**Weight residency.** Each weight is registered on the device *once* (the handle cached on the owner), so
only activations move per call; the degradation chain is resident → per-call upload → `None` (CPU).

**M3 monolith.** `bonsai_prefill_forward_gpu` runs the *entire* prefill forward on-device — the residual `x`
lives in a device buffer for the whole pass (no per-op host↔device round-trips), and it optionally exports a
byte-identical KV cache so generative decode continues on CPU. The output-head argmax has no fused GPU kernel
yet; under GPU it computes GPU logits then `np.argmax`, whose first-max tie-break matches the committed
lowest-index rule.

The **DP4A** kernels in the `.cu` (int8 dot-product reformulation) are exercised only by the parity gate and
microbenches — they are **not wired into the production forward**, which uses the direct int64
`q1_linear_kernel` via residency. Do not attribute any production receipt to DP4A.

## 2. How it stays byte-identical to the CPU oracle

The CPU invariants are reproduced verbatim in the kernel header (`bonsai_q1_gpu.cu:16-18`) and hold across
the NumPy oracle, the C kernel, and the CUDA kernel:

- **I3 — per-block int64 scale**, applied before any shift.
- **I4 — per-block arithmetic-right-shift (floor toward −∞) THEN sum** — never floor-once, and **never** a
  CUDA signed `>>` (implementation-defined) or truncating division (wrong for negatives). `arshift_i64_floor`
  is an exact port of the C `arshift_i64`.
- **I5 — order-free integer reductions.** One warp per output `(t,o)`; the 32 lanes split the 128-weight block
  and warp-reduce with `__shfl_down_sync`. Integer add is exactly associative, so the tree order is
  bit-identical to the CPU's serial 128-element sum.
- **mod-2⁶⁴ wrap, no accumulator wider than 64 bits** (the Q1 linear's deliberate wrap-not-raise policy),
  with the single documented exception below.
- **RMSNorm via device `__int128`** — a near-verbatim port of the C `bonsai_rmsnorm_i64`: 128-bit
  sum-of-squares (the residual stream across the layers exceeds int64), bit-exact integer `isqrt`, and
  floor-division toward −∞. It **declines (`rc=4`)** exactly when the CPU kernel would, handing back to the
  unbounded big-int NumPy oracle — a *lockstep refuse*, never a silent value.
- **Integer softmax** in attention uses the same fixed-point `2^-u` polynomial as the CPU (`g_exp2_neg_fixed`,
  identical constants), with **no `expf`**; the float-dependent scalars (`inv_sqrt_fp`, `log2e`, `d_clip`) are
  computed host-side and passed in (no on-device `sqrtf`). Its `Q@Kᵀ`/`probs@V` overflow bounds are written in
  division-form so the 128-bit guard cannot itself wrap.
- **No fast-math** — the build deliberately omits `--use_fast_math`/`-ffast-math` (which would reorder
  arithmetic).

The parity gate `tests/test_bonsai_gpu.py` asserts `np.array_equal(gpu, cpu_oracle)` across shapes (incl.
K=12288 and the int64 wrap boundary), resident applies, an end-to-end forward (asserting the GPU path
actually ran), RMSNorm + `rc=4` lockstep-refuse, prefill-attention, the FULL end-to-end path, the resident
monolith across `T`, DP4A for L=4/−4/8, KV-export seeding, and `generate_cached`/`generate_batched`. It
**skips cleanly on CPU-only hosts**, so a green CI run is *not* proof of parity — the gate must be run on a
GPU box (it "must pass before `--gpu` is permitted to emit a receipt").

## 3. Build

```bash
# arch defaults to sm_86 (Ampere, e.g. RTX 3090); override CUDA_ARCH for other GPUs.
CUDA_ARCH=sm_86 bash tools/build_bonsai_q1_gpu.sh      # sm_89 Ada/L4/4090, sm_80 A100, sm_90 H100
# raw line it execs:  nvcc -O3 -arch=$CUDA_ARCH -Xcompiler -fPIC -shared tools/bonsai_q1_gpu.cu -o tools/libbonsai_q1_gpu.so
```

- Compiles **for this host's GPU only**; the arch is pinned explicitly (no `-arch=native`) so a wrong-arch
  `.so` cannot ship. **The arch must match the deploying GPU** — a mismatched `.so` fails to load and the
  path silently falls back to CPU. `sm_86` is the default, not universal.
- Requires **`nvcc`** → build on a CUDA **`-devel`** image; a `-runtime` image has no compiler and the script
  exits 1 (CPU path stays). The `.so` static-links the CUDA runtime, so at *run* time only the NVIDIA driver
  is needed. Verify with `nvcc --version`, `nvidia-smi`, and `cuobjdump tools/libbonsai_q1_gpu.so | grep arch`.

## 4. Availability & fallback

- **C probe** `bonsai_gpu_available()` returns `0` (good) iff `cudaGetDeviceCount` finds ≥1 device.
- **Python** `_load_lib()` returns `None` (→ CPU) when the `.so` is absent, fails `ctypes.CDLL` (missing CUDA
  runtime / wrong arch), lacks the core symbol, or the probe reports no device. Optional symbol groups load
  under guarded `try/except` so an older `.so` still loads. `gpu_available()` is `_load_lib() is not None`.
- **Every entry point returns `None` on `rc != 0`** (overflow or launch failure) and callers fall through to
  the native/oracle CPU path. The C return codes (0 ok, 1 bad-args, 2 attention-overflow, 4 rmsnorm-overflow)
  make the GPU decline *exactly* where the CPU oracle would refuse.

## 5. Enabling it

```bash
# Plain native CLI: --gpu wires TRINOTE_GPU=1 (needs --fast); the GPU accelerates the *native* engine.
PYTHONPATH=src .venv/bin/python -m trinote.cli.run_bonsai_cli --fast --gpu --receipt -p '...'

# JSON / receipt mode: --gpu is IGNORED here — set the env var yourself (see caveat).
TRINOTE_GPU=1 PYTHONPATH=src .venv/bin/python -m trinote.cli.run_bonsai_cli --json --fast -p '...'
```

- **`--gpu` needs `--fast`.** Without `--fast` the GPU accelerates nothing (it accelerates the native engine)
  and the CLI prints a no-effect note. `enable_native()` sets both `_native` and `_fast`, and the GPU apply is
  only reached on the native Q1 path.
- **`--json` caveat (important).** The `--json` flag dispatches to `run_json` *before* `_run_native`, and
  `run_json` calls `enable_native()` but **never sets `TRINOTE_GPU`**. So in JSON/receipt mode `--gpu` does
  *not* enable the GPU — you must export `TRINOTE_GPU=1` in the environment. The receipted benchmark runner
  (`tools/bonsai_receipt_bench.py --gpu`) does exactly this (and uses `TRINOTE_GPU`, the applies-only win,
  not `_FULL`); the deploy fleet wires it end-to-end (see `~/.local/trinote/deploy`).

## 6. Receipt safety

Because every GPU result is byte-identical to `q1_linear_ref` (or declines), a receipt produced on the GPU
re-executes bit-for-bit on a CPU-only verifier. Verification does **not** trust the producing path — it
recomputes `outputCommit` from the raw ids and re-derives each token as the argmax of its logits row — so a
hypothetical GPU divergence would *fail* verification (or be a clean `None`→CPU decline) rather than be
signed. In the default `--verify-mode fast-local`, in-process verification reuses the same GPU-enabled model;
`--verify-mode fresh-oracle` re-verifies on a separate pure-NumPy oracle that never touches the GPU. Either
way the cross-machine trustless guarantee is the portable CPU oracle, not the GPU re-execution.
