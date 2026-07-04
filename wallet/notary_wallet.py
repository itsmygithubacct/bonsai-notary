#!/usr/bin/env python3
"""bonsai-notary self-managed BSV HD wallet — generate its OWN mnemonic, derive its OWN keys, and fan a
funding UTXO out into several fresh UTXOs at its OWN derived addresses (so the on-chain notary lifecycle
never waits on chained-change confirmations, and never reuses one key for change).

Ported from the poorwallet_41 tooling onto the same SDK it vendors (`bsv-sdk`), managed via uv only.

  BIP44 path  m/44'/236'/0'           (236 = BSV)
  receive     m/44'/236'/0'/0/<index>   (Elder=0, Agent=1, Counterparty=2, fan-out targets=10..)
  change      m/44'/236'/0'/1/<index>

The master mnemonic is the wallet's root secret: stored 0600 at $BONSAI_NOTARY_HOME/wallet/master_mnemonic.txt
(default ~/.local/trinote, OUTSIDE the repo), never printed. WIFs are written only to that home's keys/ on request.

Commands:
  gen-mnemonic                      generate + persist the master mnemonic (idempotent; --force to replace)
  address --role|--change/--index   print a derived address (role: elder|agent|counterparty)
  keyfile --role|--change/--index    write {address,wif,...} JSON to $BONSAI_NOTARY_HOME/wallet/keys/ (chain layer)
  balance  --address A              WhatsOnChain confirmed/unconfirmed balance
  utxos    --address A              list unspent outputs
  fanout   ...                      build (and optionally broadcast) the funding fan-out tx
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from bsv.hd.bip39 import mnemonic_from_entropy, validate_mnemonic
from bsv.hd.bip44 import bip44_derive_xprv_from_mnemonic
from bsv.keys import PrivateKey
from bsv.transaction import Transaction
from bsv.transaction_input import TransactionInput
from bsv.transaction_output import TransactionOutput
from bsv.script.type import P2PKH, OpReturn

BIP44_ACCOUNT = "m/44'/236'/0'"          # BSV, account 0
WOC = "https://api.whatsonchain.com/v1/bsv/main"

# ── WoC HTTP layer (rate-limit hardened; mirrors poorwallet_41's retry + api-key handling) ────────────
# WoC's free tier is ~3 req/s and returns HTTP 429 when exceeded. Two levers, both from poorwallet:
#   1. an API key (WOC_API_KEY env → `woc-api-key` header) lifts the ceiling far above the free tier;
#   2. retry the SAFE reads with exponential backoff + jitter, honoring Retry-After, on 429/5xx/conn errors.
# A broadcast is deliberately NOT retried on a response/timeout (an ambiguous failure may mean the tx already
# reached the network — re-posting risks a double-spend), exactly the caveat poorwallet's retry.py calls out.
_WOC_RETRY_STATUS = {429, 500, 502, 503, 504}
_WOC_SESSION: requests.Session | None = None


def _woc_session() -> requests.Session:
    global _WOC_SESSION
    if _WOC_SESSION is None:
        _WOC_SESSION = requests.Session()
    return _WOC_SESSION


def _woc_headers(extra: dict | None = None) -> dict:
    h = dict(extra or {})
    key = os.environ.get("WOC_API_KEY")
    if key:
        h["woc-api-key"] = key
    return h


def _woc_request(method: str, url: str, *, retry: bool = True, max_retries: int = 4,
                 base_delay: float = 0.6, **kw) -> requests.Response:
    """One WoC HTTP call with exponential backoff + jitter on 429/5xx/connection errors (reads only).
    Pass retry=False for broadcasts — they must be single-shot (re-posting an ambiguous send can double-spend)."""
    kw.setdefault("timeout", 25)
    kw["headers"] = _woc_headers(kw.pop("headers", None))
    sess = _woc_session()
    r = None
    for attempt in range(max_retries + 1):
        err = None
        try:
            r = sess.request(method, url, **kw)
            if not retry or r.status_code not in _WOC_RETRY_STATUS:
                return r
            retry_after = r.headers.get("Retry-After")
        except (requests.ConnectionError, requests.Timeout) as e:
            if not retry:
                raise
            err, retry_after = e, None
        if attempt >= max_retries:
            if err is not None:
                raise err
            return r            # exhausted on a 429/5xx — hand back so the caller raises a clear error
        if retry_after and str(retry_after).isdigit():
            delay = float(retry_after)
        else:
            delay = min(base_delay * (2 ** attempt), 20.0)
        time.sleep(delay + random.uniform(0, delay * 0.25))   # jitter to avoid thundering-herd retries
    return r  # pragma: no cover
# Base58Check-looking mainnet P2PKH/P2SH address (versions 1/3). Reject anything else BEFORE it is
# interpolated into a WhatsOnChain URL — the host is fixed, so this is input hardening, not an SSRF fix.
_ADDR_RE = re.compile(r"^[13][1-9A-HJ-NP-Za-km-z]{25,34}$")
# A commitment hash written into the Third Entry OP_RETURN MUST be exactly 32 bytes (64 hex chars). Anchored
# so a 62-/66-char value can't be silently committed as a wrong-length, permanently-unverifiable OP_RETURN.
_HASH32_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Secrets live OUTSIDE the repo: under $BONSAI_NOTARY_HOME (default ~/.local/trinote), so the public tree
# never holds the wallet seed or any key. Override with the BONSAI_NOTARY_HOME env var.
BONSAI_NOTARY_HOME = Path(os.environ.get("BONSAI_NOTARY_HOME") or (Path.home() / ".local" / "trinote"))
SECRETS = BONSAI_NOTARY_HOME / "wallet"
MNEMONIC_FILE = SECRETS / "master_mnemonic.txt"
KEYS_DIR = SECRETS / "keys"
ROLES = {"elder": (0, 0), "agent": (0, 1), "counterparty": (0, 2)}   # receive-path role → (change, index)


def _write_secret(path: Path, text: str) -> None:
    """Write a secret (mnemonic / WIF keyfile) so it is NEVER world-readable, even momentarily. Plain
    Path.write_text() creates the file at the umask default (typically 0644) and only a *later* os.chmod
    narrows it — a window in which the seed/WIF is world-readable. Instead create+truncate the file in one
    os.open at mode 0600 (umask can only drop bits, so 0600 is the ceiling), then write. Overwrite-safe
    (no O_EXCL): keyfiles are legitimately rewritten. A trailing chmod re-pins 0600 in case the file
    pre-existed at a looser mode (O_CREAT honors the mode only when it actually creates the file)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


