"""Gentle Launchpad client — the only sanctioned LP API use.

One anonymous searchTasks per sync (structural_subscriber plus a
modified_since watermark) discovers which bugs changed; each of those is
then fetched by ID through the conditional-GET cache. Pipeline-referenced
bugs (block-proposed / update-excuse) are fetched by ID too. Never
per-package polling. The team's package list also comes from here (one
getBugSubscriberPackages call): the published package-team-mapping.json
fallback copy froze in May 2025, so LP is the only accurate source.

First sync is deliberately heavy — every bug touched since
FIRST_SYNC_SINCE, whatever its status — after that every run is a
watermark increment. Bugs dormant since before that date stay out of
scope even if still open: the team's Ubuntu backlog is tens of thousands
of untouched bugs, and a bug with no activity in the window is not
actionable signal for this dashboard.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import db
from .db import OPEN_BUG_STATUSES
from .fetch import USER_AGENT, SourceUnavailable, fetch

log = logging.getLogger("uplc.lpbugs")

LP_API = "https://api.launchpad.net/devel"
FIRST_SYNC_SINCE = "2026-01-01"
# Overlap re-scanned below the watermark so clock skew never loses an edit.
WATERMARK_OVERLAP = timedelta(hours=1)

CLOSED_BUG_STATUSES = (
    "Opinion", "Invalid", "Won't Fix", "Expired", "Fix Released",
)
ALL_BUG_STATUSES = OPEN_BUG_STATUSES + CLOSED_BUG_STATUSES

SEARCH_PAGE_DELAY = 2.0     # seconds between search result pages
BUG_FETCH_DELAY = 0.2       # seconds between by-ID bug fetches
MAX_BACKOFF = 60.0          # cap for our own exponential backoff
MAX_RETRY_AFTER = 120.0     # cap for a server-requested Retry-After


def _get_json(url: str, *, timeout: int = 60, retries: int = 6) -> dict:
    """GET a Launchpad API URL, backing off on 429/5xx.

    LP sheds load with routine 503s (often carrying a Retry-After header,
    which wins over our own backoff), so waiting it out is normal
    operation, not an error path.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    delay = 3.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise SourceUnavailable(f"{url}: HTTP {err.code}") from err
            wait = delay
            retry_after = err.headers.get("Retry-After") if err.headers else None
            if retry_after and retry_after.strip().isdigit():
                wait = min(float(retry_after), MAX_RETRY_AFTER)
            log.warning("HTTP %d from Launchpad, retrying in %.0fs",
                        err.code, wait)
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            if attempt == retries - 1:
                raise SourceUnavailable(f"{url}: {err}") from err
            wait = delay
            log.warning("Launchpad unreachable (%s), retrying in %.0fs",
                        err, wait)
        time.sleep(wait)
        delay = min(delay * 2, MAX_BACKOFF)
    raise SourceUnavailable(url)  # unreachable


def _cached_json(url: str) -> dict:
    """GET a stable LP resource through the conditional-GET cache."""
    return json.loads(fetch(url, timeout=30))


def bug_id_from_link(link: str) -> int | None:
    """https://api.launchpad.net/devel/bugs/2043210 -> 2043210"""
    tail = link.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def parse_target(target_link: str) -> tuple[str, str] | None:
    """Split a task target link into (package, series) within Ubuntu.

    .../devel/ubuntu                     -> ("", "")      distribution task
    .../devel/ubuntu/+source/glibc       -> ("glibc", "") devel task
    .../devel/ubuntu/noble/+source/glibc -> ("glibc", "noble")
    Non-Ubuntu targets (upstream projects, other distros) return None.
    """
    path = urllib.parse.urlparse(target_link).path.strip("/")
    parts = path.split("/")
    if len(parts) < 2 or parts[1] != "ubuntu":
        return None
    parts = parts[2:]  # drop the API version and "ubuntu"
    if not parts:
        return "", ""
    if parts[0] == "+source" and len(parts) == 2:
        return parts[1], ""
    if len(parts) == 3 and parts[1] == "+source":
        return parts[2], parts[0]
    if len(parts) == 1:
        return "", parts[0]  # series-wide task
    return None


