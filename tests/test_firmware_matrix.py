"""Executable backing for `docs/FIRMWARE-COMPATIBILITY.md`.

The compatibility matrix claims that four shapes of `mining.submit` from four different firmware
generations all land on the same accepted share, and that the pool advertises exactly the capabilities it
documents. A claim in a table is cheap; these tests drive the real engine over a real socket and assert it.

The share used throughout is one the **production pool accepted** (a real 140-byte header, a real 32-byte
nonce and a real 1344-byte Equihash(200,9) solution), so "accepted" here means the engine's real
validation and its real Equihash verifier said yes.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from tests import _fixtures as fx
from tests import synthetic_miner as sm

fx.repo_on_path()

import pool                      # noqa: E402

MINER = "t1XwoZWZCpkFWfCdzsi5QzrsDuxmHX34CZ3"
Z15_NONCE_BYTES = 28             # Antminer-style: the rig sends 28 bytes, the pool prepends extranonce1


class AdvertisedCapabilities(unittest.TestCase):
    """What the pool tells a connecting rig it supports — the facts the matrix describes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records = fx.load_real_solutions()

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **overrides):
        return fx.engine_config(self.tmp.name, default_difficulty=0.02,
                                verify_solution_below_difficulty=1.0, **overrides)

    def test_subscribe_advertises_extranonce2_size_zero(self):
        """The coinbase is Zebra's, used verbatim, so the pool does not hand out extranonce2 space."""
        with sm.EngineHarness(self._cfg(), replay=self.records[0]) as engine:
            client = engine.client()
            try:
                result = client.subscribe()["result"]
                self.assertEqual(result[2], 0, "extranonce2_size must be advertised as 0")
                self.assertEqual(len(result[1]), 8, "extranonce1 is 4 bytes (8 hex chars)")
                self.assertEqual([[m, v] for m, v in result[0]],
                                 [["mining.set_difficulty", "1"], ["mining.notify", "1"]],
                                 "the advertised subscription list must stay stable")
            finally:
                client.close()

    def test_notify_carries_the_eight_header_fields(self):
        """Header-style notify (job/version/prevhash/merkleroot/commitments/ntime/bits/clean)."""
        with sm.EngineHarness(self._cfg(), replay=self.records[0]) as engine:
            client = engine.client()
            try:
                client.subscribe()
                client.authorize("%s.rig1" % MINER)
                params = client.wait_job()
                self.assertEqual(len(params), 8)
            finally:
                client.close()

    def test_extranonce_subscribe_is_acknowledged(self):
        with sm.EngineHarness(self._cfg(), replay=self.records[0]) as engine:
            client = engine.client()
            try:
                reply = client.call("mining.extranonce.subscribe", [])
                self.assertTrue(reply["result"])
                self.assertIsNone(reply["error"])
            finally:
                client.close()

    def test_version_rolling_is_not_advertised(self):
        """`mining.configure` (BIP310 version-rolling) is not implemented; the pool must say so, not stall."""
        with sm.EngineHarness(self._cfg(), replay=self.records[0]) as engine:
            client = engine.client()
            try:
                reply = client.call("mining.configure", [["version-rolling"], {}])
                self.assertIsNone(reply["result"])
                self.assertIsNotNone(reply["error"], "an unsupported method must return an error, not silence")
            finally:
                client.close()


class SubmitShapes(unittest.TestCase):
    """Four firmware generations, four `mining.submit` shapes, one accepted share."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.record = fx.load_real_solutions()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **overrides):
        return fx.engine_config(self.tmp.name, default_difficulty=0.02,
                                verify_solution_below_difficulty=1.0, **overrides)

    def _ntime(self) -> str:
        return sm.replay_job(self.record).notify_params()[5]

    def _assert_accepted(self, params: list) -> None:
        with sm.EngineHarness(self._cfg(), replay=self.record) as engine:
            client = engine.client()
            try:
                client.subscribe()
                client.authorize("%s.rig1" % MINER)
                client.wait_job()
                reply = client.submit_params(params)
                self.assertEqual(reply["error"], None, "expected accept, got %r" % (reply["error"],))
                self.assertTrue(reply["result"])
            finally:
                client.close()

    def test_five_parameter_submit_is_accepted(self):
        """`[worker, job_id, ntime, nonce, solution]` — most Zcash firmware."""
        r = self.record
        self._assert_accepted([MINER + ".rig1", r["job_id"], self._ntime(), r["nonce"], r["solution"]])

    def test_six_parameter_submit_is_accepted(self):
        """`[worker, job_id, ntime, nonce, extranonce2, solution]` — BTC-style clients.

        The pool advertises `extranonce2_size = 0`, yet such a client still sends the (empty) field. The
        solution must be picked by length, otherwise the pool would read `extranonce2` as the solution and
        reject a perfectly good share — the failure mode a miner sees as "connected but every share errors".
        """
        r = self.record
        self._assert_accepted([MINER + ".rig1", r["job_id"], self._ntime(), r["nonce"], "00000000", r["solution"]])

    def test_compact_size_prefixed_solution_is_accepted(self):
        """Some firmware sends the solution with its CompactSize length prefix (`fd4005` + 1344 bytes)."""
        r = self.record
        self._assert_accepted([MINER + ".rig1", r["job_id"], self._ntime(), r["nonce"], "fd4005" + r["solution"]])

    def test_antminer_z15_28_byte_nonce_is_accepted(self):
        """Antminer Z15 sends 28 nonce bytes; the 32-byte header nonce is `extranonce1 ‖ miner nonce`.

        The recorded share's nonce is `extranonce1 ‖ 28 bytes`, so pinning the session's extranonce1 to the
        one from the recording lets us submit the *short* shape and reproduce the accepted header exactly.
        """
        r = self.record
        extranonce1 = r["nonce"][:8]                 # 4 bytes, as the recorded session advertised it
        miner_nonce = r["nonce"][8:]                 # 28 bytes, what the rig actually sends
        self.assertEqual(len(miner_nonce), Z15_NONCE_BYTES * 2, "the recorded nonce is the short shape")

        def fake_urandom(n: int) -> bytes:
            return bytes.fromhex(extranonce1).ljust(n, b"\x00")[:n]

        with mock.patch("os.urandom", fake_urandom):
            with sm.EngineHarness(self._cfg(), replay=self.record) as engine:
                client = engine.client()
                try:
                    result = client.subscribe()["result"]
                    self.assertEqual(result[1], extranonce1, "session extranonce1 must match the recording")
                    client.authorize("%s.rig1" % MINER)
                    client.wait_job()
                    reply = client.submit_params([MINER + ".rig1", r["job_id"], self._ntime(),
                                                  miner_nonce, r["solution"]])
                finally:
                    client.close()
        self.assertEqual(reply["error"], None, "28-byte nonce must be completed with extranonce1, not padded")
        self.assertTrue(reply["result"])


if __name__ == "__main__":
    unittest.main()
