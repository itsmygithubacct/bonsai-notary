"""Privacy-gated terminal recording and rendering for GPU acceptance.

The raw asciicast remains private.  A normalized public cast is written only
after strict validation, and rendering consumes only that public cast.  Timing
in the cast is never rewritten; idle-time limiting belongs solely to the
renderer so the source evidence preserves the real command timeline.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .evidence import atomic_write, privacy_violations, sanitize_text, sha256_file, write_json


MEDIA_SCHEMA = "receipt-acceptance-media/v1"
TERMINAL_COLUMNS = 120
TERMINAL_ROWS = 36
RAW_CAST_NAME = "acceptance.cast"
PUBLIC_CAST_NAME = "acceptance.cast"
GIF_NAME = "acceptance.gif"
MP4_NAME = "acceptance.mp4"
MANIFEST_NAME = "media-manifest.json"
STATUS_NAME = "media-command-exit.txt"
MEDIA_PRIVACY_EXIT = 5
MEDIA_TOOL_EXIT = 4

_WIF = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])(?:5[HJK][1-9A-HJ-NP-Za-km-z]{49}|"
    r"9[1-9A-HJ-NP-Za-km-z]{50}|[KLc][1-9A-HJ-NP-Za-km-z]{51})"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)
_PRIVATE_KEY_LABEL = "PRIVATE" + " KEY"
_AUTH_TOKEN_FIELD = "oauth" + "_token"
_RAW_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("WIF", _WIF),
    ("mnemonic marker", re.compile(r"(?i)\b(?:mnemonic|seed[ -]phrase)\b")),
    ("private-key marker", re.compile(
        rf"(?i)(?:BEGIN[^\r\n]*{_PRIVATE_KEY_LABEL}|private[_ -]?key\s*[:=])"
    )),
    ("OAuth credential", re.compile(
        rf"(?i)(?:\b(?:{_AUTH_TOKEN_FIELD}|access_token|refresh_token)\b|"
        r"\b(?:ghp|gho)_[A-Za-z0-9_]+\b)"
    )),
    ("provider host", re.compile(
        r"(?i)\b(?:[a-z0-9.-]*vast\.ai(?::\d+)?|(?:[0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]{2,5})\b"
    )),
)
_ABSOLUTE_HOST_PATH = re.compile(
    r"(?<![\w./\-])/(?!/)(?=[^\s/])\S+"
)
_FILE_URI_HOST_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9+.\-])file:(?:///|//[^/\s]+/|/)[^\s]+"
)
_UNC_HOST_PATH = re.compile(
    r"(?<![\w.:/\\\-])(?://[^/\s]+/[^\s]+|\\\\[^\\\s]+\\[^\s]+)"
)
_REDACTED_PATH = re.compile(r"<redacted-(?:home|path)>")
_SGR_CONTROL = re.compile(r"\x1b\[[0-9;:]*m")
_UNSUPPORTED_TERMINAL_CONTROL = re.compile(r"\x1b|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class MediaError(RuntimeError):
    """A fail-closed recording or publication failure with a stable exit."""

    def __init__(
        self,
        message: str,
        *,
        code: int = MEDIA_PRIVACY_EXIT,
        command_exit_code: int | None = None,
    ):
        super().__init__(message)
        self.code = code or 1
        self.command_exit_code = command_exit_code
        self.manifest: dict[str, Any] | None = None
        self.manifest_path: Path | None = None

    @property
    def effective_exit_code(self) -> int:
        """Prefer the recorded command's first failure over later media work."""
        if self.command_exit_code not in (None, 0):
            return self.command_exit_code
        return self.code


@dataclass(frozen=True)
class MediaTools:
    asciinema: str
    agg: str | None
    ffmpeg: str | None

    @property
    def render_available(self) -> bool:
        return self.agg is not None and self.ffmpeg is not None


def detect_tools(
    which: Callable[[str], str | None] = shutil.which,
    *,
    require_render: bool = False,
) -> MediaTools:
    """Resolve recording tools without exposing their host paths in evidence."""
    asciinema = which("asciinema")
    if not asciinema:
        raise MediaError("media recording requires asciinema", code=MEDIA_TOOL_EXIT)
    tools = MediaTools(asciinema=asciinema, agg=which("agg"), ffmpeg=which("ffmpeg"))
    if require_render and not tools.render_available:
        missing = [name for name, path in (("agg", tools.agg), ("ffmpeg", tools.ffmpeg)) if not path]
        raise MediaError(
            "required media render tools are unavailable: " + ", ".join(missing),
            code=MEDIA_TOOL_EXIT,
        )
    return tools


