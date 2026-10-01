"""Validate the public telemetry contracts (ZCG milestone 4) against real, frozen payloads.

The three fixtures were captured from the live endpoints on 2026-10-01; validating them means a reviewer
can reproduce the contract check offline:
  * `telemetry_mcp_zec_chain_info.json` <- MCP tool `zec_chain_info`
  * `telemetry_zec_summary.json`        <- https://robotbase.cc/api/zec/summary
  * `telemetry_factors.json`            <- https://robotbase.cc/api/factors
"""
from __future__ import annotations

import copy
import glob
import json
import os
import unittest

from tests import _fixtures as fx
from tests import _schema as sch

ROOT = fx.ROOT
SCHEMAS = os.path.join(ROOT, "schemas")
FIXTURES = fx.FIXTURES

CASES = [
    ("zec_chain_info_v1.json", "telemetry_mcp_zec_chain_info.json"),
    ("zec_node_status_v1.json", "telemetry_node_status.json"),
    ("zec_summary_v1.json", "telemetry_zec_summary.json"),
    ("zec_value_pools_v1.json", "telemetry_factors.json"),
]


def load(name: str):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def check(payload, schema_name: str):
    with open(os.path.join(SCHEMAS, schema_name), encoding="utf-8") as fh:
        schema = json.load(fh)
    return sch.validate(payload, schema, base_dir=SCHEMAS)


class ContractFiles(unittest.TestCase):
    def test_every_schema_is_valid_json_with_a_unique_id(self):
        ids = set()
        for path in sorted(glob.glob(os.path.join(SCHEMAS, "*.json"))):
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertIn("$schema", doc, path)
            self.assertIn("$id", doc, path)
            self.assertNotIn(doc["$id"], ids, "duplicate $id in %s" % path)
            ids.add(doc["$id"])
        self.assertGreaterEqual(len(ids), 5, "expected telemetry_v1 + four endpoint schemas")

    def test_every_ref_resolves(self):
        for path in sorted(glob.glob(os.path.join(SCHEMAS, "*.json"))):
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            refs = sch.collect_refs(doc, SCHEMAS)     # raises SchemaError when a ref is broken
            for ref in refs:
                if "#" in ref and ref.split("#")[0]:
                    self.assertTrue(os.path.exists(os.path.join(SCHEMAS, ref.split("#")[0])), ref)

    def test_shared_definitions_cover_the_six_pools(self):
        with open(os.path.join(SCHEMAS, "telemetry_v1.json"), encoding="utf-8") as fh:
            shared = json.load(fh)["$defs"]
        self.assertEqual(shared["poolName"]["enum"],
                         ["transparent", "sprout", "sapling", "orchard", "ironwood", "lockbox"])
        self.assertEqual(sorted(shared["valuePools"]["required"]), sorted(shared["poolName"]["enum"]))


class RecordedPayloads(unittest.TestCase):
    """The frozen live payloads must satisfy the published contracts."""

    def test_all_fixtures_validate(self):
        for schema_name, fixture in CASES:
            with self.subTest(schema=schema_name, fixture=fixture):
                errors = check(load(fixture), schema_name)
                self.assertEqual(errors, [], "%s failed %s:\n%s" % (fixture, schema_name, "\n".join(errors)))

    def test_value_pools_are_the_same_six_everywhere(self):
        pools = load("telemetry_mcp_zec_chain_info.json")["valuePools"]
        self.assertEqual(sorted(pools), ["ironwood", "lockbox", "orchard", "sapling", "sprout", "transparent"])
        factors = load("telemetry_factors.json")["groups"]["zec"]
        for name in pools:
            self.assertIn(name, factors, "shielded-pool factor missing: %s" % name)
            self.assertIn("%s_delta" % name, factors)

    def test_supply_and_pool_totals_are_internally_consistent(self):
        chain = load("telemetry_mcp_zec_chain_info.json")
        pools = chain["valuePools"]
        shielded = sum(pools[k]["chainValue"] for k in ("sprout", "sapling", "orchard", "ironwood"))
        supply = chain["chainSupply"]["chainValue"]
        # shielded share must be a sane fraction of total supply (catches a unit mix-up, e.g. zatoshis)
        self.assertLess(shielded, supply)
        self.assertGreater(shielded / supply, 0.05)
        # the node's own numbers must agree with what the summary endpoint reports for the same chain
        summary = load("telemetry_zec_summary.json")
        self.assertLessEqual(abs(summary["network"]["height"] - chain["blocks"]), 5,
                             "height skew between the two contracts should stay tiny")


