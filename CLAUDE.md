# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`uplc` observes where a team's Ubuntu packages (default: `foundations-bugs`,
~152 packages) sit in the development pipeline — Debian delta,
proposed-migration, blockers — for a **manager audience** (funnel counts,
what's stuck, biggest unblock opportunity).

**Core constraint: never add Launchpad API load.** All current data comes
from bulk-published archive artifacts fetched with conditional GETs. The
planned bug ingester (see README roadmap) is the only sanctioned LP API use:
one `searchTasks(structural_subscriber=..., modified_since=watermark)` sync
plus targeted by-ID fetches — never per-package polling.

## Commands

```console
python3 -m unittest discover -s tests          # run tests
python3 -m unittest tests.test_state -v        # run one test module
python3 -m uplc -v ingest                      # fetch sources + snapshot (network)
python3 -m uplc status | stuck | blockers      # terminal reports
python3 -m uplc html -o dash.html | serve      # dashboard
```

Run from the repo root (or `pip install -e .` for the `uplc` entry point).
Only third-party dependency is PyYAML — keep it that way; the tool must run
from cron on a stock Ubuntu box with system Python.

## Architecture (data flow)

```
fetch.py      conditional-GET cache (ETag/If-Modified-Since) → ~/.cache/uplc
sources.py    clients+parsers: team mapping, update_excuses.yaml.xz (britney),
              Sources.xz indexes, Merge-o-Matic (optional) → plain dicts/Excuse
state.py      PURE functions: facts per package → PackageState (one of STATES,
              funnel-ordered; see README for meanings)
ingest.py     orchestrates one run; devel series from /usr/share/distro-info
db.py         SQLite (~/.local/share/uplc/uplc.db): ingest_runs + full snapshot
              per run + derived transitions rows
report.py     terminal tables      htmlreport.py  static dashboard
cli.py        argparse subcommands
```

Design invariants:

- `state.py` stays pure (no network/DB) so pipeline logic is unit-testable;
  parsing lives in `sources.py`, persistence in `db.py`.
- Every ingest appends a **full snapshot**; history is never rewritten.
  Time-in-state comes from `transitions` (bounded by ingest cadence — store
  both `last_seen_old` and `first_seen_new`, never pretend to exact times).
- Sources may be stale relative to each other: excuses vs archive indexes
  skew is resolved by version comparison (`already_migrated` in state.py).
- Any unreachable source degrades gracefully (stale cache, or MoM → Sources
  comparison) rather than failing the run.
- `debversion.py` is a pure-Python dpkg comparator; its tests cross-check
  against real `dpkg --compare-versions` — extend CASES when touching it.

## Network facts (verified 2026-07-15)

- `people.canonical.com/~ubuntu-archive/*` redirects to
  `ubuntu-archive-team.ubuntu.com` — use the latter directly.
- `merges.ubuntu.com` and `static-reports.ubuntu.com` share an ingress that
  is unreachable from off-VPN networks (TCP timeout). Hence:
  team mapping falls back to `package-team-mapping.json.apw` on the
  archive-team host, and MoM is optional. Never make these sources required.
- `archive.ubuntu.com/ubuntu/dists/devel/...` resolves the devel series
  server-side; `deb.debian.org` provides Debian unstable Sources.

## Roadmap context (agreed with the user)

Priorities: MoM+excuses first (done), then bugs. Bug ingester scope:
open bugs `modified_since=2026-01-01` (deliberately modified-since, not
created-in-2026), watermark sync thereafter, targeted fetches for bugs
referenced by pipeline data (SRU verification, block-proposed) with no date
filter. Then: pending-sru.json SRU track, sponsorship queue, trend rollups
(day/week/month) over snapshot history.
