# Changelog

All notable changes to **bonsai-notary** (the composition layer) are documented here. Changes to the
composed pieces live in their own repos' changelogs: `engine/` (inference engine), `chain_c/` (on-chain
C software), `bsv_third_entry/` (on-chain orchestration).

## [Unreleased]

### Added
- Initial composition: `bonsai-notary` wires three independently-versioned projects — the integer
  inference engine (`engine/`), the chain_c on-chain CLIs (`chain_c/`), and the on-chain orchestration
  (`bsv_third_entry/`) — referenced by symlink (forward-compatible with git submodules).
- Launchers: `bonsai-notary` (inference → receipt → Third Entry), `bonsai-agent` (resumable AgentTea
  identity lifecycle), `scripts/bonsai.sh` (curated modes).
- `--onchain` publishes the receipt's Third Entry through chain_c via `bsv_third_entry`, defaulting to
  a **resumable** `agentd action` under a persisted identity (one-shot lifecycle available as an
  escape hatch).
- `scripts/fetch_weights.sh` populates the **state home** (`$BONSAI_NOTARY_HOME/models`) with the model
  weights — downloading from HuggingFace or reusing/linking a verified local checkout.
- Composed docs: `docs/architecture/COMPOSED-ARCHITECTURE.md`; `INSTALL.md`, `README.md`,
  `CONTRIBUTING.md`, `SECURITY.md` rewritten for the composed layout.

### Security
- `wallet/notary_wallet.py third-entry` now validates `--model-hash`/`--receipt-hash` as **exactly 64 hex
  chars (32 bytes)** before they reach the chain (anchored `_hash32`), failing closed instead of silently
  committing a wrong-length, permanently-unverifiable OP_RETURN.

### Notes
- The two-key interlock (DRY-RUN by default; real broadcast needs `--chain-confirm` *and*
  `CONFIRM_MAINNET_BROADCAST=yes`) is preserved end-to-end.
- All generated state, secrets, and weights live OUTSIDE every repo under `$BONSAI_NOTARY_HOME`.

This project is an extraction/recomposition of the single-repo `bonsai-notarized-bitnet`; engine and
chain internals that used to live there now live in their respective repos.
