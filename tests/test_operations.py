"""Offline tests for acceptance evidence, handoff trust states, and fleet safety."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from notary_tools import acceptance, evidence, handoff
from operations import provider_lifecycle as lifecycle


ROOT = Path(__file__).resolve().parents[1]
MODEL_PUBKEY = "02" + "11" * 32
COUNTERPARTY_PUBKEY = "03" + "22" * 32
HANDOFF_REQUIRED_TRUE_CLAIMS = (
    "offlineOk",
    "reexecOk",
    "signaturePinned",
    "sigModelPresent",
    "sigCounterpartyPresent",
    "sigModelAuthenticated",
    "sigCounterpartyAuthenticated",
    "rawSigModelOk",
    "rawSigCounterpartyOk",
    "rawSigModelAuthenticated",
    "rawSigCounterpartyAuthenticated",
)


def _pending_args() -> list[str]:
    return [
        "--model-pubkey", MODEL_PUBKEY,
        "--counterparty-pubkey", COUNTERPARTY_PUBKEY,
    ]


def test_acceptance_dry_run_declares_fail_closed_phases_and_no_broadcast(tmp_path):
    target = tmp_path / "must-not-exist"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "accept-gpu.py"), "--dry-run",
         "--record-dir", str(target), "--cpu-threads", "20"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["schema"] == "receipt-run/v1"
    assert plan["gpuRequired"] is True
    assert plan["cpuThreads"] == 20
    assert plan["networkBroadcastAttempted"] is False
    assert plan["phases"] == [
        "prerequisites", "gpu-identity", "cuda-availability", "telemetry-start",
        "cuda-parity", "signer-metadata", "one-token-receipt", "producer-report", "bundle",
        "bundle-verification", "verifier-report",
        "telemetry-stop",
    ]
    assert not target.exists()


def test_acceptance_prerequisite_failure_is_structured_and_preserves_exit(tmp_path):
    target = tmp_path / "evidence"
    state = tmp_path / "missing-state"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "accept-gpu.py"),
         "--record-dir", str(target), "--notary-home", str(state),
         "--engine-dir", str(ROOT / "engine"),
         "--python", str(ROOT / "engine" / "bonsai" / ".venv" / "bin" / "python"),
         "--cpu-threads", "2"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 4
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["schema"] == "receipt-run/v1"
    assert manifest["status"] == "fail"
    assert manifest["firstFailure"]["phase"] == "prerequisites"
    assert manifest["firstFailure"]["exitCode"] == 4
    assert manifest["cleanup"]["networkBroadcastAttempted"] is False
    assert (target / "SHA256SUMS").is_file()


def test_acceptance_command_phase_preserves_exact_first_nonzero_exit(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence.initialize(evidence_root)
    args = acceptance.build_parser().parse_args([
        "--notary-home", str(tmp_path / "state"),
        "--engine-dir", str(ROOT / "engine"),
        "--python", sys.executable,
        "--artifact", str(tmp_path / "artifact.safetensors"),
        "--progress-seconds", "1",
    ])
    args.prompt = acceptance.FIXED_PROMPT
    runner = acceptance.Runner(args, evidence_root)
    with pytest.raises(acceptance.AcceptanceFailure) as raised:
        runner.command_phase("deliberate-failure", [sys.executable, "-c", "raise SystemExit(23)"])
    assert raised.value.code == 23
    assert runner.first_failure == {
        "phase": "deliberate-failure", "exitCode": 23, "message": "command exited 23",
    }


def test_acceptance_command_phase_has_hard_timeout(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence.initialize(evidence_root)
    args = acceptance.build_parser().parse_args([
        "--notary-home", str(tmp_path / "state"),
        "--engine-dir", str(ROOT / "engine"),
        "--python", sys.executable,
        "--artifact", str(tmp_path / "artifact.safetensors"),
        "--progress-seconds", "1",
        "--command-timeout-seconds", "1",
    ])
    args.prompt = acceptance.FIXED_PROMPT
    runner = acceptance.Runner(args, evidence_root)
    started = time.monotonic()
    with pytest.raises(acceptance.AcceptanceFailure) as raised:
        runner.command_phase("bounded-sleep", [sys.executable, "-c", "import time; time.sleep(30)"])
    assert raised.value.code == acceptance.COMMAND_TIMEOUT_EXIT
    assert time.monotonic() - started < 5
    assert "timed out" in (evidence_root / "raw" / "bounded-sleep.stderr.log").read_text()


def _init_git_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run([
        "git", "-C", str(path), "-c", "user.name=fixture",
        "-c", "user.email=fixture@invalid", "commit", "-q", "-m", "fixture",
    ], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, text=True, capture_output=True,
    ).stdout.strip()


def _clean_composition(tmp_path: Path):
    engine = tmp_path / "integer_inference_engine"
    chain = tmp_path / "chain_c"
    third = tmp_path / "bsv_third_entry"
    revisions = {
        "integer_inference_engine": _init_git_repo(engine),
        "chain_c": _init_git_repo(chain),
        "bsv_third_entry": _init_git_repo(third),
    }
    notary = tmp_path / "notary"
    notary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(notary)], check=True)
    (notary / "dependencies.lock").write_text("".join(
        f"{name} {revision}\n" for name, revision in revisions.items()
    ))
    os.symlink(chain, notary / "chain_c")
    os.symlink(third, notary / "bsv_third_entry")
    subprocess.run(["git", "-C", str(notary), "add", "dependencies.lock", "chain_c", "bsv_third_entry"], check=True)
    subprocess.run([
        "git", "-C", str(notary), "-c", "user.name=fixture",
        "-c", "user.email=fixture@invalid", "commit", "-q", "-m", "composition",
    ], check=True)
    return notary, engine, revisions


def test_acceptance_source_gate_requires_clean_lock_matched_composition(tmp_path):
    notary, engine, revisions = _clean_composition(tmp_path)
    record = acceptance.require_clean_composition(notary, engine)
    assert record["notary"]["treeState"] == "clean"
    assert record["engine"]["revision"] == revisions["integer_inference_engine"]
    assert record["engine"]["lockMatch"] is True

    (engine / "uncommitted.txt").write_text("not represented by the commit\n")
    with pytest.raises(ValueError, match="engine source tree is dirty"):
        acceptance.require_clean_composition(notary, engine)
    dirty = acceptance.composition_source_record(notary, engine)["engine"]
    assert dirty["treeState"] == "dirty"
    assert dirty["revision"] is None
    assert dirty["baseCommit"] == revisions["integer_inference_engine"]


def test_acceptance_source_gate_rejects_clean_dependency_head_mismatch(tmp_path):
    notary, engine, revisions = _clean_composition(tmp_path)
    (engine / "tracked.txt").write_text("new clean commit\n")
    subprocess.run(["git", "-C", str(engine), "add", "tracked.txt"], check=True)
    subprocess.run([
        "git", "-C", str(engine), "-c", "user.name=fixture",
        "-c", "user.email=fixture@invalid", "commit", "-q", "-m", "mismatch",
    ], check=True)
    with pytest.raises(ValueError, match="engine commit does not match dependencies.lock"):
        acceptance.require_clean_composition(notary, engine)
    state = acceptance.composition_source_record(notary, engine)["engine"]
    assert state["treeState"] == "clean"
    assert state["revision"] != revisions["integer_inference_engine"]
    assert state["lockMatch"] is False


def test_acceptance_manifest_refreshes_sources_and_rejects_post_gate_changes(
        tmp_path, monkeypatch):
    notary, engine, revisions = _clean_composition(tmp_path)
    monkeypatch.setattr(acceptance, "ROOT", notary)
    evidence_root = tmp_path / "evidence"
    evidence.initialize(evidence_root)
    args = acceptance.build_parser().parse_args([
        "--notary-home", str(tmp_path / "state"), "--engine-dir", str(engine),
        "--python", sys.executable, "--artifact", str(tmp_path / "artifact"),
    ])
    args.prompt = acceptance.FIXED_PROMPT
    runner = acceptance.Runner(args, evidence_root)
    runner.sources = acceptance.require_clean_composition(notary, engine)
    runner.initial_sources = runner.sources

    (engine / "uncommitted.txt").write_text("changed after prerequisite gate\n")
    with pytest.raises(acceptance.AcceptanceFailure, match="source composition changed"):
        runner.manifest(status="pass", bundle=None, verification=None)
    failed = runner.manifest(status="fail", bundle=None, verification=None)
    assert failed["sources"]["engine"]["treeState"] == "dirty"
    assert failed["sources"]["engine"]["revision"] is None
    assert failed["sources"]["engine"]["baseCommit"] == revisions["integer_inference_engine"]


def test_acceptance_run_report_requires_oracle_worker_entitlement(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence.initialize(evidence_root)
    args = acceptance.build_parser().parse_args([
        "--notary-home", str(tmp_path / "state"), "--engine-dir", str(ROOT / "engine"),
        "--python", sys.executable, "--artifact", str(tmp_path / "artifact"), "--cpu-threads", "3",
    ])
    args.prompt = acceptance.FIXED_PROMPT
    runner = acceptance.Runner(args, evidence_root)
    report = {
        "schema": acceptance.SCHEMA, "status": "pass", "exitCode": 0,
        "operation": "verify-receipt-bundles", "engine": {"policyApplied": False},
        "resources": {
            "threads": {name: 3 for name in acceptance.THREAD_ENV},
            "oracleQ1Workers": 3,
        },
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    assert runner.validate_run_report("verifier-report", path) == report
    report["resources"]["oracleQ1Workers"] = 2
    path.write_text(json.dumps(report))
    with pytest.raises(acceptance.AcceptanceFailure, match="CPU thread entitlement"):
        runner.validate_run_report("verifier-report", path)
    report["resources"]["oracleQ1Workers"] = 3
    report["resources"]["threads"][acceptance.THREAD_ENV[0]] = True
    path.write_text(json.dumps(report))
    with pytest.raises(acceptance.AcceptanceFailure, match="CPU thread entitlement"):
        runner.validate_run_report("verifier-report", path)


def test_evidence_sanitizer_redacts_secrets_paths_and_provider_endpoints(tmp_path):
    raw = tmp_path / "raw.log"
    public = tmp_path / "public.log"
    wif = "K" + "1" * 51
    raw.write_text(
        f"path=/srv/private/notary token=Bearer abcdefghijklmnop\n"
        f"wif={wif}\nssh=ssh7.vast.ai:22022\n"
        "url=https://cdn.invalid/file?X-Amz-Signature=secret&Expires=7\n",
        encoding="utf-8",
    )
    evidence.publish_sanitized(raw, public, private_paths=["/srv/private/notary"])
    value = public.read_text()
    assert "abcdefghijklmnop" not in value
    assert wif not in value
    assert "ssh7.vast.ai" not in value
    assert "X-Amz-Signature" not in value
    assert evidence.privacy_violations(value) == []


def test_evidence_schema_separates_private_raw_and_checksums_public_only(tmp_path):
    evidence.initialize(tmp_path)
    (tmp_path / "raw" / "private.log").write_text("private\n")
    (tmp_path / "public" / "run.log").write_text("public\n")
    evidence.write_json(tmp_path / "manifest.json", {"schema": evidence.SCHEMA}, public=True)
    evidence.write_checksums(tmp_path)
    sums = (tmp_path / "SHA256SUMS").read_text()
    assert "public/run.log" in sums
    assert "manifest.json" in sums
    assert "raw/private.log" not in sums
    assert stat.S_IMODE((tmp_path / "raw").stat().st_mode) == 0o700


def _strict_result(bundle_sha256: str, index: int = 0) -> dict:
    return {
        "ok": True,
        "inputBundleSha256": bundle_sha256,
        "bundleHash": f"{index + 1:064x}",
        "offline": {"ok": True},
        "reexec": {
            "ok": True, "signaturePinned": True, "sampled": False,
            "sigModelPresent": True, "sigCounterpartyPresent": True,
            "sigModelAuthenticated": True, "sigCounterpartyAuthenticated": True,
            "strategy": "resample-full",
            "raw": {
                "sigModelOk": True, "sigCounterpartyOk": True,
                "sigModelAuthenticated": True, "sigCounterpartyAuthenticated": True,
            },
        },
    }


def _verifier_key(path: Path):
    engine_src = ROOT / "engine" / "bonsai" / "src"
    sys.path.insert(0, str(engine_src))
    from trinote.receipts.signing_ec import ECKey
    key = ECKey.generate(secret_hex="01".zfill(64), label="test-verifier")
    key.save(path)
    return key


def _signed_strict_response(tmp_path: Path):
    bundle = tmp_path / "candidate.tar.gz"
    bundle.write_bytes(b"candidate")
    transport = tmp_path / "transport"
    assert handoff.main([
        "prepare", "--bundle", str(bundle), "--out-dir", str(transport), *_pending_args(),
    ]) == 0
    request_path = transport / "pending.json"
    request = json.loads(request_path.read_text())
    result_path = tmp_path / "strict-result.json"
    result_path.write_text(json.dumps(_strict_result(request["candidates"][0]["bundleSha256"])))
    key_path = tmp_path / "verifier.key.json"
    key = _verifier_key(key_path)
    response_path = tmp_path / "response.json"
    assert handoff.main([
        "respond", "--request", str(request_path), "--verification-json", str(result_path),
        "--signing-key", str(key_path), "--out", str(response_path),
    ]) == 0
    return request_path, response_path, key


def _resign_response(response: dict, key) -> None:
    response.pop("signature", None)
    response["signature"] = key.sign(handoff.canonical_bytes(response))


def test_pending_batch_requires_signed_pinned_verifier_response_before_verified(tmp_path):
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    first.write_bytes(b"candidate one")
    second.write_bytes(b"candidate two")
    transport = tmp_path / "transport"
    assert handoff.main([
        "prepare", "--bundle", str(first), "--bundle", str(second),
        "--batch-id", "batch-7", "--out-dir", str(transport), *_pending_args(),
    ]) == 0
    request_path = transport / "pending.json"
    request = json.loads(request_path.read_text())
    assert request["schema"] == handoff.PENDING_SCHEMA
    assert request["label"] == "PENDING / UNVERIFIED"
    assert request["batching"]["candidateCount"] == 2
    assert request["schema"] != handoff.FINAL_SCHEMA

    results = []
    for index, candidate in enumerate(request["candidates"]):
        path = tmp_path / f"result-{index}.json"
        path.write_text(json.dumps(_strict_result(candidate["bundleSha256"], index)))
        results.append(path)
    key_path = tmp_path / "verifier.key.json"
    key = _verifier_key(key_path)
    response_path = tmp_path / "response.json"
    assert handoff.main([
        "respond", "--request", str(request_path),
        "--verification-json", str(results[0]), "--verification-json", str(results[1]),
        "--signing-key", str(key_path), "--out", str(response_path),
    ]) == 0
    response = json.loads(response_path.read_text())
    assert handoff.VERIFIED_TRUE_CLAIMS == HANDOFF_REQUIRED_TRUE_CLAIMS
    for item in response["items"]:
        assert all(item[claim] is True for claim in HANDOFF_REQUIRED_TRUE_CLAIMS)
        assert item["sampled"] is False
    final_path = tmp_path / "verified.json"
    assert handoff.main([
        "finalize", "--request", str(request_path), "--response", str(response_path),
        "--verifier-pubkey", key.public_hex, "--out", str(final_path),
    ]) == 0
    final = json.loads(final_path.read_text())
    assert final["schema"] == handoff.FINAL_SCHEMA
    assert final["label"] == "VERIFIED"
    assert len(final["candidates"]) == 2


@pytest.mark.parametrize("claim", HANDOFF_REQUIRED_TRUE_CLAIMS)
@pytest.mark.parametrize("mutation", ["missing", "false"])
def test_handoff_finalization_requires_every_signed_true_claim(tmp_path, claim, mutation):
    request_path, response_path, key = _signed_strict_response(tmp_path)
    response = json.loads(response_path.read_text())
    assert response["verdict"] == "VERIFIED"
    if mutation == "missing":
        response["items"][0].pop(claim)
    else:
        response["items"][0][claim] = False
    _resign_response(response, key)
    response_path.write_text(json.dumps(response))

    final_path = tmp_path / "must-not-verify.json"
    assert handoff.main([
        "finalize", "--request", str(request_path), "--response", str(response_path),
        "--verifier-pubkey", key.public_hex, "--out", str(final_path),
    ]) == 2
    assert not final_path.exists()


@pytest.mark.parametrize("mutation", ["missing", "true"])
def test_handoff_finalization_requires_explicit_unsampled_claim(tmp_path, mutation):
    request_path, response_path, key = _signed_strict_response(tmp_path)
    response = json.loads(response_path.read_text())
    assert response["verdict"] == "VERIFIED"
    if mutation == "missing":
        response["items"][0].pop("sampled")
    else:
        response["items"][0]["sampled"] = True
    _resign_response(response, key)
    response_path.write_text(json.dumps(response))

    final_path = tmp_path / "must-not-verify.json"
    assert handoff.main([
        "finalize", "--request", str(request_path), "--response", str(response_path),
        "--verifier-pubkey", key.public_hex, "--out", str(final_path),
    ]) == 2
    assert not final_path.exists()


def test_handoff_request_rejects_noncanonical_uppercase_identity_pin(tmp_path):
    bundle = tmp_path / "candidate.tar.gz"
    bundle.write_bytes(b"candidate")
    transport = tmp_path / "transport"
    assert handoff.main([
        "prepare", "--bundle", str(bundle), "--out-dir", str(transport), *_pending_args(),
    ]) == 0
    request = json.loads((transport / "pending.json").read_text())
    request["verificationPolicy"]["modelPubKey"] = "02" + "AB" * 32
    request["requestId"] = hashlib.sha256(
        handoff.canonical_bytes(handoff._request_content(request))
    ).hexdigest()
    with pytest.raises(ValueError, match="canonical lowercase"):
        handoff._validate_request(request)


def test_handoff_rejects_tampered_response_and_failed_full_replay(tmp_path):
    bundle = tmp_path / "candidate.tar.gz"
    bundle.write_bytes(b"candidate")
    transport = tmp_path / "transport"
    assert handoff.main([
        "prepare", "--bundle", str(bundle), "--out-dir", str(transport), *_pending_args(),
    ]) == 0
    request = transport / "pending.json"
    key_path = tmp_path / "verifier.key.json"
    key = _verifier_key(key_path)

    request_value = json.loads(request.read_text())
    bundle_digest = request_value["candidates"][0]["bundleSha256"]
    failed = _strict_result(bundle_digest)
    failed["reexec"]["signaturePinned"] = False
    failed_path = tmp_path / "failed.json"
    failed_path.write_text(json.dumps(failed))
    rejected_path = tmp_path / "rejected.json"
    assert handoff.main([
        "respond", "--request", str(request), "--verification-json", str(failed_path),
        "--signing-key", str(key_path), "--out", str(rejected_path),
    ]) == 8
    assert handoff.main([
        "finalize", "--request", str(request), "--response", str(rejected_path),
        "--verifier-pubkey", key.public_hex, "--out", str(tmp_path / "must-not-exist.json"),
    ]) == 2
    assert not (tmp_path / "must-not-exist.json").exists()

    forged_pin = _strict_result(bundle_digest)
    forged_pin["reexec"]["raw"].pop("sigCounterpartyOk")
    forged_pin_path = tmp_path / "forged-pin.json"
    forged_pin_path.write_text(json.dumps(forged_pin))
    assert handoff.main([
        "respond", "--request", str(request), "--verification-json", str(forged_pin_path),
        "--signing-key", str(key_path), "--out", str(tmp_path / "forged-response.json"),
    ]) == 8

    passed_path = tmp_path / "passed.json"
    passed_path.write_text(json.dumps(_strict_result(bundle_digest)))
    response_path = tmp_path / "response.json"
    assert handoff.main([
        "respond", "--request", str(request), "--verification-json", str(passed_path),
        "--signing-key", str(key_path), "--out", str(response_path),
    ]) == 0
    response = json.loads(response_path.read_text())
    response["items"][0]["bundleSha256"] = "00" * 32
    response_path.write_text(json.dumps(response))
    assert handoff.main([
        "finalize", "--request", str(request), "--response", str(response_path),
        "--verifier-pubkey", key.public_hex, "--out", str(tmp_path / "tampered-final.json"),
    ]) == 2


def test_handoff_verifier_worker_has_hard_timeout(tmp_path):
    bundle = tmp_path / "candidate.tar.gz"
    bundle.write_bytes(b"candidate")
    request = tmp_path / "pending.json"
    request.write_text("{}")
    worker = tmp_path / "sleep-worker"
    worker.write_text("#!/bin/sh\nsleep 30\n")
    worker.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    candidate = {
        "index": 0, "file": bundle.name, "bundleSha256": evidence.sha256_file(bundle),
    }
    started = time.monotonic()
    code, results = handoff._verify_batch(
        worker, request, [candidate], tmp_path / "artifact",
        MODEL_PUBKEY, COUNTERPARTY_PUBKEY, out, 0, 1, None, 1,
    )
    assert code == handoff.WORKER_TIMEOUT_EXIT
    assert time.monotonic() - started < 5
    assert results[0]["ok"] is False and "timed out" in results[0]["error"]
    assert "timed out" in (out / "verification-shard-0000.stderr.log").read_text()


def _descriptor(path: Path, *, attempts: int = 1, timeout: int = 1,
                adapter_timeout: float | None = None) -> Path:
    value = {
        "schema": lifecycle.DESCRIPTOR_SCHEMA,
        "provider": "fake-provider",
        "image": "image@sha256:fixture",
        "label": "offline-test",
        "requirements": {
            "os": "ubuntu-22.04", "gpu": "Fixture GPU", "computeCapability": "8.6",
            "minCpuCores": 20, "minRamGb": 24, "minGpuRamGb": 12,
        },
        "limits": {
            "baseHourlyUsd": 0.2, "storageHourlyUsd": 0.02,
            "totalHourlyUsd": 0.22, "maxAttempts": attempts,
        },
        "storageGb": 40,
        "sshTimeoutSeconds": timeout,
        "pollSeconds": 0.01,
    }
    if adapter_timeout is not None:
        value["adapterTimeoutSeconds"] = adapter_timeout
    path.write_text(json.dumps(value))
    return path


def _fake_adapter(path: Path) -> Path:
    path.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
op = sys.argv[1]
payload = json.load(sys.stdin)
log = Path(os.environ["FAKE_ADAPTER_LOG"])
with log.open("a") as stream: stream.write(op + "\\n")
offer = {"offerId":"offer-1","machineId":"machine-1","gpu":"Fixture GPU",
         "computeCapability":"8.6","cpuCores":20,"ramGb":32,"gpuRamGb":24,
         "baseHourlyUsd":0.19,"storageHourlyUsdPerGb":0.0005}
if op == "offers": result = {"offers":[offer]}
elif op == "create": result = {"instanceId":"instance-1","machineId":"machine-1",
                                "idempotencyToken":payload["idempotencyToken"]}
elif op == "reconcile": result = {"status":"created","instanceId":"instance-1",
                                   "machineId":"machine-1",
                                   "idempotencyToken":payload["idempotencyToken"]}
elif op == "ready":
    state = json.load(open(os.environ["FAKE_LIFECYCLE_STATE"]))
    persisted = state["instances"][-1]["instanceId"] == "instance-1"
    result = {"ready": persisted and os.environ.get("FAKE_READY") == "1",
              "ssh":{"host":"fixture.invalid","port":22}}
elif op == "destroy": result = {"destroyed": True}
elif op == "active": result = {"instances": []}
else: raise SystemExit(2)
print(json.dumps(result))
""")
    path.chmod(0o755)
    return path


