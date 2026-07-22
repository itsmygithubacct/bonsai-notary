"""Fail-closed Bonsai GPU receipt acceptance runner.

This is deliberately orchestration-only: CUDA parity and receipt verification
remain owned by the pinned engine.  The runner makes their ordering, resource
bounds, evidence, and failure semantics a stable notary interface.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .evidence import (
    SCHEMA,
    atomic_write,
    initialize,
    privacy_violations,
    publish_sanitized,
    sanitize_text,
    sanitize_value,
    sha256_file,
    write_checksums,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
FIXED_PROMPT = "Reply with only the word OK."
THREAD_ENV = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "TRINOTE_ORACLE_Q1_THREADS",
)
DEPENDENCIES = (
    ("engine", "integer_inference_engine"),
    ("chainC", "chain_c"),
    ("thirdEntry", "bsv_third_entry"),
)
GIT_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_EXIT = 124


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    """Bounded termination for a command and any children it spawned."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    process.wait(timeout=5)


def _git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded, read-only git query used by the acceptance trust gate."""
    try:
        return subprocess.run(
            ["git", "-C", str(path), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git source inspection timed out") from exc


def load_dependency_lock(path: Path) -> dict[str, str]:
    """Parse the exact three-repository immutable composition lock."""
    expected = {lock_name for _label, lock_name in DEPENDENCIES}
    revisions: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("dependency lock is unreadable") from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2 or fields[0] not in expected or not re.fullmatch(r"[0-9a-f]{40}", fields[1]):
            raise ValueError(f"dependency lock line {number} is invalid")
        if fields[0] in revisions:
            raise ValueError(f"dependency lock repeats {fields[0]}")
        revisions[fields[0]] = fields[1]
    if set(revisions) != expected:
        raise ValueError("dependency lock does not contain exactly the required repositories")
    return revisions


def git_source_state(path: Path, *, expected_revision: str | None = None) -> dict[str, Any]:
    """Describe a source tree without claiming dirty worktree bytes are HEAD.

    ``revision`` is populated only for a clean tree.  A dirty tree records its
    committed base separately, because the executed bytes are not represented
    by that commit and must never be presented as a HEAD source revision.
    """
    resolved = path.expanduser().resolve()
    result: dict[str, Any] = {
        "treeState": "unavailable",
        "revision": None,
    }
    if expected_revision is not None:
        result.update(expectedRevision=expected_revision, lockMatch=False)
    top = _git(resolved, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return result
    try:
        if Path(top.stdout.strip()).resolve() != resolved:
            result["treeState"] = "wrong-root"
            return result
    except OSError:
        result["treeState"] = "wrong-root"
        return result
    head = _git(resolved, "rev-parse", "HEAD")
    revision = head.stdout.strip().lower()
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", revision):
        return result
    status = _git(resolved, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        return result
    clean = not status.stdout
    result["treeState"] = "clean" if clean else "dirty"
    if clean:
        result["revision"] = revision
    else:
        result["baseCommit"] = revision
    if expected_revision is not None:
        result["lockMatch"] = revision == expected_revision
    return result


def composition_source_record(root: Path, engine: Path) -> dict[str, Any]:
    """Return the public-safe source/lock record used in every manifest."""
    lock_path = root / "dependencies.lock"
    revisions = load_dependency_lock(lock_path)
    record: dict[str, Any] = {
        "dependencyLock": {
            "sha256": sha256_file(lock_path),
            "valid": True,
        },
        "notary": git_source_state(root),
    }
    paths = {
        "engine": engine,
        "chainC": root / "chain_c",
        "thirdEntry": root / "bsv_third_entry",
    }
    for label, lock_name in DEPENDENCIES:
        record[label] = git_source_state(paths[label], expected_revision=revisions[lock_name])
    return record


def require_clean_composition(root: Path, engine: Path) -> dict[str, Any]:
    """Fail unless all four source trees are clean and dependencies match the lock."""
    record = composition_source_record(root, engine)
    problems: list[str] = []
    for label in ("notary", "engine", "chainC", "thirdEntry"):
        if record[label]["treeState"] != "clean":
            problems.append(f"{label} source tree is {record[label]['treeState']}")
    for label, _lock_name in DEPENDENCIES:
        if record[label].get("lockMatch") is not True:
            problems.append(f"{label} commit does not match dependencies.lock")
    if problems:
        raise ValueError("; ".join(problems))
    return record


def git_revision(path: Path) -> str | None:
    """Compatibility helper: only a clean worktree can be named by its commit."""
    return git_source_state(path).get("revision")


def _safe_git_source_state(path: Path) -> dict[str, Any]:
    try:
        return git_source_state(path)
    except (OSError, ValueError):
        return {"treeState": "unavailable", "revision": None}


class AcceptanceFailure(RuntimeError):
    def __init__(self, phase: str, code: int, message: str):
        super().__init__(message)
        self.phase = phase
        self.code = code or 1


class Runner:
    def __init__(self, args: argparse.Namespace, evidence_root: Path):
        self.args = args
        self.root = ROOT
        self.evidence_root = evidence_root
        self.phases: list[dict[str, Any]] = []
        self.first_failure: dict[str, Any] | None = None
        self.started = utc_now()
        self.start_monotonic = time.monotonic()
        self.state_home = Path(args.notary_home).expanduser().resolve()
        self.engine = Path(args.engine_dir).expanduser().resolve()
        self.python = Path(args.python).expanduser().resolve()
        self.artifact = Path(args.artifact).expanduser().resolve()
        self.verifier_policy = (
            Path(args.verifier_policy).expanduser().resolve() if args.verifier_policy else None
        )
        self.bundle_dir = self.state_home / "bundles"
        self.private_paths = (self.state_home, self.engine, self.root, self.evidence_root, Path.home())
        self.engine_reports: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, Any] | None = None
        self.initial_sources: dict[str, Any] | None = None
        self.telemetry_process: subprocess.Popen[bytes] | None = None
        self.telemetry_stream = None
        self.telemetry_started = False
        self.telemetry_stopped = True
        self.env = os.environ.copy()
        self.env.update(
            BONSAI_NOTARY_HOME=str(self.state_home),
            BONSAI_ENGINE_DIR=str(self.engine),
            BONSAI_GPU="1",
            BONSAI_CPU_THREADS=str(args.cpu_threads),
        )
        for name in THREAD_ENV:
            self.env[name] = str(args.cpu_threads)
        self.env["OMP_DYNAMIC"] = "FALSE"
        self.env.setdefault("OMP_WAIT_POLICY", "PASSIVE")
        engine_src = self.engine / "bonsai" / "src"
        bsv_dir = Path(os.environ.get("BONSAI_BSV_TE_DIR", self.root / "bsv_third_entry"))
        existing = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = os.pathsep.join(
            [str(engine_src), str(bsv_dir)] + ([existing] if existing else [])
        )

    def _record_failure(self, phase: str, code: int, message: str) -> None:
        if self.first_failure is None:
            safe_message = sanitize_text(str(message), private_paths=self.private_paths)
            if privacy_violations(safe_message):
                safe_message = "failure detail withheld by privacy policy"
            self.first_failure = {"phase": phase, "exitCode": code or 1, "message": safe_message}

    def local_phase(self, name: str, callback) -> Any:
        started_at = utc_now()
        begun = time.monotonic()
        print(f"[accept-gpu] {name}: starting", flush=True)
        try:
            result = callback()
        except AcceptanceFailure as exc:
            duration = time.monotonic() - begun
            self.phases.append({
                "name": name, "status": "fail", "exitCode": exc.code,
                "startedAt": started_at, "durationSeconds": round(duration, 6),
            })
            self._record_failure(name, exc.code, str(exc))
            print(f"[accept-gpu] {name}: FAILED ({exc})", file=sys.stderr, flush=True)
            raise
        except Exception as exc:
            duration = time.monotonic() - begun
            self.phases.append({
                "name": name, "status": "fail", "exitCode": 1,
                "startedAt": started_at, "durationSeconds": round(duration, 6),
            })
            self._record_failure(name, 1, str(exc))
            raise AcceptanceFailure(name, 1, str(exc)) from exc
        duration = time.monotonic() - begun
        self.phases.append({
            "name": name, "status": "pass", "exitCode": 0,
            "startedAt": started_at, "durationSeconds": round(duration, 6),
        })
        print(f"[accept-gpu] {name}: passed in {duration:.2f}s", flush=True)
        return result

    def command_phase(
        self,
        name: str,
        command: Sequence[str],
        *,
        stdout_name: str | None = None,
    ) -> Path:
        started_at = utc_now()
        begun = time.monotonic()
        raw_stdout = self.evidence_root / "raw" / (stdout_name or f"{name}.stdout.log")
        raw_stderr = self.evidence_root / "raw" / f"{name}.stderr.log"
        print(f"[accept-gpu] {name}: starting", flush=True)
        try:
            with raw_stdout.open("wb") as out, raw_stderr.open("wb") as err:
                process = subprocess.Popen(
                    list(command), cwd=self.root, env=self.env, stdout=out, stderr=err,
                    start_new_session=True,
                )
                next_progress = time.monotonic() + self.args.progress_seconds
                deadline = begun + self.args.command_timeout_seconds
                try:
                    while process.poll() is None:
                        time.sleep(min(1.0, self.args.progress_seconds))
                        if time.monotonic() >= deadline:
                            err.write(
                                f"command timed out after {self.args.command_timeout_seconds} seconds\n".encode()
                            )
                            err.flush()
                            _terminate_process(process)
                            returncode = COMMAND_TIMEOUT_EXIT
                            break
                        if time.monotonic() >= next_progress:
                            elapsed = time.monotonic() - begun
                            print(f"[accept-gpu] {name}: still running ({elapsed:.0f}s elapsed)", flush=True)
                            next_progress = time.monotonic() + self.args.progress_seconds
                    else:
                        returncode = process.returncode or 0
                except BaseException:
                    _terminate_process(process)
                    raise
        except FileNotFoundError as exc:
            returncode = 127
            raw_stderr.write_text(f"{exc}\n", encoding="utf-8")
        duration = time.monotonic() - begun
        status = "pass" if returncode == 0 else "fail"
        self.phases.append({
            "name": name, "status": status, "exitCode": returncode,
            "startedAt": started_at, "durationSeconds": round(duration, 6),
        })
        if returncode:
            stderr = raw_stderr.read_text(encoding="utf-8", errors="replace")[-4000:]
            summary = sanitize_text(stderr, private_paths=self.private_paths).strip()
            message = f"command exited {returncode}" + (f": {summary}" if summary else "")
            # Record the process result before publication/sanitization so a
            # later privacy failure can never mask the first command exit.
            self._record_failure(name, returncode, message)
        for raw_path in (raw_stdout, raw_stderr):
            if raw_path.exists():
                public_path = self.evidence_root / "public" / raw_path.name
                try:
                    publish_sanitized(raw_path, public_path, private_paths=self.private_paths)
                except ValueError as exc:
                    self._record_failure(name, 5, str(exc))
                    raise AcceptanceFailure(name, 5, str(exc)) from exc
        if returncode:
            print(f"[accept-gpu] {name}: FAILED with exit {returncode}", file=sys.stderr, flush=True)
            raise AcceptanceFailure(name, returncode, message)
        print(f"[accept-gpu] {name}: passed in {duration:.2f}s", flush=True)
        return raw_stdout

    def prerequisites(self) -> None:
        required = {
            "engine Python": self.python,
            "notary launcher": self.root / "bonsai-notary",
            "27B artifact": self.artifact,
            "GPU parity suite": self.engine / "bonsai" / "tests" / "test_bonsai_gpu.py",
            "Qwen3.5 parity suite": self.engine / "bonsai" / "tests" / "test_bonsai35_gpu.py",
            "agent signing key": self.state_home / "agent" / "keys" / "agent.key.json",
            "counterparty signing key": self.state_home / "agent" / "keys" / "counterparty.key.json",
        }
        missing = [label for label, path in required.items() if not path.is_file()]
        if shutil.which("nvidia-smi") is None:
            missing.append("nvidia-smi")
        if missing:
            raise AcceptanceFailure("prerequisites", 4, "missing required inputs: " + ", ".join(missing))
        if self.verifier_policy is not None and not self.verifier_policy.is_file():
            raise AcceptanceFailure("prerequisites", 4, "configured verifier policy is missing")
        if self.verifier_policy is not None:
            try:
                policy = json.loads(self.verifier_policy.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AcceptanceFailure("prerequisites", 4, f"invalid verifier policy: {exc}") from exc
            if not isinstance(policy, dict) or policy.get("schema") != "receipt-verifier-policy/v1":
                raise AcceptanceFailure("prerequisites", 4, "verifier policy has the wrong schema")
            public_policy = sanitize_value(policy, private_paths=self.private_paths)
            if public_policy != policy or privacy_violations(json.dumps(public_policy, sort_keys=True)):
                raise AcceptanceFailure("prerequisites", 5, "verifier policy failed privacy scan")
            atomic_write(
                self.evidence_root / "verification" / "verifier-policy.json",
                self.verifier_policy.read_bytes(), mode=0o644,
            )
        if self.args.cpu_threads <= 0:
            raise AcceptanceFailure("prerequisites", 2, "CPU thread entitlement must be positive")
        try:
            self.sources = require_clean_composition(self.root, self.engine)
        except ValueError as exc:
            raise AcceptanceFailure("prerequisites", 4, f"source composition rejected: {exc}") from exc
        self.initial_sources = self.sources

    def source_record(self) -> dict[str, Any]:
        try:
            self.sources = composition_source_record(self.root, self.engine)
        except (OSError, ValueError) as exc:
            # Failure manifests still need an explicit non-claim.  Never fall
            # back to raw rev-parse output, which would mislabel dirty bytes as
            # the commit at HEAD.
            self.sources = {
                "dependencyLock": {"valid": False, "error": sanitize_text(str(exc))},
                "notary": _safe_git_source_state(self.root),
                "engine": _safe_git_source_state(self.engine),
                "chainC": _safe_git_source_state(self.root / "chain_c"),
                "thirdEntry": _safe_git_source_state(self.root / "bsv_third_entry"),
            }
        return self.sources

    def signer_metadata(self) -> dict[str, str]:
        code = """
