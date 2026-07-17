"""Daily curated digest: team-bug changes + pipeline lifecycle events.

Compute is pure (parsers, event derivation, facts building, fallback
rendering); network is confined to `fetch_bug_extras` which goes through
`lpbugs._cached_json` (conditional-GET cache, a handful of by-ID GETs per
touched bug — never searches, never per-package polling); persistence in
db.py. The narrative step shells out to an LLM command (`claude -p` by
default, any runner honouring the contract below); when it fails or is
disabled, a deterministic rendering of the same facts is emitted instead,
so a cron run always produces a digest.

LLM runner contract: invoked as `<cmd> <prompt>` with the facts JSON on
stdin, prints Markdown on stdout, exits 0. Configured via --llm-cmd /
$UPLC_LLM_CMD; uplc knows nothing about which model runs.
"""

import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db, lpbugs
from .state import PROPOSED_STATES

log = logging.getLogger("uplc.digest")

BUG_URL = "https://bugs.launchpad.net/bugs/{id}"
MAX_MESSAGES = 6            # recent-comment fetch window per bug
MAX_ACTIVITY_PAGES = 10     # a years-old bug's activity log is finite anyway
MESSAGE_TRUNCATE = 1500     # chars of comment body shown to the LLM
ACTIVITY_TRUNCATE = 300     # chars per activity old/new value (description
                            # edits carry whole apport reports otherwise)

DEFAULT_LLM_CMD = "claude -p"
DEFAULT_LLM_TIMEOUT = 300.0

MERGE_STATES = {"merge-needed", "sync-available"}


# ---------------------------------------------------------------------------
# pure parsers (LP-shaped dicts in, plain dicts out)

def person_from_link(link: str) -> str:
    """https://api.launchpad.net/devel/~seb128 -> seb128"""
    return link.rstrip("/").rsplit("/", 1)[-1].lstrip("~") if link else ""


def parse_message(entry: dict) -> dict:
    return {
        "owner": person_from_link(entry.get("owner_link") or ""),
        "date": entry.get("date_created") or "",
        "subject": entry.get("subject") or "",
        "content": entry.get("content") or "",
    }


def parse_activity(entry: dict) -> dict:
    return {
        "person": person_from_link(entry.get("person_link") or ""),
        "date": entry.get("datechanged") or "",
        "whatchanged": entry.get("whatchanged") or "",
        "oldvalue": entry.get("oldvalue") or "",
        "newvalue": entry.get("newvalue") or "",
    }


def parse_bug_stats(entry: dict) -> dict:
    return {
        "message_count": entry.get("message_count", 0),
        "users_affected": entry.get("users_affected_count", 0),
        "duplicates": entry.get("number_of_duplicates", 0),
    }


def _norm(iso: str) -> str:
    return iso.replace("Z", "+00:00")


def in_window(iso: str, since: str) -> bool:
    """All timestamps are UTC ISO-8601, so string order is time order."""
    return bool(iso) and _norm(iso) >= _norm(since)


def closed_in_window(activity: list[dict], since: str) -> bool:
    return any(
        a["whatchanged"].endswith(": status")
        and a["newvalue"] in lpbugs.CLOSED_BUG_STATUSES
        and in_window(a["date"], since)
        for a in activity)


# whatchanged looks like "flashrom (Ubuntu Noble): status"
_SERIES_TASK = re.compile(r"^(\S+) \(Ubuntu (\w+)\): status$")


def sru_events(bug_facts: list[dict], since: str) -> list[dict]:
    """Stable-series task status changes = observable SRU activity.

    Approximation until a pending-sru.json source lands: a status change
    on a series task is the only SRU signal the bug stream carries.
    """
    events = []
    for bug in bug_facts:
        for act in bug["activity"]:
            m = _SERIES_TASK.match(act["whatchanged"])
            if not m or not in_window(act["date"], since):
                continue
            package, series = m.group(1), m.group(2).lower()
            events.append({
                "package": package,
                "kind": "sru",
                "detail": (f"{series} task: {act['oldvalue']} → "
                           f"{act['newvalue']} (bug #{bug['id']})"),
            })
    return events


