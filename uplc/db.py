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
"""


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
