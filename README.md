# uplc — Ubuntu Package Life Cycle

Observe where a team's Ubuntu packages sit in the development pipeline —
Debian delta, proposed-migration, blockers — **without touching the
Launchpad API**. Built for the manager view: funnel counts, what's stuck,
where the biggest unblock opportunity is.

## How it works

All data comes from bulk artifacts the Ubuntu archive tooling already
publishes, fetched with conditional GETs (ETag / If-Modified-Since) into a
local cache:

| Question | Source |
|---|---|
| Which packages does my team own? | `package-team-mapping.json` (ubuntu-archive-team.ubuntu.com) |
| Why is something stuck in -proposed? | `update_excuses.yaml.xz` (proposed-migration) |
| What version is where? | `Sources.xz` from archive.ubuntu.com (devel) and deb.debian.org (unstable) |
| Merge metadata | merges.ubuntu.com (optional; degrades to Sources comparison when unreachable) |

Each ingest run snapshots every package's derived lifecycle state into
SQLite (`~/.local/share/uplc/uplc.db`) and records state transitions, so
time-in-state and per-day/week/month metrics accumulate locally — nothing
is ever re-scraped.

## Usage

```console
$ python3 -m uplc ingest            # fetch + snapshot (run from cron/timer)
$ python3 -m uplc status            # funnel summary + proposed pipeline
$ python3 -m uplc stuck --days 7    # blocked items, oldest first
$ python3 -m uplc blockers          # migrations blocking the most packages
$ python3 -m uplc html -o dash.html # self-contained HTML dashboard
$ python3 -m uplc serve             # serve the dashboard on localhost
```

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

## Roadmap

- **Bug ingester** (Launchpad API, gentle): one `searchTasks` per sync with
  `structural_subscriber=foundations-bugs` and a `modified_since=2026-01-01`
  watermark, plus targeted fetches for bugs referenced by pipeline data
  (SRU verification, block-proposed). Bug `date_created`/`date_closed` give
  retroactive trend lines.
- **SRU track**: pending-sru.json + verification-needed bug aging.
- **Sponsorship queue** annotations.
- **Trend views**: per-day/week/month rollups over the snapshot history.