class NegativeCases(unittest.TestCase):
    """The contracts must actually reject malformed payloads."""

    def test_missing_value_pool_is_rejected(self):
        payload = load("telemetry_mcp_zec_chain_info.json")
        del payload["valuePools"]["ironwood"]
        errors = check(payload, "zec_chain_info_v1.json")
        self.assertTrue(any("ironwood" in e for e in errors), errors)

    def test_unknown_value_pool_is_rejected(self):
        payload = load("telemetry_mcp_zec_chain_info.json")
        payload["valuePools"]["newpool"] = {"chainValue": 1, "monitored": True}
        errors = check(payload, "zec_chain_info_v1.json")
        self.assertTrue(any("newpool" in e for e in errors), errors)

    def test_wrong_scalar_types_are_rejected(self):
        payload = load("telemetry_zec_summary.json")
        payload["pool"]["shares_total"] = "18092"          # string instead of integer
        payload["pool"]["valid_rate"] = 1.5                # above the documented maximum
        errors = check(payload, "zec_summary_v1.json")
        self.assertTrue(any("shares_total" in e for e in errors), errors)
        self.assertTrue(any("maximum" in e for e in errors), errors)

    def test_malformed_block_hash_is_rejected(self):
        payload = load("telemetry_mcp_zec_chain_info.json")
        payload["bestblockhash"] = "not-a-hash"
        errors = check(payload, "zec_chain_info_v1.json")
        self.assertTrue(errors and any("pattern" in e for e in errors), errors)

    def test_placeholder_and_delta_formats_are_accepted(self):
        """The display strings allow em dashes and U+2212 negatives; a broken format must be caught."""
        payload = load("telemetry_factors.json")
        payload["groups"]["zec"]["shielded_delta_24h"] = "\u2014"          # legitimately unknown
        payload["groups"]["zec"]["sapling_delta"] = "\u221269 ZEC"          # legitimately negative
        self.assertEqual(check(payload, "zec_value_pools_v1.json"), [])
        payload["groups"]["zec"]["sapling_delta"] = "minus 69 ZEC"          # not the documented format
        errors = check(payload, "zec_value_pools_v1.json")
        self.assertTrue(any("sapling_delta" in e for e in errors), errors)

    def test_node_status_contract_rejects_a_wrong_disk_row(self):
        payload = load("telemetry_node_status.json")
        payload["disk"]["df"] = ["/dev/loop7", "590G"]        # truncated df row
        errors = check(payload, "zec_node_status_v1.json")
        self.assertTrue(any("minItems" in e for e in errors), errors)

    def test_node_status_contract_rejects_unknown_chain(self):
        payload = load("telemetry_node_status.json")
        payload["chain"] = "test"                             # only mainnet is published
        errors = check(payload, "zec_node_status_v1.json")
        self.assertTrue(any("enum" in e for e in errors), errors)


