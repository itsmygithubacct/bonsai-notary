# Fail-closed GPU receipt acceptance

`scripts/accept-gpu.py` is the supported Bonsai-27B release gate. It never broadcasts or rents hardware. Run it
on an already provisioned host and pass the CPU cores actually contracted from the provider—not a larger
host-visible `nproc` value:

```bash
./scripts/accept-gpu.py \
  --cpu-threads 20 \
  --record-dir "$BONSAI_NOTARY_HOME/acceptance/2026-07-22"
```

Add `--record-media` to capture the same acceptance run as a fixed 120×36 asciinema session. Recording is
strictly opt-in, requires both `--record-dir` and `asciinema`, and does not alter phase commands or their order.
The wrapper records the child status separately because asciinema 2 does not propagate it; the acceptance
command's first nonzero status remains the final status even if later media publication also fails.

Add `--verifier-policy policy.json --verifier-policy-evidence benchmark.json` to gate replay through an engine
benchmark's artifact/thread-bound `receipt-verifier-policy/v1`. Both are required together: the pinned engine
refuses a policy that arrives without the complete benchmark report it was generated from, and recomputes that
report's release matrix and winners before the policy may route anything. Supplying one alone is rejected in
`prerequisites`, before any phase runs, rather than surfacing later as a failed verification phase.

A policy/thread/artifact mismatch fails; the selected route and both document digests are recorded in evidence,
and the policy and its benchmark are published to `verification/verifier-policy.json` and
`verification/verifier-policy-evidence.json`. A generated policy also fails outside its measured input/output
token-count points. Without a policy, the engine's exact full-replay `auto` route remains in effect.

The runner sets `BONSAI_CPU_THREADS`, OpenMP, OpenBLAS, MKL, BLIS, VecLib, NumExpr, and the fresh-oracle
`TRINOTE_ORACLE_Q1_THREADS` bound to the same positive value. It then runs these dependent phases in order and
stops at the first failure:

1. required files, signing roles, `nvidia-smi`, a clean notary checkout, and clean engine/chain/third-entry
   checkouts whose commits exactly match `dependencies.lock`;
2. CUDA identity and the engine availability probe;
3. both engine CUDA parity suites in one process;
4. distinct public signer extraction without printing either private key;
5. one fixed-prompt, one-token receipt with engine `--require-gpu`;
6. validation of the engine-owned `receipt-run/v1` producer report, including actual residency, GPU close,
   all seven thread environment values, and `resources.oracleQ1Workers` equal to `--cpu-threads`;
7. discovery and content-addressed copy of the generated portable bundle;
8. offline + full re-execution with both signer identities pinned;
9. validation of the independent verifier `receipt-run/v1` report.

Every subprocess runs without `--onchain`. A missing library, unsuitable architecture, memory refusal, poisoned
context, runtime range guard, dirty/mismatched source composition, CPU fallback, parity failure, missing bundle, unpinned signature, sampled replay,
memory-proof peak above the default 7.5 GiB ceiling, or cleanup failure returns nonzero. The ceiling can be
changed explicitly with `--max-gpu-proof-bytes` for a separately defined device envelope. The runner records a command's exit code before attempting evidence
publication, so a later sanitizer error cannot overwrite the first failure. Every phase subprocess has a hard
timeout (one hour by default, configurable with `--command-timeout-seconds`); long phases emit progress every
15 seconds by default.

The signature gate requires both receipt signatures to exist and requires the raw engine result to report
`sigModelOk`, `sigCounterpartyOk`, `sigModelAuthenticated`, and `sigCounterpartyAuthenticated` as true. Merely
passing two expected-key arguments never satisfies the gate by itself.

## Evidence directory

`--record-dir` is opt-in and must name an empty directory. Without it, the same gate runs in a private temporary
directory and prints only the final structured record. A persisted run has this stable layout:

```text
record-dir/
  raw/                 private command output, engine reports, and optional raw cast (excluded from checksums)
  public/              sanitized logs/report views and optional public media
  bundle/              portable receipt bundle
  verification/        sanitized pinned replay result
  manifest.json        receipt-run/v1 aggregate with phase timing and first failure
  SHA256SUMS           every non-raw evidence file, including manifest.json
```

`manifest.json` distinguishes two kinds of statement. Bundle hashes, receipt commitments, signatures, and
re-execution outcomes are cryptographically checked. GPU name, driver, timings, memory observations, and source
checkout revisions are operator observations; the manifest explicitly does not elevate them into receipt
claims. The engine reports do prove which code path that process says it selected and are acceptance-gated, but
remain non-consensus operational sidecars.

Source evidence never equates dirty bytes with a commit. A clean tree records `revision`; a dirty tree records
`revision: null`, `treeState: "dirty"`, and the last committed value only as `baseCommit`. Passing acceptance
requires all trees clean, so a dirty source record can appear only in a failed manifest.

Raw output may contain private paths or provider facts. The public sanitizer redacts home/state/workspace paths,
WIFs, private-key and mnemonic fields, bearer/OAuth tokens, signed download URLs, and provider SSH endpoints,
then rescans before publication. Keep `raw/` private even after a passing scan of `public/`.

### Optional terminal media

```bash
./scripts/accept-gpu.py \
  --cpu-threads 20 \
  --record-dir "$BONSAI_NOTARY_HOME/acceptance/2026-07-22" \
  --record-media
```

The private source is `raw/acceptance.cast`. Publication normalizes only private header metadata and terminal
text; event order and timestamps remain unchanged in `public/acceptance.cast`. WIF, mnemonic, private-key,
OAuth, provider-host, or unredacted absolute host-path patterns reject public media before a renderer runs.
The scan compares a color-control-normalized view and rejects unsupported terminal controls so ANSI sequences
cannot divide a forbidden value.
The source cast is never placed in `SHA256SUMS`, but its digest is recorded in
`public/media-manifest.json`.

When both `agg` and `ffmpeg` are available, the runner also creates `public/acceptance.gif` and
`public/acceptance.mp4`. The playback idle cap and speed apply only to rendering, never recording or the public
cast. If either renderer is absent, automatic mode keeps the sanitized cast and records a skipped render;
`--require-media-render` instead fails before acceptance begins. The public cast, renders, media manifest, and
aggregate manifest are included in `SHA256SUMS`.

## Producer/verifier separation and node operations

For asynchronous CPU verification and batches, use the distinct pending/signed-response workflow documented in
[`RECEIPT-BUNDLE.md`](../receipts/RECEIPT-BUNDLE.md#asynchronous-producerverifier-handoff). It never calls a
pending artifact verified. For externally provisioned acceptance nodes, the reviewed provider-adapter state
machine and its explicit billing/teardown interlocks are documented in
[`operations/README.md`](../../operations/README.md).