def _ambiguous_create_adapter(path: Path) -> Path:
    path.write_text("""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
op = sys.argv[1]
payload = json.load(sys.stdin)
log = Path(os.environ["FAKE_ADAPTER_LOG"])
with log.open("a") as stream: stream.write(op + "\\n")
remote = Path(os.environ["FAKE_REMOTE_STATE"])
offer = {"offerId":"offer-1","machineId":"machine-1","gpu":"Fixture GPU",
         "computeCapability":"8.6","cpuCores":20,"ramGb":32,"gpuRamGb":24,
         "baseHourlyUsd":0.19,"storageHourlyUsdPerGb":0.0005}
if op == "offers":
    result = {"offers":[offer]}
elif op == "create":
    token = payload["idempotencyToken"]
    local = json.load(open(os.environ["FAKE_LIFECYCLE_STATE"]))
    pending = local["instances"][-1]
    if pending["idempotencyToken"] != token or pending["createStatus"] != "pending":
        raise SystemExit(9)
    remote.write_text(json.dumps({"idempotencyToken":token,"instanceId":"instance-ambiguous",
                                  "machineId":"machine-1"}))
    mode = os.environ["FAKE_CREATE_MODE"]
    if mode == "timeout": time.sleep(30)
    if mode == "malformed":
        print("{not-json")
        raise SystemExit(0)
    result = {"idempotencyToken":token,"instanceId":"instance-ambiguous","machineId":"machine-1"}
elif op == "reconcile":
    accepted = json.loads(remote.read_text()) if remote.exists() else None
    token = payload["idempotencyToken"]
    if accepted and accepted["idempotencyToken"] == token:
        result = dict(accepted, status="created")
    else:
        result = {"status":"absent","idempotencyToken":token}
elif op == "ready":
    result = {"ready": True, "ssh":{"host":"fixture.invalid","port":22}}
elif op == "destroy":
    remote.unlink(missing_ok=True)
    result = {"destroyed": True}
elif op == "active":
    result = {"instances": [] if not remote.exists() else [json.loads(remote.read_text())]}
else:
    raise SystemExit(2)
print(json.dumps(result))
""")
    path.chmod(0o755)
    return path


