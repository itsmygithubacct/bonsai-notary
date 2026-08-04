"""Collect semantos inference jobs from a spool directory, report state back.

## Why a directory

The producer boundary requires the *worker* to initiate every connection: the
control plane must never need to dial the machine holding model weights. A spool
satisfies that without inventing a network protocol — semantos writes jobs, this
collects them, and nothing here listens on anything.

The transport is deliberately replaceable. Everything below deals in validated
wire objects (`semantos.trinote.job/v1`), so swapping the spool for an
authenticated outbound WebSocket changes this module and nothing else.

## What this module refuses to do

It does not run inference. Execution is injected as a callable, because deciding
*what* runs is composition and this repository is the composition layer, not the
engine — and because a worker that both defines and performs the work has no
boundary left to enforce.

It also never reports a state a job has already passed. States are monotonic:
`accepted → running → awaiting-signature → complete`, or `failed`. A worker that
could rewind could report `running` after `complete` and make a finished job look
in-flight forever.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol

PROTOCOL_VERSION = 1
MAX_ENCODED_BYTES = 65536

SUBMIT_DOMAIN = "semantos.trinote.job.submit/v1"
STATE_DOMAIN = "semantos.trinote.job.state/v1"
#: worker-local diagnostic, never a semantos wire object: emitted for a job so
#: malformed that no state file can be named for it
UNREPORTABLE_DOMAIN = "trinote.worker.unreportable/v1"

#: monotonic order; index comparison is the whole rule
STATE_ORDER = ("accepted", "running", "awaiting-signature", "complete")
TERMINAL = ("complete", "failed")

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_ASCII = re.compile(r"\A[\x20-\x7e]*\Z")

SUBMIT_FIELDS = frozenset((
    "domain", "protocolVersion", "idempotencyKey", "executionIntentHash",
    "contextCommit", "modelBindingHash", "sealedPromptCiphertextHash",
    "sealedPromptSize", "recipientKeyId", "maxOutputTokens", "expiresAt",
))


class JobRejected(ValueError):
    """A job that must not be executed. Carries the specification's reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Executor(Protocol):
    """What actually performs an inference for a validated job.

    Returns ``(receipt_hash, encrypted_bundle_hash)``. Raising is a normal outcome:
    the caller reports it as a failure with a bounded code, never as a result.
    """

    def __call__(self, job: dict) -> tuple[str, str]: ...


def _check_hex(value, key: str) -> None:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise JobRejected("bad-hex", key)


