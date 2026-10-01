"""Job construction + the 8-parameter `mining.notify` contract.

The contract test pins the byte order of every field: a mistake here makes every share (and every real
block) invalid, which is exactly what happened in the 2026-09-14/15 incident. The merkle-root test pins the
2026-10-01 fix: the job must commit to the *block's* merkle root, which is a tree over
[coinbase, tx1, tx2, ...] — not a walk over the transaction list.
"""
from __future__ import annotations

import asyncio
import io
import struct
import unittest
from contextlib import redirect_stdout

from tests import _fixtures as fx

fx.repo_on_path()

import pool                      # noqa: E402
import zcash_v6 as zv            # noqa: E402


def make_server(tmpdir, **cfg_overrides):
    server = pool.PoolServer(fx.engine_config(tmpdir, **cfg_overrides))
    server.rpc = fx.StubRPC()
    server.jobs.rpc = server.rpc
    return server


class JobConstruction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = make_server(cls.tmp.name)
        cls.tpl, cls.subsidy = fx.load_gbt()
        cls.job = asyncio.run(cls.server.jobs.refresh())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_job_is_built_from_the_template(self):
        self.assertIsNotNone(self.job)
        self.assertEqual(self.job.height, int(self.tpl["height"]))
        self.assertEqual(self.job.version, int(self.tpl["version"]))
        self.assertEqual(self.job.prevhash_display, self.tpl["previousblockhash"])
        self.assertEqual(self.job.bits, self.tpl["bits"])
        self.assertEqual(self.job.commitments_hash, bytes.fromhex(
            self.tpl["blockcommitmentshash"])[::-1])
        self.assertEqual(self.job.coinb1, bytes.fromhex(self.tpl["coinbasetxn"]["data"]))
        self.assertEqual(self.job.coinb2, b"", "the node's coinbase is used verbatim")

    def test_merkle_root_commits_to_the_real_block(self):
        """Regression: the root must equal Zebra's own merkleroot for the same template."""
        self.assertEqual(self.job.merkle_root[::-1].hex(), self.tpl["defaultroots"]["merkleroot"])
        # the coinbase txid comes from the node (`coinbasetxn.hash`): a Zcash v5/v6 txid is a ZIP-244
        # digest, not dsha256(raw), and a shielded coinbase cannot be re-derived from the raw bytes at all
        txids = [bytes.fromhex(self.tpl["coinbasetxn"]["hash"])[::-1]] + \
                [bytes.fromhex(t["hash"])[::-1] for t in self.tpl["transactions"]]
        self.assertEqual(self.job.merkle_root, zv.merkle_root(txids))

    def test_merkle_branch_is_the_coinbases_sibling_path(self):
        cb_txid = bytes.fromhex(self.tpl["coinbasetxn"]["hash"])[::-1]
        txids = [cb_txid] + \
                [bytes.fromhex(t["hash"])[::-1] for t in self.tpl["transactions"]]
        self.assertEqual(self.job.merkle_branch, pool.merkle_branch(txids, 0))
        self.assertEqual(pool.merkle_root(cb_txid, self.job.merkle_branch),
                         self.job.merkle_root)

    def test_coinbase_value_comes_from_the_coinbase_itself(self):
        # Zebra's GBT has no `coinbasevalue`; the transparent funding-stream output is what we can read.
        self.assertEqual(self.job.coinbase_value, 12_500_000)
        self.assertEqual(self.job.subsidy_miner, float(self.subsidy["miner"]))
        self.assertEqual(self.job.subsidy_total, float(self.subsidy["totalblocksubsidy"]))

    def test_job_id_is_stable_for_an_unchanged_template(self):
        again = asyncio.run(self.server.jobs.refresh())
        self.assertIs(again, self.job, "same template must not rotate the job (miners would reset)")
        self.assertEqual(again.job_id, self.job.job_id)

    def test_new_template_rotates_the_job_and_keeps_the_previous_one(self):
        changed = dict(self.tpl)
        changed["height"] = int(self.tpl["height"]) + 1
        changed["previousblockhash"] = "ab" * 32
        self.server.rpc.template = changed
        new_job = asyncio.run(self.server.jobs.refresh())
        self.assertIsNotNone(new_job)
        self.assertNotEqual(new_job.job_id, self.job.job_id)
        self.assertIs(self.server.jobs.previous, self.job)

    def test_refresh_returns_none_when_the_node_fails(self):
        class _Broken(fx.StubRPC):
            def call(self, method, params=None, timeout=30.0):
                raise RuntimeError("node unreachable")

        server = make_server(self.tmp.name)
        server.rpc = _Broken()
        server.jobs.rpc = server.rpc
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertIsNone(asyncio.run(server.jobs.refresh()))

    def test_build_job_rejects_a_template_without_commitments(self):
        tpl = dict(self.tpl)
        tpl.pop("blockcommitmentshash", None)
        tpl.pop("finalsaplingroothash", None)      # the code falls back to this key, so drop both
        with self.assertLogs(level="ERROR") as logs:
            self.assertIsNone(self.server.jobs._build_job(tpl, self.subsidy))
        self.assertTrue(any("blockcommitmentshash" in m for m in logs.output))


