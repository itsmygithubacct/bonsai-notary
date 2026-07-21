# Set up a notarized Bonsai-27B agent

[`scripts/setup-bonsai-27b.sh`](../scripts/setup-bonsai-27b.sh) turns a fresh Linux clone into a
receipt-capable Bonsai-27B notary. It installs/builds the four composed projects, downloads and verifies
the pinned model, imports the deterministic artifact, provisions signing identities, and optionally checks
that a BSV wallet can fund public Third Entries.

The script is idempotent. If a download, funding step, or optional deployment is not ready, fix that item
and rerun the same command. Existing model files, builds, mnemonic, and keys are reused; different existing
keys are never overwritten silently.

## What you need before starting

| Information or resource | Required when | Notes |
|---|---|---|
| Linux host with `sudo` or preinstalled build dependencies | Always | Debian/Ubuntu (`apt`) and Fedora (`dnf`) package installation is automated. |
| Python 3.11+ | Always | Setup defaults to a uv-managed Python 3.12 and downloads it automatically; the host's older `python3` is not used for the engine environment. |
| Internet access to GitHub, Hugging Face, Astral, and PyPI | Fresh install | The 27B GGUF is public; no Hugging Face token is normally needed. |
| About 16 GB free disk | Full 27B install | The verified GGUF is 3.80 GB and its deterministic artifact is about 4.23 GB; conversion needs working room. Setup checks the space needed for every missing output, including artifact-only resume runs. |
| At least 12 GB RAM; 16 GB recommended | Running deterministic 27B | An NVIDIA GPU is optional. CPU-only installs work but inference and fresh-oracle receipt replay are slower. |
| Signing-key choice | Always | Generate a new BIP39 wallet, import an existing BIP39 mnemonic, import three `{wif,address}` JSON files, or reuse a prior setup. |
| BSV funds | Only for public Third Entries | At least 12,000 satoshis at one wallet-derived address by default. Setup only checks; it does not acquire funds. |
| Explicit mainnet-deployment consent | Only to deploy immediately | Both `--deploy-agent` and `--confirm-mainnet` are required. Merely enabling public Third Entries never broadcasts. |

The deterministic path does not need the PrismML CUDA runtime. CUDA is an optional producer acceleration;
receipt verification still uses an independently loaded CPU oracle. See [Bonsai-27B](BONSAI-27B.md) for
the backend and resource distinctions.

## Fresh interactive setup

```bash
git clone https://github.com/itsmygithubacct/bonsai-notary.git
cd bonsai-notary
./scripts/setup-bonsai-27b.sh
```

The prompts ask:

1. whether public BSV Third Entries should be configured;
2. whether to generate a wallet, import a mnemonic, or import existing signing-key files;
3. for protected input paths when importing existing material.

Mnemonic input is hidden and never placed on a command line. Generated or imported secrets live under
`$BONSAI_NOTARY_HOME` (default `~/.local/trinote`) with owner-only permissions. Setup never prints a
mnemonic or WIF.

### Unattended local-receipt setup

This installs everything needed for deterministic 27B inference and verified local receipts, but neither
checks a wallet nor enables a blockchain operation:

```bash
./scripts/setup-bonsai-27b.sh \
  --yes --key-mode generate --local-only
```

`--yes` accepts safe installation defaults. It never implies permission to spend or broadcast.

### Import an existing mnemonic

Use a protected file to keep the words out of shell history and the process list:

```bash
chmod 600 /secure/path/bsv-mnemonic.txt
./scripts/setup-bonsai-27b.sh \
  --key-mode import-mnemonic \
  --mnemonic-file /secure/path/bsv-mnemonic.txt \
  --local-only
```

Omit `--mnemonic-file` for a hidden terminal prompt. The mnemonic must pass BIP39 word-list and checksum
validation. Importing the same seed again is harmless; replacing a different existing seed is refused.

### Import existing signing-key files

For local receipts, three compressed-mainnet-WIF JSON files can be supplied directly:

```bash
./scripts/setup-bonsai-27b.sh \
  --key-mode keyfiles \
  --elder-key-file /secure/elder.json \
  --agent-key-file /secure/agent.json \
  --counterparty-key-file /secure/counterparty.json \
  --local-only
```

Each file must contain at least:

```json
{
  "wif": "<compressed mainnet WIF>",
  "address": "<the P2PKH address derived from that WIF>"
}
```

Setup verifies the WIF/address binding before copying the key with mode `0600`. Automatic funding discovery
and fresh change-address rotation require the HD mnemonic, so the all-in-one public Third Entry mode uses a
generated or imported mnemonic. Advanced externally managed key deployments can still use the launcher’s
`ELDER_KEY_FILE`, `AGENT_KEY_FILE`, `COUNTERPARTY_KEY_FILE`, and `FUND_*_KEY_FILE` overrides.

## Public Third Entry and funding

Enable public Third Entry preparation explicitly:

```bash
./scripts/setup-bonsai-27b.sh \
  --yes --key-mode generate --public-third-entry
```

After installation, setup queries public WhatsOnChain data for wallet-derived addresses. If none can cover
the default 12,000-satoshi threshold, it exits with status `3` and a message like:

```text
PUBLIC THIRD ENTRY IS ENABLED, BUT THE WALLET IS NOT FUNDED.
Required: one wallet-derived address with at least 12000 satoshis.
Fund this wallet-owned address: 1...
No transaction was built or broadcast. Fund it, then rerun the same setup command.
```

