"""SQLite persistence: snapshot every ingest run, derive transitions.

History accumulates locally so time-based metrics (per day/week/month)
never require re-fetching anything.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

from .state import PackageState

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,               -- UTC ISO-8601
    team TEXT NOT NULL,
    series TEXT NOT NULL,
    excuses_generated TEXT,
    package_count INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id INTEGER NOT NULL REFERENCES ingest_runs(id),
    package TEXT NOT NULL,
    state TEXT NOT NULL,
    summary TEXT,
    ubuntu_version TEXT,
    debian_version TEXT,
    proposed_version TEXT,
    age_days REAL,
    detail TEXT,                        -- JSON: blocked_by/regressions/builds/bugs
    PRIMARY KEY (run_id, package)
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    last_seen_old TEXT NOT NULL,        -- ran_at of last run in old state
    first_seen_new TEXT NOT NULL        -- ran_at of first run in new state
);
CREATE INDEX IF NOT EXISTS idx_snapshots_package ON snapshots(package);
CREATE INDEX IF NOT EXISTS idx_transitions_package ON transitions(package);
CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY,             -- Launchpad bug number
    title TEXT,
    tags TEXT,                          -- JSON array
    date_created TEXT,
    date_last_updated TEXT,
    heat INTEGER,
    origin TEXT NOT NULL DEFAULT 'subscription',  -- or 'pipeline'
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS bug_tasks (
    bug_id INTEGER NOT NULL REFERENCES bugs(id),
    package TEXT NOT NULL,              -- '' for distribution-wide tasks
    series TEXT NOT NULL DEFAULT '',    -- '' for the devel task
    status TEXT,
    importance TEXT,
    assignee TEXT,
    date_created TEXT,
    date_closed TEXT,
    PRIMARY KEY (bug_id, package, series)
);
CREATE TABLE IF NOT EXISTS bug_sync (
    team TEXT PRIMARY KEY,
    watermark TEXT,                     -- max bug date_last_updated ingested
    last_synced TEXT,
    bug_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bug_tasks_package ON bug_tasks(package);
CREATE TABLE IF NOT EXISTS digest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,               -- UTC ISO-8601
    team TEXT NOT NULL,
    since_iso TEXT NOT NULL,            -- window covered by this digest
    until_iso TEXT NOT NULL,
    date TEXT NOT NULL,                 -- YYYY-MM-DD the digest is "for"
    bug_count INTEGER,
    used_llm INTEGER,                   -- 0/1
    body TEXT                           -- final markdown
);
"""

# Launchpad statuses that count as "open" everywhere in uplc.
OPEN_BUG_STATUSES = (
    "New", "Incomplete", "Confirmed", "Triaged", "In Progress",
    "Fix Committed", "Deferred",
)


