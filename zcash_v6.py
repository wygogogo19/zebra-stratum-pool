#!/usr/bin/env python3
"""Zcash v6 transaction digests (txid / auth digest) and block-commitment recomputation.

Every convention below is taken from the reference implementations:
  * librustzcash `zcash_primitives/src/transaction/txid.rs` (ZIP-244 digest tree + v6 differences)
  * orchard `src/bundle/commitments.rs` (per-version personalizations and empty-bundle digests for orchard/ironwood)
  * zcashd `src/primitives/block.cpp` (auth-data tree and DeriveBlockCommitmentsHash)

Key points:
  * every BLAKE2b call uses hash_length=32; personalizations are padded to 16 bytes;
  * the txid root personalization = "ZcashTxHash_" ‖ LE32(consensusBranchId);
    the auth root = "ZTxAuthHash_" ‖ LE32(consensusBranchId);
  * an empty bundle's digest is BLAKE2b over *empty input* (i.e. hashing the personalization alone);
  * block commitment = BLAKE2b-256("ZcashBlockCommit") over (chainHistoryRoot ‖ authDataRoot ‖ 32 zero bytes).

Acceptance checks (plan gate 1/3):
  1. txid(transparent coinbase of a real block) == the txid the RPC reports for that transaction;
  2. auth-data tree + block commitment == `authdataroot` / `blockcommitmentshash` from GBT `defaultroots`.
"""

from __future__ import annotations

import hashlib

# ---------------------------------------------------------------- personalization
PREFIX_TXID = b"ZcashTxHash_"          # + LE32(branch_id)
PREFIX_AUTH = b"ZTxAuthHash_"          # + LE32(branch_id)

P_HEADERS = b"ZTxIdHeadersHash"
P_TRANSPARENT = b"ZTxIdTranspaHash"
P_SAPLING = b"ZTxIdSaplingHash"
P_PREVOUTS = b"ZTxIdPrevoutHash"
P_SEQUENCE = b"ZTxIdSequencHash"
P_OUTPUTS = b"ZTxIdOutputsHash"
P_TRANSPARENT_SCRIPTS = b"ZTxAuthTransHash"

# Empty-bundle digests, selected by (pool, transaction version)
P_SAPLING_EMPTY_TXID = P_SAPLING
P_ORCHARD_V6_EMPTY_TXID = b"ZTxIdOrchardH_v6"
P_IRONWOOD_V6_EMPTY_TXID = b"ZTxIdIronwd_H_v6"
P_SAPLING_V6_EMPTY_AUTH = b"ZTxAuthSapliH_v6"
P_ORCHARD_V6_EMPTY_AUTH = b"ZTxAuthOrchaH_v6"
P_IRONWOOD_V6_EMPTY_AUTH = b"ZTxAuthIrnwdH_v6"

P_AUTH_DATA_TREE = b"ZcashAuthDatHash"
P_BLOCK_COMMITMENTS = b"ZcashBlockCommit"


def blake2b_256(data: bytes, personal: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32, person=personal).digest()


def empty_digest(personal: bytes) -> bytes:
    return blake2b_256(b"", personal)


# ---------------------------------------------------------------------- parsing
def read_compact(buf: bytes, off: int):
    first = buf[off]
    if first < 0xFD:
        return first, off + 1
    if first == 0xFD:
        return int.from_bytes(buf[off + 1:off + 3], "little"), off + 3
    if first == 0xFE:
        return int.from_bytes(buf[off + 1:off + 5], "little"), off + 5
    return int.from_bytes(buf[off + 1:off + 9], "little"), off + 9


