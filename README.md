# uplc — Ubuntu Package Life Cycle

Observe where a team's Ubuntu packages sit in the development pipeline —
Debian delta, proposed-migration, blockers, bugs — **without hammering the
Launchpad API**. Built for the manager view: funnel counts, what's stuck,
where the biggest unblock opportunity is, where to press on bugs.

## How it works

Pipeline data comes from bulk artifacts the Ubuntu archive tooling already
publishes, fetched with conditional GETs (ETag / If-Modified-Since) into a
local cache:

| Question | Source |
|---|---|
| Which packages does my team own? | `package-team-mapping.json` (ubuntu-archive-team.ubuntu.com) |
| Why is something stuck in -proposed? | `update_excuses.yaml.xz` (proposed-migration) |
| What version is where? | `Sources.xz` from archive.ubuntu.com (devel) and deb.debian.org (unstable) |
| Merge metadata | merges.ubuntu.com (optional; degrades to Sources comparison when unreachable) |
| Team bugs | anonymous Launchpad API, watermarked (see below) |

Each ingest run snapshots every package's derived lifecycle state into
SQLite (`~/.local/share/uplc/uplc.db`) and records state transitions, so
time-in-state and per-day/week/month metrics accumulate locally — nothing
is ever re-scraped.

Bugs are the one sanctioned Launchpad API use, and it is deliberately
gentle: each `bugs-sync` runs **one** `searchTasks` query
(`structural_subscriber=<team>` + a `modified_since` watermark) and then
fetches exactly the bugs that search returned by ID through the same
conditional-GET cache — never per-package polling. The first sync is heavy
(every bug open or touched this year); every later sync is a small
increment. Bugs referenced by pipeline data (block-proposed /
update-excuse) are fetched by ID regardless of subscription.

## Usage

```console
$ python3 -m uplc ingest            # fetch + snapshot (run from cron/timer)
$ python3 -m uplc bugs-sync         # sync team bugs from Launchpad
$ python3 -m uplc status            # funnel summary + proposed pipeline
$ python3 -m uplc stuck --days 7    # blocked items, oldest first
$ python3 -m uplc blockers          # migrations blocking the most packages
$ python3 -m uplc html -o dash      # static dashboard site (3 pages)
$ python3 -m uplc serve             # serve the dashboard on localhost
```

The dashboard is a self-contained static site (inline CSS/JS, no external
requests) with three interconnected pages:

- **index.html** — manager overview: tiles, funnel, proposed pipeline,
  biggest unblock opportunities.
- **packages.html** — every team package with client-side filters (state,
  bug counts, time in state, free text) and sortable columns; each row
  expands to detail plus out-links (Launchpad, excuses, Debian tracker).
  Package names everywhere link here first (`#pkg-<name>`).
- **bugs.html** — bug prioritization: pipeline-gating bugs first, opened
  vs closed trend, and a filterable table (open/closed, importance,
  untriaged, unassigned, stale, gating). Bug numbers always link to
  Launchpad. Filters are reflected in the URL hash, so filtered views are
  shareable (e.g. `bugs.html#q=glibc`).

Default team is `foundations-bugs`; use `--team` for any team in the
mapping. Only dependency beyond the standard library is PyYAML
(`python3-yaml`). Install as a command with `pip install -e .`.

Run the tests with `python3 -m unittest discover -s tests` — the Debian
version comparator is cross-checked against real `dpkg --compare-versions`.

## Lifecycle states

```
blocked-build      missing builds / FTBFS in -proposed
blocked-tests      autopkgtest regressions
blocked-depends    waiting on another package's migration
blocked-other      freeze block, block bug, ...
waiting-age        only waiting for the age policy
ready-to-migrate   valid candidate, migrates on next britney run
merge-needed       Debian is newer, Ubuntu delta to remerge
sync-available     Debian is newer, no Ubuntu delta
ubuntu-only        no Debian counterpart
in-sync            devel is current vs Debian unstable
not-in-devel       subscribed package absent from the devel series
```

## Caveats

- "Seen"/stuck ages are measured from local observation history; they
  mature as ingest runs accumulate (there is no retroactive backfill for
  pipeline state).
- The excuses file and archive indexes are generated at different times;
  a migration completed in between is detected via version comparison.
- Debian comparison uses unstable main only.
- Bug sync is anonymous, so private bugs are invisible. Bugs closed before
  2026 and untouched since are out of scope by design.

## Roadmap

- **SRU track**: pending-sru.json + verification-needed bug aging.
- **Sponsorship queue** annotations.
- **Trend views**: per-day/week/month rollups over the snapshot history.
