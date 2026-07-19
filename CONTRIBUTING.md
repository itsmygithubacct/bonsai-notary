# Contributing to bonsai-notary

`bonsai-notary` is a **composition layer**, not a monolith. Most code lives in three sibling projects;
this repo only wires them together. Knowing *where* a change belongs is the first step.

## Where does my change go?

| If you're changing… | It belongs in… |
|---|---|
| the integer inference engine, kernels, receipts, GGUF import, sampler | **`engine/`** ([repository](https://github.com/itsmygithubacct/integer_inference_engine)) |
| BSV tx building, contracts, Rabin/sighash, the C CLIs | **`chain_c/`** ([repository](https://github.com/itsmygithubacct/chain_c)) |
| the on-chain orchestration (Third Entry backend, agent lifecycle, the engine `--onchain` bridge) | **`bsv_third_entry/`** ([repository](https://github.com/itsmygithubacct/bsv_third_entry)) |
| launchers, the wallet, model identity, composed docs, weight-fetch | **here** (`bonsai-notary`) |

Do **not** edit code under the `engine/`, `chain_c/`, or `bsv_third_entry/` symlinks from this repo —
those are independent projects with their own tests and review. Open the change in the right repo.

## What this repo contains

```
bonsai-notary           launcher: inference → receipt → chain_c Third Entry
bonsai-agent            launcher: resumable AgentTea identity (deploy/action/revoke/status)
scripts/bonsai.sh       curated modes (json/repl/deterministic/receipted/onchain/original)
scripts/fetch_weights.sh fetch weights into $BONSAI_NOTARY_HOME/models
wallet/notary_wallet.py self-managed BSV HD wallet (key mgmt / funding)
artifacts/              the model identity record
docs/                   composed architecture + receipt/identity docs
tests/                  composition self-checks
```

## Ground rules

1. **Keep the composition thin.** New behaviour usually belongs in a sibling repo; this repo wires and
   launches. If you find yourself adding inference or chain logic here, it's in the wrong place.
2. **Never write into the repo tree at run time.** All generated state and secrets go under
   `$BONSAI_NOTARY_HOME` (default `~/.local/trinote`); weights go in `$BONSAI_NOTARY_HOME/models`. The
   single source of truth for paths is the launchers' env (and `engine`'s `notary_paths`).
3. **On-chain stays DRY-RUN by default.** A real broadcast requires the two-key interlock
   (`--chain-confirm`/`--confirm` *and* the binary's `CONFIRM_MAINNET_BROADCAST=yes`). Never weaken it.
4. **No secrets, no machine-specific absolute paths** in committed files. The three sibling references
   are gitignored symlinks.
5. **Run the checks** before a PR:
   ```bash
   PYTHONPATH=engine/bonsai/src:bsv_third_entry engine/bonsai/.venv/bin/python -m pytest tests/ -q
   ```

## Reporting security issues

See `SECURITY.md` — do not open public issues for vulnerabilities involving keys, broadcast, or the
interlock.
