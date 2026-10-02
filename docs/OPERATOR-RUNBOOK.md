# Operator runbook

How to stand up this pool from the repository alone: a `zebrad` node, the Stratum engine, a payout
address you control, and a way to prove all three actually work before you point a real rig at them.

This is the runbook half of ZCG milestone 3. It is written to be followed by someone who has never run a
Zcash node — including the parts that are inconvenient. An independent operator running from this
document and reporting what went wrong is the point of the milestone, so corrections are welcome as
issues or pull requests.

## 1. What you are about to run, in one paragraph

The **engine holds no keys and no funds**. It asks your node for a block template, assembles a coinbase
transaction that pays the miner directly, hands the header to the miner over Stratum, validates what
comes back, and submits a solved block to your node. The 99% / 1% split happens *inside that coinbase*,
so there is no pool balance and nothing to withdraw. Nothing in this repository can move coins.

## 2. Prerequisites

| Item | Requirement | Why |
| --- | --- | --- |
| CPU / RAM | 4 cores, 8 GB RAM | zebrad verification is the load; the engine itself is nearly idle |
| Disk | **400 GB SSD or better** | mainnet chain state is ~280 GB today and grows; an HDD makes sync painfully slow |
| Network | Unmetered-ish, inbound TCP 8233 helpful | a full node peers continuously |
| Ports | **3132/tcp** public (miners), 8233/tcp (peers, optional) | 8232 (RPC) must stay private |
| Software | Docker + Compose **or** Python 3.11+ and systemd | the engine needs no third-party Python packages |
| Time | Sync is **days, not hours** | do not expect to mine the same afternoon |
| Address | A Zcash `t1…`/`t3…` address for the 1% fee | the engine refuses to build a coinbase it cannot pay |

Two things about the fee address are non-negotiable in the code, and you should decide them now:

1. it must be **a different address from any miner's payout address** — the engine rejects a miner whose
   address equals the fee address, because an on-chain "split" between the same wallet proves nothing;
2. it must be an address you control, and it will be visible in every block's coinbase.

## 3. Path A — containers (recommended)

```bash
git clone https://github.com/wygogogo19/zebra-stratum-pool.git
cd zebra-stratum-pool/deploy

mkdir -p data/zebra-state data/zebra-rpc data/pool
sudo chown -R 10001:10001 data          # both containers run as uid 10001
cp config.compose.json data/config.json

# edit data/config.json:
#   mode2.pool_fee_address -> your fee address
#   (nothing else needs to change to get started)

docker compose up -d
docker compose ps
```

What the two services are:

| Service | Image | Runs as | Publishes |
| --- | --- | --- | --- |
| `zebrad` | `zfnd/zebra:6.3.0` | uid 10001 | 8233 (peers) |
| `pool` | built from this repo's `Dockerfile` | uid 10001 | **3132** (miners) |

The RPC port (8232) is deliberately **not** published. The engine reaches it across the compose network as
`http://zebrad:8232/`, authenticated with the cookie in the shared `data/zebra-rpc` volume.

### Why the containers share a uid, and why the cookie directory is separate

`zfnd/zebra` drops privileges to uid/gid **10001** and writes its RPC cookie mode `0600`. The engine image
is therefore built to use the same uid, so both can read that file without either container running as
root. The cookie lives in its own small volume rather than in the chain-data volume so the engine never
has a view of ~280 GB of state it does not need.

One trap worth knowing, because it silently wastes an afternoon: the `zfnd/zebra` entrypoint exports
`ZEBRA_STATE__CACHE_DIR` and `ZEBRA_RPC__COOKIE_DIR`, and **zebrad's configuration layer gives environment
variables precedence over the TOML file**. Setting `cookie_dir` in a TOML file therefore has no effect if
those variables are present. `compose.yaml` sets them explicitly instead of fighting them, which is why
there is no `zebrad.toml` in this recipe at all.

## 4. Path B — bare metal with systemd

The engine is standard-library-only Python, so this path needs no virtualenv, no pip and no image.

