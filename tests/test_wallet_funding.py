"""Offline unit tests for the bonsai-notary self-managed wallet (wallet/notary_wallet.py).

These NEVER touch the network: an autouse fixture poisons the low-level WoC HTTP entrypoint
(``_woc_request``) so any accidental request fails loudly, and every test that needs chain data
monkeypatches the higher-level helpers (``woc_bulk_balance`` / ``woc_get``) directly.

Coverage:
  * fund_key UTXO/address selection — confirmed-first, sufficiency (skips too-small/dust),
    change-path first, smallest-covering.
  * next_change always hands back a FRESH unused change index (never an already-issued one or a role).
  * secret files (mnemonic + WIF keyfiles) land at 0600 and keys/ at 0700 — no world-readable window.
  * no WIF / mnemonic ever appears in the stdout/stderr of the address-printing CLI commands
    (guards the bsv-sdk ``PrivateKey.__repr__``-returns-WIF landmine).

Run from the bonsai-notary checkout with its venv:
  .venv/bin/python -m pytest tests/test_wallet_funding.py -q
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

# Load wallet/notary_wallet.py directly (its only deps — requests, bsv — are installed in the venv).
_WALLET_PY = Path(__file__).resolve().parents[1] / "wallet" / "notary_wallet.py"
_spec = importlib.util.spec_from_file_location("notary_wallet", _WALLET_PY)
nw = importlib.util.module_from_spec(_spec)
sys.modules["notary_wallet"] = nw
_spec.loader.exec_module(nw)


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hard offline guard: any code path that reaches the real WoC HTTP layer fails immediately."""
    def _boom(*a, **k):
        raise RuntimeError("network access attempted in an offline test")
    monkeypatch.setattr(nw, "_woc_request", _boom)


@pytest.fixture
def wallet_home(tmp_path, monkeypatch):
    """Point the wallet's secret paths at an isolated tmp home and seed it with a real (offline) mnemonic.
    All wallet functions resolve these module globals at call time, so patching them fully redirects I/O."""
    secrets = tmp_path / "trinote" / "wallet"
    keys = secrets / "keys"
    monkeypatch.setattr(nw, "BONSAI_NOTARY_HOME", tmp_path / "trinote")
    monkeypatch.setattr(nw, "SECRETS", secrets)
    monkeypatch.setattr(nw, "MNEMONIC_FILE", secrets / "master_mnemonic.txt")
    monkeypatch.setattr(nw, "KEYS_DIR", keys)
    monkeypatch.setattr(nw, "CHANGE_FLOOR_FILE", secrets / "change_floor.txt")
    monkeypatch.setattr(nw, "CHANGE_LOCK_FILE", secrets / ".change.lock")
    nw.gen_mnemonic(force=True)                 # 12-word BIP39 mnemonic, generated locally (no network)
    return secrets


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────
def _addr(c, i):
    return nw._key(c, i).address()


def _balmap(entries):
    """entries: {(change, index): (confirmed, unconfirmed)} → {address: {confirmed, unconfirmed}}."""
    return {_addr(c, i): {"confirmed": cf, "unconfirmed": un} for (c, i), (cf, un) in entries.items()}


def _patch_balances(monkeypatch, balmap):
    monkeypatch.setattr(nw, "woc_bulk_balance",
                        lambda addrs: {a: balmap.get(a, {"confirmed": 0, "unconfirmed": 0}) for a in addrs})


def _all_wifs():
    """Every WIF the test home could plausibly expose (the roles + a sweep of both paths)."""
    wifs = set()
    for c in (0, 1):
        for i in range(8):
            wifs.add(nw._key(c, i).wif())
    return wifs


# ── fund_key selection ────────────────────────────────────────────────────────────────────────────────
def test_fund_key_prefers_change_path_then_smallest_covering(wallet_home, monkeypatch):
    _patch_balances(monkeypatch, _balmap({
        (0, 0): (1_000_000, 0),   # Elder receive — big, must NOT be cracked while change covers the need
        (0, 3): (13_000, 0),      # receive, covers
        (1, 5): (15_000, 0),      # change, covers
        (1, 7): (12_000, 0),      # change, smallest covering → the winner
        (1, 2): (100, 0),         # dust, far below need → skipped
    }))
    c, i, addr, sats = nw.fund_key(12_000, scan=24)
    assert (c, i) == (1, 7), "should prefer the change path, then the smallest covering UTXO"
    assert addr == _addr(1, 7)
    assert sats == 12_000


