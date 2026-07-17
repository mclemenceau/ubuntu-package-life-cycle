"""Ingest orchestration: fetch all sources, derive states, persist a run."""

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import db, lpbugs, sources
from .fetch import SourceUnavailable
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
    sru_available: bool


def run_ingest(team: str, db_path: Path | None = None) -> IngestResult:
    series = devel_series()
    log.info("ingesting team=%s series=%s", team, series)

    log.info("fetching team package list from Launchpad ...")
    try:
        packages = lpbugs.subscribed_packages(team)
    except SourceUnavailable as err:
        log.warning("Launchpad package list unavailable (%s); falling back "
                    "to package-team-mapping (frozen May 2025)", err)
        packages = sources.team_packages(team)
    log.info("%d packages subscribed by %s", len(packages), team)

    log.info("fetching + parsing proposed-migration excuses "
             "(largest source, may take a minute) ...")
    excuses_generated, excuses = sources.load_excuses()
    log.info("excuses for %d sources (generated %s)",
             len(excuses), excuses_generated)

    log.info("fetching Ubuntu devel Sources indexes ...")
    ubuntu_versions = sources.ubuntu_devel_versions()
    log.info("%d Ubuntu source versions", len(ubuntu_versions))

    log.info("fetching Debian unstable Sources index ...")
    debian_versions = sources.debian_unstable_versions()
    log.info("%d Debian source versions", len(debian_versions))

    log.info("fetching Merge-o-Matic reports ...")
    mom = sources.mom_merges()
    if mom:
        log.info("%d outstanding merges in Merge-o-Matic", len(mom))

    log.info("fetching pending-SRU report ...")
    team_packages = set(packages)
    sru_rows = [r for r in sources.pending_sru()
                if r["package"] in team_packages]
    if sru_rows:
        log.info("%d pending-SRU rows for team packages", len(sru_rows))

    log.info("deriving package states ...")
    states = derive_all(packages, excuses, ubuntu_versions, debian_versions, mom)

    log.info("recording snapshot ...")
    conn = db.connect(db_path)
    run_id = db.record_run(
        conn, states, team=team, series=series,
        excuses_generated=excuses_generated)
    db.record_pending_sru(conn, run_id, sru_rows)
    conn.close()

    return IngestResult(
        run_id=run_id, series=series, team=team,
        excuses_generated=excuses_generated, states=states,
        mom_available=bool(mom), sru_available=bool(sru_rows))
