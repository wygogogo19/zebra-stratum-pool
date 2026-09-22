#!/usr/bin/env python3
"""
ZEC Solo Stratum V1 pool engine (non-custodial) — RobotBase

Design principles
-----------------
1. The pool holds no coins, keeps no payout ledger and moves no funds of its own: zebrad
   supplies the block template, and the coinbase transaction itself pays the miner.
2. Payouts are non-custodial. In mode 2 each miner's own `t`-address is baked into the coinbase
   the pool hands out — 99% to the miner, 1% to the pool's fee address — so there is nothing to
   withdraw. A worker name that is not a valid address is refused at `mining.authorize`: the pool
   will not mine for an account it cannot pay.
3. Share validation checks the block-header double-SHA256 against the share target. The Equihash
   solution is validated by zebrad at `submitblock` time under consensus rules.
4. **zebrad's coinbase is never edited in place.** The template's `coinbasetxn` is bound to its
   `blockcommitmentshash`, so injecting an extraNonce would break the commitment and produce an
   invalid block. The engine therefore either
     (a) reuses zebrad's coinbase byte-for-byte — `coinb1 = whole coinbase`, `coinb2 = ""`,
         `extranonce2_size = 0` — relying on Zcash's 32-byte nonce (2^256) plus ntime for search
         space, or
     (b) builds its own coinbase for mode 2 and recomputes the commitment roots from scratch
         (see `build_coinbase.py` and `zcash_v6.py`).
   Firmware that insists on a non-zero extranonce2 needs path (b), which mode 2 already implements.

Zcash block header (post-NU5, 140 bytes)
    version(4, LE) | prevhash(32) | merkleroot(32) | blockcommitments(32)
    | time(4, LE) | bits(4, LE) | nonce(32)

The Equihash solution (1344 B) is not part of the header: it is a separate, length-prefixed field
inside the block. The header hash is the PoW hash.
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
from dataclasses import dataclass, field
from typing import Any

# mode-2 (on-chain split) coinbase/digest helpers; if missing, the engine falls back to
# zebrad's node-level coinbase only
try:
    import build_coinbase
    import zcash_v6 as zv
    _MODE2_MODULES = True
except Exception:  # noqa: BLE001
    build_coinbase = None      # type: ignore[assignment]
    zv = None                  # type: ignore[assignment]
    _MODE2_MODULES = False

# Equihash 200,9 difficulty-1 target (matches the Miningcore zcash definition)
# Share difficulty 1 == 2^13 = 8192 hashes (the Zcash pool convention, and the same
# constant HASHRATE_FACTOR uses). The previous 0x0007ffff… = 2^251 made difficulty 1
# only 32 hashes, i.e. 256x off — a real ASIC's shares were then 256x "too easy".
DIFF1_TARGET = 1 << 243
MAX_TARGET = (1 << 256) - 1

log = logging.getLogger("zecpool")


class ZebraRPC:
    """zebrad JSON-RPC client (cookie file, or user/password)."""

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
    """Display-order (big-endian) hex → header byte order (reverse the 32 bytes)."""
    return bytes.fromhex(h)[::-1]


def swab32(h: str) -> str:
    """Swap 4-byte words (the internal order ASIC firmware usually expects)."""
    raw = bytes.fromhex(h)
    out = bytearray()
    for i in range(0, len(raw), 4):
        out += raw[i : i + 4][::-1]
    return out.hex()


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
    coinbase_value: int = 0
    # mode 2: this job carries the pool-built coinbase verbatim (miner / pool / protocol lockbox)
    coinbase_raw: bytes = b""
    mode2_address: str = ""
    generated_at: float = field(default_factory=time.time)

    def notify_params(self, clean: bool = True) -> list[Any]:
        """Zcash SV1 mining.notify parameter order:
        [job_id, version, prevhash, merkleroot, reserved(=commitments), ntime, bits, clean]

        **Every field is sent in header-internal (standard) byte order**: Z15 firmware writes the
        received hex string byte by byte into the 140-byte header — verified by reverse-engineering
        with an Equihash(200,9) verifier. So the encoding here must be the standard header form:
          version  → little-endian ("04000000" for 4)
          prevhash → fully reversed (internal order)
          ntime    → little-endian
          bits     → little-endian
        merkleroot / reserved are already internal order and are passed through as-is.
        Get the byte order wrong here and every share — and every real block — is invalid.
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
    """Pull a template from zebrad GBT and turn it into an SV1 job."""

    def __init__(self, rpc: ZebraRPC, cfg: dict[str, Any]) -> None:
        self.rpc = rpc
        self.poll_seconds = float(cfg.get("template_poll_seconds", 5))
        self.current: Job | None = None
        self.previous: Job | None = None      # keep the previous job: in-flight submissions stay valid
        # No extraNonce is injected into the coinbase: zebrad's version is used verbatim and the
        # search space comes from the 32-byte nonce (see the module docstring).
        self.extranonce1_size = 4
        self.extranonce2_size = 0
        self._seq = 0
        self._current_fingerprint: tuple | None = None
        self.last_template: dict[str, Any] | None = None   # mode 2 rebuilds the coinbase from this
        self._lock = asyncio.Lock()

    async def refresh(self) -> Job | None:
        async with self._lock:
            try:
                tpl = await asyncio.get_running_loop().run_in_executor(
                    None, self.rpc.call, "getblocktemplate", []
                )
            except Exception as exc:
                log.warning("getblocktemplate failed: %s", exc)
                return None
            self.last_template = tpl
            fingerprint = self._fingerprint(tpl)
            # Reuse the job_id while the template is unchanged so miners do not reset per poll
            if (
                fingerprint is not None
                and self.current is not None
                and fingerprint == self._current_fingerprint
            ):
                return self.current
            job = self._build_job(tpl)
            if job:
                self._current_fingerprint = fingerprint
                log.info(
                    "new job height=%s tx=%s coinbase=%.8f ZEC",
                    job.height,
                    len(job.template_txs),
                    job.coinbase_value / 1e8,
                )
            if job is not None and job is not self.current:
                self.previous = self.current
            self.current = job
            return job

    @staticmethod
    def _fingerprint(tpl: dict[str, Any]) -> tuple | None:
        """Template identity: height + prevhash + transaction set + commitment roots. Only a change needs a new job."""
        try:
            return (
                int(tpl["height"]),
                tpl["previousblockhash"],
                tpl.get("blockcommitmentshash") or tpl.get("finalsaplingroothash"),
                tuple(t["hash"] for t in tpl.get("transactions", [])),
            )
        except Exception:
            return None

    def _build_job(self, tpl: dict[str, Any]) -> Job | None:
        try:
            coinbase_hex: str = tpl["coinbasetxn"]["data"]
            commitments = tpl.get("blockcommitmentshash") or tpl.get("finalsaplingroothash")
            if not commitments:
                log.error("template is missing blockcommitmentshash / finalsaplingroothash")
                return None
            txs = [t["data"] for t in tpl.get("transactions", [])]
            branch = [hex2bytes_reversed(t["hash"]) for t in tpl.get("transactions", [])]

            raw = bytes.fromhex(coinbase_hex)
            coinb1 = raw      # whole coinbase, verbatim
            coinb2 = b""      # no split
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
                coinbase_value=int(tpl.get("coinbasevalue") or 0),
            )
        except Exception as exc:
            log.exception("failed to build job: %s", exc)
            return None

