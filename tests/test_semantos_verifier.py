"""The verifier, weighted towards what it refuses and what it admits it cannot know.

A verifier that says yes is easy. What matters is that it refuses to verify for a
key that already signed, refuses anything short of a full replay, refuses a request
addressed to a different verifier, and is honest about the one thing it cannot
determine — whether it is running in the producer's trust domain.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notary_tools.semantos_verifier import (  # noqa: E402
    RESULT_DOMAIN,
    VerificationRefused,
    Verifier,
    canonical_bytes,
    independence_asserted,
    results_dir,
    run_once,
    validate_request,
    write_result,
)

NOW = 1785001200
RECEIPT = "e5" * 32


class FakeSigner:
    key_id = "key:verifier-c"

    def sign(self, payload: bytes) -> str:
        return f"fake@v1:{len(payload)}"


def request(**over):
    base = {
        "domain": "semantos.trinote.verification.submit/v1",
        "protocolVersion": 1,
        "receiptHash": RECEIPT,
        "encryptedBundleHash": "f3" * 32,
        "modelBindingHash": "44" * 32,
        "contextCommit": "c0" * 32,
        "verifierPolicyHash": "77" * 32,
        "replayMode": "full-unsampled",
        "acceptedModelKeyIds": ["key:model-a"],
        "acceptedCounterpartyKeyIds": ["key:cp-b"],
        "verifierKeyId": "key:verifier-c",
        "maxThreads": 8,
    }
    base.update(over)
    return base


def passing(_request):
    return True, None


def failing(_request):
    return False, "trace-commit-mismatch"


# ── refusals before any work ──────────────────────────────────────────────────

@pytest.mark.parametrize(("over", "reason"), [
    ({"replayMode": "sampled"}, "weak-replay"),
    ({"replayMode": "signature-only"}, "weak-replay"),
    ({"replayMode": "offline-consistency"}, "weak-replay"),
    ({"verifierKeyId": "key:model-a"}, "signer-collapse"),
    ({"verifierKeyId": "key:cp-b"}, "signer-collapse"),
    ({"protocolVersion": 2}, "bad-version"),
    ({"maxThreads": 0}, "bad-number"),
    ({"contextCommit": "C0" * 32}, "bad-hex"),
    ({"acceptedModelKeyIds": []}, "bad-type"),
])
def test_requests_that_must_not_be_verified(over, reason):
    with pytest.raises(VerificationRefused) as excinfo:
        validate_request(request(**over))
    assert excinfo.value.code == reason


def test_replay_never_runs_for_a_refused_request():
    ran = []

    def tracking(req):
        ran.append(req)
        return True, None

    verifier = Verifier(signer=FakeSigner(), replay=tracking)
    with pytest.raises(VerificationRefused):
        verifier.verify(request(replayMode="sampled"), checked_at=NOW)
    assert ran == []


def test_a_request_for_another_verifier_is_refused():
    verifier = Verifier(signer=FakeSigner(), replay=passing)
    with pytest.raises(VerificationRefused) as excinfo:
        verifier.verify(request(verifierKeyId="key:verifier-d"), checked_at=NOW)
    assert excinfo.value.code == "wrong-verifier"


# ── results ───────────────────────────────────────────────────────────────────

def test_a_pass_echoes_the_context_and_the_replay_mode():
    """The finalizer has to be able to confirm this answers *this* request under a
    full replay without trusting the transport."""
    result = Verifier(signer=FakeSigner(), replay=passing).verify(request(), checked_at=NOW)
    assert result["domain"] == RESULT_DOMAIN
    assert result["verdict"] == "pass"
    assert result["contextCommit"] == "c0" * 32
    assert result["replayMode"] == "full-unsampled"
    assert result["rejectionCode"] is None
    assert result["signature"]


def test_a_failure_names_a_bounded_code():
    result = Verifier(signer=FakeSigner(), replay=failing).verify(request(), checked_at=NOW)
    assert result["verdict"] == "fail"
    assert result["rejectionCode"] == "trace-commit-mismatch"


def test_the_signature_covers_the_body_without_itself():
    result = Verifier(signer=FakeSigner(), replay=passing).verify(request(), checked_at=NOW)
    body = {k: v for k, v in result.items() if k != "signature"}
    assert result["signature"] == f"fake@v1:{len(canonical_bytes(body))}"


@pytest.mark.parametrize("replay", [
    lambda _r: (True, "why"),      # a pass carrying a rejection
    lambda _r: (False, None),      # a failure naming nothing
])
def test_an_incoherent_replay_result_is_refused(replay):
    with pytest.raises(VerificationRefused) as excinfo:
        Verifier(signer=FakeSigner(), replay=replay).verify(request(), checked_at=NOW)
    assert excinfo.value.code == "incoherent-replay"


# ── the honesty about independence ────────────────────────────────────────────

def test_independence_is_an_operator_assertion_not_a_default():
    assert independence_asserted({}) is False
    assert independence_asserted({"TRINOTE_VERIFIER_INDEPENDENT": "0"}) is False
    assert independence_asserted({"TRINOTE_VERIFIER_INDEPENDENT": "1"}) is True


def test_development_results_are_segregated_and_marked(tmp_path):
    result = Verifier(signer=FakeSigner(), replay=passing).verify(request(), checked_at=NOW)
    path = write_result(tmp_path, result, env={})
    assert path.parent.name == "results-development"
    sidecar = path.with_suffix(".development")
    assert sidecar.exists()
    assert "must not" in sidecar.read_text()


def test_asserted_independence_writes_plain_results(tmp_path):
    result = Verifier(signer=FakeSigner(), replay=passing).verify(request(), checked_at=NOW)
    path = write_result(tmp_path, result, env={"TRINOTE_VERIFIER_INDEPENDENT": "1"})
    assert path.parent.name == "results"
    assert not path.with_suffix(".development").exists()


def test_the_wire_object_is_identical_in_both_modes(tmp_path):
    """Adding a development flag would make every dev result structurally invalid on
    the semantos side, hiding the mode rather than marking it. What keeps dev
    results out of production is the pinned verifier key."""
    result = Verifier(signer=FakeSigner(), replay=passing).verify(request(), checked_at=NOW)
    dev = write_result(tmp_path / "a", result, env={})
    prod = write_result(tmp_path / "b", result, env={"TRINOTE_VERIFIER_INDEPENDENT": "1"})
    assert dev.read_text() == prod.read_text()


def test_results_dir_names_are_distinct(tmp_path):
    assert results_dir(tmp_path, {}) != results_dir(tmp_path, {"TRINOTE_VERIFIER_INDEPENDENT": "1"})


# ── the loop ──────────────────────────────────────────────────────────────────

def test_run_once_verifies_queued_requests(tmp_path):
    queue = tmp_path / "verifications"
    queue.mkdir()
    (queue / f"{RECEIPT}.json").write_text(json.dumps(request()), encoding="utf-8")

    results = run_once(tmp_path, Verifier(signer=FakeSigner(), replay=passing), now=NOW, env={})
    assert [r["verdict"] for r in results] == ["pass"]
    assert (tmp_path / "results-development" / f"{RECEIPT}.json").exists()


def test_a_refused_request_records_nothing(tmp_path):
    """A refusal is not a verdict — the control plane keeps waiting rather than
    seeing a failed verification that never ran."""
    queue = tmp_path / "verifications"
    queue.mkdir()
    (queue / f"{RECEIPT}.json").write_text(json.dumps(request(replayMode="sampled")), encoding="utf-8")

    assert run_once(tmp_path, Verifier(signer=FakeSigner(), replay=passing), now=NOW, env={}) == []
    assert not (tmp_path / "results-development").exists()
