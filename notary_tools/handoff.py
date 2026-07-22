"""File-based producer/verifier handoff with explicit trust-state schemas.

The producer emits ``receipt-pending/v1`` and the literal label
``PENDING / UNVERIFIED``.  A separate verifier signs a strict full-replay
result.  Only ``finalize`` can emit ``receipt-verified-handoff/v1`` and its
``VERIFIED`` label, after checking a pinned verifier public key.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .evidence import atomic_write, canonical_bytes, sha256_file, write_json


ROOT = Path(__file__).resolve().parents[1]
PENDING_SCHEMA = "receipt-pending/v1"
RESPONSE_SCHEMA = "verifier-response/v1"
FINAL_SCHEMA = "receipt-verified-handoff/v1"
WORKER_TIMEOUT_EXIT = 124

# These claims cross the verifier/producer trust boundary in each signed item.
# Finalization checks them individually instead of treating ``signaturePinned``
# as a summary assertion for facts that the response did not otherwise retain.
VERIFIED_TRUE_CLAIMS = (
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _engine_src() -> Path:
    engine = Path(os.environ.get("BONSAI_ENGINE_DIR", ROOT / "engine"))
    source = engine / "bonsai" / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return source


def _crypto():
    _engine_src()
    try:
        from trinote.receipts.signing_ec import ECKey, verify_ec
    except ImportError as exc:
        raise RuntimeError(
            "engine receipt dependencies are unavailable; run with the engine virtual environment"
        ) from exc
    return ECKey, verify_ec


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _require_hex(label: str, value: Any, length: int = 64) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise ValueError(f"{label} must be {length} hexadecimal characters")
    return value.lower()


def _require_pubkey(label: str, value: Any, *, canonical: bool = False) -> str:
    key = _require_hex(label, value, 66)
    if not key.startswith(("02", "03")):
        raise ValueError(f"{label} must be a compressed secp256k1 public key")
    if canonical and value != key:
        raise ValueError(f"{label} must use canonical lowercase hexadecimal")
    return key


def _request_content(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key != "requestId"}


def _validate_request(request: dict[str, Any]) -> None:
    if request.get("schema") != PENDING_SCHEMA:
        raise ValueError(f"request schema must be {PENDING_SCHEMA}")
    if request.get("label") != "PENDING / UNVERIFIED":
        raise ValueError("pending request must carry the literal PENDING / UNVERIFIED label")
    expected = hashlib.sha256(canonical_bytes(_request_content(request))).hexdigest()
    if request.get("requestId") != expected:
        raise ValueError("pending requestId does not match its canonical content")
    candidates = request.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("pending request needs at least one candidate")
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or candidate.get("index") != index:
            raise ValueError("candidate indices must be contiguous and ordered")
        digest = _require_hex("candidate.bundleSha256", candidate.get("bundleSha256"))
        if digest in seen:
            raise ValueError("duplicate candidate bundle digest")
        seen.add(digest)
        if not isinstance(candidate.get("file"), str) or Path(candidate["file"]).is_absolute():
            raise ValueError("candidate file must be a relative transport path")
    policy = request.get("verificationPolicy") or {}
    model_pubkey = _require_pubkey(
        "verificationPolicy.modelPubKey", policy.get("modelPubKey"), canonical=True
    )
    counterparty_pubkey = _require_pubkey(
        "verificationPolicy.counterpartyPubKey",
        policy.get("counterpartyPubKey"),
        canonical=True,
    )
    if model_pubkey == counterparty_pubkey:
        raise ValueError("model and counterparty pins must be distinct")


def prepare(args: argparse.Namespace) -> int:
    model_pubkey = _require_pubkey("model-pubkey", args.model_pubkey)
    counterparty_pubkey = _require_pubkey("counterparty-pubkey", args.counterparty_pubkey)
    if model_pubkey == counterparty_pubkey:
        raise ValueError("model and counterparty pins must be distinct")
    output_dir = args.out_dir.resolve()
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError(f"output path is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise ValueError(f"output directory is not empty: {output_dir}")
    bundle_dir = output_dir / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    os.chmod(bundle_dir, 0o700)
    candidates: list[dict[str, Any]] = []
    for index, source in enumerate(args.bundle):
        source = source.resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError(f"candidate bundle is missing or empty: {source}")
        digest = sha256_file(source)
        suffix = "".join(source.suffixes) or ".bundle"
        target = bundle_dir / f"{digest}{suffix}"
        if target.exists() and sha256_file(target) != digest:
            raise ValueError(f"refusing to replace disagreeing transport bundle: {target}")
        if not target.exists():
            temporary = target.with_name(f".{target.name}.part")
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != digest:
                temporary.unlink(missing_ok=True)
                raise ValueError("candidate changed while it was copied")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        candidates.append({
            "index": index,
            "file": target.relative_to(output_dir).as_posix(),
            "bundleSha256": digest,
            "bytes": target.stat().st_size,
        })
    base: dict[str, Any] = {
        "schema": PENDING_SCHEMA,
        "label": "PENDING / UNVERIFIED",
        "createdAt": utc_now(),
        "batchId": args.batch_id,
        "candidates": candidates,
        "verificationPolicy": {
            "offlineRequired": True,
            "fullReexecutionRequired": True,
            "signerPinningRequired": True,
            "sampledAuditAccepted": False,
            "modelPubKey": model_pubkey,
            "counterpartyPubKey": counterparty_pubkey,
        },
        "batching": {
            "candidateCount": len(candidates),
            "independentWorkersAllowed": True,
            "orderedResponseRequired": True,
        },
    }
    request = dict(base, requestId=hashlib.sha256(canonical_bytes(base)).hexdigest())
    write_json(output_dir / "pending.json", request)
    print(json.dumps({
        "schema": PENDING_SCHEMA, "label": request["label"],
        "requestId": request["requestId"], "candidateCount": len(candidates),
        "path": str(output_dir / "pending.json"),
    }, sort_keys=True))
    return 0


def _strict_verification(result: dict[str, Any], expected_bundle_sha256: str) -> tuple[bool, str]:
    offline_value = result.get("offline")
    reexec_value = result.get("reexec")
    offline = offline_value if isinstance(offline_value, dict) else {}
    reexec = reexec_value if isinstance(reexec_value, dict) else {}
    raw_value = reexec.get("raw")
    raw = raw_value if isinstance(raw_value, dict) else {}
    pinned_signatures = (
        reexec.get("signaturePinned") is True
        and reexec.get("sigModelPresent") is True
        and reexec.get("sigCounterpartyPresent") is True
        and reexec.get("sigModelAuthenticated") is True
        and reexec.get("sigCounterpartyAuthenticated") is True
        and raw.get("sigModelOk") is True
        and raw.get("sigCounterpartyOk") is True
        and raw.get("sigModelAuthenticated") is True
        and raw.get("sigCounterpartyAuthenticated") is True
    )
    ok = (
        result.get("ok") is True
        and offline.get("ok") is True
        and reexec.get("ok") is True
        and pinned_signatures
        and reexec.get("sampled") is False
        and result.get("inputBundleSha256") == expected_bundle_sha256
    )
    if not ok:
        return False, "full offline, pinned-signature, unsampled re-execution did not pass"
    return True, "full pinned replay passed"


def _build_response(
    request: dict[str, Any],
    results: Sequence[dict[str, Any]],
    signing_key: Path,
) -> dict[str, Any]:
    _validate_request(request)
    if len(results) != len(request["candidates"]):
        raise ValueError("verification result count does not match candidate count")
    items: list[dict[str, Any]] = []
    all_ok = True
    for candidate, result in zip(request["candidates"], results, strict=True):
        ok, detail = _strict_verification(result, candidate["bundleSha256"])
        all_ok &= ok
        offline_value = result.get("offline")
        reexec_value = result.get("reexec")
        offline = offline_value if isinstance(offline_value, dict) else {}
        reexec = reexec_value if isinstance(reexec_value, dict) else {}
        raw_value = reexec.get("raw")
        raw = raw_value if isinstance(raw_value, dict) else {}
        bundle_hash = result.get("bundleHash")
        if ok:
            bundle_hash = _require_hex("verification.bundleHash", bundle_hash)
        items.append({
            "index": candidate["index"],
            "bundleSha256": candidate["bundleSha256"],
            "ok": ok,
            "detail": detail,
            "bundleHash": bundle_hash,
            "reexecStrategy": reexec.get("strategy"),
            "offlineOk": offline.get("ok"),
            "reexecOk": reexec.get("ok"),
            "sampled": reexec.get("sampled"),
            "signaturePinned": reexec.get("signaturePinned"),
            "sigModelPresent": reexec.get("sigModelPresent"),
            "sigCounterpartyPresent": reexec.get("sigCounterpartyPresent"),
            "sigModelAuthenticated": reexec.get("sigModelAuthenticated"),
            "sigCounterpartyAuthenticated": reexec.get("sigCounterpartyAuthenticated"),
            "rawSigModelOk": raw.get("sigModelOk"),
            "rawSigCounterpartyOk": raw.get("sigCounterpartyOk"),
            "rawSigModelAuthenticated": raw.get("sigModelAuthenticated"),
            "rawSigCounterpartyAuthenticated": raw.get("sigCounterpartyAuthenticated"),
            "verifierPolicySha256": result.get("inputVerifierPolicySha256"),
        })
    ECKey, _verify_ec = _crypto()
    key_data = _load(signing_key)
    key = ECKey.from_json(key_data)
    policy = request["verificationPolicy"]
    if key.public_hex.lower() in {
        policy["modelPubKey"].lower(), policy["counterpartyPubKey"].lower()
    }:
        raise ValueError("verifier identity must be distinct from both receipt signing identities")
    unsigned: dict[str, Any] = {
        "schema": RESPONSE_SCHEMA,
        "verdict": "VERIFIED" if all_ok else "REJECTED",
        "requestId": request["requestId"],
        "batchId": request.get("batchId"),
        "verifiedAt": utc_now(),
        "verifierPubKey": key.public_hex,
        "items": items,
    }
    return dict(unsigned, signature=key.sign(canonical_bytes(unsigned)))


def respond(args: argparse.Namespace) -> int:
    request = _load(args.request)
    results = [_load(path) for path in args.verification_json]
    response = _build_response(request, results, args.signing_key)
    write_json(args.out, response)
    print(json.dumps({
        "schema": RESPONSE_SCHEMA, "verdict": response["verdict"],
        "requestId": response["requestId"], "verifierPubKey": response["verifierPubKey"],
        "path": str(args.out),
    }, sort_keys=True))
    return 0 if response["verdict"] == "VERIFIED" else 8


def _verify_batch(
    python: Path,
    request_path: Path,
    candidates: Sequence[dict[str, Any]],
    artifact: Path,
    model_pubkey: str,
    counterparty_pubkey: str,
    output_dir: Path,
    shard: int,
    cpu_threads: int,
    verifier_policy: Path | None,
    timeout_seconds: int,
) -> tuple[int, dict[int, dict[str, Any]]]:
    expected_parent = request_path.parent.resolve()
    bundles: list[Path] = []
    policy_sha256 = sha256_file(verifier_policy) if verifier_policy is not None else None
    for candidate in candidates:
        bundle = (request_path.parent / candidate["file"]).resolve()
        if not bundle.is_relative_to(expected_parent):
            raise ValueError("candidate transport path escapes its request directory")
        if sha256_file(bundle) != candidate["bundleSha256"]:
            raise ValueError(f"candidate {candidate['index']} bundle digest mismatch")
        bundles.append(bundle)
    command = [str(python), "-m", "trinote.cli.receipt_bundle_cli", "verify"]
    command.extend(str(bundle) for bundle in bundles)
    command.extend([
        "--reexec", "--artifact", str(artifact), "--model-pubkey", model_pubkey,
        "--counterparty-pubkey", counterparty_pubkey, "--cpu-threads", str(cpu_threads),
        "--run-report", str(output_dir / f"run-report-{shard:04d}.json"), "--json",
    ])
    if verifier_policy is not None:
        command.extend(["--strategy-policy", str(verifier_policy)])
    env = os.environ.copy()
    source = _engine_src()
    env["PYTHONPATH"] = str(source) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    process = subprocess.Popen(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr_text = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        try:
            stdout, stderr_text = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr_text = "", ""
        stderr_text += f"\nverifier worker timed out after {timeout_seconds} seconds\n"
    returncode = WORKER_TIMEOUT_EXIT if timed_out else (process.returncode or 0)
    raw = output_dir / f"verification-shard-{shard:04d}.json"
    stderr = output_dir / f"verification-shard-{shard:04d}.stderr.log"
    atomic_write(raw, stdout.encode("utf-8"))
    atomic_write(stderr, stderr_text.encode("utf-8"))
    if returncode:
        return returncode, {
            candidate["index"]: {
                "ok": False,
                "error": (
                    f"verifier timed out after {timeout_seconds} seconds"
                    if timed_out else f"verifier exited {returncode}"
                ),
                "inputBundleSha256": candidate["bundleSha256"],
                "inputVerifierPolicySha256": policy_sha256,
            }
            for candidate in candidates
        }
    try:
        decoded = json.loads(stdout)
        results = decoded if isinstance(decoded, list) else [decoded]
        if len(results) != len(candidates) or not all(isinstance(result, dict) for result in results):
            raise ValueError("verifier result count does not match its batch shard")
        indexed: dict[int, dict[str, Any]] = {}
        for candidate, result in zip(candidates, results, strict=True):
            result["inputBundleSha256"] = candidate["bundleSha256"]
            result["inputVerifierPolicySha256"] = policy_sha256
            indexed[candidate["index"]] = result
        return 0, indexed
    except json.JSONDecodeError as exc:
        detail = f"invalid verifier JSON: {exc}"
    except ValueError as exc:
        detail = str(exc)
    return 9, {
        candidate["index"]: {
            "ok": False, "error": detail,
            "inputBundleSha256": candidate["bundleSha256"],
            "inputVerifierPolicySha256": policy_sha256,
        }
        for candidate in candidates
    }


def verify_request(args: argparse.Namespace) -> int:
    request_path = args.request.resolve()
    request = _load(request_path)
    _validate_request(request)
    policy = request["verificationPolicy"]
    model_pubkey = policy["modelPubKey"]
    counterparty_pubkey = policy["counterpartyPubKey"]
    verifier_policy = args.verifier_policy.resolve() if args.verifier_policy else None
    if verifier_policy is not None and not verifier_policy.is_file():
        raise ValueError(f"verifier policy is missing: {verifier_policy}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results_by_index: dict[int, dict[str, Any]] = {}
    first_failure = 0
    shard_count = min(args.jobs, len(request["candidates"]))
    shards = [request["candidates"][index::shard_count] for index in range(shard_count)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                _verify_batch, args.python, request_path, shard, args.artifact,
                model_pubkey, counterparty_pubkey, args.out_dir, shard_index,
                args.cpu_threads, verifier_policy, args.worker_timeout_seconds,
            ): shard
            for shard_index, shard in enumerate(shards)
        }
        for future in concurrent.futures.as_completed(futures):
            shard = futures[future]
            try:
                code, results = future.result()
            except Exception as exc:
                code = 9
                results = {
                    candidate["index"]: {
                        "ok": False, "error": str(exc),
                        "inputBundleSha256": candidate["bundleSha256"],
                        "inputVerifierPolicySha256": (
                            sha256_file(verifier_policy) if verifier_policy is not None else None
                        ),
                    }
                    for candidate in shard
                }
            if code and not first_failure:
                first_failure = code
            results_by_index.update(results)
    ordered = [results_by_index[index] for index in range(len(request["candidates"]))]
    response = _build_response(request, ordered, args.signing_key)
    response_path = args.out_dir / "verifier-response.json"
    write_json(response_path, response)
    print(json.dumps({
        "schema": RESPONSE_SCHEMA, "verdict": response["verdict"],
        "requestId": response["requestId"], "path": str(response_path),
    }, sort_keys=True))
    if first_failure:
        return first_failure
    return 0 if response["verdict"] == "VERIFIED" else 8


def finalize(args: argparse.Namespace) -> int:
    request = _load(args.request)
    response = _load(args.response)
    _validate_request(request)
    if response.get("schema") != RESPONSE_SCHEMA:
        raise ValueError(f"response schema must be {RESPONSE_SCHEMA}")
    pinned = _require_pubkey("verifier-pubkey", args.verifier_pubkey)
    if response.get("verifierPubKey", "").lower() != pinned:
        raise ValueError("verifier response is not from the pinned verifier identity")
    signature = response.get("signature")
    unsigned = {key: value for key, value in response.items() if key != "signature"}
    _ECKey, verify_ec = _crypto()
    if not isinstance(signature, str) or not verify_ec(
        canonical_bytes(unsigned), signature, expected_pubkey_hex=pinned
    ):
        raise ValueError("verifier response signature is invalid")
    if response.get("requestId") != request["requestId"]:
        raise ValueError("verifier response addresses a different pending request")
    if response.get("verdict") != "VERIFIED":
        raise ValueError("a rejected response cannot be finalized as VERIFIED")
    items = response.get("items")
    if not isinstance(items, list) or len(items) != len(request["candidates"]):
        raise ValueError("verifier response item count does not match the pending request")
    for candidate, item in zip(request["candidates"], items, strict=True):
        if (
            not isinstance(item, dict)
            or item.get("index") != candidate["index"]
            or item.get("bundleSha256") != candidate["bundleSha256"]
            or item.get("ok") is not True
        ):
            raise ValueError("verifier response does not authorize every candidate")
        for claim in VERIFIED_TRUE_CLAIMS:
            if item.get(claim) is not True:
                raise ValueError(f"verifier response claim {claim} must be explicitly true")
        if item.get("sampled") is not False:
            raise ValueError("verifier response claim sampled must be explicitly false")
    final = {
        "schema": FINAL_SCHEMA,
        "label": "VERIFIED",
        "requestId": request["requestId"],
        "batchId": request.get("batchId"),
        "finalizedAt": utc_now(),
        "verifierPubKey": pinned,
        "verifierResponseSha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
        "candidates": [
            {"index": item["index"], "bundleSha256": item["bundleSha256"],
             "bundleHash": item.get("bundleHash")}
            for item in items
        ],
    }
    write_json(args.out, final, public=True)
    print(json.dumps(final, sort_keys=True))
    return 0


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    engine = Path(os.environ.get("BONSAI_ENGINE_DIR", ROOT / "engine"))
    parser = argparse.ArgumentParser(description="Pending receipt handoff and signed verifier response plumbing")
    commands = parser.add_subparsers(dest="command", required=True)

    pending = commands.add_parser("prepare", help="create a PENDING / UNVERIFIED transport request")
    pending.add_argument("--bundle", type=Path, action="append", required=True)
    pending.add_argument("--out-dir", type=Path, required=True)
    pending.add_argument("--batch-id")
    pending.add_argument("--model-pubkey", required=True,
                         help="expected compressed model signer identity")
    pending.add_argument("--counterparty-pubkey", required=True,
                         help="expected compressed counterparty signer identity")
    pending.set_defaults(handler=prepare)

    response = commands.add_parser("respond", help="sign already-produced strict verification JSON")
    response.add_argument("--request", type=Path, required=True)
    response.add_argument("--verification-json", type=Path, action="append", required=True)
    response.add_argument("--signing-key", type=Path, required=True)
    response.add_argument("--out", type=Path, required=True)
    response.set_defaults(handler=respond)

    verify = commands.add_parser("verify", help="run pinned full replay workers and sign their response")
    verify.add_argument("--request", type=Path, required=True)
    verify.add_argument("--artifact", type=Path, required=True)
    verify.add_argument("--signing-key", type=Path, required=True)
    verify.add_argument("--out-dir", type=Path, required=True)
    verify.add_argument("--jobs", type=positive_int, default=1,
                        help="independent model-loading shards; one worker batches all candidates by default")
    verify.add_argument(
        "--cpu-threads", type=positive_int,
        default=positive_int(os.environ.get("BONSAI_CPU_THREADS", "1")),
        help="OpenMP/BLAS threads per verifier worker (total demand is jobs × cpu-threads)",
    )
    verify.add_argument("--verifier-policy", type=Path,
                        help="optional artifact/thread-bound receipt-verifier-policy/v1 JSON")
    verify.add_argument("--python", type=Path,
                        default=engine / "bonsai" / ".venv" / "bin" / "python")
    verify.add_argument("--worker-timeout-seconds", type=positive_int, default=3600,
                        help="hard timeout per verifier subprocess (default: 3600)")
    verify.set_defaults(handler=verify_request)

    final = commands.add_parser("finalize", help="emit VERIFIED only after checking a pinned response")
    final.add_argument("--request", type=Path, required=True)
    final.add_argument("--response", type=Path, required=True)
    final.add_argument("--verifier-pubkey", required=True)
    final.add_argument("--out", type=Path, required=True)
    final.set_defaults(handler=finalize)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"receipt-handoff: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