def default_db_path() -> Path:
    override = os.environ.get("UPLC_DB")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return Path(xdg) / "uplc" / "uplc.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_run(
    conn: sqlite3.Connection,
    states: list[PackageState],
    *,
    team: str,
    series: str,
    excuses_generated: str = "",
) -> int:
    """Insert one ingest run: snapshot rows plus any observed transitions."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    previous: dict[str, str] = {}
    prev_ran_at = None
    row = conn.execute(
        "SELECT id, ran_at FROM ingest_runs WHERE team = ? "
        "ORDER BY id DESC LIMIT 1", (team,)).fetchone()
    if row:
        prev_ran_at = row["ran_at"]
        previous = {
            r["package"]: r["state"]
            for r in conn.execute(
                "SELECT package, state FROM snapshots WHERE run_id = ?",
                (row["id"],))
        }

    cur = conn.execute(
        "INSERT INTO ingest_runs (ran_at, team, series, excuses_generated,"
        " package_count) VALUES (?, ?, ?, ?, ?)",
        (now, team, series, excuses_generated, len(states)))
    run_id = cur.lastrowid

    for ps in states:
        detail = json.dumps({
            "blocked_by": ps.blocked_by,
            "regressions": ps.regressions,
            "missing_builds": ps.missing_builds,
            "bugs": ps.bugs,
        })
        conn.execute(
            "INSERT INTO snapshots (run_id, package, state, summary,"
            " ubuntu_version, debian_version, proposed_version, age_days,"
            " detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, ps.package, ps.state, ps.summary, ps.ubuntu_version,
             ps.debian_version, ps.proposed_version, ps.age_days, detail))
        old = previous.get(ps.package)
        if old is not None and old != ps.state:
            conn.execute(
                "INSERT INTO transitions (package, from_state, to_state,"
                " last_seen_old, first_seen_new) VALUES (?, ?, ?, ?, ?)",
                (ps.package, old, ps.state, prev_ran_at, now))

    conn.commit()
    return run_id


def latest_run(conn: sqlite3.Connection, team: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ingest_runs WHERE team = ? ORDER BY id DESC LIMIT 1",
        (team,)).fetchone()


def first_run_at(conn: sqlite3.Connection, team: str) -> str | None:
    row = conn.execute(
        "SELECT MIN(ran_at) AS t FROM ingest_runs WHERE team = ?",
        (team,)).fetchone()
    return row["t"] if row else None


def run_at_or_before(
    conn: sqlite3.Connection, team: str, iso: str,
) -> sqlite3.Row | None:
    """Latest ingest run not newer than `iso` — for point-in-time compares."""
    return conn.execute(
        "SELECT * FROM ingest_runs WHERE team = ? AND ran_at <= ? "
        "ORDER BY id DESC LIMIT 1", (team, iso)).fetchone()


def all_transitions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT package, from_state, to_state, last_seen_old, first_seen_new"
        " FROM transitions ORDER BY id").fetchall()


def snapshots_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots WHERE run_id = ? ORDER BY package",
        (run_id,)).fetchall()


def state_entered_at(conn: sqlite3.Connection, package: str) -> str | None:
    """When we first observed the package in its current state.

    Falls back to the earliest snapshot mentioning the package in that
    state if no transition was recorded (i.e. state predates our history).
    """
    row = conn.execute(
        "SELECT first_seen_new FROM transitions WHERE package = ? "
        "ORDER BY id DESC LIMIT 1", (package,)).fetchone()
    if row:
        return row["first_seen_new"]
    row = conn.execute(
        "SELECT MIN(r.ran_at) AS since FROM snapshots s"
        " JOIN ingest_runs r ON r.id = s.run_id"
        " WHERE s.package = ? AND s.state = ("
        "   SELECT state FROM snapshots WHERE package = ?"
        "   ORDER BY run_id DESC LIMIT 1)",
        (package, package)).fetchone()
    return row["since"] if row else None


def record_bug(conn: sqlite3.Connection, bug: dict, tasks: list[dict]) -> None:
    """Upsert one bug and replace its task rows.

    Bugs are mutable current-state (unlike pipeline snapshots): Launchpad's
    date_created/date_closed already give retroactive history, so there is
    nothing to lose by updating in place. A bug once seen via the team
    subscription keeps origin='subscription' even if later re-fetched as a
    pipeline reference.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO bugs (id, title, tags, date_created, date_last_updated,"
        " heat, origin, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        " title=excluded.title, tags=excluded.tags,"
        " date_created=excluded.date_created,"
        " date_last_updated=excluded.date_last_updated,"
        " heat=excluded.heat, fetched_at=excluded.fetched_at,"
        " origin=CASE WHEN bugs.origin='subscription' THEN 'subscription'"
        "         ELSE excluded.origin END",
        (bug["id"], bug.get("title", ""), json.dumps(bug.get("tags", [])),
         bug.get("date_created", ""), bug.get("date_last_updated", ""),
         bug.get("heat", 0), bug.get("origin", "subscription"), now))
    conn.execute("DELETE FROM bug_tasks WHERE bug_id = ?", (bug["id"],))
    for t in tasks:
        conn.execute(
            "INSERT OR REPLACE INTO bug_tasks (bug_id, package, series,"
            " status, importance, assignee, date_created, date_closed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bug["id"], t.get("package", ""), t.get("series", ""),
             t.get("status", ""), t.get("importance", ""),
             t.get("assignee", ""), t.get("date_created", ""),
             t.get("date_closed", "")))


