# Zebra Stratum Pool

**Non-custodial solo mining for Zcash, built on `zebrad` — the Rust node. No `zcashd`, no pool balance, no withdrawal step.**

[![CI](https://github.com/wygogogo19/zebra-stratum-pool/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/wygogogo19/zebra-stratum-pool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](#requirements)

A small Stratum V1 engine that

1. pulls block templates from **`zebrad`** (`getblocktemplate`),
2. assembles the coinbase transaction so the **miner is paid directly on-chain** — 99% to the miner's own
   `t`-address, 1% to the pool's infrastructure address, both inside the same coinbase (ZIP-244 *mode 2*),
3. validates Equihash (200,9) shares with vardiff, and
4. submits the solved block itself.

The pool never holds a balance, never takes custody, and there is nothing to withdraw.

## Status

This is the engine that serves **`stratum+tcp://zec.robotbase.cc:3032`** in production.

- Share accounting to date: **2,600+ accepted / 8 rejected (99.7% valid)** at share difficulty 512, with vardiff
  enabled — cumulative, restored across engine restarts, and visible live on the pool portal. Only external
  miners are counted; shares from the operator's own test rigs are reported separately (`internal_*`)
- No Zcash block has been found by this pool yet — the numbers above are share-level, and the true-custody test is the coinbase split itself
- Operating since September 2026 against `Zebra v6.3.0`

We publish it because Zcash is deprecating `zcashd` and mining infrastructure has not followed. Solo mining
on Zcash should not require trusting a third party's ledger of who is owed what.

## How it is different

| | Typical pool | This engine |
| --- | --- | --- |
| Node | `zcashd` (deprecated) | **`zebrad`** (Rust) |
| Payout | pool balance + withdrawal | **coinbase pays the miner directly** |
| Custody | pool holds funds until payout | **zero — nothing to hold** |
| Fee | 1–3% taken from a balance | **1% cut on-chain, visible in the coinbase** |
| Registration | account / email / KYC | **none — a `t`-address is the account** |

## Requirements

- Python **3.11+** — standard library only, no `pip install` step
- A synced **`zebrad`** with mining RPC reachable (cookie or user/password auth)
- A publicly reachable TCP port for miners (the engine itself can bind to `127.0.0.1` and sit behind a TCP relay)
- A Zcash `t`-address you control for the pool fee, if you enable the 1% fee

## Quickstart

```bash
git clone https://github.com/wygogogo19/zebra-stratum-pool.git
cd zebra-stratum-pool
cp config.example.json config.json     # then edit it
python3 pool.py config.json
```

Miners connect with the username format **`<your_t_address>.<worker_name>`** and any password:

```text
stratum+tcp://<your-host>:3132
username: t1YourMinerAddress................................................
password: x
```

That username *is* the payout address. A worker name that is not a valid `t1…`/`t3…` address is rejected at
`mining.authorize` — the engine will not mine for an account it cannot pay. (Prefixes listed in
`mode2.whitelist_prefixes` bypass per-miner coinbase construction; use that only for pool-internal test rigs.)

### Configuration

See [`config.example.json`](./config.example.json). The important fields:

| Field | Meaning |
| --- | --- |
| `listen_host` / `listen_port` | Where the Stratum listener binds (default `127.0.0.1:3132`) |
| `template_poll_seconds` | How often `getblocktemplate` is refreshed (`5`) |
| `default_difficulty` | Starting share difficulty before vardiff adapts (`512`) |
| `vardiff.*` | Difficulty bounds and the target shares-per-minute window (8–15/min by default) |
| `mode2.enabled` | Turn the non-custodial coinbase split on/off |
| `mode2.pool_fee_address` | Your `t1…`/`t3…` fee address (required when mode 2 is on) |
| `mode2.fee_pct` | Pool share of the block reward, cut inside the coinbase (`1.0`) |
| `zebra.url` | `zebrad` JSON-RPC endpoint, e.g. `http://127.0.0.1:8232/` |
| `status_file` | Where the live status snapshot is written (JSON, polled by dashboards) |
| `status_push_url` / `status_push_token` | Optional: POST the snapshot to an external dashboard |
| `internal_worker_prefixes` | Worker names that belong to your own test rigs (`["local.", "cpurig"]`). Matched against the whole worker string or the label after the last dot; their shares stay out of the public counters |

### Running under systemd

An example unit lives in [`deploy/zebra-stratum-pool.service`](./deploy/zebra-stratum-pool.service).

## Monitoring

The engine writes `status_file` every couple of seconds. Fields worth knowing:

| Field | Meaning |
| --- | --- |
| `workers_online` | Sessions that completed `mining.authorize` successfully — **a bare TCP connection does not count** |
| `connections_open` | Raw open sockets, including probes that never authorised (diagnostics only) |
| `shares_total` / `shares_rejected` | Cumulative counters; the engine also persists them to `counters.json` so restarts do not reset the public numbers |
| `internal_accepted` / `internal_rejected` | Shares from workers listed in `internal_worker_prefixes` — counted here instead of the public counters, so a loopback test rig never inflates or pollutes what visitors see |
| `internal_sessions` | How many connected sessions were classified as internal and therefore left out of `sessions`, `worker_list`, `hashrate` and `workers_online` |

With `internal_worker_prefixes` set, the public surface describes external miners only: `hashrate`,
`shares_1m`, `worker_list`, `sessions` and `workers_online` all skip the operator's own rigs, so a
quiet pool honestly reports **0 workers online** instead of showing a test rig to visitors.
| `hashrate` | Rolling estimate from accepted shares |
| `worker_list` | Per-worker shares, difficulty and last-share age (only workers with accepted shares appear) |
| `sessions` | Live and recently closed sessions, with `worker`, `state`, `accepted`, `rejected` |

> Counting only authorised sessions as "online miners" is deliberate: internet-exposed mining ports are
> scanned constantly, and a scanner that opens a socket should never make a dashboard claim a miner is
> present.

## Testing

CI runs on every push: every module is byte-compiled, imported and checked for the mode-2 coinbase
path, the shipped config template is parsed, and the tracked sources are verified to be English-only
(Python 3.11 / 3.12 / 3.13, standard library only, no install step).

`pool_selftest_submit.py` connects to a running engine, subscribes, authorises and submits a share — useful to
verify a relay/firewall path end to end:

```bash
python3 pool_selftest_submit.py --host 127.0.0.1 --port 3132 --worker t1YourAddress.rig1
```

## The 99/1 split, precisely

See [`docs/MODE2-99-1.md`](./docs/MODE2-99-1.md) for the transaction layout, the value-pool accounting
(including the NU6 lockbox commitment) and the failure modes we test for.

## Security notes

- The engine holds **no keys and no funds**. It cannot move coins; it can only construct a coinbase that pays
  the addresses involved.
- Keep `zebrad`'s RPC private. Prefer a cookie file over a password, and bind the engine to loopback behind a
  TCP relay or VPN if the host is internet-facing.
- Share validation is local; invalid or low-difficulty shares are rejected and counted, not silently dropped.
- If you expose the Stratum port, put a per-IP concurrent-connection limit in front of it. Scanners are
  constant; the engine treats them as unauthorised sockets, but the edge limit keeps them cheap.

## Roadmap

- [ ] Publish the integration test harness (synthetic miners, difficulty transitions, stale-job handling)
- [ ] Publish an operator runbook and container recipe
- [ ] Stabilise the status/telemetry JSON as a documented schema
- [ ] Reproducible deployment validated by an independent operator

## Maintainer

Maintained by [@wygogogo19](https://github.com/wygogogo19) as part of
[RobotBase](https://robotbase.cc/) — bare-metal PoW infrastructure, Zcash included. The live reference
deployment is <https://zec.robotbase.cc/>.

## License

[MIT](./LICENSE).