import json, sys
from trinote.receipts.signing_ec import ECKey
result = {}
for label, path in (("model", sys.argv[1]), ("counterparty", sys.argv[2])):
    data = json.load(open(path, encoding="utf-8"))
    key = ECKey.from_json(data)
    result[label] = key.public_hex
print(json.dumps(result, sort_keys=True))
"""
        raw = self.command_phase(
            "signer-metadata",
            [str(self.python), "-c", code,
             str(self.state_home / "agent" / "keys" / "agent.key.json"),
             str(self.state_home / "agent" / "keys" / "counterparty.key.json")],
            stdout_name="signers.json",
        )
        try:
            metadata = json.loads(raw.read_text(encoding="utf-8"))
            for label in ("model", "counterparty"):
                if not re.fullmatch(r"0[23][0-9a-fA-F]{64}", metadata[label]):
                    raise ValueError(f"invalid {label} compressed public key")
            if metadata["model"].lower() == metadata["counterparty"].lower():
                raise ValueError("model and counterparty signing identities must be distinct")
            return {key: value.lower() for key, value in metadata.items()}
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise AcceptanceFailure("signer-metadata", 5, f"invalid public signer metadata: {exc}") from exc

    def start_telemetry(self) -> None:
        raw = self.evidence_root / "raw" / "gpu-telemetry.csv"
        self.telemetry_stream = raw.open("wb")
        try:
            self.telemetry_process = subprocess.Popen(
                ["nvidia-smi",
                 "--query-gpu=timestamp,index,utilization.gpu,memory.used,memory.total,power.draw",
                 "--format=csv,noheader,nounits", "-l", "1"],
                stdout=self.telemetry_stream, stderr=subprocess.STDOUT, env=self.env,
            )
            time.sleep(0.15)
            if self.telemetry_process.poll() not in (None, 0):
                raise AcceptanceFailure("telemetry-start", 3, "nvidia-smi telemetry sampler exited early")
            self.telemetry_started = True
            self.telemetry_stopped = False
        except BaseException:
            self.telemetry_stream.close()
            self.telemetry_stream = None
            self.telemetry_process = None
            raise

    def stop_telemetry(self) -> None:
        process = self.telemetry_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        returncode = process.returncode if process is not None else 0
        if self.telemetry_stream is not None:
            self.telemetry_stream.close()
        self.telemetry_process = None
        self.telemetry_stream = None
        self.telemetry_stopped = True
        raw = self.evidence_root / "raw" / "gpu-telemetry.csv"
        if raw.exists():
            publish_sanitized(
                raw, self.evidence_root / "public" / "gpu-telemetry.csv",
                private_paths=self.private_paths,
            )
        if returncode not in (0, -15):
            raise AcceptanceFailure("telemetry-stop", 3, f"telemetry sampler exited {returncode}")

    def validate_run_report(self, label: str, path: Path) -> dict[str, Any]:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcceptanceFailure(label, 7, f"missing or invalid engine run report: {exc}") from exc
        if report.get("schema") != SCHEMA or report.get("status") != "pass" or report.get("exitCode") != 0:
            raise AcceptanceFailure(label, 7, "engine run report did not finish with receipt-run/v1 pass")
        resources = report.get("resources") or {}
        threads = resources.get("threads") or {}
        oracle_workers = resources.get("oracleQ1Workers")
        if (
            any(
                type(threads.get(name)) is not int
                or threads.get(name) != self.args.cpu_threads
                for name in THREAD_ENV
            )
            or type(oracle_workers) is not int
            or oracle_workers != self.args.cpu_threads
        ):
            raise AcceptanceFailure(label, 7, "engine run report does not preserve the CPU thread entitlement")
        if label == "producer-report":
            if (
                (report.get("options") or {}).get("gpuRequired") is not True
                or (report.get("options") or {}).get("receipt") is not True
                or (report.get("options") or {}).get("verifyMode") != "fresh-oracle"
                or (report.get("engine") or {}).get("gpuResident") is not True
                or (report.get("model") or {}).get("architecture") != "qwen35"
                or (report.get("cleanup") or {}).get("gpuClosed") is not True
            ):
                raise AcceptanceFailure(
                    label, 7, "producer report does not prove required GPU residency and clean closure"
                )
            residency = next(
                (phase for phase in report.get("phases") or [] if phase.get("name") == "gpu-residency-upload"),
                None,
            )
            proof = (residency or {}).get("report") or {}
            peak = proof.get("peak_used_bytes")
            if (
                not isinstance(residency, dict)
                or residency.get("enabled") is not True
                or residency.get("status") != "ok"
                or not isinstance(peak, int)
                or peak <= 0
                or peak > self.args.max_gpu_proof_bytes
            ):
                raise AcceptanceFailure(
                    label, 7,
                    f"CUDA memory proof is absent or exceeds {self.args.max_gpu_proof_bytes} bytes",
                )
        else:
            if report.get("operation") != "verify-receipt-bundles":
                raise AcceptanceFailure(label, 7, "verifier report has the wrong operation")
            if bool((report.get("engine") or {}).get("policyApplied")) != (self.verifier_policy is not None):
                raise AcceptanceFailure(label, 7, "verifier report policy selection disagrees with acceptance")
        public_report = sanitize_value(report, private_paths=self.private_paths)
        rendered = json.dumps(public_report, sort_keys=True)
        violations = privacy_violations(rendered)
        if violations:
            raise AcceptanceFailure(label, 5, "run report failed the public privacy scan")
        write_json(self.evidence_root / "public" / f"{label}.json", public_report, public=True)
        summary = {
            "status": report["status"], "operation": report.get("operation"),
            "totalSeconds": report.get("totalSeconds"),
            "phaseCount": len(report.get("phases") or []),
            "threads": (report.get("resources") or {}).get("threads"),
        }
        if label == "producer-report":
            summary.update(
                gpuResident=(report.get("engine") or {}).get("gpuResident"),
                gpuClosed=(report.get("cleanup") or {}).get("gpuClosed"),
            )
        self.engine_reports[label] = summary
        return report

    def select_bundle(self, before: dict[Path, tuple[int, int]]) -> Path:
        candidates: list[Path] = []
        if self.bundle_dir.is_dir():
            for path in self.bundle_dir.glob("bonsai-*.tar.gz"):
                stat = path.stat()
                current = (stat.st_mtime_ns, stat.st_size)
                if path not in before or before[path] != current:
                    candidates.append(path)
        if not candidates:
            raise AcceptanceFailure("bundle", 6, "receipt command did not create or refresh a bundle")
        selected = max(candidates, key=lambda item: item.stat().st_mtime_ns)
        if selected.stat().st_size <= 0:
            raise AcceptanceFailure("bundle", 6, "receipt bundle is empty")
        return selected

    def manifest(self, *, status: str, bundle: Path | None, verification: dict[str, Any] | None) -> dict[str, Any]:
        elapsed = time.monotonic() - self.start_monotonic
        sources = self.source_record()
        if status == "pass" and (self.initial_sources is None or sources != self.initial_sources):
            raise AcceptanceFailure(
                "source-stability", 4,
                "source composition changed after the prerequisite trust gate",
            )
        artifacts: dict[str, Any] = {
            "artifact": {"file": self.artifact.name, "sha256": sha256_file(self.artifact)}
            if self.artifact.is_file() else None,
        }
        if bundle is not None and bundle.is_file():
            artifacts["bundle"] = {
                "file": bundle.name, "bytes": bundle.stat().st_size, "sha256": sha256_file(bundle),
            }
        if verification:
            artifacts["verification"] = {
                "ok": verification.get("ok"),
                "bundleHash": verification.get("bundleHash"),
                "offlineOk": (verification.get("offline") or {}).get("ok"),
                "reexecOk": (verification.get("reexec") or {}).get("ok"),
                "reexecStrategy": (verification.get("reexec") or {}).get("strategy"),
            }
        if self.verifier_policy is not None and self.verifier_policy.is_file():
            artifacts["verifierPolicy"] = {
                "file": "verification/verifier-policy.json",
                "sha256": sha256_file(self.verifier_policy),
            }
        result = {
            "schema": SCHEMA,
            "status": status,
            "label": "VERIFIED" if status == "pass" else "FAILED",
            "startedAt": self.started,
            "finishedAt": utc_now(),
            "durationSeconds": round(elapsed, 6),
            "request": {
                "model": "27b", "promptUtf8Sha256": __import__("hashlib").sha256(
                    self.args.prompt.encode("utf-8")
                ).hexdigest(), "maxNewTokens": 1,
            },
            "resources": {
                "cpuThreads": self.args.cpu_threads,
                "threadEnvironment": {name: self.args.cpu_threads for name in THREAD_ENV},
                "gpuRequired": True,
                "maxGpuProofBytes": self.args.max_gpu_proof_bytes,
                "commandTimeoutSeconds": self.args.command_timeout_seconds,
            },
            "sources": sources,
            "phases": self.phases,
            "firstFailure": self.first_failure,
            "artifacts": artifacts,
            "engineReports": self.engine_reports,
            "evidence": {
                "namespaces": {
                    "raw": "private; may contain host paths or provider output",
                    "public": "sanitized logs and non-secret observations",
                    "bundle": "portable receipt bundle",
                    "verification": "pinned independent replay result",
                },
                "rawPublic": False,
                "operatorObservationsAreCryptographicClaims": False,
            },
            "cleanup": {
                "telemetryStarted": self.telemetry_started,
                "telemetryProcessStopped": self.telemetry_stopped,
                "networkBroadcastAttempted": False,
            },
        }
        return result

    def execute(self) -> tuple[int, dict[str, Any]]:
        selected_copy: Path | None = None
        verification: dict[str, Any] | None = None
        try:
            self.local_phase("prerequisites", self.prerequisites)
            self.command_phase(
                "gpu-identity",
                ["nvidia-smi", "--query-gpu=name,compute_cap,driver_version,memory.total",
                 "--format=csv,noheader,nounits"],
            )
            availability_code = """