def pipeline_events(
    prev_snapshots: list[dict],
    cur_snapshots: list[dict],
    transitions: list[dict],
) -> list[dict]:
    """Lifecycle events between two snapshot runs, plus window transitions.

    Timestamps are bounded by ingest cadence (transitions carry
    last_seen_old/first_seen_new); details say what changed between the
    runs, never pretend to exact times.
    """
    prev = {s["package"]: s for s in prev_snapshots}
    events: list[dict] = []

    def add(package, kind, detail, **extra):
        events.append({"package": package, "kind": kind, "detail": detail,
                       **extra})

    for cur in cur_snapshots:
        old = prev.get(cur["package"])
        if old is None:
            continue
        if cur["proposed_version"] and \
                cur["proposed_version"] != old["proposed_version"]:
            add(cur["package"], "upload",
                f"{cur['proposed_version']} uploaded to proposed",
                old_version=old["proposed_version"],
                new_version=cur["proposed_version"])
        if cur["ubuntu_version"] != old["ubuntu_version"]:
            if cur["ubuntu_version"] == old["proposed_version"]:
                add(cur["package"], "migrated",
                    f"{cur['ubuntu_version']} migrated to the release pocket"
                    f" (was {old['ubuntu_version']})",
                    old_version=old["ubuntu_version"],
                    new_version=cur["ubuntu_version"])
            else:
                add(cur["package"], "upload",
                    f"{cur['ubuntu_version']} landed in the release pocket"
                    f" (was {old['ubuntu_version']})",
                    old_version=old["ubuntu_version"],
                    new_version=cur["ubuntu_version"])
        old_builds = set(old["detail"].get("missing_builds", []))
        new_builds = set(cur["detail"].get("missing_builds", []))
        if new_builds - old_builds:
            add(cur["package"], "ftbfs",
                "build failures in proposed: "
                + ", ".join(sorted(new_builds - old_builds)))
        old_regr = set(old["detail"].get("regressions", []))
        new_regr = set(cur["detail"].get("regressions", []))
        if new_regr - old_regr:
            add(cur["package"], "excuse-regression",
                "new autopkgtest regressions: "
                + ", ".join(sorted(new_regr - old_regr)))
        if (old_regr or old_builds) and not (new_regr or new_builds):
            add(cur["package"], "unblocked",
                "autopkgtest regressions and build failures cleared")

    seen = {(e["package"], e["kind"]) for e in events}
    for tr in transitions:
        frm, to = tr["from_state"], tr["to_state"]
        if frm in MERGE_STATES and to not in MERGE_STATES:
            kind, detail = "merged", (
                f"Debian delta resolved ({frm} → {to})")
        elif to in MERGE_STATES:
            kind, detail = "merge-needed", (
                f"new Debian upload opened a delta ({frm} → {to})")
        elif to == "blocked-build":
            kind, detail = "ftbfs", f"entered blocked-build (was {frm})"
        elif frm in PROPOSED_STATES and to in ("in-sync", "ubuntu-only"):
            kind, detail = "migrated", f"left proposed ({frm} → {to})"
        else:
            kind, detail = "state-change", f"{frm} → {to}"
        if (tr["package"], kind) in seen:
            continue
        seen.add((tr["package"], kind))
        add(tr["package"], kind, detail, from_state=frm, to_state=to)

    return sorted(events, key=lambda e: (e["package"], e["kind"]))


# ---------------------------------------------------------------------------
# network: targeted by-ID fetches through the conditional-GET cache

def fetch_bug_extras(bug_id: int) -> dict:
    """Recent comments + full activity for one bug (~3 cached GETs).

    Raises SourceUnavailable only if the bug entry itself is unfetchable;
    messages/activity failures degrade to empty lists.
    """
    entry = lpbugs._cached_json(f"{lpbugs.LP_API}/bugs/{bug_id}")
    stats = parse_bug_stats(entry)

    messages: list[dict] = []
    start = max(0, stats["message_count"] - MAX_MESSAGES)
    try:
        page = lpbugs._cached_json(
            f"{lpbugs.LP_API}/bugs/{bug_id}/messages"
            f"?ws.start={start}&ws.size={MAX_MESSAGES}")
        messages = [parse_message(m) for m in page.get("entries", [])]
    except lpbugs.SourceUnavailable as err:
        log.warning("bug %d: messages unavailable (%s)", bug_id, err)

    activity: list[dict] = []
    url = f"{lpbugs.LP_API}/bugs/{bug_id}/activity"
    try:
        for _ in range(MAX_ACTIVITY_PAGES):
            page = lpbugs._cached_json(url)
            activity.extend(
                parse_activity(a) for a in page.get("entries", []))
            url = page.get("next_collection_link")
            if not url:
                break
    except lpbugs.SourceUnavailable as err:
        log.warning("bug %d: activity unavailable (%s)", bug_id, err)

    return {"stats": stats, "messages": messages, "activity": activity}


