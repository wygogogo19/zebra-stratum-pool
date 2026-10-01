"""Shared helpers for the test suite.

Works under both `pytest` (CI) and the stdlib `unittest` runner (so the suite can be executed on a
machine without pip). Every fixture is **real mainnet data** frozen into `tests/fixtures/`:

* `gbt_mainnet_3502592.json` — a verbatim `getblocktemplate` + `getblocksubsidy` response from our
  production Zebra node (height 3,502,592), used to exercise the coinbase/merkle/commitment code
  against the same data Zebra computes its own `defaultroots` from.
* `real_solutions.json` — two shares that the production engine accepted, each with the 140-byte
  header, nonce and the 1344-byte Equihash(200,9) solution, so the share/verification path can be
  tested without running a solver.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def repo_on_path() -> None:
    """Make the repository modules (`pool`, `zcash_v6`, `build_coinbase`, ...) importable."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


def load_gbt():
    """Return (template, subsidy) — the frozen production getblocktemplate / getblocksubsidy pair."""
    with open(os.path.join(FIXTURES, "gbt_mainnet_3502592.json"), encoding="utf-8") as fh:
        dumped = json.load(fh)
    return dumped["template"], dumped["subsidy"]


def load_real_solutions():
    """Return the list of accepted-share records (header140 / nonce / solution / difficulty)."""
    with open(os.path.join(FIXTURES, "real_solutions.json"), encoding="utf-8") as fh:
        return json.load(fh)["solutions"]


class StubRPC:
    """Deterministic stand-in for `pool.ZebraRPC` that serves the frozen mainnet template.

    It records every call, so tests can assert *what* the engine asked the node for (and that it
    never asked for anything outside the read-only/submit surface).
    """

    def __init__(self, template=None, subsidy=None):
        self.template, self.subsidy = load_gbt() if template is None else (template, subsidy)
        self.calls: list[tuple] = []
        self.submitted_blocks: list[str] = []
        self.submit_result = None

    def call(self, method, params=None, timeout=30.0):  # noqa: D401 - mirrors ZebraRPC.call
        params = params or []
        self.calls.append((method, params))
        if method == "getblocktemplate":
            return self.template
        if method == "getblocksubsidy":
            return self.subsidy
        if method == "getbestblockhash":
            return self.template["previousblockhash"]
        if method == "getblockcount":
            return int(self.template["height"])
        if method == "submitblock":
            self.submitted_blocks.append(params[0] if params else "")
            return self.submit_result
        raise AssertionError("StubRPC received an unexpected method: %r" % (method,))


class FakeClient:
    """Duck-typed stand-in for `pool.Client`, for tests that exercise message dispatch without a socket."""

    def __init__(self, extranonce1: str = "deadbeef", worker: str = ""):
        self.extranonce1 = extranonce1
        self.worker = worker
        self.difficulty = 1.0
        self.subscribed = False
        self.authorized = False
        self.shares = 0
        self.accepted = 0
        self.rejected = 0
        self.connected_at = 0.0
        self.last_activity = 0.0
        self.peer = ("127.0.0.1", 0)
        self.share_times: list[float] = []
        self.low_diff_times: list[float] = []
        self.diff_locked = False
        self.last_vardiff = 0.0
        self.grace_difficulty = 0.0
        self.grace_until = 0.0
        self.fixed_difficulty = False
        self.mode2_address = ""
        self.mode2_job = None
        self.mode2_prev_job = None
        self.mode2_prev_jobs = []
        self.reaped = False
        self.sent: list[dict] = []
        self.closed = False

        class _Writer:
            @staticmethod
            def close() -> None:
                outer.closed = True

        outer = self
        self.writer = _Writer()

    async def send(self, obj: dict) -> None:
        self.sent.append(obj)

    async def notify_difficulty(self) -> None:
        await self.send({"id": None, "method": "mining.set_difficulty", "params": [self.difficulty]})

    async def notify_job(self, job, clean: bool = True) -> None:
        await self.send({"id": None, "method": "mining.notify", "params": job.notify_params(clean)})

    def last(self, key="result"):
        return self.sent[-1].get(key) if self.sent else None


def engine_config(tmpdir: str, **overrides) -> dict:
    """A minimal, hermetic engine configuration: no node, no daemon files, nothing under /var/lib."""
    cfg = {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "template_poll_seconds": 30,
        "default_difficulty": 128.0,
        "log_level": "info",
        "zebra": {"url": "http://127.0.0.1:1/", "cookie_file": os.path.join(tmpdir, "no.cookie"),
                  "user": "", "password": ""},
        "status_file": os.path.join(tmpdir, "status.json"),
        "counter_file": os.path.join(tmpdir, "counters.json"),
        "learned_difficulty_file": os.path.join(tmpdir, "worker_difficulty.json"),
        "vardiff": {"enabled": False},
        "mode2": {"enabled": False},
        "internal_worker_prefixes": [],
    }
    cfg.update(overrides)
    return cfg
