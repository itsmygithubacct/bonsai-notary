#!/usr/bin/env python3
"""Provider-neutral, fail-closed lifecycle controller.

Cloud-specific API access lives behind a small executable adapter protocol.
``plan`` and ``status`` are read-only.  ``up`` requires two explicit billing
confirmations; an idempotency token is durably persisted before every create.
No adapter is bundled and this module never imports provider credentials.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


DESCRIPTOR_SCHEMA = "provider-node/v1"
STATE_SCHEMA = "provider-lifecycle/v1"
ADAPTER_TIMEOUT_DEFAULT = 30.0
ADAPTER_TIMEOUT_MAX = 300.0
ADAPTER_TERMINATE_GRACE = 0.25
ADAPTER_REAP_TIMEOUT = 5.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def require_number(label: str, value: Any, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    parsed = float(value)
    if parsed < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return parsed


def validate_descriptor(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != DESCRIPTOR_SCHEMA:
        raise ValueError(f"descriptor schema must be {DESCRIPTOR_SCHEMA}")
    for key in ("provider", "image"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"descriptor.{key} must be a non-empty string")
    requirements = value.get("requirements")
    limits = value.get("limits")
    if not isinstance(requirements, dict) or not isinstance(limits, dict):
        raise ValueError("descriptor requires requirements and limits objects")
    for key in ("minCpuCores", "minRamGb", "minGpuRamGb"):
        require_number(f"requirements.{key}", requirements.get(key), minimum=0)
    for key in ("baseHourlyUsd", "storageHourlyUsd", "totalHourlyUsd"):
        require_number(f"limits.{key}", limits.get(key), minimum=0)
    attempts = limits.get("maxAttempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts <= 0:
        raise ValueError("limits.maxAttempts must be a positive integer")
    storage = value.get("storageGb")
    if isinstance(storage, bool) or not isinstance(storage, int) or storage <= 0:
        raise ValueError("descriptor.storageGb must be a positive integer")
    timeout = value.get("sshTimeoutSeconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("descriptor.sshTimeoutSeconds must be a positive integer")
    poll = value.get("pollSeconds", 5)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 0 < poll <= 60:
        raise ValueError("descriptor.pollSeconds must be in (0, 60]")
    adapter_timeout = require_number(
        "descriptor.adapterTimeoutSeconds",
        value.get("adapterTimeoutSeconds", ADAPTER_TIMEOUT_DEFAULT),
        minimum=0.01,
    )
    if adapter_timeout > ADAPTER_TIMEOUT_MAX:
        raise ValueError(f"descriptor.adapterTimeoutSeconds must be at most {ADAPTER_TIMEOUT_MAX:g}")
    return value


class AdapterError(RuntimeError):
    """A bounded provider-adapter operation failed or violated its contract."""


class AdapterTimeout(AdapterError):
    """The adapter process exceeded its configured operation deadline."""


def _signal_process_group(process: subprocess.Popen[str], signum: int) -> None:
    """Signal an adapter's private process group, with a leader-only fallback."""
    try:
        os.killpg(process.pid, signum)
        return
    except ProcessLookupError:
        return
    except OSError:
        # ``start_new_session`` makes pid == pgid.  The fallback is only for
        # unusual platforms where group signalling is unavailable after Popen.
        pass
    try:
        process.send_signal(signum)
    except ProcessLookupError:
        pass


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_signal_mask() -> set[signal.Signals] | None:
    """Defer repeated lifecycle interrupts while an adapter is being reaped."""
    if not hasattr(signal, "pthread_sigmask"):
        return None
    return signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})


