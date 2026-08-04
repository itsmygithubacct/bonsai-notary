"""Independent replay for semantos verification requests.

Takes a `semantos.trinote.verification.submit/v1`, replays the committed
computation through an injected callable, and emits a signed
`semantos.trinote.verification.result/v1`.

## The independence this module can and cannot enforce

It can refuse the cases that are visible in the request: a verifier key that also
signed the receipt or co-signed as counterparty, and any replay mode weaker than
full-unsampled. It does, before any work happens.

It cannot tell whether it is running on the same machine as the producer. Nothing
in a request says so, and a verifier that trusted a self-report would be trusting
the party it exists to check. So the operator asserts it, and the assertion is
recorded rather than believed:

- with `TRINOTE_VERIFIER_INDEPENDENT=1`, results are written to `results/`;
- without it, results are written to `results-development/` **and** accompanied by
  a sidecar recording that they were produced in development mode.

The wire object is byte-identical either way, deliberately: the schema is a closed
set of fields and adding a "development" flag would make every dev result
structurally invalid on the semantos side, which would hide the mode rather than
mark it. What actually keeps development results out of production is the control
plane's pinned verifier key — a development verifier signs with a development key,
and an unpinned key is refused at finalization.

## What a pass means

That this implementation replayed the committed computation and got the committed
result. Not that the answer is true, useful, or safe.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

PROTOCOL_VERSION = 1
FULL_REPLAY = "full-unsampled"

SUBMIT_DOMAIN = "semantos.trinote.verification.submit/v1"
RESULT_DOMAIN = "semantos.trinote.verification.result/v1"

SUBMIT_FIELDS = frozenset((
    "domain", "protocolVersion", "receiptHash", "encryptedBundleHash",
    "modelBindingHash", "contextCommit", "verifierPolicyHash", "replayMode",
    "acceptedModelKeyIds", "acceptedCounterpartyKeyIds", "verifierKeyId", "maxThreads",
))

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_ASCII = re.compile(r"\A[\x20-\x7e]*\Z")


class VerificationRefused(ValueError):
    """A request that must not be verified. Carries the specification's reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Replay(Protocol):
    """Re-executes the committed computation.

    Returns ``(ok, rejection_code)``. ``ok`` is True only when the complete
    unsampled replay reproduced every committed value. Raising is a normal outcome
    and is reported as an operational failure, never as a verdict.
    """

    def __call__(self, request: dict) -> tuple[bool, str | None]: ...


class Signer(Protocol):
    """Signs the canonical result bytes. The private key never leaves it."""

    key_id: str

    def sign(self, payload: bytes) -> str: ...


def _check_hex(value, key: str) -> None:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise VerificationRefused("bad-hex", key)


def validate_request(request) -> dict:
    """Refuse anything that must not be verified, before any work happens."""
    if not isinstance(request, dict):
        raise VerificationRefused("bad-type", "request must be an object")

    unknown = set(request) - SUBMIT_FIELDS
    if unknown:
        raise VerificationRefused("unknown-field", ", ".join(sorted(unknown)))
    missing = SUBMIT_FIELDS - set(request)
    if missing:
        raise VerificationRefused("missing-field", ", ".join(sorted(missing)))

    if request["domain"] != SUBMIT_DOMAIN:
        raise VerificationRefused("bad-domain", str(request["domain"]))
    if request["protocolVersion"] != PROTOCOL_VERSION:
        raise VerificationRefused("bad-version", str(request["protocolVersion"]))

    for key in ("receiptHash", "encryptedBundleHash", "modelBindingHash",
                "contextCommit", "verifierPolicyHash"):
        _check_hex(request[key], key)

    if request["replayMode"] != FULL_REPLAY:
        # a diagnostic mode must never be the request whose answer promotes a cell
        raise VerificationRefused("weak-replay", str(request["replayMode"]))

    verifier = request["verifierKeyId"]
    if not isinstance(verifier, str) or not _ASCII.match(verifier) or not verifier:
        raise VerificationRefused("bad-charset", "verifierKeyId")

    for key in ("acceptedModelKeyIds", "acceptedCounterpartyKeyIds"):
        value = request[key]
        if not isinstance(value, list) or not value:
            raise VerificationRefused("bad-type", key)
        for item in value:
            if not isinstance(item, str) or not _ASCII.match(item):
                raise VerificationRefused("bad-charset", key)

    if verifier in request["acceptedModelKeyIds"] or verifier in request["acceptedCounterpartyKeyIds"]:
        # a verifier that signed the receipt is not checking anything
        raise VerificationRefused("signer-collapse", "the verifier also signed the receipt")

    threads = request["maxThreads"]
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise VerificationRefused("bad-number", "maxThreads")

    return request