def test_fund_key_smallest_covering_on_receive_path(wallet_home, monkeypatch):
    # No change-path funds: the smallest covering RECEIVE address wins, not the giant Elder UTXO.
    _patch_balances(monkeypatch, _balmap({
        (0, 0): (1_000_000, 0),
        (0, 3): (12_500, 0),
    }))
    c, i, addr, sats = nw.fund_key(12_000, scan=24)
    assert (c, i) == (0, 3)
    assert sats == 12_500


def test_fund_key_is_confirmed_first(wallet_home, monkeypatch):
    # An address with only UNconfirmed balance is invisible by default, and only counts with the opt-in flag.
    _patch_balances(monkeypatch, _balmap({(1, 4): (0, 50_000)}))
    with pytest.raises(SystemExit):
        nw.fund_key(12_000, scan=24, allow_unconfirmed=False)
    c, i, addr, sats = nw.fund_key(12_000, scan=24, allow_unconfirmed=True)
    assert (c, i) == (1, 4)
    assert sats == 50_000


def test_fund_key_skips_insufficient_and_dust(wallet_home, monkeypatch):
    # Every candidate is below the need (incl. a dust output) → no spendable address → fail closed.
    _patch_balances(monkeypatch, _balmap({
        (0, 0): (100, 0),         # dust
        (0, 3): (5_000, 0),       # too small
        (1, 1): (11_999, 0),      # 1 sat short — still rejected
    }))
    with pytest.raises(SystemExit):
        nw.fund_key(12_000, scan=24)


# ── next_change freshness / no-reuse ────────────────────────────────────────────────────────────────────
def test_next_change_skips_issued_indices(wallet_home, monkeypatch):
    monkeypatch.setattr(nw, "_issued_change_indices", lambda: {0, 1, 2})
    i, addr = nw.next_change()
    assert i == 3
    assert addr == _addr(1, 3)


def test_next_change_does_not_collide_with_an_issued_keyfile(wallet_home):
    # Real reservation path: writing a keyfile for change index 0 must push next_change to index 1.
    nw.keyfile(1, 0, label="change")
    i, addr = nw.next_change()
    assert i == 1
    assert addr == _addr(1, 1)


def test_next_change_verify_skips_onchain_used_addresses(wallet_home, monkeypatch):
    monkeypatch.setattr(nw, "_issued_change_indices", lambda: set())
    used = {_addr(1, 0), _addr(1, 1)}

    def fake_history(path):
        addr = path.split("/")[2]                 # /address/<addr>/history
        return [{"tx_hash": "deadbeef"}] if addr in used else []

    monkeypatch.setattr(nw, "woc_get", fake_history)
    i, addr = nw.next_change(verify=True)
    assert i == 2
    assert addr == _addr(1, 2)


def test_next_change_cli_returns_a_fresh_index_each_call(wallet_home, capsys):
    # The serialized CLI path reserves each index (writes its keyfile), so back-to-back calls never repeat
    # — exactly what the TOCTOU lock guarantees for two concurrent confirmed broadcasts.
    assert nw.main(["next-change"]) == 0
    addr1 = capsys.readouterr().out.strip()
    assert nw.main(["next-change"]) == 0
    addr2 = capsys.readouterr().out.strip()

    assert addr1 != addr2, "second next-change must hand back a different (fresh) change address"
    assert addr1 == _addr(1, 0) and addr2 == _addr(1, 1)
    assert (nw.KEYS_DIR / f"{addr1}.json").exists() and (nw.KEYS_DIR / f"{addr2}.json").exists()

    # never collides with a role address (Elder / Agent / Counterparty)
    role_addrs = {_addr(c, i) for (c, i) in nw.ROLES.values()}
    assert addr1 not in role_addrs and addr2 not in role_addrs