# ---------------------------------------------------------------------------
# pure facts building

def _bug_facts(bug_rows: list[dict], extras: dict, since: str) -> dict:
    """One bug's task rows (same bug id) + its extras -> facts entry."""
    first = bug_rows[0]
    stats = extras.get("stats", {})
    activity = [
        {**a, "oldvalue": a["oldvalue"][:ACTIVITY_TRUNCATE],
         "newvalue": a["newvalue"][:ACTIVITY_TRUNCATE]}
        for a in extras.get("activity", [])
        if in_window(a["date"], since)]
    messages = [
        {**m, "content": m["content"][:MESSAGE_TRUNCATE]}
        for m in extras.get("messages", [])
        if in_window(m["date"], since)]
    tasks = [{
        "package": r["package"],
        "series": r["series"],
        "status": r["status"],
        "importance": r["importance"],
        "assignee": r["assignee"],
        "date_closed": r["date_closed"] or "",
    } for r in bug_rows]
    return {
        "id": first["id"],
        "title": first["title"],
        "url": BUG_URL.format(id=first["id"]),
        "tags": json.loads(first["tags"] or "[]"),
        "heat": first["heat"],
        "users_affected": stats.get("users_affected", 0),
        "duplicates": stats.get("duplicates", 0),
        "message_count": stats.get("message_count", 0),
        "date_created": first["date_created"],
        "date_last_updated": first["date_last_updated"],
        "is_new_today": in_window(first["date_created"], since),
        "is_closed_today": (
            closed_in_window(activity, since)
            or any(in_window(t["date_closed"], since) for t in tasks)),
        "tasks": tasks,
        "activity": activity,
        "messages": messages,
        "pipeline": [],
    }


def _pipeline_ref(snap: dict) -> dict:
    return {
        "package": snap["package"],
        "state": snap["state"],
        "summary": snap["summary"],
        "ubuntu_version": snap["ubuntu_version"],
        "debian_version": snap["debian_version"],
        "proposed_version": snap["proposed_version"],
        "age_days": snap["age_days"],
        "blocked_by": snap["detail"].get("blocked_by", []),
        "regressions": snap["detail"].get("regressions", []),
    }


def build_facts(
    bug_rows: list[dict],
    extras: dict[int, dict],
    snapshot_rows: list[dict],
    prev_snapshot_rows: list[dict],
    transitions: list[dict],
    run_row: dict | None,
    sync_row: dict | None,
    *,
    team: str,
    since: str,
    until: str,
    date: str,
) -> dict:
    by_bug: dict[int, list[dict]] = {}
    for row in bug_rows:
        by_bug.setdefault(row["id"], []).append(row)

    bugs = [_bug_facts(rows, extras.get(bug_id, {}), since)
            for bug_id, rows in sorted(by_bug.items())]

    snaps = {s["package"]: s for s in snapshot_rows}
    for bug in bugs:
        bug["pipeline"] = [
            _pipeline_ref(snaps[t["package"]])
            for t in bug["tasks"]
            if t["package"] in snaps]

    events = pipeline_events(prev_snapshot_rows, snapshot_rows, transitions)
    events += sru_events(bugs, since)

    packages = {t["package"] for b in bugs for t in b["tasks"]
                if t["package"]}
    open_unassigned = sum(
        1 for b in bugs
        if any(t["status"] in db.OPEN_BUG_STATUSES for t in b["tasks"])
        and not any(t["assignee"] for t in b["tasks"]))
    return {
        "digest": {
            "team": team,
            "date": date,
            "human_date": _human_date(date),
            "since": since,
            "until": until,
            "bug_count": len(bugs),
            "package_count": len(packages),
            "new_today": sum(b["is_new_today"] for b in bugs),
            "closed_today": sum(b["is_closed_today"] for b in bugs),
            "critical": sum(
                any(t["importance"] == "Critical" for t in b["tasks"])
                for b in bugs),
            "unassigned": open_unassigned,
            "event_count": len(events),
            "bug_sync": {
                "watermark": sync_row["watermark"] if sync_row else "",
                "last_synced": sync_row["last_synced"] if sync_row else "",
            },
            "pipeline_run": {
                "id": run_row["id"] if run_row else None,
                "ran_at": run_row["ran_at"] if run_row else "",
                "series": run_row["series"] if run_row else "",
            },
        },
        "bugs": bugs,
        "pipeline_events": events,
    }