```bash
sudo useradd --system --home /var/lib/zebra-stratum-pool --shell /usr/sbin/nologin zecpool
sudo install -d -o zecpool -g zecpool /var/lib/zebra-stratum-pool /etc/zebra-stratum-pool

sudo git clone https://github.com/wygogogo19/zebra-stratum-pool.git /opt/zebra-stratum-pool
sudo cp /opt/zebra-stratum-pool/config.example.json /etc/zebra-stratum-pool/config.json
sudo chown zecpool /etc/zebra-stratum-pool/config.json
# edit /etc/zebra-stratum-pool/config.json: pool_fee_address, status_file, zebra.url, zebra.cookie_file

sudo cp /opt/zebra-stratum-pool/deploy/zebra-stratum-pool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zebra-stratum-pool
journalctl -u zebra-stratum-pool -f
```

The shipped unit already sets `Environment=ZECPOOL_CONFIG=/etc/zebra-stratum-pool/config.json`, which is
what matters here: **the engine reads its config path from the `ZECPOOL_CONFIG` environment variable and
ignores a path passed on the command line.** Running `python3 pool.py /some/config.json` therefore does
not do what it looks like it does — it falls back to the built-in default
(`/etc/zecpool/config.json`) and exits if that file is absent. Always drive it through the variable:

```bash
ZECPOOL_CONFIG=/etc/zebra-stratum-pool/config.json python3 /opt/zebra-stratum-pool/pool.py
```

`config.example.json` documents every field; the two that are easy to miss are
`heartbeat_seconds` (re-broadcast the job after N seconds of template silence, so idle sessions survive a
slow RPC moment) and `verify_solution_below_difficulty` (set `1.0` to run the full Equihash verifier on
every share — correct for a solo pool, where a bad share is a wasted block).

You need a `zebrad` reachable at `zebra.url`. If it is on the same host, bind the RPC to loopback and
point `cookie_file` at the node's `.cookie` file; the engine re-reads that file on **every** RPC call, so a
node restart that rotates the cookie does not require restarting the pool.

## 5. Verify before you trust it

Do these in order. The first two only prove the plumbing; the third is the one that matters.

### 5.1 The node is synced and serving templates

```bash
docker compose logs zebrad | tail -20          # look for sync progress / "synced"
cat deploy/data/pool/status.json | python3 -m json.tool | head -30
```

`status.json` is rewritten every couple of seconds. You are looking for `"rpc_ok": true` and a
`blocks`/`template_height` that tracks the chain tip.

### 5.2 The engine really submits a share

This is a live protocol probe that does the full handshake — subscribe, authorize, wait for the job,
build the 140-byte header byte-for-byte, solve for the assigned difficulty and submit:

```bash
# whitelisted worker: mines to the node-level coinbase
python3 pool_selftest_submit.py <your-host> 3132 local.selftest

# mode-2 worker: exercises the per-miner coinbase carrying the 99/1 split
python3 pool_selftest_submit.py <your-host> 3132 t1YourAddress....rig1
```

A run that ends with an accepted share means subscribe/authorize/notify/submit and the share validator
all work end to end. If it ends in `low_difficulty`, your difficulty settings — not your plumbing — are
the problem.

### 5.3 The coinbase builder agrees with your own node

This is the check most worth running, because it is the one that catches a broken block header before a
block is ever found. It pulls real blocks from **your** node and re-derives their transaction ids and
authorization digests with this repository's own serializer:

```bash
python3 build_coinbase.py --selftest
# [selftest] real transparent-coinbase regression: bytes+txid N/N, auth digest N/N
```

`N/N` with no `FAILED` lines means your node and this engine agree byte-for-byte. Anything less means the
node is a different version than this engine was written against — stop and open an issue rather than
mining into it.

### 5.4 The payout separation holds

Try to authorize as the fee address itself. The engine must refuse:

```bash
python3 pool_selftest_submit.py <your-host> 3132 t1YourFeeAddress.rig1
# expected: authorization refused — "mining address must differ from pool fee address"
```

A refusal here is the property working. If it accepts, you are running a modified engine.

### 5.5 A real rig produces accepted shares

Point a miner at the pool with `<your t-address>.<rigname>` as the username. Watch
`worker_list` / `shares_total` in `status.json` and the engine log. On a slow CPU rig, allow a few minutes:
vardiff starts at difficulty 128 and walks down toward the 8–15 shares/minute window, and a very slow rig
takes several steps to get there.

## 6. Operating it

