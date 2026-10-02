# Miner firmware compatibility

Which Zcash mining firmware this pool actually works with, what "works" was measured against, and what is
still unknown. This is the compatibility-matrix half of ZCG milestone 4.

The honest summary: **one real ASIC family has been tested against the live pool** (Bitmain Antminer Z15),
plus our own CPU client. Everything else below is either derived from the published protocol or untested —
and it is labelled that way. If you run a rig this pool has not seen, see [Reporting a new
firmware](#reporting-a-new-firmware).

## The matrix

Legend — **Verified**: a real session against the production pool, with accepted shares. **Untested**: no
hardware has connected; the pool is expected to work, but nobody has proved it. **Not supported**: the pool
cannot serve this shape today, and the reason is stated.

| Firmware / miner | Algo | Status | Evidence or reason |
| --- | --- | --- | --- |
| **Bitmain Antminer Z15** (Equihash/zcash firmware) | Equihash 200,9 | **Verified** | 2,226 accepted shares in a 3-hour rented session on 2026-09-22/23; peak 419,430 Sol/s = 99.86% of the 420 kSol/s nameplate; 0 stale shares; `low_difficulty` rejects only before vardiff converged |
| **Self-hosted CPU probe** (`zec_test_miner.py`, XMRig-style client) | Equihash 200,9 | **Verified** | The long-running reference client; the two shares frozen in `tests/fixtures/` come from it |
| Bitmain Antminer Z15 **Pro** | Equihash 200,9 | Untested | Same firmware family as the Z15; expected to work, not proved |
| Bitmain Antminer Z11 / Z9 (Equihash 144,5 / 200,9 legacy) | — | Not supported | These mine different Equihash parameters; the pool verifies Equihash(200,9) only |
| MicroBT Whatsminer (ZEC models) | Equihash 200,9 | Untested | No hardware has connected |
| Innosilicon (ZEC models) | Equihash 200,9 | Untested | No hardware has connected |
| GPU miners (lolMiner, GMiner, BzMiner, TeamRedMiner with `--algo ZCASH`) | Equihash 200,9 | Untested | Expected to work — they use the same 5-parameter `mining.submit`; none has connected |
| NiceHash / hashpower marketplaces | Equihash 200,9 | Untested | Marketplace front-ends sometimes relay a non-standard submit shape; the 5- and 6-parameter shapes are both handled (see below) |
| Firmware that **requires a non-zero `extranonce2`** | — | Not supported | See [The extranonce2 constraint](#the-extranonce2-constraint) — it is a design property, not a bug |
| Firmware that requires **9-field `mining.notify`** (coinb1/coinb2/merkle branch) | — | Not supported | The pool sends the 8-field header-style notify; see [notify shape](#notify-shape-and-why-it-is-eight-fields) |
| BIP310 version-rolling (`mining.configure`) | — | Not supported | The method returns an explicit error rather than stalling. No Zcash ASIC firmware needs it |

## What the pool advertises, and what it accepts

Every row below is asserted by an executable test in
[`tests/test_firmware_matrix.py`](../tests/test_firmware_matrix.py) — drive the real engine over a socket,
submit a share the production pool once accepted, assert the outcome. The table is a description of the
tests, not a substitute for them.

| Protocol surface | Behaviour | Test |
| --- | --- | --- |
| `mining.subscribe` | `[[["mining.set_difficulty","1"],["mining.notify","1"]], extranonce1 (4 B), extranonce2_size = 0]` | `AdvertisedCapabilities` |
| `mining.set_difficulty` | Sent immediately after subscribe and again after authorize | `test_subscribe_advertises_extranonce2_size_zero` |
| `mining.notify` | 8 parameters: `job_id, version, prevhash, merkleroot, commitments, ntime, bits, clean` | `test_notify_carries_the_eight_header_fields` |
| `mining.extranonce.subscribe` | Acknowledged (`true`) | `test_extranonce_subscribe_is_acknowledged` |
| `mining.configure` | Explicit error — version-rolling is not implemented | `test_version_rolling_is_not_advertised` |

### `mining.submit` shapes the pool accepts

Four shapes, one accepted share. `pick_solution()` finds the real solution by **length** (an Equihash(200,9)
solution is 2688 hex characters), so the pool never has to guess which parameter is which.

| Shape | Who sends it | Test |
| --- | --- | --- |
| `[worker, job_id, ntime, nonce, solution]` | most Zcash firmware | `test_five_parameter_submit_is_accepted` |
| `[worker, job_id, ntime, nonce, extranonce2, solution]` | BTC-style clients that keep the field even when `extranonce2_size = 0` | `test_six_parameter_submit_is_accepted` |
| solution carrying its CompactSize prefix (`fd4005` + 1344 bytes = 2694 hex) | some firmware | `test_compact_size_prefixed_solution_is_accepted` |
| **28-byte nonce** (Antminer Z15) | Antminer firmware | `test_antminer_z15_28_byte_nonce_is_accepted` |

The nonce row is the one that mattered most in practice, and it is worth stating why: the 32-byte header
nonce is **`extranonce1 (4 bytes) ‖ the rig's 28 bytes`**. An early version of this pool padded the short
nonce with four zero bytes instead, which silently produced a different header — shares were rejected as
low-difficulty and, had a block been found, it would have been invalid. The current behaviour is pinned by
the test above and by the Equihash verifier that reverse-engineered the topology.

## The `extranonce2` constraint

The pool advertises `extranonce2_size = 0`, and this is deliberate.

Zcash block templates carry a **block commitments hash that is bound to the coinbase transaction**. This
pool builds the coinbase (miner / pool-fee / protocol lockbox outputs) and uses Zebra's coinbase verbatim;
injecting an extra nonce into it would invalidate the commitments hash and make any block the pool found
invalid. So the search space is the 32-byte nonce plus `ntime`, not `extranonce2`.

What this means for compatibility:

- A rig that **tolerates** `extranonce2_size = 0` (which includes the Z15 and every mainstream Zcash
  client) mines normally.
- A rig that **insists** on rolling its own `extranonce2` would submit shares against a coinbase the pool
  does not know about. No such firmware has been observed; the Z15 field test is what established that the
  mainstream firmware does not require it.

## Notify shape, and why it is eight fields

The pool sends the **8-field header-style** `mining.notify`, and the rig writes the fields it receives
byte-for-byte into the 140-byte header. That last part is the finding that took a reverse-engineering
effort: the Antminer firmware does not reinterpret the byte order, so the pool must send version/ntime/bits
little-endian and `prevhash` fully byte-reversed. Sending "display order" made every share — and every
future block — invalid.

The classic 9-field form (with `coinb1`/`coinb2` and a merkle branch) is not sent, because the coinbase
here is per-miner and fixed for the life of a job, so there is no `extranonce2` space for a branch to vary
over. Supporting that shape would mean recomputing the merkle root *and* the block commitments on every
submit — a substantially larger change, deliberately deferred until there is evidence any target firmware
needs it.

## Difficulty behaviour worth knowing

- New sessions start at difficulty **128** and vardiff walks them to a target of 8–15 shares/minute.
- Some firmware **ignores `mining.set_difficulty`** and keeps its own. The pool detects this (rejects the
  rig cannot explain) and adapts downward instead of fighting it; the converged value is remembered per
  worker, so a reconnect does not repeat the expensive probe. In the Z15 field test this settled in about
  two minutes (512 → 256), then produced zero rejects for the rest of the session.
- Mining-rental dashboards (MiningRigRentals, NiceHash and similar) compute their own hashrate from the
  difficulty *number* and may report a figure that disagrees with the pool's by orders of magnitude. That
  is a units mismatch in the panel, not a pool fault; the pool's figure is share-count × difficulty.

## Reporting a new firmware

If you point a rig at this pool and it does not work, or you want it added to the Verified column, the
useful report is small and precise:

```bash
# 1) what your firmware advertised back (subscribe reply, then the first notify)
#    most miners with a debug/API page will show this; otherwise tcpdump the exchange

# 2) the pool's view of your session — run against the pool on a machine you control
python3 pool_selftest_submit.py --host zecpool.robotbase.cc --port 3032 --worker t1YourAddr.rig1

# 3) the engine log lines for your worker (the operator can run this)
journalctl -u zecpool-sv1 --since "-10 min" | grep -i rig1
```

Include: the miner model, the firmware version string, whether `mining.submit` carried 5 or 6 parameters,
the nonce length your rig sends, and whether it honoured `mining.set_difficulty`. With those five facts a
new family can usually be classified in one session.

## How these claims are kept true

`tests/test_firmware_matrix.py` runs in CI on every push, on Python 3.11, 3.12 and 3.13. If a future change
stops accepting one of the four submit shapes, or starts advertising a capability this document says it
does not, the suite fails before the document can go stale.