import json
from trinote.infer_int.gpu_native import gpu_available, gpu_memory_info
available = bool(gpu_available())
print(json.dumps({"available": available, "memory": gpu_memory_info() if available else None}, sort_keys=True))
raise SystemExit(0 if available else 3)
"""
            self.command_phase("cuda-availability", [str(self.python), "-c", availability_code])
            self.local_phase("telemetry-start", self.start_telemetry)
            self.command_phase(
                "cuda-parity",
                [str(self.python), "-m", "pytest",
                 str(self.engine / "bonsai" / "tests" / "test_bonsai_gpu.py"),
                 str(self.engine / "bonsai" / "tests" / "test_bonsai35_gpu.py"), "-q"],
            )
            signers = self.signer_metadata()
            before = {
                path: (path.stat().st_mtime_ns, path.stat().st_size)
                for path in self.bundle_dir.glob("bonsai-*.tar.gz")
            } if self.bundle_dir.is_dir() else {}
            self.command_phase(
                "one-token-receipt",
                [str(self.root / "bonsai-notary"), self.args.prompt,
                 "--model", "27b", "--receipts", "--no-think", "-n", "1",
                 "--verbose", "--require-gpu", "--cpu-threads", str(self.args.cpu_threads),
                 "--run-report", str(self.evidence_root / "raw" / "producer-report.json")],
            )
            self.local_phase(
                "producer-report",
                lambda: self.validate_run_report(
                    "producer-report", self.evidence_root / "raw" / "producer-report.json"
                ),
            )
            selected = self.local_phase("bundle", lambda: self.select_bundle(before))
            selected_copy = self.evidence_root / "bundle" / selected.name
            shutil.copy2(selected, selected_copy)
            os.chmod(selected_copy, 0o644)
            verify_command = [
                str(self.python), "-m", "trinote.cli.receipt_bundle_cli", "verify",
                str(selected_copy), "--reexec", "--artifact", str(self.artifact),
                "--model-pubkey", signers["model"],
                "--counterparty-pubkey", signers["counterparty"],
                "--cpu-threads", str(self.args.cpu_threads),
                "--run-report", str(self.evidence_root / "raw" / "verifier-report.json"), "--json",
            ]
            if self.verifier_policy is not None:
                verify_command.extend(["--strategy-policy", str(self.verifier_policy)])
            verify_raw = self.command_phase(
                "bundle-verification", verify_command,
                stdout_name="verification.json",
            )
            self.local_phase(
                "verifier-report",
                lambda: self.validate_run_report(
                    "verifier-report", self.evidence_root / "raw" / "verifier-report.json"
                ),
            )
            try:
                verification = json.loads(verify_raw.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AcceptanceFailure("bundle-verification", 7, f"verifier emitted invalid JSON: {exc}") from exc
            reexec = verification.get("reexec") or {}
            raw_reexec = reexec.get("raw") or {}
            pinned_signatures = (
                reexec.get("signaturePinned") is True
                and reexec.get("sigModelPresent") is True
                and reexec.get("sigCounterpartyPresent") is True
                and reexec.get("sigModelAuthenticated") is True
                and reexec.get("sigCounterpartyAuthenticated") is True
                and raw_reexec.get("sigModelOk") is True
                and raw_reexec.get("sigCounterpartyOk") is True
                and raw_reexec.get("sigModelAuthenticated") is True
                and raw_reexec.get("sigCounterpartyAuthenticated") is True
            )
            required_ok = (
                verification.get("ok") is True
                and (verification.get("offline") or {}).get("ok") is True
                and reexec.get("ok") is True
                and pinned_signatures
                and reexec.get("sampled") is not True
            )
            if not required_ok:
                raise AcceptanceFailure(
                    "bundle-verification", 7,
                    "verifier did not pass offline, pinned signatures, and re-execution",
                )
            self.local_phase("telemetry-stop", self.stop_telemetry)
            verification_path = self.evidence_root / "verification" / "result.json"
            public_verification = sanitize_value(verification, private_paths=self.private_paths)
            if privacy_violations(json.dumps(public_verification, sort_keys=True)):
                raise AcceptanceFailure("bundle-verification", 5, "verification JSON failed privacy scan")
            write_json(verification_path, public_verification, public=True)
            manifest = self.manifest(status="pass", bundle=selected_copy, verification=verification)
            write_json(self.evidence_root / "manifest.json", manifest, public=True)
            write_checksums(self.evidence_root)
            return 0, manifest
        except AcceptanceFailure as exc:
            self._record_failure(exc.phase, exc.code, str(exc))
            if self.telemetry_process is not None or self.telemetry_stream is not None:
                try:
                    self.local_phase("telemetry-stop", self.stop_telemetry)
                except AcceptanceFailure as cleanup_exc:
                    self._record_failure(cleanup_exc.phase, cleanup_exc.code, str(cleanup_exc))
            manifest = self.manifest(status="fail", bundle=selected_copy, verification=verification)
            write_json(self.evidence_root / "manifest.json", manifest, public=True)
            write_checksums(self.evidence_root)
            return self.first_failure["exitCode"] if self.first_failure else exc.code, manifest
        except KeyboardInterrupt:
            self._record_failure("interrupted", 130, "acceptance interrupted")
            if self.telemetry_process is not None or self.telemetry_stream is not None:
                try:
                    self.local_phase("telemetry-stop", self.stop_telemetry)
                except AcceptanceFailure:
                    pass
            manifest = self.manifest(status="fail", bundle=selected_copy, verification=verification)
            write_json(self.evidence_root / "manifest.json", manifest, public=True)
            write_checksums(self.evidence_root)
            return 130, manifest
        except Exception as exc:
            self._record_failure("acceptance", 1, str(exc))
            if self.telemetry_process is not None or self.telemetry_stream is not None:
                try:
                    self.local_phase("telemetry-stop", self.stop_telemetry)
                except AcceptanceFailure:
                    pass
            manifest = self.manifest(status="fail", bundle=selected_copy, verification=verification)
            write_json(self.evidence_root / "manifest.json", manifest, public=True)
            write_checksums(self.evidence_root)
            return 1, manifest


def build_parser() -> argparse.ArgumentParser:
    state_home = Path(os.environ.get("BONSAI_NOTARY_HOME", Path.home() / ".local" / "trinote"))
    engine = Path(os.environ.get("BONSAI_ENGINE_DIR", ROOT / "engine"))
    default_python = engine / "bonsai" / ".venv" / "bin" / "python"
    parser = argparse.ArgumentParser(
        description="Run the fail-closed Bonsai-27B CUDA receipt and replay acceptance gate."
    )
    parser.add_argument("--record-dir", type=Path, help="persist raw/public/bundle/verification evidence here")
    parser.add_argument("--cpu-threads", type=positive_int,
                        default=positive_int(os.environ.get("BONSAI_CPU_THREADS", str(os.cpu_count() or 1))))
    parser.add_argument("--notary-home", default=str(state_home))
    parser.add_argument("--engine-dir", default=str(engine))
    parser.add_argument("--python", default=str(default_python))
    parser.add_argument("--artifact", default=None,
                        help="27B artifact (default: <notary-home>/models/Bonsai-27B-Q1_0-int-qwen35.safetensors)")
    parser.add_argument("--progress-seconds", type=positive_int, default=15)
    parser.add_argument("--command-timeout-seconds", type=positive_int, default=3600,
                        help="hard timeout for each acceptance subprocess (default: 3600)")
    parser.add_argument("--max-gpu-proof-bytes", type=positive_int, default=15 * (1 << 29),
                        help="maximum accepted CUDA feasibility peak (default: 7.5 GiB)")
    parser.add_argument("--verifier-policy",
                        help="optional artifact/thread-bound receipt-verifier-policy/v1 JSON")
    parser.add_argument("--dry-run", action="store_true", help="print the phase plan without running commands")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.artifact is None:
        args.artifact = str(
            Path(args.notary_home).expanduser()
            / "models" / "Bonsai-27B-Q1_0-int-qwen35.safetensors"
        )
    args.prompt = FIXED_PROMPT
    if args.dry_run:
        print(json.dumps({
            "schema": SCHEMA,
            "dryRun": True,
            "gpuRequired": True,
            "cpuThreads": args.cpu_threads,
            "maxGpuProofBytes": args.max_gpu_proof_bytes,
            "commandTimeoutSeconds": args.command_timeout_seconds,
            "verifierPolicy": str(args.verifier_policy) if args.verifier_policy else None,
            "recordDir": str(args.record_dir) if args.record_dir else None,
            "phases": [
                "prerequisites", "gpu-identity", "cuda-availability", "telemetry-start",
                "cuda-parity", "signer-metadata", "one-token-receipt", "producer-report", "bundle",
                "bundle-verification", "verifier-report",
                "telemetry-stop",
            ],
            "networkBroadcastAttempted": False,
        }, indent=2, sort_keys=True))
        return 0
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.record_dir:
        evidence_root = args.record_dir.expanduser().resolve()
        if evidence_root.exists():
            if not evidence_root.is_dir():
                print(f"accept-gpu: record path is not a directory: {evidence_root}", file=sys.stderr)
                return 2
            if any(evidence_root.iterdir()):
                print(f"accept-gpu: record directory is not empty: {evidence_root}", file=sys.stderr)
                return 2
    else:
        temporary = tempfile.TemporaryDirectory(prefix="bonsai-acceptance-")
        evidence_root = Path(temporary.name)
    initialize(evidence_root)
    runner = Runner(args, evidence_root)
    code, manifest = runner.execute()
    rendered = json.dumps(manifest, indent=2, sort_keys=True)
    if privacy_violations(rendered):
        print("accept-gpu: refusing manifest with private material", file=sys.stderr)
        code = code or 5
    else:
        print(rendered)
        if args.record_dir:
            print(f"[accept-gpu] evidence: {evidence_root}", file=sys.stderr)
    if temporary is not None:
        temporary.cleanup()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
