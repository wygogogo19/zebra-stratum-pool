# Contributing

Thanks for looking at the Zebra Stratum Pool. This project exists so that solo mining on Zcash does not
depend on `zcashd` or on trusting a pool operator's ledger — contributions that move it in that direction are
welcome.

## Ground rules

- **Keep it dependency-free.** The engine runs on the Python standard library. A pull request that adds a
  runtime dependency needs a written justification in the PR description (what it buys, what it costs to
  audit) and should expect pushback.
- **No custody, ever.** Code that accumulates a balance, defers payouts, or requires a registration step will
  not be merged. The coinbase is the payout.
- **Nothing host-specific.** No IP addresses, hostnames, credentials, wallet addresses or internal
  identifiers. Everything configurable lives in `config.json`; the example file carries placeholders only.
- **Security first.** Anything touching address parsing, coinbase construction, share validation or
  difficulty handling is security-relevant. Explain the threat model in the PR.

## Style

- Python 3.11+, PEP 8, type hints on new functions, `from __future__ import annotations` where it helps.
- Prefer explicit error handling over broad `except Exception`; when you must catch broadly, say why in a
  comment and log enough to debug it.
- Comments explain intent and invariants, not syntax. Non-obvious protocol details (value-pool accounting,
  coinbase layout, Stratum corner cases) deserve a short comment with a reference to the relevant ZIP.
- For commit messages, branch history and pull-request hygiene we follow the standards the Zcash ecosystem
  uses in [`librustzcash/CONTRIBUTING.md`](https://github.com/zcash/librustzcash/blob/main/CONTRIBUTING.md#styleguides):
  small focused commits, imperative subject lines, one logical change per PR, reviewable diffs.

## Pull requests

1. Describe the problem, the change, and how you verified it. If you cannot verify it on a real node, say so.
2. For protocol-adjacent changes, include the test or the reproduction that shows the old behaviour was
   wrong.
3. Keep unrelated reformatting out of the diff.
4. State which `zebrad` version you tested against (`zebrad --version`).

## Reporting bugs and security issues

- **Functional bugs / feature requests:** open a GitHub issue with logs, `zebrad` version, and the relevant
  `status_file` fields (`shares_total`, `shares_rejected`, `reject_reasons`, `worker_list`).
- **Security issues:** please do not open a public issue. Email the maintainer listed in the README, or use
  GitHub's private vulnerability reporting on this repository, and we will coordinate a fix and disclosure.

## What is most useful right now

- Independent deployment reports — did the quickstart work on your node? What broke?
- Tests around share validation, vardiff transitions and stale-job handling.
- Documentation for operators (runbook, alerting recipes, capacity notes).

See the README roadmap for the current priorities.
