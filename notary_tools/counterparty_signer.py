"""The counterparty signature, from a key the producer does not hold.

## Why this exists

A Trinote receipt carries two signatures. The model signs what it computed; the
counterparty co-signs that *this* input produced *this* output. Today both keys sit
side by side in `$BONSAI_NOTARY_HOME/keys/` on the machine that runs inference, which
means the second entry attests nothing an attacker with the producer could not forge.
Two signatures from one custody boundary are one signature wearing a hat.

So the counterparty becomes a **port**: `build_receipt` already calls
`counterparty_key.sign(payload)` and reads `.key_id`/`.public_hex`, so anything with
that shape works — including something that holds no secret at all and asks a service
on another host.

## Why the service validates structure instead of signing bytes

A service that signs whatever it is handed is a forgery oracle: give it any
attacker-chosen bytes and it produces a valid counterparty signature over them. The
signer therefore reconstructs the canonical message itself from named fields and
signs only that. It cannot be persuaded to sign an arbitrary payload, because it
never accepts one.

The policy it applies before signing is the point of having a separate party:

- the message must be exactly a counterparty entry — three fields, or four with the
  context commitment, and nothing else;
- `requireContext` refuses to co-sign a receipt that is not bound to a request,
  which is the whole reason `trinote.receipt/v3` exists;
- `acceptedModelHashes`, when set, pins what this counterparty is willing to vouch
  for at all.

What crosses the wire back is a signature string. The private key never leaves.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Protocol

CANONICAL_SEPARATORS = (",", ":")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

#: the only two shapes a counterparty may ever sign
V2_FIELDS = ("modelHash", "inputCommit", "outputCommit")
V3_FIELDS = ("contextCommit", "modelHash", "inputCommit", "outputCommit")


class SigningRefused(ValueError):
    """A request the counterparty will not attest. Carries a reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Signer(Protocol):
    """What `build_receipt` needs. Deliberately the same shape as `ECKey`."""

    key_id: str

    def sign(self, payload: bytes) -> str: ...


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=CANONICAL_SEPARATORS,
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def counterparty_message(*, model_hash: str, input_commit: str, output_commit: str,
                         context_commit: str | None = None) -> dict:
    """Build the message a counterparty signs — from named fields, never from bytes."""
    for value, name in ((model_hash, "modelHash"), (input_commit, "inputCommit"),
                        (output_commit, "outputCommit")):
        if not isinstance(value, str) or not _HEX64.match(value):
            raise SigningRefused("bad-hex", name)
    msg = {"modelHash": model_hash, "inputCommit": input_commit, "outputCommit": output_commit}
    if context_commit is not None:
        if not isinstance(context_commit, str) or not _HEX64.match(context_commit):
            raise SigningRefused("bad-hex", "contextCommit")
        msg["contextCommit"] = context_commit
    return msg


@dataclass(frozen=True)
class SigningPolicy:
    """What this counterparty is willing to attest."""

    #: refuse to co-sign a receipt that is not bound to a request
    require_context: bool = True
    #: when non-empty, the only models this counterparty vouches for
    accepted_model_hashes: frozenset[str] = field(default_factory=frozenset)

    def check(self, msg: dict) -> None:
        keys = tuple(sorted(msg))
        if keys not in (tuple(sorted(V2_FIELDS)), tuple(sorted(V3_FIELDS))):
            # never sign a shape that is not a counterparty entry
            raise SigningRefused("not-a-counterparty-message", ", ".join(keys))
        if self.require_context and "contextCommit" not in msg:
            raise SigningRefused(
                "unbound-request",
                "this counterparty co-signs only request-bound (v3) receipts")
        if self.accepted_model_hashes and msg["modelHash"] not in self.accepted_model_hashes:
            raise SigningRefused("unpinned-model", msg["modelHash"][:16] + "…")


@dataclass
class HeldKeySigner:
    """Signs with a key in this process. Correct **only** where this process is the
    counterparty — i.e. not on the producer. Used by the service, and by tests."""

    key: object                      # anything with .sign(bytes) -> str and .key_id
    policy: SigningPolicy = field(default_factory=SigningPolicy)

    @property
    def key_id(self) -> str:
        return self.key.key_id       # type: ignore[attr-defined]

    @property
    def public_hex(self) -> str | None:
        return getattr(self.key, "public_hex", None)

    def sign_message(self, msg: dict) -> str:
        self.policy.check(msg)
        return self.key.sign(canonical_bytes(msg))   # type: ignore[attr-defined]


