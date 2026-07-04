# Security Policy

This document describes the security model of **bonsai-notary** (the composed notary; the chain layer
`chain_c` and the orchestration `bsv_third_entry` also carry their own `SECURITY.md`): where secrets
live, how to inject keys safely, the two-key interlock that protects real-money mainnet broadcasts,
guidance for CI/HSM secret handling, and how to report a vulnerability.

This is a **public** repository under the Apache-2.0 license. It moves **real
BSV** on mainnet. Treat every key, mnemonic, and WIF as live funds.

---

## 1. The `$BONSAI_NOTARY_HOME` secret-home convention

**No secret ever lives in the repository tree. Not once, not "temporarily".**

All secrets — the wallet's master mnemonic, derived WIF private keys, and the
chain layer's funding keys — live **outside** the working tree, under a single
secret home directory:

```
$BONSAI_NOTARY_HOME        # default: ~/.local/trinote
├── wallet/                # mode 0700
│   ├── master_mnemonic.txt    # BIP39 root secret, mode 0600 — NEVER printed, NEVER committed
│   └── keys/                  # mode 0700
│       └── <address>.json     # {address, wif, publicKeyHex, derivationPath, ...}, mode 0600
└── keys/                  # receipt signing keys (secp256k1, RFC 6979)
    ├── model.key.json         # issuer private key, mode 0600 — generated on first receipted run if absent
    └── counterparty.key.json  # counterparty private key, mode 0600
```

- **Receipt signing keys** (`keys/model.key.json`, `keys/counterparty.key.json`) are secp256k1 private keys
  that produce the **third-party-verifiable** 1st/2nd-entry signatures (a receipt carries only the *public*
  key, so anyone can verify with no shared secret). They are **auto-generated at `0600` on the first receipted
  run** if absent — so a deployment is authentic by default — and never leave `$BONSAI_NOTARY_HOME`. Supply
  pre-provisioned keys with `--model-key`/`--counterparty-key` (or `model_key=`/`counterparty_key=` to
  `emit_and_verify_bonsai_receipt`). `--demo-keys` (or `TRINOTE_DEMO_KEYS_OK=1`, and pytest) selects the legacy
  deterministic HMAC vouch instead — **no authenticity**, for reproducible snapshots only.

- The location is resolved at runtime from the `BONSAI_NOTARY_HOME` environment
  variable; if unset it defaults to `~/.local/trinote`
  (`wallet/notary_wallet.py`). Override it to point at a tmpfs, an encrypted
  volume, or a per-CI ephemeral path.
- `wallet/notary_wallet.py gen-mnemonic` creates `wallet/` as `0700` and writes
  `master_mnemonic.txt` as `0600`. The mnemonic is **never** echoed to stdout;
  only the account `xpub` and derived public addresses are printed.
- `keyfile` writes WIF keyfiles only into `$BONSAI_NOTARY_HOME/wallet/keys/`,
  each `0600`, and only on explicit request.
- The chain layer keeps its own funding material out of the tree too: `chain_c`
  reads its Elder / funding key files (JSON keyfiles) from under
  `$BONSAI_NOTARY_HOME/chain/` (e.g. `chain/test_bsv.json`), never from inside
  the repo.

### Defense in depth: `.gitignore`

Even though secrets are supposed to live outside the tree, `.gitignore` is a
backstop against an accidental `cp` or stray write. The following are ignored
and must **never** be force-added:

```
*.wif
*_bsv.json
*.key.json
master_mnemonic*
keys/
wallet/.secrets/
.env
.env.*            # but .env.example IS tracked (template only, no real values)
```

**If you ever commit a secret:** treat the key as permanently compromised.
Rotate it (generate a fresh mnemonic/key, move funds to a new derived address),
then scrub history. Removing the file in a later commit is **not** sufficient —
the value is already public the moment it is pushed.

---

## 2. Injecting keys safely

Pick the lowest-privilege path that works for your context.

### Local / interactive

1. Generate the wallet once:
   ```bash
   .venv_wallet/bin/python wallet/notary_wallet.py gen-mnemonic
   ```
   This writes the mnemonic to `$BONSAI_NOTARY_HOME/wallet/master_mnemonic.txt`
   (0600) and prints only public material (xpub + addresses).
