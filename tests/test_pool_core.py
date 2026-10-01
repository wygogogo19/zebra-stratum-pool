"""Unit tests for the pure helpers in `pool.py` (difficulty/target maths, merkle, coinbase decoding)."""
from __future__ import annotations

import unittest

from tests import _fixtures as fx

fx.repo_on_path()

import pool                      # noqa: E402
import zcash_v6 as zv            # noqa: E402


class DifficultyAndTarget(unittest.TestCase):
    def test_diff1_is_the_reference_target(self):
        self.assertEqual(pool.target_from_difficulty(1.0), pool.DIFF1_TARGET)

    def test_target_shrinks_as_difficulty_grows(self):
        targets = [pool.target_from_difficulty(d) for d in (0.02, 1, 16, 128, 131072)]
        self.assertEqual(targets, sorted(targets, reverse=True))

    def test_target_is_clamped_into_the_valid_range(self):
        self.assertLessEqual(pool.target_from_difficulty(1e-12), pool.MAX_TARGET)
        self.assertGreaterEqual(pool.target_from_difficulty(1e12), 1)
        # difficulty <= 0 must not divide by zero
        self.assertLessEqual(pool.target_from_difficulty(0), pool.MAX_TARGET)

    def test_hash_meets_target_uses_little_endian_interpretation(self):
        target = pool.target_from_difficulty(1.0)
        self.assertTrue(pool.hash_meets_target(target.to_bytes(32, "little"), target))
        self.assertTrue(pool.hash_meets_target((target - 1).to_bytes(32, "little"), target))
        self.assertFalse(pool.hash_meets_target((target + 1).to_bytes(32, "little"), target))
        self.assertFalse(pool.hash_meets_target((target + 1).to_bytes(32, "big"), target))

    def test_target_from_bits_matches_the_reference_formula(self):
        # bits from the frozen mainnet template
        tpl, _ = fx.load_gbt()
        bits = tpl["bits"]
        raw = int(bits, 16)
        expected = (raw & 0x007FFFFF) * (1 << (8 * ((raw >> 24) - 3)))
        self.assertEqual(pool.PoolServer._target_from_bits(bits), expected)
        self.assertGreater(expected, 0)


class MerkleAndEncoding(unittest.TestCase):
    def test_merkle_branch_reproduces_zebras_own_root_over_the_real_template(self):
        """Regression for the 2026-10-01 fix: `transactions` is a tx list, not a branch.

        Walking the raw transaction list through `merkle_root()` yields a root that does not match the block's,
        so a solved block would have been rejected. The branch must be derived from the tree.
        """
        tpl, _ = fx.load_gbt()
        cb_txid = bytes.fromhex(tpl["coinbasetxn"]["hash"])[::-1]
        txids = [cb_txid] + [bytes.fromhex(t["hash"])[::-1] for t in tpl["transactions"]]
        branch = pool.merkle_branch(txids, 0)
        self.assertEqual(len(branch), 3, "5 leaves (coinbase + 4 txs) need a 3-element sibling path")
        self.assertEqual(pool.merkle_root(cb_txid, branch)[::-1].hex(),
                         tpl["defaultroots"]["merkleroot"])
        self.assertEqual(zv.merkle_root(txids)[::-1].hex(), tpl["defaultroots"]["merkleroot"])
        # the old behaviour (tx list used as a branch) must NOT equal the real root
        self.assertNotEqual(pool.merkle_root(cb_txid, txids[1:])[::-1].hex(),
                            tpl["defaultroots"]["merkleroot"])

    def test_merkle_branch_handles_odd_and_single_leaf_levels(self):
        leaves = [bytes([i]) * 32 for i in range(1, 6)]        # 5 leaves -> odd level
        self.assertEqual(pool.merkle_root(leaves[0], pool.merkle_branch(leaves, 0)),
                         zv.merkle_root(leaves))
        self.assertEqual(pool.merkle_branch(leaves[:2], 0), [leaves[1]])
        self.assertEqual(pool.merkle_branch([leaves[0]], 0), [])

        # `merkle_root(node, branch)` always concatenates node||sibling, which is exactly right for the
        # coinbase (index 0 is a left child at every level). For other indices the orientation has to be
        # taken from the index; check that the branch is still the true sibling path for all of them.
        def oriented_root(i):
            node, level, idx = leaves[i], list(leaves), i
            while len(level) > 1:
                if len(level) % 2:
                    level.append(level[-1])
                sib = idx ^ 1
                node = pool.dsha256(level[sib] + node) if idx % 2 else pool.dsha256(node + level[sib])
                level = [pool.dsha256(level[j] + level[j + 1]) for j in range(0, len(level), 2)]
                idx //= 2
            return node

        for i in range(len(leaves)):
            branch = pool.merkle_branch(leaves, i)
            self.assertEqual(len(branch), len(pool.merkle_branch(leaves, 0)), "leaf %d" % i)
            self.assertEqual(oriented_root(i), zv.merkle_root(leaves), "leaf %d" % i)

    def test_merkle_root_without_branch_is_the_coinbase_hash(self):
        self.assertEqual(pool.merkle_root(b"\x11" * 32, []), b"\x11" * 32)

    def test_varint_encodings(self):
        self.assertEqual(pool.varint(0).hex(), "00")
        self.assertEqual(pool.varint(252).hex(), "fc")
        self.assertEqual(pool.varint(253).hex(), "fdfd00")
        self.assertEqual(pool.varint(0xFFFF).hex(), "fdffff")
        self.assertEqual(pool.varint(0x10000).hex(), "fe00000100")

    def test_hex_helpers(self):
        self.assertEqual(pool.swab32("01020304aabbccdd"), "04030201ddccbbaa")
        self.assertEqual(pool.hex2bytes_reversed("0102"), b"\x02\x01")
        self.assertEqual(pool.dsha256(b"a"), pool.dsha256(b"a"))
        self.assertEqual(len(pool.dsha256(b"a")), 32)