# ---------------------------------------------------------------------------
# deterministic rendering (the no-LLM / fallback digest)

def _human_date(date: str) -> str:
    dt = datetime.strptime(date, "%Y-%m-%d")
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B %Y')}"


def _scope_line(d: dict) -> str:
    run = d["pipeline_run"]
    line = (f"*Scope: bugs with `~{d['team']}` structural subscription "
            f"modified {d['since']} → {d['until']}.")
    if run["id"] is not None:
        line += (f" Pipeline cross-references from uplc snapshot run "
                 f"{run['id']} ({run['ran_at']}, devel series: "
                 f"{run['series']}).")
    return line + "*"


def _bug_heading(bug: dict) -> str:
    packages = sorted({t["package"] for t in bug["tasks"] if t["package"]})
    label = ", ".join(packages) or "ubuntu"
    return f"### [#{bug['id']}]({bug['url']}) {label} — {bug['title']}"


def _bug_block(bug: dict) -> list[str]:
    lines = [_bug_heading(bug), ""]
    facts = []
    for t in bug["tasks"]:
        where = f"{t['package']}/{t['series']}" if t["series"] else t["package"]
        bits = f"{t['importance']} · {t['status']}"
        bits += f" · {t['assignee']}" if t["assignee"] else " · unassigned"
        facts.append(f"{where or 'ubuntu'}: {bits}")
    lines += ["**" + " | ".join(facts) + "**", ""]
    for act in bug["activity"]:
        lines.append(f"- {act['person']}: {act['whatchanged']}: "
                     f"{act['oldvalue']} → {act['newvalue']}")
    for msg in bug["messages"]:
        quote = " ".join(msg["content"].split())
        if len(quote) > 300:
            quote = quote[:300] + "…"
        lines.append(f"- comment by {msg['owner']}: “{quote}”")
    for ref in bug["pipeline"]:
        note = f"pipeline: {ref['package']} is {ref['state']}"
        if ref["age_days"]:
            note += f" ({ref['age_days']:.0f} days)"
        lines.append(f"- {note}")
    lines.append("")
    return lines


