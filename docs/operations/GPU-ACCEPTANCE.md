# Fail-closed GPU receipt acceptance

`scripts/accept-gpu.py` is the supported Bonsai-27B release gate. It never broadcasts or rents hardware. Run it
on an already provisioned host and pass the CPU cores actually contracted from the provider—not a larger
host-visible `nproc` value:

```bash
./scripts/accept-gpu.py \
  --cpu-threads 20 \
  --record-dir "$BONSAI_NOTARY_HOME/acceptance/2026-07-22"
```

Add `--verifier-policy policy.json` to gate replay through an engine benchmark's artifact/thread-bound
`receipt-verifier-policy/v1`. A policy/thread/artifact mismatch fails; the selected route and policy digest are
recorded in evidence. A generated policy also fails outside its measured input/output token-count points.
Without a policy, the engine's exact full-replay `auto` route remains in effect.

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
  raw/                 private command output and engine reports (excluded from checksums)
  public/              sanitized phase logs and engine report views
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

## Producer/verifier separation and node operations

For asynchronous CPU verification and batches, use the distinct pending/signed-response workflow documented in
[`RECEIPT-BUNDLE.md`](../receipts/RECEIPT-BUNDLE.md#asynchronous-producerverifier-handoff). It never calls a
pending artifact verified. For externally provisioned acceptance nodes, the reviewed provider-adapter state
machine and its explicit billing/teardown interlocks are documented in
[`operations/README.md`](../../operations/README.md).
