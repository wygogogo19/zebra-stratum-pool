"""Synthetic miner / engine harness — protocol-level tests with **no hashrate and no node**.

Two pieces:

* `StratumClient` — a small Stratum V1 client (subscribe / authorize / read messages / submit), used to drive
  the engine over a real TCP socket exactly like a rig would.
* `EngineHarness` — runs the real `pool.PoolServer` on 127.0.0.1 with a `StubRPC` that serves the frozen
  mainnet template, in a background thread. Nothing here is mocked at the protocol level.

`replay_job()` turns a *recorded* share (see `tests/fixtures/real_solutions.json`, taken from the production
CPU probe) back into a `pool.Job`, so a test can submit a **pre-computed, genuinely valid Equihash(200,9)
solution** and have the engine run its real verification path. That is how the suite tests the accept path
without running a solver: no fake hashes, no monkeypatched validator.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

from tests import _fixtures as fx

fx.repo_on_path()

import pool  # noqa: E402


class StratumClient:
    """Minimal Stratum V1 client (line-delimited JSON)."""

    def __init__(self, host: str, port: int, timeout: float = 15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.rfile = self.sock.makefile("rwb")
        self.messages: list[dict] = []
        self._id = 0

    def close(self) -> None:
        try:
            self.rfile.close()
        finally:
            self.sock.close()

    def send(self, obj: dict) -> None:
        self.rfile.write((json.dumps(obj) + "\n").encode())
        self.rfile.flush()

    def read(self) -> dict:
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("engine closed the connection")
        msg = json.loads(line.decode())
        self.messages.append(msg)
        return msg

    def wait_for(self, *, method: str | None = None, msg_id: int | None = None, timeout: float = 15.0):
        for msg in self.messages:            # a notify may already have arrived with the subscribe reply
            if method is not None and msg.get("method") != method:
                continue
            if msg_id is not None and msg.get("id") != msg_id:
                continue
            return msg
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.read()
            if method is not None and msg.get("method") != method:
                continue
            if msg_id is not None and msg.get("id") != msg_id:
                continue
            return msg
        raise TimeoutError("no matching message (method=%r id=%r)" % (method, msg_id))

    # ---- high level helpers -------------------------------------------------
    def subscribe(self) -> dict:
        self._id += 1
        self.send({"id": self._id, "method": "mining.subscribe", "params": ["synthetic-miner/1.0"]})
        return self.wait_for(msg_id=self._id)

    def authorize(self, worker: str) -> dict:
        self._id += 1
        self.send({"id": self._id, "method": "mining.authorize", "params": [worker, "x"]})
        return self.wait_for(msg_id=self._id)

    def wait_job(self) -> list:
        return self.wait_for(method="mining.notify")["params"]

    def jobs_seen(self) -> list:
        return [m["params"] for m in self.messages if m.get("method") == "mining.notify"]

    def wait_for_extra_job(self, previous_job_id: str, timeout: float = 15.0) -> list:
        """Wait for a *new* job (used after authorize, when a mode-2 job replaces the generic one)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for params in self.jobs_seen():
                if params[0] != previous_job_id:
                    return params
            self.read()
        raise TimeoutError("no replacement job arrived")

    def submit(self, worker: str, job_id: str, ntime: str, nonce: str, solution: str) -> dict:
        self._id += 1
        self.send({"id": self._id, "method": "mining.submit",
                   "params": [worker, job_id, ntime, nonce, solution]})
        return self.wait_for(msg_id=self._id)

    def submit_params(self, params: list) -> dict:
        """Submit an arbitrary parameter list.

        Firmware disagrees on the shape of `mining.submit` — most Zcash rigs send five parameters,
        BTC-style clients add an `extranonce2` and send six — so the compatibility tests drive both
        through this instead of the fixed five-parameter `submit()` helper.
        """
        self._id += 1
        self.send({"id": self._id, "method": "mining.submit", "params": params})
        return self.wait_for(msg_id=self._id)

    def call(self, method: str, params: list) -> dict:
        """Send any Stratum method and return its reply (used to probe advertised capabilities)."""
        self._id += 1
        self.send({"id": self._id, "method": method, "params": params})
        return self.wait_for(msg_id=self._id)


def replay_job(record: dict, job_class=pool.Job, job_id: str | None = None):
    """Rebuild the job a recorded share was solved against, from its 140-byte header.

    The Zcash block header is version|prevhash|merkleroot|commitments|time|bits|nonce (the solution follows
    with its CompactSize prefix), so every job field can be recovered exactly. Submitting the recorded nonce
    and solution then reproduces a header the production engine already accepted.
    """
    header = bytes.fromhex(record["header140"])
    if len(header) != 140:
        raise ValueError("expected a 140-byte header, got %d bytes" % len(header))
    return job_class(
        job_id=job_id or record.get("job_id") or "replay",
        height=0,
        version=int.from_bytes(header[0:4], "little"),
        prevhash_display=header[4:36][::-1].hex(),
        bits=header[104:108][::-1].hex(),
        curtime=int.from_bytes(header[100:104], "little"),
        commitments_hash=header[68:100],
        coinb1=b"",
        coinb2=b"",
        merkle_branch=[],
        merkle_root=header[36:68],
    )


class EngineHarness:
    """Runs the real engine with a stub node in a background thread."""

    def __init__(self, cfg: dict, template: dict | None = None, subsidy: dict | None = None,
                 replay: dict | None = None, refresh_first: bool = True):
        self.cfg = cfg
        self.stub = fx.StubRPC(template, subsidy) if template is not None else fx.StubRPC()
        self.replay = replay
        self.refresh_first = refresh_first
        self.port: int | None = None
        self.server: pool.PoolServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 15.0) -> "EngineHarness":
        self._thread = threading.Thread(target=self._run, name="engine-harness", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("engine harness did not start")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        server = pool.PoolServer(self.cfg)
        server.rpc = self.stub
        server.jobs.rpc = self.stub
        if self.refresh_first:
            loop.run_until_complete(server.jobs.refresh())
        if self.replay is not None:
            server.jobs.current = replay_job(self.replay)
        self.server = server
        tcp = loop.run_until_complete(asyncio.start_server(server.handle, "127.0.0.1", 0))
        self.port = tcp.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    def stop(self) -> None:
        time.sleep(0.15)          # let the last handler coroutine finish before the loop goes away
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def client(self, timeout: float = 15.0) -> StratumClient:
        return StratumClient("127.0.0.1", int(self.port), timeout=timeout)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
