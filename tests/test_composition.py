"""Composition self-check for bonsai-notary.

Verifies the four pieces are wired: the sibling symlinks resolve, the on-chain orchestration imports
from its symlink, and the on-chain *seam* the launcher depends on still exists on the engine
(``run_bonsai_cli.WalletThirdEntryBackend``). The engine import needs numpy, so that check skips when
run under a bare interpreter.

Run with the engine venv:
  PYTHONPATH=engine/bonsai/src:bsv_third_entry <engine>/bonsai/.venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

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
