#!/usr/bin/env python3
"""The counterparty side of a two-party receipt: read a request, sign, answer.

Designed to be the far end of an SSH command, so the producer opens the connection
and this host needs no listener:

    ssh notary /path/to/counterparty-sign.py --key ~/.local/trinote/keys/counterparty.json

One request per invocation, stdin to stdout. That is the whole protocol, and the
narrowness is the point — there is no session, no state, and nothing to poll.

What this process will and will not do is `SigningPolicy`. By default it co-signs only
request-bound (v3) receipts, because a v2 counterparty entry vouches for a computation
without saying which request asked for it, and a signature that cannot be tied to a
request can be replayed against a different one.

Refusals are bounded codes on stdout with a non-zero exit. A signing service that
explained itself would be describing its policy to whoever probes it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notary_tools.counterparty_signer import (   # noqa: E402
    HeldKeySigner,
    SigningPolicy,
    canonical_bytes,
    serve,
)


def _engine_key(path: Path):
    """Load the held secp256k1 key using the engine's own key format."""
    try:
        from trinote.receipts.signing_ec import ECKey
    except ImportError as exc:                                 # pragma: no cover - env
        raise SystemExit(
            "engine receipt dependencies are unavailable; run with the engine "
            "virtual environment (PYTHONPATH=<engine>/bonsai/src)"
        ) from exc
    if not path.exists():
        # never generate here. A counterparty key that this script invented on first
        # contact would make the producer's pin meaningless: whatever key answered
        # would be the key, which is the failure the pin exists to prevent.
        raise SystemExit(f"counterparty key not found: {path}")
    return ECKey.from_json(json.loads(path.read_text(encoding="utf-8")))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--key", required=True, type=Path,
                   help="path to THIS host's counterparty signing key (JSON)")
    p.add_argument("--allow-unbound", action="store_true",
                   help="also co-sign v2 receipts, which carry no link to the request that "
                        "asked for them. Off by default.")
    p.add_argument("--accept-model-hash", action="append", default=[], metavar="HEX",
                   help="vouch only for these models (repeatable). Omit to accept any.")
    p.add_argument("--print-identity", action="store_true",
                   help="write this counterparty's public key and key id, then exit — what "
                        "the producer pins with --counterparty-pubkey")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    key = _engine_key(args.key)

    if args.print_identity:
        print(json.dumps({"publicKey": key.public_hex, "keyId": key.key_id}, indent=2))
        return 0

    signer = HeldKeySigner(
        key=key,
        policy=SigningPolicy(require_context=not args.allow_unbound,
                             accepted_model_hashes=frozenset(args.accept_model_hash)),
    )

    request = sys.stdin.buffer.read()
    response = serve(request, signer)
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.flush()

    try:
        ok = bool(json.loads(response).get("ok"))
    except json.JSONDecodeError:                               # pragma: no cover - serve is canonical
        sys.stdout.buffer.write(canonical_bytes({"ok": False, "code": "internal-error"}))
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
