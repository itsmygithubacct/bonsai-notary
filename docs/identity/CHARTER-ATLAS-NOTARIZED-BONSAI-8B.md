# CHARTER — ATLAS-Notarized-Bonsai-8B

This charter binds PrismML Bonsai-8B Q1_0 GGUF weights to an ATLAS-style, reproducible reference
artifact and receipt identity. It is a separate identity from ATLAS-Notarized-BitNet-2B4T: Bonsai is
Qwen3-8B dense with binary Q1_0 weights, not BitNet b1.58 ternary.

## 1. Scope

The model is imported from `prism-ml/Bonsai-8B-gguf`, file `Bonsai-8B-Q1_0.gguf`, under Apache-2.0.
The deployment identity commits the exact source GGUF hash, the imported Bonsai reference artifact hash,
the tokenizer hash, and this charter's Ricardian hash. The receipt system proves reproducible inference
under the committed `int-ref@bonsai-qwen3` path; it does not warrant factual correctness.

## 2. Machine Parameters

<!-- ricardian:params:begin -->
name                    = ATLAS-Notarized-Bonsai-8B
sourceRepo              = prism-ml/Bonsai-8B-gguf
sourceFile              = Bonsai-8B-Q1_0.gguf
architecture            = qwen3
vocab                   = 151669
dModel                  = 4096
nLayers                 = 36
nHeads                  = 32
nHeadsKv                = 8
headDim                 = 128
dFfn                    = 12288
contextLen              = 65536
tieEmbeddings           = false
tokenizer               = qwen2-gpt2-bpe
posEncoding             = rope-yarn
ropeBase                = 1000000
ropeScalingType         = yarn
ropeScalingFactor       = 4.0
ropeOriginalContextLen  = 16384
ropeConvention          = neox
ffnActivation           = silu
ffnGated                = true
norm                    = rmsnorm-qk
rmsEps                  = 1e-06
quant                   = q1_0-g128
quantBitsEffective      = 1.125
fpFracBits              = 16
inferenceEngine         = int-ref@bonsai-qwen3
<!-- ricardian:params:end -->

## 3. Identity Binding

`ricardianHash = H(prose || params)` after verifying this params block byte-for-byte against
`trinote.config_bonsai.ATLAS_NOTARIZED_BONSAI_8B.as_params_block()`.

The minted identity must include:

- `modelHash`: SHA-256 of the imported Bonsai safetensors artifact.
- `weightProvenance`: source repo, source file, Apache-2.0 license, source GGUF SHA-256, and importer.
- `tokenizerHash`: canonical hash of the GGUF tokenizer metadata.
- `qualityGate`: status of the Bonsai reference path versus PrismML/llama.cpp.

## 4. Honest Boundary

The Bonsai reference path imports the GGUF Q1_0 weights into a deterministic safetensors artifact and
commits llama.cpp-compatible YaRN RoPE tables into that artifact. The native receipt runner verifies
receipts by replaying the committed sampler against `int-ref@bonsai-qwen3`; the fast PrismML
`llama-cli` path is available for interactive demos but does not emit receipts.

The Bonsai quality gate is a teacher-forced top-1 agreement check against the PrismML llama.cpp fork.
The gate result is recorded in the minted identity as `qualityGate`; generated-token agreement is
reported as a secondary diagnostic because text retokenization can add or split whitespace tokens.
