# Changelog

## Unreleased — 2026-10-01

Test suite (ZCG milestone 2) and its two correctness fixes, plus the telemetry contracts (ZCG milestone 4).

### Fixed

- **Merkle root of node-coinbase ("mode 1") jobs.** Two defects, both silent because shares stayed
  self-consistent while the *header* was wrong:
  1. the coinbase txid was taken as `dsha256(raw)`. A Zcash v5/v6 txid is a ZIP-244 digest, and a shielded
     coinbase cannot be re-derived from its raw bytes at all — the job now uses the txid the node already
     publishes as `coinbasetxn.hash` (falling back to `txid_v6()` for transparent-only coinbases);
  2. the block merkle root was computed by walking the template's **transaction list** as if it were a
     merkle branch. It is a tx list: with two or more transactions the coinbase's sibling is a subtree
     hash. The root is now built as a tree (and `merkle_branch()` derives the coinbase's sibling path).
     Consequence before the fix: a solved block would have been rejected by the network
     (`bad-txnmrklroot`). Verified against the live node: the fixed engine reproduces
     `defaultroots.merkleroot` byte-for-byte; the previous algorithm did not.

### Added

- `tests/` — 107 tests that run offline against **real mainnet data** (a frozen `getblocktemplate` and two
  shares the production pool accepted, including their 1344-byte Equihash solutions):
  * `test_build_coinbase.py` — the 99/1/Lockbox split, address handling, digest regression, offline `--selftest`;
  * `test_pool_core.py` — difficulty⇄target conversion, vardiff bounds, merkle tree, coinbase decoding,
    submit-shape picking, internal-worker classification (the `zcash_v6` digests are exercised from
    `test_build_coinbase.py`, `test_job_contract.py` and this file);
  * `test_job_contract.py` — the 8-parameter `mining.notify` byte-order contract;
  * `test_mode2_guards.py` — payout-address validation and the "miner address must differ from the pool fee
    address" red line, including the miner-specific coinbase layout;
  * `test_equihash_verify.py` — the verifier accepts recorded real solutions and rejects tampered ones;
  * `test_synthetic_miner.py` + `synthetic_miner.py` — a hashrate-free synthetic miner that drives the real
    engine over TCP (subscribe/authorize/notify/submit) and submits a *pre-computed, genuinely valid*
    Equihash solution; also covers stale-job rejection and the mode-2 workflow;
  * `test_telemetry_schemas.py` — the M4 telemetry contracts (below) validate four frozen live payloads,
    and the validator itself is tested against deliberately broken schemas and payloads.
- `schemas/` — **telemetry contracts, v1** (ZCG milestone 4). Formal JSON Schema (draft 2020-12) for every
  public Zcash telemetry surface, with a documented versioning policy (see `schemas/README.md`):
  * `telemetry_v1.json` — shared definitions: the six value pools (`required`, `additionalProperties:false`),
    chain supply, pool/network/price/template telemetry, and the display-string formats;
  * `zec_chain_info_v1.json` — the MCP tool `zec_chain_info`;
  * `zec_summary_v1.json` — `https://robotbase.cc/api/zec/summary`;
  * `zec_value_pools_v1.json` — `https://robotbase.cc/api/factors`;
  * `zec_node_status_v1.json` — `https://zec.robotbase.cc/api/status`.
- `tests/_schema.py` — a dependency-free JSON Schema 2020-12 subset validator (with a
  `python3 tests/_schema.py <schema> <payload>` CLI) so the contracts can be checked without `pip`.
- `docs/TESTING.md` — the test inventory, the coverage table, fixture provenance, the CI gates and the
  honest limits; `docs/FORUM-UPDATE-2026-10.md` — the milestone-2 report draft for the grant thread.
- `equihash_verify.py` — the Equihash(200,9) verifier the engine optionally imports (English variant,
  token-identical to the deployed engine).
- `pytest.ini` / `requirements-dev.txt` — test configuration with a coverage gate; CI now runs the suite on
  Python 3.11/3.12/3.13 and publishes a coverage table in the job summary.
- `tests/_coverage.py` — offline statement-coverage harness for machines without pip (reports the same
  metric as `coverage.py`; stdlib `trace` under-reports branches).

## v0.2.1 — 2026-09-28

Session-liveness hardening, mirroring the engine that is running in production.

### Added

- **Silent-session reaper.** `_reap_stale_clients(limit=300.0)` closes and removes any client that has
  sent nothing for five minutes, and it runs from the status loop roughly every ten seconds. Every
  inbound message (subscribe, authorize, share, response) refreshes `Client.last_activity`, so a miner
  that is really working — it submits every few seconds — can never be reaped. Reaped sessions are
  recorded with `state="reaped"` and logged as
  `reaped silent session worker=… (inbound silent Ns, half-open connection)`.
- **Tighter TCP keepalive on client sockets.** `TCP_KEEPIDLE=60`, `TCP_KEEPINTVL=15`, `TCP_KEEPCNT=4`
  replace the OS default (two hours idle), so a half-open connection is detected by the kernel in about
  two minutes instead of two hours. Each option is applied only when the platform exposes it.

### Why

Measured on the production deployment: a cross-border link can fail in one direction, leaving the
client socket half-open — the miner has already reconnected while the pool still counts the old
connection as online. With the previous defaults a zombie session lingered for up to two hours and
inflated `workers_online` and the public `sessions` list. With this release the online miner count stays
exact; shares, counters, settlement and the coinbase path are untouched.

### Verified

- Synthetic half-open session against both production entry points: reaped after **310 s** (front
  engine) and **306 s** (fallback engine), with the corresponding reaper log line.
- Kernel accepted and reported back `KEEPIDLE=60 KEEPINTVL=15 KEEPCNT=4 KEEPALIVE=1`.
- A live miner session stayed connected across the whole observation window (`connected_seconds`
  monotonically increasing, inbound silence always under 50 s) — no false positives.
- Token-level equivalence with the deployed engine: 8259 tokens on both sides, identical token streams
  apart from comments and string literals.

### Unchanged

- Share validation, Equihash (200,9) checking, vardiff, the 8-parameter `mining.notify` contract,
  `submitblock`, counter persistence and the fee-address isolation guard.

## v0.2.0 — 2026-09-27

Engine parity with the live production deployment, plus input hardening.

### Added

- **Fee-address isolation guard.** A miner whose payout address equals `mode2.pool_fee_address` is rejected
  (`"Mining address must differ from pool fee address"`). This makes the 99/1 split structurally verifiable:
  the 1% fee can never land in the same wallet as the miner's 99%, so the on-chain split cannot be an artefact
  of the operator's own address layout.
- **Built-in internal-rig filter.** `INTERNAL_WORKER_LABELS = ("local.", "u1")` keeps the operator's own probes
  out of the public counters, worker list, rolling hashrate and session view — even if a deployment script
  rewrites `config.json` without those prefixes. Internal traffic is reported separately as
  `internal_accepted` / `internal_rejected` / `internal_sessions`.
- **Coinbase accounting.** `coinbase_output_sum()` decodes the transparent outputs of the `coinbasetxn`, and
  `getblocksubsidy` supplies the miner/funding breakdown, so the job log reports the real split
  (`new job height=... tx=... subsidy=1.56250000 ZEC (miner 1.25000000 - transparent 0.12500000)`). The previous
  `coinbase=0.00000000 ZEC` line was misleading: Zebra's `getblocktemplate` does not return `coinbasevalue`, and
  the miner's share is usually a shielded output that cannot be read off-chain.

### Fixed

- Non-UTF-8 bytes from a public scanner raised an unhandled `UnicodeDecodeError` inside the connection handler;
  over-long lines without a newline could raise `LimitOverrunError`. Both are now handled per connection, so a
  malformed client can no longer produce tracebacks in the engine log.
- Sessions that were already closed by internal probe connections leaked into the public `sessions` list through
  the short-lived debug buffer (`recent_sessions`); they are now filtered like live sessions.

### Notes

- Share validation, Equihash (200,9) checking, vardiff, the 8-parameter `mining.notify` contract and the
  `submitblock` path are unchanged.
- This tree is the English-language publication of the engine that runs in production; the deployed copy carries
  localized (Chinese) log output and comments. Both files are token-identical apart from comments and string
  literals (verified by comparing their Python token streams).
- No deployment specifics are included: addresses, hostnames and credentials remain placeholders.

## v0.1.0 — 2026-09-21

- Initial public release: Zebra-native, non-custodial Stratum V1 pool engine with ZIP-244 mode-2 coinbase
  construction (99% miner / 1% pool on-chain), vardiff, share accounting and a protocol self-test probe.
