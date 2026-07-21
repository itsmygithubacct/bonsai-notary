"""Offline contract tests for immutable dependency bootstrapping."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True,
    )


def _remote_with_two_commits(base: Path, name: str) -> tuple[Path, str, str]:
    remote = base / "remotes" / f"{name}.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    work = base / "sources" / name
    work.mkdir(parents=True)
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.name", "bootstrap test", cwd=work)
    _git("config", "user.email", "bootstrap@example.invalid", cwd=work)
    marker = work / "revision.txt"
    marker.write_text("pinned\n")
    _git("add", "revision.txt", cwd=work)
    _git("commit", "-m", "pinned", cwd=work)
    pinned = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    marker.write_text("newer-unlocked\n")
    _git("commit", "-am", "newer", cwd=work)
    latest = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    )
    return remote, pinned, latest


def test_bootstrap_checks_out_locked_commits_and_rejects_dirty_trees(tmp_path):
    notary = tmp_path / "notary"
    scripts = notary / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "bootstrap-deps.sh", scripts / "bootstrap-deps.sh")

    revisions = {}
    for name in ("integer_inference_engine", "chain_c", "bsv_third_entry"):
        _remote, pinned, latest = _remote_with_two_commits(tmp_path, name)
        assert pinned != latest
        revisions[name] = pinned
    (notary / "dependencies.lock").write_text(
        "".join(f"{name} {revision}\n" for name, revision in revisions.items())
    )

    deps = tmp_path / "checkouts"
    # Simulate interruption after git init/remote setup but before a first
    # checkout. Bootstrap must complete this clean, unborn repository.
    interrupted = deps / "integer_inference_engine"
    interrupted.mkdir(parents=True)
    _git("init", cwd=interrupted)
    _git("remote", "add", "origin", str(tmp_path / "remotes" / "integer_inference_engine.git"),
         cwd=interrupted)
    env = os.environ.copy()
    env.update(
        BONSAI_DEPS_BASE=str(tmp_path / "remotes"),
        BONSAI_DEPS_DIR=str(deps),
    )
    result = subprocess.run(
        [str(scripts / "bootstrap-deps.sh")], cwd=notary, env=env,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    for name, revision in revisions.items():
        assert _git("rev-parse", "HEAD", cwd=deps / name).stdout.strip() == revision
        assert (deps / name / "revision.txt").read_text() == "pinned\n"
    assert (notary / "engine").resolve() == (deps / "integer_inference_engine").resolve()

    # Never let ln turn an unexpected real directory into a nested link.
    (notary / "engine").unlink()
    (notary / "engine").mkdir()
    rejected_link = subprocess.run(
        [str(scripts / "bootstrap-deps.sh")], cwd=notary, env=env,
        text=True, capture_output=True, check=False,
    )
    assert rejected_link.returncode == 2
    assert "is not a symlink" in rejected_link.stderr
    (notary / "engine").rmdir()
    restored = subprocess.run(
        [str(scripts / "bootstrap-deps.sh")], cwd=notary, env=env,
        text=True, capture_output=True, check=False,
    )
    assert restored.returncode == 0, restored.stderr

    (deps / "chain_c" / "revision.txt").write_text("dirty\n")
    rejected = subprocess.run(
        [str(scripts / "bootstrap-deps.sh")], cwd=notary, env=env,
        text=True, capture_output=True, check=False,
    )
    assert rejected.returncode == 2
    assert "uncommitted changes" in rejected.stderr