class NotifyContract(unittest.TestCase):
    """`mining.notify` must carry exactly 8 parameters, each in the header's internal byte order."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tpl, _ = fx.load_gbt()
        cls.server = make_server(cls.tmp.name)
        cls.job = asyncio.run(cls.server.jobs.refresh())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exactly_eight_parameters(self):
        params = self.job.notify_params(clean=True)
        self.assertEqual(len(params), 8, "the golden 8-parameter contract is frozen")
        self.assertIs(params[7], True)
        self.assertIs(self.job.notify_params(clean=False)[7], False)

    def test_field_byte_order(self):
        params = self.job.notify_params()
        self.assertEqual(params[0], self.job.job_id)
        self.assertEqual(params[1], struct.pack("<I", self.job.version).hex(), "version: little endian")
        self.assertEqual(params[2], bytes.fromhex(self.job.prevhash_display)[::-1].hex(),
                         "prevhash: fully reversed into internal order")
        self.assertEqual(params[3], self.job.merkle_root.hex(), "merkleroot: internal order, as-is")
        self.assertEqual(params[4], self.job.commitments_hash.hex(), "commitments: internal order, as-is")
        self.assertEqual(params[5], struct.pack("<I", self.job.curtime).hex(), "ntime: little endian")
        self.assertEqual(params[6], bytes.fromhex(self.job.bits)[::-1].hex(), "bits: little endian")

    def test_header_reconstruction_matches_the_notified_fields(self):
        """The header the pool validates must be the header the miner builds from the notify payload."""
        params = self.job.notify_params()
        ntime, nonce = params[5], "11" * 32
        header = self.server.build_header(self.job, ntime, nonce, None, "deadbeef")
        self.assertEqual(header[0:4], bytes.fromhex(params[1]))
        self.assertEqual(header[4:36], bytes.fromhex(params[2]))
        self.assertEqual(header[36:68], bytes.fromhex(params[3]))
        self.assertEqual(header[68:100], bytes.fromhex(params[4]))
        self.assertEqual(header[100:104], bytes.fromhex(params[5]))
        self.assertEqual(header[104:108], bytes.fromhex(params[6]))
        self.assertEqual(header[108:140], bytes.fromhex(nonce))
        self.assertEqual(len(header), 140)

    def test_solution_length_prefix_is_appended_once(self):
        solution = "ab" * 1344
        with_prefix = "fd4005" + solution
        for candidate in (solution, with_prefix):
            header = self.server.build_header(self.job, "11" * 4, "22" * 32, candidate, "deadbeef")
            self.assertEqual(len(header), 140 + 3 + 1344)
            self.assertEqual(header[140:143].hex(), "fd4005", "CompactSize(1344) once, never twice")
            self.assertEqual(header[143:], bytes.fromhex(solution))

    def test_nonce_topology_extranonce1_then_miner_nonce(self):
        miner_nonce = "aa" * 28                      # a Z15 submits 28 bytes
        header = self.server.build_header(self.job, "11" * 4, miner_nonce, None, "deadbeef")
        self.assertEqual(header[108:140].hex(), "deadbeef" + miner_nonce)
        # a 32-byte nonce is used verbatim
        full = "bb" * 32
        header = self.server.build_header(self.job, "11" * 4, full, None, "deadbeef")
        self.assertEqual(header[108:140].hex(), full)


if __name__ == "__main__":
    unittest.main()