# ── secret-file permissions (no world-readable window) ──────────────────────────────────────────────────
def test_secret_files_are_0600_and_keysdir_0700(wallet_home):
    assert stat.S_IMODE(nw.MNEMONIC_FILE.stat().st_mode) == 0o600
    p = nw.keyfile(1, 0)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600, "WIF keyfile must be owner-only"
    assert stat.S_IMODE(nw.KEYS_DIR.stat().st_mode) == 0o700, "keys/ must be owner-only"
    # rewriting an existing keyfile keeps it 0600 (overwrite-safe, still not world-readable)
    p2 = nw.keyfile(1, 0)
    assert p2 == p and stat.S_IMODE(p2.stat().st_mode) == 0o600


# ── WIF / mnemonic must never reach stdout or stderr ────────────────────────────────────────────────────
def test_privatekey_repr_contains_wif_is_a_real_landmine():
    # Documents WHY the leak tests below matter: the SDK's repr embeds the WIF, so printing a PrivateKey
    # (or anything that str()s one) would leak the secret. We must only ever print addresses.
    pk = nw.PrivateKey()
    assert pk.wif() in repr(pk)


def test_address_cli_leaks_no_secret(wallet_home, capsys):
    mnemonic = nw.load_mnemonic()
    assert nw.main(["address", "--role", "elder"]) == 0
    out = capsys.readouterr()
    blob = out.out + out.err
    assert mnemonic not in blob
    for wif in _all_wifs():
        assert wif not in blob


def test_next_change_cli_leaks_no_secret(wallet_home, capsys):
    mnemonic = nw.load_mnemonic()
    assert nw.main(["next-change"]) == 0
    out = capsys.readouterr()
    blob = out.out + out.err
    assert mnemonic not in blob
    for wif in _all_wifs():
        assert wif not in blob


def test_fund_key_cli_leaks_no_secret(wallet_home, capsys, monkeypatch):
    _patch_balances(monkeypatch, _balmap({(1, 0): (20_000, 0)}))
    assert nw.main(["fund-key", "--need", "12000", "--json"]) == 0
    out = capsys.readouterr()
    blob = out.out + out.err
    for wif in _all_wifs():                        # fund-key writes a keyfile but must print only path/address
        assert wif not in blob


def test_gen_mnemonic_cli_leaks_no_secret(wallet_home, capsys):
    assert nw.main(["gen-mnemonic", "--force"]) == 0
    out = capsys.readouterr()
    blob = out.out + out.err
    mnemonic = nw.load_mnemonic()                  # the freshly (re)generated seed
    assert mnemonic not in blob
    for wif in _all_wifs():
        assert wif not in blob


def test_import_mnemonic_is_validated_idempotent_and_secret_safe(wallet_home, tmp_path, capsys):
    mnemonic = nw.load_mnemonic()
    source = tmp_path / "seed.txt"
    source.write_text(mnemonic + "\n", encoding="utf-8")

    # Importing the same seed is idempotent and the CLI never echoes it.
    assert nw.main(["import-mnemonic", "--file", str(source)]) == 0
    out = capsys.readouterr()
    assert mnemonic not in out.out + out.err
    assert nw.load_mnemonic() == mnemonic

    with pytest.raises(SystemExit, match="BIP39"):
        nw.import_mnemonic("this is not a valid mnemonic")


def test_validate_keyfile_prints_public_metadata_only(wallet_home, capsys):
    path = nw.keyfile(*nw.ROLES["agent"], label="agent")
    wif = nw.load_keyfile(str(path)).wif()
    assert nw.main(["validate-keyfile", "--path", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["address"] == nw._key(*nw.ROLES["agent"]).address()
    assert payload["publicKeyHex"]
    assert wif not in json.dumps(payload)


def test_funding_status_cli_exits_three_and_names_deposit_address(wallet_home, monkeypatch, capsys):
    _patch_balances(monkeypatch, _balmap({}))
    assert nw.main(["funding-status", "--need", "12000"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "allowUnconfirmed": False,
        "depositAddress": _addr(*nw.ROLES["elder"]),
        "funded": False,
        "needSatoshis": 12000,
        "reason": "no wallet-derived address with ≥ 12000 sats (allow_unconfirmed=False); fund one or fan-out first",
    }


def test_funding_status_cli_reports_covering_wallet_address(wallet_home, monkeypatch, capsys):
    _patch_balances(monkeypatch, _balmap({(1, 4): (0, 15000)}))
    assert nw.main(["funding-status", "--need", "12000", "--allow-unconfirmed"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["funded"] is True
    assert payload["address"] == _addr(1, 4)
    assert payload["satoshis"] == 15000