# ── mnemonic / derivation ─────────────────────────────────────────────────────────────────────────────
def gen_mnemonic(force: bool = False) -> str:
    SECRETS.mkdir(parents=True, exist_ok=True)
    if MNEMONIC_FILE.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing mnemonic at {MNEMONIC_FILE} (use --force)")
    m = mnemonic_from_entropy()                      # 128-bit entropy → 12 words (BIP39)
    _write_secret(MNEMONIC_FILE, m + "\n")           # atomic 0600 — never world-readable, even briefly
    try:
        os.chmod(SECRETS, 0o700)
    except OSError:
        pass
    return m


def load_mnemonic() -> str:
    if not MNEMONIC_FILE.exists():
        raise SystemExit(f"no mnemonic at {MNEMONIC_FILE} — run `gen-mnemonic` first")
    m = MNEMONIC_FILE.read_text(encoding="utf-8").strip()
    try:
        validate_mnemonic(m)            # returns None on success, raises on a bad checksum/wordlist
    except Exception as e:
        raise SystemExit(f"stored mnemonic failed BIP39 validation: {e}")
    return m


def _key(change: int, index: int) -> PrivateKey:
    xprv = bip44_derive_xprv_from_mnemonic(load_mnemonic(), path=BIP44_ACCOUNT)
    return xprv.ckd(change).ckd(index).private_key()


def _resolve(role: str | None, change: int, index: int) -> tuple[int, int]:
    if role:
        if role not in ROLES:
            raise SystemExit(f"unknown role {role!r} (one of {sorted(ROLES)})")
        return ROLES[role]
    return change, index


