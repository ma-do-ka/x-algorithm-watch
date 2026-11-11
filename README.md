# Public assumption watch

A deterministic Python standard-library monitor for public changes to X algorithm parameters, release notes, and developer rules and pricing. It keeps raw snapshots and itemised numeric or unified text diffs so upstream changes can be reviewed independently of any other system. It does not interpret changes with AI.

The enabled targets are `xai-org/x-algorithm`'s `home-mixer/params/param.rs` and `README.md`, fetched from `raw.githubusercontent.com`. `watch_targets.json` also records disabled TODOs for automation rules, developer policy, and pricing in `xdevplatform/docs`: those entries must have their actual paths confirmed through the public GitHub API repository tree before enabling. No speculative URLs are used. Discovery attempted `https://api.github.com/repos/xdevplatform/docs/git/trees/main?recursive=1`; direct API access was blocked by network permissions and the read-only web tool could not access it. Therefore none of those three paths is confirmed. Retry this API tree listing and review the returned paths before replacing each TODO and enabling its target. Freshness uses the algorithm repository's GitHub commits API and warns at exactly 14 days without a commit.

## Run

Python 3.9 or newer; no installation or third-party dependencies:

```sh
python3 watch.py
python3 -m unittest -v
```

The first run establishes baselines in `snapshots/`. Later runs update snapshots, write `last_run.json`, and append meaningful changes and STALE warnings to UTC-dated `history/YYYY-MM-DD.md`. Changes return exit code 0; failed targets, freshness checks, configuration, or output writes return 1. Each target is checked independently. Notifications failing are reported as warnings, without exposing endpoint URLs. Whitespace-only changes update snapshots but do not append history or notify. Other text changes are conservatively meaningful; keyword classification is mechanical, not a judgment about impact. STALE warnings repeat daily until the source becomes fresh.

Synthetic fixtures allow entirely offline runs, with all notifications disabled even if environment variables are set:

```sh
python3 watch.py --offline-fixture fixtures/baseline --output-dir /tmp/watch-example
python3 watch.py --offline-fixture fixtures/changed --output-dir /tmp/watch-example
```

Fixtures use `<fixture>/<owner>/<repo>/<path>` and `commits.json` containing the same list shape as the GitHub commits API. Fixture commit dates are fixed, so their freshness result depends on the current date. Tests supply a fixed UTC time and prevent network access. `--config` selects another target file; `--output-dir` isolates generated files.

Numeric parsing supports Rust `const NAME: TYPE = literal;` declarations with signed decimal, exponent, hex, octal, binary, separators and numeric suffixes. Added and removed constants are itemised. Expressions are retained in text diffs but are not evaluated. Categories are `weights`, `visibility`, `rules_pricing`, and `other`, based on changed lines rather than unchanged context.

## Public repository safety and automation

Only public-source snapshots, generic code, and synthetic test fixtures belong here. Keep secrets, account data, private strategy, and strategy/ledger mapping outside this repository. Never put webhook or ping URLs into files. Set `DISCORD_WEBHOOK_URL` and `HEALTHCHECKS_PING_URL` only through environment variables locally or Actions Secrets in GitHub. Unset variables safely do nothing. Discord receives compact summaries for meaningful changes or hard failures. Healthchecks receives a success GET every successful run; on failure, known `hc-ping.com` HTTPS endpoints receive the `/fail` path suffix. Other endpoint hosts receive no failure ping. Notification errors never include secret URLs in reports.

The workflow runs at 00:00 UTC daily (09:00 JST), or manually with `workflow_dispatch`. It tests first, watches sources, and commits snapshots, history and the latest report using the GitHub Actions bot identity. It requires repository contents write permission. Publishing or initializing a repository is a separate manual step; this delivery creates files only.
