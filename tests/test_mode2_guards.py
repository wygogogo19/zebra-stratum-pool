"""Mode-2 authorization guards.

These are the diligence red lines: a worker must carry a valid `<t1/t3 address>.<rig>` payout address, and
that address must differ from the pool's own 1% fee address — otherwise the on-chain "99/1 split" would
just move coins between the operator's own wallets and prove nothing.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest

from tests import _fixtures as fx

fx.repo_on_path()

import build_coinbase as bc      # noqa: E402
import pool                      # noqa: E402
import zcash_v6 as zv            # noqa: E402

MINER = "t1XwoZWZCpkFWfCdzsi5QzrsDuxmHX34CZ3"
FEE = "t1Urcc2cLVMo1hYbL4r7886JfWk2iVDVWhy"


class Mode2Authorization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = pool.PoolServer(fx.engine_config(
            self.tmp.name,
            mode2={"enabled": True, "pool_fee_address": FEE, "fee_pct": 1.0,
                   "whitelist_prefixes": ["local.", "u1"]},
            default_difficulty=0.02,
        ))
        self.server.rpc = fx.StubRPC()
        self.server.jobs.rpc = self.server.rpc
        asyncio.run(self.server.jobs.refresh())          # a job must exist before mode 2 can serve one
        self.server._mode2_refresh = _async_none          # keep the guard tests hermetic (no coinbase build)

    def tearDown(self):
        self.tmp.cleanup()

    def authorize(self, worker):
        client = fx.FakeClient(worker=worker)
        asyncio.run(self.server.dispatch(client, {"id": 7, "method": "mining.authorize",
                                                  "params": [worker, "x"]}))
        return client

    @staticmethod
    def reply(client, mid=7):
        """The engine may follow the authorize reply with set_difficulty/notify, so match on the id."""
        return next((m for m in client.sent if m.get("id") == mid), {})

    def test_rejects_a_worker_without_a_payout_address(self):
        client = self.authorize("miner1")
        reply = self.reply(client)
        self.assertFalse(reply.get("result"))
        self.assertEqual(reply["error"][0], 20)
        self.assertIn("Invalid ZEC address", reply["error"][1])
        self.assertTrue(client.closed, "the pool hangs up on an invalid address")

    def test_rejects_a_payout_address_equal_to_the_pool_fee_address(self):
        client = self.authorize(FEE + ".rig1")
        reply = self.reply(client)
        self.assertFalse(reply.get("result"))
        self.assertIn("must differ", reply["error"][1])
        self.assertTrue(client.closed)

    def test_rejects_a_non_zcash_address(self):
        client = self.authorize("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2.rig1")   # a Bitcoin address
        self.assertFalse(self.reply(client).get("result"))
        self.assertIn("Invalid ZEC address", self.reply(client)["error"][1])

    def test_accepts_a_valid_miner_address(self):
        server = self.server

        async def dummy(client):
            client.mode2_address = MINER
            return server.jobs.current

        server._mode2_refresh = dummy
        client = self.authorize(MINER + ".rig1")
        self.assertTrue(self.reply(client).get("result"))
        self.assertEqual(client.mode2_address, MINER)
        self.assertIsNotNone(client.mode2_job)

    def test_whitelisted_prefixes_bypass_mode2(self):
        for worker in ("local.testrig", "u1test.something"):
            client = self.authorize(worker)
            self.assertTrue(self.reply(client).get("result"), worker)
            self.assertEqual(client.mode2_address, "", "local/u1 rigs use the node's own coinbase")

    def test_invalid_address_is_logged_for_audit(self):
        with self.assertLogs(level="WARNING") as logs:
            self.authorize(FEE + ".rig1")
        self.assertTrue(any("separation" in m for m in logs.output), logs.output)


class Mode2JobOnTheWire(unittest.TestCase):
    """The job handed to a mode-2 miner must carry the 99/1 coinbase."""

    def test_miner_specific_job_contains_the_split(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            server = pool.PoolServer(fx.engine_config(
                tmp.name, mode2={"enabled": True, "pool_fee_address": FEE, "fee_pct": 1.0,
                                 "whitelist_prefixes": ["local."]}))
            server.rpc = fx.StubRPC()
            server.jobs.rpc = server.rpc
            base = asyncio.run(server.jobs.refresh())
            client = fx.FakeClient(worker=MINER + ".rig1")
            client.mode2_address = MINER
            job = asyncio.run(server._mode2_refresh(client))
            self.assertIsNotNone(job)
            self.assertNotEqual(job.job_id, base.job_id, "a mode-2 miner gets its own job id")
            self.assertEqual(job.mode2_address, MINER)
            tx = zv.parse_transparent(job.coinbase_raw)
            self.assertEqual(len(tx["vouts"]), 3)
            self.assertEqual(tx["vouts"][0]["script"], bc.address_to_script(MINER))
            self.assertEqual(tx["vouts"][1]["script"], bc.address_to_script(FEE))
            self.assertGreater(tx["vouts"][0]["value"], tx["vouts"][1]["value"] * 90,
                               "the miner's output must dominate the split")
            # the mode-2 merkle root is a tree over the pool-built coinbase txid + the template txs
            tpl = server.jobs.last_template
            txids = [zv.txid_v6(tx)] + [bytes.fromhex(t["hash"])[::-1] for t in tpl["transactions"]]
            self.assertEqual(job.merkle_root, zv.merkle_root(txids))
            self.assertEqual(zv.serialize_transparent_tx(tx), job.coinbase_raw)
        finally:
            tmp.cleanup()


async def _async_none(client):
    return None


if __name__ == "__main__":
    unittest.main()