def keyfile(change: int, index: int, label: str = "") -> Path:
    """Write a {address, wif, ...} JSON the chain layer can load as a signing key. Secret — gitignored."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(KEYS_DIR, 0o700)                     # keys/ holds WIFs — owner-only, like SECRETS itself
    except OSError:
        pass
    pk = _key(change, index)
    addr = pk.address()
    data = {"address": addr, "wif": pk.wif(), "publicKeyHex": pk.public_key().hex(),
            "derivationPath": f"{BIP44_ACCOUNT}/{change}/{index}", "change": change, "index": index, "label": label}
    p = KEYS_DIR / f"{addr}.json"
    _write_secret(p, json.dumps(data, indent=2, sort_keys=True) + "\n")   # atomic 0600 (holds the WIF)
    return p


# ── fresh change-address rotation (blockchain hygiene) ───────────────────────────────────────────────
# Mirrors poorwallet_41: every change output lands on a brand-new derived key instead of reusing one
# address. We walk the change path m/44'/236'/0'/1/<index> and take the first UNUSED address — one the
# wallet can always re-derive (its WIF is written to KEYS_DIR), so the change is never stranded.
CHANGE_FLOOR_FILE = SECRETS / "change_floor.txt"
CHANGE_LOCK_FILE = SECRETS / ".change.lock"


@contextlib.contextmanager
def _change_lock():
    """Serialize the next-change critical section (select index → reserve its keyfile → bump the floor)
    across processes. Without it, two concurrent confirmed broadcasts can both read the same issued-index
    set, both pick index i, and both route change to the SAME address — change-address reuse. An exclusive
    fcntl.flock on a lock file makes the select+reserve atomic: the second caller blocks until the first has
    written the keyfile for i, so it then sees i as issued and picks i+1. flock is advisory but auto-released
    on close/process-death (no stale-lock wedge), and a single process simply takes the lock uncontended —
    behavior identical to before."""
    SECRETS.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CHANGE_LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                                   # closing the fd releases the flock


def _issued_change_indices() -> set[int]:
    """Change-path indices already handed out (one keyfile per issued change address). Local and
    authoritative — re-derivable from KEYS_DIR even if the floor hint is lost — and needs no network."""
    used: set[int] = set()
    if KEYS_DIR.exists():
        for p in KEYS_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text())
                if d.get("change") == 1 and isinstance(d.get("index"), int):
                    used.add(d["index"])
            except Exception:
                pass
    return used


def _address_unused(addr: str) -> bool:
    """On-chain freshness check (opt-in --verify): unused iff it has NO history.

    A lookup FAILURE PROPAGATES rather than being swallowed as False (review finding #19): the old
    'conservative on error -> False' made next_change()'s loop treat every address as 'used' when
    WhatsOnChain was unreachable, so it incremented forever (one retried WoC request per iteration)
    and never returned a change address. Letting the error surface fails loud instead of spinning."""
    return not woc_get(f"/address/{addr}/history")


def next_change(start: int | None = None, verify: bool = False) -> tuple[int, str]:
    """Next fresh change-path index — the lowest m/44'/236'/0'/1/<i> not already issued as a keyfile
    (and, with verify, also unused on-chain). Each change output thus lands on a brand-new key the
    wallet can always re-derive (its WIF is written), exactly like poorwallet_41's fresh-address rotation
    — no Elder/address reuse. Local-only by default (no per-tx WoC calls, so no rate-limit stalls)."""
    issued = _issued_change_indices()
    i = start if start is not None else 0
    if start is None:
        try:
            i = max(i, int(CHANGE_FLOOR_FILE.read_text().strip()))
        except Exception:
            pass
    while True:
        if i in issued or (verify and not _address_unused(_key(1, i).address())):
            i += 1
            continue
        return i, _key(1, i).address()


def woc_bulk_balance(addrs: list[str]) -> dict[str, dict]:
    """One WoC call per ≤20 addresses → {address: {confirmed, unconfirmed}}. Far fewer requests than
    per-address polling (the whole candidate set in ~1-3 calls instead of N)."""
    out: dict[str, dict] = {}
    for k in range(0, len(addrs), 20):                       # WoC caps /addresses/balance at 20/req
        chunk = addrs[k:k + 20]
        r = _woc_request("POST", f"{WOC}/addresses/balance", json={"addresses": chunk})
        r.raise_for_status()
        for row in r.json():
            out[row["address"]] = row.get("balance", {}) or {}
    return out


def _candidate_funding_keys(scan: int = 24) -> list[tuple[int, int]]:
    """Wallet-derived (change,index) pairs that might hold spendable funds: the named roles, every change
    address we've issued, and a sweep of the receive+change paths. All re-derivable from the mnemonic."""
    cands: set[tuple[int, int]] = set(ROLES.values())            # elder/agent/counterparty
    cands |= {(1, i) for i in _issued_change_indices()}
    for i in range(scan):
        cands.add((0, i)); cands.add((1, i))
    return sorted(cands)


def fund_key(need_sats: int, scan: int = 24, allow_unconfirmed: bool = False) -> tuple[int, int, str, int]:
    """Pick a wallet-OWNED address holding ≥ need_sats, so the chain layer funds from the wallet's own
    derived UTXOs instead of a hand-picked external keyfile. Prefers the CHANGE path (spend rolled change,
    keep funds moving on fresh keys) and the smallest covering UTXO (don't crack a big one for a small fee),
    so the Elder/receive funds are preserved until change is exhausted."""
    cands = _candidate_funding_keys(scan)
    addr_of = {ci: _key(*ci).address() for ci in cands}
    bals = woc_bulk_balance(list(addr_of.values()))

    def spendable(ci: tuple[int, int]) -> int:
        b = bals.get(addr_of[ci], {})
        return int(b.get("confirmed", 0)) + (int(b.get("unconfirmed", 0)) if allow_unconfirmed else 0)

    funded = [ci for ci in cands if spendable(ci) >= need_sats]
    if not funded:
        raise SystemExit(f"no wallet-derived address with ≥ {need_sats} sats "
                         f"(allow_unconfirmed={allow_unconfirmed}); fund one or fan-out first")
    funded.sort(key=lambda ci: (0 if ci[0] == 1 else 1, spendable(ci)))   # change-path first, smallest covering
    c, i = funded[0]
    return c, i, addr_of[(c, i)], spendable((c, i))


def load_keyfile(path: str) -> PrivateKey:
    """Load a {wif, address?} signing keyfile and return its PrivateKey. If the keyfile records an
    `address`, verify the WIF actually derives that address — so a swapped/corrupted keyfile can't
    silently spend from the wrong key (mirrors chain/scripts/cpfp.ts's WIF/address check)."""
    data = json.loads(Path(path).read_text())
    pk = PrivateKey(data["wif"])
    recorded = data.get("address")
    if recorded and pk.address() != recorded:
        raise SystemExit(f"keyfile {path}: WIF derives {pk.address()} but records address {recorded} — aborting")
    return pk


# ── WhatsOnChain (read-only queries + broadcast) ────────────────────────────────────────────────────────
def woc_get(path: str):
    r = _woc_request("GET", f"{WOC}{path}")
    r.raise_for_status()
    return r.json()


def _check_addr(addr: str) -> str:
    """Validate a Base58Check-looking BSV address before it is interpolated into a WoC URL."""
    if not _ADDR_RE.match(addr):
        raise SystemExit(f"refusing malformed address {addr!r} (expected Base58Check P2PKH/P2SH)")
    return addr


_TAG_MAX_BYTES = 64                       # sane cap on the on-chain OP_RETURN protocol tag


def _check_tag(tag: str) -> bytes:
    """Validate the OP_RETURN protocol tag (user input written on-chain): printable, no control chars,
    and ≤ _TAG_MAX_BYTES UTF-8 bytes. Keeps the automated `trinote/r1` path working."""
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in tag):
        raise SystemExit("refusing OP_RETURN tag with control/non-printable characters")
    raw = tag.encode("utf-8")
    if len(raw) > _TAG_MAX_BYTES:
        raise SystemExit(f"refusing OP_RETURN tag of {len(raw)} bytes (max {_TAG_MAX_BYTES})")
    return raw


