# bonsai-notary — documentation index

`bonsai-notary` is a composition layer. The docs here fall into two groups:

- **Composition & notary** — specific to how the four pieces fit and how the notary behaves.
- **Inherited engine reference** — describe the *inference engine* itself (`engine/`,
  `~/integer_inference_engine`). They are kept here as a convenience snapshot; the engine repo is the
  authoritative home for engine internals. Some still use the original single-repo names (`src/trinote`
  → now `engine/bonsai/src/trinote`; the `trinote-*` CLIs are the engine's, surfaced here as `bonsai-*`).

## Composition & notary

- [`architecture/COMPOSED-ARCHITECTURE.md`](architecture/COMPOSED-ARCHITECTURE.md) — how the four pieces
  compose, where the on-chain seam is, and the symlink → GitHub migration path.
- [`identity/AGENT-LIFECYCLE.md`](identity/AGENT-LIFECYCLE.md) — the stateful on-chain agent identity and
  running inference under it (here: `bonsai-agent` / `bsv-agent`, driven by chain_c `agentd`).
- [`identity/CHARTER-ATLAS-NOTARIZED-BONSAI-8B.md`](identity/CHARTER-ATLAS-NOTARIZED-BONSAI-8B.md) — the
  Bonsai-8B Ricardian charter (model identity).

## Receipts (the notary's core)

- [`receipts/RECEIPTS.md`](receipts/RECEIPTS.md) — receipt build, verification, ledgering, publication.
- [`receipts/THIRD-ENTRY.md`](receipts/THIRD-ENTRY.md) — the triple-entry (Third Entry) design + worked
  mainnet examples. (In the composed system the Third Entry is published by chain_c via `bsv_third_entry`.)
- [`receipts/RECEIPT-BUNDLE.md`](receipts/RECEIPT-BUNDLE.md) — packaging/verifying portable bundles.

## Inherited engine reference (authoritative home: `engine/`)

- [`architecture/INFERENCE-ENGINE.md`](architecture/INFERENCE-ENGINE.md) — integer engine overview
  (`int-ref@bonsai-qwen3`) and the byte-identical oracle / CPU-native / GPU backends.
- [`architecture/DETERMINISM.md`](architecture/DETERMINISM.md) — the bit-exactness contract.
- [`architecture/SAMPLER-INTEGER.md`](architecture/SAMPLER-INTEGER.md) — the receipt-bound deterministic sampler.
- [`architecture/GPU-INTEGER-KERNEL.md`](architecture/GPU-INTEGER-KERNEL.md) — the per-host opt-in CUDA kernel.
- [`architecture/BOUNDARY.md`](architecture/BOUNDARY.md) — the operator/initiation boundary.
- [`architecture/PERFORMANCE.md`](architecture/PERFORMANCE.md),
  [`performance/BONSAI-SPEED-IMPLEMENTATION.md`](performance/BONSAI-SPEED-IMPLEMENTATION.md),
  [`benchmarks/MODEL-WEAKNESSES.md`](benchmarks/MODEL-WEAKNESSES.md) — performance + model notes.
