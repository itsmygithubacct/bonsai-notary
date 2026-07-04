# BOUNDARY.md — the gpu ↔ host contract

## The one invariant (security-critical)

**gpu is the sole initiator. host never opens a connection to gpu** — no ssh, no fetch, no
"host pulls from a gpu endpoint." There is no gpu-served endpoint and no host-side puller; that
whole `host → gpu` direction does not exist.

```
        gpu (Python/GPU) — SOLE INITIATOR              host (chain_c / C / BSV) — PASSIVE
   ┌────────────────────────┐                      ┌────────────────────────┐
   │ trinote-export-bundle      │  (1) gpu pushes JSON │ chain_c CLIs:          │
   │  charter.json          │  ───outbound ssh───▶ │ deploy / agentd        │
   │  model_announce.json   │      (gpu initiates) │ bonsai_third_entry     │
   │  inference_receipt.json│                      │ reputation_indexer     │
   │  mi_label.json         │  (2) gpu pulls result│ verify_ricardian       │
   │  ◀── txid / ρ / reads ─┼──────────────────────┤ (stdout → gpu local)   │
   └────────────────────────┘                      └────────────────────────┘
   NEVER: host ──▶ gpu (no ssh, no fetch)   ALWAYS: gpu ──outbound──▶ host, pulls results to local
```

gpu runs each chain step as a remote invocation and captures the result on stdout, e.g.:

```bash
# gpu-initiated; the receipt artifact crosses outbound, the chain_c plan/txid pulled straight back to local
ssh host 'cd ~/bsv_third_entry && ./bsv-third-entry --artifact /dev/stdin --plan' \
    < boundary/inference_receipt.json > .out/third_entry.json
```

Nothing is left "published" for host to come and get. Weights never leave gpu; only **versioned
JSON + root hashes** cross. host's BSV keys never leave host.

## Frozen artifact schemas

All carry `schemaVersion`; all hashes are hex SHA-256. A field change is a versioned, reviewed
event (`/vN` bump), not an ad-hoc edit.

```jsonc
// boundary/model_announce.json   (gpu → host → ARP announce)
// params is config_bonsai.BonsaiQwen3Config.as_params_block() for the shipped Bonsai-8B identity.
{ "schemaVersion": 1, "modelHash": "…", "ricardianHash": "…",
  "datasetRoot": "…", "weightsRoot": "…", "tokenizerHash": "…",
  "params": { "name":"ATLAS-Notarized-Bonsai-8B","sourceRepo":"prism-ml/Bonsai-8B-gguf",
              "sourceFile":"Bonsai-8B-Q1_0.gguf","architecture":"qwen3",
              "vocab":151669,"dModel":4096,"nLayers":36,"nHeads":32,"nHeadsKv":8,
              "headDim":128,"dFfn":12288,"contextLen":65536,"tieEmbeddings":false,
              "tokenizer":"qwen2-gpt2-bpe","posEncoding":"rope-yarn","ropeBase":1000000,
              "ropeScalingType":"yarn","ropeScalingFactor":4.0,"ropeOriginalContextLen":16384,
              "ropeConvention":"neox","ffnActivation":"silu","ffnGated":true,
              "norm":"rmsnorm-qk","rmsEps":1e-6,"quant":"q1_0-g128",
              "quantBitsEffective":1.125,"fpFracBits":16,"inferenceEngine":"int-ref@bonsai-qwen3" },
  "licenceTag": "Apache-2.0", "samplerSpec": {"mode":"greedy","seed":0} }

// boundary/inference_receipt.json   (gpu → host → triple-entry tx)
{ "schemaVersion": 1, "modelHash": "…", "inputHash": "…", "outputHash": "…",
  "miTraceRoot": "…", "samplerSpec": {"mode":"greedy","seed":0}, "envFingerprint": "…",
  "receiptHash": "…", "sig_Model": "…" }   // counterparty co-sign + ledger entry added on host

// boundary/mi_label.json   (gpu → host → validator attestation; Rabin-signed, no OP_CHECKDATASIG)
{ "schemaVersion": 1, "modelHash": "…", "featureId": 4123,
  "proseLabel": "fires only in archaic-legal-formality contexts",
  "behavioralClaim": "never fires on tokens outside legal-formality spans (checkable vs corpus)",
  "activationEvidenceHash": "…", "falsifiable": true, "signer": "…" }
```

## Invariant: parameters == prose

The `params` block in `model_announce.json` MUST equal the machine-parsable params block in the charter
(delimited by `<!-- ricardian:params:begin/end -->`), which in turn equals
`config_bonsai.BonsaiQwen3Config.as_params_block()`. `src/trinote/charter.py::assert_matches` is the
gpu-side gate; in this extraction it is exercised **at mint time** by `cli/trinote-mint-bonsai`
(`src/trinote/cli/mint_bonsai_cli.py` calls `assert_matches(charter_path, CFG.as_params_block())` before
computing `ricardianHash`), not by a bundled boundary test (`tests/boundary/test_charter_params.py` is
parent-repo, not shipped here). The host-side gate (`chain_c`'s `verify_ricardian` CLI) lives in the
composed `chain_c` chain layer (see [`../receipts/RECEIPTS.md`](../receipts/RECEIPTS.md) "Scope"). A mismatch is a hard fail —
it is a *different model identity*, not a warning.