| Task | Command |
| --- | --- |
| Follow engine logs | `docker compose logs -f pool` |
| Follow node logs | `docker compose logs -f zebrad` |
| Live share counters | `python3 -m json.tool deploy/data/pool/status.json` |
| Restart the engine | `docker compose restart pool` (share counters are restored from `counters.json`) |
| Stop everything | `docker compose down` |
| Upgrade the engine | `git pull && docker compose build pool && docker compose up -d pool` |
| Back up what matters | `deploy/data/pool/counters.json` and `deploy/data/config.json` |

Back up **`counters.json`**: it is what keeps the public share totals from resetting to zero across a
restart. Everything else in `data/pool` is regenerable. The chain state in `data/zebra-state` does not
need a backup — it re-syncs from the network.

Upgrading `zebrad` is a deliberate act: change the image tag, then re-run the check in 5.3 afterwards.
The engine tolerates a node restart on its own (the cookie is re-read per call, and jobs resume when a
template is available), so a rolling upgrade does not need to stop the pool.

## 7. Troubleshooting

| Symptom | Likely cause | What to check |
| --- | --- | --- |
| Engine logs `node unreachable` | node not up, RPC bound elsewhere, or cookie mismatch | `docker compose logs zebrad`; confirm `zebra.url` uses the service name; confirm the cookie file exists in `data/zebra-rpc` |
| Miner connects, then is disconnected immediately | worker name is not a valid `t1…`/`t3…` address | the log line says `authorize rejected (Invalid ZEC address)` — the username *is* the payout address |
| `Mining address must differ from pool fee address` | miner is using the fee address | use a different address for mining |
| Connected but zero shares for minutes | vardiff still converging, or firmware ignoring `mining.set_difficulty` | wait; check `sessions`/`difficulty` in `status.json`; the engine adapts downward and remembers the value per worker |
| Shares rejected as `low_difficulty` | miner's own difficulty is above what it is actually solving at | check the miner's configured difficulty against the pool's `default_difficulty` |
| `stale job` rejects | miner is slow to submit across a slow link | normal in small numbers; a burst means latency between the miner and 3132 |
| Everything works, no block for weeks | this is solo mining | one ASIC on Zcash is a multi-week expected time to a block; that is not a fault |
| `docker compose up` fails with permission errors on `data/` | the host directories are not owned by uid 10001 | `sudo chown -R 10001:10001 deploy/data` |

## 8. Security notes

- The engine holds no keys and no funds; the worst case of a compromise is a wrong coinbase, not stolen
  coins. Keep the RPC port private anyway.
- Do not publish 8232. In the compose recipe it is never published; on bare metal bind it to loopback.
- Put a per-IP concurrent-connection limit in front of 3132. Internet-exposed mining ports are scanned
  constantly; the engine treats scanners as unauthorized sockets, but an edge limit keeps that cheap.
- The fee address is public by construction. Use an address you are comfortable having in every block.

## 9. What to report back

Milestone 3 closes when one independent operator has run this from the repository. The useful report is
short and specific:

1. which path you took (compose or systemd), and your host OS / Docker version;
2. how long the initial sync took, and the disk it needed — the runbook's estimate is the thing most
   likely to be wrong;
3. the output of the three checks in §5.2–§5.4 (accepted share, `N/N` selftest, refusal);
4. anything in this document that was unclear, missing or wrong — that is the actual deliverable of the
   milestone, and it is more valuable than a report that everything worked.

Open an issue on the repository, or reply in the grant thread.

## 10. Known rough edges

Recorded honestly, because a runbook that only describes the happy path is not much use:

- **The engine ignores a command-line config path.** It reads `ZECPOOL_CONFIG` only (§4). This is
  deliberately left as-is for now: the published `pool.py` is kept token-identical to the engine running
  in production, and any source change breaks that property until the same change ships to both. Accepting
  an optional argv path is a small, safe change and is queued for the next engine release, at which point
  the identity is re-established rather than quietly broken.
- **Containers share a uid, so you must own the host directories.** `data/` has to be `chown`ed to uid
  10001 before the first `docker compose up`, or the node cannot write its cookie. Compose cannot do this
  for you without running as root.
- **Nothing here has been run by a second person yet.** That is milestone 3's remaining work: the parts
  most likely to be wrong are the sync-time and disk estimates in §2, and the verification steps in §5.
- **The pool has not found a block**, on this or the reference deployment. Everything in §5 verifies the
  path up to a solved block; the final proof is a coinbase on chain, and it has not happened yet for
  anyone.
