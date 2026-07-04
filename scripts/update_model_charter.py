#!/usr/bin/env python3
"""update_model_charter.py — regenerate the Bonsai model's identity card ("charter")
from the actual model files, after a weight re-import or a charter-prose edit.

The identity card (``atlas-…-identity.json``) pins the hashes the off-chain receipt and
the on-chain AgentTea identity bind to. Two of them are derived and go stale:

  * ``modelHash``     = sha256 of the imported ``.safetensors`` artifact
                        (a re-import — e.g. after the open_lm→trinote clean break —
                        changes the artifact magic, hence this digest).
  * ``ricardianHash`` = H(charter prose ‖ model-config params)  [trinote.charter.ricardian_hash]
                        (editing the CHARTER prose — or the config params — changes this).

This recomputes them (plus paramCount + the GGUF sha256) with the *canonical* engine
functions, fails closed if the charter's params block diverges from the model config,
and rewrites the identity file(s). Everything it does not derive (tokenizerHash,
qualityGate, provenance metadata, …) is preserved verbatim; use ``--set k=v`` to override.

Dry-run by default — prints the old→new diff and writes nothing until ``--apply``.

Usage (from the bonsai-notary checkout, with the engine venv on the engine src path):
    engine/bonsai/.venv/bin/python scripts/update_model_charter.py            # dry-run
    engine/bonsai/.venv/bin/python scripts/update_model_charter.py --apply
    … scripts/update_model_charter.py --artifact A --gguf G --charter C --identity J [--identity J2] --apply
    … scripts/update_model_charter.py --set tokenizerHash=<hex> --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]          # the bonsai-notary checkout
_IDENTITY_NAME = "atlas-notarized-bonsai-8b.identity.json"
_CHARTER_NAME = "CHARTER-ATLAS-NOTARIZED-BONSAI-8B.md"


def _add_engine_to_path() -> None:
    """Put the engine's ``trinote`` package on sys.path (``$BONSAI_ENGINE_DIR`` or the engine symlink)."""
    cands = []
    env = os.environ.get("BONSAI_ENGINE_DIR")
    if env:
        cands.append(Path(env).expanduser() / "bonsai" / "src")
    cands.append(REPO / "engine" / "bonsai" / "src")     # bonsai-notary/engine -> integer_inference_engine
    for src in cands:
        if (src / "trinote").is_dir():
            sys.path.insert(0, str(src))
            return
    sys.exit("cannot locate the engine 'trinote' src; set BONSAI_ENGINE_DIR or fix the engine symlink")


def _default_charter() -> Path:
    for c in (REPO / "docs" / "identity" / _CHARTER_NAME, REPO / "engine" / "bonsai" / "docs" / _CHARTER_NAME):
        if c.exists():
            return c
    return REPO / "docs" / "identity" / _CHARTER_NAME


def _default_identities() -> list[Path]:
    """The tracked identity cards to keep in sync (composition copy + engine copy)."""
    out = []
    for c in (REPO / "artifacts" / _IDENTITY_NAME,
              REPO / "engine" / "bonsai" / "artifacts" / _IDENTITY_NAME):
        if c.exists():
            out.append(c)
    return out


def compute_fields(artifact: Path, gguf: Path, charter: Path) -> dict:
    """Recompute the derived identity fields with the canonical engine functions."""
    from trinote.hashing.sha import sha256_file
    from trinote.charter import ricardian_hash               # gates params==charter, fails closed
    from trinote.config_bonsai import ATLAS_NOTARIZED_BONSAI_8B as CFG
    return {
        "modelHash": sha256_file(str(artifact)),
        "ricardianHash": ricardian_hash(str(charter), CFG.as_params_block()),
        "paramCount": CFG.param_count(),
        "_ggufSha256": sha256_file(str(gguf)),               # nested under weightProvenance below
    }


