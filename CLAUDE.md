# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`uplc` observes where a team's Ubuntu packages (default: `foundations-bugs`,
~152 packages) sit in the development pipeline — Debian delta,
proposed-migration, blockers, bugs — for a **manager audience** (funnel
counts, what's stuck, biggest unblock opportunity, where to press on bugs).

**Core constraint: never add Launchpad API load.** Pipeline data comes
from bulk-published archive artifacts fetched with conditional GETs.
`lpbugs.py` is the only sanctioned LP API use: one
`searchTasks(structural_subscriber=..., modified_since=watermark)` sync
plus targeted by-ID fetches (through the conditional-GET cache) — never
per-package polling — plus one `getBugSubscriberPackages` call per ingest
for the team package list (the published package-team-mapping.json copy
froze in May 2025 at 152 packages vs 210 live; it remains only as a
fallback when LP is unreachable). First sync is deliberately heavy (everything touched
since 2026-01-01, any status); increments after that. Dormant-but-open
bugs are excluded on purpose — an all-open sweep was tried and pulled in
12k untouched bugs, drowning the actionable ~1k.

## Commands

```console
python3 -m unittest discover -s tests          # run tests
python3 -m unittest tests.test_state -v        # run one test module
python3 -m uplc -v ingest                      # fetch sources + snapshot (network)
python3 -m uplc -v bugs-sync                   # sync team bugs from LP (network)
python3 -m uplc status | stuck | blockers      # terminal reports
python3 -m uplc html -o dash | serve           # dashboard site (dir out, 3 pages)
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
lpbugs.py     watermarked LP bug sync (searchTasks + by-ID fetches) and the
              team package list (getBugSubscriberPackages); parsing is pure
              functions, mocked in tests — never hit LP from tests
db.py         SQLite (~/.local/share/uplc/uplc.db): ingest_runs + full snapshot
              per run + derived transitions rows; bugs/bug_tasks/bug_sync are
              upserted current state (LP dates give retroactive history)
kpi.py        PURE metric computations (percentages, windowed rates, backlog
              reconstruction from LP dates) — no network/DB, like state.py
report.py     terminal tables
htmlreport.py static dashboard site: index/packages/bugs/kpi pages (PAGES
              dict); inline CSS+JS only, degrades to plain tables without JS.
              KPI charts are static inline SVG (native <title> tooltips).
              Package names link to packages.html#pkg-<name> first; bug
              numbers always link to Launchpad; filters live in URL hashes
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

Priorities: MoM+excuses (done), bugs (done: `lpbugs.py` + packages/bugs
dashboard pages). Bug scope: everything touched since 2026-01-01
(deliberately modified-since, not created-in-2026 and not all-open),
watermark sync
thereafter, targeted fetches for pipeline-referenced bugs (SRU
verification, block-proposed) with no date filter. KPI page (kpi.html,
2026-07-17) covers day/week/month rates: bug rates from LP dates are
complete, pipeline rates ride the young transition history and mature
with it. Next: pending-sru.json SRU track, sponsorship queue, richer
trend rollups over snapshot history once it deepens.