def _process_tree_adapter(path: Path) -> Path:
    path.write_text("""#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path

op = sys.argv[1]
payload = json.load(sys.stdin)
pids_path = Path(os.environ["FAKE_ADAPTER_PIDS"])
observation_path = Path(os.environ["FAKE_RECONCILE_OBSERVATION"])
offer = {"offerId":"offer-1","machineId":"machine-1","gpu":"Fixture GPU",
         "computeCapability":"8.6","cpuCores":20,"ramGb":32,"gpuRamGb":24,
         "baseHourlyUsd":0.19,"storageHourlyUsdPerGb":0.0005}

def live(pid):
    try:
        remainder = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return remainder.split()[0] != "Z"

if op == "normal":
    result = {"ok": True, "echo": payload}
elif op == "offers":
    result = {"offers": [offer]}
elif op in ("stall", "create"):
    ready_path = Path(str(pids_path) + ".child-ready")
    child_pid = os.fork()
    if child_pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ready_path.write_text(str(os.getpid()))
        while True:
            signal.pause()
    deadline = time.monotonic() + 5
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready_path.exists():
        raise SystemExit(7)
    pids_path.write_text(json.dumps({
        "leader": os.getpid(), "descendant": child_pid, "pgid": os.getpgrp(),
    }))
    time.sleep(60)
    raise SystemExit(8)
elif op == "reconcile":
    old = json.loads(pids_path.read_text())
    observation_path.write_text(json.dumps({
        "leaderLive": live(old["leader"]),
        "descendantLive": live(old["descendant"]),
    }))
    result = {"status": "absent", "idempotencyToken": payload["idempotencyToken"]}
elif op == "active":
    result = {"instances": []}
else:
    raise SystemExit(2)
print(json.dumps(result))
""")
    path.chmod(0o755)
    return path


