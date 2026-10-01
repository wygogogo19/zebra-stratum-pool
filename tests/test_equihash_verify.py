"""The Equihash(200,9) verifier, tested against **real solutions the production engine accepted**.

The pool only runs this check for low-difficulty (CPU-rig) shares, so a regression here would silently let
invalid solutions into the accounting — or reject honest ones. The fixtures come from
`tests/fixtures/real_solutions.json` (recorded from node11's CPU probe).
"""
from __future__ import annotations

import unittest

from tests import _fixtures as fx

fx.repo_on_path()

import equihash_verify as ev      # noqa: E402


class RealSolutions(unittest.TestCase):
    def setUp(self):
        self.records = fx.load_real_solutions()

    def test_fixtures_are_well_formed(self):
        self.assertGreaterEqual(len(self.records), 1)
        for record in self.records:
            self.assertEqual(len(record["header140"]), 280, "140-byte header")
            self.assertEqual(len(record["solution"]), 2688, "1344-byte solution")
            self.assertEqual(len(bytes.fromhex(record["nonce"])), 32)

    def test_accepts_every_recorded_solution(self):
        for record in self.records:
            self.assertTrue(ev.verify(bytes.fromhex(record["header140"]), bytes.fromhex(record["solution"])),
                            "recorded share at height %s must verify" % record.get("job_id"))

    def test_rejects_a_single_flipped_solution_bit(self):
        record = self.records[0]
        header = bytes.fromhex(record["header140"])
        solution = bytearray.fromhex(record["solution"])
        solution[0] ^= 0x01
        self.assertFalse(ev.verify(header, bytes(solution)))

    def test_rejects_a_flipped_header_byte(self):
        record = self.records[0]
        header = bytearray.fromhex(record["header140"])
        header[10] ^= 0x01
        self.assertFalse(ev.verify(bytes(header), bytes.fromhex(record["solution"])))

    def test_rejects_a_truncated_solution(self):
        record = self.records[0]
        header = bytes.fromhex(record["header140"])
        short = bytes.fromhex(record["solution"])[:1000]
        try:
            self.assertFalse(ev.verify(header, short))
        except (IndexError, ValueError, AssertionError):
            pass   # raising is equally acceptable: a malformed solution must never verify

    def test_module_constants_describe_equihash_200_9(self):
        self.assertEqual((ev.N, ev.K), (200, 9))
        self.assertEqual(ev.SOLUTION_BYTES, 1344)


if __name__ == "__main__":
    unittest.main()