@dataclass
class RemoteCounterpartySigner:
    """A counterparty that lives somewhere else.

    Satisfies the shape `build_receipt` expects while holding no secret. `transport`
    receives one canonical request and returns the service's canonical response; the
    default runs a command (an SSH invocation, typically), so the producer opens the
    connection and the counterparty needs no inbound listener.
    """

    key_id: str
    public_hex: str | None
    command: list[str] | None = None
    transport: object | None = None       # callable(bytes) -> bytes, for tests
    #: the fields to sign, set per receipt before build_receipt is called
    pending: dict | None = None

    def for_message(self, msg: dict) -> "RemoteCounterpartySigner":
        self.pending = msg
        return self

    def sign(self, payload: bytes) -> str:
        """Called by `build_receipt` with the canonical payload it assembled.

        The bytes are **parsed, not forwarded**. What travels is the set of named
        fields; the service rebuilds the canonical form itself and signs that. So a
        producer that hands over something other than a counterparty message gets a
        refusal rather than a signature, and one that hands over a *valid* message
        cannot make the service sign different bytes than the ones it rebuilt.

        `for_message()` is optional and adds a second check: the caller states in
        advance what it expects to be signed, and a mismatch stops on this side.
        """
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SigningRefused("bad-payload", str(exc)) from exc
        if not isinstance(msg, dict):
            raise SigningRefused("bad-payload", "not an object")

        if self.pending is not None and msg != self.pending:
            raise SigningRefused(
                "payload-mismatch",
                "the bytes offered are not the message this counterparty approved")

        # fail fast on this side too, so an obvious non-message never leaves the host
        SigningPolicy(require_context=False).check(msg)

        request = {"schema": "trinote.counterparty-signing-request/v1",
                   "message": msg}
        raw = self._send(canonical_bytes(request))
        try:
            resp = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SigningRefused("bad-response", str(exc)) from exc
        if not resp.get("ok"):
            raise SigningRefused(resp.get("code", "refused"), str(resp.get("detail", "")))
        sig = resp.get("signature")
        if not isinstance(sig, str) or not sig:
            raise SigningRefused("bad-response", "no signature")
        return sig

    def _send(self, payload: bytes) -> bytes:
        if self.transport is not None:
            return self.transport(payload)          # type: ignore[operator]
        if not self.command:
            raise SigningRefused("no-transport", "set command or transport")
        proc = subprocess.run(self.command, input=payload, capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise SigningRefused("transport-failed", proc.stderr.decode()[:200])
        return proc.stdout


def serve(request_bytes: bytes, signer: HeldKeySigner) -> bytes:
    """The counterparty side: validate, sign, answer. Never echoes the key.

    Every refusal is a bounded code — a signing service that explained itself in
    detail would leak its policy to whoever probes it.
    """
    try:
        req = json.loads(request_bytes)
    except json.JSONDecodeError:
        return canonical_bytes({"ok": False, "code": "bad-request"})

    if not isinstance(req, dict) or req.get("schema") != "trinote.counterparty-signing-request/v1":
        return canonical_bytes({"ok": False, "code": "bad-schema"})

    msg = req.get("message")
    if not isinstance(msg, dict):
        return canonical_bytes({"ok": False, "code": "bad-request"})

    try:
        # rebuild from named fields: the service never signs bytes it was handed
        rebuilt = counterparty_message(
            model_hash=msg.get("modelHash"),
            input_commit=msg.get("inputCommit"),
            output_commit=msg.get("outputCommit"),
            context_commit=msg.get("contextCommit"),
        )
        if rebuilt != msg:
            raise SigningRefused("not-a-counterparty-message", "unexpected fields")
        signature = signer.sign_message(rebuilt)
    except SigningRefused as exc:
        return canonical_bytes({"ok": False, "code": exc.code})
    except Exception:                                            # noqa: BLE001
        return canonical_bytes({"ok": False, "code": "internal-error"})

    return canonical_bytes({"ok": True, "signature": signature,
                            "keyId": signer.key_id, "publicKey": signer.public_hex})
