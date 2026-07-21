"""Composition self-check for bonsai-notary.

Verifies the four pieces are wired: the sibling symlinks resolve, the on-chain orchestration imports
from its symlink, and the on-chain *seam* the launcher depends on still exists on the engine
(``run_bonsai_cli.WalletThirdEntryBackend``). The engine import needs numpy, so that check skips when
run under a bare interpreter.

Run with the engine venv:
  PYTHONPATH=engine/bonsai/src:bsv_third_entry <engine>/bonsai/.venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_sibling_symlinks_resolve():
    # Each reference must point at the sibling's SOURCE tree. NB: chain_c binaries
    # build OUT of the checkout (into $BONSAI_NOTARY_HOME/chain_c/build), so the
    # wiring marker here is the build script in source, not a built binary.
    for name, must_contain in (("engine", "bonsai/src/trinote"),
                               ("chain_c", "build_chain_c.sh"),
                               ("bsv_third_entry", "bsv_third_entry/engine_run.py")):
        link = ROOT / name
        assert link.is_symlink() or link.is_dir(), f"{name} reference missing"
        assert (link / must_contain).exists(), f"{name} does not contain {must_contain}"


def test_chain_c_binary_builds_out_of_source():
    """chain_c CLIs build into $BONSAI_NOTARY_HOME/chain_c/build (out of the source
    tree), per build_chain_c.sh + bsv_third_entry.paths.chain_c_build_dir(). Guard the
    relocation: the resolved binary path must NOT live under the chain_c checkout."""
    sys.path.insert(0, str(ROOT / "bsv_third_entry"))
    from bsv_third_entry import paths

    binary = paths.chain_c_bin("bonsai_third_entry")
    chain_c_src = (ROOT / "chain_c").resolve()
    assert not str(binary.resolve()).startswith(str(chain_c_src)), \
        f"chain_c binary resolved into the source tree: {binary} (build must be out-of-source)"
    if not binary.exists():
        pytest.skip(f"chain_c not built yet at {binary}; run chain_c/build_chain_c.sh --test")
    assert binary.is_file(), f"{binary} exists but is not a file"


def test_bsv_third_entry_imports_from_symlink():
    sys.path.insert(0, str(ROOT / "bsv_third_entry"))
    from bsv_third_entry.chain_backends import ChainCThirdEntryBackend
    from bsv_third_entry import engine_run  # noqa: F401

    # drop-in shape: accepts the wallet backend kwargs the engine passes for --onchain
    be = ChainCThirdEntryBackend(source_index=23, sat_per_kb=100, confirm=False)
    assert be.confirm is False


def test_engine_onchain_seam_present():
    """The launcher rebinds run_bonsai_cli.WalletThirdEntryBackend; it must still exist."""
    sys.path.insert(0, str(ROOT / "engine" / "bonsai" / "src"))
    try:
        import trinote.cli.run_bonsai_cli as rbc
    except Exception as exc:  # numpy/engine not installed in this interpreter
        pytest.skip(f"engine not importable here: {exc}")
    assert hasattr(rbc, "WalletThirdEntryBackend"), \
        "engine no longer exposes the WalletThirdEntryBackend seam — update bsv_third_entry.engine_run"


def _dryrun(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(BONSAI_DRYRUN="1", BONSAI_GPU="0")
    return subprocess.run(
        [str(ROOT / "bonsai-notary"), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_27b_receipt_profile_wires_qwen35_artifact_and_fresh_oracle():
    result = _dryrun(
        "how many r's are in strawberry?", "--model", "27b", "--receipts", "-n", "384"
    )
    assert result.returncode == 0, result.stderr
    command = result.stdout
    assert "Bonsai-27B-Q1_0.gguf" in command
    assert "Bonsai-27B-Q1_0-int-qwen35.safetensors" in command
    assert "atlas-notarized-bonsai-27b.identity.json" in command
    assert "--verify-mode fresh-oracle" in command
    assert "--sampler bonsai27-rec" in command
    assert "--no-repeat-ngram 4" in command
    assert "--max-new 1024" in command
    assert "--receipt" in command
    assert "-n 384" in command


def test_setup_role_keys_bind_receipt_signers_to_agent_identity(tmp_path):
    role_dir = tmp_path / "agent" / "keys"
    role_dir.mkdir(parents=True)
    for role in ("elder", "agent", "counterparty"):
        (role_dir / f"{role}.key.json").write_text("{}\n")

    env = os.environ.copy()
    env.update(BONSAI_DRYRUN="1", BONSAI_GPU="0", BONSAI_NOTARY_HOME=str(tmp_path))
    result = subprocess.run(
        [str(ROOT / "bonsai-notary"), "hello", "--model", "27b", "--receipts"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"--model-key {role_dir / 'agent.key.json'}" in result.stdout
    assert f"--counterparty-key {role_dir / 'counterparty.key.json'}" in result.stdout


def test_27b_nonreceipt_profile_does_not_load_fresh_oracle():
    result = _dryrun("hello", "--model=27b")
    assert result.returncode == 0, result.stderr
    assert "Bonsai-27B-Q1_0-int-qwen35.safetensors" in result.stdout
    assert "--verify-mode fresh-oracle" not in result.stdout
    assert "--max-new 1024" in result.stdout


def test_27b_repl_selects_contextual_chat_front_door():
    result = _dryrun(
        "repl", "--model", "27b", "--no-receipt", "--context-size", "4096"
    )
    assert result.returncode == 0, result.stderr
    command = result.stdout
    assert "--repl" in command
    assert "--chat" in command
    assert "--context-size 4096" in command


def test_unknown_model_fails_before_inference():
    result = _dryrun("hello", "--model", "99b")
    assert result.returncode == 2
    assert "unknown model" in result.stderr


def test_options_before_prompt_require_explicit_prompt_and_preserve_values():
    ambiguous = _dryrun("-n", "1", "hello world")
    assert ambiguous.returncode == 2
    assert "explicit --prompt PROMPT" in ambiguous.stderr

    result = _dryrun(
        "--model", "27b", "--context-size", "4096", "--seed", "42",
        "--prompt", "hello world", "-n", "1",
    )
    assert result.returncode == 0, result.stderr
    command = result.stdout
    assert "-p hello\\ world" in command
    assert "--context-size 4096" in command
    assert "--seed 42" in command
    assert "-n 1" in command


def test_duplicate_prompt_is_rejected():
    result = _dryrun("first", "--prompt", "second")
    assert result.returncode == 2
    assert "PROMPT supplied more than once" in result.stderr


@pytest.mark.parametrize("args", [("",), ("--prompt", ""), ("--prompt=",)])
def test_empty_prompt_is_rejected(args):
    result = _dryrun(*args)
    assert result.returncode == 2
    assert "non-empty PROMPT" in result.stderr