def _restore_signal_mask(previous: set[signal.Signals] | None) -> None:
    if previous is not None:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _communicate_for_cleanup(process: subprocess.Popen[str], timeout: float) -> bool:
    """Drain and reap despite a second asynchronous BaseException."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            process.communicate(timeout=remaining)
            return True
        except subprocess.TimeoutExpired:
            return False
        except BaseException:
            # SIGINT/SIGTERM are masked by the caller.  This also makes cleanup
            # robust to another asynchronous BaseException injected by a host.
            continue


def _terminate_and_reap_process_group(
        process: subprocess.Popen[str], *, graceful: bool) -> None:
    """Synchronously eliminate an adapter session and reap its direct child."""
    previous_mask = _cleanup_signal_mask()
    try:
        if graceful:
            _signal_process_group(process, signal.SIGTERM)
            _communicate_for_cleanup(process, ADAPTER_TERMINATE_GRACE)

        # The session leader may have exited on SIGTERM while a descendant
        # ignored it.  Check the group independently of ``process.returncode``.
        if _process_group_exists(process.pid):
            _signal_process_group(process, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()

        if not _communicate_for_cleanup(process, ADAPTER_REAP_TIMEOUT):
            _signal_process_group(process, signal.SIGKILL)
            process.kill()
            if not _communicate_for_cleanup(process, ADAPTER_REAP_TIMEOUT):
                raise AdapterError("adapter process could not be reaped after termination")
    finally:
        _restore_signal_mask(previous_mask)


def _adapter_interrupted(signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


class Adapter:
    def __init__(self, executable: Path, timeout_seconds: float = ADAPTER_TIMEOUT_DEFAULT):
        executable = executable.expanduser().resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"provider adapter is not executable: {executable}")
        self.executable = executable
        self.timeout_seconds = require_number("adapter timeout", timeout_seconds, minimum=0.01)
        if self.timeout_seconds > ADAPTER_TIMEOUT_MAX:
            raise ValueError(f"adapter timeout must be at most {ADAPTER_TIMEOUT_MAX:g} seconds")

    def call(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # Block lifecycle signals across Popen so a pending signal cannot land
        # after fork but before ``process`` is assigned.  Temporary handlers
        # turn SIGTERM into a Python exception just like SIGINT, including for
        # read-only commands that do not install ``up``'s outer handlers.
        previous_mask = _cleanup_signal_mask()
        old_handlers: dict[int, Any] = {}
        process: subprocess.Popen[str] | None = None
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    old_handlers[signum] = signal.signal(signum, _adapter_interrupted)
                except ValueError:
                    # Python only permits handler installation in the main
                    # thread.  BaseException cleanup remains active for calls
                    # made by library worker threads.
                    break
            process = subprocess.Popen(
                [str(self.executable), operation],
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            # Mark the mask restored before unblocking.  Delivery of a pending
            # signal can raise from pthread_sigmask, but ``process`` is now
            # available to the BaseException cleanup below.
            mask_to_restore = previous_mask
            previous_mask = None
            _restore_signal_mask(mask_to_restore)
            try:
                stdout, stderr = process.communicate(
                    json.dumps(payload or {}, sort_keys=True), timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                _terminate_and_reap_process_group(process, graceful=False)
                raise AdapterTimeout(
                    f"adapter {operation} timed out after {self.timeout_seconds:g} seconds"
                ) from exc
            except BaseException:
                _terminate_and_reap_process_group(process, graceful=True)
                raise
        except BaseException:
            if process is not None and process.poll() is None:
                _terminate_and_reap_process_group(process, graceful=True)
            raise
        finally:
            try:
                _restore_signal_mask(previous_mask)
            finally:
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
        assert process is not None
        if process.returncode:
            detail = stderr.strip() or f"exit {process.returncode}"
            raise AdapterError(f"adapter {operation} failed: {detail[:1000]}")
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"adapter {operation} emitted invalid JSON") from exc
        if not isinstance(result, dict):
            raise AdapterError(f"adapter {operation} must emit a JSON object")
        return result


def validate_ssh_key(path: Path) -> tuple[Path, Path]:
    path = path.expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"SSH private key must be a regular non-symlink file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(f"SSH private key permissions must not allow group/other access (mode {mode:o})")
    prefix = path.read_bytes()[:80]
    begin_marker = b"-----BEGIN "
    private_key_marker = b"PRIVATE" + b" KEY-----"
    accepted = (
        begin_marker + b"OPENSSH " + private_key_marker,
        begin_marker + b"RSA " + private_key_marker,
        begin_marker + b"EC " + private_key_marker,
    )
    if not prefix.startswith(accepted):
        raise ValueError("SSH key is not a supported private-key file")
    public_path = Path(str(path) + ".pub")
    if not public_path.is_file() or public_path.is_symlink():
        raise ValueError(f"SSH public key must be a regular non-symlink sibling: {public_path}")
    public = public_path.read_text(encoding="ascii").strip()
    if "\n" in public or not re.fullmatch(
        r"(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+(?: [^\r\n]+)?",
        public,
    ):
        raise ValueError("SSH public key has an invalid authorized_keys form")
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        raise ValueError("ssh-keygen is required to validate the SSH keypair")
    try:
        derived = subprocess.run(
            [ssh_keygen, "-y", "-f", str(path)], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("ssh-keygen validation timed out") from exc
    if derived.returncode:
        raise ValueError("SSH private key failed ssh-keygen validation")
    if derived.stdout.strip().split()[:2] != public.split()[:2]:
        raise ValueError("SSH private key does not match its .pub sibling")
    return path, public_path


def offer_total(offer: dict[str, Any], storage_gb: int) -> tuple[float, float, float]:
    base = require_number("offer.baseHourlyUsd", offer.get("baseHourlyUsd"), minimum=0)
    storage_per_gb = require_number("offer.storageHourlyUsdPerGb", offer.get("storageHourlyUsdPerGb", 0), minimum=0)
    storage = storage_per_gb * storage_gb
    return base, storage, base + storage


def eligible_offers(descriptor: dict[str, Any], offers: Sequence[dict[str, Any]], excluded: set[str]) -> list[dict[str, Any]]:
    req = descriptor["requirements"]
    limits = descriptor["limits"]
    selected: list[dict[str, Any]] = []
    for offer in offers:
        try:
            if str(offer.get("machineId")) in excluded:
                continue
            if require_number("offer.cpuCores", offer.get("cpuCores"), minimum=0) < req["minCpuCores"]:
                continue
            if require_number("offer.ramGb", offer.get("ramGb"), minimum=0) < req["minRamGb"]:
                continue
            if require_number("offer.gpuRamGb", offer.get("gpuRamGb"), minimum=0) < req["minGpuRamGb"]:
                continue
            if req.get("gpu") and str(req["gpu"]).lower() not in str(offer.get("gpu", "")).lower():
                continue
            if req.get("computeCapability") and str(offer.get("computeCapability")) != str(req["computeCapability"]):
                continue
            base, storage, total = offer_total(offer, descriptor["storageGb"])
            if base > limits["baseHourlyUsd"] or storage > limits["storageHourlyUsd"] or total > limits["totalHourlyUsd"]:
                continue
            normalized = dict(offer, resolvedBaseHourlyUsd=base,
                              resolvedStorageHourlyUsd=storage, resolvedTotalHourlyUsd=total)
            selected.append(normalized)
        except (TypeError, ValueError):
            continue
    return sorted(selected, key=lambda item: (item["resolvedTotalHourlyUsd"], str(item.get("offerId"))))


def initial_state(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "provider": descriptor["provider"],
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "descriptor": descriptor,
        "excludedMachineIds": [],
        "instances": [],
        "audit": None,
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updatedAt"] = utc_now()
    atomic_json(path, state)


def _adapter_timeout(args: argparse.Namespace, descriptor: dict[str, Any] | None = None) -> float:
    explicit = getattr(args, "adapter_timeout_seconds", None)
    if explicit is not None:
        timeout = require_number("--adapter-timeout-seconds", explicit, minimum=0.01)
    else:
        timeout = require_number(
            "descriptor.adapterTimeoutSeconds",
            (descriptor or {}).get("adapterTimeoutSeconds", ADAPTER_TIMEOUT_DEFAULT),
            minimum=0.01,
        )
    if timeout > ADAPTER_TIMEOUT_MAX:
        raise ValueError(f"adapter timeout must be at most {ADAPTER_TIMEOUT_MAX:g} seconds")
    return timeout


def _new_create_record(attempt: int, offer: dict[str, Any]) -> dict[str, Any]:
    """Build the state that is fsynced before any billable create request."""
    return {
        "attempt": attempt,
        "idempotencyToken": secrets.token_hex(32),
        "createStatus": "pending",
        "createRequestedAt": utc_now(),
        "instanceId": None,
        "machineId": str(offer.get("machineId")),
        "offerId": offer.get("offerId"),
        "gpu": offer.get("gpu"),
        "cpuCores": offer.get("cpuCores"),
        "baseHourlyUsd": offer["resolvedBaseHourlyUsd"],
        "storageHourlyUsd": offer["resolvedStorageHourlyUsd"],
        "totalHourlyUsd": offer["resolvedTotalHourlyUsd"],
        "ready": False,
        "destroyVerified": False,
    }


def _record_created(record: dict[str, Any], response: dict[str, Any]) -> None:
    """Validate and persistable-normalize a create/reconcile 'created' result."""
    token = record.get("idempotencyToken")
    if response.get("idempotencyToken") != token:
        raise AdapterError("adapter response did not echo the persisted idempotency token")
    instance_id = response.get("instanceId")
    if isinstance(instance_id, bool) or not isinstance(instance_id, (str, int)) or not str(instance_id):
        raise AdapterError("adapter created response has no instanceId")
    normalized = str(instance_id)
    if record.get("instanceId") not in (None, normalized):
        raise AdapterError("adapter reconciliation changed the instanceId for one idempotency token")
    record["instanceId"] = normalized
    record["machineId"] = str(response.get("machineId") or record.get("machineId"))
    record["createStatus"] = "created"
    record["createdAt"] = record.get("createdAt") or utc_now()
    record["reconciledAt"] = utc_now()


def _reconcile_record(adapter: Adapter, record: dict[str, Any]) -> str:
    """Resolve a persisted create token to created, absent, or unresolved."""
    token = record.get("idempotencyToken")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise AdapterError("persisted create record has an invalid idempotency token")
    response = adapter.call("reconcile", {"idempotencyToken": token})
    if response.get("idempotencyToken") != token:
        raise AdapterError("adapter reconcile response did not echo the idempotency token")
    status = response.get("status")
    if status == "created":
        _record_created(record, response)
        return "created"
    if status == "absent":
        record["createStatus"] = "absent"
        record["reconciledAt"] = utc_now()
        record["destroyVerified"] = True
        return "absent"
    if status == "pending":
        record["createStatus"] = "reconciliationRequired"
        record["reconciledAt"] = utc_now()
        return "pending"
    raise AdapterError("adapter reconcile response needs status created, absent, or pending")


def plan(args: argparse.Namespace) -> int:
    descriptor = validate_descriptor(load_json(args.descriptor))
    adapter = Adapter(args.adapter, _adapter_timeout(args, descriptor))
    response = adapter.call("offers", {"descriptor": descriptor})
    offers = response.get("offers")
    if not isinstance(offers, list):
        raise RuntimeError("adapter offers response needs an offers array")
    eligible = eligible_offers(descriptor, offers, set())
    if not eligible:
        print(json.dumps({"schema": "provider-plan/v1", "eligible": 0, "selection": None}, sort_keys=True))
        return 3
    selected = eligible[0]
    public = {
        "schema": "provider-plan/v1", "provider": descriptor["provider"],
        "eligible": len(eligible),
        "selection": {
            key: selected.get(key) for key in (
                "offerId", "machineId", "gpu", "computeCapability", "cpuCores", "ramGb", "gpuRamGb",
                "resolvedBaseHourlyUsd", "resolvedStorageHourlyUsd", "resolvedTotalHourlyUsd",
            )
        },
        "billingStarted": False,
    }
    print(json.dumps(public, indent=2, sort_keys=True))
    return 0


def _destroy_recorded(adapter: Adapter, state_path: Path, state: dict[str, Any]) -> int:
    first_failure = 0
    for instance in state.get("instances", []):
        if instance.get("destroyVerified") is True:
            continue
        # State-schema v1 records written before create-token hardening already
        # contain the billable ID and remain safely destroyable.
        if instance.get("instanceId") and "idempotencyToken" not in instance:
            instance["createStatus"] = "created"
        if instance.get("createStatus") != "created" or not instance.get("instanceId"):
            try:
                reconciled = _reconcile_record(adapter, instance)
                instance.pop("reconcileError", None)
            except Exception as exc:
                reconciled = "unresolved"
                instance["createStatus"] = "reconciliationRequired"
                instance["reconcileError"] = str(exc)
            save_state(state_path, state)
            if reconciled == "absent":
                continue
            if reconciled != "created":
                if not first_failure:
                    first_failure = 1
                continue
        try:
            result = adapter.call("destroy", {
                "instanceId": instance["instanceId"],
                "idempotencyToken": instance.get("idempotencyToken"),
            })
            ok = result.get("destroyed") is True
        except Exception as exc:
            ok = False
            instance["destroyError"] = str(exc)
        instance["destroyAttemptedAt"] = utc_now()
        instance["destroyVerified"] = ok
        if not ok and not first_failure:
            first_failure = 1
        save_state(state_path, state)
    try:
        audit = adapter.call("active", {})
        active = audit.get("instances")
        if not isinstance(active, list):
            raise RuntimeError("adapter active response needs an instances array")
        zero = len(active) == 0
        state["audit"] = {"checkedAt": utc_now(), "activeCount": len(active), "zeroActive": zero}
        if not zero and not first_failure:
            first_failure = 4
    except Exception as exc:
        state["audit"] = {"checkedAt": utc_now(), "zeroActive": False, "error": str(exc)}
        if not first_failure:
            first_failure = 4
    save_state(state_path, state)
    return first_failure


def up(args: argparse.Namespace) -> int:
    descriptor = validate_descriptor(load_json(args.descriptor))
    if not args.authorize_billing:
        raise ValueError("up requires --authorize-billing")
    confirmed = require_number("--confirm-max-hourly-usd", args.confirm_max_hourly_usd, minimum=0)
    ceiling = float(descriptor["limits"]["totalHourlyUsd"])
    if not math.isclose(confirmed, ceiling, rel_tol=0, abs_tol=1e-9):
        raise ValueError(f"--confirm-max-hourly-usd must exactly match the descriptor ceiling ({ceiling})")
    _ssh_private_key, ssh_public_key_path = validate_ssh_key(args.ssh_key)
    ssh_public_key = ssh_public_key_path.read_text(encoding="ascii").strip()
    adapter = Adapter(args.adapter, _adapter_timeout(args, descriptor))
    state_path = args.state.resolve()
    if state_path.exists():
        state = load_json(state_path)
        if any(item.get("destroyVerified") is not True for item in state.get("instances", [])):
            raise ValueError("state already contains an instance not verified destroyed")
    state = initial_state(descriptor)
    save_state(state_path, state)
    excluded: set[str] = set()

    old_handlers: dict[int, Any] = {}
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, interrupted)
    try:
        for attempt in range(1, descriptor["limits"]["maxAttempts"] + 1):
            response = adapter.call("offers", {"descriptor": descriptor, "excludeMachineIds": sorted(excluded)})
            offers = response.get("offers")
            if not isinstance(offers, list):
                raise RuntimeError("adapter offers response needs an offers array")
            eligible = eligible_offers(descriptor, offers, excluded)
            if not eligible:
                raise RuntimeError("no eligible offer remains within the cost and hardware limits")
            offer = eligible[0]
            record = _new_create_record(attempt, offer)
            # Billing-safety invariant: persist and fsync the lookup token
            # before the create request can reach a provider.  A timeout or
            # malformed response can then be reconciled without guessing IDs.
            state["instances"].append(record)
            save_state(state_path, state)
            create_payload = {
                "offerId": offer.get("offerId"), "image": descriptor["image"],
                "storageGb": descriptor["storageGb"], "label": descriptor.get("label", "bonsai-acceptance"),
                "sshPublicKey": ssh_public_key,
                "idempotencyToken": record["idempotencyToken"],
            }
            try:
                created = adapter.call("create", create_payload)
                _record_created(record, created)
            except (OSError, AdapterError) as exc:
                record["createResponseError"] = type(exc).__name__
                record["createStatus"] = "reconciliationRequired"
                save_state(state_path, state)
                try:
                    outcome = _reconcile_record(adapter, record)
                    record.pop("reconcileError", None)
                except (OSError, AdapterError) as reconcile_exc:
                    record["reconcileError"] = type(reconcile_exc).__name__
                    save_state(state_path, state)
                    raise RuntimeError(
                        "create outcome is unresolved; persisted idempotency token requires reconciliation"
                    ) from reconcile_exc
                save_state(state_path, state)
                if outcome == "absent":
                    raise RuntimeError("create failed and reconciliation confirmed no instance") from exc
                if outcome != "created":
                    raise RuntimeError(
                        "create outcome remains pending; persisted idempotency token requires reconciliation"
                    ) from exc
            save_state(state_path, state)
            instance_id = record["instanceId"]
            machine_id = record["machineId"]
            deadline = time.monotonic() + descriptor.get("sshTimeoutSeconds", 600)
            while time.monotonic() < deadline:
                readiness = adapter.call("ready", {"instanceId": str(instance_id)})
                if readiness.get("ready") is True:
                    record["ready"] = True
                    record["readyAt"] = utc_now()
                    # Endpoint is private operational state and is never printed.
                    if isinstance(readiness.get("ssh"), dict):
                        record["ssh"] = readiness["ssh"]
                    save_state(state_path, state)
                    print(json.dumps({
                        "schema": STATE_SCHEMA, "status": "ready", "instanceId": str(instance_id),
                        "machineId": machine_id, "state": str(state_path),
                    }, sort_keys=True))
                    return 0
                time.sleep(float(descriptor.get("pollSeconds", 5)))
            record["readinessError"] = "bounded SSH readiness timeout"
            excluded.add(machine_id)
            state["excludedMachineIds"] = sorted(excluded)
            save_state(state_path, state)
            destroyed = adapter.call("destroy", {
                "instanceId": str(instance_id),
                "idempotencyToken": record["idempotencyToken"],
            })
            record["destroyAttemptedAt"] = utc_now()
            record["destroyVerified"] = destroyed.get("destroyed") is True
            save_state(state_path, state)
            if not record["destroyVerified"]:
                raise RuntimeError("failed instance could not be verified destroyed")
        raise RuntimeError("maximum rental attempts exhausted")
    except BaseException:
        _destroy_recorded(adapter, state_path, state)
        raise
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def status(args: argparse.Namespace) -> int:
    state = load_json(args.state)
    summary = {
        "schema": STATE_SCHEMA, "provider": state.get("provider"),
        "instances": [
            {key: item.get(key) for key in (
                "instanceId", "machineId", "createStatus", "ready", "destroyVerified",
            )}
            for item in state.get("instances", [])
        ],
        "audit": state.get("audit"),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def reconcile(args: argparse.Namespace) -> int:
    """Resolve persisted ambiguous create tokens without issuing a create."""
    adapter = Adapter(args.adapter, _adapter_timeout(args))
    state_path = args.state.resolve()
    state = load_json(state_path)
    resolved = 0
    unresolved = 0
    for record in state.get("instances", []):
        if record.get("instanceId") and "idempotencyToken" not in record:
            record["createStatus"] = "created"
        if record.get("createStatus") in ("created", "absent"):
            continue
        try:
            outcome = _reconcile_record(adapter, record)
            record.pop("reconcileError", None)
            if outcome in ("created", "absent"):
                resolved += 1
            else:
                unresolved += 1
        except Exception as exc:
            record["createStatus"] = "reconciliationRequired"
            record["reconcileError"] = str(exc)
            unresolved += 1
        save_state(state_path, state)
    print(json.dumps({
        "schema": STATE_SCHEMA,
        "status": "reconciled" if unresolved == 0 else "reconciliation-required",
        "resolved": resolved,
        "unresolved": unresolved,
        "state": str(state_path),
    }, sort_keys=True))
    return 0 if unresolved == 0 else 4


def down(args: argparse.Namespace) -> int:
    if not args.authorize_destroy:
        raise ValueError("down requires --authorize-destroy")
    adapter = Adapter(args.adapter, _adapter_timeout(args))
    state_path = args.state.resolve()
    state = load_json(state_path)
    code = _destroy_recorded(adapter, state_path, state)
    print(json.dumps({
        "schema": STATE_SCHEMA, "status": "destroyed" if code == 0 else "cleanup-failed",
        "zeroActive": (state.get("audit") or {}).get("zeroActive") is True,
        "state": str(state_path),
    }, sort_keys=True))
    return code


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Provider-neutral, state-first GPU node lifecycle controller")
    sub = ap.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan", help="read-only offer selection")
    plan_cmd.add_argument("--descriptor", type=Path, required=True)
    plan_cmd.add_argument("--adapter", type=Path, required=True)
    plan_cmd.add_argument("--adapter-timeout-seconds", type=float)
    plan_cmd.set_defaults(handler=plan)
    up_cmd = sub.add_parser("up", help="rent one node; explicit billing authorization required")
    up_cmd.add_argument("--descriptor", type=Path, required=True)
    up_cmd.add_argument("--adapter", type=Path, required=True)
    up_cmd.add_argument("--state", type=Path, required=True)
    up_cmd.add_argument("--ssh-key", type=Path, required=True)
    up_cmd.add_argument("--authorize-billing", action="store_true")
    up_cmd.add_argument("--confirm-max-hourly-usd", type=float)
    up_cmd.add_argument("--adapter-timeout-seconds", type=float)
    up_cmd.set_defaults(handler=up)
    status_cmd = sub.add_parser("status", help="show persisted local lifecycle state")
    status_cmd.add_argument("--state", type=Path, required=True)
    status_cmd.set_defaults(handler=status)
    reconcile_cmd = sub.add_parser("reconcile", help="resolve persisted ambiguous create tokens")
    reconcile_cmd.add_argument("--adapter", type=Path, required=True)
    reconcile_cmd.add_argument("--state", type=Path, required=True)
    reconcile_cmd.add_argument("--adapter-timeout-seconds", type=float, default=ADAPTER_TIMEOUT_DEFAULT)
    reconcile_cmd.set_defaults(handler=reconcile)
    down_cmd = sub.add_parser("down", help="destroy recorded nodes and assert the account is empty")
    down_cmd.add_argument("--adapter", type=Path, required=True)
    down_cmd.add_argument("--state", type=Path, required=True)
    down_cmd.add_argument("--authorize-destroy", action="store_true")
    down_cmd.add_argument("--adapter-timeout-seconds", type=float, default=ADAPTER_TIMEOUT_DEFAULT)
    down_cmd.set_defaults(handler=down)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("provider-lifecycle: interrupted; recorded instances were sent to cleanup", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"provider-lifecycle: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
