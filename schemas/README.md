# RobotBase Zcash telemetry contracts (v1)

These files are the **formal contracts** for the Zcash telemetry this project publishes. They are JSON
Schema (draft 2020-12) documents, one per public surface, plus a shared definition library that the
endpoint schemas `$ref` into.

The point is not documentation. A reviewer, an integrator or an agent can fetch a payload and check it
against one of these files instead of trusting a prose description — and CI re-checks it on every push.

## Endpoint → schema

| Surface | Where | Schema |
| --- | --- | --- |
| MCP tool `zec_chain_info` | `POST https://robotbase.cc/mcp` | [`zec_chain_info_v1.json`](./zec_chain_info_v1.json) |
| Node status | `https://zec.robotbase.cc/api/status` | [`zec_node_status_v1.json`](./zec_node_status_v1.json) |
| Pool + network summary | `https://robotbase.cc/api/zec/summary` | [`zec_summary_v1.json`](./zec_summary_v1.json) |
| Shielded-pool factors | `https://robotbase.cc/api/factors` | [`zec_value_pools_v1.json`](./zec_value_pools_v1.json) |
| Shared building blocks | — | [`telemetry_v1.json`](./telemetry_v1.json) |

`telemetry_v1.json` is a definition library, not a payload schema: it has no top-level `type` and exists to
be `$ref`-ed. Four endpoint schemas depend on it, so a change there is a change to every contract.

## The six value pools

`/api/factors` and `zec_chain_info` both describe the same six NU6.3 value pools — `transparent`, `sprout`,
`sapling`, `orchard`, `ironwood`, `lockbox`. The `valuePools` definition marks all six as `required` and
sets `additionalProperties: false`, so a payload that silently drops or renames a pool fails validation.
That guardrail is deliberate: a value-pool set that drifts is a data-integrity bug, not a formatting
detail.

## Versioning policy

The files are named `..._v1.json` because the following are **breaking changes** and require a new `v2`
document (the `v1` file stays published):

1. removing a field, or making an optional field required;
2. changing a field's type (e.g. a ZEC amount from `number` to `string`);
3. changing the set of value pools (adding a protocol pool, or renaming one);
4. tightening a constraint in a way existing payloads could violate.

Additive, backward-compatible changes (a new optional field, a relaxed bound) can ship inside `v1` — but
an endpoint schema uses `"additionalProperties": false`, so *adding a field to the payload* is itself a
contract change: add it to the schema in the same commit.

## Validate a payload yourself

The repository ships a dependency-free validator (`tests/_schema.py`, a small JSON Schema 2020-12 subset)
so this works on a bare CPython with no `pip install`:

```bash
# against a frozen fixture captured from the live endpoint
python3 tests/_schema.py schemas/zec_summary_v1.json tests/fixtures/telemetry_zec_summary.json

# against a live fetch (note: Cloudflare rejects the default urllib User-Agent — send a real one)
curl -s -A 'RobotBase-schema-check/1.0 (+https://robotbase.cc)' \
  https://robotbase.cc/api/zec/summary -o /tmp/summary.json
python3 tests/_schema.py schemas/zec_summary_v1.json /tmp/summary.json
```

Both commands print `OK: … conforms to …` and exit `0` on success, or list every violation and exit `1`.

The full contract test lives in [`tests/test_telemetry_schemas.py`](../tests/test_telemetry_schemas.py):
it validates the four frozen fixtures, exercises the validator against deliberately broken schemas and
payloads, and — when `ROBOTBASE_LIVE_TELEMETRY=1` is set — checks the live endpoints too. A validator that
accepted everything could not pass that suite.

## Why these shapes look the way they do

- **Two different node views.** `zec_chain_info` (pool-facing, six value pools) and `zec_node_status`
  (operator-facing, RPC/peer/disk health) describe the same node but are not the same payload; they are
  separate schemas on purpose.
- **Display strings are part of the contract.** `/api/factors` renders amounts, percentages and deltas as
  they appear on the site (including the em-dash placeholder and U+2212 for negatives). Those are pinned by
  `pattern`, so a formatting regression fails the contract instead of shipping silently.
- **Units are explicit.** Zatoshis (`chainValueZat`, `miner_reward`, `lockbox_value`) are integers; ZEC
  amounts are decimal `number`s. The two are never mixed in one field.