def render_fallback(facts: dict) -> str:
    d = facts["digest"]
    bugs = facts["bugs"]
    events = facts["pipeline_events"]

    out = [f"# 🐛 {d['team']} bugs digest — {_human_date(d['date'])}", ""]
    out += [f"**{d['bug_count']} bugs touched** across "
            f"{d['package_count']} packages · {d['new_today']} filed · "
            f"{d['closed_today']} closed · {d['event_count']} pipeline "
            f"events", ""]
    out += [_scope_line(d), "", "---", ""]

    if events:
        out += ["## Pipeline", ""]
        for ev in events:
            out.append(f"- **{ev['package']}** ({ev['kind']}): "
                       f"{ev['detail']}")
        out.append("")

    groups = [
        ("New bugs", [b for b in bugs if b["is_new_today"]]),
        ("Closed", [b for b in bugs
                    if b["is_closed_today"] and not b["is_new_today"]]),
        ("Activity", [b for b in bugs
                      if not b["is_new_today"] and not b["is_closed_today"]]),
    ]
    for title, group in groups:
        if not group:
            continue
        out += [f"## {title}", ""]
        for bug in group:
            out += _bug_block(bug)

    if bugs:
        out += ["## Summary", "",
                "| Bug | Package | Importance | Status | Assignee |",
                "|---|---|---|---|---|"]
        for bug in bugs:
            t = bug["tasks"][0] if bug["tasks"] else {}
            packages = ", ".join(
                sorted({x["package"] for x in bug["tasks"] if x["package"]}))
            out.append(
                f"| [#{bug['id']}]({bug['url']}) | {packages} "
                f"| {t.get('importance', '')} | {t.get('status', '')} "
                f"| {t.get('assignee') or '—'} |")
        out.append("")
    elif not events:
        out += ["No team bug activity and no pipeline events in this "
                "window.", ""]

    out += ["---", "",
            "*Deterministic digest rendered from the local uplc database "
            "(LLM narrative unavailable or disabled).*"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# LLM narrative

PROMPT = """\
You are writing the daily bugs digest for an Ubuntu Foundations engineering
manager. stdin carries a JSON facts document: `digest` (counts and scope),
`bugs` (each with tasks, window-filtered `activity` and `messages`, and
`pipeline` cross-references), and `pipeline_events` (package lifecycle:
uploads, migrations, merges, FTBFS, autopkgtest regressions, SRU task
changes — each with a ground-truth `detail` string).

Write a skimmable, decision-oriented Markdown digest:

- `# 🐛 {team} bugs digest — {digest.human_date}` (verbatim — do not
  compute the weekday yourself)
- a bold one-line stats summary, then an italic scope line built from
  digest.since/until and digest.pipeline_run
- if three or more bugs/events share a package or cause, open with a
  "Theme of the day" section
- `## Pipeline` — the lifecycle events worth a manager's attention,
  grouped sensibly (migrations/merges as wins, FTBFS/regressions as new
  blockers); skip noise
- `## Needs attention` — bugs needing a decision or an owner
- `## Moving` — bugs with real progress today
- `## Closed / closable` — closed today, or where facts say closing is the
  action
- `## New information` — new bugs or new context
- `## Takeaway` — 2-4 sentences: the single biggest unblock opportunity
  and where to press
- a summary table `| Bug | Package | Importance | Status | Assignee |
  Today |` listing EVERY bug in the facts
- an italic one-line generation footer

Omit any section with nothing to say. Hard rules:
- cite ONLY bug numbers present in the facts; link them as
  [#NNN](https://bugs.launchpad.net/bugs/NNN)
- quote people only from messages[].content, attributed to messages[].owner
- for pipeline events, treat their `detail` strings as ground truth; SRU
  events are approximations from series-task changes — do not overclaim
- never invent facts, links, versions, or people; timestamps between
  ingest runs are approximate — say "between runs", not exact times
- output raw Markdown only: no preamble, no code fence around the document

Skeleton to match (tone and structure, not content):

# 🐛 foundations-bugs digest — Wednesday 16 July 2026
**6 bugs touched today** across 5 packages · 1 Critical · 3 still unassigned
*Scope: bugs modified since … Pipeline cross-references from run 4 (…).*
---
## Needs attention
### [#2160614](…) rust-coreutils — `id`: fakeroot does not fake root
**Critical · Triaged · assigned (bamf0)**
Active investigation today: … No fix direction settled yet.
## Moving
### [#2160111](…) livecd-rootfs — debootstrap fails on stonking
Overnight, … linked a merge proposal and took the task In Progress.
## Takeaway
The … breakage is being handled; the gap is … — two unassigned upgrade
failures, one two months old with users pinging for help.
| Bug | Package | Importance | Status | Assignee | Today |
|---|---|---|---|---|---|
| [#2160614](…) | rust-coreutils | **Critical** | Triaged | bamf0 | investigation comment |
"""


def build_prompt(facts: dict) -> str:
    d = facts["digest"]
    return PROMPT + (f"\nToday: team={d['team']}, date={d['date']}, "
                     f"{d['bug_count']} bugs, {d['event_count']} events.\n")


def cited_bug_ids(markdown: str) -> set[int]:
    ids = {int(m) for m in re.findall(r"#(\d{5,8})", markdown)}
    ids |= {int(m) for m in re.findall(r"/\+?bugs?/(\d+)", markdown)}
    return ids


def validate_output(markdown: str, facts: dict) -> list[str]:
    """Violations that force the deterministic fallback ([] = ok)."""
    problems = []
    text = markdown.strip()
    if not text:
        return ["empty output"]
    if not text.startswith("#"):
        problems.append("does not start with a Markdown heading")
    known = {b["id"] for b in facts["bugs"]}
    invented = cited_bug_ids(text) - known
    if invented:
        problems.append(
            "cites bug ids absent from the facts: "
            + ", ".join(str(i) for i in sorted(invented)))
    return problems


def run_llm(
    facts: dict,
    *,
    cmd: str | None = None,
    timeout: float | None = None,
) -> str | None:
    """Run the narrative command; None on any failure (never raises)."""
    cmd = cmd or os.environ.get("UPLC_LLM_CMD") or DEFAULT_LLM_CMD
    if timeout is None:
        timeout = float(os.environ.get("UPLC_LLM_TIMEOUT",
                                       DEFAULT_LLM_TIMEOUT))
    argv = shlex.split(cmd) + [build_prompt(facts)]
    log.info("running LLM narrative: %s (timeout %.0fs)", cmd, timeout)
    try:
        proc = subprocess.run(
            argv, input=json.dumps(facts), capture_output=True,
            text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as err:
        log.warning("LLM command failed: %s", err)
        return None
    if proc.returncode != 0:
        log.warning("LLM command exited %d: %s", proc.returncode,
                    proc.stderr.strip()[:500])
        return None
    return proc.stdout.strip() or None


# ---------------------------------------------------------------------------
# orchestration

@dataclass
class DigestResult:
    date: str
    since: str
    until: str
    bug_count: int
    used_llm: bool
    body: str


def _snap_dict(row) -> dict:
    d = dict(row)
    d["detail"] = json.loads(d.get("detail") or "{}")
    return d


def generate(
    conn,
    team: str,
    *,
    since: str | None = None,
    no_llm: bool = False,
    llm_cmd: str | None = None,
) -> DigestResult:
    sync_row = db.bug_sync_state(conn, team)
    if sync_row is None:
        raise SystemExit("no bug data for team — run `uplc bugs-sync` first")
    until = sync_row["watermark"]
    if since is None:
        prev_digest = db.latest_digest_run(conn, team)
        if prev_digest:
            since = prev_digest["until_iso"]
        else:
            since = time.strftime("%Y-%m-%dT00:00:00+00:00", time.gmtime())
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    bug_rows = [dict(r) for r in db.bugs_modified_since(conn, since)]
    bug_ids = sorted({r["id"] for r in bug_rows})
    log.info("digest window %s → %s: %d bugs", since, until, len(bug_ids))

    extras: dict[int, dict] = {}
    for bug_id in bug_ids:
        try:
            extras[bug_id] = fetch_bug_extras(bug_id)
        except lpbugs.SourceUnavailable as err:
            log.warning("bug %d: no extras (%s)", bug_id, err)
            extras[bug_id] = {}
        time.sleep(lpbugs.BUG_FETCH_DELAY)

    cur_run = db.latest_run(conn, team)
    snapshot_rows, prev_snapshot_rows, transitions = [], [], []
    if cur_run is not None:
        snapshot_rows = [
            _snap_dict(r) for r in db.snapshots_for_run(conn, cur_run["id"])]
        prev_run = db.run_at_or_before(conn, team, since)
        if prev_run is not None and prev_run["id"] != cur_run["id"]:
            prev_snapshot_rows = [
                _snap_dict(r)
                for r in db.snapshots_for_run(conn, prev_run["id"])]
            transitions = [dict(r) for r in db.transitions_between(
                conn, prev_run["ran_at"], cur_run["ran_at"])]

    facts = build_facts(
        bug_rows, extras, snapshot_rows, prev_snapshot_rows, transitions,
        dict(cur_run) if cur_run else None, dict(sync_row),
        team=team, since=since, until=until, date=date)

    body, used_llm = None, False
    if not no_llm:
        body = run_llm(facts, cmd=llm_cmd)
        if body is not None:
            problems = validate_output(body, facts)
            if problems:
                log.warning("LLM output rejected: %s; using fallback",
                            "; ".join(problems))
                body = None
    if body is None:
        body = render_fallback(facts)
    else:
        used_llm = True
        if not body.endswith("\n"):
            body += "\n"

    db.record_digest_run(
        conn, team=team, since_iso=since, until_iso=until, date=date,
        bug_count=len(bug_ids), used_llm=used_llm, body=body)
    return DigestResult(date=date, since=since, until=until,
                        bug_count=len(bug_ids), used_llm=used_llm, body=body)