def parse_task(entry: dict) -> dict | None:
    """One searchTasks/bug_tasks entry -> plain task dict, or None."""
    bug_id = bug_id_from_link(entry.get("bug_link") or "")
    target = parse_target(entry.get("target_link") or "")
    if bug_id is None or target is None:
        return None
    assignee = entry.get("assignee_link") or ""
    if assignee:
        assignee = assignee.rstrip("/").rsplit("/", 1)[-1].lstrip("~")
    return {
        "bug_id": bug_id,
        "package": target[0],
        "series": target[1],
        "status": entry.get("status", ""),
        "importance": entry.get("importance", ""),
        "assignee": assignee,
        "date_created": entry.get("date_created") or "",
        "date_closed": entry.get("date_closed") or "",
    }


def parse_bug(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "title": entry.get("title", ""),
        "tags": entry.get("tags", []),
        "date_created": entry.get("date_created") or "",
        "date_last_updated": entry.get("date_last_updated") or "",
        "heat": entry.get("heat", 0),
    }


def parse_subscriber_packages(entries: list[dict]) -> list[str]:
    """getBugSubscriberPackages entries -> sorted Ubuntu source packages."""
    packages = set()
    for entry in entries:
        target = parse_target(entry.get("self_link") or "")
        if target and target[0]:
            packages.add(target[0])
    return sorted(packages)


def subscribed_packages(team: str) -> list[str]:
    """Source packages *team* is a bug subscriber for, straight from LP.

    One paginated call per ingest, through the conditional-GET cache so
    an unreachable LP degrades to the last-fetched list.
    """
    url = f"{LP_API}/~{team}?" + urllib.parse.urlencode(
        [("ws.op", "getBugSubscriberPackages"), ("ws.size", "300")])
    entries: list[dict] = []
    while url:
        data = _cached_json(url)
        entries.extend(data.get("entries", []))
        url = data.get("next_collection_link")
    return parse_subscriber_packages(entries)


def search_tasks(
    team: str,
    *,
    statuses: tuple[str, ...],
    modified_since: str | None = None,
) -> tuple[list[dict], bool]:
    """All bug tasks structurally subscribed by *team*, paginated.

    Returns (entries, complete). A page that stays unreachable through the
    retries is a warning, not a failure: whatever was gathered still gets
    ingested, and the caller holds the watermark so the next sync
    re-covers the gap.
    """
    params = [
        ("ws.op", "searchTasks"),
        ("structural_subscriber", f"{LP_API}/~{team}"),
        ("ws.size", "300"),
    ]
    params += [("status", s) for s in statuses]
    if modified_since:
        params.append(("modified_since", modified_since))
    url = f"{LP_API}/ubuntu?" + urllib.parse.urlencode(params)

    entries: list[dict] = []
    while url:
        try:
            data = _get_json(url)
        except SourceUnavailable as err:
            log.warning("search interrupted (%s); continuing with the "
                        "%d tasks gathered so far", err, len(entries))
            return entries, False
        entries.extend(data.get("entries", []))
        log.info("search page: %d/%s tasks", len(entries),
                 data.get("total_size", "?"))
        url = data.get("next_collection_link")
        if url:
            time.sleep(SEARCH_PAGE_DELAY)
    return entries, True


def pipeline_bug_ids(conn, team: str) -> set[int]:
    """Bug numbers referenced by the latest pipeline snapshot."""
    run = db.latest_run(conn, team)
    if run is None:
        return set()
    ids: set[int] = set()
    for snap in db.snapshots_for_run(conn, run["id"]):
        detail = json.loads(snap["detail"] or "{}")
        ids.update(int(b) for b in detail.get("bugs", []) if str(b).isdigit())
    return ids


