"""The counterparty signer, weighted towards what it will not sign.

A signing service that can be talked into signing arbitrary bytes is a forgery
oracle, and a counterparty whose key sits on the producer is not a second party at
all. Most of these tests are about those two failures.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notary_tools.counterparty_signer import (  # noqa: E402
    HeldKeySigner,
    RemoteCounterpartySigner,
    SigningPolicy,
    SigningRefused,
    canonical_bytes,
    counterparty_message,
    serve,
)

MODEL = "e5" * 32
IN = "01" * 32
OUT = "02" * 32
CONTEXT = "c0" * 32


class FakeKey:
    """Stands in for an ECKey. Records what it was asked to sign."""

    key_id = "cp-key-1234"
    public_hex = "02" + "ab" * 32

    def __init__(self):
        self.signed: list[bytes] = []

    def sign(self, payload: bytes) -> str:
        self.signed.append(payload)
        return f"secp256k1-ecdsa@v1:{self.public_hex}:{len(payload):0128x}"


def held(**policy):
    return HeldKeySigner(key=FakeKey(), policy=SigningPolicy(**policy))


def request_for(msg: dict) -> bytes:
    return canonical_bytes({"schema": "trinote.counterparty-signing-request/v1", "message": msg})


# ── the message it will sign ─────────────────────────────────────────────────

def test_a_v3_counterparty_message_is_signed():
    signer = held()
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    resp = json.loads(serve(request_for(msg), signer))
    assert resp["ok"] is True
    assert resp["signature"].startswith("secp256k1-ecdsa@v1:")
    assert resp["keyId"] == "cp-key-1234"


def test_the_response_never_carries_the_secret():
    signer = held()
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    raw = serve(request_for(msg), signer).decode()
    assert "secret" not in raw and "private" not in raw


# ── the oracle problem ───────────────────────────────────────────────────────

def test_it_will_not_sign_arbitrary_bytes():
    """There is no code path that signs attacker-chosen bytes: the service rebuilds
    the message from named fields and signs only that."""
    signer = held()
    resp = json.loads(serve(canonical_bytes({
        "schema": "trinote.counterparty-signing-request/v1",
        "message": {"payload": "anything at all"},
    }), signer))
    assert resp["ok"] is False
    assert signer.key.signed == []


def test_extra_fields_are_refused_not_ignored():
    signer = held()
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    resp = json.loads(serve(request_for({**msg, "note": "please sign"}), signer))
    assert resp["ok"] is False
    assert resp["code"] == "not-a-counterparty-message"
    assert signer.key.signed == []


def test_a_model_message_is_not_a_counterparty_message():
    """The model entry carries traceCommit. A counterparty must never produce a
    signature over it — that would let the producer manufacture a first entry."""
    signer = held()
    resp = json.loads(serve(request_for({
        "modelHash": MODEL, "inputCommit": IN, "outputCommit": OUT,
        "traceCommit": "03" * 32, "contextCommit": CONTEXT,
    }), signer))
    assert resp["ok"] is False
    assert signer.key.signed == []


@pytest.mark.parametrize("bad", ["", "zz" * 32, "E5" * 32, "e5" * 31, 42, None])
def test_malformed_hashes_are_refused(bad):
    signer = held()
    resp = json.loads(serve(request_for({
        "modelHash": bad, "inputCommit": IN, "outputCommit": OUT, "contextCommit": CONTEXT,
    }), signer))
    assert resp["ok"] is False
    assert signer.key.signed == []


# ── policy: what this party is willing to attest ─────────────────────────────

def test_an_unbound_v2_message_is_refused_by_default():
    """Co-signing a receipt that binds no request is the failure v3 exists to
    prevent, so the default policy declines it."""
    signer = held()
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT)
    resp = json.loads(serve(request_for(msg), signer))
    assert resp["ok"] is False
    assert resp["code"] == "unbound-request"


def test_v2_can_be_allowed_explicitly_for_compatibility():
    signer = held(require_context=False)
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT)
    assert json.loads(serve(request_for(msg), signer))["ok"] is True


def test_a_model_this_party_does_not_vouch_for_is_refused():
    signer = held(accepted_model_hashes=frozenset({"aa" * 32}))
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    resp = json.loads(serve(request_for(msg), signer))
    assert resp["ok"] is False
    assert resp["code"] == "unpinned-model"


def test_refusals_are_bounded_codes_not_explanations():
    """A service that explained itself would leak its policy to whoever probes it."""
    signer = held(accepted_model_hashes=frozenset({"aa" * 32}))
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    resp = json.loads(serve(request_for(msg), signer))
    assert set(resp) == {"ok", "code"}


# ── the remote signer the producer holds ─────────────────────────────────────

def make_remote(signer: HeldKeySigner) -> RemoteCounterpartySigner:
    return RemoteCounterpartySigner(
        key_id=signer.key_id, public_hex=signer.public_hex,
        transport=lambda payload: serve(payload, signer))


def test_the_producer_side_holds_no_secret():
    remote = make_remote(held())
    assert not hasattr(remote, "secret")
    assert "secret" not in json.dumps(remote.__dict__, default=str)


def test_a_primed_remote_signer_satisfies_build_receipt():
    """`build_receipt` calls .sign(payload) and reads .key_id — nothing else."""
    signer = held()
    remote = make_remote(signer)
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    sig = remote.for_message(msg).sign(canonical_bytes(msg))
    assert sig.startswith("secp256k1-ecdsa@v1:")
    assert remote.key_id == "cp-key-1234"


def test_the_producer_cannot_swap_the_bytes_after_approval():
    """The producer assembles the payload it wants signed. If those bytes are not the
    message the counterparty approved, signing stops on the producer side — and the
    service would rebuild and refuse them anyway."""
    signer = held()
    remote = make_remote(signer)
    approved = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                                    context_commit=CONTEXT)
    other = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit="03" * 32,
                                 context_commit=CONTEXT)
    with pytest.raises(SigningRefused) as excinfo:
        remote.for_message(approved).sign(canonical_bytes(other))
    assert excinfo.value.code == "payload-mismatch"
    assert signer.key.signed == []


def test_signing_without_priming_works_and_still_validates():
    """build_receipt computes the payload itself, so priming cannot be required —
    but an obvious non-message must still never leave the host."""
    signer = held()
    remote = make_remote(signer)
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    assert remote.sign(canonical_bytes(msg)).startswith("secp256k1-ecdsa@v1:")

    with pytest.raises(SigningRefused) as excinfo:
        remote.sign(canonical_bytes({"payload": "anything"}))
    assert excinfo.value.code == "not-a-counterparty-message"
    assert len(signer.key.signed) == 1        # only the good one reached the key


def test_a_service_refusal_surfaces_as_a_refusal_not_a_signature():
    signer = held(accepted_model_hashes=frozenset({"aa" * 32}))
    remote = make_remote(signer)
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    with pytest.raises(SigningRefused) as excinfo:
        remote.for_message(msg).sign(canonical_bytes(msg))
    assert excinfo.value.code == "unpinned-model"


def test_a_broken_transport_is_a_refusal_not_a_forged_signature():
    remote = RemoteCounterpartySigner(key_id="x", public_hex=None,
                                      transport=lambda _p: b"not json")
    msg = counterparty_message(model_hash=MODEL, input_commit=IN, output_commit=OUT,
                               context_commit=CONTEXT)
    with pytest.raises(SigningRefused) as excinfo:
        remote.for_message(msg).sign(canonical_bytes(msg))
    assert excinfo.value.code == "bad-response"
