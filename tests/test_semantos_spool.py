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
    UNREPORTABLE_DOMAIN,
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


# ── nothing is discarded without a trace ──────────────────────────────────────

def test_a_job_rejected_for_its_own_key_is_still_reported(spool):
    """The rejection and the thing needed to report it were the same field.

    Reporting against the job's `idempotencyKey` cannot work when that key is what
    the job is being rejected for, so the filename the control plane chose is used
    instead. Without it this job gets no state at all and the control plane waits
    on it forever.
    """
    keyless = job()
    del keyless["idempotencyKey"]
    (spool.jobs_dir / f"{'a1' * 32}.json").write_text(json.dumps(keyless), encoding="utf-8")

    run_once(spool, ok_executor, now=NOW)

    state = spool.current_state("a1" * 32)
    assert state is not None, "a queued job produced no state at all"
    assert state["state"] == "failed"
    assert state["failureCode"] == "missing-field"


def test_a_job_nothing_can_name_is_surfaced_rather_than_dropped(spool):
    """When neither the job nor its filename can name a state file there is nothing
    to write, so it is returned to this worker's caller. A silent drop is
    indistinguishable from a job that was never queued."""
    (spool.jobs_dir / "not-a-key.json").write_text("{not json", encoding="utf-8")

    outcomes = run_once(spool, ok_executor, now=NOW)

    assert [o["domain"] for o in outcomes] == [UNREPORTABLE_DOMAIN]
    assert outcomes[0]["source"] == "not-a-key.json"
    assert outcomes[0]["failureCode"] == "unreadable"


# ── claiming is atomic, not read-then-write ───────────────────────────────────

def test_only_one_worker_can_claim_a_job(spool):
    a, b = Spool(spool.root), Spool(spool.root)
    assert a.claim("a1" * 32, attempt=1, updated_at=NOW) is not None
    assert b.claim("a1" * 32, attempt=1, updated_at=NOW) is None


def test_a_lost_claim_does_not_abandon_the_rest_of_the_batch(spool):
    """Two workers observing an unclaimed job both proceeded, and the loser raised
    out of run_once — taking every job still queued behind it down too."""
    first, second = "a1" * 32, "b2" * 32
    queue(spool, job())
    queue(spool, job(idempotencyKey=second))

    other = Spool(spool.root)
    other.claim(first, attempt=1, updated_at=NOW)      # another worker got there first

    executed = []
    outcomes = run_once(spool, lambda j: executed.append(j["idempotencyKey"]) or (RECEIPT, BUNDLE),
                        now=NOW)

    assert executed == [second], "the job behind the contended one was skipped"
    assert [o["state"] for o in outcomes] == ["complete"]


def test_a_claim_publishes_a_whole_state_and_leaves_no_debris(spool):
    spool.claim("a1" * 32, attempt=1, updated_at=NOW)
    names = sorted(p.name for p in spool.states_dir.iterdir())
    assert names == [f"{'a1' * 32}.json"]
    assert spool.current_state("a1" * 32)["state"] == "accepted"