def _watermark_query_date(watermark: str) -> str:
    then = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
    return (then - WATERMARK_OVERLAP).isoformat()


@dataclass
class BugSyncResult:
    first_sync: bool
    bugs_synced: int = 0
    pipeline_fetched: int = 0
    watermark: str = ""
    failed: list = field(default_factory=list)  # bug ids we could not fetch
    search_complete: bool = True


def sync(conn, team: str) -> BugSyncResult:
    """Run one bug sync: search, fetch changed bugs by ID, upsert."""
    state = db.bug_sync_state(conn, team)
    result = BugSyncResult(first_sync=state is None)

    if state is None:
        log.info("first sync: every bug touched since %s, any status "
                 "(heavy, one-time)", FIRST_SYNC_SINCE)
        entries, result.search_complete = search_tasks(
            team, statuses=ALL_BUG_STATUSES,
            modified_since=FIRST_SYNC_SINCE)
    else:
        since = _watermark_query_date(state["watermark"])
        log.info("incremental sync: bugs modified since %s", since)
        entries, result.search_complete = search_tasks(
            team, statuses=ALL_BUG_STATUSES, modified_since=since)

    tasks_by_bug: dict[int, dict[tuple, dict]] = {}
    for entry in entries:
        task = parse_task(entry)
        if task:
            key = (task["package"], task["series"])
            tasks_by_bug.setdefault(task["bug_id"], {})[key] = task
    log.info("%d bugs to fetch (%d task rows)",
             len(tasks_by_bug), sum(len(t) for t in tasks_by_bug.values()))

    watermark = state["watermark"] if state else ""
    done = 0
    for bug_id, tasks in tasks_by_bug.items():
        try:
            bug = parse_bug(_cached_json(f"{LP_API}/bugs/{bug_id}"))
        except SourceUnavailable as err:
            log.warning("skipping bug %d: %s", bug_id, err)
            result.failed.append(bug_id)
            continue
        bug["origin"] = "subscription"
        db.record_bug(conn, bug, list(tasks.values()))
        conn.commit()
        if bug["date_last_updated"] > watermark:
            watermark = bug["date_last_updated"]
        done += 1
        if done % 100 == 0:
            log.info("fetched %d/%d bugs ...", done, len(tasks_by_bug))
        time.sleep(BUG_FETCH_DELAY)
    result.bugs_synced = done

    # Targeted fetches: bugs the pipeline references (block-proposed,
    # update-excuse) regardless of subscription or date. These do not move
    # the watermark — they are not part of the modified_since stream.
    for bug_id in sorted(pipeline_bug_ids(conn, team) - set(tasks_by_bug)):
        try:
            raw = _cached_json(f"{LP_API}/bugs/{bug_id}")
            task_entries = _cached_json(
                f"{LP_API}/bugs/{bug_id}/bug_tasks").get("entries", [])
        except SourceUnavailable as err:
            log.warning("skipping pipeline bug %d: %s", bug_id, err)
            result.failed.append(bug_id)
            continue
        bug = parse_bug(raw)
        bug["origin"] = "pipeline"
        tasks = [t for t in map(parse_task, task_entries) if t]
        db.record_bug(conn, bug, tasks)
        conn.commit()
        result.pipeline_fetched += 1
        time.sleep(BUG_FETCH_DELAY)

    # A failed fetch or an interrupted search means we may have missed
    # updates; holding the watermark keeps them inside the next run's
    # search window (or, on a first sync, re-runs the full sweep — cheap
    # via the cache).
    if result.failed or not result.search_complete:
        watermark = state["watermark"] if state is not None else ""
        log.warning("sync incomplete (%d fetch failures%s); "
                    "watermark held for retry",
                    len(result.failed),
                    "" if result.search_complete else ", search interrupted")
    if watermark:
        db.set_bug_sync_state(conn, team, watermark)
        conn.commit()
    result.watermark = watermark
    return result