def _normalized_exit(returncode: int) -> int:
    return 128 + (-returncode) if returncode < 0 else returncode


def run_child(command: Sequence[str], status_path: Path) -> int:
    """Run the recorded child and persist its real status for asciinema 2.

    Asciinema 2 exits successfully after a completed recording even when its
    command fails.  This wrapper records the command's exact shell-style exit
    before returning it, allowing the parent acceptance process to preserve
    the first failure.
    """
    if not command:
        raise MediaError("recorded child command is empty", code=2)
    try:
        result = subprocess.run(list(command), check=False)
        code = _normalized_exit(result.returncode)
    except FileNotFoundError:
        code = 127
    atomic_write(status_path, f"{code}\n".encode("ascii"), mode=0o600)
    return code


def record_command(
    command: Sequence[str],
    *,
    raw_cast: Path,
    status_path: Path,
    tools: MediaTools,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """Record one command at fixed geometry and recover its true exit code."""
    raw_cast.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = [
        sys.executable,
        "-m",
        "notary_tools.media",
        "run-child",
        str(status_path),
        "--",
        *command,
    ]
    invocation = [
        tools.asciinema,
        "rec",
        "--quiet",
        "--overwrite",
        "--cols",
        str(TERMINAL_COLUMNS),
        "--rows",
        str(TERMINAL_ROWS),
        "--command",
        shlex.join(wrapper),
        str(raw_cast),
    ]
    # Deliberately do not pass asciinema's idle-time option.  The raw and
    # public casts retain the real timeline; only rendering may cap idle time.
    record_env = dict(env) if env is not None else os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    existing_python_path = record_env.get("PYTHONPATH")
    record_env["PYTHONPATH"] = (
        package_root + (os.pathsep + existing_python_path if existing_python_path else "")
    )
    record_env.update(
        TERM="xterm-256color",
        COLUMNS=str(TERMINAL_COLUMNS),
        LINES=str(TERMINAL_ROWS),
    )
    try:
        result = run(invocation, cwd=cwd, env=record_env, check=False)
    except OSError as exc:
        raise MediaError("asciinema recorder could not be started", code=MEDIA_TOOL_EXIT) from exc
    child_code: int | None = None
    status_error: MediaError | None = None
    try:
        rendered_status = status_path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", rendered_status):
            raise MediaError("recorded command exit status is malformed")
        child_code = int(rendered_status)
        if child_code > 255:
            raise MediaError("recorded command exit status is outside 0..255")
    except OSError:
        status_error = MediaError("recorded command did not persist its exit status")
    except MediaError as exc:
        status_error = exc
    if result.returncode != 0:
        raise MediaError(
            f"asciinema recorder exited {_normalized_exit(result.returncode)}",
            code=MEDIA_TOOL_EXIT,
            command_exit_code=child_code,
        )
    try:
        raw_cast_ok = raw_cast.is_file() and raw_cast.stat().st_size > 0
    except OSError as exc:
        raise MediaError(
            "asciinema recording could not be inspected",
            code=MEDIA_TOOL_EXIT,
            command_exit_code=child_code,
        ) from exc
    if not raw_cast_ok:
        raise MediaError(
            "asciinema did not produce a non-empty cast",
            code=MEDIA_TOOL_EXIT,
            command_exit_code=child_code,
        )
    if status_error is not None:
        raise status_error
    assert child_code is not None
    return child_code


def _is_invisible_unicode_control(character: str) -> bool:
    """Return whether a Unicode formatting code point can invisibly split a secret."""
    codepoint = ord(character)
    return (
        unicodedata.category(character) == "Cf"
        or codepoint == 0x034F  # combining grapheme joiner
        or 0x180B <= codepoint <= 0x180D  # Mongolian free variation selectors
        or codepoint == 0x180F  # Mongolian free variation selector four
        or 0xFE00 <= codepoint <= 0xFE0F  # variation selectors
        or 0xE0100 <= codepoint <= 0xE01EF  # variation selectors supplement
    )


def _terminal_scan_text(text: str) -> tuple[str, bool]:
    """Return a control-normalized scan view and whether unsafe controls remain."""
    decolored = _SGR_CONTROL.sub("", text).replace("\r", "")
    normalized_characters: list[str] = []
    invisible_control = False
    for character in decolored:
        if _is_invisible_unicode_control(character):
            invisible_control = True
        else:
            normalized_characters.append(character)
    normalized = "".join(normalized_characters)
    unsupported = (
        invisible_control
        or _UNSUPPORTED_TERMINAL_CONTROL.search(normalized) is not None
    )
    return normalized, unsupported


def _raw_privacy_violations(text: str) -> list[str]:
    scan_text, unsupported_control = _terminal_scan_text(text)
    labels = [
        label
        for label, pattern in _RAW_FORBIDDEN
        if pattern.search(text) or pattern.search(scan_text)
    ]
    labels.extend(privacy_violations(text))
    labels.extend(privacy_violations(scan_text))
    if unsupported_control:
        labels.append("unsupported terminal control")
    return sorted(set(labels))


def _public_privacy_violations(text: str) -> list[str]:
    labels = _raw_privacy_violations(text)
    scan_text, _unsupported_control = _terminal_scan_text(text)
    path_text = _REDACTED_PATH.sub("redacted-path", text)
    path_scan_text = _REDACTED_PATH.sub("redacted-path", scan_text)
    path_patterns = (_ABSOLUTE_HOST_PATH, _FILE_URI_HOST_PATH, _UNC_HOST_PATH)
    if any(
        pattern.search(candidate)
        for pattern in path_patterns
        for candidate in (path_text, path_scan_text)
    ):
        labels.append("absolute host path")
    return sorted(set(labels))


def sanitize_cast(
    raw_cast: Path,
    public_cast: Path,
    *,
    private_paths: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Validate and sanitize an asciicast without changing event timing."""
    try:
        lines = raw_cast.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise MediaError("raw recording is unreadable") from exc
    if not lines:
        raise MediaError("raw recording is empty")
    raw_violations = _raw_privacy_violations("\n".join(lines))
    if raw_violations:
        raise MediaError(
            "raw recording contains forbidden private markers: " + ", ".join(raw_violations)
        )
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise MediaError("raw recording header is invalid JSON") from exc
    if not isinstance(header, dict) or header.get("version") != 2:
        raise MediaError("raw recording must be asciicast v2")
    if header.get("width") != TERMINAL_COLUMNS or header.get("height") != TERMINAL_ROWS:
        raise MediaError(
            f"raw recording terminal must be {TERMINAL_COLUMNS}x{TERMINAL_ROWS}"
        )
    public_header = {
        "version": 2,
        "width": TERMINAL_COLUMNS,
        "height": TERMINAL_ROWS,
        "timestamp": 0,
        "env": {"TERM": "xterm-256color"},
        "title": "Bonsai-27B GPU receipt acceptance",
    }
    output = [json.dumps(public_header, sort_keys=True, separators=(",", ":"))]
    raw_payloads: list[str] = []
    public_payloads: list[str] = []
    last_timestamp = 0.0
    event_count = 0
    for number, line in enumerate(lines[1:], 2):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MediaError(f"raw recording event {number} is invalid JSON") from exc
        if (
            not isinstance(event, list)
            or len(event) != 3
            or not isinstance(event[0], (int, float))
            or isinstance(event[0], bool)
            or not math.isfinite(float(event[0]))
            or float(event[0]) < last_timestamp
            or event[1] != "o"
            or not isinstance(event[2], str)
        ):
            raise MediaError(f"raw recording event {number} has an invalid shape or order")
        timestamp = float(event[0])
        raw_payloads.append(event[2])
        safe_text = sanitize_text(event[2], private_paths=private_paths)
        violations = _public_privacy_violations(safe_text)
        if violations:
            raise MediaError(
                f"sanitized recording event {number} remains private: " + ", ".join(violations)
            )
        output.append(json.dumps(
            [event[0], event[1], safe_text], ensure_ascii=False, separators=(",", ":")
        ))
        public_payloads.append(safe_text)
        last_timestamp = timestamp
        event_count += 1
    if event_count == 0:
        raise MediaError("raw recording has no terminal output events")
    cross_event_raw_violations = _raw_privacy_violations("".join(raw_payloads))
    if cross_event_raw_violations:
        raise MediaError(
            "raw recording contains cross-event private markers: "
            + ", ".join(cross_event_raw_violations)
        )
    cross_event_public_violations = _public_privacy_violations("".join(public_payloads))
    if cross_event_public_violations:
        raise MediaError(
            "sanitized recording retains cross-event private markers: "
            + ", ".join(cross_event_public_violations)
        )
    public_text = "\n".join(output) + "\n"
    violations = _public_privacy_violations(public_text)
    if violations:
        raise MediaError("refusing public recording: " + ", ".join(violations))
    atomic_write(public_cast, public_text.encode("utf-8"), mode=0o644)
    return {
        "version": 2,
        "columns": TERMINAL_COLUMNS,
        "rows": TERMINAL_ROWS,
        "eventCount": event_count,
        "sourceTimelineSeconds": last_timestamp,
        "timingPreserved": True,
    }


def _artifact(root: Path, path: Path, *, public: bool) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "public": public,
    }


def _run_renderer(
    command: Sequence[str],
    *,
    cwd: Path,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    label: str,
) -> None:
    try:
        result = run(
            list(command),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise MediaError(f"{label} renderer could not be started") from exc
    if result.returncode:
        raise MediaError(f"{label} renderer exited {_normalized_exit(result.returncode)}")


def _render(
    public_cast: Path,
    evidence_root: Path,
    *,
    tools: MediaTools,
    idle_limit_seconds: float,
    playback_speed: float,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[Path, Path]:
    if not tools.render_available:
        raise MediaError("media render tools are unavailable", code=MEDIA_TOOL_EXIT)
    raw_dir = evidence_root / "raw"
    public_dir = evidence_root / "public"
    temporary_gif = raw_dir / "acceptance.render.gif"
    temporary_mp4 = raw_dir / "acceptance.render.mp4"
    _run_renderer(
        [
            str(tools.agg),
            "--quiet",
            "--no-loop",
            "--speed",
            str(playback_speed),
            "--idle-time-limit",
            str(idle_limit_seconds),
            "--last-frame-duration",
            "3",
            str(public_cast),
            str(temporary_gif),
        ],
        cwd=evidence_root,
        run=run,
        label="GIF",
    )
    if not temporary_gif.is_file() or temporary_gif.stat().st_size <= 0:
        raise MediaError("GIF renderer did not produce a non-empty artifact")
    _run_renderer(
        [
            str(tools.ffmpeg),
            "-y",
            "-v",
            "error",
            "-i",
            str(temporary_gif),
            "-vf",
            "fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary_mp4),
        ],
        cwd=evidence_root,
        run=run,
        label="MP4",
    )
    if not temporary_mp4.is_file() or temporary_mp4.stat().st_size <= 0:
        raise MediaError("MP4 renderer did not produce a non-empty artifact")
    gif = public_dir / GIF_NAME
    mp4 = public_dir / MP4_NAME
    try:
        os.replace(temporary_gif, gif)
        os.replace(temporary_mp4, mp4)
        os.chmod(gif, 0o644)
        os.chmod(mp4, 0o644)
    except OSError as exc:
        raise MediaError("rendered media artifacts could not be published") from exc
    return gif, mp4


def _write_media_manifest(evidence_root: Path, manifest: dict[str, Any]) -> Path:
    path = evidence_root / "public" / MANIFEST_NAME
    write_json(path, manifest, public=True)
    return path


def write_failure_manifest(
    evidence_root: Path,
    *,
    command_exit_code: int | None,
    tools: MediaTools,
    error: MediaError,
    require_render: bool = False,
    idle_limit_seconds: float = 1.0,
    playback_speed: float = 1.5,
) -> tuple[dict[str, Any], Path]:
    """Persist a media failure even when recording never reached publication."""
    raw_artifact: dict[str, Any] | None = None
    raw_cast = evidence_root / "raw" / RAW_CAST_NAME
    try:
        if raw_cast.is_file():
            raw_artifact = _artifact(evidence_root, raw_cast, public=False)
    except OSError:
        raw_artifact = None
    manifest: dict[str, Any] = {
        "schema": MEDIA_SCHEMA,
        "status": "fail",
        "commandExitCode": command_exit_code,
        "terminal": {"columns": TERMINAL_COLUMNS, "rows": TERMINAL_ROWS},
        "rawCast": raw_artifact,
        "privacy": {"status": "not-published", "rawPublic": False},
        "rendering": {
            "requested": "required" if require_render else "auto",
            "status": "not-run",
            "idleTimeLimitSeconds": idle_limit_seconds,
            "playbackSpeed": playback_speed,
            "idleLimitAppliedOnlyDuringRendering": True,
            "tools": {
                "asciinema": True,
                "agg": tools.agg is not None,
                "ffmpeg": tools.ffmpeg is not None,
            },
        },
        "failure": {"exitCode": error.code, "message": str(error)},
    }
    try:
        path = _write_media_manifest(evidence_root, manifest)
    except OSError as exc:
        raise MediaError(
            "media failure manifest could not be written",
            code=error.code,
            command_exit_code=command_exit_code,
        ) from exc
    error.manifest = manifest
    error.manifest_path = path
    return manifest, path


def publish_recording(
    evidence_root: Path,
    *,
    command_exit_code: int | None,
    tools: MediaTools,
    private_paths: Sequence[str | Path] = (),
    require_render: bool = False,
    idle_limit_seconds: float = 1.0,
    playback_speed: float = 1.5,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[dict[str, Any], Path]:
    """Publish a safe cast and optional renders, always writing a manifest."""
    if not math.isfinite(idle_limit_seconds) or idle_limit_seconds <= 0:
        raise MediaError("media idle limit must be positive", code=2)
    if not math.isfinite(playback_speed) or playback_speed <= 0:
        raise MediaError("media playback speed must be positive", code=2)
    raw_cast = evidence_root / "raw" / RAW_CAST_NAME
    public_cast = evidence_root / "public" / PUBLIC_CAST_NAME
    try:
        raw_artifact = _artifact(evidence_root, raw_cast, public=False)
    except OSError as exc:
        raise MediaError(
            "raw recording is missing or unreadable",
            command_exit_code=command_exit_code,
        ) from exc
    manifest: dict[str, Any] = {
        "schema": MEDIA_SCHEMA,
        "status": "fail",
        "commandExitCode": command_exit_code,
        "terminal": {"columns": TERMINAL_COLUMNS, "rows": TERMINAL_ROWS},
        "rawCast": raw_artifact,
        "privacy": {"status": "pending", "rawPublic": False},
        "rendering": {
            "requested": "required" if require_render else "auto",
            "status": "pending",
            "idleTimeLimitSeconds": idle_limit_seconds,
            "playbackSpeed": playback_speed,
            "idleLimitAppliedOnlyDuringRendering": True,
            "tools": {
                "asciinema": True,
                "agg": tools.agg is not None,
                "ffmpeg": tools.ffmpeg is not None,
            },
        },
    }
    try:
        cast_info = sanitize_cast(raw_cast, public_cast, private_paths=private_paths)
        manifest["privacy"] = {"status": "pass", "rawPublic": False, "publicScan": "pass"}
        manifest["publicCast"] = {**_artifact(evidence_root, public_cast, public=True), **cast_info}
        if tools.render_available:
            gif, mp4 = _render(
                public_cast,
                evidence_root,
                tools=tools,
                idle_limit_seconds=idle_limit_seconds,
                playback_speed=playback_speed,
                run=run,
            )
            manifest["rendering"]["status"] = "pass"
            manifest["gif"] = _artifact(evidence_root, gif, public=True)
            manifest["mp4"] = _artifact(evidence_root, mp4, public=True)
        elif require_render:
            missing = [name for name, path in (("agg", tools.agg), ("ffmpeg", tools.ffmpeg)) if not path]
            raise MediaError(
                "required media render tools are unavailable: " + ", ".join(missing),
                code=MEDIA_TOOL_EXIT,
            )
        else:
            manifest["rendering"]["status"] = "skipped-tools-unavailable"
        manifest["status"] = "pass"
        return manifest, _write_media_manifest(evidence_root, manifest)
    except (MediaError, OSError) as caught:
        if isinstance(caught, MediaError):
            exc = caught
            if exc.command_exit_code is None:
                exc.command_exit_code = command_exit_code
        else:
            exc = MediaError(
                "media artifact publication failed",
                command_exit_code=command_exit_code,
            )
        manifest["status"] = "fail"
        manifest["failure"] = {"exitCode": exc.code, "message": str(exc)}
        if manifest["privacy"]["status"] == "pending":
            manifest["privacy"] = {"status": "fail", "rawPublic": False}
            manifest["rendering"]["status"] = "rejected-privacy"
        elif manifest["rendering"]["status"] == "pending":
            manifest["rendering"]["status"] = "fail"
        try:
            path = _write_media_manifest(evidence_root, manifest)
        except OSError as write_exc:
            raise MediaError(
                "media failure manifest could not be written",
                code=exc.code,
                command_exit_code=command_exit_code,
            ) from write_exc
        exc.manifest = manifest
        exc.manifest_path = path
        raise exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal acceptance media helper")
    commands = parser.add_subparsers(dest="command", required=True)
    child = commands.add_parser("run-child")
    child.add_argument("status_path", type=Path)
    child.add_argument("child_command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = list(args.child_command)
    if command[:1] == ["--"]:
        command = command[1:]
    return run_child(command, args.status_path)


if __name__ == "__main__":
    raise SystemExit(main())
