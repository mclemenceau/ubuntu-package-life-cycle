"""Terminal reports over the latest snapshot (manager funnel views)."""

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from . import db
from .state import BLOCKED_STATES, PROPOSED_STATES, STATES

STATE_LABELS = {
    "blocked-build": "Blocked: missing builds",
    "blocked-tests": "Blocked: autopkgtest",
    "blocked-depends": "Blocked: dependencies",
    "blocked-other": "Blocked: other",
    "waiting-age": "Waiting: age policy",
    "ready-to-migrate": "Ready to migrate",
    "merge-needed": "Merge needed",
    "sync-available": "Sync available",
    "ubuntu-only": "Ubuntu-only",
    "in-sync": "In sync with Debian",
    "not-in-devel": "Not in devel",
}


def _age_str(entered: str | None) -> str:
    if not entered:
        return "?"
    then = datetime.fromisoformat(entered.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - then).total_seconds() / 86400
    if days < 1:
        return "<1d"
    return f"{days:.0f}d"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(c)) for c in col) for col in zip(headers, *rows)] \
        if rows else [len(h) for h in headers]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*(str(c) for c in row)) for row in rows]
    return "\n".join(lines)


def _latest(conn: sqlite3.Connection, team: str):
    run = db.latest_run(conn, team)
    if run is None:
        raise SystemExit(f"no data for team {team!r} — run `uplc ingest` first")
    return run, db.snapshots_for_run(conn, run["id"])


def status(conn: sqlite3.Connection, team: str) -> str:
    run, snaps = _latest(conn, team)
    counts = Counter(s["state"] for s in snaps)

    out = [
        f"Ubuntu Package Life Cycle — {team} — {run['series']} series",
        f"snapshot {run['ran_at']} · excuses {run['excuses_generated']} · "
        f"{run['package_count']} packages",
        "",
    ]
    funnel_rows = [
        [STATE_LABELS[s], counts[s]] for s in STATES if counts.get(s)
    ]
    out.append(_table(funnel_rows, ["State", "Packages"]))

    pipeline = [s for s in snaps if s["state"] in PROPOSED_STATES]
    if pipeline:
        out += ["", f"In -proposed ({len(pipeline)}):", ""]
        rows = []
        for s in sorted(pipeline, key=lambda r: STATES.index(r["state"])):
            rows.append([
                s["package"], STATE_LABELS[s["state"]],
                f"{s['ubuntu_version']} → {s['proposed_version']}",
                _age_str(db.state_entered_at(conn, s["package"])),
                (s["summary"] or "")[:70],
            ])
        out.append(_table(rows, ["Package", "State", "Version", "Seen", "Why"]))
    return "\n".join(out)


def stuck(conn: sqlite3.Connection, team: str, min_days: float = 0) -> str:
    run, snaps = _latest(conn, team)
    rows = []
    for s in snaps:
        if s["state"] not in BLOCKED_STATES:
            continue
        entered = db.state_entered_at(conn, s["package"])
        days = 0.0
        if entered:
            then = datetime.fromisoformat(entered.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - then).total_seconds() / 86400
        if days >= min_days:
            rows.append((days, [
                s["package"], STATE_LABELS[s["state"]],
                f"{days:.0f}d" if days >= 1 else "<1d",
                (s["summary"] or "")[:80],
            ]))
    if not rows:
        return f"nothing blocked ≥ {min_days:.0f} days — pipeline is healthy"
    rows.sort(key=lambda r: -r[0])
    note = ("(ages measured from local observation history; "
            "they mature as ingest runs accumulate)")
    return _table([r for _, r in rows],
                  ["Package", "State", "Stuck", "Why"]) + "\n" + note


def blockers(conn: sqlite3.Connection, team: str) -> str:
    """Fan-out: which single migration blocks the most team packages."""
    _, snaps = _latest(conn, team)
    fan: Counter = Counter()
    victims: dict[str, list[str]] = {}
    for s in snaps:
        detail = json.loads(s["detail"] or "{}")
        for blocker in detail.get("blocked_by", []):
            fan[blocker] += 1
            victims.setdefault(blocker, []).append(s["package"])
    if not fan:
        return "no cross-package blockers right now"
    rows = [
        [blocker, count, ", ".join(sorted(victims[blocker]))[:70]]
        for blocker, count in fan.most_common(20)
    ]
    return _table(rows, ["Blocking item", "Blocks", "Team packages affected"])