class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.extranonce1 = os.urandom(4).hex()
        self.worker = ""
        self.difficulty = 1.0
        self.subscribed = False
        self.shares = 0
        self.accepted = 0
        self.rejected = 0
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.peer = writer.get_extra_info("peername")
        # Vardiff: share timestamps for the last minute + a grace window after difficulty changes
        self.share_times: list[float] = []
        self.low_diff_times: list[float] = []      # low_difficulty reject timestamps (difficulty feedback)
        self.diff_locked = False                   # miner keeps its own difficulty (ignores set_difficulty)
        self.last_vardiff = time.time()
        self.grace_difficulty = 0.0
        self.grace_until = 0.0
        self.fixed_difficulty = False
        # mode 2: this miner's payout address and per-miner job (99/1 split inside the coinbase)
        self.mode2_address = ""
        self.mode2_job: Job | None = None
        self.mode2_prev_job: Job | None = None
        # Only sessions that completed authorize count as online miners; bare scanner sockets do not
        self.authorized = False

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
        # ---- vardiff (adaptive difficulty) ----
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
        # Reject-rate feedback: a rig mining at a lower difficulty than we assigned (typical: rental
        # platforms pinning a static difficulty, firmware ignoring set_difficulty) produces many
        # low_difficulty rejects. Above the threshold the pool steps down to match what it actually mines.
        self.vardiff_reject_ratio = float(vd.get("reject_ratio_threshold", 0.30))
        self.vardiff_min_samples = int(vd.get("min_samples", 6))
        # Remembered per-worker difficulty, so a reconnect does not re-converge from scratch
        self.learned_difficulty: dict[str, float] = {}
        self.learned_path = cfg.get("learned_difficulty_file",
                                    os.path.join(os.path.dirname(
                                        cfg.get("status_file", "/var/lib/zecpool/status.json")),
                                        "worker_difficulty.json"))
        try:
            self.learned_difficulty = {str(k): float(v)
                                       for k, v in json.load(
                                           open(self.learned_path, encoding="utf-8")).items()}
            log.info("loaded learned difficulty for %d worker(s)", len(self.learned_difficulty))
        except Exception:  # noqa: BLE001
            self.learned_difficulty = {}
        # ---- mode 2: on-chain split (99% miner / 1% pool / fixed protocol lockbox) ----
        m2 = cfg.get("mode2") or {}
        self.mode2_enabled = bool(m2.get("enabled", False))
        self.mode2_fee_address = str(m2.get("pool_fee_address", "")).strip()
        self.mode2_fee_pct = float(m2.get("fee_pct", 1.0))
        self.mode2_whitelist = tuple(m2.get("whitelist_prefixes", ["local."]))
        self._mode2_seq = 0
        if self.mode2_enabled and not self.mode2_fee_address:
            log.error("mode2.enabled=true but pool_fee_address is empty — mode 2 disabled")
            self.mode2_enabled = False
        # ---- dashboard statistics ----
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
        # ---- share counters are persisted so a restart does not reset the public numbers ----
        self.counter_file = cfg.get("counter_file", "/var/lib/zecpool/counters.json")
        try:
            _c = json.load(open(self.counter_file, encoding="utf-8"))
            self.shares_total = int(_c.get("shares_total", 0))
            self.shares_rejected = int(_c.get("shares_rejected", 0))
            log.info("restored share counters: %d accepted / %d rejected",
                     self.shares_total, self.shares_rejected)
        except Exception:  # noqa: BLE001
            pass
        # ---- public counters describe external miners only ----
        # Our own test rigs (loopback CPU probes, whitelabel rigs) must never move the numbers a
        # visitor compares against our claims; they are counted separately for diagnostics.
        self.internal_prefixes = tuple(
            str(p).strip().lower() for p in cfg.get("internal_worker_prefixes", []) if str(p).strip())
        self.internal_accepted = 0
        self.internal_rejected = 0
        self.reject_reasons: dict[str, int] = {}
        self.recent_sessions: list[dict[str, Any]] = []   # recently closed sessions, for troubleshooting

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Disable Nagle (removes ~40 ms coalescing latency) and enable TCP keepalive so that
        # mining.notify / mining.submit travel unbuffered.
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:  # noqa: BLE001
            pass
        client = Client(reader, writer)
        self.clients.add(client)
        log.info("miner connected %s", client.peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    continue
                await self.dispatch(client, msg)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(client)
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
            writer.close()
            log.info("miner disconnected %s (shares=%s)", client.peer, client.shares)

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
            # ---- mode 2: strictly refuse invalid addresses (worker must be <t1/t3 address>.<name>) ----
            mode2_bypass = bool(
                self.mode2_whitelist and client.worker.startswith(self.mode2_whitelist)
            )
            if self.mode2_enabled and _MODE2_MODULES and not mode2_bypass:
                addr = self._mode2_parse_worker(client.worker)
                if addr is None:
                    log.warning("authorize refused (invalid ZEC address) worker=%s peer=%s",
                                client.worker, client.peer)
                    await client.send({"id": mid, "result": False,
                                       "error": [20, "Invalid ZEC address", None]})
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
                log.info("mode-2 authorize worker=%s payout=%s fee=%.2f%% → pool %s",
                         client.worker, addr, self.mode2_fee_pct, self.mode2_fee_address)
            # Start from the remembered difficulty to skip re-convergence on reconnect
            if client.worker in self.learned_difficulty:
                client.difficulty = self.learned_difficulty[client.worker]
                client.last_vardiff = time.time()
                # A remembered value below the default means the rig keeps its own difficulty
                # (it ignores set_difficulty), so lock it instead of probing upward and eating rejects.
                if client.difficulty < self.default_difficulty:
                    client.diff_locked = True
            client.authorized = True
            await client.send({"id": mid, "result": True, "error": None})
            log.info("authorize worker=%s difficulty=%.3f%s", client.worker, client.difficulty,
                     " (fixed-difficulty prefix)" if client.fixed_difficulty else "")
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
        _, job_id, ntime, nonce, solution = params[0], params[1], params[2], params[3], params[4]
        if job_id != job.job_id:
            # Right after a broadcast, miners are usually still submitting against the previous job:
            # validate with that job's fields, otherwise those shares would be mislabelled stale.
            previous = self.jobs.previous
            if previous is not None and job_id == previous.job_id:
                job = previous
            elif client.mode2_prev_job is not None and job_id == client.mode2_prev_job.job_id:
                job = client.mode2_prev_job
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
            log.warning("failed to build header: %s", exc)
            client.rejected += 1
            self._count_share(client, False)
            self._bump_reason("bad_share", client)
            await client.send({"id": mid, "result": False, "error": [20, "bad share", None]})
            return

        header_hash = dsha256(header)
        # After a difficulty change, in-flight shares are still accepted at the older (lower) value
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
            await self._maybe_vardiff(client)      # reject-rate feedback may step difficulty down
            return

        client.shares += 1
        client.accepted += 1
        client.last_activity = time.time()
        self._count_share(client, True)
        self.share_events.append((time.time(), client.worker or "unknown", effective))
        client.share_times.append(time.time())
        self.worker_total[client.worker or "unknown"] = self.worker_total.get(client.worker or "unknown", 0) + 1
        self.worker_last[client.worker or "unknown"] = time.time()
        await client.send({"id": mid, "result": True, "error": None})
        await self._maybe_vardiff(client)

        if hash_meets_target(header_hash, self._target_from_bits(job.bits)):
            log.warning("network difficulty met height=%s worker=%s → submitblock", job.height, client.worker)
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
        """Assemble the block header from zebrad's coinbase plus the miner's ntime/nonce.

        Note: a Zcash (Equihash) block header is version|prevhash|merkleroot|commitments|time|bits|nonce|solution,
        where the solution is a **variable-length, length-prefixed field that belongs to the header**; the
        block hash is SHA256d over the *whole* header. Dropping the solution yields a different hash, which

        nonce: a Z15 submits 28 bytes; the header's 32-byte nonce = **pool extranonce1(4B) ‖ miner 28B**
        (confirmed by Equihash verification; padding with four zero bytes was the original bug).
        """
        coinbase = job.coinb1 + job.coinb2
        # In a mode-2 job the merkle root is already final in the pool-built coinbase, so use it;
        # node-coinbase jobs are computed from coinb1/coinb2 + the branch (both agree).
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
            # ntime comes back verbatim: notify sent it as a **little-endian** hex string, so it must be
            # written byte for byte — repacking it as an integer would reverse it a second time.
            + bytes.fromhex(ntime.rjust(8, "0"))
            + bytes.fromhex(job.bits)[::-1]
            + nonce_bytes
        )
        if solution is None:
            return fixed
        sol = bytes.fromhex(solution)
        # Z15 firmware already prefixes the solution with its CompactSize length (fd4005 = 1344).
        # Adding a second prefix produces an invalid block — a solved block would simply be lost.
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
        # mode-2 jobs use the pool-built coinbase (99/1 split); everything else uses zebrad's
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
            log.error("submitblock error: %s", exc)

    async def poll_templates(self) -> None:
        """Two-speed polling: a 0.5 s lightweight getbestblockhash probe that triggers a full GBT pull the

        moment a new block appears. Routine refreshes follow poll_seconds to track mempool and fee
        changes, but a bestblockhash change is broadcast immediately — sub-second block awareness.
        """
        loop = asyncio.get_running_loop()
        last_full = 0.0
        last_best: str | None = None
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
                    log.info("probe saw new block %.8s… → refreshing template and broadcasting clean_jobs", str(best))
                for client in list(self.clients):
                    if client.subscribed:
                        try:
                            # mode-2 clients: every template change needs a fresh per-miner coinbase job
                            # (the merkle root commits to the 99/1 coinbase)
                            if self.mode2_enabled and _MODE2_MODULES and client.mode2_address:
                                newjob = await self._mode2_refresh(client)
                                if newjob is not None:
                                    client.mode2_prev_job = client.mode2_job
                                    client.mode2_job = newjob
                                    await client.notify_job(newjob, clean=clean)
                                continue
                            await client.notify_job(job, clean=clean)
                        except Exception:
                            pass
            await asyncio.sleep(0.5)

    # Equihash 200,9: a difficulty-1 share needs 2^13 hashes on average
    HASHRATE_FACTOR = 8192

    # ------------------------------------------------------------------- mode 2
    def _mode2_parse_worker(self, worker: str) -> str | None:
        """`<t1/t3 address>.<worker name>` → address; None when invalid (strictly refused)."""
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
        """Build the per-miner job (99/1 split) from the current template and this miner's address."""
        try:
            built = build_coinbase.build_dynamic_coinbase(
                tpl, client.mode2_address, self.mode2_fee_address, self.mode2_fee_pct)
        except Exception as exc:  # noqa: BLE001
            log.error("mode-2 coinbase build failed worker=%s: %s", client.worker, exc)
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
        """Difficulty used for validation: during the post-change grace window the lower of new/old wins."""
        if client.grace_until > time.time() and client.grace_difficulty > 0:
            return min(client.difficulty, client.grace_difficulty)
        return client.difficulty

    async def _maybe_vardiff(self, client: Client) -> None:
        """Adaptive difficulty, including **reject-rate feedback**.

        Decision order (evaluated every interval_seconds over a 60 s window):
          1. **reject rate above the threshold** (30% default) → step down: the rig is mining at a
             lower difficulty than assigned (static rental-platform difficulty, firmware ignoring set_difficulty);
          2. accepted rate above the target ceiling → step up; below half the floor → step down;
        After a change the window is cleared and a grace period protects in-flight shares.
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
            # A reject rate above the threshold means the rig mines at its own lower difficulty.
            # Mark it as self-managed: step down only, so we do not oscillate back up.
            client.diff_locked = True
            reason = f"reject rate {ratio * 100:.0f}% ({rejected} rejected / {accepted} accepted) → step down and lock"
        elif not client.diff_locked and accepted > self.vardiff_target_max:
            new = min(client.difficulty * 2, self.vardiff_max)
            reason = f"rate too high ({accepted}/min) → step up"
        elif not client.diff_locked and accepted < self.vardiff_target_min / 2 \
                and client.difficulty > self.vardiff_min:
            new = max(client.difficulty / 2, self.vardiff_min)
            reason = f"rate too low ({accepted}/min) → step down"
        client.last_vardiff = now
        if new == client.difficulty:
            return
        old = client.difficulty
        client.grace_difficulty = old
        client.grace_until = now + self.vardiff_grace
        client.difficulty = new
        client.share_times = []          # re-measure after the change
        client.low_diff_times = []
        self.learned_difficulty[client.worker] = new      # remember this rig's sweet spot
        try:
            tmp = self.learned_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.learned_difficulty, fh)
            os.replace(tmp, self.learned_path)
        except Exception:  # noqa: BLE001
            pass
        await client.notify_difficulty()
        log.info("vardiff worker=%s difficulty %.0f → %.0f (%s, grace %.0fs)",
                 client.worker or "?", old, new, reason, self.vardiff_grace)

    def _is_internal_worker(self, worker: str) -> bool:
        """True for our own test rigs, which must not shape the public statistics.

        Matches either the whole worker string (`local.rig1`, `u1abc…`) or the label after the last
        dot (`t1abc….cpurig`), case-insensitively. Empty list → nothing is internal.
        """
        w = (worker or "").strip().lower()
        if not w or not self.internal_prefixes:
            return False
        label = w.rsplit(".", 1)[-1]
        return any(w.startswith(p) or label.startswith(p) for p in self.internal_prefixes)

    def _count_share(self, client: "Client", accepted: bool) -> None:
        """Public counters track external miners; internal rigs are counted separately."""
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

    def _persist_counters(self) -> None:
        """Write share counters next to the status file so restarts keep history."""
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
        for c in self.clients:
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
            "workers_online": sum(1 for c in self.clients if getattr(c, "authorized", False)),
            "connections_open": len(self.clients),
            "shares_total": self.shares_total,
            "shares_rejected": self.shares_rejected,
            "internal_accepted": self.internal_accepted,
            "internal_rejected": self.internal_rejected,
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

    async def status_loop(self) -> None:
        os.makedirs(os.path.dirname(self.status_file), exist_ok=True)
        tick = 0
        while True:
            snap = self._snapshot()
            tick += 1
            if tick % 3 == 0:  # one sample every ~6 s (≈10 min window)
                self.history.append({"ts": snap["updated_at"], "hashrate": snap["hashrate"]})
                snap = self._snapshot()
            self._persist_counters()
            tmp = f"{self.status_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False)
            os.replace(tmp, self.status_file)
            # When the engine runs as a front-end, push the snapshot to a dashboard so public pages stay live
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
            "SV1 pool listening on %s:%s (current job: %s)",
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
