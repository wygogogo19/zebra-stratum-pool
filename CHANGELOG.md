# Changelog

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