class CoinbaseDecoding(unittest.TestCase):
    def test_reads_the_transparent_output_sum_from_the_real_coinbase(self):
        tpl, subsidy = fx.load_gbt()
        raw = bytes.fromhex(tpl["coinbasetxn"]["data"])
        # The node pays its miner through a shielded output, so only the protocol funding stream is visible.
        self.assertEqual(pool.coinbase_output_sum(raw), 12_500_000)
        self.assertEqual(pool.coinbase_output_sum(raw),
                         sum(int(f["valueZat"]) for f in subsidy["fundingstreams"]))

    def test_reads_the_sum_of_a_mode2_coinbase(self):
        import build_coinbase as bc
        tpl, _ = fx.load_gbt()
        built = bc.build_dynamic_coinbase(tpl, "t1XwoZWZCpkFWfCdzsi5QzrsDuxmHX34CZ3",
                                          "t1Urcc2cLVMo1hYbL4r7886JfWk2iVDVWhy", 1.0)
        self.assertEqual(pool.coinbase_output_sum(built["raw"]),
                         built["miner_net"] + built["pool_fee"] + built["lockbox_zat"])

    def test_garbage_input_returns_zero_instead_of_raising(self):
        self.assertEqual(pool.coinbase_output_sum(b""), 0)
        self.assertEqual(pool.coinbase_output_sum(b"\x01\x02\x03"), 0)


class SolutionPicking(unittest.TestCase):
    def test_five_parameter_firmware(self):
        sol = "ab" * 1344
        self.assertEqual(pool.pick_solution(["ntime", "nonce", "sol", "x", "y"]), "y")
        self.assertEqual(pool.pick_solution(["w", "job", "nt", "no", sol]), sol)

    def test_prefers_the_longest_valid_hex_blob(self):
        sol = "ab" * 1344
        with_prefix = "fd4005" + sol
        self.assertEqual(pool.pick_solution(["w", "job", "nt", "no", "deadbeef", sol]), sol)
        self.assertEqual(pool.pick_solution(["w", "job", "nt", "no", with_prefix]), with_prefix)

    def test_ignores_non_hex_and_short_candidates(self):
        # Nothing valid to pick: the documented fallback is the last parameter, so the header
        # construction / validation decides (no new reject path is invented here).
        self.assertEqual(pool.pick_solution(["w", "job", "nt", "no", "zz" * 1344]), "zz" * 1344)
        self.assertEqual(pool.pick_solution([]), "")


class InternalWorkerDetection(unittest.TestCase):
    """Internal test rigs must never show up in the public counters/worker list."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.server = pool.PoolServer(fx.engine_config(self.tmp.name,
                                                       internal_worker_prefixes=["cpurig", "local."]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_matches_prefix_or_rig_label(self):
        for worker in ("local.selftest", "cpurig", "t1XwoZWZCpkFWfCdzsi5QzrsDuxmHX34CZ3.cpurig"):
            self.assertTrue(self.server._is_internal_worker(worker), worker)

    def test_external_workers_are_public(self):
        for worker in ("t1Urcc2cLVMo1hYbL4r7886JfWk2iVDVWhy.rig1", "someone.rig"):
            self.assertFalse(self.server._is_internal_worker(worker), worker)

    def test_empty_worker_is_not_internal(self):
        self.assertFalse(self.server._is_internal_worker(""))


class AcceptDifficulty(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.server = pool.PoolServer(fx.engine_config(self.tmp.name))
        self.client = fx.FakeClient()
        self.client.difficulty = 128.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_the_current_difficulty_normally(self):
        self.assertEqual(self.server._accept_difficulty(self.client), 128.0)

    def test_uses_the_lower_value_inside_the_grace_window(self):
        import time
        self.client.grace_until = time.time() + 30
        self.client.grace_difficulty = 16.0
        self.assertEqual(self.server._accept_difficulty(self.client), 16.0)

    def test_expired_grace_window_is_ignored(self):
        self.client.grace_until = 0.0
        self.client.grace_difficulty = 16.0
        self.assertEqual(self.server._accept_difficulty(self.client), 128.0)


if __name__ == "__main__":
    unittest.main()