The displayed address is derived from the protected mnemonic. Fund it through your normal BSV wallet or
exchange, then rerun the command. For a quick recheck that skips builds and downloads:

```bash
./scripts/setup-bonsai-27b.sh \
  --yes --key-mode existing --public-third-entry --funding-check-only
```

Unconfirmed wallet funds are accepted by the setup preflight, but miners/network policy still determine
whether a dependent transaction is accepted.

### One-time AgentTea deployment

Funding alone does not broadcast anything. Inspect the dry-run deployment first:

```bash
./bonsai-agent deploy
```

Deploy the resumable identity only when ready:

```bash
./scripts/setup-bonsai-27b.sh \
  --yes --key-mode existing --public-third-entry \
  --skip-system-packages --skip-model-download --skip-model-import \
  --deploy-agent --confirm-mainnet
```

Both final flags are required because deployment spends real BSV. Subsequent public notarizations retain
their own two-part interlock:

```bash
./bonsai-notary "Your prompt" --model 27b --receipts --onchain                 # dry-run
./bonsai-notary "Your prompt" --model 27b --receipts --onchain --chain-confirm # real BSV
```

If the public mode is selected but the AgentTea identity has not been deployed, a confirmed notarization
fails closed with a “deploy first” message.

## How the signing identities are bound

With the recommended mnemonic flow, setup derives three BIP44 BSV roles:

| Role | Derivation | Used for |
|---|---|---|
| Elder | `m/44'/236'/0'/0/0` | AgentTea owner and recovery authority |
| Agent | `m/44'/236'/0'/0/1` | AgentTea Agent key and the receipt’s model/issuer signature |
| Counterparty | `m/44'/236'/0'/0/2` | AgentTea counterparty key and the receipt’s counterparty signature |

Stable copies are installed under `$BONSAI_NOTARY_HOME/agent/keys/`. The launchers automatically use them,
so an on-chain stateful bundle’s expected `agentPubKey` and `counterpartyPubKey` match the two signatures in
the receipt. The model artifact identity is separate: it binds the exact 27B graph and quality gate, while
these keys identify who issued and accepted a particular receipt.

Back up `$BONSAI_NOTARY_HOME/wallet/master_mnemonic.txt` securely and offline. Anyone with those words can
derive every role and spend wallet funds. Never copy the entire state home into a repository or machine image.

## What the script installs

In order, the script:

1. installs or verifies Linux compiler, CMake, crypto, HTTP, Python, and Git prerequisites;
2. installs a pinned `uv` without editing shell startup files when `uv` is absent;
3. clones and wires the immutable `integer_inference_engine`, `chain_c`, and `bsv_third_entry` commits in `dependencies.lock`;
4. creates the engine uv environment with a supported uv-managed Python (3.12 by default) plus inference,
   wallet, and test dependencies;
5. builds `chain_c` outside the source checkout and runs its offline tests;
6. builds the portable byte-exact CPU Q1 kernel and the pinned CPU-only `llama-tokenize` required for exact
   Qwen tokenization; when `nvcc` exists, it also builds the optional CUDA producer;
7. validates/provisions the three signing roles and binds receipt signing to AgentTea;
8. downloads and SHA-256-verifies the pinned 27B GGUF;
9. imports the 4,096-token deterministic artifact through an atomic temporary file, then hashes the complete
   safetensors file and validates it against the pinned release identity and quality gate;
10. runs composition/Third Entry tests and a no-inference command-resolution smoke test;
11. when requested, checks wallet funding and optionally performs one explicitly confirmed deployment.

Generated state, binaries, models, receipt keys, wallet keys, and the setup manifest are kept outside Git
under `$BONSAI_NOTARY_HOME`. The three dependency source checkouts sit next to `bonsai-notary` by default.

## Useful controls and troubleshooting

```bash
./scripts/setup-bonsai-27b.sh --help
./scripts/setup-bonsai-27b.sh --dry-run --yes --key-mode generate --local-only
```

- **No NVIDIA/CUDA:** expected; setup reports CPU-only mode. Local receipts remain valid.
- **Custom Python:** pass `--python 3.11` (or set `BONSAI_PYTHON_VERSION`) to override the default 3.12.
  Python older than 3.11 is rejected before dependency installation. A partial venv created by an older
  setup is moved aside with an `.unsupported-python-*` suffix instead of being deleted.
- **Insufficient disk:** free at least 16 GB under the state-home filesystem and rerun. Downloads resume.
- **Funding check API error:** this is different from zero funds and exits with an API/network error. Retry
  when WhatsOnChain is reachable; no broadcast is attempted.
- **Interrupted download/import:** rerun. The pinned GGUF download uses a `.part` file and resumes; artifact
  imports write a same-filesystem temporary file and only replace the final path after completion. Every reused
  artifact is revalidated against its release hash, identity, and quality gate before setup can report ready.
- **Existing different keys:** setup refuses to replace them. Move the old state home aside only after a
  verified backup, or select a different `--notary-home`.
- **Custom state location:** pass `--notary-home /private/path` on every setup run and export the same
  `BONSAI_NOTARY_HOME` when launching later.
- **Manual prerequisites:** use `--skip-system-packages` after installing the packages listed in
  [`INSTALL.md`](../INSTALL.md).
