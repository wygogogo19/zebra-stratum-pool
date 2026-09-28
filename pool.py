#!/usr/bin/env python3
"""
ZEC Solo Stratum V1 bridge (non-custodial) - RobotBase

Design principles
--------
1. The pool holds no funds, keeps no ledger and makes no payouts: the coinbase is assembled by Zebra from
   the node-level [mining] miner_address; the pool only forwards that template to miners, and submits the
block back to Zebra when a share meets the network target.
   2. Every miner therefore shares one miner_address (private-pool semantics). Per-miner accounting would
need a rewritten coinbase, which this implementation deliberately does not do.
   3. Share validation only checks that the block header's double-SHA256 is at or below the share target;
the Equihash solution is left to Zebra, which enforces consensus rules at submitblock time (acceptable
for a private pool mining with its own hashrate).
   4. **The coinbase Zebra provides is never modified**: the template's `coinbasetxn` was assembled by Zebra
   from the node-level `miner_address`, and the header's `blockcommitmentshash` is bound to it; changing the
   coinbase (for example to insert an extraNonce) breaks that commitment and the block would be rejected.
   This implementation therefore uses `coinb1 = the whole coinbase`, `coinb2 = empty`, `extranonce2_size = 0`
   and relies on Zcash's 32-byte nonce (2^256, plenty) plus ntime for the search space. Firmware that insists
on a non-zero extranonce2 needs the full 'insert and recompute the commitments' path.
Zcash block header (140 bytes since NU5)
    version(4, LE) | prevhash(32) | merkleroot(32) | blockcommitments(32)
    | time(4, LE) | bits(4, LE) | nonce(32)
Note: the solution (1344 B) is not inside the header - it is a separate block field; the header hash is the PoW hash.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
import struct
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# Mode 2 (on-chain dynamic split) coinbase / v6 digest modules; when missing we fall back to Zebra's own coinbase
try:
    import build_coinbase
    import zcash_v6 as zv
    _MODE2_MODULES = True
except Exception:  # noqa: BLE001
    build_coinbase = None      # type: ignore[assignment]
    zv = None                  # type: ignore[assignment]
    _MODE2_MODULES = False

# Equihash 200,9 difficulty-1 target (matches Miningcore's Zcash definition)
# Share difficulty 1 == 2^13 = 8192 hashes (the Zcash pool convention, and the same
# constant HASHRATE_FACTOR uses). The previous 0x0007ffff… = 2^251 made difficulty 1
# only 32 hashes, i.e. 256x off — a real ASIC's shares were then 256x "too easy".
DIFF1_TARGET = 1 << 243
MAX_TARGET = (1 << 256) - 1

log = logging.getLogger("zecpool")


# Internal test rigs of this pool (loopback CPU rig, whitelabel rigs, dev probes). These labels
# are always treated as internal, even if a deploy script later rewrites config.json without
# them: otherwise the pool's own miner leaks back into the public counters and the worker list.
INTERNAL_WORKER_LABELS = ("local.", "u1")


EQUIHASH_SOLUTION_HEX = 2688      # Equihash(200,9) solution = 1344 bytes = 2688 hex chars


def pick_solution(params: list[Any]) -> Any:
    """Pick the real solution out of the mining.submit params (by length, no manual configuration).

    * Most Zcash firmware: [worker, job_id, ntime, nonce, solution] (5 params)
    * BTC-style clients: [worker, job_id, ntime, nonce, extranonce2, solution] (6 params)
    * Some firmware counts the CompactSize length prefix (fd4005) as part of the solution (2694 hex chars)

    So take the longest valid-hex string of length >= 2688 as the solution; if none is found fall back to the
    last parameter and let the header construction / validation decide (keeps the old behaviour, adds no new reject path).
    """
    best: Any = None
    for item in params:
        if not isinstance(item, str) or len(item) < EQUIHASH_SOLUTION_HEX or len(item) % 2:
            continue
        try:
            bytes.fromhex(item)
        except ValueError:
            continue
        if best is None or len(item) > len(best):
            best = item
    if best is not None:
        return best
    return params[-1] if params else ""


class ZebraRPC:
    """Zebra JSON-RPC client (cookie file, or user/password auth)."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.url = cfg["url"]
        self.cookie_file = cfg.get("cookie_file")
        self.user = cfg.get("user", "")
        self.password = cfg.get("password", "")
        self._id = 0

    def _auth_header(self) -> str:
        user, pwd = self.user, self.password
        if self.cookie_file and os.path.exists(self.cookie_file):
            with open(self.cookie_file, "r", encoding="utf-8") as fh:
                user, _, pwd = fh.read().strip().partition(":")
        token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        return f"Basic {token}"

    def call(self, method: str, params: list[Any] | None = None, timeout: float = 30.0) -> Any:
        self._id += 1
        payload = json.dumps(
            {"jsonrpc": "1.0", "id": self._id, "method": method, "params": params or []}
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={"content-type": "text/plain", "Authorization": self._auth_header()},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        if body.get("error"):
            raise RuntimeError(f"{method} failed: {body['error']}")
        return body.get("result")


def dsha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def target_from_difficulty(difficulty: float) -> int:
    if difficulty <= 0:
        difficulty = 1e-9
    return min(max(int(DIFF1_TARGET / difficulty), 1), MAX_TARGET)


def hash_meets_target(header_hash: bytes, target: int) -> bool:
    return int.from_bytes(header_hash, "little") <= target


def merkle_root(coinbase_hash: bytes, branch: list[bytes]) -> bytes:
    root = coinbase_hash
    for node in branch:
        root = dsha256(root + node)
    return root


def hex2bytes_reversed(h: str) -> bytes:
    """Display-order (big-endian) hex -> header internal byte order (32-byte reversal)."""
    return bytes.fromhex(h)[::-1]


def swab32(h: str) -> str:
    """Byte-swap in 4-byte words (the internal order miner firmware normally uses)."""
    raw = bytes.fromhex(h)
    out = bytearray()
    for i in range(0, len(raw), 4):
        out += raw[i : i + 4][::-1]
    return out.hex()


def coinbase_output_sum(raw: bytes) -> int:
    """Decode the total transparent output value from raw coinbase bytes.

    Background (2026-09-23): Zebra's getblocktemplate does NOT return coinbasevalue (it only returns
    coinbasetxn), and older code read tpl["coinbasevalue"] directly -> always 0, so the job log kept
    printing "coinbase=0.00000000 ZEC", which reads like "the block pays nothing". This parses the Zcash
    v4/v5 transaction format and sums every transparent output (= block subsidy + fees).

    Layout: version(4, LE) [v4+: versionGroupId(4)] [v5+: branchId(4) + lockTime(4) + expiryHeight(4)]
          → inputsCount(varint) → inputs(prevout36 + scriptLen(varint) + script + sequence4)
          → outputsCount(varint) → outputs(value8 LE + scriptLen(varint) + script) …
    """
    try:
        b = raw
        ver = int.from_bytes(b[0:4], "little")
        off = 4
        if ver & 0x80000000:                    # overwintered (v4 and later)
            off += 4                            # versionGroupId
            if (ver & 0x7FFFFFFF) >= 5:
                off += 12                       # consensusBranchId + lockTime + expiryHeight

        def rv(o: int):
            v = b[o]; o += 1
            if v < 0xFD: return v, o
            if v == 0xFD: return int.from_bytes(b[o:o + 2], "little"), o + 2
            if v == 0xFE: return int.from_bytes(b[o:o + 4], "little"), o + 4
            return int.from_bytes(b[o:o + 8], "little"), o + 8

        nin, off = rv(off)
        for _ in range(nin):
            off += 36                           # prevout(32+4)
            sl, off = rv(off); off += sl + 4    # script + sequence
        nout, off = rv(off)
        total = 0
        for _ in range(nout):
            total += int.from_bytes(b[off:off + 8], "little"); off += 8
            sl, off = rv(off); off += sl        # value + script
        return total
    except Exception:                           # noqa: BLE001
        return 0

def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


@dataclass
class Job:
    job_id: str
    height: int
    version: int
    prevhash_display: str
    bits: str
    curtime: int
    commitments_hash: bytes
    coinb1: bytes
    coinb2: bytes
    merkle_branch: list[bytes]
    merkle_root: bytes = b""
    template_txs: list[str] = field(default_factory=list)
    coinbase_value: int = 0            # total transparent outputs (shielded ones cannot be read off-chain)
    subsidy_miner: float = 0.0         # miner subsidy (ZEC), from Zebra getblocksubsidy
    subsidy_total: float = 0.0         # total block subsidy (ZEC)
    # Mode 2: this job carries the pool-built coinbase (miner / pool / protocol outputs) verbatim
    coinbase_raw: bytes = b""
    mode2_address: str = ""
    generated_at: float = field(default_factory=time.time)

    def notify_params(self, clean: bool = True) -> list[Any]:
        """Zcash SV1 mining.notify parameter order:
        [job_id, version, prevhash, merkleroot, reserved(=commitments), ntime, bits, clean]

        **Every field goes out in the header's internal (standard) byte order**: Z15 firmware writes the hex
        string it receives byte-for-byte into the 140-byte header - proven by reverse-engineering the
        Equihash(200,9) verifier (see equihash_verify.py). The standard header encoding is therefore required:
          version  -> little endian (4 bytes as "04000000")
          prevhash -> fully byte-reversed (internal order)
          ntime    -> little endian
          bits     -> little endian
        merkleroot / reserved are already in internal order and go out as-is.
        A byte-order mistake here makes every share and every real block invalid.
        """
        return [
            self.job_id,
            struct.pack("<I", self.version).hex(),
            bytes.fromhex(self.prevhash_display)[::-1].hex(),
            self.merkle_root.hex(),
            self.commitments_hash.hex(),
            struct.pack("<I", self.curtime).hex(),
            bytes.fromhex(self.bits)[::-1].hex(),
            clean,
        ]


class JobManager:
    """Pull templates from Zebra GBT and turn them into SV1 jobs."""

    def __init__(self, rpc: ZebraRPC, cfg: dict[str, Any]) -> None:
        self.rpc = rpc
        self.poll_seconds = float(cfg.get("template_poll_seconds", 5))
        self.current: Job | None = None
        self.previous: Job | None = None      # keep the previous job: submissions during a switch stay valid
        # No extraNonce is inserted into the coinbase: Zebra's version is used verbatim and the search
        # space comes from the 32-byte nonce (see the module header).
        self.extranonce1_size = 4
        self.extranonce2_size = 0
        self._seq = 0
        self._current_fingerprint: tuple | None = None
        self.last_template: dict[str, Any] | None = None   # mode 2 rebuilds its own coinbase from this
        self._lock = asyncio.Lock()

    async def refresh(self) -> Job | None:
        async with self._lock:
            try:
                tpl = await asyncio.get_running_loop().run_in_executor(
                    None, self.rpc.call, "getblocktemplate", []
                )
                # 2026-09-23: Zebra's GBT has no coinbasevalue, so the subsidy comes from getblocksubsidy;
                # the miner's share is usually a shielded output and cannot be read off-chain (only funding streams are transparent).
                try:
                    _sub = await asyncio.get_running_loop().run_in_executor(
                        None, self.rpc.call, "getblocksubsidy", []
                    ) or {}
                except Exception:
                    _sub = {}
            except Exception as exc:
                log.warning("getblocktemplate failed: %s", exc)
                return None
            self.last_template = tpl
            fingerprint = self._fingerprint(tpl)
            # Reuse the same job_id while the template is unchanged, so miners do not reset on every poll
            if (
                fingerprint is not None
                and self.current is not None
                and fingerprint == self._current_fingerprint
            ):
                return self.current
            job = self._build_job(tpl, _sub)
            if job:
                self._current_fingerprint = fingerprint
                log.info(
                    "new job height=%s tx=%s subsidy=%.8f ZEC (miner %.8f - transparent %.8f)",
                    job.height,
                    len(job.template_txs),
                    job.subsidy_total or (job.subsidy_miner + job.coinbase_value / 1e8),
                    job.subsidy_miner,
                    job.coinbase_value / 1e8,
                )
            if job is not None and job is not self.current:
                self.previous = self.current
            self.current = job
            return job

    @staticmethod
    def _fingerprint(tpl: dict[str, Any]) -> tuple | None:
        """Template identity: height + prevhash + tx set + commitment root. A new job is needed only on change."""
        try:
            return (
                int(tpl["height"]),
                tpl["previousblockhash"],
                tpl.get("blockcommitmentshash") or tpl.get("finalsaplingroothash"),
                tuple(t["hash"] for t in tpl.get("transactions", [])),
            )
        except Exception:
            return None

    def _build_job(self, tpl: dict[str, Any], sub: dict | None = None) -> Job | None:
        try:
            coinbase_hex: str = tpl["coinbasetxn"]["data"]
            commitments = tpl.get("blockcommitmentshash") or tpl.get("finalsaplingroothash")
            if not commitments:
                log.error("template is missing blockcommitmentshash / finalsaplingroothash")
                return None
            txs = [t["data"] for t in tpl.get("transactions", [])]
            branch = [hex2bytes_reversed(t["hash"]) for t in tpl.get("transactions", [])]

            raw = bytes.fromhex(coinbase_hex)
            coinb1 = raw      # the whole coinbase, verbatim
            coinb2 = b""      # nothing is split off
            root = merkle_root(dsha256(raw), branch)

            self._seq += 1
            return Job(
                job_id=f"{int(tpl['height']):x}{self._seq:04x}",
                height=int(tpl["height"]),
                version=int(tpl["version"]),
                prevhash_display=tpl["previousblockhash"],
                bits=tpl["bits"],
                curtime=int(tpl.get("curtime") or time.time()),
                commitments_hash=hex2bytes_reversed(commitments),
                coinb1=coinb1,
                coinb2=coinb2,
                merkle_branch=branch,
                merkle_root=root,
                template_txs=txs,
                # 2026-09-23 fix: Zebra returns no coinbasevalue, so decode the real amount from coinbasetxn
                coinbase_value=coinbase_output_sum(raw) or int(tpl.get("coinbasevalue") or 0),
                subsidy_miner=float((sub or {}).get("miner") or 0.0),
                subsidy_total=float((sub or {}).get("totalblocksubsidy") or 0.0),
            )
        except Exception as exc:
            log.exception("job construction failed: %s", exc)
            return None

class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.extranonce1 = os.urandom(4).hex()
        self.worker = ""
        self.difficulty = 1.0
        self.subscribed = False
        self.authorized = False
        self.shares = 0
        self.accepted = 0
        self.rejected = 0
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.reaped = False          # set when the silent-session reaper closed this client
        self.peer = writer.get_extra_info("peername")
        # P2 vardiff: share timestamps of the last minute + the grace window around a difficulty switch
        self.share_times: list[float] = []
        self.low_diff_times: list[float] = []      # low_difficulty reject timestamps (drive difficulty adaptation)
        self.diff_locked = False                   # miner keeps its own difficulty (ignore set_difficulty)
        self.last_vardiff = time.time()
        self.grace_difficulty = 0.0
        self.grace_until = 0.0
        self.fixed_difficulty = False
        # Mode 2: this miner's own payout address and dedicated job (its coinbase carries the 99/1 split)
        self.mode2_address = ""
        self.mode2_job: Job | None = None
        self.mode2_prev_job: Job | None = None
        self.mode2_prev_jobs: "deque[Job]" = deque(maxlen=6)   # keep recent generations to absorb job rotation

    async def send(self, obj: dict[str, Any]) -> None:
        self.writer.write((json.dumps(obj) + "\n").encode())
        await self.writer.drain()

    async def notify_difficulty(self) -> None:
        await self.send({"id": None, "method": "mining.set_difficulty", "params": [self.difficulty]})

    async def notify_job(self, job: Job, clean: bool = True) -> None:
        await self.send({"id": None, "method": "mining.notify", "params": job.notify_params(clean)})


class PoolServer:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.rpc = ZebraRPC(cfg["zebra"])
        self.jobs = JobManager(self.rpc, cfg)
        self.clients: set[Client] = set()
        self.default_difficulty = float(cfg.get("default_difficulty", 1.0))
        # ---- P5 heartbeat broadcast: re-send the current job to subscribed miners every N seconds ----
        self.heartbeat_seconds = float(cfg.get("heartbeat_seconds", 60))
        # ---- P2 vardiff (dynamic difficulty) ----
        vd = cfg.get("vardiff") or {}
        self.vardiff_enabled = bool(vd.get("enabled", True))
        self.vardiff_min = float(vd.get("min_difficulty", 16))
        self.vardiff_max = float(vd.get("max_difficulty", 131072))
        self.vardiff_target_min = float(vd.get("shares_per_min_min", 8))
        self.vardiff_target_max = float(vd.get("shares_per_min_max", 15))
        self.vardiff_interval = float(vd.get("interval_seconds", 30))
        self.vardiff_grace = float(vd.get("grace_seconds", 45))
        self.vardiff_warmup = float(vd.get("warmup_seconds", 45))
        self.vardiff_fixed_prefixes = tuple(vd.get("fixed_worker_prefixes", ["local."]))
        # Reject-rate feedback: when a rig mines at a lower difficulty than we send (a rental panel's static
        # difficulty, or firmware ignoring set_difficulty) we see many low_difficulty rejects. Once the ratio
        # crosses the threshold we step down, so the pool's target matches what the miner actually solves.
        self.vardiff_reject_ratio = float(vd.get("reject_ratio_threshold", 0.30))
        self.vardiff_min_samples = int(vd.get("min_samples", 6))
        # Real-solution check: below this share-difficulty threshold we additionally verify the submitted
        # solution with the Equihash(200,9) verifier (CPU-rig channel only; not for high-difficulty ASIC traffic).
        self.verify_solution_below = float(cfg.get("verify_solution_below_difficulty", 0.0))
        # Per-worker learned difficulty, so a reconnect does not re-converge from scratch
        self.learned_difficulty: dict[str, float] = {}
        self.learned_path = cfg.get("learned_difficulty_file",
                                    os.path.join(os.path.dirname(
                                        cfg.get("status_file", "/var/lib/zecpool/status.json")),
                                        "worker_difficulty.json"))
        try:
            self.learned_difficulty = {str(k): float(v)
                                       for k, v in json.load(
                                           open(self.learned_path, encoding="utf-8")).items()}
            log.info("loaded remembered difficulty for %d miners", len(self.learned_difficulty))
        except Exception:  # noqa: BLE001
            self.learned_difficulty = {}
        # ---- Mode 2: on-chain dynamic split (miner 99% / pool 1% / protocol lockbox fixed) ----
        m2 = cfg.get("mode2") or {}
        self.mode2_enabled = bool(m2.get("enabled", False))
        self.mode2_fee_address = str(m2.get("pool_fee_address", "")).strip()
        self.mode2_fee_pct = float(m2.get("fee_pct", 1.0))
        self.mode2_whitelist = tuple(m2.get("whitelist_prefixes", ["local."]))
        self._mode2_seq = 0
        if self.mode2_enabled and not self.mode2_fee_address:
            log.error("mode2.enabled=true but pool_fee_address is not configured; disabling mode 2")
            self.mode2_enabled = False
        # -- dashboard statistics --
        self.started_at = time.time()
        self.status_file = cfg.get("status_file", "/var/lib/zecpool/status.json")
        self.status_push_url = str(cfg.get("status_push_url", "") or "")
        self.status_push_token = str(cfg.get("status_push_token", "") or "")
        self.share_events: list[tuple[float, str, float]] = []   # (ts, worker, difficulty)
        self.worker_total: dict[str, int] = {}
        self.worker_last: dict[str, float] = {}
        self.history: list[dict[str, float]] = []                # hashrate curve samples
        self.shares_total = 0
        self.shares_rejected = 0
        # -- share counters are persisted: a restart must not reset the public history --
        self.counter_file = cfg.get("counter_file", "/var/lib/zecpool/counters.json")
        try:
            _c = json.load(open(self.counter_file, encoding="utf-8"))
            self.shares_total = int(_c.get("shares_total", 0))
            self.shares_rejected = int(_c.get("shares_rejected", 0))
            log.info("restored share counters: %d accepted / %d rejected",
                     self.shares_total, self.shares_rejected)
        except Exception:  # noqa: BLE001
            pass
        # -- internal test rigs: excluded from the public statistics --
        # config plus built-in defaults, so a deploy script rewriting config cannot leak the rig back
        _internal_cfg = [str(p).strip().lower() for p in cfg.get("internal_worker_prefixes", []) if str(p).strip()]
        self.internal_prefixes = tuple(dict.fromkeys(_internal_cfg + list(INTERNAL_WORKER_LABELS)))
        self.internal_accepted = 0
        self.internal_rejected = 0
        self.reject_reasons: dict[str, int] = {}
        self.recent_sessions: list[dict[str, Any]] = []   # keep just-closed sessions for debugging

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # P1: disable Nagle (removes 40 ms-class coalescing delay) and enable TCP keepalive, so
        # mining.notify / mining.submit travel unbuffered.
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # 2026-09-28: one-way link failures leave half-open connections and the OS default
                # keepalive (2 h idle) detects them far too late, so zombie sessions keep counting
                # as online. Probe after 60 s idle, every 15 s, give up after 4 misses (~2 min),
                # complementing the silent-session reaper below.
                for _opt, _val in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
                    _o = getattr(socket, _opt, None)
                    if _o is not None:
                        sock.setsockopt(socket.IPPROTO_TCP, _o, _val)
        except Exception:  # noqa: BLE001
            pass
        client = Client(reader, writer)
        self.clients.add(client)
        log.info("miner connected %s", client.peer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError,
                        ConnectionResetError, asyncio.IncompleteReadError):
                    # Over-long line (>64 KB without a newline): asyncio raises LimitOverrunError/ValueError.
                    # Drop this one client; it must not become an unhandled exception.
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Public scanners / broken firmware send non-UTF-8 or non-JSON bytes: skip the line instead
                    # of turning the connection into a traceback (the ERROR observed in the logs on 2026-09-27).
                    continue
                client.last_activity = time.time()   # any inbound message counts as alive (reaper criterion)
                await self.dispatch(client, msg)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(client)
            if not getattr(client, "reaped", False):
                # only record the disconnect here if the reaper did not already do it
                self.recent_sessions.append({
                    "worker": client.worker or "(unauthorized)",
                    "extranonce1": client.extranonce1,
                    "difficulty": client.difficulty,
                    "connected_seconds": round(time.time() - client.connected_at, 1),
                    "accepted": client.accepted,
                    "rejected": client.rejected,
                    "peer": str(client.peer[0]) if client.peer else None,
                    "state": "closed",
                    "ended_at": time.time(),
                })
                self.recent_sessions = self.recent_sessions[-10:]
                log.info("miner disconnected %s (shares=%s)", client.peer, client.shares)
            writer.close()

    async def dispatch(self, client: Client, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or []

        if method == "mining.subscribe":
            client.subscribed = True
            client.difficulty = self.default_difficulty
            await client.send(
                {
                    "id": mid,
                    "result": [
                        [["mining.set_difficulty", "1"], ["mining.notify", "1"]],
                        client.extranonce1,
                        self.jobs.extranonce2_size,
                    ],
                    "error": None,
                }
            )
            await client.notify_difficulty()
            if self.jobs.current:
                await client.notify_job(self.jobs.current)
            return

        if method == "mining.authorize":
            client.worker = params[0] if params else ""
            client.fixed_difficulty = bool(
                self.vardiff_fixed_prefixes and client.worker.startswith(self.vardiff_fixed_prefixes)
            )
            # ---- Mode 2: strictly reject invalid addresses (worker must be <t1/t3 address>.<rig name>) ----
            mode2_bypass = bool(
                self.mode2_whitelist and client.worker.startswith(self.mode2_whitelist)
            )
            if self.mode2_enabled and _MODE2_MODULES and not mode2_bypass:
                addr = self._mode2_parse_worker(client.worker)
                if addr is None:
                    log.warning("authorize rejected (Invalid ZEC address) worker=%s peer=%s",
                                client.worker, client.peer)
                    await client.send({"id": mid, "result": False,
                                       "error": [20, "Invalid ZEC address", None]})
                    try:
                        client.writer.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                if addr == self.mode2_fee_address:
                    # Diligence red line: if the miner's payout address equals the pool's 1% fee address, the on-chain
                    # 99/1 split is just moving coins between the operator's own pockets (it proves nothing and looks
                    # like a related-party transfer). Rejecting it here makes the pool/miner separation a property of
                    log.warning("authorize rejected (miner address equals the pool fee address; 99/1 separation violated)"
                                " worker=%s peer=%s addr=%s",
                                client.worker, client.peer, addr)
                    await client.send({"id": mid, "result": False,
                                       "error": [20, "Mining address must differ from pool fee address", None]})
                    try:
                        client.writer.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                client.mode2_address = addr
                client.fixed_difficulty = False
                client.mode2_job = await self._mode2_refresh(client)
                if client.mode2_job is None:
                    await client.send({"id": mid, "result": False,
                                       "error": [20, "Pool is building your payout coinbase, retry", None]})
                    return
                log.info("mode2 authorize worker=%s payout=%s fee=%.2f%% -> pool %s",
                         client.worker, addr, self.mode2_fee_pct, self.mode2_fee_address)
            # Start from the remembered difficulty, skipping the re-convergence after every reconnect
            if client.worker in self.learned_difficulty:
                client.difficulty = self.learned_difficulty[client.worker]
                client.last_vardiff = time.time()
                # A remembered value below the default means this rig keeps its own difficulty (it ignores
                # set_difficulty): lock it, so a reconnect does not repeat the upward probe that costs rejects.
                if client.difficulty < self.default_difficulty:
                    client.diff_locked = True
            await client.send({"id": mid, "result": True, "error": None})
            client.authorized = True
            log.info("authorize worker=%s difficulty=%.3f%s", client.worker, client.difficulty,
                     " (fixed-difficulty whitelist)" if client.fixed_difficulty else "")
            # Subscribe sent the default difficulty; authorize may change it (remembered difficulty / mode 2),
            # so set_difficulty is re-sent - otherwise the miner keeps the old target and wastes real solutions.
            await client.notify_difficulty()
            if client.mode2_job is not None:
                await client.notify_job(client.mode2_job, clean=True)
            return

        if method == "mining.submit":
            await self.handle_submit(client, mid, params)
            return

        if method == "mining.extranonce.subscribe":
            await client.send({"id": mid, "result": True, "error": None})
            return

        await client.send({"id": mid, "result": None, "error": [20, "unsupported", None]})

    async def handle_submit(self, client: Client, mid: Any, params: list[Any]) -> None:
        client.last_activity = time.time()
        job = client.mode2_job or self.jobs.current
        if not job or len(params) < 5:
            client.rejected += 1
            self._count_share(client, False)
            self._bump_reason("bad_params", client)
            await client.send({"id": mid, "result": False, "error": [20, "bad params", None]})
            return
        _, job_id, ntime, nonce = params[0], params[1], params[2], params[3]
        solution = pick_solution(params[4:])
        if len(params) > 5:
            # Compatibility observation: log the 6-parameter firmware shape (we advertise extranonce2_size=0)
            log.info("submit shape: %d params worker=%s", len(params), client.worker)
        if job_id != job.job_id:
            # Right after a job is broadcast a miner often still submits the previous job: if that job is one
            # we issued, validate against its fields (otherwise those shares would be wrongly rejected as stale).
            previous = self.jobs.previous
            if previous is not None and job_id == previous.job_id:
                job = previous
            elif client.mode2_prev_job is not None and job_id == client.mode2_prev_job.job_id:
                job = client.mode2_prev_job
            else:
                hist = next((j for j in client.mode2_prev_jobs if j.job_id == job_id), None)
                if hist is not None:
                    job = hist
                else:
                    client.rejected += 1
                    self._count_share(client, False)
                    self._bump_reason("stale_job", client)
                    await client.send({"id": mid, "result": False, "error": [21, "stale job", None]})
                    return

        try:
            # The solution is part of the header: hash the COMPLETE header, otherwise the
            # value we compare against the target has nothing to do with the real block
            # hash (and real ASIC shares would be rejected as "low difficulty").
            header = self.build_header(job, ntime, nonce, solution, client.extranonce1)
        except Exception as exc:
            log.warning("header construction failed: %s", exc)
            client.rejected += 1
            self._count_share(client, False)
            self._bump_reason("bad_share", client)
            await client.send({"id": mid, "result": False, "error": [20, "bad share", None]})
            return

        header_hash = dsha256(header)
        # After a vardiff switch, in-flight shares are still accepted at the old (lower) difficulty for a grace period
        effective = self._accept_difficulty(client)
        if not hash_meets_target(header_hash, target_from_difficulty(effective)):
            client.rejected += 1
            self._count_share(client, False)
            self._bump_reason("low_difficulty", client)
            client.low_diff_times.append(time.time())
            log.warning(
                "DIAG low_difficulty worker=%s mode2=%s job_sub=%s job_cur=%s ntime=%s nonce=%s "
                "e1=%s header=%s hash=%s target=%x",
                client.worker, bool(client.mode2_address), job_id, job.job_id, ntime, nonce,
                client.extranonce1, header.hex()[:64], header_hash[::-1].hex(),
                target_from_difficulty(effective),
            )
            await client.send({"id": mid, "result": False, "error": [23, "low difficulty share", None]})
            await self._maybe_vardiff(client)      # reject-rate feedback: may need to step down
            return

        client.shares += 1
        client.accepted += 1
        client.last_activity = time.time()
        # CPU-rig channel: below the threshold, verify the submitted solution for real (Equihash 200,9)
        if self.verify_solution_below > 0 and effective <= self.verify_solution_below:
            try:
                import equihash_verify as _ev
                if not _ev.verify(header[:140], bytes.fromhex(solution)):
                    log.warning("DIAG invalid_solution worker=%s height=%s solution failed the Equihash check",
                                client.worker, job.height)
                    client.accepted -= 1
                    client.rejected += 1
                    self._count_share(client, False)
                    self._bump_reason("invalid_solution", client)
                    await client.send({"id": mid, "result": False,
                                       "error": [20, "invalid equihash solution", None]})
                    return
                log.info("real-solution check passed worker=%s height=%s - Valid Equihash 200,9 solution",
                         client.worker, job.height)
            except ImportError:
                pass
        self._count_share(client, True)
        client.share_times.append(time.time())
        # Internal test rigs never reach the public view (rolling hashrate / worker list)
        if not self._is_internal_worker(client.worker):
            self.share_events.append((time.time(), client.worker or "unknown", effective))
            self.worker_total[client.worker or "unknown"] = (
                self.worker_total.get(client.worker or "unknown", 0) + 1)
            self.worker_last[client.worker or "unknown"] = time.time()
        await client.send({"id": mid, "result": True, "error": None})
        await self._maybe_vardiff(client)

        if hash_meets_target(header_hash, self._target_from_bits(job.bits)):
            log.warning("network difficulty hit height=%s worker=%s -> submitblock", job.height, client.worker)
            await self.submit_block(job, ntime, nonce, solution, client.extranonce1)
        else:
            log.info(
                "share accepted worker=%s height=%s hash=%s",
                client.worker,
                job.height,
                header_hash[::-1].hex()[:16],
            )

    def build_header(self, job: Job, ntime: str, nonce: str, solution: str | None = None,
                     extranonce1: str = "") -> bytes:
        """Assemble the block header from Zebra's verbatim coinbase plus the miner's ntime/nonce.

        Note: the Zcash (Equihash) block header is version|prevhash|merkleroot|commitments|time|bits|nonce|solution,
        where the solution is a variable-length field (with a length prefix) that IS part of the header; the block hash
        is SHA256d over the complete header including the solution. Omitting it yields a completely different hash -
shares could not be validated correctly and a real block could never be detected.
        nonce: a Z15 submits only 28 bytes; the 32-byte header nonce = pool-sent extranonce1 (4 B) || miner 28 B
        (proven by Equihash verification; padding with four zero bytes was the direct cause of that earlier bug).
        """
        coinbase = job.coinb1 + job.coinb2
        # Mode 2 jobs already fix the merkle root through the pool-built coinbase; node-built coinbase jobs
        # compute it from coinb1/coinb2 plus the branch (both give the same result).
        root = job.merkle_root or merkle_root(dsha256(coinbase), job.merkle_branch)
        submitted = bytes.fromhex(nonce)
        if len(submitted) == 32:
            nonce_bytes = submitted
        else:
            e1 = bytes.fromhex(extranonce1) if extranonce1 else b"\x00" * 4
            nonce_bytes = (e1 + submitted)[-32:].rjust(32, b"\x00")
        fixed = (
            struct.pack("<I", job.version)
            + hex2bytes_reversed(job.prevhash_display)
            + root
            + job.commitments_hash
            # ntime is echoed back by the miner: notify sends it as a little-endian hex string, so it must be
            # written back byte-for-byte, not re-packed as an integer (that would reverse it twice).
            + bytes.fromhex(ntime.rjust(8, "0"))
            + bytes.fromhex(job.bits)[::-1]
            + nonce_bytes
        )
        if solution is None:
            return fixed
        sol = bytes.fromhex(solution)
        # The solution submitted by Z15 firmware already carries the CompactSize length prefix (fd4005 = 1344).
        # Wrapping another prefix around it produces an invalid block - a real block would be lost outright.
        if len(sol) > 3 and sol[0] == 0xFD and int.from_bytes(sol[1:3], "little") == len(sol) - 3:
            sol = sol[3:]
        return fixed + varint(len(sol)) + sol

    @staticmethod
    def _target_from_bits(bits: str) -> int:
        raw = int(bits, 16)
        exponent = raw >> 24
        mantissa = raw & 0x007FFFFF
        return mantissa * (1 << (8 * (exponent - 3)))

    async def submit_block(self, job: Job, ntime: str, nonce: str, solution: str,
                           extranonce1: str = "") -> None:
        # Mode 2 jobs use the pool-built coinbase (with the 99/1 split); everything else uses the node's
        coinbase = job.coinbase_raw or (job.coinb1 + job.coinb2)
        # build_header now appends the length-prefixed solution itself, so the block is
        # simply the full header followed by the transactions.
        block = self.build_header(job, ntime, nonce, solution, extranonce1)
        txs = [coinbase] + [bytes.fromhex(t) for t in job.template_txs]
        block += varint(len(txs)) + b"".join(txs)
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, self.rpc.call, "submitblock", [block.hex()]
            )
            log.warning("submitblock returned: %s (None = accepted)", result)
        except Exception as exc:
            log.error("submitblock failed: %s", exc)

    async def poll_templates(self) -> None:
        """P3 dual-speed probe: a 0.5 s lightweight getbestblockhash, then a full GBT + broadcast on a new block.

        Templates refresh on poll_seconds (only to follow mempool / fee changes), but as soon as bestblockhash
        changes we refresh immediately, cutting the 'a block was found' latency from seconds to sub-second.
        """
        loop = asyncio.get_running_loop()
        last_full = 0.0
        last_best: str | None = None
        last_broadcast = time.time()
        while True:
            started = time.time()
            try:
                best = await loop.run_in_executor(None, self.rpc.call, "getbestblockhash", [])
            except Exception:  # noqa: BLE001
                best = None
            new_block = bool(best and last_best and best != last_best)
            if best:
                last_best = best
            if not (new_block or (started - last_full) >= self.jobs.poll_seconds):
                await asyncio.sleep(0.5)
                continue
            last_full = started
            # Snapshot the current job BEFORE refreshing: JobManager.refresh() replaces
            # self.jobs.current, so previousblockhash must be captured up front.
            prev = self.jobs.current
            before = prev.job_id if prev else None
            prev_prevhash = prev.prevhash_display if prev else None
            job = await self.jobs.refresh()
            if job and job.job_id != before:
                # clean_jobs must mean ONE thing: the previous block changed, so work on
                # the old template is now stale and must be discarded. A mere mempool /
                # transaction-set change is NOT a reason to clean — it only needs a new
                # job (clean=False), otherwise ASICs throw away in-flight work every few
                # seconds and effective hashrate collapses.
                clean = (prev_prevhash is None) or (job.prevhash_display != prev_prevhash)
                if new_block:
                    log.info("probe saw a new block %.8s... -> refreshing the template and broadcasting clean_jobs", str(best))
                for client in list(self.clients):
                    if client.subscribed:
                        try:
                            # Mode 2 clients rebuild "their own coinbase job" on every template change
                            # (the merkle root commits to the 99/1 split coinbase)
                            if self.mode2_enabled and _MODE2_MODULES and client.mode2_address:
                                newjob = await self._mode2_refresh(client)
                                if newjob is not None:
                                    if client.mode2_job is not None:
                                        client.mode2_prev_jobs.append(client.mode2_job)
                                    client.mode2_prev_job = client.mode2_job
                                    client.mode2_job = newjob
                                    await client.notify_job(newjob, clean=clean)
                                continue
                            await client.notify_job(job, clean=clean)
                        except Exception:
                            pass
                last_broadcast = time.time()
            # P5 heartbeat: more than heartbeat_seconds since the last broadcast -> re-send the current job
            # (same content, clean_jobs=false) so NAT/firewalls keep seeing traffic and do not silently drop the
            # long-lived connection during quiet periods (the 2026-09-22 false-alive incident).
            if self.heartbeat_seconds > 0 and (time.time() - last_broadcast) >= self.heartbeat_seconds:
                await self.broadcast_heartbeat()
                last_broadcast = time.time()
            await asyncio.sleep(0.5)

    async def broadcast_heartbeat(self) -> None:
        """Heartbeat broadcast (P5): re-send the current job to subscribed miners, clean_jobs=false.

        The pool only pushes mining.notify when the template changes, so during RPC stalls or quiet gaps the
        connection stays silent and NAT/firewall paths may drop it - presenting as 'process alive, connection
        ESTABLISHED, every share lost locally' (the 2026-09-22 16:39 incident). The heartbeat changes neither
        job_id nor difficulty, so miners do not discard work in progress.
        """
        sent = 0
        for client in list(self.clients):
            if not client.subscribed:
                continue
            try:
                if self.mode2_enabled and _MODE2_MODULES and client.mode2_address:
                    job = client.mode2_job
                else:
                    job = self.jobs.current
                if job is None:
                    continue
                await client.notify_job(job, clean=False)
                sent += 1
            except Exception:  # noqa: BLE001
                pass
        if sent:
            log.info("heartbeat broadcast: %d miners received the current job (clean_jobs=false)", sent)

    # Equihash 200,9: a difficulty-1 share needs 2^13 hashes on average
    HASHRATE_FACTOR = 8192

    # ---------------------------------------------------------------- mode 2
    def _mode2_parse_worker(self, worker: str) -> str | None:
        """`<t1/t3 address>.<rig name>` -> address; None when invalid (strictly rejected)."""
        addr = (worker or "").split(".", 1)[0].strip()
        if len(addr) < 26 or len(addr) > 40 or not addr[0] in ("t",):
            return None
        try:
            build_coinbase.address_to_script(addr)      # base58check + t1/t3 validation
        except Exception:  # noqa: BLE001
            return None
        return addr

    def _mode2_job_id(self, client: Client) -> str:
        self._mode2_seq += 1
        base = self.jobs.current
        return f"{base.job_id if base else '0'}{self._mode2_seq % 0x10000:04x}"

    def _mode2_build_job(self, client: Client, base: Job, tpl: dict) -> Job | None:
        """Build the miner-specific job (with the 99/1 split) from the current template and payout address."""
        try:
            built = build_coinbase.build_dynamic_coinbase(
                tpl, client.mode2_address, self.mode2_fee_address, self.mode2_fee_pct)
        except Exception as exc:  # noqa: BLE001
            log.error("mode 2 coinbase construction failed worker=%s: %s", client.worker, exc)
            return None
        txids = [built["txid"]] + [bytes.fromhex(t["hash"])[::-1]
                                   for t in tpl.get("transactions", [])]
        root = zv.merkle_root(txids)
        return Job(
            job_id=self._mode2_job_id(client),
            height=base.height,
            version=base.version,
            prevhash_display=base.prevhash_display,
            bits=base.bits,
            curtime=base.curtime,
            commitments_hash=base.commitments_hash,
            coinb1=b"",
            coinb2=b"",
            merkle_branch=[],
            merkle_root=root,
            template_txs=base.template_txs,
            coinbase_value=built["miner_total"],
            coinbase_raw=built["raw"],
            mode2_address=client.mode2_address,
        )

    async def _mode2_refresh(self, client: Client) -> Job | None:
        base = self.jobs.current
        tpl = self.jobs.last_template
        if base is None or tpl is None:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._mode2_build_job, client, base, tpl)

    def _accept_difficulty(self, client: Client) -> float:
        """Validation difficulty: during the grace window after a switch, take the lower of new/old (friendlier to the miner)."""
        if client.grace_until > time.time() and client.grace_difficulty > 0:
            return min(client.difficulty, client.grace_difficulty)
        return client.difficulty

    async def _maybe_vardiff(self, client: Client) -> None:
        """P2 vardiff (with **reject-rate feedback**).

        Decision order (evaluated every interval_seconds over a 60 s window):
          1. **reject ratio > threshold** (30% by default) -> step down: the rig really mines at a lower
             difficulty (typical causes: a rental panel's static difficulty, or firmware ignoring set_difficulty);
          2. accepted rate above the target ceiling -> step up; below half the target floor -> step down;
        after a switch the window resets and a grace period keeps in-flight shares from being wrongly rejected.
        The caller records events into share_times / low_diff_times.
        """
        if not self.vardiff_enabled or client.fixed_difficulty:
            return
        now = time.time()
        client.share_times = [t for t in client.share_times if now - t <= 60]
        client.low_diff_times = [t for t in client.low_diff_times if now - t <= 60]
        if now - client.connected_at < self.vardiff_warmup:
            return
        if now - client.last_vardiff < self.vardiff_interval:
            return
        accepted = len(client.share_times)              # accepted in the last minute
        rejected = len(client.low_diff_times)           # low_difficulty rejects in the last minute
        total = accepted + rejected
        ratio = (rejected / total) if total else 0.0
        new = client.difficulty
        reason = ""
        if (total >= self.vardiff_min_samples and ratio > self.vardiff_reject_ratio
                and client.difficulty > self.vardiff_min):
            new = max(client.difficulty / 2, self.vardiff_min)
            # A reject ratio over the threshold means this rig mines at its own (lower) difficulty and ignores
            # what we send. Mark it as self-difficulty: step down only, never up, to avoid oscillating.
            client.diff_locked = True
            reason = f"reject ratio {ratio * 100:.0f}% ({rejected} rejected / {accepted} accepted) -> step down and lock"
        elif not client.diff_locked and accepted > self.vardiff_target_max:
            new = min(client.difficulty * 2, self.vardiff_max)
            reason = f"rate too high ({accepted}/min) -> step up"
        elif not client.diff_locked and accepted < self.vardiff_target_min / 2 \
                and client.difficulty > self.vardiff_min:
            new = max(client.difficulty / 2, self.vardiff_min)
            reason = f"rate too low ({accepted}/min) -> step down"
        client.last_vardiff = now
        if new == client.difficulty:
            return
        old = client.difficulty
        client.grace_difficulty = old
        client.grace_until = now + self.vardiff_grace
        client.difficulty = new
        client.share_times = []          # re-measure after a switch
        client.low_diff_times = []
        self.learned_difficulty[client.worker] = new      # remember this rig's suitable difficulty
        try:
            tmp = self.learned_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.learned_difficulty, fh)
            os.replace(tmp, self.learned_path)
        except Exception:  # noqa: BLE001
            pass
        await client.notify_difficulty()
        log.info("vardiff worker=%s difficulty %.0f -> %.0f (%s, grace %.0fs)",
                 client.worker or "?", old, new, reason, self.vardiff_grace)

    def _is_internal_worker(self, worker: str) -> bool:
        """Internal test-rig detection: local./u1 prefixes, or a rig-name suffix such as .cpurig."""
        w = (worker or "").strip().lower()
        if not w or not self.internal_prefixes:
            return False
        label = w.rsplit(".", 1)[-1]
        return any(w.startswith(p) or label.startswith(p) for p in self.internal_prefixes)

    def _count_share(self, client: "Client", accepted: bool) -> None:
        """Public counters only count external miners; internal test rigs are counted separately for debugging."""
        if self._is_internal_worker(client.worker):
            if accepted:
                self.internal_accepted += 1
            else:
                self.internal_rejected += 1
            return
        if accepted:
            self.shares_total += 1
        else:
            self.shares_rejected += 1

    def _bump_reason(self, reason: str, client: "Client | None" = None) -> None:
        if client is not None and self._is_internal_worker(client.worker):
            return
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1

    def _snapshot(self) -> dict[str, Any]:
        now = time.time()
        window = 60.0
        self.share_events = [e for e in self.share_events if now - e[0] <= 600]
        recent = [e for e in self.share_events if now - e[0] <= window]
        hashrate = sum(self.HASHRATE_FACTOR * d for _, _, d in recent) / window
        per_worker: dict[str, dict[str, float]] = {}
        for ts, worker, diff in self.share_events:
            w = per_worker.setdefault(worker, {"shares_1m": 0.0, "diff_sum": 0.0})
            if now - ts <= window:
                w["shares_1m"] += 1
                w["diff_sum"] += diff
        worker_list = []
        for worker, total in sorted(self.worker_total.items(), key=lambda kv: -kv[1]):
            d = per_worker.get(worker, {"shares_1m": 0.0, "diff_sum": 0.0})
            avg_diff = (d["diff_sum"] / d["shares_1m"]) if d["shares_1m"] else self.default_difficulty
            hr = d["shares_1m"] / window * self.HASHRATE_FACTOR * avg_diff
            last = self.worker_last.get(worker)
            worker_list.append({
                "worker": worker,
                "shares": total,
                "hashrate": round(hr, 2),
                "difficulty": avg_diff,
                "last_share_ago": round(now - last, 1) if last else None,
            })
        job = self.jobs.current
        sessions = []
        internal_sessions = 0
        for c in self.clients:
            if self._is_internal_worker(c.worker):
                internal_sessions += 1
                continue
            sessions.append({
                "worker": c.worker or "(unauthorized)",
                "extranonce1": c.extranonce1,
                "difficulty": c.difficulty,
                "mode2": bool(c.mode2_address),
                "payout": c.mode2_address or "",
                "connected_seconds": round(now - c.connected_at, 1),
                "last_activity_ago": round(now - c.last_activity, 1),
                "accepted": c.accepted,
                "rejected": c.rejected,
                "peer": str(c.peer[0]) if c.peer else None,
                "subscribed": c.subscribed,
                "state": "active",
            })
        self.recent_sessions = [s for s in self.recent_sessions if now - s["ended_at"] <= 600]
        for s in self.recent_sessions:
            # 2026-09-27 fix: closed internal sessions must not reach the public view either (V19 rule -
            # only live connections were filtered before, and this recent_sessions path was missed)
            if self._is_internal_worker(s.get("worker", "")):
                continue
            s["ended_ago"] = round(now - s["ended_at"], 1)
            s["last_activity_ago"] = s["ended_ago"]
            sessions.append(s)
        return {
            "height": job.height if job else 0,
            "job_id": job.job_id if job else None,
            "bits": job.bits if job else None,
            "txs": len(job.template_txs) if job else 0,
            "uptime_seconds": round(now - self.started_at, 1),
            "hashrate": round(hashrate, 2),
            "workers_online": sum(
                1 for c in self.clients
                if getattr(c, "authorized", False) and not self._is_internal_worker(c.worker)),
            "shares_total": self.shares_total,
            "shares_rejected": self.shares_rejected,
            "internal_accepted": self.internal_accepted,
            "internal_rejected": self.internal_rejected,
            "internal_sessions": internal_sessions,
            "valid_rate": (
                round(self.shares_total / (self.shares_total + self.shares_rejected), 4)
                if (self.shares_total + self.shares_rejected) > 0
                else None
            ),
            "shares_1m": len(recent),
            "reject_reasons": dict(self.reject_reasons),
            "sessions": sessions,
            "default_difficulty": self.default_difficulty,
            "worker_list": worker_list,
            "history": self.history[-120:],
            "updated_at": round(now, 1),
        }

    async def _reap_stale_clients(self, limit: float = 300.0) -> int:
        """Reap "zombie" sessions (half-open connections) that went silent, returning the count.

        Background (measured 2026-09-28): when a one-way link failure leaves a client socket
        half-open (the miner has already reconnected, the pool still believes it is online), the
        kernel keepalive is only a backstop. Any inbound message refreshes ``last_activity``;
        a session that has been silent for ``limit`` seconds (default 5 minutes) is closed and
        removed from the public statistics. Shares, counters and settlement logic are untouched:
        a miner that is really working submits every few seconds, so silence for 5 minutes means
        the connection is dead.
        """
        now = time.time()
        reaped = 0
        for c in list(self.clients):
            last = getattr(c, "last_activity", 0.0) or 0.0
            if now - last <= limit:
                continue
            reaped += 1
            try:
                c.reaped = True
                c.writer.close()
            except Exception:  # noqa: BLE001
                pass
            self.clients.discard(c)
            self.recent_sessions.append({
                "worker": c.worker or "(unauthorized)",
                "extranonce1": c.extranonce1,
                "difficulty": c.difficulty,
                "connected_seconds": round(now - c.connected_at, 1),
                "accepted": c.accepted,
                "rejected": c.rejected,
                "peer": str(c.peer[0]) if c.peer else None,
                "state": "reaped",
                "ended_at": now,
            })
            log.warning("reaped silent session worker=%s (inbound silent %.0fs, half-open connection)",
                        c.worker or "?", now - last)
        if reaped:
            self.recent_sessions = self.recent_sessions[-10:]
        return reaped

    def _persist_counters(self) -> None:
        """Persist share counters next to status_file so a restart does not roll the public numbers back."""
        cur = (self.shares_total, self.shares_rejected)
        if cur == getattr(self, "_persisted", None):
            return
        try:
            tmp = self.counter_file + ".tmp"
            os.makedirs(os.path.dirname(self.counter_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"shares_total": self.shares_total,
                           "shares_rejected": self.shares_rejected,
                           "updated_at": time.time()}, fh)
            os.replace(tmp, self.counter_file)
            self._persisted = cur
        except Exception:  # noqa: BLE001
            pass

    async def status_loop(self) -> None:
        os.makedirs(os.path.dirname(self.status_file), exist_ok=True)
        tick = 0
        while True:
            snap = self._snapshot()
            tick += 1
            if tick % 5 == 0:   # reap silent zombie sessions roughly every 10 s
                try:
                    await self._reap_stale_clients()
                except Exception as exc:  # noqa: BLE001
                    log.warning("zombie session reaper failed: %s", exc)
            if tick % 3 == 0:  # sample every ~6 s (about a 10 minute window)
                self.history.append({"ts": snap["updated_at"], "hashrate": snap["hashrate"]})
                snap = self._snapshot()
            self._persist_counters()
            tmp = f"{self.status_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False)
            os.replace(tmp, self.status_file)
            # When deployed as the front engine (P4), push status back to the domestic dashboard so the public
            if self.status_push_url:
                try:
                    body = json.dumps(snap, ensure_ascii=False).encode()
                    req = urllib.request.Request(
                        self.status_push_url, data=body,
                        headers={"Content-Type": "application/json",
                                 "X-Pool-Token": self.status_push_token},
                    )
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda r=req: urllib.request.urlopen(r, timeout=5).read()
                    )
                except Exception:  # noqa: BLE001 - a failed push must not affect local accounting
                    pass
            await asyncio.sleep(2)

    async def serve(self) -> None:
        await self.jobs.refresh()
        host = self.cfg.get("listen_host", "0.0.0.0")
        port = int(self.cfg.get("listen_port", 3032))
        server = await asyncio.start_server(self.handle, host, port)
        log.info(
            "SV1 pool started, listening on %s:%s (current job: %s)",
            host,
            port,
            self.jobs.current.height if self.jobs.current else "none (GBT not ready)",
        )
        asyncio.create_task(self.poll_templates())
        asyncio.create_task(self.status_loop())
        async with server:
            await server.serve_forever()


def main() -> None:
    cfg_path = os.environ.get("ZECPOOL_CONFIG", "/etc/zecpool/config.json")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    logging.basicConfig(
        level=getattr(logging, str(cfg.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(PoolServer(cfg).serve())


if __name__ == "__main__":
    main()
