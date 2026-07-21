"""Fast, non-mutating contract tests for the fresh-host Bonsai-27B setup script."""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts" / "setup-bonsai-27b.sh"
TOKENIZER_SETUP = ROOT / "scripts" / "install-llama-tokenizer.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SETUP), *args], cwd=ROOT, text=True, capture_output=True, check=False
    )


def test_setup_script_is_valid_bash():
    for script in (SETUP, TOKENIZER_SETUP):
        result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr


def test_wallet_dependency_pins_are_resolver_compatible():
    requirements = (ROOT / "requirements_wallet.txt").read_text()
    assert "bsv-sdk==2.2.0" in requirements
    assert "requests==2.34.2" in requirements
    assert "requests==2.32.5" not in requirements


def test_setup_help_documents_keys_funding_and_broadcast_interlock():
    result = _run("--help")
    assert result.returncode == 0
    assert "import-mnemonic" in result.stdout
    assert "--public-third-entry" in result.stdout
    assert "--funding-check-only" in result.stdout
    assert "--deploy-agent" in result.stdout and "--confirm-mainnet" in result.stdout
    assert "--python VERSION" in result.stdout
    assert "minimum: 3.11" in result.stdout


def test_cpu_tokenizer_plan_is_pinned_and_uses_engine_default_path(tmp_path):
    result = subprocess.run(
        [str(TOKENIZER_SETUP), "--dry-run"],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "62061f91088281e65071cc38c5f69ee95c39f14e" in result.stdout
    assert str(tmp_path / ".local/trinote/vendor/llama.cpp/build/bin/llama-tokenize") in result.stdout


def test_setup_dry_run_resolves_complete_local_plan_without_writes(tmp_path):
    state = tmp_path / "state-that-must-not-exist"
    result = _run(
        "--dry-run", "--yes", "--key-mode", "generate", "--local-only",
        "--notary-home", str(state),
    )
    assert result.returncode == 0, result.stderr
    assert "Resolved setup plan" in result.stdout
    assert "keys: generate" in result.stdout
    assert "Third Entry: local" in result.stdout
    assert "download+checksum" in result.stdout
    assert "Python: 3.12 (uv-managed; downloaded if absent)" in result.stdout
    assert "blockchain broadcast: none" in result.stdout
    assert not state.exists(), "--dry-run must not create the state home"


def test_setup_uses_supported_managed_python_and_preserves_bad_venvs():
    script = SETUP.read_text()
    assert 'python_spec="${BONSAI_PYTHON_VERSION:-3.12}"' in script
    assert 'venv --managed-python --python "$python_spec"' in script
    assert "sys.version_info >= (3, 11)" in script
    assert 'backup_engine_venv unsupported' in script


def test_mainnet_confirmation_cannot_be_supplied_without_deployment():
    result = _run(
        "--dry-run", "--yes", "--key-mode", "generate", "--public-third-entry",
        "--confirm-mainnet",
    )
    assert result.returncode == 2
    assert "--confirm-mainnet requires --deploy-agent" in result.stderr