def _pid_is_live(pid: int) -> bool:
    try:
        remainder = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return remainder.split()[0] != "Z"


def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path.name}"


def _process_tree_status(pids_path: Path) -> tuple[dict[str, int], dict[str, bool]]:
    pids = json.loads(pids_path.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        live = {name: _pid_is_live(pids[name]) for name in ("leader", "descendant")}
        if not any(live.values()):
            return pids, live
        time.sleep(0.01)
    return pids, {name: _pid_is_live(pids[name]) for name in ("leader", "descendant")}


def _kill_test_process_tree(pids: dict[str, int]) -> None:
    try:
        os.killpg(pids["pgid"], signal.SIGKILL)
    except ProcessLookupError:
        pass


def _ssh_key(path: Path) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "offline-test", "-f", str(path)],
        check=True,
    )
    return path


def test_provider_adapter_normal_call_regression(tmp_path, monkeypatch):
    adapter_path = _process_tree_adapter(tmp_path / "adapter.py")
    monkeypatch.setenv("FAKE_ADAPTER_PIDS", str(tmp_path / "pids.json"))
    monkeypatch.setenv("FAKE_RECONCILE_OBSERVATION", str(tmp_path / "observation.json"))
    result = lifecycle.Adapter(adapter_path, timeout_seconds=1).call("normal", {"fixture": 7})
    assert result == {"ok": True, "echo": {"fixture": 7}}


