#!/usr/bin/env python3
"""Mode 2: coinbase builder for the on-chain payout split (with a shadow-mode self-test).

Split model (reverse-verified against 8 real blocks):
    subsidy(h)   = 1.25e9 zat >> floor(h / 1_048_576)
    miner_total  = 0.80 * subsidy + fees          (fees go to the miner)
    lockbox      = 0.08 * subsidy                 (0.125 ZEC at this height — preserved verbatim)
    pool_fee     = floor(miner_total * pct/100)
    miner_net    = miner_total - pool_fee

Coinbase output order: [miner t-address][pool t-address][protocol lockbox]; scriptSig is copied verbatim

Usage (on the pool host):
  python3 build_coinbase.py --selftest                      # serialisation/digest regression on real blocks
  python3 build_coinbase.py <miner_t_addr> [pct] [pool_t_addr]  # shadow-mode build
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zcash_v6 as zv   # noqa: E402

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
HALVING = 1_048_576


def subsidy_zat(height: int) -> int:
    return 1_250_000_000 >> min(height // HALVING, 63)


# ----------------------------------------------------------------- address helpers
def b58_decode(addr: str) -> bytes:
    num = 0
    for ch in addr:
        num = num * 58 + _B58.index(ch)
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(addr) - len(addr.lstrip("1"))) + raw


def b58_encode(payload: bytes) -> str:
    num = int.from_bytes(payload, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return "1" * (len(payload) - len(payload.lstrip(b"\x00"))) + out


def address_to_script(addr: str) -> bytes:
    raw = b58_decode(addr)
    payload, checksum = raw[:-4], raw[-4:]
    if checksum != hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]:
        raise ValueError(f"address checksum mismatch: {addr}")
    prefix, h160 = payload[:2], payload[2:]
    if len(h160) != 20:
        raise ValueError(f"unexpected address length: {addr}")
    if prefix == b"\x1c\xb8":
        return b"\x76\xa9\x14" + h160 + b"\x88\xac"      # P2PKH (t1)
    if prefix == b"\x1c\xbd":
        return b"\xa9\x14" + h160 + b"\x87"              # P2SH (t3)
    raise ValueError(f"not a Zcash mainnet transparent address (t1/t3): {addr}")


def script_to_address(script: bytes) -> str:
    if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
        payload = b"\x1c\xb8" + script[3:23]
    elif len(script) == 23 and script[:2] == b"\xa9\x14" and script[22:] == b"\x87":
        payload = b"\x1c\xbd" + script[2:22]
    else:
        return "nonstandard:" + script.hex()
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return b58_encode(payload + chk)


# --------------------------------------------------------------------- RPC
def make_rpc():
    user, _, pwd = open("/etc/zecpool/zebra.cookie").read().strip().partition(":")
    auth = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()

    def rpc(method, params):
        req = urllib.request.Request(
            os.environ.get("ZEBRA_RPC_URL", "http://127.0.0.1:8232/"),
            data=json.dumps({"jsonrpc": "1.0", "id": 1, "method": method,
                             "params": params}).encode(),
            headers={"content-type": "text/plain", "Authorization": auth},
        )
        return json.loads(urllib.request.urlopen(req, timeout=40).read())["result"]
    return rpc


# ------------------------------------------------------------------- build
def build_dynamic_coinbase(tpl: dict, miner_addr: str, pool_addr: str, fee_pct: float):
    ref = zv.parse_transparent(bytes.fromhex(tpl["coinbasetxn"]["data"]))
    height = int(tpl["height"])
    subsidy = subsidy_zat(height)
    fees = sum(int(t.get("fee", 0)) for t in tpl.get("transactions", []))
    miner_total = subsidy * 8 // 10 + fees
    pool_fee = int(miner_total * fee_pct / 100.0)
    miner_net = miner_total - pool_fee
    lockbox_zat = subsidy * 8 // 100

    lockbox_outs = [v for v in ref["vouts"]
                    if len(v["script"]) == 23 and v["script"][:2] == b"\xa9\x14"]
    lockbox_script = lockbox_outs[-1]["script"] if lockbox_outs else None
    if lockbox_script is None:
        raise RuntimeError("no protocol lockbox output in the template — refusing to build")

    tx = {
        "version": 0x80000006,
        "group_id": ref["group_id"],
        "branch_id": ref["branch_id"],
        "lock_time": 0,
        "expiry": height,
        "vins": ref["vins"],                     # scriptSig copied verbatim (height + node tag)
        "vouts": [
            {"value": miner_net, "script": address_to_script(miner_addr)},
            {"value": pool_fee, "script": address_to_script(pool_addr)},
            {"value": lockbox_zat, "script": lockbox_script},
        ],
    }
    raw = zv.serialize_transparent_tx(tx)
    return {
        "tx": tx, "raw": raw, "height": height, "subsidy": subsidy, "fees": fees,
        "miner_total": miner_total, "miner_net": miner_net, "pool_fee": pool_fee,
        "lockbox_zat": lockbox_zat,
        "txid": zv.txid_v6(tx), "auth_digest": zv.auth_digest_v6(tx),
        "script_sig": ref["vins"][0]["script"],
    }


def block_roots(tpl: dict, cb_txid: bytes, cb_auth: bytes) -> dict:
    roots = {k: bytes.fromhex(v)[::-1] for k, v in (tpl.get("defaultroots") or {}).items()}
    txids = [cb_txid] + [bytes.fromhex(t["hash"])[::-1] for t in tpl.get("transactions", [])]
    auths = [cb_auth] + [bytes.fromhex(t["authdigest"])[::-1] for t in tpl.get("transactions", [])]
    auth_root = zv.auth_data_root(auths)
    return {
        "merkleroot": zv.merkle_root(txids),
        "authdataroot": auth_root,
        "blockcommitmentshash": zv.block_commitments(roots["chainhistoryroot"], auth_root),
        "chainhistoryroot": roots["chainhistoryroot"],
    }


# ----------------------------------------------------------------- self-test
def selftest(rpc) -> int:
    tip = int(rpc("getblockcount", []))
    checked = ok = ok_auth = 0
    for height in range(tip, tip - 30, -1):
        bh = rpc("getblockhash", [height])
        blk = rpc("getblock", [bh, 2])
        txobj = blk["tx"][0]                      # verbosity 2 includes authdigest
        raw = bytes.fromhex(txobj["hex"])
        try:
            tx = zv.parse_transparent(raw)
        except Exception:
            continue
        if tx["version"] != 0x80000006:
            continue
        if not zv.is_transparent_only(tx):
            continue
        checked += 1
        rebuilt = zv.serialize_transparent_tx(tx)
        same_bytes = rebuilt == raw
        same_txid = zv.txid_v6(tx)[::-1].hex() == txobj["txid"]
        same_auth = zv.auth_digest_v6(tx)[::-1].hex() == txobj.get("authdigest")
        if same_auth:
            ok_auth += 1
        if same_bytes and same_txid:
            ok += 1
        else:
            print(f"  h={height} FAILED: bytes={same_bytes} txid={same_txid}")
        if not same_auth:
            print(f"  h={height} auth digest mismatch: ours={zv.auth_digest_v6(tx)[::-1].hex()[:20]}… "
                  f"rpc={str(txobj.get('authdigest'))[:20]}…")
    print(f"[selftest] real transparent-coinbase regression: bytes+txid {ok}/{checked}, auth digest {ok_auth}/{checked}")
    return 0 if ok == checked and ok_auth == checked and checked else 1


def main() -> int:
    rpc = make_rpc()
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return selftest(rpc)
    miner = sys.argv[1] if len(sys.argv) > 1 else ""
    pct = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    pool = sys.argv[3] if len(sys.argv) > 3 else ""
    tpl = rpc("getblocktemplate", [{"capabilities": ["coinbasetxn"]}])
    print(f"template height={tpl['height']}  txs={len(tpl.get('transactions', []))}")
    if not miner or not pool:
        return selftest(rpc)
    built = build_dynamic_coinbase(tpl, miner, pool, pct)
    print(f"subsidy={built['subsidy']/1e8:.8f}  fees={built['fees']/1e8:.8f} ZEC")
    print(f"split: miner={built['miner_net']/1e8:.8f}  pool({pct}%)={built['pool_fee']/1e8:.8f}  "
          f"Lockbox={built['lockbox_zat']/1e8:.8f} ZEC")
    print(f"coinbase bytes={len(built['raw'])}  txid={built['txid'][::-1].hex()}")
    print(f"auth_digest={built['auth_digest'][::-1].hex()}")
    total = built["miner_net"] + built["pool_fee"] + built["lockbox_zat"]
    print(f"check: outputs total={total/1e8:.8f} <= cap (0.88*subsidy + fees)="
          f"{(built['subsidy']*88//100 + built['fees'])/1e8:.8f} -> "
          f"{total <= built['subsidy']*88//100 + built['fees']}")
    print(f"check: re-parse matches -> {zv.serialize_transparent_tx(zv.parse_transparent(built['raw'])) == built['raw']}")
    roots = block_roots(tpl, built["txid"], built["auth_digest"])
    print("If this coinbase is used, the header must commit to:")
    print(f"  merkleroot           = {roots['merkleroot'][::-1].hex()}")
    print(f"  blockcommitmentshash = {roots['blockcommitmentshash'][::-1].hex()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
