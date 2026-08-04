"""The spool worker, weighted towards what it must refuse.

Collecting a well-formed job and reporting completion is the easy half. What
matters is that an expired job, a job carrying a filesystem path, a job from an
unknown protocol version, or a second delivery of a job already claimed cannot
produce an execution — and that a state can never move backwards.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notary_tools.semantos_spool import (  # noqa: E402
    JobRejected,
    Spool,
    run_once,
    validate_job,
)

NOW = 1785000000
RECEIPT = "e5" * 32
BUNDLE = "f3" * 32


def job(**over):
    base = {
        "domain": "semantos.trinote.job.submit/v1",
        "protocolVersion": 1,
        "idempotencyKey": "a1" * 32,
        "executionIntentHash": "a2" * 32,
        "contextCommit": "c0" * 32,
        "modelBindingHash": "44" * 32,
        "sealedPromptCiphertextHash": "b7" * 32,
        "sealedPromptSize": 4096,
        "recipientKeyId": "key:workload-a",
        "maxOutputTokens": 256,
        "expiresAt": NOW + 3600,
    }
    base.update(over)
    return base


@pytest.fixture()
def spool(tmp_path):
    s = Spool(tmp_path)
    s.jobs_dir.mkdir(parents=True)
    return s


def queue(spool, j):
    (spool.jobs_dir / f"{j['idempotencyKey']}.json").write_text(json.dumps(j), encoding="utf-8")


def ok_executor(_job):
    return RECEIPT, BUNDLE


# ── validation ────────────────────────────────────────────────────────────────

def test_a_well_formed_job_validates():
    assert validate_job(job(), NOW)["idempotencyKey"] == "a1" * 32


@pytest.mark.parametrize(("over", "reason"), [
    ({"expiresAt": NOW - 1}, "expired"),
    ({"protocolVersion": 2}, "bad-version"),
    ({"maxOutputTokens": 0}, "bad-number"),
    ({"maxOutputTokens": True}, "bad-number"),
    ({"contextCommit": "C0" * 32}, "bad-hex"),
    ({"domain": "semantos.trinote.job.submit/v2"}, "bad-domain"),
])
def test_jobs_that_must_not_run(over, reason):
    with pytest.raises(JobRejected) as excinfo:
        validate_job(job(**over), NOW)
    assert excinfo.value.code == reason


def test_a_locator_is_refused_not_ignored():
    """A path would leak the control plane's deployment topology into the model host."""
    with pytest.raises(JobRejected) as excinfo:
        validate_job({**job(), "path": "/var/lib/semantos/blobs/ab12"}, NOW)
    assert excinfo.value.code == "unknown-field"


def test_a_missing_field_is_refused():
    incomplete = job()
    del incomplete["contextCommit"]
    with pytest.raises(JobRejected) as excinfo:
        validate_job(incomplete, NOW)
    assert excinfo.value.code == "missing-field"


# ── monotonic state ───────────────────────────────────────────────────────────

def test_states_move_forwards_only(spool):
    key = "a1" * 32
    spool.report(key, "accepted", attempt=1, updated_at=NOW)
    spool.report(key, "running", attempt=1, updated_at=NOW + 1)
    with pytest.raises(JobRejected) as excinfo:
        spool.report(key, "accepted", attempt=1, updated_at=NOW + 2)
    assert excinfo.value.code == "not-monotonic"


def test_a_terminal_state_cannot_be_reopened(spool):
    key = "a1" * 32
    spool.report(key, "complete", attempt=1, updated_at=NOW,
                 receipt_hash=RECEIPT, encrypted_bundle_hash=BUNDLE)
    with pytest.raises(JobRejected) as excinfo:
        spool.report(key, "running", attempt=1, updated_at=NOW + 1)
    assert excinfo.value.code == "terminal-state"


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"state": "complete"}, "state-mismatch"),
    ({"state": "failed"}, "state-mismatch"),
    ({"state": "running", "receipt_hash": RECEIPT}, "state-mismatch"),
])
def test_incoherent_states_never_reach_the_control_plane(spool, kwargs, reason):
    state = kwargs.pop("state")
    with pytest.raises(JobRejected) as excinfo:
        spool.report("a1" * 32, state, attempt=1, updated_at=NOW, **kwargs)
    assert excinfo.value.code == reason


def test_state_writes_are_atomic_and_leave_no_debris(spool):
    spool.report("a1" * 32, "accepted", attempt=1, updated_at=NOW)
    assert [p.name for p in spool.states_dir.iterdir()] == [f"{'a1' * 32}.json"]


# ── the loop ──────────────────────────────────────────────────────────────────

def test_a_valid_job_runs_and_reports_completion(spool):
    queue(spool, job())
    outcomes = run_once(spool, ok_executor, now=NOW)
    assert [o["state"] for o in outcomes] == ["complete"]
    assert outcomes[0]["receiptHash"] == RECEIPT


def test_an_expired_job_fails_instead_of_running(spool):
    queue(spool, job(expiresAt=NOW - 1))
    executed = []
    run_once(spool, lambda j: executed.append(j) or (RECEIPT, BUNDLE), now=NOW)
    assert executed == []
    assert spool.current_state("a1" * 32)["failureCode"] == "expired"


def test_a_second_delivery_does_not_execute_twice(spool):
    queue(spool, job())
    calls = []

    def counting(j):
        calls.append(j)
        return RECEIPT, BUNDLE

    run_once(spool, counting, now=NOW)
    run_once(spool, counting, now=NOW)     # same job still on the spool
    assert len(calls) == 1


def test_an_executor_that_raises_produces_a_failure_not_a_result(spool):
    queue(spool, job())

    def exploding(_job):
        raise RuntimeError("gpu fell over")

    outcomes = run_once(spool, exploding, now=NOW)
    assert outcomes[0]["state"] == "failed"
    assert outcomes[0]["receiptHash"] is None
    assert outcomes[0]["failureCode"] == "RuntimeError"


def test_an_unreadable_job_is_reported_rather_than_silently_dropped(spool):
    (spool.jobs_dir / f"{'a1' * 32}.json").write_text("{not json", encoding="utf-8")
    run_once(spool, ok_executor, now=NOW)
    assert spool.current_state("a1" * 32)["failureCode"] == "unreadable"


def test_an_unconfigured_queue_refuses_rather_than_defaulting():
    with pytest.raises(JobRejected) as excinfo:
        Spool.from_env({})
    assert excinfo.value.code == "queue-unconfigured"
