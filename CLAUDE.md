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
python3 -m uplc digest -o digests              # daily digest (network + LLM;
                                               #   --no-llm for deterministic)
python3 -m uplc status | stuck | blockers      # terminal reports
python3 -m uplc html -o dash | serve           # dashboard site (5 pages + feeds)
```

Run from the repo root (or `pip install -e .` for the `uplc` entry point).
Only third-party dependency is PyYAML — keep it that way; the tool must run
from cron on a stock Ubuntu box with system Python.

## Architecture (data flow)

```
fetch.py      conditional-GET cache (ETag/If-Modified-Since) → ~/.cache/uplc
sources.py    clients+parsers: team mapping, update_excuses.yaml.xz (britney),
              Sources.xz indexes, Merge-o-Matic (optional), pending-SRU
              report (optional, sru_report.yaml) → plain dicts/Excuse
state.py      PURE functions: facts per package → PackageState (one of STATES,
              funnel-ordered; see README for meanings)
ingest.py     orchestrates one run; devel series from /usr/share/distro-info
lpbugs.py     watermarked LP bug sync (searchTasks + by-ID fetches) and the
              team package list (getBugSubscriberPackages); parsing is pure
              functions, mocked in tests — never hit LP from tests
db.py         SQLite (~/.local/share/uplc/uplc.db): ingest_runs + full snapshot
              per run + derived transitions rows; pending_sru is likewise a
              full snapshot per run (team-filtered pending-SRU report rows,
              so digest.py can diff consecutive runs the same way it diffs
              snapshots); bugs/bug_tasks/bug_sync are upserted current state
              (LP dates give retroactive history)
kpi.py        PURE metric computations (percentages, windowed rates, backlog
              reconstruction from LP dates, SRU verification-queue health)
              — no network/DB, like state.py
digest.py     daily curated digest: PURE parsers/event-derivation/facts/
              fallback renderer; network only in fetch_bug_extras (by-ID
              messages+activity via lpbugs._cached_json); the only
              subprocess use in the codebase (LLM runner contract:
              `<UPLC_LLM_CMD> <prompt>`, facts JSON on stdin, Markdown on
              stdout; validated, deterministic fallback on any failure).
              SRU events come from diffing consecutive pending_sru snapshots
              (sources.bug_verification_status reads sru-report's own `cls`
              — real verified/failed/removal-candidate, not derived); falls
              back to sru_activity_events (stable-series bug-task changes)
              only when pending_sru has no rows this run
report.py     terminal tables
htmlreport.py static dashboard site: index/packages/bugs/kpi/digest pages
              (PAGES dict) + feed.json/feed.xml (JSON Feed + Atom from
              digest_runs); inline CSS+JS only, degrades to plain tables
              without JS. KPI charts are static inline SVG (native <title>
              tooltips). digest.html is a blog-style month-grouped archive
              rendered by a small pure _md_html converter. Package names
              link to packages.html#pkg-<name> first; bug numbers always
              link to Launchpad; filters live in URL hashes. bugs.html has
              a pending-SRU table, kpi.html an SRU verification-queue section
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
  comparison, or pending-SRU report → digest's bug-activity approximation)
  rather than failing the run.
- `debversion.py` is a pure-Python dpkg comparator; its tests cross-check
  against real `dpkg --compare-versions` — extend CASES when touching it.

## Network facts (verified 2026-07-15, corrected 2026-07-17)

- `people.canonical.com/~ubuntu-archive/*` redirects to
  `ubuntu-archive-team.ubuntu.com` — use the latter directly.
- `merges.ubuntu.com` and `static-reports.ubuntu.com` share an ingress that
  has been observed unreachable (TCP timeout) on more than one occasion.
  This is **not** a VPN/access-control wall — none of these archive hosts
  require VPN access; treat outages as transient infra flakiness on
  Canonical's side, not a topology fact to design around. The
  degrade-gracefully behavior below is kept regardless (any bulk source
  can have a bad day), but don't describe it as "VPN-only" in code or
  docs. Practically: team mapping falls back to
  `package-team-mapping.json.apw` on the archive-team host, MoM is
  optional, and the pending-SRU report (`sources.PENDING_SRU_URL`,
  sru-report's own `sru_report.yaml`, also on static-reports) is optional
  too. `ubuntu-archive-team.ubuntu.com/pending-sru.html` redirects to
  static-reports (308) but the `.yaml` sibling doesn't exist on the
  archive-team host at all — fetch straight from static-reports.
- `archive.ubuntu.com/ubuntu/dists/devel/...` resolves the devel series
  server-side; `deb.debian.org` provides Debian unstable Sources.

## Roadmap context (agreed with the user)

Priorities: MoM+excuses (done), bugs (done: `lpbugs.py` + packages/bugs
dashboard pages), SRU track (done, 2026-07-17: `sources.pending_sru()` +
`pending_sru` table + real digest events/KPI/dashboard section — see
below). Bug scope: everything touched since 2026-01-01
(deliberately modified-since, not created-in-2026 and not all-open),
watermark sync
thereafter, targeted fetches for pipeline-referenced bugs (SRU
verification, block-proposed) with no date filter. KPI page (kpi.html,
2026-07-17) covers day/week/month rates: bug rates from LP dates are
complete, pipeline rates ride the young transition history and mature
with it. Daily digest (2026-07-17): `uplc digest` windows tile on the
bugs-sync watermark ((since, until], strict >), pipeline events diff the
runs bounding the window, SRU events diff consecutive `pending_sru`
snapshots (real verification status via sru-report's own `cls`,
2026-07-17) — falls back to the original stable-series-task-change
approximation only when that report had no rows this run; digest bodies
live in the digest_runs table so html/serve stay self-contained. Cron
order: ingest → bugs-sync → digest → html. Next: sponsorship queue,
richer trend rollups over snapshot history once it deepens.
