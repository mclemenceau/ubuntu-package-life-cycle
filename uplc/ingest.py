"""Ingest orchestration: fetch all sources, derive states, persist a run."""

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import db, sources
from .state import PackageState, derive_all

log = logging.getLogger("uplc.ingest")

DISTRO_INFO_CSV = Path("/usr/share/distro-info/ubuntu.csv")


def devel_series() -> str:
    """Current devel series codename, best effort from distro-info."""
    try:
        today = date.today()
        with DISTRO_INFO_CSV.open() as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            created = date.fromisoformat(row["created"])
            released = row.get("release") or "9999-12-31"
            if created <= today < date.fromisoformat(released):
                return row["series"]
    except (OSError, KeyError, ValueError):
        pass
    return "devel"


@dataclass
class IngestResult:
    run_id: int
    series: str
    team: str
    excuses_generated: str
    states: list[PackageState]
    mom_available: bool


def run_ingest(team: str, db_path: Path | None = None) -> IngestResult:
    series = devel_series()
    log.info("ingesting team=%s series=%s", team, series)

    packages = sources.team_packages(team)
    log.info("%d packages subscribed by %s", len(packages), team)

    excuses_generated, excuses = sources.load_excuses()
    ubuntu_versions = sources.ubuntu_devel_versions()
    debian_versions = sources.debian_unstable_versions()
    mom = sources.mom_merges()

    states = derive_all(packages, excuses, ubuntu_versions, debian_versions, mom)

    conn = db.connect(db_path)
    run_id = db.record_run(
        conn, states, team=team, series=series,
        excuses_generated=excuses_generated)
    conn.close()

    return IngestResult(
        run_id=run_id, series=series, team=team,
        excuses_generated=excuses_generated, states=states,
        mom_available=bool(mom))
