"""Pure KPI computations: health percentages and rates of change.

Inputs are plain dicts/Counters (aggregated bugs, transition rows, state
counts) and an explicit `now`, so everything here is unit-testable without
a database or network — same contract as state.py.

Two very different clocks feed these numbers:

- Bug rates come from Launchpad's own dates (date_created/date_closed),
  so daily/weekly/monthly figures are complete even before uplc existed —
  limited only by the sync scope (bugs touched since the first-sync date).
- Pipeline rates come from observed snapshot-to-snapshot transitions, so
  they start at the first ingest run and mature as history accumulates.
"""

from collections import Counter
from datetime import datetime, timedelta
from statistics import median

from .state import BLOCKED_STATES, PROPOSED_STATES

# (label, days) — the three rate-of-change windows on the KPI page.
WINDOWS = (("Daily", 1), ("Weekly", 7), ("Monthly", 30))

TRIAGED_IMPORTANCES = ("Critical", "High", "Medium", "Low", "Wishlist")


def parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def pct(part: int, whole: int) -> float | None:
    """Percentage, or None when the denominator is empty."""
    return 100.0 * part / whole if whole else None


def package_kpis(counts: Counter) -> dict:
    """Set-level health from one snapshot's state counts."""
    total = sum(counts.values())
    in_proposed = sum(counts.get(s, 0) for s in PROPOSED_STATES)
    blocked = sum(counts.get(s, 0) for s in BLOCKED_STATES)
    behind = counts.get("merge-needed", 0) + counts.get("sync-available", 0)
    current = counts.get("in-sync", 0) + counts.get("ubuntu-only", 0)
    return {
        "total": total,
        "current": current,
        "current_pct": pct(current, total),
        "behind": behind,
        "behind_pct": pct(behind, total),
        "in_proposed": in_proposed,
        "proposed_pct": pct(in_proposed, total),
        "blocked": blocked,
        "blocked_of_proposed_pct": pct(blocked, in_proposed),
    }


def bug_kpis(bugs: list[dict], now: datetime) -> dict:
    """Health of the open-bug backlog (triage/assignment/activity)."""
    open_bugs = [b for b in bugs if b["open"]]
    triaged = [b for b in open_bugs
               if b["status"] != "New"
               and b["importance"] in TRIAGED_IMPORTANCES]
    assigned = [b for b in open_bugs if b["assignees"]]
    high = [b for b in open_bugs
            if b["importance"] in ("Critical", "High")]
    active_cut = now - timedelta(days=30)
    active = [b for b in open_bugs
              if (u := parse_dt(b["updated"])) and u > active_cut]
    ages = [(now - c).days for b in open_bugs
            if (c := parse_dt(b["created"]))]
    return {
        "open": len(open_bugs),
        "triaged": len(triaged),
        "triaged_pct": pct(len(triaged), len(open_bugs)),
        "assigned": len(assigned),
        "assigned_pct": pct(len(assigned), len(open_bugs)),
        "high": len(high),
        "high_pct": pct(len(high), len(open_bugs)),
        "active": len(active),
        "active_pct": pct(len(active), len(open_bugs)),
        "median_age_days": median(ages) if ages else None,
    }


def bug_window_rates(bugs: list[dict], now: datetime, days: int) -> dict:
    """Bugs opened/closed in the trailing window, from Launchpad dates."""
    cutoff = now - timedelta(days=days)
    opened = sum(1 for b in bugs
                 if (c := parse_dt(b["created"])) and c > cutoff)
    closed = sum(1 for b in bugs
                 if (c := parse_dt(b["closed"])) and c > cutoff)
    return {"opened": opened, "closed": closed, "net": opened - closed}


def transition_window_rates(
    transitions: list[dict], now: datetime, days: int,
) -> dict:
    """Observed pipeline movement in the trailing window.

    `migrated` = left -proposed for a released state; `entered` = the
    reverse. Bounded by ingest history, so early windows undercount.
    """
    cutoff = now - timedelta(days=days)
    changes = migrated = entered = 0
    for t in transitions:
        seen = parse_dt(t["first_seen_new"])
        if seen is None or seen <= cutoff:
            continue
        changes += 1
        was_prop = t["from_state"] in PROPOSED_STATES
        is_prop = t["to_state"] in PROPOSED_STATES
        if was_prop and not is_prop and t["to_state"] != "not-in-devel":
            migrated += 1
        if not was_prop and is_prop:
            entered += 1
    return {"changes": changes, "migrated": migrated, "entered": entered}


def open_backlog_at(bugs: list[dict], when: datetime) -> int:
    """Open bugs at a past instant, reconstructed from Launchpad dates.

    A closed bug without a close date can't be placed in time and is
    skipped at every instant, so deltas between instants stay consistent.
    Only counts the synced set (touched since the first-sync date).
    """
    n = 0
    for b in bugs:
        created = parse_dt(b["created"])
        if created is None or created > when:
            continue
        closed = parse_dt(b["closed"])
        if closed is not None and closed <= when:
            continue
        if closed is None and not b["open"]:
            continue
        n += 1
    return n


def daily_bug_series(
    bugs: list[dict], days: int, now: datetime,
) -> list[tuple]:
    """[(date, opened, closed)] per calendar day, oldest first."""
    start = (now - timedelta(days=days - 1)).date()
    opened: Counter = Counter()
    closed: Counter = Counter()
    for b in bugs:
        if (c := parse_dt(b["created"])) and c.date() >= start:
            opened[c.date()] += 1
        if (c := parse_dt(b["closed"])) and c.date() >= start:
            closed[c.date()] += 1
    out = []
    for i in range(days):
        day = start + timedelta(days=i)
        if day > now.date():
            break
        out.append((day, opened[day], closed[day]))
    return out


def backlog_series(
    bugs: list[dict], days: int, now: datetime,
) -> list[tuple]:
    """[(date, open_backlog)] at each day's end, oldest first, ending now."""
    out = []
    for i in range(days - 1, 0, -1):
        day = now - timedelta(days=i)
        eod = day.replace(hour=23, minute=59, second=59)
        out.append((day.date(), open_backlog_at(bugs, eod)))
    out.append((now.date(), open_backlog_at(bugs, now)))
    return out


def bug_concentration(bugs: list[dict], top: int = 5) -> dict:
    """How concentrated open bugs are: the top-N packages' share."""
    per_pkg: Counter = Counter()
    total = 0
    for b in bugs:
        if not b["open"] or not b["packages"]:
            continue
        total += 1
        for p in b["packages"]:
            per_pkg[p] += 1
    leaders = per_pkg.most_common(top)
    covered = len([b for b in bugs if b["open"] and b["packages"]
                   and any(p in dict(leaders) for p in b["packages"])])
    return {
        "leaders": leaders,
        "share_pct": pct(covered, total),
        "packages_with_bugs": len(per_pkg),
    }
