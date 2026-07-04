# SAMPLER-INTEGER.md — the fully-integer seeded sampler (every mode receipt-bound)

> **Status: implemented.** `RECEIPT_SAFE_MODES = {greedy, temp, top_k, top_p}`. A seeded
> `temp`/`top_k`/`top_p` turn is re-executable bit-for-bit, so it can emit and verify a receipt exactly
> like greedy.
>
> **Scope of THIS extraction.** The only bundled test is `tests/test_bonsai_smoke.py`. It now covers
> **greedy** receipts (re-execution + the receipt-runner dispatch), a **seeded `temp` receipt
> round-trip** (`test_bonsai_seeded_receipt_reexecutes`), and the **tampered-output forgery** case
> (`test_bonsai_tampered_output_fails_verification`). A dedicated `top_k`/`top_p` and direct
> `draw_uniform_int`/Lemire-uniformity unit test are not yet bundled. The `tests/test_notarized_smoke.py`
> referenced below is the parent-repo (`ATLAS-Notarized-BitNet`) suite and is not shipped here.
> (The end-to-end `trinote-run-notarized-v2` run against the 2.4B model is
> likewise parent-repo; the shipped runner is `cli/trinote-run-bonsai`.)

## 1. The problem

Greedy used to be the *only* receipt-bound sampler, because re-executability requires a forward pass that
re-derives the **same token** on any machine (via the portable pure-NumPy oracle — no `.so`/`fcntl`
needed; see [DETERMINISM.md](DETERMINISM.md) "Platform scope"). Greedy clears that bar trivially (integer
`argmax` over committed fixed-point logits — no RNG, no float). The old exploration path did not, for three reasons
(all in `infer_int/sampler.py`):

1. **temperature** was a float divide, `rint(logits / T)`;
2. the **draw** was `int(rng.random() * total)` — a float multiply over numpy's Philox *double* stream;
3. that tied determinism to numpy's RNG-stream/double-conversion contract, not to integer math.

None of these is a *reduction* or a *transcendental*, so they were probably deterministic on IEEE
hardware — but "probably, for a fixed numpy version" is exactly the kind of soft guarantee the
determinism keystone ([`DETERMINISM.md`](DETERMINISM.md)) refuses to rest a receipt on. So sampling was recorded but
marked `receiptBound=False`.

## 2. The change — three edits, all making the hot path integer

The per-token path is now **100% integer**. The only floating-point left is two *scalar*,
correctly-rounded, once-per-turn conversions (`inv_temp_fp`, `top_p_fp`), which are deterministic across
IEEE platforms (a single divide/multiply + round-half-to-even — neither a reduction nor a transcendental).

### (1) Committed fixed-point inverse-temperature
`inv_temp_fp(T, frac) = round(2^frac / T)` (a committed integer). Temperature scaling becomes
`(_apply_temp_fp)`: `(logit * inv_temp_fp) >> frac` — pure integer, arithmetic-shift floor. `T == 1.0` →
`inv_temp_fp == 2^frac`, an exact identity (no rounding). Overflow fails **loud** (`OverflowError`)
rather than silently wrapping. `top_p` is likewise canonicalized to a committed integer threshold
`top_p_fp(p, frac) = round(p · 2^frac)` (probabilities sum to ~`2^frac`).

### (2) Reuse the existing integer softmax + truncation
`fixed_point_softmax` and `_truncate` (top-k / nucleus) were already pure integer (stable argsort +
integer `cumsum`/`searchsorted`). `_truncate` now takes the integer `top_p_fp` directly, so the last
float (the `round(top_p · 2^frac)` inside the loop) is gone.

### (3) Integer uniform draw — counter-based PRNG + Lemire
`draw_uniform_int(total, seed, position)` returns an unbiased integer in `[0, total)` with **no float**:

- **PRNG word** — `_prng_word(seed, position, counter)` = the top 64 bits of
  `SHA-256("trinote-sampler-draw/v1" ‖ seed ‖ position ‖ counter)` (each field a big-endian `uint64`).
- **Bounded reduction** — Lemire's multiply-shift: `m = word * n; return m >> 64`, with the standard
  rejection on `m & (2^64−1) < (2^64 mod n)` for exact unbiasedness (the loop is essentially never taken
  for `n ≈ 2^16`, and consumes successive `counter` words, staying deterministic).