def write_compact(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def parse_transparent(raw: bytes) -> dict:
    """Parse the header and transparent section of a v5/v6 transaction (shielded parts untouched)."""
    version = int.from_bytes(raw[0:4], "little")
    if not (version & 0x80000000):
        raise ValueError(f"pre-Overwintered tx version {version:#x} unsupported")
    tx_version = version & 0x7FFFFFFF
    if tx_version not in (5, 6):
        raise ValueError(f"unsupported tx version {tx_version}")
    group_id = int.from_bytes(raw[4:8], "little")
    branch_id = int.from_bytes(raw[8:12], "little")
    lock_time = int.from_bytes(raw[12:16], "little")
    expiry = int.from_bytes(raw[16:20], "little")
    off = 20
    n_in, off = read_compact(raw, off)
    vins = []
    for _ in range(n_in):
        prevout = raw[off:off + 36]; off += 36
        sl, off = read_compact(raw, off)
        script = raw[off:off + sl]; off += sl
        seq = int.from_bytes(raw[off:off + 4], "little"); off += 4
        vins.append({"prevout": prevout, "script": script, "sequence": seq})
    n_out, off = read_compact(raw, off)
    vouts = []
    for _ in range(n_out):
        value = int.from_bytes(raw[off:off + 8], "little"); off += 8
        sl, off = read_compact(raw, off)
        script = raw[off:off + sl]; off += sl
        vouts.append({"value": value, "script": script})
    n_spend, off = read_compact(raw, off)
    n_sout, off = read_compact(raw, off)
    n_orchard, off = read_compact(raw, off)
    n_ironwood = 0
    if tx_version >= 6:
        n_ironwood, off = read_compact(raw, off)
    return {"version": version, "group_id": group_id, "branch_id": branch_id,
            "lock_time": lock_time, "expiry": expiry, "vins": vins, "vouts": vouts,
            "shielded": {"sapling_spends": n_spend, "sapling_outputs": n_sout,
                         "orchard_actions": n_orchard, "ironwood_actions": n_ironwood},
            "transparent_end": off}


def is_transparent_only(tx: dict) -> bool:
    s = tx["shielded"]
    if any(s.values()):
        return False
    # With no shielded actions the flags byte is 0x01 (no shielded data present)
    return True


def serialize_transparent_tx(tx: dict) -> bytes:
    out = bytearray()
    out += tx["version"].to_bytes(4, "little")
    out += tx["group_id"].to_bytes(4, "little")
    out += tx["branch_id"].to_bytes(4, "little")
    out += tx["lock_time"].to_bytes(4, "little")
    out += tx["expiry"].to_bytes(4, "little")
    out += write_compact(len(tx["vins"]))
    for vin in tx["vins"]:
        out += vin["prevout"]
        out += write_compact(len(vin["script"])) + vin["script"]
        out += vin["sequence"].to_bytes(4, "little")
    out += write_compact(len(tx["vouts"]))
    for vout in tx["vouts"]:
        out += vout["value"].to_bytes(8, "little")
        out += write_compact(len(vout["script"])) + vout["script"]
    # No shielded data: v6 serialisation stops here (four zero counters, no flags/balance/proof fields).
    # Evidence: the tail bytes of 28 real transparent v6 coinbases are exactly "00000000" (build_coinbase.py --selftest).
    out += write_compact(0) * 4
    return bytes(out)


# -------------------------------------------------------------------- digests
def header_digest(tx: dict) -> bytes:
    h = blake2b_256_sink(P_HEADERS)
    h.update(tx["version"].to_bytes(4, "little"))
    h.update(tx["group_id"].to_bytes(4, "little"))
    h.update(tx["branch_id"].to_bytes(4, "little"))
    h.update(tx["lock_time"].to_bytes(4, "little"))
    h.update(tx["expiry"].to_bytes(4, "little"))
    return h.digest()


def blake2b_256_sink(personal: bytes):
    return hashlib.blake2b(digest_size=32, person=personal)


def transparent_digests(tx: dict) -> tuple[bytes, bytes, bytes]:
    hp = blake2b_256_sink(P_PREVOUTS)
    hs = blake2b_256_sink(P_SEQUENCE)
    ho = blake2b_256_sink(P_OUTPUTS)
    for vin in tx["vins"]:
        hp.update(vin["prevout"])
        hs.update(vin["sequence"].to_bytes(4, "little"))
    for vout in tx["vouts"]:
        ho.update(vout["value"].to_bytes(8, "little"))
        ho.update(write_compact(len(vout["script"])))
        ho.update(vout["script"])
    return hp.digest(), hs.digest(), ho.digest()


def txid_v6(tx: dict) -> bytes:
    """txid of a transparent (v6) transaction, internal byte order (reverse for RPC display)."""
    assert tx["version"] == 0x80000006, f"not v6: {tx['version']:#x}"
    ht = blake2b_256_sink(P_TRANSPARENT)
    for d in transparent_digests(tx):
        ht.update(d)
    root = blake2b_256_sink(PREFIX_TXID + tx["branch_id"].to_bytes(4, "little"))
    root.update(header_digest(tx))
    root.update(ht.digest())
    root.update(empty_digest(P_SAPLING_EMPTY_TXID))
    root.update(empty_digest(P_ORCHARD_V6_EMPTY_TXID))
    root.update(empty_digest(P_IRONWOOD_V6_EMPTY_TXID))
    return root.digest()


def auth_digest_v6(tx: dict) -> bytes:
    """auth digest of a transparent (v6) transaction, internal byte order."""
    hscripts = blake2b_256_sink(P_TRANSPARENT_SCRIPTS)
    for vin in tx["vins"]:
        hscripts.update(write_compact(len(vin["script"])))
        hscripts.update(vin["script"])
    root = blake2b_256_sink(PREFIX_AUTH + tx["branch_id"].to_bytes(4, "little"))
    root.update(hscripts.digest())
    root.update(empty_digest(P_SAPLING_V6_EMPTY_AUTH))
    root.update(empty_digest(P_ORCHARD_V6_EMPTY_AUTH))
    root.update(empty_digest(P_IRONWOOD_V6_EMPTY_AUTH))
    return root.digest()


# ------------------------------------------------------ block layer: merkle and commitments
def dsha256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def merkle_root(txids_internal: list[bytes]) -> bytes:
    level = list(txids_internal)
    if not level:
        return b"\x00" * 32
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [dsha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def auth_data_root(auth_digests_internal: list[bytes]) -> bytes:
    """zcashd CBlock::BuildAuthDataMerkleTree: includes the coinbase, zero-pads to a power of two and
    combines nodes with BLAKE2b-256("ZcashAuthDatHash")."""
    leaves = list(auth_digests_internal)
    perfect = 1
    while perfect < len(leaves):
        perfect *= 2
    level = leaves + [bytes(32)] * (perfect - len(leaves))
    while len(level) > 1:
        level = [blake2b_256(level[i] + level[i + 1], P_AUTH_DATA_TREE)
                 for i in range(0, len(level), 2)]
    return level[0]


def block_commitments(chain_history_root: bytes, auth_root: bytes) -> bytes:
    return blake2b_256(chain_history_root + auth_root + bytes(32), P_BLOCK_COMMITMENTS)
