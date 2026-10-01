#!/usr/bin/env python3
"""Equihash(200,9) solution verifier (faithful translation of the Zcash reference implementation).

Purpose: **reverse-engineer the real assembly topology of the 28-byte nonce a rig submits**. An Equihash
solution is only valid for the exact 140-byte block header it was solved against, so feeding candidate
headers to the verifier one by one returns True for the real one (a wrong header passes with probability

Reference implementation notes (zcash/src/crypto/equihash.{h,cpp}):
  * state: BLAKE2b-50 with personalization = "ZcashPoW" || LE32(200) || LE32(9); the block header is
    absorbed first (108-byte prefix || 32-byte nonce, i.e. the full 140-byte header)
  * index hash: H(g) = BLAKE2b(header || LE32(g)), 50 bytes holding two 25-byte sub-hashes;
    index i uses sub-hash (i % 2)
  * those 25 bytes (200 bits) become ten 20-bit groups, each right-aligned in 3 bytes (big endian)
  * each round: the first 20-bit group of two adjacent rows must be equal; the fold drops that group and
    XORs the remaining nine groups pairwise
  * 512 indices fold down to one group in nine rounds; the final group must be zero
  * compact solution format: 512 21-bit values packed in **big-endian bit order** (ExpandArray, bit_len=21, byte_pad=1)
  $ python3 equihash_verify.py --selftest /tmp/real_blocks.json
  $ python3 equihash_verify.py --shares /tmp/diag.txt <extranonce1> [...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys

N, K = 200, 9
CW = N // (K + 1)                       # 20-bit collision width
INDICES = 1 << K                        # 512
HASH_OUT = (512 // N) * N // 8          # 50 bytes
CHUNK = N // 8                          # 25 bytes
CHUNKS_PER_HASH = HASH_OUT // CHUNK     # 2
COLLISION_BYTES = (CW + 7) // 8         # 3
SOLUTION_BYTES = INDICES * (CW + 1) // 8   # 1344
PERSONAL = b"ZcashPoW" + struct.pack("<I", N) + struct.pack("<I", K)


def expand_array(data: bytes, bit_len: int, byte_pad: int) -> bytes:
    """zcash::ExpandArray - big-endian bit-order unpacking (each bit_len-bit field right-aligned)."""
    out_width = (bit_len + 7) // 8 + byte_pad
    out_len = 8 * out_width * len(data) // bit_len
    mask = (1 << bit_len) - 1
    out = bytearray(out_len)
    acc = 0
    acc_bits = 0
    j = 0
    for byte in data:
        acc = (acc << 8) | byte
        acc_bits += 8
        if acc_bits >= bit_len:
            acc_bits -= bit_len
            for x in range(byte_pad):
                out[j + x] = 0
            for x in range(byte_pad, out_width):
                out[j + x] = (acc >> (acc_bits + 8 * (out_width - x - 1))) & (
                    (mask >> (8 * (out_width - x - 1))) & 0xFF
                )
            j += out_width
    return bytes(out)


def get_indices_from_minimal(sol: bytes) -> list[int]:
    """Compact solution (1344 B, 512 x 21 bit big-endian) -> 512 indices."""
    expanded = expand_array(sol, CW + 1, 1)          # 4 bytes per index, leading byte zero
    return [int.from_bytes(expanded[i * 4:i * 4 + 4], "big") for i in range(INDICES)]


def strip_prefix(sol: bytes) -> bytes:
    """The solution a rig submits usually carries the CompactSize prefix (fd4005 = 1344)."""
    if len(sol) > 3 and sol[0] == 0xFD and int.from_bytes(sol[1:3], "little") == len(sol) - 3:
        return sol[3:]
    return sol


def _row_for_index(header140: bytes, index: int) -> list[int]:
    """Index -> the row's ten 20-bit groups (one integer per group)."""
    g = index // CHUNKS_PER_HASH
    digest = hashlib.blake2b(header140 + struct.pack("<I", g), digest_size=HASH_OUT,
                             person=PERSONAL).digest()
    chunk = digest[(index % CHUNKS_PER_HASH) * CHUNK:(index % CHUNKS_PER_HASH) * CHUNK + CHUNK]
    expanded = expand_array(chunk, CW, 0)            # 30 bytes = 10 groups x 3 bytes
    return [int.from_bytes(expanded[i * 3:i * 3 + 3], "big") for i in range(len(expanded) // 3)]


def verify(header140: bytes, solution: bytes) -> bool:
    """Is this (header, solution) a valid Equihash(200,9) solution?"""
    if len(header140) != 140:
        return False
    raw = strip_prefix(solution)
    if len(raw) != SOLUTION_BYTES:
        return False
    indices = get_indices_from_minimal(raw)
    # indices are 21-bit fields (the reference implementation checks collisions/order only, not ranges).
    if any(i >= (1 << (CW + 1)) for i in indices):
        return False
    rows = [_row_for_index(header140, i) for i in indices]
    while len(rows) > 1:
        collapsed = []
        for j in range(0, len(rows), 2):
            a, b = rows[j], rows[j + 1]
            if a[0] != b[0]:                          # this round's 20-bit collision check
                return False
            collapsed.append([a[k] ^ b[k] for k in range(1, len(a))])
        rows = collapsed
    return all(chunk == 0 for chunk in rows[0])       # the final round must be all zeros


# ------------------------------------------------------------- calibration / reverse engineering

def selftest(path: str) -> int:
    blocks = json.load(open(path, encoding="utf-8"))
    print(f"{len(blocks)} real mainnet blocks - the verifier must pass every one of them:")
    ok_all = True
    for blk in blocks:
        try:
            ok = verify(bytes.fromhex(blk["header140"]), bytes.fromhex(blk["solution"]))
        except Exception as exc:  # noqa: BLE001
            ok = f"error: {exc}"
        print(f"  height={blk['height']}  ->  {ok}")
        ok_all = ok_all and ok is True
    print("\nConclusion:", "verifier is trustworthy" if ok_all else "verifier still deviates")
    return 0 if ok_all else 1


def parse_diag(path: str) -> list[dict]:
    shares, cur = [], None
    for line in open(path, encoding="utf-8", errors="replace"):
        if "DIAG reject" in line:
            f = dict(re.findall(r"(\w+)=([0-9a-fA-F]+)", line))
            cur = {
                "version": f.get("version", "00000004"),
                "prevhash": f.get("prevhash", ""),
                "merkle": f.get("merkle", ""),
                "commitments": f.get("commitments", ""),
                "bits": f.get("bits", ""),
                "ntime": f.get("ntime", "0"),
                "nonce": f.get("nonce", ""),
            }
        elif "DIAG full" in line and cur is not None:
            m = re.search(r"nonce_raw=([0-9a-fA-F]+)", line)
            s = re.search(r"solution=([0-9a-fA-F]+)", line)
            if m:
                cur["nonce_raw"] = m.group(1)
            if s:
                cur["solution"] = s.group(1)
            if "solution" in cur:
                shares.append(cur)
                cur = None
    return shares


def swab32(b: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(b), 4):
        out += b[i:i + 4][::-1]
    return bytes(out)


def nonce_candidates(share: dict, e1s: list[str]):
    """Assembly topologies for a 28-byte miner nonce plus the pool-supplied 4-byte extranonce1."""
    n28 = bytes.fromhex(share.get("nonce_raw") or share["nonce"])
    out = [("padL28", n28.rjust(32, b"\x00")), ("padR28", n28.ljust(32, b"\x00"))]
    for e1 in e1s:
        eb = bytes.fromhex(e1)
        out += [
            (f"e1+{e1} (4+28)", eb + n28),
            (f"rev(e1)+{e1}", eb[::-1] + n28),
            (f"{e1}+e1 (28+4)", n28 + eb),
            (f"{e1}+rev(e1)", n28 + eb[::-1]),
        ]
        for pos in (4, 8, 20, 24):
            for tag, block in (("e1", eb), ("rev", eb[::-1])):
                padded = n28[:pos] + block + n28[pos:]
                out.append((f"ins[{tag}]@{pos}", padded[:32].ljust(32, b"\x00")))
    for name, value in list(out):
        out.append((name + "/rev", value[::-1]))
    return out


def reverse_from_shares(path: str, e1s: list[str], limit: int) -> int:
    shares = parse_diag(path)
    print(f"parsed {len(shares)} shares carrying a full solution; extranonce1 candidates {e1s}")
    if not shares:
        return 1
    tested = 0
    for si, share in enumerate(shares[:limit]):
        prev = bytes.fromhex(share["prevhash"])
        merkle = bytes.fromhex(share["merkle"])
        commit = bytes.fromhex(share["commitments"])
        bits = bytes.fromhex(share["bits"])
        ntime = struct.pack("<I", int(share["ntime"], 10))
        sol = bytes.fromhex(share["solution"])
        layouts = {
            "prev[A]/mrk[A]/cmt[A]/bits[A]": (prev, merkle, commit, bits),
            "prev[S]/mrk[A]/cmt[A]/bits[A]": (swab32(prev), merkle, commit, bits),
            "prev[R]/mrk[R]/cmt[R]/bits[R]": (prev[::-1], merkle[::-1], commit[::-1], bits[::-1]),
            "prev[SR]/mrk[S]/cmt[S]/bits[S]": (swab32(prev[::-1]), swab32(merkle), swab32(commit), swab32(bits)),
        }
        for lname, (p, m, c, b) in layouts.items():
            for nname, nonce in nonce_candidates(share, e1s):
                for ver_name, ver in (("LE", struct.pack("<I", int(share["version"], 16))),
                                      ("ASIS", bytes.fromhex(share["version"].rjust(8, "0")))):
                    header = ver + p + m + c + ntime + b + nonce
                    tested += 1
                    if verify(header, sol):
                        print(f"\n*** HIT! share#{si} layout={lname} nonce={nname} version={ver_name}")
                        print(f"    header140={header.hex()}")
                        print(f"    solution length={len(sol)} bytes")
                        return 0
        print(f"  share#{si} tried (cumulative {tested} candidate headers)", flush=True)
    print(f"\ntested {tested} candidate header combinations, no hit.")
    return 1


def _field_variants(name: str, hex_value: str):
    b = bytes.fromhex(hex_value)
    return [(f"{name}A", b), (f"{name}S", swab32(b)),
            (f"{name}R", b[::-1]), (f"{name}SR", swab32(b[::-1]))]


def _exhaustive_one(job):
    """(header assembly parameters) -> (hit?, description). Used by the multiprocessing workers."""
    (prev, merkle, commit, bits, version, ntime, nonce, sol, desc) = job
    header = version + prev + merkle + commit + ntime + bits + nonce
    if verify(header, sol):
        return (True, desc, header.hex())
    return (False, desc, None)


def exhaustive(path: str, e1s: list[str], shares_limit: int, deltas: int, workers: int) -> int:
    """Exhaustive: 4 fields x 4 byte-order transforms x 2 versions x ntime jitter x every nonce topology."""
    import concurrent.futures as cf

    shares = parse_diag(path)
    if not shares:
        print("no usable shares")
        return 1
    print(f"exhaustive mode: {len(shares)} shares, first {shares_limit}; extranonce1={e1s}")
    for si, share in enumerate(shares[:shares_limit]):
        jobs = []
        sol = bytes.fromhex(share["solution"])
        base_ntime = int(share["ntime"], 10)
        nonces = nonce_candidates(share, e1s)
        versions = [("vLE", struct.pack("<I", int(share["version"], 16))),
                    ("vAS", bytes.fromhex(share["version"].rjust(8, "0")))]
        for pname, prev in _field_variants("p", share["prevhash"]):
            for mname, merkle in _field_variants("m", share["merkle"]):
                for cname, commit in _field_variants("c", share["commitments"]):
                    for bname, bits in _field_variants("b", share["bits"]):
                        for vname, ver in versions:
                            for delta in range(-deltas, deltas + 1):
                                ntime = struct.pack("<I", base_ntime + delta)
                                for nname, nonce in nonces:
                                    desc = (f"share#{si} {vname} {pname}/{mname}/{cname}/{bname} "
                                            f"ntime{delta:+d} nonce[{nname}]")
                                    jobs.append((prev, merkle, commit, bits, ver, ntime,
                                                 nonce, sol, desc))
        print(f"  share#{si}: {len(jobs)} candidate headers, verifying with multiple processes...", flush=True)
        with cf.ProcessPoolExecutor(max_workers=workers) as ex:
            for hit, desc, header_hex in ex.map(_exhaustive_one, jobs, chunksize=64):
                if hit:
                    print(f"\n*** HIT! {desc}\n    header140={header_hex}")
                    ex.shutdown(wait=False, cancel_futures=True)
                    return 0
        print(f"  share#{si}: no hit", flush=True)
    print("\nexhaustive run finished, still no hit.")
    return 1


def curated_layouts(share: dict):
    """The few most likely field conventions (ntime endianness, bits reversal, ...)."""
    prev_disp = bytes.fromhex(share["prevhash"])
    merkle = bytes.fromhex(share["merkle"])
    commit = bytes.fromhex(share["commitments"])
    bits = bytes.fromhex(share["bits"])
    version_int = struct.pack("<I", int(share["version"], 16))
    version_raw = bytes.fromhex(share["version"].rjust(8, "0"))
    nt = int(share["ntime"], 10)
    ntime_le = struct.pack("<I", nt)
    ntime_raw = nt.to_bytes(4, "big")
    return {
        # what the pool does today: prev byte-swapped, everything else verbatim, version and ntime little endian
        "pool": (version_int, swab32(prev_disp), merkle, commit, ntime_le, bits),
        # ntime written as the raw bytes received
        "pool/ntimeBE": (version_int, swab32(prev_disp), merkle, commit, ntime_raw, bits),
        # bits reversed
        "pool/bitsR": (version_int, swab32(prev_disp), merkle, commit, ntime_le, bits[::-1]),
        # firmware treating prevhash as display order
        "prevA": (version_int, prev_disp, merkle, commit, ntime_le, bits),
        # merkle/commitments reversed
        "mrkR/cmtR": (version_int, swab32(prev_disp), merkle[::-1], commit[::-1], ntime_le, bits),
        # everything interpreted as display order
        "allR": (version_raw, prev_disp[::-1], merkle[::-1], commit[::-1], ntime_raw, bits[::-1]),
    }


def matrix(path: str, e1s: list[str], shares_limit: int) -> int:
    """Statistical search: a hypothesis that is right must hit for nearly every share."""
    shares = parse_diag(path)
    if not shares:
        print("no usable shares")
        return 1
    shares = shares[:shares_limit]
    print(f"matrix mode: {len(shares)} shares, extranonce1={e1s}\n")
    tally: dict[str, int] = {}
    for share in shares:
        sol = bytes.fromhex(share["solution"])
        nonces = dict(nonce_candidates(share, e1s))
        for lname, (ver, prev, merkle, commit, ntime, bits) in curated_layouts(share).items():
            for nname, nonce in nonces.items():
                header = ver + prev + merkle + commit + ntime + bits + nonce
                key = f"{lname} | nonce[{nname}]"
                if verify(header, sol):
                    tally[key] = tally.get(key, 0) + 1
    print(f"tested {len(shares)} shares x 6 field conventions x ~{len(list(nonce_candidates(shares[0], e1s)))} nonce topologies")
    if not tally:
        print("no hypothesis hit (not even once)")
        return 1
    print("\nhit counts (a real match should be close to 100%):")
    for key, count in sorted(tally.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {count:4d}/{len(shares)}  {key}")
    best = max(tally.values())
    print("\nConclusion:", "real assembly found" if best >= len(shares) * 0.8 else "still no stable hypothesis")
    return 0 if best >= len(shares) * 0.8 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", metavar="REAL_BLOCKS_JSON")
    ap.add_argument("--shares", metavar="DIAG_TXT")
    ap.add_argument("--exhaustive", metavar="DIAG_TXT")
    ap.add_argument("--matrix", metavar="DIAG_TXT")
    ap.add_argument("extranonce1", nargs="*")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--deltas", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    if args.selftest:
        return selftest(args.selftest)
    if args.shares:
        return reverse_from_shares(args.shares, args.extranonce1 or [], args.limit)
    if args.exhaustive:
        return exhaustive(args.exhaustive, args.extranonce1 or [], args.limit,
                          args.deltas, args.workers)
    if args.matrix:
        return matrix(args.matrix, args.extranonce1 or [], args.limit)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