The result feeds the unchanged selection rule `searchsorted(cumsum(probs), target, side="right")`.

> **Why SHA-256 instead of Philox** (the suggestion was "a raw Philox 64-bit word"). SHA-256 is already
> the determinism *bedrock* of this project — every commitment, ledger link, and `logitsDigest` assumes
> it is byte-identical on every machine and every version. A counter-mode PRNG built on it therefore adds
> **zero new dependency** and inherits the strongest possible portability guarantee, stronger than
> numpy's Philox stream (a library-version contract). Lemire's reduction is applied identically. (The
> separate `verify_sampled` *audit* — which only picks *which* positions to spot-check — still uses numpy
> Philox; that choice is not determinism-critical because the check itself is the integer re-derivation.)

## 3. Commitment & re-execution

A receipt already commits the sampler block — `mode`, `temperature`, `topK`, `topP`, `seed`, `repPenalty`,
`noRepeatNgram` (`receipts/receipt.py::sampler_to_block`) — and the PRNG is keyed by **(seed, absolute
position)**. `position` is the index of the token being generated: `ReferenceModelV2.generate_cached`
passes `position = len(seq)` and `history = seq` to `pick(row, position, history)`. So a verifier with the
input ids, the committed output ids, and the committed sampler can replay the exact draws.

`infer_int/verify.py::verify_resample` does this (teacher-forced, one prefill when the turn fits the
window): at each output position `i` it re-derives
`sample_token(predicting_row, cfg, position = len(input)+i, frac, history = input + output[:i])` and
checks it equals the committed token. `receipts/verify.py::verify_receipt` dispatches the re-execution
step on the committed mode — `greedy → verify_greedy`, else `verify_resample` with
`sampler_config_from_block(committed_block)`. `receipt_bound` is now `mode in RECEIPT_SAFE_MODES`.

This is exactly the greedy teacher-forcing argument, generalized: in a causal model the row predicting
output `i` depends only on the committed prefix, and the draw at `i` is a deterministic function of
`(that row, seed, i, prefix)` — so a single prefill over `input+output` yields every row, and the
integer draw re-selects each token. Greedy is just the special case where the "draw" is `argmax`.

## 4. Backward compatibility & a fixed latent bug

- **Greedy receipts are byte-identical to before.** `greedy ∈ RECEIPT_SAFE_MODES`, the sampler block
  schema is unchanged, and `receipt_bound` is still `True` for greedy. No `trinote.receipt/v1` schema bump.
- **No receipt-schema change.** The inverse-temperature is *committed via* the receipt's `temperature`
  field + a pinned `round()` conversion shared by producer and verifier; nothing new is stored.
- **Fixed:** the runner's `_emit_and_verify` (parent repo, not bundled) previously hard-coded
  `sampler={"mode":"greedy"}`, so a greedy turn run *with a repetition penalty* (or any sampled turn)
  would have recorded the wrong sampler and failed to re-derive. It now records the real `cfg` and binds
  `model_digest` to `modelHash`. The shipped runner is `cli/trinote-run-bonsai`
  (`src/trinote/cli/run_bonsai_cli.py`).

## 5. What is float-free (and what is not)

| Step (per token) | Arithmetic |
|---|---|
| repetition penalty / n-gram ban | integer (`apply_rep_penalty`) |
| temperature scaling | integer `(logit · inv_temp_fp) >> frac` |
| softmax | integer (`fixed_point_softmax`) |
| top-k / top-p truncation | integer (stable argsort + `cumsum`/`searchsorted`) |
| uniform draw | integer (SHA-256 word + Lemire) |
| token selection | integer (`cumsum`/`searchsorted`) |

| Per **turn** (once) | Arithmetic | Determinism |
|---|---|---|
| `inv_temp_fp = round(2^frac / T)` | one IEEE divide + round | correctly-rounded scalar → cross-platform exact |
| `top_p_fp = round(p · 2^frac)` | one IEEE multiply + round | correctly-rounded scalar → cross-platform exact |

The one external primitive is **SHA-256** (already assumed bit-exact everywhere). There is no `libm`
transcendental and no float reduction anywhere in the sampling path.