def validate_job(job, now: int | None = None) -> dict:
    """Structural validation of one `job.submit`. Well-formed is necessary, never
    sufficient — the signatures, the ciphertext and the replay all still apply."""
    if not isinstance(job, dict):
        raise JobRejected("bad-type", "job must be an object")

    encoded = json.dumps(job, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_ENCODED_BYTES:
        raise JobRejected("too-large", f"{len(encoded)} bytes")

    unknown = set(job) - SUBMIT_FIELDS
    if unknown:
        # a locator (path/uri/location) lands here on purpose: it would leak the
        # control plane's deployment topology into the model host
        raise JobRejected("unknown-field", ", ".join(sorted(unknown)))
    missing = SUBMIT_FIELDS - set(job)
    if missing:
        raise JobRejected("missing-field", ", ".join(sorted(missing)))

    if job["domain"] != SUBMIT_DOMAIN:
        raise JobRejected("bad-domain", str(job["domain"]))
    if job["protocolVersion"] != PROTOCOL_VERSION:
        # refuse rather than guess what an unknown version meant
        raise JobRejected("bad-version", str(job["protocolVersion"]))

    for key in ("idempotencyKey", "executionIntentHash", "contextCommit",
                "modelBindingHash", "sealedPromptCiphertextHash"):
        _check_hex(job[key], key)

    if not isinstance(job["recipientKeyId"], str) or not _ASCII.match(job["recipientKeyId"]):
        raise JobRejected("bad-charset", "recipientKeyId")

    for key in ("sealedPromptSize", "maxOutputTokens", "expiresAt"):
        value = job[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise JobRejected("bad-number", key)

    if now is not None and job["expiresAt"] <= now:
        raise JobRejected("expired", "the request expired before this worker collected it")

    return job


@dataclass(frozen=True)
class Spool:
    """A semantos job spool. `jobs/` is written by the control plane, `states/` by
    this worker; neither side writes the other's directory."""

    root: Path

    @classmethod
    def from_env(cls, env=os.environ) -> "Spool":
        root = env.get("TRINOTE_QUEUE_DIR")
        if not root:
            # no default: a worker polling a directory nobody writes looks exactly
            # like a worker with nothing to do
            raise JobRejected("queue-unconfigured", "TRINOTE_QUEUE_DIR is not set")
        return cls(Path(root))

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def states_dir(self) -> Path:
        return self.root / "states"

    def collect(self, now: int | None = None) -> Iterator[tuple[dict, JobRejected | None, Path]]:
        """Yield `(job, rejection, path)` for every queued job, oldest name first.

        A malformed job is yielded with its rejection rather than skipped: it still
        needs a `failed` state written, or the control plane waits forever for a
        job this worker silently discarded.

        The source path is yielded with it because a malformed job may be malformed
        in its `idempotencyKey`, and then the filename the control plane chose is the
        only identity left to report a failure against.
        """
        if not self.jobs_dir.is_dir():
            return
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                yield {}, JobRejected("unreadable", str(exc)), path
                continue
            try:
                yield validate_job(job, now), None, path
            except JobRejected as exc:
                yield job if isinstance(job, dict) else {}, exc, path

    def current_state(self, idempotency_key: str) -> dict | None:
        path = self.states_dir / f"{idempotency_key}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _record(self, idempotency_key: str, state: str, *, attempt: int, updated_at: int,
                receipt_hash: str | None = None, encrypted_bundle_hash: str | None = None,
                failure_code: str | None = None) -> dict:
        return {
            "domain": STATE_DOMAIN,
            "protocolVersion": PROTOCOL_VERSION,
            "idempotencyKey": idempotency_key,
            "state": state,
            "attempt": attempt,
            "updatedAt": updated_at,
            "receiptHash": receipt_hash,
            "encryptedBundleHash": encrypted_bundle_hash,
            "failureCode": failure_code,
        }

    def _publish(self, record: dict, target: Path, *, exclusive: bool) -> bool:
        """Write one state through a same-directory temp file, so semantos never
        reads a half-written state.

        `exclusive` publishes with a hard link, which fails when the target already
        exists. That makes taking a job atomic against another worker, where a read
        followed by a write leaves a window between the two.
        """
        self.states_dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.states_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            os.chmod(temp, 0o600)
            if not exclusive:
                os.replace(temp, target)      # atomic: no half-written state is ever read
                return True
            try:
                os.link(temp, target)         # atomic AND refuses an existing target
            except FileExistsError:
                return False
            return True
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def claim(self, idempotency_key: str, *, attempt: int, updated_at: int) -> dict | None:
        """Take a job by publishing its first state, or return None if another worker
        already holds it.

        Checking `current_state` and then writing would let two workers both observe
        nothing and both proceed; the loser then fails its own monotonic check and
        raises, abandoning every job still left in its batch.
        """
        _check_hex(idempotency_key, "idempotencyKey")
        record = self._record(idempotency_key, "accepted", attempt=attempt, updated_at=updated_at)
        target = self.states_dir / f"{idempotency_key}.json"
        return record if self._publish(record, target, exclusive=True) else None

    def report(self, idempotency_key: str, state: str, *, attempt: int, updated_at: int,
               receipt_hash: str | None = None, encrypted_bundle_hash: str | None = None,
               failure_code: str | None = None) -> dict:
        """Write one state, atomically, refusing any move that is not forwards."""
        _check_hex(idempotency_key, "idempotencyKey")
        if state not in (*STATE_ORDER, "failed"):
            raise JobRejected("bad-state", state)

        previous = self.current_state(idempotency_key)
        if previous is not None:
            old = previous.get("state")
            if old in TERMINAL:
                raise JobRejected("terminal-state", f"{old} is terminal")
            if state != "failed" and old in STATE_ORDER:
                if STATE_ORDER.index(state) <= STATE_ORDER.index(old):
                    raise JobRejected("not-monotonic", f"{old} -> {state}")

        record = self._record(idempotency_key, state, attempt=attempt, updated_at=updated_at,
                              receipt_hash=receipt_hash,
                              encrypted_bundle_hash=encrypted_bundle_hash,
                              failure_code=failure_code)

        # coherence, checked here so a malformed state never reaches the control
        # plane: completion means a receipt, failure means a code, and neither
        # means both
        if state == "complete" and not (receipt_hash and encrypted_bundle_hash):
            raise JobRejected("state-mismatch", "complete requires receipt and bundle hashes")
        if state == "complete" and failure_code is not None:
            raise JobRejected("state-mismatch", "complete cannot carry a failureCode")
        if state == "failed" and not failure_code:
            raise JobRejected("state-mismatch", "failed requires a failureCode")
        if state == "failed" and (receipt_hash or encrypted_bundle_hash):
            raise JobRejected("state-mismatch", "failed cannot carry result hashes")
        if state not in TERMINAL and (receipt_hash or encrypted_bundle_hash):
            raise JobRejected("state-mismatch", f"{state} cannot carry result hashes")

        self._publish(record, self.states_dir / f"{idempotency_key}.json", exclusive=False)
        return record


def _reporting_key(job: dict, path: Path) -> str | None:
    """The identity a failure can be reported against.

    The job's own `idempotencyKey` when it has a usable one, otherwise the filename
    the control plane chose, which is the handle it already holds for this job. A job
    rejected *for* its key would otherwise get no state written at all — the silent
    discard this module exists to prevent.
    """
    for candidate in (job.get("idempotencyKey"), path.stem):
        if isinstance(candidate, str) and _HEX64.match(candidate):
            return candidate
    return None


def run_once(spool: Spool, execute: Executor, *, now: int,
             clock: Callable[[], int] | None = None) -> list[dict]:
    """Collect every queued job, execute the valid ones, report each outcome.

    Returns the final state record per job. An executor that raises produces a
    `failed` state with a bounded code — never a result, and never a silent retry.

    A job that nothing can name yields an `UNREPORTABLE_DOMAIN` record in the returned
    list rather than a state file. That is the one case where no state can be written
    for semantos to read, so it is surfaced to this worker's own caller instead of
    dropped, because a drop is indistinguishable from a job that was never queued.
    """
    tick = clock or (lambda: now)
    outcomes: list[dict] = []

    for job, rejection, path in spool.collect(now):
        key = _reporting_key(job, path)

        if rejection is not None:
            if key is None:
                outcomes.append({"domain": UNREPORTABLE_DOMAIN, "source": path.name,
                                 "failureCode": rejection.code, "updatedAt": tick()})
                continue
            try:
                outcomes.append(spool.report(key, "failed", attempt=1, updated_at=tick(),
                                             failure_code=rejection.code))
            except JobRejected:
                # a terminal state already exists: this is a duplicate delivery of
                # something already resolved, so nothing is lost by not rewriting it
                pass
            continue

        claimed = spool.claim(key, attempt=1, updated_at=tick())
        if claimed is None:
            continue                          # another worker holds it; exactly-once
        spool.report(key, "running", attempt=1, updated_at=tick())
        try:
            receipt_hash, bundle_hash = execute(job)
        except Exception as exc:                                  # noqa: BLE001
            code = getattr(exc, "code", None) or exc.__class__.__name__
            outcomes.append(spool.report(key, "failed", attempt=1, updated_at=tick(),
                                         failure_code=str(code)[:64]))
            continue
        outcomes.append(spool.report(key, "complete", attempt=1, updated_at=tick(),
                                     receipt_hash=receipt_hash,
                                     encrypted_bundle_hash=bundle_hash))

    return outcomes
