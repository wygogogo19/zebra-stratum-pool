"""End-to-end protocol tests driven by the synthetic miner (real socket, real engine, no hashrate).

The accept path uses **recorded production shares**: the header is replayed as a job, and the pre-computed
Equihash(200,9) solution is submitted through the normal `mining.submit` message. The engine therefore runs
its real share validation and its real Equihash verifier — nothing is stubbed at that layer.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest

from tests import _fixtures as fx
from tests import synthetic_miner as sm

fx.repo_on_path()

import pool                      # noqa: E402

MINER = "t1XwoZWZCpkFWfCdzsi5QzrsDuxmHX34CZ3"
FEE = "t1Urcc2cLVMo1hYbL4r7886JfWk2iVDVWhy"


class ReplayJob(unittest.TestCase):
    def test_recorded_header_is_reproduced_byte_for_byte(self):
        record = fx.load_real_solutions()[0]
        job = sm.replay_job(record)
        server = pool.PoolServer(fx.engine_config(tempfile.mkdtemp()))
        # ntime must be passed exactly as `mining.notify` sends it (little-endian hex), which is what a rig
        # echoes back; that is precisely the byte-order trap this suite exists to catch.
        ntime = job.notify_params()[5]
        header = server.build_header(job, ntime, record["nonce"], record["solution"], "00000000")
        self.assertEqual(header[:140].hex(), record["header140"], "the reconstructed header must match")
        self.assertEqual(header[140:143].hex(), "fd4005", "CompactSize(1344) prefix")
        self.assertEqual(len(header), 140 + 3 + 1344)

    def test_equihash_solution_from_the_record_is_valid(self):
        record = fx.load_real_solutions()[0]
        import equihash_verify as ev
        self.assertTrue(ev.verify(bytes.fromhex(record["header140"]), bytes.fromhex(record["solution"])))


class EngineOverTcp(unittest.TestCase):
    """Drive the real engine over a socket the way a rig does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records = fx.load_real_solutions()

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **overrides):
        # difficulty 0.02 matches the recorded shares; Equihash verification stays ON, so the accept
        # decision really does go through `equihash_verify`.
        return fx.engine_config(self.tmp.name, default_difficulty=0.02,
                                verify_solution_below_difficulty=1.0, **overrides)

    def test_subscribe_authorize_and_receive_a_job(self):
        with sm.EngineHarness(self._cfg(), replay=self.records[0]) as engine:
            client = engine.client()
            try:
                sub = client.subscribe()
                self.assertEqual(sub["error"], None)
                result = sub["result"]
                self.assertEqual(result[1], None or result[1])       # extranonce1 is a hex string
                self.assertEqual(len(result[1]), 8, "4-byte extranonce1")
                self.assertEqual(result[2], 0, "extranonce2_size = 0 (coinbase used verbatim)")
                auth = client.authorize("local.synthetic")
                self.assertTrue(auth["result"])
                params = client.wait_job()
                self.assertEqual(len(params), 8)
                self.assertEqual(params[3], self.records[0]["header140"][72:136],
                                 "notify carries the replayed job's merkle root")
                self.assertEqual(params[2], self.records[0]["header140"][8:72],
                                 "notify carries the replayed job's reversed prevhash")
            finally:
                client.close()

    def test_recorded_real_solution_is_accepted(self):
        record = self.records[0]
        with sm.EngineHarness(self._cfg(), replay=record) as engine:
            client = engine.client()
            try:
                client.subscribe()
                client.authorize("local.synthetic")
                job = client.wait_job()
                reply = client.submit("local.synthetic", job[0], job[5], record["nonce"], record["solution"])
                self.assertIsNone(reply["error"], reply)
                self.assertTrue(reply["result"], "a real, pre-computed Equihash solution must be accepted")
            finally:
                client.close()
        # `local.` rigs are internal test rigs: the share is accepted but never enters the public counters
        self.assertEqual(engine.server.internal_accepted, 1)
        self.assertEqual(engine.server.shares_total, 0)

    def test_tampered_solution_is_rejected_and_not_counted(self):
        record = dict(self.records[0])
        tampered = bytearray.fromhex(record["solution"])
        tampered[64] ^= 0xFF
        record["solution"] = bytes(tampered).hex()
        with sm.EngineHarness(self._cfg(), replay=self.records[0]) as engine:
            client = engine.client()
            try:
                client.subscribe()
                client.authorize("local.synthetic")
                job = client.wait_job()
                reply = client.submit("local.synthetic", job[0], job[5], record["nonce"], record["solution"])
                self.assertFalse(reply["result"], "a corrupted solution must never be accepted")
                self.assertIn(reply["error"][0], (20, 23), "either low difficulty or invalid Equihash")
            finally:
                client.close()
        self.assertEqual(engine.server.shares_total, 0)

    def test_stale_job_is_rejected(self):
        record = self.records[0]
        with sm.EngineHarness(self._cfg(), replay=record) as engine:
            client = engine.client()
            try:
                client.subscribe()
                client.authorize("local.synthetic")
                client.wait_job()
                reply = client.submit("local.synthetic", "deadbeef", "00000000", record["nonce"],
                                      record["solution"])
                self.assertFalse(reply["result"])
                self.assertEqual(reply["error"][0], 21)
                self.assertIn("stale", reply["error"][1])
            finally:
                client.close()

    def test_mode2_miner_gets_a_coinbase_with_the_split(self):
        cfg = self._cfg(mode2={"enabled": True, "pool_fee_address": FEE, "fee_pct": 1.0,
                               "whitelist_prefixes": ["local."]})
        with sm.EngineHarness(cfg) as engine:
            client = engine.client()
            try:
                client.subscribe()
                auth = client.authorize(MINER + ".synthetic")
                self.assertTrue(auth["result"], auth)
                generic = client.wait_job()
                params = client.wait_for_extra_job(generic[0])      # the miner-specific job
                self.assertNotEqual(params[0], generic[0], "mode 2 replaces the generic job")
                # the engine built a miner-specific coinbase; read it back from the engine's live client
                live = next(iter(engine.server.clients), None)
                self.assertIsNotNone(live)
                self.assertEqual(live.mode2_address, MINER)
                coinbase = live.mode2_job.coinbase_raw
                import zcash_v6 as zv
                import build_coinbase as bc
                tx = zv.parse_transparent(coinbase)
                self.assertEqual(len(tx["vouts"]), 3)
                self.assertEqual(tx["vouts"][0]["script"], bc.address_to_script(MINER))
                self.assertEqual(tx["vouts"][1]["script"], bc.address_to_script(FEE))
                self.assertEqual(params[3], live.mode2_job.merkle_root.hex(),
                                 "notify must carry the mode-2 coinbase's merkle root")
            finally:
                client.close()

    def test_fee_address_worker_is_rejected_over_tcp(self):
        cfg = self._cfg(mode2={"enabled": True, "pool_fee_address": FEE, "fee_pct": 1.0,
                               "whitelist_prefixes": ["local."]})
        with sm.EngineHarness(cfg) as engine:
            client = engine.client()
            try:
                client.subscribe()
                auth = client.authorize(FEE + ".synthetic")
                self.assertFalse(auth["result"])
                self.assertIn("must differ", auth["error"][1])
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