## 6. Files changed
- `src/trinote/infer_int/sampler.py` — `inv_temp_fp`, `top_p_fp`, `_apply_temp_fp`, `_prng_word`,
  `draw_uniform_int`, integer `_probs_fp`/`_truncate`/`sample_token`, `sampler_config_from_block`,
  expanded `RECEIPT_SAFE_MODES`.
- `src/trinote/infer_int/verify.py` — `verify_resample` (replay the committed sampler).
- `src/trinote/receipts/receipt.py` — `receipt_bound = mode in RECEIPT_SAFE_MODES`.
- `src/trinote/receipts/verify.py` — dispatch re-execution by mode.
- the notarized runner CLI (parent repo, not bundled) — record the real sampler + bind `model_digest`;
  the shipped runner is `src/trinote/cli/run_bonsai_cli.py`.
- `tests/test_notarized_smoke.py` — acceptance tests (below; **parent-repo suite, not bundled here** —
  see the status banner; the shipped suite is `tests/test_bonsai_smoke.py`).

## 7. Acceptance tests (parent-repo `tests/test_notarized_smoke.py`; the seeded round-trip + forgery cases are now bundled in `tests/test_bonsai_smoke.py`, the rest below remain parent-repo)
- `test_all_sampler_modes_are_receipt_safe` — the four modes are in `RECEIPT_SAFE_MODES`.
- `test_integer_draw_is_deterministic_and_bounded` — `draw_uniform_int` is deterministic, in `[0,total)`,
  and varies with position.
- `test_integer_temperature_is_exact_no_float_divide` — `T=1` identity, `÷0.5 == ×2` in integer.
- `test_sample_token_draws_across_the_distribution` — a flat row samples ≥2 ids, never out of range.
- `test_seeded_sampling_is_reproducible` — same seed → identical continuation.
- `test_seeded_sampled_receipt_is_bound_and_reexecutes` — temp/top_k/top_p turns are `receiptBound` and
  re-derive (`reexec.ok`, `strategy="resample-*"`).
- `test_tampered_sampled_output_fails_reexecution` — flipping one token breaks re-execution.

## 8. Randomized seeds & on-chain recording

A randomized seed is just a randomly-**chosen but then committed** integer — outputs vary run-to-run, yet
each turn stays re-executable because the draw is a pure function of `(committed seed, position, committed
prefix)`. There is no hidden entropy; the only variation is the seed, and it is recorded.

- `trinote-run-bonsai --random-seed` draws a fresh 64-bit seed via `secrets.randbits(64)`, prints it
  (`[run] random seed = …`), and threads it into the one `SamplerConfig` used for *both* generation and
  the receipt — so the committed seed always equals the one that produced the output. (No effect under
  greedy, which ignores the seed.) Default runs still use `--seed 0` (identical every time).
- **On-chain.** The seed is recorded in the third-entry artifact: `chain_artifact` (now
  `trinote.chain-receipt/v2`) carries `samplerMode` + `seed` alongside `modelHash`/`receiptHash`. So the
  notarized publish payload states *how* the output was drawn, not only *that* it was. The seed was
  already bound transitively via `receiptHash` (recompute it from the preimage to prove the on-chain seed
  is the one that produced the output); v2 surfaces it directly. Caveat: the literal BSV
  OP_RETURN bytes are built by the `chain_c` builder (default-OFF — see
  [`../receipts/RECEIPTS.md`](../receipts/RECEIPTS.md) "Scope"); to put `seed` in those bytes that builder
  must encode the field. The default
  network-free log backend already records the full v2 artifact, seed included.
- Effective seed space is 64-bit (`seed & 0xFFFF…`); a verifier masks identically, so re-execution is
  unaffected. Negative seeds are handled deterministically (two's-complement low 64 bits).

## 9. Honest scope
- This binds *re-executability*, not quality. A seeded sampled turn is reproducible and receipt-bound; it
  is not "more correct" than greedy. Greedy remains the determinism floor (no RNG at all).
- Re-derivation requires the committed seed; a turn drawn from system entropy with an unrecorded seed
  would not be re-executable (the CLI always commits its `--seed`, default 0).
- Cross-platform exactness rests on SHA-256 (bedrock) and on the two scalar `round()` conversions being
  IEEE-correctly-rounded — both far stronger than the previous float-RNG-stream assumption.