2. Materialize a signing keyfile on demand:
   ```bash
   .venv_wallet/bin/python wallet/notary_wallet.py keyfile --role elder
   ```
   The chain layer reads these keyfiles by **path**, via env vars such as
   `ELDER_KEY_FILE`, `AGENT_KEY_FILE`, `COUNTERPARTY_KEY_FILE`,
   `FUND_DEPLOY_KEY_FILE`, `FUND_ACTION_KEY_FILE`, `FUND_REVOKE_KEY_FILE`, and
   `KEY_FILE`. Point them at files under `$BONSAI_NOTARY_HOME`, never at paths
   inside the repo.

### Passing a WIF directly (legacy / TypeScript path)

> **Note — `PRIVATE_KEY` is the legacy TypeScript path and is deprecated in
> chain_c.** The chain_c C `deploy` now reads the funded Elder key from a **JSON
> key file**, pointed to by `ELDER_KEY_FILE` (or the generic `KEY_FILE`), exactly
> like the other `*_KEY_FILE` keyfiles in [§2 above](#local--interactive).
> Prefer a key **file** over `PRIVATE_KEY`: putting the WIF in the process
> environment exposes it to anything that can read `/proc/<pid>/environ`, child
> processes that inherit the env, and many crash/diagnostic dumps. Use
> `ELDER_KEY_FILE`/`KEY_FILE` pointed at a `0600` file under
> `$BONSAI_NOTARY_HOME`.

The historical TypeScript deploy path reads the funded Elder key from the
`PRIVATE_KEY` environment variable (WIF, mainnet). It is described here for the
TS path only. Even there, prefer **passing secrets through the environment,
never on the command line** (argv is visible in `ps`, shell history, and many
process listings):

```bash
# Good: value is read from the environment, not echoed, not in history
read -rs PRIVATE_KEY; export PRIVATE_KEY        # or: source a 0600 env file you own
CONFIRM_MAINNET_BROADCAST=yes npm run deploy
unset PRIVATE_KEY

# Bad: ends up in shell history and process tables
PRIVATE_KEY=L1aw... npm run deploy   # don't
```

### Rules of thumb

- Never paste a mnemonic or WIF into a chat, issue, PR, log line, or commit.
- Never `echo`/`cat` a secret in a script that may be logged or screen-shared.
- Keep the secret home on a volume you control; back it up encrypted.
- Use a **separate, low-balance key** for testing. Fund production keys with the
  minimum needed for the lifecycle.

---

## 3. The two-key mainnet-broadcast interlock

Every code path that can move real BSV is **dry-run by default** and requires
**two independent things** to broadcast for real. Possession of a signing key
alone does nothing; the explicit confirmation alone does nothing. Both must be
present, and the human (or system) supplying them must intend both.

| Factor | What it is | Where |
|---|---|---|
| **Key 1 — the signing key** | A funded WIF / keyfile that can actually sign the inputs (a `*_KEY_FILE` JSON keyfile such as `ELDER_KEY_FILE`, or the wallet's derived key). | `chain_c`'s `deploy` / `bonsai_third_entry` / `agentd` / `cpfp` CLIs; `wallet/notary_wallet.py` |
| **Key 2 — the broadcast confirmation** | An explicit, out-of-band confirmation flag that must be set to a literal value. | `chain_c` CLIs: `CONFIRM_MAINNET_BROADCAST=yes`. Python wallet: the `--broadcast` flag. Composed launchers: `./bonsai-notary --onchain --chain-confirm`, `./bonsai-agent {deploy,action,revoke} --confirm` (all DRY-RUN by default). |

**Default behavior with the key but without the confirmation:** the transaction
is built, signed, and printed (txid, fee, size, outputs) — and **not sent**. You
see exactly what *would* go on-chain.

```bash
# Dry run (Key 1 present, Key 2 absent): builds + prints, broadcasts nothing.
ELDER_KEY_FILE=$BONSAI_NOTARY_HOME/chain/test_bsv.json ./bonsai-agent deploy --ricardian-hash <64hex>
#   → "DRY RUN — not broadcasting. Set CONFIRM_MAINNET_BROADCAST=yes to run live."

# Real broadcast: BOTH factors present and intentional (--confirm sets CONFIRM_MAINNET_BROADCAST=yes).
ELDER_KEY_FILE=$BONSAI_NOTARY_HOME/chain/test_bsv.json ./bonsai-agent deploy --ricardian-hash <64hex> --confirm
```

```bash
# Python wallet — third entry / fan-out. Key 2 is the explicit --broadcast flag.
python wallet/notary_wallet.py third-entry --model-hash <64hex> --receipt-hash <64hex>
#   → "DRY RUN — not broadcasting."
python wallet/notary_wallet.py third-entry --model-hash <64hex> --receipt-hash <64hex> --broadcast
#   → broadcasts
```

Additional safety rails on the broadcast path:

- **Network check.** `chain_c`'s broadcaster is mainnet-only: `chain_broadcast`
  refuses to send a testnet-built tx to the mainnet WhatsOnChain endpoint, so a
  network mismatch cannot silently drive a real spend.
- **Keyfile address check.** Loading a keyfile verifies the WIF actually derives
  the `address` recorded in the file, and refuses to continue otherwise.
- **Read-only smoke tests** (`chain_c`'s `live_smoke` / `live_agent_smoke`) use
  no keys and never broadcast — use them to validate the chain adapter without risk.

**Do not weaken this interlock.** Do not hard-code `CONFIRM_MAINNET_BROADCAST`,
do not default `--broadcast` to true, and do not bake a WIF into a script,
Makefile, or workflow file. The whole point is that broadcasting real funds
takes two deliberate, separable acts.

---

## 4. CI / HSM secret handling

### CI

- **Inject via the CI secret store**, never via committed files. Map the secret
  to an environment variable at job runtime (e.g. GitHub Actions
  `secrets.PRIVATE_KEY` → `env: PRIVATE_KEY`).
- **Default CI to dry-run.** CI must run builds, tests, and dry-run deploys
  *without* `CONFIRM_MAINNET_BROADCAST`. A real broadcast from CI, if ever
  desired, must be a separate, manually-approved, environment-protected job —
  never on push/PR.
- **Scope and rotate.** Use a dedicated, minimally-funded key for any
  CI-initiated broadcast. Rotate on a schedule and immediately on any exposure.
- **Don't leak in logs.** Never `echo` a secret; rely on the CI runner's secret
  masking; turn off command echo (`set +x`) around any secret use.
- **Ephemeral secret home.** Point `BONSAI_NOTARY_HOME` at a per-job temp
  directory (ideally tmpfs) and delete it at job end so no key material survives
  the runner.
- **Pin and review dependencies.** Both the `chain_c` (C — system libs +
  vendored `third_party/`) and Python toolchains handle keys; pin versions
  (lockfiles / pinned system libs) and review updates.

### HSM / signer isolation

The current reference path holds the signing key as a WIF in memory at sign
time. For higher-value operation, isolate the key so it is never extractable:

- Keep the master mnemonic and derived keys inside an HSM, enclave/TEE, or a
  hardware signer. The repo already separates **public** material (the agent's
  pubkey is provisioned via `AGENT_PUBKEY`, not its private key) from secrets —
  extend that pattern so the signer only ever receives a transaction to sign and
  returns a signature.
- Treat the two-key interlock as policy enforced **at the signer boundary**: the
  signer should refuse to sign a mainnet-spending transaction unless the
  broadcast confirmation is also asserted, so neither factor alone is sufficient
  even if one side is compromised.
- Maintain an audit log of every signature request (which input, which amount,
  which destination) outside the signer.

---

## 5. Responsible disclosure

If you discover a security vulnerability — especially anything that could move
funds, leak a key/mnemonic, forge a receipt, or bypass the two-key broadcast
interlock — please report it privately and give us a chance to fix it before any
public disclosure.

- **Report privately via GitHub Security Advisories** — use **"Report a vulnerability"** on this
  repository's **Security** tab (or the repo-relative [`security/advisories/new`](../../security/advisories/new)).
  This keeps the report private until a fix is coordinated. Do **not** open a public issue for an
  unfixed vulnerability.
- **Please include:** affected component/path, version/commit, reproduction
  steps or a proof of concept, and the impact you observed.
- **Please do not:** open a public issue/PR for an unfixed vulnerability, test
  against keys or funds that are not yours, or perform any action that moves
  third-party funds.

We aim to acknowledge reports promptly and will coordinate a disclosure timeline
with you. Good-faith research conducted under this policy is welcome.