def canonical_bytes(obj) -> bytes:
    """The canonical encoding both systems commit under."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class Verifier:
    """A replay environment with its own signing key."""

    signer: Signer
    replay: Replay

    def verify(self, request: dict, *, checked_at: int) -> dict:
        """Validate, replay, sign. The result echoes contextCommit and replayMode so
        the finalizer can confirm it answers this request under a full replay."""
        validate_request(request)

        if request["verifierKeyId"] != self.signer.key_id:
            raise VerificationRefused(
                "wrong-verifier", f"request names {request['verifierKeyId']}, this is {self.signer.key_id}")

        ok, rejection = self.replay(request)
        if ok and rejection is not None:
            raise VerificationRefused("incoherent-replay", "a pass cannot carry a rejection code")
        if not ok and not rejection:
            raise VerificationRefused("incoherent-replay", "a failure must name a code")

        body = {
            "domain": RESULT_DOMAIN,
            "protocolVersion": PROTOCOL_VERSION,
            "receiptHash": request["receiptHash"],
            "contextCommit": request["contextCommit"],
            "verifierKeyId": self.signer.key_id,
            "replayMode": FULL_REPLAY,
            "verdict": "pass" if ok else "fail",
            "checkedAt": checked_at,
            "rejectionCode": None if ok else rejection,
        }
        return {**body, "signature": self.signer.sign(canonical_bytes(body))}


def independence_asserted(env=os.environ) -> bool:
    """Has the operator asserted that this verifier is not the producer?

    Nothing in a request can establish this, and a self-report from the producer
    would be worthless, so it is an operator assertion — recorded, not believed.
    """
    return env.get("TRINOTE_VERIFIER_INDEPENDENT") == "1"


def results_dir(root: Path, env=os.environ) -> Path:
    """Development results are segregated on disk so they cannot be mistaken for
    production evidence during an audit."""
    return root / ("results" if independence_asserted(env) else "results-development")


def write_result(root: Path, result: dict, env=os.environ) -> Path:
    """Write a result atomically, with a development sidecar when independence has
    not been asserted."""
    directory = results_dir(root, env)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{result['receiptHash']}.json"

    fd, temp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_bytes(result).decode("utf-8"))
            handle.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    except BaseException:
        os.unlink(temp)
        raise

    if not independence_asserted(env):
        # the wire object stays byte-identical — adding a flag would make every
        # development result structurally invalid on the semantos side, hiding the
        # mode rather than marking it
        target.with_suffix(".development").write_text(
            "Produced without TRINOTE_VERIFIER_INDEPENDENT=1.\n"
            "The producer and verifier may share a trust domain. This result must not\n"
            "be treated as independent replay evidence.\n",
            encoding="utf-8",
        )
    return target


def run_once(root: Path, verifier: Verifier, *, now: int,
             clock: Callable[[], int] | None = None, env=os.environ) -> list[dict]:
    """Verify every queued verification request under `root/verifications`."""
    tick = clock or (lambda: now)
    requests_dir = root / "verifications"
    outcomes: list[dict] = []
    if not requests_dir.is_dir():
        return outcomes

    for path in sorted(requests_dir.glob("*.json")):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            result = verifier.verify(request, checked_at=tick())
        except VerificationRefused:
            # a refusal is not a verdict: the control plane keeps waiting rather
            # than recording a failed verification that never ran
            continue
        except Exception:                                        # noqa: BLE001
            continue
        write_result(root, result, env)
        outcomes.append(result)

    return outcomes
