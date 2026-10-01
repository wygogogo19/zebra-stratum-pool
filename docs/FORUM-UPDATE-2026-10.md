# Milestone update — October 2026 (draft for the forum thread)

This is a ready-to-post update for the grant thread
[*Grant Application - Zebra-Native Non-Custodial Mining Stack (RobotBase)*](https://forum.zcashcommunity.com/t/grant-application-zebra-native-non-custodial-mining-stack-robotbase/57749)
and for milestone reporting. It is written to be checked, not taken on trust: every number below is
reproducible from the public repository or the live endpoints.

---

## Post body

**Milestone 2 is complete, ahead of schedule — and it caught two real bugs.**

Quick status against the plan, then the parts worth reading.

| Milestone | Plan | Status |
| --- | --- | --- |
| M1 — public repo, CI, reproducible dev env | Month 1 | Done (published before the grant) |
| M2 — test suite + synthetic-miner harness + public review | Months 2–3 | **Complete** |
| M3 — operator runbook + one independent operator | Months 3–4 | Starting now — operator welcome |
| M4 — six-pool telemetry spec + firmware matrix | Months 4–5 | In progress — spec landed, matrix next |

### The test suite, and what it is not

The repository now carries **107 offline tests** that run with nothing but a CPython interpreter — no
dependency install, no node, no solver, no network:

```
python3 tests/_run.py     # 107 tests, OK in ~1.3 s
```

What matters is *what* they run against. The fixtures are not invented: a verbatim
`getblocktemplate`/`getblocksubsidy` pair from our production `zebrad`, and two shares the live pool
actually accepted, with their 140-byte headers and 1344-byte Equihash(200,9) solutions. So the accept
path is exercised with a genuine proof of work, and a change in how the engine handles a *real* mainnet
template fails the suite.

Coverage is concentrated where it counts — the consensus-critical arithmetic:

| Module | Coverage |
| --- | ---: |
| `build_coinbase.py` (the 99/1/Lockbox split) | **98.1%** |
| `zcash_v6.py` (transaction digests) | **92.9%** |
| overall | 50.8% |

CI runs on every push across **Python 3.11 / 3.12 / 3.13**, with a coverage floor so the number cannot
slide silently. `tests/synthetic_miner.py` is the harness behind the end-to-end tests: a hashrate-free
miner that speaks Stratum over a real socket to the real engine and replays a pre-computed, genuinely
valid Equihash solution.

### The two bugs the suite found

This is the honest part, and the reason we built the suite early. Writing the coinbase tests surfaced two
defects in how the block header was assembled. Both were **silent**: shares stayed self-consistent, so
nothing looked wrong until the tests compared our output against the node's own.

1. **Coinbase txid.** We were computing the coinbase transaction id as `dsha256(raw)`. A Zcash v5/v6 txid
   is a ZIP-244 digest, and a shielded coinbase cannot be re-derived from its raw bytes at all. The job
   now uses the txid the node itself publishes (`coinbasetxn.hash`).
2. **Merkle root.** We were walking the template's *transaction list* as if it were a merkle *branch*. It
   is not: with two or more transactions, the coinbase's sibling is a subtree hash. The root is now built
   as a tree.

Consequence before the fix: a solved block would have been rejected by the network
(`bad-txnmrklroot`). After the fix, the engine reproduces the node's own `defaultroots.merkleroot`
**byte-for-byte** over a live template — that comparison is now a test
(`tests/test_job_contract.py`), not a one-off check. Both fixes are deployed to both production engines.

We are reporting this rather than quietly patching it because "the test suite found real bugs and we
fixed them" is the point of the milestone, and because a reviewer can re-run the same comparison on any
future template.

### Live production numbers

As of 2026-10-01:

- **18,000+ shares accepted, 21 rejected (99.88% valid)**, cumulative across engine restarts.
- **Zebra 6.3.0, zero restarts since 2026-09-14** — the node has been up continuously through the whole
  test-and-fix cycle, and its chain view is 100% synced at the live tip.
- The engine holds **no keys and no funds**; the 99% / 1% split is inside the coinbase, unchanged by any
  of the above.

Honest caveat, unchanged from the application: **the pool has not found a mainnet block**, and the shares
above are still the pool's own testing rigs, not third-party miners. We are not presenting them as
adoption.

### Repository and production are the same code

The published `pool.py` and the engine running in production (both the front-end and the fallback) are
**token-identical** after normalising comments and string literals — 8,437 identical tokens. This is a
claim a reader can verify, not a slogan; the diff is comments and localised strings, never logic.

### Milestone 4 is underway

The six-value-pool telemetry spec has landed as formal JSON Schema contracts in
[`schemas/`](https://github.com/wygogogo19/zebra-stratum-pool/tree/main/schemas) — one contract per public
surface (`/api/zec/summary`, `/api/factors`, the node status page, and the MCP `zec_chain_info` tool),
plus a shared definition library and a documented versioning policy. Each contract is checked against a
frozen real payload in CI, and you can validate a live fetch yourself with the shipped, dependency-free
validator. The miner-firmware compatibility matrix is the next piece.

### Want to be the independent operator (M3)?

Milestone 3 needs one independent operator to stand up a pool from the repository alone and tell us where
the runbook is wrong. If that is you — or you know a miner who wants to help pressure-test a
Zebra-native pool — reply here or open an issue. That is the milestone the grant is really paying for,
and it is the one that needs someone other than us.

---

## Reviewers' checklist (not for posting)

- Reproduce the suite: `python3 tests/_run.py` -> `107 tests, OK`.
- Reproduce coverage: `python3 tests/_coverage.py` -> the table above.
- Reproduce the merkle fix: `python3 -m pytest tests/test_job_contract.py -v` (the assertion compares our
  root with the node's `defaultroots.merkleroot`).
- Verify the token-identity claim: strip comments/strings from `pool.py` and diff against the deployed
  file; only comments and localised strings differ.
- Verify a live contract: `curl -s -A 'RobotBase/1.0' https://robotbase.cc/api/zec/summary` then
  `python3 tests/_schema.py schemas/zec_summary_v1.json <file>`.
- Live counters are on the portal (<https://zec.robotbase.cc/>) and in `/api/zec/summary`.

## Posting notes

- Thread: <https://forum.zcashcommunity.com/t/57749>
- Tone target: factual, data-first, no superlatives — the thread already has a critical reader
  (`@strahncryptography`) who challenged an earlier over-claim, and the update answers with numbers.
- Numbers to refresh before posting if the post slips past early October: share totals, node uptime, and
  the live tip height. Everything else is static.
