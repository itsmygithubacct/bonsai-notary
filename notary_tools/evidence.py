"""Privacy-safe evidence helpers for supported notary acceptance runs.

Raw command output is private by construction.  Only sanitized copies are
placed in the public namespace, and publication fails if a known secret or
provider-endpoint pattern remains after redaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "receipt-run/v1"
NAMESPACES = ("raw", "public", "bundle", "verification")
_PRIVATE_KEY_LABEL = "PRIVATE" + " KEY"
_AUTH_TOKEN_FIELD = "oauth" + "_token"

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key PEM", re.compile(
        rf"-----BEGIN (?:EC |RSA |OPENSSH )?{_PRIVATE_KEY_LABEL}-----.*?"
        rf"-----END (?:EC |RSA |OPENSSH )?{_PRIVATE_KEY_LABEL}-----", re.DOTALL)),
    ("WIF", re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[5KL][1-9A-HJ-NP-Za-km-z]{50,51}(?![1-9A-HJ-NP-Za-km-z])")),
    ("bearer token", re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("OAuth token", re.compile(
        rf"(?i)\b(?:{_AUTH_TOKEN_FIELD}|access_token|refresh_token)\s*[:=]\s*[^\s,;}}]+"
    )),
    ("private-key field", re.compile(r"(?i)(?:\"?(?:wif|mnemonic|private[_-]?key|secret)\"?\s*[:=]\s*)\"?[^\s,}\"]+")),
    ("provider SSH host", re.compile(r"(?i)\b(?:ssh\d+\.vast\.ai|[a-z0-9.-]+\.vast\.ai:\d+)\b")),
    ("signed URL", re.compile(r"https?://[^\s\"']+[?&](?:X-Amz-|Policy=|Signature=|Expires=|token=)[^\s\"']*", re.IGNORECASE)),
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
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
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def write_json(path: Path, value: Any, *, public: bool = False) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    atomic_write(path, payload, mode=0o644 if public else 0o600)


def sanitize_text(text: str, *, private_paths: Iterable[str | Path] = ()) -> str:
    sanitized = text
    paths = sorted({str(Path(item).expanduser()) for item in private_paths if str(item)}, key=len, reverse=True)
    for item in paths:
        sanitized = sanitized.replace(item, "<redacted-path>")
    # Home paths are private even when a caller forgot to enumerate one.
    sanitized = re.sub(r"(?<![A-Za-z0-9_.-])/(?:home|root)/[^/\s]+", "<redacted-home>", sanitized)
    for label, pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(f"<redacted-{label.replace(' ', '-')}>", sanitized)
    return sanitized


def sanitize_value(value: Any, *, private_paths: Iterable[str | Path] = ()) -> Any:
    """Recursively sanitize strings in a JSON-compatible evidence value."""
    if isinstance(value, str):
        return sanitize_text(value, private_paths=private_paths)
    if isinstance(value, list):
        return [sanitize_value(item, private_paths=private_paths) for item in value]
    if isinstance(value, dict):
        return {
            str(key): sanitize_value(item, private_paths=private_paths)
            for key, item in value.items()
        }
    return value


def privacy_violations(text: str) -> list[str]:
    return [label for label, pattern in _SECRET_PATTERNS if pattern.search(text)]


def publish_sanitized(raw_path: Path, public_path: Path, *, private_paths: Iterable[str | Path] = ()) -> None:
    sanitized = sanitize_text(raw_path.read_text(encoding="utf-8", errors="replace"), private_paths=private_paths)
    violations = privacy_violations(sanitized)
    if violations:
        raise ValueError(f"refusing public evidence; privacy scan found: {', '.join(sorted(set(violations)))}")
    atomic_write(public_path, sanitized.encode("utf-8"), mode=0o644)


def initialize(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for namespace in NAMESPACES:
        path = root / namespace
        path.mkdir(exist_ok=True)
        os.chmod(path, 0o700 if namespace == "raw" else 0o755)


def checksum_entries(root: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS" or "raw" in path.relative_to(root).parts:
            continue
        entries.append((path.relative_to(root).as_posix(), sha256_file(path)))
    return entries


def write_checksums(root: Path) -> None:
    lines = [f"{digest}  {relative}" for relative, digest in checksum_entries(root)]
    atomic_write(root / "SHA256SUMS", ("\n".join(lines) + "\n").encode("utf-8"), mode=0o644)