def _hash32(name: str, value: str) -> bytes:
    """Validate a 32-byte commitment hash (model/receipt) BEFORE it reaches the chain, and return its 32 raw
    bytes. `bytes.fromhex` alone accepts ANY even-length hex, so a 62-/66-char value would be silently committed
    as a wrong-length, permanently-unverifiable OP_RETURN — this fails closed instead (mirrors `_check_addr`)."""
    if not _HASH32_RE.match(value):
        raise SystemExit(f"refusing {name} {value!r}: expected exactly 64 hex chars (32 bytes)")
    return bytes.fromhex(value)


def woc_balance(addr: str) -> dict:
    return woc_get(f"/address/{_check_addr(addr)}/balance")


def woc_utxos(addr: str) -> list[dict]:
    return woc_get(f"/address/{_check_addr(addr)}/unspent")


def woc_rawtx(txid: str) -> str:
    r = _woc_request("GET", f"{WOC}/tx/{txid}/hex")
    r.raise_for_status()
    return r.text.strip()


def woc_broadcast(raw_hex: str) -> str:
    # single-shot (retry=False): an ambiguous broadcast may already have reached the network, so
    # re-posting it (or rebuilding from the same UTXOs) risks a double-spend — never auto-retry this.
    r = _woc_request("POST", f"{WOC}/tx/raw", json={"txhex": raw_hex}, retry=False, timeout=40)
    if not r.ok:
        raise SystemExit(f"WhatsOnChain broadcast failed HTTP {r.status_code}: {r.text[:300]}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text.strip()


# ── fan-out ─────────────────────────────────────────────────────────────────────────────────────────────
DUST = 135                                              # below this a P2PKH change output isn't worth creating
_FEE_SAT_PER_KB = 100                                    # fee target in sat/KILOBYTE (= 0.1 sat/byte)


def _estimate_size(n_in: int, n_out: int) -> int:
    """UPPER bound on a signed P2PKH tx size: 10 overhead + 148/input (sig+pubkey+prevout) + 34/output.
    Using an upper bound makes fee/actual_size >= the target rate (i.e. AT/ABOVE the requested sat/byte)."""
    return 10 + 148 * n_in + 34 * n_out


def build_fanout(source_priv: PrivateKey, dest_amounts: list[tuple[str, int]], change_addr: str,
                 sat_per_kb: int = _FEE_SAT_PER_KB) -> tuple[Transaction, int, int]:
    """Spend the source key's confirmed UTXOs into `dest_amounts` [(address, sats), …] + a change output to
    `change_addr` (a DIFFERENT, derived address — never the spending key). The fee is computed EXACTLY as
    ceil(size_bytes × `sat_per_kb` / 1000) over an UPPER-bound size estimate (so the realized rate is AT/ABOVE
    the target sat/KB); we do NOT delegate to the SDK fee model (it overpaid). Returns (signed_tx, total_dest, fee)."""
    src_addr = source_priv.address()
    utxos = [u for u in woc_utxos(src_addr) if u.get("height", 0) > 0]      # confirmed only
    if not utxos:
        raise SystemExit(f"no confirmed UTXOs at source {src_addr}")
    need = sum(a for _, a in dest_amounts)
    utxos.sort(key=lambda u: -u["value"])
    chosen, have = [], 0
    max_fee = (_estimate_size(len(utxos), len(dest_amounts) + 1) * sat_per_kb + 999) // 1000
    for u in utxos:
        chosen.append(u); have += u["value"]
        if have >= need + max_fee:
            break
    n_out = len(dest_amounts) + 1
    fee = (_estimate_size(len(chosen), n_out) * sat_per_kb + 999) // 1000   # ceil; >= target sat/KB (upper-bound size)
    change_val = have - need - fee
    if change_val < 0:
        raise SystemExit(f"insufficient confirmed funds at {src_addr}: have {have}, need {need}+fee {fee}")
    tx_inputs = []
    for u in chosen:                                    # source_transaction lets the SDK read prev satoshis+script
        src_tx = Transaction.from_hex(woc_rawtx(u["tx_hash"]))
        tx_inputs.append(TransactionInput(source_transaction=src_tx, source_output_index=u["tx_pos"],
                                          unlocking_script_template=P2PKH().unlock(source_priv)))
    tx_outputs = [TransactionOutput(P2PKH().lock(addr), sats) for addr, sats in dest_amounts]
    if change_val >= DUST:
        tx_outputs.append(TransactionOutput(P2PKH().lock(change_addr), change_val))   # change → derived addr
    else:
        fee += change_val                               # dust change folds into the fee
    tx = Transaction(tx_inputs, tx_outputs)
    tx.sign()
    return tx, need, fee


def build_third_entry(source_priv: PrivateKey, data_items: list[bytes], change_addr: str,
                      sat_per_kb: int = _FEE_SAT_PER_KB, allow_unconfirmed: bool = False) -> tuple[Transaction, int]:
    """Spend the source key's largest UTXO into a 0-sat OP_RETURN (the public Third Entry,
    `OP_FALSE OP_RETURN <tag> <modelHash> <receiptHash>`) + change to `change_addr`. Exact fee at `sat_per_kb`
    (sat/KILOBYTE) via a measure-then-set-change pass. With `allow_unconfirmed` it will also spend mempool UTXOs
    — needed for a self-rolling hot source (change → source) emitting receipts back-to-back. Returns (tx, fee)."""
    src_addr = source_priv.address()
    utxos = woc_utxos(src_addr) if allow_unconfirmed \
        else [u for u in woc_utxos(src_addr) if u.get("height", 0) > 0]      # confirmed only unless rolling
    if not utxos:
        raise SystemExit(f"no {'spendable' if allow_unconfirmed else 'confirmed'} UTXOs at source {src_addr}")
    u = max(utxos, key=lambda x: x["value"])
    have = u["value"]
    src_tx = Transaction.from_hex(woc_rawtx(u["tx_hash"]))
    op_out = TransactionOutput(OpReturn().lock(data_items), 0)

    def _signed(change_val: int) -> Transaction:
        txin = TransactionInput(source_transaction=src_tx, source_output_index=u["tx_pos"],
                                unlocking_script_template=P2PKH().unlock(source_priv))
        outs = [op_out]
        if change_val >= DUST:
            outs.append(TransactionOutput(P2PKH().lock(change_addr), change_val))
        t = Transaction([txin], outs)
        t.sign()
        return t

    probe = _signed(have)                                  # measure the real signed size
    size = len(probe.hex()) // 2
    fee = (size * sat_per_kb + 999) // 1000                 # ceil(sat/KB over the measured size)
    change_val = have - fee
    if change_val < 0:
        raise SystemExit(f"source UTXO {have} too small for fee {fee}")
    return _signed(change_val), fee


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="notary_wallet", description="bonsai-notary self-managed BSV HD wallet")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("gen-mnemonic").add_argument("--force", action="store_true")

    for name in ("address", "keyfile"):
        p = sub.add_parser(name)
        p.add_argument("--role", choices=sorted(ROLES))
        p.add_argument("--change", type=int, default=0)
        p.add_argument("--index", type=int, default=0)
        p.add_argument("--label", default="")

    nc = sub.add_parser("next-change",
        help="print the next UNUSED change address (m/44'/236'/0'/1/i) + write its keyfile — for CHANGE_ADDRESS")
    nc.add_argument("--start", type=int, default=None, help="scan from this change index (default: persisted floor)")
    nc.add_argument("--no-keyfile", action="store_true", help="just print the address, don't write the keyfile")
    nc.add_argument("--verify", action="store_true", help="also confirm the address is unused on-chain (1 WoC call)")

    fk = sub.add_parser("fund-key",
        help="pick a wallet-derived address holding >= --need sats, write its keyfile, print the path (for FUND_*_KEY_FILE)")
    fk.add_argument("--need", type=int, default=12000, help="minimum spendable satoshis required (default 12000)")
    fk.add_argument("--scan", type=int, default=24, help="receive/change indices to sweep (default 24)")
    fk.add_argument("--allow-unconfirmed", action="store_true", help="also count mempool (unconfirmed) balance")
    fk.add_argument("--json", action="store_true", help="emit JSON {keyfile,address,satoshis,change,index}")

    for name in ("balance", "utxos"):
        sub.add_parser(name).add_argument("--address", required=True)

    f = sub.add_parser("fanout", help="fan a source UTXO into N fresh UTXOs at the wallet's own derived addresses")
    src = f.add_mutually_exclusive_group(required=True)
    src.add_argument("--source-wif-file", help="JSON keyfile {wif:…} for the funding source (e.g. an external <1 BSV UTXO)")
    src.add_argument("--source-index", type=int, help="receive-path index of an OWN funded address to spend")
    f.add_argument("--count", type=int, required=True, help="number of fresh UTXOs to create")
    f.add_argument("--size", type=int, required=True, help="satoshis per fresh UTXO")
    f.add_argument("--to-start", type=int, default=10, help="receive index for the first fresh UTXO (default 10)")
    f.add_argument("--to-same", action="store_true",
                   help="put all --count outputs at the SINGLE --to-start address (several UTXOs at one key)")
    f.add_argument("--change-index", type=int, default=0, help="CHANGE-path index for the change output (default 0)")
    f.add_argument("--sat-per-kb", type=int, default=_FEE_SAT_PER_KB,
                   help="fee in sat/KILOBYTE (default 100 = 0.1 sat/byte; computed exactly, at/above target)")
    f.add_argument("--broadcast", action="store_true", help="broadcast (default: dry-run prints the signed tx)")

    te = sub.add_parser("third-entry", help="land a public OP_RETURN Third Entry from a pre-split UTXO")
    te.add_argument("--source-index", type=int, default=10, help="receive index of a pre-split funding UTXO to spend")
    te.add_argument("--tag", default="trinote/r1", help="protocol tag (default trinote/r1)")
    te.add_argument("--model-hash", required=True, help="64-hex modelHash to commit")
    te.add_argument("--receipt-hash", required=True, help="64-hex receiptHash to commit (the inference receipt)")
    te.add_argument("--change-index", type=int, default=2, help="CHANGE-path index for the change output")
    te.add_argument("--sat-per-kb", type=int, default=_FEE_SAT_PER_KB, help="fee in sat/KILOBYTE (default 100)")
    te.add_argument("--broadcast", action="store_true")
    te.add_argument("--json", action="store_true", help="emit one-line JSON result (for the notary emit backend)")
    te.add_argument("--change-to-source", action="store_true",
                    help="route change back to the SOURCE receive address (self-rolling hot UTXO for live emits)")
    te.add_argument("--allow-unconfirmed", action="store_true",
                    help="also spend mempool UTXOs (needed for back-to-back rolling emits)")

    args = ap.parse_args(argv)

    if args.cmd == "gen-mnemonic":
        gen_mnemonic(force=args.force)
        x = bip44_derive_xprv_from_mnemonic(load_mnemonic(), path=BIP44_ACCOUNT)
        print(f"[wallet] mnemonic generated → {MNEMONIC_FILE} (0600, gitignored)")
        print(f"[wallet] account xpub: {x.xpub()}")
        for role, (c, i) in ROLES.items():
            print(f"[wallet]   {role:12} {BIP44_ACCOUNT}/{c}/{i} → {_key(c, i).address()}")
        return 0

    if args.cmd == "address":
        c, i = _resolve(args.role, args.change, args.index)
        print(_key(c, i).address())
        return 0

    if args.cmd == "keyfile":
        c, i = _resolve(args.role, args.change, args.index)
        print(keyfile(c, i, args.label))
        return 0

    if args.cmd == "next-change":
        with _change_lock():                           # serialize select→reserve→bump so two concurrent
            i, addr = next_change(args.start, verify=args.verify)   # broadcasts can't claim the same index
            if not args.no_keyfile:
                keyfile(1, i, label="change")         # recoverable {address, wif} so change is never stranded
            SECRETS.mkdir(parents=True, exist_ok=True)
            CHANGE_FLOOR_FILE.write_text(str(i) + "\n")   # persisted floor (re-checked each call; dry-runs waste nothing)
        print(addr)                                    # stdout = the fresh change address (for CHANGE_ADDRESS)
        return 0

    if args.cmd == "fund-key":
        c, i, addr, sats = fund_key(args.need, scan=args.scan, allow_unconfirmed=args.allow_unconfirmed)
        p = keyfile(c, i, label="funding")             # ensure {address, wif} exists for the chain layer
        if args.json:
            print(json.dumps({"keyfile": str(p), "address": addr, "satoshis": sats, "change": c, "index": i}))
        else:
            print(p)                                   # stdout = keyfile path (for FUND_*_KEY_FILE)
        return 0

    if args.cmd == "balance":
        print(json.dumps(woc_balance(args.address)))
        return 0

    if args.cmd == "utxos":
        u = woc_utxos(args.address)
        print(json.dumps(u, indent=2))
        print(f"# {len(u)} utxos, total {sum(x['value'] for x in u)} sats", file=sys.stderr)
        return 0

    if args.cmd == "fanout":
        if args.source_wif_file:
            src_priv = load_keyfile(args.source_wif_file)
        else:
            src_priv = _key(0, args.source_index)
        if args.to_same:
            # All `count` outputs to the SINGLE receive address at `to_start` — several UTXOs at ONE address
            # (e.g. the Elder/default key, the only key scrypt-ts signs funding inputs with).
            dests = [(_key(0, args.to_start).address(), args.size) for _ in range(args.count)]
        else:
            dests = [(_key(0, args.to_start + n).address(), args.size) for n in range(args.count)]
        change_addr = _key(1, args.change_index).address()
        tx, need, fee = build_fanout(src_priv, dests, change_addr, sat_per_kb=args.sat_per_kb)
        size = len(tx.hex()) // 2
        print(f"[fanout] source       : {src_priv.address()}")
        _span = f"receive {args.to_start} ×{args.count}" if args.to_same \
            else f"receive {args.to_start}..{args.to_start + args.count - 1}"
        print(f"[fanout] creating     : {args.count} × {args.size} sats = {need} sats at OWN addresses ({_span})")
        for n, (addr, sats) in enumerate(dests):
            print(f"[fanout]   out{n}: {addr}  {sats}")
        print(f"[fanout] change → m/.../1/{args.change_index}: {change_addr}")
        print(f"[fanout] fee          : {fee} sats over {size} B = {fee*1000//size} sat/KB "
              f"({fee/size:.3f} sat/byte)  [target ≥{args.sat_per_kb} sat/KB]")
        print(f"[fanout] txid (computed): {tx.txid()}")
        if not args.broadcast:
            print("[fanout] DRY RUN — not broadcasting. Re-run with --broadcast to send.")
            return 0
        res = woc_broadcast(tx.hex())
        print(f"[fanout] BROADCAST → {res}")
        return 0

    if args.cmd == "third-entry":
        src_priv = _key(0, args.source_index)
        data = [_check_tag(args.tag),
                _hash32("--model-hash", args.model_hash), _hash32("--receipt-hash", args.receipt_hash)]
        change_addr = src_priv.address() if args.change_to_source else _key(1, args.change_index).address()
        tx, fee = build_third_entry(src_priv, data, change_addr, sat_per_kb=args.sat_per_kb,
                                    allow_unconfirmed=args.allow_unconfirmed)
        size = len(tx.hex()) // 2
        result = {"txid": tx.txid(), "opReturn": tx.outputs[0].locking_script.hex(),
                  "rawTx": tx.hex(),
                  "tag": args.tag, "modelHash": args.model_hash, "receiptHash": args.receipt_hash,
                  "source": src_priv.address(), "sourceIndex": args.source_index,
                  "changeAddress": change_addr, "fee": fee, "sizeBytes": size,
                  "satPerKb": fee * 1000 // size, "broadcast": False, "status": "dry-run"}
        if args.broadcast:
            woc_broadcast(tx.hex())
            result["broadcast"] = True
            result["status"] = "broadcast"
        if args.json:
            print(json.dumps(result))
            return 0
        print(f"[third-entry] source       : {result['source']} (recv idx {args.source_index})")
        print(f"[third-entry] OP_RETURN     : {result['opReturn']}")
        print(f"[third-entry]   tag={args.tag}  modelHash={args.model_hash}  receiptHash={args.receipt_hash}")
        print(f"[third-entry] change → {'SOURCE 0/' + str(args.source_index) if args.change_to_source else '1/' + str(args.change_index)}: {change_addr}")
        print(f"[third-entry] fee          : {fee} sats over {size} B = {result['satPerKb']} sat/KB ({fee/size:.3f} sat/byte)")
        print(f"[third-entry] {'BROADCAST' if result['broadcast'] else 'DRY RUN — not broadcasting'}  txid={result['txid']}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
