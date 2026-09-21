#!/usr/bin/env python3
"""Liveness / protocol regression probe: really submits a share using the *current* wire protocol.

Flow: subscribe → record extranonce1 → authorize (the worker name decides the payout path) → **wait for
the mining.notify that follows authorize** (mode-2 clients get a per-miner job whose coinbase carries the
99/1 split) → build the 140-byte header from the notify fields **byte for byte** → brute-force a nonce for
the assigned difficulty (`extranonce1 ‖ 28B`) → submit → match the response by id → if the job rotated and

Usage:
  python3 pool_selftest_submit.py [host] [port] [worker]
     worker defaults to local.selftest (whitelisted → node-level coinbase)
     Mode-2 example: python3 pool_selftest_submit.py 127.0.0.1 3032 t1YourAddress….rig
"""

from __future__ import annotations

import hashlib
import json
import queue
import socket
import sys
import threading
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 3032
WORKER = sys.argv[3] if len(sys.argv) > 3 else "local.selftest"
MAX_ATTEMPTS = 3
MAX_HASHES = 60_000_000


def dsha256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


class Conn:
    """Stratum connection with a background reader thread: tracks the latest job, matches replies by id."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=20)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.file = self.sock.makefile("rwb")
        self.inbox: queue.Queue[dict] = queue.Queue()
        self.latest_job: list | None = None
        self.difficulty = 1.0
        self._next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        while True:
            try:
                line = self.file.readline()
            except Exception:  # noqa: BLE001
                return
            if not line:
                return
            try:
                msg = json.loads(line.decode().strip())
            except Exception:  # noqa: BLE001
                continue
            if msg.get("method") == "mining.notify":
                self.latest_job = msg["params"]
            elif msg.get("method") == "mining.set_difficulty":
                self.difficulty = float(msg["params"][0])
            else:
                self.inbox.put(msg)

    def call(self, method: str, params: list) -> dict:
        mid = self._next_id
        self._next_id += 1
        self.file.write(json.dumps({"id": mid, "method": method, "params": params}).encode() + b"\n")
        self.file.flush()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                msg = self.inbox.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == mid:
                return msg
        return {"error": "no reply"}


def wait_job(conn: Conn, timeout: float = 20.0) -> list | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if conn.latest_job is not None:
            return conn.latest_job
        time.sleep(0.05)
    return None


def mine(conn: Conn, job: list) -> tuple[bytes, bytes, int] | None:
    version = bytes.fromhex(job[1])
    prevhash = bytes.fromhex(job[2])
    merkle = bytes.fromhex(job[3])
    reserved = bytes.fromhex(job[4])
    ntime = bytes.fromhex(job[5])
    bits = bytes.fromhex(job[6])
    e1 = bytes.fromhex(conn.e1)
    diag1 = 1 << 243
    target = diag1 // max(conn.difficulty, 1e-9)
    fixed = version + prevhash + merkle + reserved + ntime + bits
    # Note: a Zcash block hash (the share target) is SHA256d over "140-byte header ‖ variable-length
    # solution field". This probe submits an all-zero 1344-byte solution, so the fd4005 length prefix
    # must be included here as well — otherwise the local hash disagrees with the pool's (which is what
    dummy_solution = b"\xfd\x40\x05" + bytes(1344)
    t0 = time.time()
    for nonce in range(MAX_HASHES):
        n = e1 + nonce.to_bytes(28, "little")
        if int.from_bytes(dsha256(fixed + n + dummy_solution), "little") <= target:
            dt = time.time() - t0
            print(f"  nonce found ({nonce} tries, {dt:.1f}s, {nonce/max(dt,1e-9)/1000:.0f} kH/s)")
            return fixed + n, n, nonce
    return None


def main() -> int:
    conn = Conn(HOST, PORT)
    sub = conn.call("mining.subscribe", ["zebra-stratum-pool-selftest/1"])
    if "result" not in sub:
        print("subscribe failed:", sub)
        return 1
    conn.e1 = sub["result"][1]           # type: ignore[attr-defined]
    print(f"worker={WORKER}  extranonce1={conn.e1}  (difficulty is assigned after authorize)")

    auth = conn.call("mining.authorize", [WORKER, "x"])
    print("authorize reply:", json.dumps(auth, ensure_ascii=False)[:120])
    if auth.get("result") is not True:
        print("authorization refused → strict address validation is working (expected for invalid addresses)")
        return 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        job = wait_job(conn)
        if job is None:
            print("no mining.notify received")
            return 1
        print(f"attempt {attempt}: job={job[0]} difficulty={conn.difficulty}")
        got = mine(conn, job)
        if got is None:
            print("no nonce met the target")
            return 1
        _, nonce32, _ = got
        resp = conn.call("mining.submit", [WORKER, job[0], job[5], nonce32.hex(), "00" * 1344])
        print("submit returned:", json.dumps(resp, ensure_ascii=False)[:140])
        if resp.get("result") is True:
            print("result: share accepted ✅")
            return 0
        err = str(resp.get("error"))
        if "stale" not in err:
            print("result: share rejected ❌")
            return 1
        print("  job rotated, retrying with the newest job…")
        conn.latest_job = None
    print("all three attempts failed because the job kept rotating")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
