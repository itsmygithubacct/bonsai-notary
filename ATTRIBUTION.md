# Attribution & Third-Party Notices

`bonsai-notary` is a thin **composition layer**. It contains launchers, the BSV HD wallet, the model
identity record, and docs. It does **not** vendor the inference engine or the chain layer — those are
separate projects referenced by symlink, each with its own attribution:

- **Inference engine** — `engine/` (`~/integer_inference_engine`): see that project's `LICENSE` /
  `NOTICE`. It is the integer-reference engine extracted from the `ATLAS-Notarized-BitNet` lineage.
- **On-chain C software** — `chain_c/` (`~/chain_c`): see `chain_c/NOTICE` and `chain_c/SECURITY.md`.
  chain_c is a byte-exact C port of the **Priscilla BSV** chain layer (TypeScript / scrypt-ts); it
  vendors cJSON (MIT), Unity (MIT), a public-domain RIPEMD160, and the circomlib MiMC7 round constants,
  and links libsecp256k1 / OpenSSL libcrypto / libcurl. The compiled scrypt contract artifacts and the
  Ricardian prose under `chain_c/` originate from that upstream project.
- **On-chain orchestration** — `bsv_third_entry/` (`~/bsv_third_entry`): pure-Python glue that drives
  `chain_c`'s CLIs; see its `LICENSE`.

## This repository

- **Origin.** Extracted/recomposed from the single-repo `bonsai-notarized-bitnet` (Apache-2.0). The
  wallet (`wallet/notary_wallet.py`), launchers, identity record, and docs derive from it.
- **License.** Apache-2.0 (`LICENSE`, `NOTICE`).
- **Runtime dependencies** (installed into the engine venv, not vendored): `numpy`, `safetensors`,
  `ecdsa` (receipt signatures), and — optionally for the wallet — `bsv-sdk` + `requests`. Their licenses
  are their own.

For the on-chain contracts' upstream license provenance (the `ricardian-tea-bsv` / scrypt-ts lineage),
see `chain_c/NOTICE`, which is the authoritative record for the chain layer.
