"""Unit tests for `build_coinbase.py` — the on-chain 99/1/Lockbox split (ZIP-244 mode 2).

Everything here runs **offline against real mainnet data**: the template in `tests/fixtures/` is a
verbatim `getblocktemplate` response from our production Zebra node, including the coinbase whose
merkle root / auth-data root / block-commitments hash Zebra itself publishes in `defaultroots`.
That lets the suite check the builder against the node's own numbers instead of against itself.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests import _fixtures as fx

fx.repo_on_path()

import build_coinbase as bc      # noqa: E402
import zcash_v6 as zv            # noqa: E402

MINER = "t1XwoZWZCpkFWfCdzsi5QzrsDuxmHX34CZ3"
FEE = "t1Urcc2cLVMo1hYbL4r7886JfWk2iVDVWhy"
LOCKBOX_SCRIPT = "a914c20cd5bdf7964ca61764db66bc2531b1792a084d87"
# Frozen 2026-10-01 from the fixture below; a change here means the coinbase serialisation or the
# digest computation changed — which would be a consensus-visible regression, so it must be deliberate.
GOLDEN_TXID = "f36e2cc898cf3fe64c80ff6d2370cd258d1153a84223ead8c61d86b01792d382"
GOLDEN_AUTH = "302565fa59034e06eef575c1e1689233e508291cf6b36b72a2ec01891322cb1f"


class SubsidyModel(unittest.TestCase):
    """subsidy(h) = 1.25e9 zat >> floor(h / 1_048_576), per the protocol inflation curve."""

    def test_halving_schedule(self):
        self.assertEqual(bc.subsidy_zat(0), 1_250_000_000)
        self.assertEqual(bc.subsidy_zat(1_048_575), 1_250_000_000)
        self.assertEqual(bc.subsidy_zat(1_048_576), 625_000_000)
        self.assertEqual(bc.subsidy_zat(2_097_152), 312_500_000)

    def test_current_height_total_subsidy_and_the_miner_share_agree_with_the_node(self):
        # 3,502,592 is 3 halvings past the first, so the total subsidy is 1.25e9 >> 3 = 1.5625 ZEC;
        # the miner side (0.8 x) is the 1.25 ZEC that getblocksubsidy reports for the same fixture.
        tpl, subsidy = fx.load_gbt()
        total = bc.subsidy_zat(int(tpl["height"]))
        self.assertEqual(total, 156_250_000)
        self.assertEqual(total * 8 // 10, int(subsidy["miner"] * 1e8))

    def test_extreme_height_does_not_underflow(self):
        # The shift is clamped to 63 bits, so the value never goes negative and eventually reaches 0.
        self.assertEqual(bc.subsidy_zat(63 * 1_048_576), 0)
        self.assertGreaterEqual(bc.subsidy_zat(10 ** 12), 0)
        self.assertGreaterEqual(bc.subsidy_zat(4 * 1_048_576), bc.subsidy_zat(5 * 1_048_576))


class AddressHelpers(unittest.TestCase):
    def test_base58_roundtrip(self):
        for addr in (MINER, FEE, "t1Urcc2cLVMo1hYbL4r7886JfWk2iVDVWhy"):
            self.assertEqual(bc.b58_encode(bc.b58_decode(addr)), addr)

    def test_t1_is_p2pkh_and_t3_is_p2sh(self):
        self.assertEqual(bc.address_to_script(MINER), bytes.fromhex("76a9149a5d11fa92cb89bf5682c737fbcd2d46f32a7f5b88ac"))
        self.assertEqual(bc.address_to_script(FEE), bytes.fromhex("76a9147879778ba12d7cf2bbadf875e88ec388b25dc93e88ac"))
        # the protocol lockbox from the real template (P2SH)
        self.assertEqual(bc.address_to_script("t3cFfPt1Bcvgez9ZbMBFWeZsskxTkPzGCow")[:2], b"\xa9\x14")

    def test_script_to_address_roundtrip(self):
        for addr in (MINER, FEE):
            self.assertEqual(bc.script_to_address(bc.address_to_script(addr)), addr)
        self.assertTrue(bc.script_to_address(b"\x00\x01\x02").startswith("nonstandard:"))

    def test_rejects_corrupted_checksum(self):
        bad = MINER[:-1] + ("A" if MINER[-1] != "A" else "B")
        with self.assertRaises(ValueError) as ctx:
            bc.address_to_script(bad)
        self.assertIn("checksum", str(ctx.exception))

    def test_rejects_non_mainnet_transparent_prefix(self):
        with self.assertRaises(ValueError) as ctx:
            bc.address_to_script("t1" + "1" * 32)
        self.assertTrue("checksum" in str(ctx.exception) or "transparent" in str(ctx.exception))

    def test_rejects_prefixed_address_of_other_network(self):
        # A valid base58check string with a non-Zcash prefix must not be accepted.
        payload = b"\x00\x11" + b"\x22" * 20            # 0x0011 is not t1/t3
        import hashlib
        chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        with self.assertRaises(ValueError) as ctx:
            bc.address_to_script(bc.b58_encode(payload + chk))
        self.assertIn("t1/t3", str(ctx.exception))

    def test_rejects_address_with_wrong_payload_length(self):
        payload = b"\x1c\xb8" + b"\x33" * 19            # correct prefix, 19-byte hash160
        import hashlib
        chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        with self.assertRaises(ValueError) as ctx:
            bc.address_to_script(bc.b58_encode(payload + chk))
        self.assertIn("length", str(ctx.exception))

    def test_script_to_address_decodes_p2sh_and_round_trips(self):
        script = b"\xa9\x14" + b"\x11" * 20 + b"\x87"   # the protocol lockbox shape
        addr = bc.script_to_address(script)
        self.assertTrue(addr.startswith("t3"))
        self.assertEqual(bc.address_to_script(addr), script)


class SplitModel(unittest.TestCase):
    """The split arithmetic and output layout, checked on the real mainnet template."""

    @classmethod
    def setUpClass(cls):
        cls.tpl, cls.subsidy = fx.load_gbt()
        cls.built = bc.build_dynamic_coinbase(cls.tpl, MINER, FEE, 1.0)

    def test_subsidy_and_fee_arithmetic(self):
        b = self.built
        subsidy = bc.subsidy_zat(int(self.tpl["height"]))
        self.assertEqual(b["subsidy"], subsidy)
        self.assertEqual(b["fees"], sum(int(t.get("fee", 0)) for t in self.tpl["transactions"]))
        self.assertEqual(b["miner_total"], subsidy * 8 // 10 + b["fees"])
        self.assertEqual(b["pool_fee"], int(b["miner_total"] * 1.0 / 100.0))
        self.assertEqual(b["miner_net"], b["miner_total"] - b["pool_fee"])
        self.assertEqual(b["lockbox_zat"], subsidy * 8 // 100)

    def test_reported_subsidy_matches_the_node_side_calculation(self):
        # getblocksubsidy (from the same fixture) must agree with our own numbers:
        # miner = 0.8 x subsidy, and the protocol funding stream (the P2SH output we preserve) = 0.08 x subsidy.
        self.assertAlmostEqual(self.subsidy["miner"] * 1e8, self.built["subsidy"] * 8 // 10)
        funding = sum(int(f["valueZat"]) for f in self.subsidy.get("fundingstreams", []))
        self.assertEqual(funding, self.built["subsidy"] * 8 // 100)
        self.assertEqual(self.subsidy["totalblocksubsidy"] * 1e8,
                         self.subsidy["miner"] * 1e8 + funding
                         + sum(int(f["valueZat"]) for f in self.subsidy.get("lockboxstreams", [])))

    def test_output_layout_and_address_isolation(self):
        vouts = self.built["tx"]["vouts"]
        self.assertEqual(len(vouts), 3, "miner + pool + protocol lockbox, in that order")
        self.assertEqual(vouts[0]["script"], bc.address_to_script(MINER))
        self.assertEqual(vouts[1]["script"], bc.address_to_script(FEE))
        self.assertEqual(vouts[2]["script"].hex(), LOCKBOX_SCRIPT)
        self.assertNotEqual(vouts[0]["script"], vouts[1]["script"], "miner and pool must be different addresses")
        self.assertEqual([v["value"] for v in vouts],
                         [self.built["miner_net"], self.built["pool_fee"], self.built["lockbox_zat"]])

    def test_payouts_never_exceed_the_consensus_cap(self):
        b = self.built
        total = b["miner_net"] + b["pool_fee"] + b["lockbox_zat"]
        self.assertLessEqual(total, b["subsidy"] * 88 // 100 + b["fees"])
        # the miner keeps ~99% of what the protocol gives the miner side
        self.assertGreater(b["miner_net"] / (b["subsidy"] * 8 // 10), 0.985)

    def test_fee_percentage_is_honoured(self):
        for pct in (0.0, 0.5, 1.0, 2.5):
            built = bc.build_dynamic_coinbase(self.tpl, MINER, FEE, pct)
            self.assertEqual(built["pool_fee"], int(built["miner_total"] * pct / 100.0))
            self.assertEqual(built["miner_net"] + built["pool_fee"], built["miner_total"])

    def test_serialisation_is_deterministic_and_reparsable(self):
        self.assertEqual(zv.serialize_transparent_tx(zv.parse_transparent(self.built["raw"])), self.built["raw"])
        again = bc.build_dynamic_coinbase(self.tpl, MINER, FEE, 1.0)
        self.assertEqual(again["raw"], self.built["raw"])

    def test_txid_and_auth_digest_regression(self):
        self.assertEqual(self.built["txid"][::-1].hex(), GOLDEN_TXID)
        self.assertEqual(self.built["auth_digest"][::-1].hex(), GOLDEN_AUTH)

    def test_script_sig_is_copied_verbatim_from_the_template(self):
        ref = zv.parse_transparent(bytes.fromhex(self.tpl["coinbasetxn"]["data"]))
        self.assertEqual(self.built["script_sig"], ref["vins"][0]["script"])

    def test_refuses_when_template_has_no_lockbox_output(self):
        tx = zv.parse_transparent(bytes.fromhex(self.tpl["coinbasetxn"]["data"]))
        tx = dict(tx)
        tx["vouts"] = [v for v in tx["vouts"]
                       if not (len(v["script"]) == 23 and v["script"][:2] == b"\xa9\x14")]
        tpl = dict(self.tpl)
        tpl["coinbasetxn"] = dict(self.tpl["coinbasetxn"],
                                  data=zv.serialize_transparent_tx(tx).hex())
        with self.assertRaises(RuntimeError) as ctx:
            bc.build_dynamic_coinbase(tpl, MINER, FEE, 1.0)
        self.assertIn("lockbox", str(ctx.exception))


class BlockRoots(unittest.TestCase):
    """`block_roots()` must reproduce the tree/commitment values Zebra itself computes."""

    @classmethod
    def setUpClass(cls):
        cls.tpl, _ = fx.load_gbt()

    def test_matches_zebra_defaultroots_when_using_the_nodes_own_coinbase(self):
        cb = self.tpl["coinbasetxn"]
        txid = bytes.fromhex(cb["hash"])[::-1]
        auth = bytes.fromhex(cb["authdigest"])[::-1]
        roots = bc.block_roots(self.tpl, txid, auth)
        default = self.tpl["defaultroots"]
        self.assertEqual(roots["merkleroot"][::-1].hex(), default["merkleroot"])
        self.assertEqual(roots["authdataroot"][::-1].hex(), default["authdataroot"])
        self.assertEqual(roots["blockcommitmentshash"][::-1].hex(), default["blockcommitmentshash"])
        self.assertEqual(roots["chainhistoryroot"][::-1].hex(), default["chainhistoryroot"])

    def test_roots_follow_our_own_coinbase(self):
        built = bc.build_dynamic_coinbase(self.tpl, MINER, FEE, 1.0)
        roots = bc.block_roots(self.tpl, built["txid"], built["auth_digest"])
        txids = [built["txid"]] + [bytes.fromhex(t["hash"])[::-1] for t in self.tpl["transactions"]]
        self.assertEqual(roots["merkleroot"], zv.merkle_root(txids))
        # a different coinbase must move the roots (otherwise the block would not commit to it)
        other = bc.build_dynamic_coinbase(self.tpl, FEE, MINER, 1.0)
        self.assertNotEqual(bc.block_roots(self.tpl, other["txid"], other["auth_digest"])["merkleroot"],
                            roots["merkleroot"])


class RpcAndSelftest(unittest.TestCase):
    """`make_rpc()` and `selftest()` — exercised with stubs so no node is required."""

    def test_make_rpc_uses_the_cookie_and_builds_a_basic_auth_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie = os.path.join(tmp, "cookie")
            with open(cookie, "w", encoding="utf-8") as fh:
                fh.write("__cookie__:secret\n")
            captured = {}

            class _Resp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            def fake_urlopen(req, timeout=None):
                captured["url"] = req.full_url
                captured["auth"] = req.headers.get("Authorization")
                captured["body"] = json.loads(req.data.decode())
                return _Resp(b'{"result": 42, "error": null}')

            with mock.patch("urllib.request.urlopen", fake_urlopen), \
                    mock.patch.dict(os.environ, {"ZEBRA_RPC_URL": "http://example.invalid:8232/"}):
                rpc = bc.make_rpc.__globals__  # noqa: F841  (keeps the intent explicit)
                with mock.patch("builtins.open", mock.mock_open(read_data="__cookie__:secret\n")):
                    rpc = bc.make_rpc()
                self.assertEqual(rpc("getblockcount", []), 42)
            self.assertEqual(captured["url"], "http://example.invalid:8232/")
            self.assertTrue(captured["auth"].startswith("Basic "))
            self.assertEqual(captured["body"]["method"], "getblockcount")

    @staticmethod
    def _transparent_block_stub(auth_mismatch: bool = False):
        """A `selftest()`-compatible RPC stub whose block carries our *transparent* mode-2 coinbase."""
        tpl, _ = fx.load_gbt()
        built = bc.build_dynamic_coinbase(tpl, MINER, FEE, 1.0)
        tx = {"hex": built["raw"].hex(), "txid": built["txid"][::-1].hex(),
              "authdigest": ("00" * 32 if auth_mismatch else built["auth_digest"][::-1].hex())}

        class _Stub:
            def __init__(self):
                self.calls = []
                self._tpl, self._sub = fx.load_gbt()

            def __call__(self, method, params=None):
                self.calls.append(method)
                if method == "getblocktemplate":
                    return self._tpl
                if method == "getblocksubsidy":
                    return self._sub
                if method == "getblockcount":
                    return 5
                if method == "getblockhash":
                    return "00" * 32
                if method == "getblock":
                    return {"tx": [tx]}
                raise AssertionError(method)

        return _Stub()

    def test_selftest_passes_on_a_transparent_coinbase(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = bc.selftest(self._transparent_block_stub())
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("bytes+txid", out.getvalue())

    def test_selftest_fails_when_the_auth_digest_differs(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = bc.selftest(self._transparent_block_stub(auth_mismatch=True))
        self.assertEqual(rc, 1)
        self.assertIn("auth digest mismatch", out.getvalue())

    def test_selftest_reports_failure_when_no_transparent_coinbase_is_found(self):
        class _Empty:
            def __call__(self, method, params=None):
                if method == "getblockcount":
                    return 5
                if method == "getblockhash":
                    return "00" * 32
                if method == "getblock":
                    return {"tx": [{"hex": "00", "txid": "00", "authdigest": None}]}
                raise AssertionError(method)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = bc.selftest(_Empty())
        self.assertEqual(rc, 1, "0/0 checked must not be reported as success")

    def test_selftest_skips_older_and_shielded_coinbases(self):
        """Both skip branches: a pre-v6 transaction, and a v6 coinbase with shielded outputs."""
        tpl, _ = fx.load_gbt()
        shielded_hex = tpl["coinbasetxn"]["data"]        # the node's own coinbase: v6 + shielded payout

        class _Mixed:
            def __init__(self):
                self.n = 0

            def __call__(self, method, params=None):
                self.n += 1
                if method == "getblockcount":
                    return 2
                if method == "getblockhash":
                    return "00" * 32
                if method == "getblock":
                    # first height: a v4 coinbase (old); second: the shielded v6 one
                    hexdata = "0400008085202f89" if self.n % 2 else shielded_hex
                    return {"tx": [{"hex": hexdata, "txid": "00", "authdigest": None}]}
                raise AssertionError(method)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = bc.selftest(_Mixed())
        self.assertEqual(rc, 1, "nothing verified is a failure, but both skip branches ran")

    def test_selftest_reports_a_txid_mismatch(self):
        tpl, _ = fx.load_gbt()
        built = bc.build_dynamic_coinbase(tpl, MINER, FEE, 1.0)
        tx = {"hex": built["raw"].hex(),
              "txid": "00" * 32,                                  # deliberately wrong
              "authdigest": built["auth_digest"][::-1].hex()}

        class _WrongTxid:
            def __call__(self, method, params=None):
                if method == "getblockcount":
                    return 1
                if method == "getblockhash":
                    return "00" * 32
                if method == "getblock":
                    return {"tx": [tx]}
                raise AssertionError(method)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = bc.selftest(_WrongTxid())
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: bytes=", out.getvalue())

    def test_main_prints_the_split(self):
        tpl, _ = fx.load_gbt()
        with mock.patch.object(bc, "make_rpc", lambda: fx.StubRPC(tpl, None).call), \
                mock.patch.object(bc.sys, "argv", ["build_coinbase.py", MINER, "1.0", FEE]):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = bc.main()
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("split: miner=", text)
        self.assertIn("check: outputs total=", text)
        self.assertIn("True", text)

    def test_main_falls_back_to_selftest_without_addresses(self):
        with mock.patch.object(bc, "make_rpc", lambda: self._transparent_block_stub()), \
                mock.patch.object(bc.sys, "argv", ["build_coinbase.py"]):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = bc.main()
        self.assertEqual(rc, 0)
        self.assertIn("selftest", out.getvalue())

    def test_main_selftest_flag(self):
        with mock.patch.object(bc, "make_rpc", lambda: self._transparent_block_stub()), \
                mock.patch.object(bc.sys, "argv", ["build_coinbase.py", "--selftest"]):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = bc.main()
        self.assertEqual(rc, 0)
        self.assertIn("real transparent-coinbase regression", out.getvalue())


if __name__ == "__main__":
    unittest.main()