def apply_to_identity(path: Path, fields: dict, overrides: dict, apply: bool) -> bool:
    ident = json.loads(path.read_text())
    changes: list[tuple[str, object, object]] = []

    def _set(key: str, val):
        old = ident.get(key)
        if old != val:
            changes.append((key, old, val))
        ident[key] = val

    _set("modelHash", fields["modelHash"])
    _set("ricardianHash", fields["ricardianHash"])
    _set("paramCount", fields["paramCount"])
    wp = ident.get("weightProvenance")
    if isinstance(wp, dict):
        if wp.get("ggufSha256") != fields["_ggufSha256"]:
            changes.append(("weightProvenance.ggufSha256", wp.get("ggufSha256"), fields["_ggufSha256"]))
            wp["ggufSha256"] = fields["_ggufSha256"]
    else:
        # #26: a partial regen would leave GGUF provenance pointing at a different model
        # version than the freshly-hashed safetensors. Warn loudly instead of silently dropping it.
        print(f"  WARNING: weightProvenance is missing/not an object — recomputed ggufSha256 "
              f"{fields['_ggufSha256']} was NOT written; fix the identity's weightProvenance block.")
    for k, v in overrides.items():
        _set(k, v)

    print(f"\n{path}")
    if not changes:
        print("  (already up to date)")
    for key, old, new in changes:
        print(f"  {key}:\n    - {old}\n    + {new}")
    if apply and changes:
        path.write_text(json.dumps(ident, indent=2, sort_keys=True) + "\n")
        print("  -> WRITTEN")
    return bool(changes)


def main() -> int:
    _add_engine_to_path()
    ap = argparse.ArgumentParser(description="Regenerate the Bonsai model identity card (dry-run by default).")
    import trinote.notary_paths as npaths
    ap.add_argument("--artifact", type=Path, default=Path(npaths.default_artifact()))
    ap.add_argument("--gguf", type=Path, default=Path(npaths.default_gguf()))
    ap.add_argument("--charter", type=Path, default=_default_charter())
    ap.add_argument("--identity", type=Path, action="append", default=None,
                    help="identity.json to update (repeatable); default updates all tracked copies")
    ap.add_argument("--set", dest="overrides", action="append", default=[],
                    metavar="KEY=VALUE", help="force an extra field (e.g. --set tokenizerHash=<hex>)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--force-derived", action="store_true",
                    help="allow --set to override the gate-derived fields (modelHash/ricardianHash/"
                         "paramCount/weightProvenance.ggufSha256) — normally refused")
    args = ap.parse_args()

    for p, label in ((args.artifact, "artifact"), (args.gguf, "gguf"), (args.charter, "charter")):
        if not Path(p).exists():
            sys.exit(f"--{label} not found: {p}")
    identities = args.identity or _default_identities()
    if not identities:
        sys.exit("no identity.json found to update (pass --identity)")
    overrides = {}
    for kv in args.overrides:
        k, _, v = kv.partition("=")
        try:                                   # treat as JSON (numbers/bools/objects); fall back to raw string
            overrides[k.strip()] = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            overrides[k.strip()] = v
    # #25: refuse to let --set clobber the gate-verified derived fields (which are recomputed and
    # checked fail-closed). Overriding them would write a self-inconsistent identity that off-chain
    # receipts and the on-chain AgentTea identity bind to — defeating the very gate this tool enforces.
    _DERIVED_KEYS = {"modelHash", "ricardianHash", "paramCount", "weightProvenance.ggufSha256"}
    # Refuse a derived key OR any PARENT of one: --set weightProvenance={...} would otherwise replace
    # the whole object (discarding the freshly-recomputed ggufSha256) without tripping the guard,
    # since 'weightProvenance' itself is not in the set (review-2 #14).
    clobbered = {k for k in overrides
                 if k in _DERIVED_KEYS or any(d == k or d.startswith(k + ".") for d in _DERIVED_KEYS)}
    if clobbered and not args.force_derived:
        sys.exit(f"--set may not override gate-derived field(s) {sorted(clobbered)}; "
                 f"they are recomputed from the model files. Pass --force-derived to override anyway.")

    print(f"== update_model_charter [{'APPLY' if args.apply else 'DRY-RUN'}]")
    print(f"   artifact : {args.artifact}")
    print(f"   gguf     : {args.gguf}")
    print(f"   charter  : {args.charter}")
    fields = compute_fields(args.artifact, args.gguf, args.charter)
    print(f"\n   modelHash     = {fields['modelHash']}")
    print(f"   ricardianHash = {fields['ricardianHash']}  (deploy the identity with THIS)")
    print(f"   paramCount    = {fields['paramCount']}")
    print(f"   ggufSha256    = {fields['_ggufSha256']}")

    any_change = False
    for idp in identities:
        any_change |= apply_to_identity(idp, fields, overrides, args.apply)
    if not args.apply and any_change:
        print("\n(DRY-RUN — nothing written. Re-run with --apply.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
