# Changelog

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