def test_provider_adapter_timeout_kills_and_reaps_process_group(tmp_path, monkeypatch):
    adapter_path = _process_tree_adapter(tmp_path / "adapter.py")
    pids_path = tmp_path / "pids.json"
    monkeypatch.setenv("FAKE_ADAPTER_PIDS", str(pids_path))
    monkeypatch.setenv("FAKE_RECONCILE_OBSERVATION", str(tmp_path / "observation.json"))
    started = time.monotonic()
    with pytest.raises(lifecycle.AdapterTimeout, match="timed out"):
        lifecycle.Adapter(adapter_path, timeout_seconds=0.2).call("stall")
    assert time.monotonic() - started < 3
    pids, live = _process_tree_status(pids_path)
    try:
        assert live == {"leader": False, "descendant": False}
        assert not Path(f"/proc/{pids['leader']}").exists(), "adapter leader was not reaped"
    finally:
        _kill_test_process_tree(pids)


def test_provider_adapter_arbitrary_base_exception_reaps_process_group(tmp_path, monkeypatch):
    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("requires SIGUSR1")

    adapter_path = _process_tree_adapter(tmp_path / "adapter.py")
    pids_path = tmp_path / "pids.json"
    monkeypatch.setenv("FAKE_ADAPTER_PIDS", str(pids_path))
    monkeypatch.setenv("FAKE_RECONCILE_OBSERVATION", str(tmp_path / "observation.json"))

    class ForcedAbort(BaseException):
        pass

    def abort(_signum, _frame):
        raise ForcedAbort("fixture abort")

    def send_abort():
        _wait_for_path(pids_path)
        os.kill(os.getpid(), signal.SIGUSR1)

    old_handler = signal.signal(signal.SIGUSR1, abort)
    sender = threading.Thread(target=send_abort, daemon=True)
    sender.start()
    try:
        with pytest.raises(ForcedAbort, match="fixture abort"):
            lifecycle.Adapter(adapter_path, timeout_seconds=5).call("stall")
    finally:
        sender.join(timeout=5)
        signal.signal(signal.SIGUSR1, old_handler)
    pids, live = _process_tree_status(pids_path)
    try:
        assert live == {"leader": False, "descendant": False}
        assert not Path(f"/proc/{pids['leader']}").exists(), "adapter leader was not reaped"
    finally:
        _kill_test_process_tree(pids)


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_provider_interrupt_reaps_adapter_group_before_reconciliation(
        tmp_path, interrupt_signal):
    descriptor = _descriptor(tmp_path / "descriptor.json", adapter_timeout=10)
    adapter = _process_tree_adapter(tmp_path / "adapter.py")
    state = tmp_path / "state.json"
    pids_path = tmp_path / "pids.json"
    observation = tmp_path / "observation.json"
    key = _ssh_key(tmp_path / "id")
    env = dict(os.environ)
    env.update({
        "FAKE_ADAPTER_PIDS": str(pids_path),
        "FAKE_RECONCILE_OBSERVATION": str(observation),
    })
    command = [
        sys.executable, str(ROOT / "operations" / "provider_lifecycle.py"),
        "up", "--descriptor", str(descriptor), "--adapter", str(adapter),
        "--state", str(state), "--ssh-key", str(key), "--authorize-billing",
        "--confirm-max-hourly-usd", "0.22",
    ]
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env)
    pids: dict[str, int] = {}
    try:
        _wait_for_path(pids_path)
        pids = json.loads(pids_path.read_text())
        os.kill(process.pid, interrupt_signal)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, (stdout, stderr)
        _wait_for_path(observation)
        assert json.loads(observation.read_text()) == {
            "leaderLive": False, "descendantLive": False,
        }
        persisted = json.loads(state.read_text())
        assert persisted["instances"][0]["createStatus"] == "absent"
        assert persisted["instances"][0]["destroyVerified"] is True
        assert persisted["audit"]["zeroActive"] is True
        _, live = _process_tree_status(pids_path)
        assert live == {"leader": False, "descendant": False}
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if pids:
            _kill_test_process_tree(pids)