def bug_sync_state(conn: sqlite3.Connection, team: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bug_sync WHERE team = ?", (team,)).fetchone()


def set_bug_sync_state(
    conn: sqlite3.Connection, team: str, watermark: str,
) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    count = conn.execute("SELECT COUNT(*) AS n FROM bugs").fetchone()["n"]
    conn.execute(
        "INSERT INTO bug_sync (team, watermark, last_synced, bug_count)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(team) DO UPDATE SET"
        " watermark=excluded.watermark, last_synced=excluded.last_synced,"
        " bug_count=excluded.bug_count",
        (team, watermark, now, count))


def bugs_with_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every bug task joined with its bug, for the reports."""
    return conn.execute(
        "SELECT b.id, b.title, b.tags, b.date_created, b.date_last_updated,"
        " b.origin, t.package, t.series, t.status, t.importance, t.assignee,"
        " t.date_closed FROM bugs b JOIN bug_tasks t ON t.bug_id = b.id"
        " ORDER BY b.id").fetchall()


def bugs_modified_since(
    conn: sqlite3.Connection, since_iso: str,
) -> list[sqlite3.Row]:
    """Bug tasks joined with bugs whose bug changed after `since_iso`.

    Strictly after: `since_iso` is the previous digest's end watermark,
    already covered, so consecutive windows tile without duplicates.
    """
    return conn.execute(
        "SELECT b.id, b.title, b.tags, b.date_created, b.date_last_updated,"
        " b.heat, b.origin, t.package, t.series, t.status, t.importance,"
        " t.assignee, t.date_created AS task_created, t.date_closed"
        " FROM bugs b JOIN bug_tasks t ON t.bug_id = b.id"
        " WHERE b.date_last_updated > ? ORDER BY b.id",
        (since_iso,)).fetchall()


def transitions_between(
    conn: sqlite3.Connection, since_iso: str, until_iso: str,
) -> list[sqlite3.Row]:
    """State transitions first observed inside the (since, until] window."""
    return conn.execute(
        "SELECT package, from_state, to_state, last_seen_old, first_seen_new"
        " FROM transitions WHERE first_seen_new > ? AND first_seen_new <= ?"
        " ORDER BY id", (since_iso, until_iso)).fetchall()


def record_digest_run(
    conn: sqlite3.Connection,
    *,
    team: str,
    since_iso: str,
    until_iso: str,
    date: str,
    bug_count: int,
    used_llm: bool,
    body: str,
) -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO digest_runs (ran_at, team, since_iso, until_iso, date,"
        " bug_count, used_llm, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now, team, since_iso, until_iso, date, bug_count,
         1 if used_llm else 0, body))
    conn.commit()
    return cur.lastrowid


def latest_digest_run(conn: sqlite3.Connection, team: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM digest_runs WHERE team = ? ORDER BY id DESC LIMIT 1",
        (team,)).fetchone()


def digest_runs(
    conn: sqlite3.Connection, team: str, limit: int | None = None,
) -> list[sqlite3.Row]:
    """Digest history newest-first, one row per date (last rerun wins).

    With `limit=None` returns the full history for the archive page; feeds
    slice what they need.
    """
    rows = conn.execute(
        "SELECT * FROM digest_runs WHERE team = ? AND id IN ("
        "  SELECT MAX(id) FROM digest_runs WHERE team = ? GROUP BY date)"
        " ORDER BY date DESC", (team, team)).fetchall()
    return rows[:limit] if limit is not None else rows


def bug_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM bugs").fetchone()["n"]


def open_bug_total(conn: sqlite3.Connection) -> int:
    marks = ",".join("?" * len(OPEN_BUG_STATUSES))
    return conn.execute(
        f"SELECT COUNT(DISTINCT bug_id) AS n FROM bug_tasks"
        f" WHERE status IN ({marks})", OPEN_BUG_STATUSES).fetchone()["n"]


def open_bug_counts(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-package open-bug stats: {package: {'open': n, 'high': n}}.

    A bug counts once per package it targets (any series), as open when
    any of its tasks for that package is in an open status.
    """
    marks = ",".join("?" * len(OPEN_BUG_STATUSES))
    rows = conn.execute(
        "SELECT package,"
        " COUNT(DISTINCT bug_id) AS open_bugs,"
        " COUNT(DISTINCT CASE WHEN importance IN ('Critical', 'High')"
        "   THEN bug_id END) AS high_bugs"
        f" FROM bug_tasks WHERE status IN ({marks}) AND package != ''"
        " GROUP BY package", OPEN_BUG_STATUSES).fetchall()
    return {r["package"]: {"open": r["open_bugs"], "high": r["high_bugs"]}
            for r in rows}