class ValidatorItself(unittest.TestCase):
    """Guard the guard: the subset validator must reject what it claims to check."""

    def test_type_and_required(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
        self.assertEqual(sch.validate({"a": 1}, schema), [])
        self.assertTrue(sch.validate({}, schema))
        self.assertTrue(sch.validate({"a": "1"}, schema))
        self.assertTrue(sch.validate({"a": True}, schema), "booleans are not integers")

    def test_bounds_pattern_enum_and_const(self):
        schema = {"type": "string", "pattern": "^t3", "minLength": 3}
        self.assertEqual(sch.validate("t3abc", schema), [])
        self.assertTrue(sch.validate("t1abc", schema))
        self.assertTrue(sch.validate("t3", {"type": "string", "minLength": 3}))
        self.assertEqual(sch.validate(2, {"type": "number", "minimum": 1, "maximum": 3}), [])
        self.assertTrue(sch.validate(0, {"type": "number", "minimum": 1}))
        self.assertEqual(sch.validate("a", {"enum": ["a", "b"]}), [])
        self.assertTrue(sch.validate("c", {"enum": ["a", "b"]}))

    def test_additional_properties_and_items(self):
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False}
        self.assertEqual(sch.validate({"a": 1}, schema), [])
        self.assertTrue(sch.validate({"a": 1, "b": 2}, schema))
        self.assertEqual(sch.validate([1, 2], {"type": "array", "items": {"type": "integer"}}), [])
        self.assertTrue(sch.validate([1, "x"], {"type": "array", "items": {"type": "integer"}}))

    def test_ref_resolution_and_breakage(self):
        shared = {"$defs": {"n": {"type": "integer", "minimum": 0}}}
        inline = {"allOf": [shared["$defs"]["n"]]}
        self.assertEqual(sch.validate(5, inline), [])
        with self.assertRaises(sch.SchemaError):
            sch.collect_refs({"$ref": "does_not_exist.json#/$defs/x"}, SCHEMAS)

    def test_error_paths_point_at_the_offending_field(self):
        payload = copy.deepcopy(load("telemetry_zec_summary.json"))
        payload["network"]["connections"] = -1
        errors = check(payload, "zec_summary_v1.json")
        self.assertTrue(any("$.network.connections" in e for e in errors), errors)


@unittest.skipUnless(os.environ.get("ROBOTBASE_LIVE_TELEMETRY") == "1",
                     "set ROBOTBASE_LIVE_TELEMETRY=1 to also validate the live endpoints")
class LiveEndpoints(unittest.TestCase):
    """Optional: re-check the contracts against the running service (not part of the offline CI run)."""

    # Cloudflare rejects the default `Python-urllib/3.x` User-Agent with 403, so scripted clients must
    # identify themselves (documented in docs/TESTING.md).
    UA = {"User-Agent": "robotbase-telemetry-contract-check/1.0"}

    URLS = {
        "zec_node_status_v1.json": "https://zec.robotbase.cc/api/status",
        "zec_summary_v1.json": "https://robotbase.cc/api/zec/summary",
        "zec_value_pools_v1.json": "https://robotbase.cc/api/factors",
    }

    def test_live_payloads_validate(self):
        import urllib.request
        for schema_name, url in self.URLS.items():
            with self.subTest(schema=schema_name):
                req = urllib.request.Request(url, headers=self.UA)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read().decode())
                errors = check(payload, schema_name)
                self.assertEqual(errors, [], "%s failed %s:\n%s" % (url, schema_name, "\n".join(errors)))

    def test_live_mcp_tool_validates(self):
        """The MCP tool is a POST JSON-RPC call; the contract covers its payload."""
        import urllib.request
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "zec_chain_info", "arguments": {}}}).encode()
        req = urllib.request.Request("https://robotbase.cc/mcp", data=body,
                                     headers={"content-type": "application/json",
                                              "accept": "application/json, text/event-stream",
                                              **self.UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
        payloads = [l[len("data: "):] for l in raw.splitlines() if l.startswith("data: ")]
        result = json.loads(payloads[0] if payloads else raw)
        inner = json.loads(result["result"]["content"][0]["text"])
        errors = check(inner, "zec_chain_info_v1.json")
        self.assertEqual(errors, [], "live MCP zec_chain_info failed the contract:\n%s" % "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