def test_provider_plan_is_read_only_and_includes_storage_price(tmp_path, monkeypatch, capsys):
    descriptor = _descriptor(tmp_path / "descriptor.json")
    adapter = _fake_adapter(tmp_path / "adapter.py")
    log = tmp_path / "adapter.log"
    state = tmp_path / "state.json"
    monkeypatch.setenv("FAKE_ADAPTER_LOG", str(log))
    monkeypatch.setenv("FAKE_LIFECYCLE_STATE", str(state))
    assert lifecycle.main([
        "plan", "--descriptor", str(descriptor), "--adapter", str(adapter),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["billingStarted"] is False
    assert output["selection"]["resolvedBaseHourlyUsd"] == pytest.approx(0.19)
    assert output["selection"]["resolvedStorageHourlyUsd"] == pytest.approx(0.02)
    assert output["selection"]["resolvedTotalHourlyUsd"] == pytest.approx(0.21)
    assert log.read_text().splitlines() == ["offers"]
    assert not state.exists()


def test_provider_up_requires_authorization_and_persists_before_readiness(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path / "descriptor.json")
    adapter = _fake_adapter(tmp_path / "adapter.py")
    state = tmp_path / "state.json"
    log = tmp_path / "adapter.log"
    key = _ssh_key(tmp_path / "id")
    monkeypatch.setenv("FAKE_ADAPTER_LOG", str(log))
    monkeypatch.setenv("FAKE_LIFECYCLE_STATE", str(state))
    monkeypatch.setenv("FAKE_READY", "1")
    assert lifecycle.main([
        "up", "--descriptor", str(descriptor), "--adapter", str(adapter),
        "--state", str(state), "--ssh-key", str(key), "--confirm-max-hourly-usd", "0.22",
    ]) == 2
    assert not log.exists(), "authorization failure must occur before adapter/network access"
    assert lifecycle.main([
        "up", "--descriptor", str(descriptor), "--adapter", str(adapter),
        "--state", str(state), "--ssh-key", str(key), "--authorize-billing",
        "--confirm-max-hourly-usd", "0.22",
    ]) == 0
    persisted = json.loads(state.read_text())
    assert persisted["instances"][0]["ready"] is True
    assert persisted["instances"][0]["baseHourlyUsd"] == pytest.approx(0.19)
    assert persisted["instances"][0]["storageHourlyUsd"] == pytest.approx(0.02)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert log.read_text().splitlines() == ["offers", "create", "ready"]

    assert lifecycle.main([
        "down", "--adapter", str(adapter), "--state", str(state), "--authorize-destroy",
    ]) == 0
    cleaned = json.loads(state.read_text())
    assert cleaned["instances"][0]["destroyVerified"] is True
    assert cleaned["audit"]["zeroActive"] is True
    assert log.read_text().splitlines()[-2:] == ["destroy", "active"]


@pytest.mark.parametrize("mode", ["timeout", "malformed"])
def test_provider_create_ambiguity_reconciles_persisted_idempotency_token(
        tmp_path, monkeypatch, mode):
    descriptor = _descriptor(
        tmp_path / "descriptor.json", adapter_timeout=0.5,
    )
    adapter = _ambiguous_create_adapter(tmp_path / "adapter.py")
    state = tmp_path / "state.json"
    remote = tmp_path / "remote.json"
    log = tmp_path / "adapter.log"
    key = _ssh_key(tmp_path / "id")
    monkeypatch.setenv("FAKE_ADAPTER_LOG", str(log))
    monkeypatch.setenv("FAKE_LIFECYCLE_STATE", str(state))
    monkeypatch.setenv("FAKE_REMOTE_STATE", str(remote))
    monkeypatch.setenv("FAKE_CREATE_MODE", mode)
    started = time.monotonic()
    assert lifecycle.main([
        "up", "--descriptor", str(descriptor), "--adapter", str(adapter),
        "--state", str(state), "--ssh-key", str(key), "--authorize-billing",
        "--confirm-max-hourly-usd", "0.22",
    ]) == 0
    assert time.monotonic() - started < 5
    persisted = json.loads(state.read_text())
    record = persisted["instances"][0]
    assert record["createStatus"] == "created"
    assert record["instanceId"] == "instance-ambiguous"
    assert len(record["idempotencyToken"]) == 64
    assert record["createResponseError"] in {"AdapterTimeout", "AdapterError"}
    assert log.read_text().splitlines()[:4] == ["offers", "create", "reconcile", "ready"]
    assert log.read_text().splitlines().count("create") == 1

    assert lifecycle.main([
        "down", "--adapter", str(adapter), "--state", str(state), "--authorize-destroy",
        "--adapter-timeout-seconds", "0.5",
    ]) == 0
    assert not remote.exists()


def test_provider_reconcile_command_recovers_interrupted_pending_record(tmp_path, monkeypatch):
    descriptor_path = _descriptor(tmp_path / "descriptor.json", adapter_timeout=0.5)
    descriptor = lifecycle.validate_descriptor(json.loads(descriptor_path.read_text()))
    adapter = _ambiguous_create_adapter(tmp_path / "adapter.py")
    state_path = tmp_path / "state.json"
    remote = tmp_path / "remote.json"
    log = tmp_path / "adapter.log"
    offer = {
        "offerId": "offer-1", "machineId": "machine-1", "gpu": "Fixture GPU", "cpuCores": 20,
        "resolvedBaseHourlyUsd": 0.19, "resolvedStorageHourlyUsd": 0.02,
        "resolvedTotalHourlyUsd": 0.21,
    }
    state = lifecycle.initial_state(descriptor)
    record = lifecycle._new_create_record(1, offer)
    state["instances"].append(record)
    lifecycle.save_state(state_path, state)
    remote.write_text(json.dumps({
        "idempotencyToken": record["idempotencyToken"],
        "instanceId": "instance-ambiguous", "machineId": "machine-1",
    }))
    monkeypatch.setenv("FAKE_ADAPTER_LOG", str(log))
    monkeypatch.setenv("FAKE_LIFECYCLE_STATE", str(state_path))
    monkeypatch.setenv("FAKE_REMOTE_STATE", str(remote))
    monkeypatch.setenv("FAKE_CREATE_MODE", "malformed")

    assert lifecycle.main([
        "reconcile", "--adapter", str(adapter), "--state", str(state_path),
        "--adapter-timeout-seconds", "0.5",
    ]) == 0
    recovered = json.loads(state_path.read_text())["instances"][0]
    assert recovered["createStatus"] == "created"
    assert recovered["instanceId"] == "instance-ambiguous"
    assert log.read_text().splitlines() == ["reconcile"]


def test_provider_readiness_failure_is_destroyed_and_audited(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path / "descriptor.json", attempts=1, timeout=1)
    adapter = _fake_adapter(tmp_path / "adapter.py")
    state = tmp_path / "state.json"
    log = tmp_path / "adapter.log"
    key = _ssh_key(tmp_path / "id")
    monkeypatch.setenv("FAKE_ADAPTER_LOG", str(log))
    monkeypatch.setenv("FAKE_LIFECYCLE_STATE", str(state))
    monkeypatch.setenv("FAKE_READY", "0")
    assert lifecycle.main([
        "up", "--descriptor", str(descriptor), "--adapter", str(adapter),
        "--state", str(state), "--ssh-key", str(key), "--authorize-billing",
        "--confirm-max-hourly-usd", "0.22",
    ]) == 2
    persisted = json.loads(state.read_text())
    assert persisted["excludedMachineIds"] == ["machine-1"]
    assert persisted["instances"][0]["destroyVerified"] is True
    assert persisted["audit"]["zeroActive"] is True
    operations = log.read_text().splitlines()
    assert operations.index("create") < operations.index("ready") < operations.index("destroy")
    assert operations[-1] == "active"
