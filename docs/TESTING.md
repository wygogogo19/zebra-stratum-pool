# Testing

This document records what the test suite covers, how to reproduce it, and where its limits are. The
suite is the deliverable of ZCG milestone 2 and the evidence behind the two consensus bugs it found
(see [`CHANGELOG.md`](../CHANGELOG.md)); the telemetry contracts of milestone 4 are tested alongside it.

## Running the suite

The engine itself is standard-library-only. The tests are too, by default — they run offline against
frozen mainnet fixtures, so no node, no solver and no network access are required.

```bash
python3 tests/_run.py          # plain `unittest` run (no pip needed)
python3 tests/_coverage.py     # the same suite plus a statement-coverage table (no pip needed)

python3 -m pip install -r requirements-dev.txt   # to use pytest + pytest-cov + the CI gate
python3 -m pytest                                 # the suite with the coverage gate from pytest.ini
python3 -m pytest tests/test_build_coinbase.py -v # just the 99/1 coinbase split
```

Current result on this repository: **107 tests, OK in ~1.3 s** (2 of them are the live-endpoint check,
skipped unless you opt in — see below).

## What each file pins down

| File | Tests | Covers |
| --- | ---: | --- |
| `test_build_coinbase.py` | 31 | subsidy/lockbox arithmetic, output order, the deterministic ZIP-244 txid and auth digest, address handling, the offline `--selftest`, refusal to build without a lockbox output |
| `test_pool_core.py` | 22 | difficulty ⇄ target conversion, vardiff bounds, merkle-tree construction (odd/one-leaf levels), coinbase decoding, submit-shape picking, internal-worker classification |
| `test_telemetry_schemas.py` | 20 | the milestone-4 JSON Schema contracts: four frozen payloads validate; the validator itself is exercised against broken schemas and payloads |
| `test_job_contract.py` | 13 | the 8-parameter `mining.notify` byte order, the `extranonce1 ‖ 28-byte nonce` topology, the merkle root committing to the real block |
| `test_synthetic_miner.py` | 8 | a hashrate-free miner drives the **real** engine over TCP and replays a recorded, genuinely valid Equihash solution; stale-job rejection; the mode-2 workflow |
| `test_mode2_guards.py` | 7 | payout-address validation and the "miner address must differ from the pool fee address" red line |
| `test_equihash_verify.py` | 6 | the verifier accepts recorded real solutions and rejects tampered ones |

Two helper modules are not tests but are used by them: `tests/_schema.py` (dependency-free JSON Schema
2020-12 subset) and `tests/synthetic_miner.py` (the Stratum harness).

## Coverage

`python3 tests/_coverage.py` prints the same statement metric `coverage.py` reports, without needing pip
(the stdlib `trace` module under-reports branches, so it is not used):

| Module | Statements | Hit | Coverage |
| --- | ---: | ---: | ---: |
| `build_coinbase.py` | 161 | 158 | **98.1%** |
| `zcash_v6.py` | 170 | 158 | **92.9%** |
| `pool.py` | 973 | 407 | 41.8% |
| `equihash_verify.py` | 275 | 79 | 28.7% |
| **Total** | 1579 | 802 | **50.8%** |

CI enforces a **40%** floor (`--cov-fail-under=40` in `pytest.ini`) so the number cannot silently slide.

### Reading the numbers honestly

- The two modules that carry **consensus-critical arithmetic** — the coinbase split and the transaction
  digests — are the two that are covered hardest (98.1% / 92.9%). That is where a bug silently produces an
  invalid block, so that is where the tests are.
- `pool.py` is a long-lived socket server. Most of its statements are the network loop, logging and
  shutdown paths; the parts that decide whether a share is valid, and what the coinbase pays, are covered
  by the fixture-driven and TCP-driven tests. Raising the *percentage* by executing socket plumbing would
  not raise confidence.
- `equihash_verify.py` is only lightly covered because the suite exercises the **verifier's accept path**
  with real solutions; the solver/serialisation helpers it also carries are not on the pool's hot path.
  Its behaviour is pinned by the recorded-solution tests, not by statement count.
- The pool has **not** yet found a mainnet block, so there is no end-to-end "our block confirmed on the
  network" test. The closest substitute is byte-for-byte agreement with the node's own
  `defaultroots.merkleroot` over a real template, which is asserted in `test_job_contract.py`.

## Fixtures and their provenance

All inputs live in `tests/fixtures/` and were captured from the production deployment on **2026-10-01**
(the telemetry payloads) and **2026-09-25** (the block template and the two accepted shares):

| Fixture | What it is |
| --- | --- |
| `gbt_mainnet_3502592.json` | a verbatim `getblocktemplate` + `getblocksubsidy` pair from our `zebrad` |
| `real_solutions.json` | two shares the production pool **accepted**, with their 140-byte headers and 1344-byte Equihash(200,9) solutions |
| `telemetry_mcp_zec_chain_info.json` | output of the MCP tool `zec_chain_info` |
| `telemetry_zec_summary.json` | `https://robotbase.cc/api/zec/summary` |
| `telemetry_factors.json` | `https://robotbase.cc/api/factors` |
| `telemetry_node_status.json` | `https://zec.robotbase.cc/api/status` |

Validating against real captured data — rather than synthetic round numbers — is deliberate: it makes the
tests fail if the engine's handling of a *real* mainnet template or a *real* accepted share changes.

## Optional: validate the live endpoints

The four telemetry contracts can be checked against the endpoints as they are right now:

```bash
ROBOTBASE_LIVE_TELEMETRY=1 python3 -m unittest tests.test_telemetry_schemas.LiveEndpoints
```

**Cloudflare rejects the default Python `urllib` User-Agent with a 403.** Any live fetch — script or
`curl` — must send a real `User-Agent`, or it will look like the endpoint is broken when it is not:

```bash
curl -s -A 'RobotBase-schema-check/1.0 (+https://robotbase.cc)' \
  https://robotbase.cc/api/zec/summary | head -c 400
```

## CI gates

Every push and pull request runs two jobs across **Python 3.11, 3.12 and 3.13**
(`.github/workflows/ci.yml`):

1. **pytest + coverage** — the 107 tests with the 40% floor, publishing a coverage table in the job summary.
2. **Syntax + import self-check** — byte-compiles every module, imports the shipped modules with the
   standard library only (a third-party import fails on purpose), asserts the mode-2 coinbase path loaded,
   parses `config.example.json`, checks share-counter persistence, verifies internal test rigs stay out of
   the public counters, and confirms tracked text files are English-only.

## Not covered yet

- A confirmed mainnet block, for the reason above.
- Long-run soak behaviour (multi-hour difficulty transitions, relay fail-over timing). The synthetic miner
  can drive a session, but a soak harness is not built.
- The dashboard/reporting container and the Cloudflare edge, which sit outside this repository.
