"""Self-contained HTML dashboard over the latest snapshot.

Static output: no external requests, inline CSS only, light/dark via
prefers-color-scheme. Regenerated after each ingest run.
"""

import html
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from . import db
from .report import STATE_LABELS, _age_str
from .sources import EXCUSES_HTML
from .state import BLOCKED_STATES, PROPOSED_STATES, STATES

# Status roles (icon+label always accompany the color).
_STATE_STATUS = {
    "blocked-build": "critical",
    "blocked-tests": "critical",
    "blocked-depends": "serious",
    "blocked-other": "serious",
    "waiting-age": "warning",
    "ready-to-migrate": "good",
}

_CSS = """
:root { color-scheme: light dark; }
.uplc {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  --bar: #2a78d6;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page); color: var(--ink);
  margin: 0; padding: 24px; min-height: 100vh;
}
@media (prefers-color-scheme: dark) {
  .uplc {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10); --bar: #3987e5;
  }
}
.uplc .wrap { max-width: 1100px; margin: 0 auto; }
.uplc h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
.uplc .meta { color: var(--ink-2); font-size: 13px; margin-bottom: 20px; }
.uplc h2 { font-size: 15px; font-weight: 600; margin: 28px 0 10px; }
.uplc .card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px;
}
.uplc .tiles {
  display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}
.uplc .tile { background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px; }
.uplc .tile .label { font-size: 12.5px; color: var(--ink-2); }
.uplc .tile .value { font-size: 30px; font-weight: 600; margin-top: 2px; }
.uplc .tile .hint { font-size: 12px; color: var(--muted); }
.uplc .funnel { display: grid; grid-template-columns: max-content 1fr;
  gap: 6px 12px; align-items: center; }
.uplc .funnel .name { font-size: 13px; color: var(--ink-2);
  text-align: right; white-space: nowrap; }
.uplc .funnel .track { position: relative; height: 20px; }
.uplc .funnel .fill { height: 20px; background: var(--bar);
  border-radius: 0 4px 4px 0; display: inline-block; vertical-align: top; }
.uplc .funnel .val { font-size: 13px; color: var(--ink);
  margin-left: 8px; vertical-align: top; line-height: 20px; }
.uplc .scroll { overflow-x: auto; }
.uplc table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
.uplc th { text-align: left; color: var(--muted); font-weight: 500;
  font-size: 12px; padding: 6px 12px 6px 0; border-bottom: 1px solid var(--grid); }
.uplc td { padding: 7px 12px 7px 0; border-bottom: 1px solid var(--grid);
  vertical-align: top; }
.uplc tr:last-child td { border-bottom: none; }
.uplc td.num { font-variant-numeric: tabular-nums; }
.uplc .pkg { font-weight: 600; white-space: nowrap; }
.uplc .pkg a { color: inherit; text-decoration: none;
  border-bottom: 1px dotted var(--muted); }
.uplc .chip { white-space: nowrap; font-size: 12.5px; color: var(--ink-2); }
.uplc .dot { display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 6px; }
.uplc .ver { font-family: ui-monospace, monospace; font-size: 12px;
  color: var(--ink-2); }
.uplc .why { color: var(--ink-2); max-width: 420px; }
.uplc .empty { color: var(--muted); font-size: 13.5px; }
.uplc footer { margin-top: 28px; color: var(--muted); font-size: 12px; }
"""


def _e(text) -> str:
    return html.escape(str(text or ""))


def _tile(label: str, value, hint: str = "") -> str:
    hint_html = f'<div class="hint">{_e(hint)}</div>' if hint else ""
    return (f'<div class="tile"><div class="label">{_e(label)}</div>'
            f'<div class="value">{value}</div>{hint_html}</div>')


def _chip(state: str) -> str:
    status = _STATE_STATUS.get(state)
    dot = (f'<span class="dot" style="background:var(--{status})"></span>'
           if status else "")
    return f'<span class="chip">{dot}{_e(STATE_LABELS[state])}</span>'


def _pkg_link(package: str, in_proposed: bool) -> str:
    if in_proposed:
        return (f'<a href="{EXCUSES_HTML}#{_e(package)}">{_e(package)}</a>')
    return _e(package)


def _funnel(counts: Counter) -> str:
    rows = [(STATE_LABELS[s], counts[s]) for s in STATES if counts.get(s)]
    if not rows:
        return '<p class="empty">no data</p>'
    peak = max(n for _, n in rows)
    cells = []
    for name, n in rows:
        width = max(2.0, 100.0 * n / peak)
        cells.append(f'<div class="name">{_e(name)}</div>')
        cells.append(
            f'<div class="track"><span class="fill" style="width:{width:.1f}%">'
            f'</span><span class="val">{n}</span></div>')
    return f'<div class="card funnel">{"".join(cells)}</div>'


def _pipeline_table(conn: sqlite3.Connection, snaps) -> str:
    rows = []
    for s in sorted(snaps, key=lambda r: (STATES.index(r["state"]), r["package"])):
        if s["state"] not in PROPOSED_STATES:
            continue
        detail = json.loads(s["detail"] or "{}")
        bugs = " ".join(
            f'<a href="https://launchpad.net/bugs/{_e(b)}">#{_e(b)}</a>'
            for b in detail.get("bugs", []))
        rows.append(
            "<tr>"
            f'<td class="pkg">{_pkg_link(s["package"], True)}</td>'
            f"<td>{_chip(s['state'])}</td>"
            f'<td class="ver">{_e(s["ubuntu_version"])} → '
            f'{_e(s["proposed_version"])}</td>'
            f'<td class="num">{_e(_age_str(db.state_entered_at(conn, s["package"])))}</td>'
            f'<td class="why">{_e(s["summary"])}{" · " + bugs if bugs else ""}</td>'
            "</tr>")
    if not rows:
        return '<p class="empty">nothing in -proposed right now</p>'
    return ('<div class="card scroll"><table>'
            "<tr><th>Package</th><th>State</th><th>Version</th>"
            "<th>Seen</th><th>Why</th></tr>"
            + "".join(rows) + "</table></div>")


def _delta_table(snaps) -> str:
    rows = []
    for s in snaps:
        if s["state"] not in ("merge-needed", "sync-available"):
            continue
        rows.append(
            "<tr>"
            f'<td class="pkg">{_e(s["package"])}</td>'
            f"<td>{_e(STATE_LABELS[s['state']])}</td>"
            f'<td class="ver">{_e(s["ubuntu_version"])}</td>'
            f'<td class="ver">{_e(s["debian_version"])}</td>'
            "</tr>")
    if not rows:
        return '<p class="empty">everything is current vs Debian unstable</p>'
    return ('<div class="card scroll"><table>'
            "<tr><th>Package</th><th>Action</th><th>Ubuntu devel</th>"
            "<th>Debian unstable</th></tr>"
            + "".join(rows) + "</table></div>")


def _blockers_table(snaps) -> str:
    fan: Counter = Counter()
    victims: dict[str, list[str]] = {}
    for s in snaps:
        for blocker in json.loads(s["detail"] or "{}").get("blocked_by", []):
            fan[blocker] += 1
            victims.setdefault(blocker, []).append(s["package"])
    if not fan:
        return ""
    rows = "".join(
        f'<tr><td class="pkg">{_e(b)}</td><td class="num">{n}</td>'
        f'<td class="why">{_e(", ".join(sorted(victims[b])))}</td></tr>'
        for b, n in fan.most_common(10))
    return ("<h2>Biggest unblock opportunities</h2>"
            '<div class="card scroll"><table>'
            "<tr><th>Blocking item</th><th>Blocks</th><th>Team packages</th></tr>"
            + rows + "</table></div>")


def render(conn: sqlite3.Connection, team: str) -> str:
    run = db.latest_run(conn, team)
    if run is None:
        raise SystemExit(f"no data for team {team!r} — run `uplc ingest` first")
    snaps = db.snapshots_for_run(conn, run["id"])
    counts = Counter(s["state"] for s in snaps)

    blocked = sum(counts.get(s, 0) for s in BLOCKED_STATES)
    in_proposed = sum(counts.get(s, 0) for s in PROPOSED_STATES)
    behind = counts.get("merge-needed", 0) + counts.get("sync-available", 0)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    tiles = "".join([
        _tile("Team packages", run["package_count"]),
        _tile("In -proposed", in_proposed, f"{blocked} blocked"),
        _tile("Blocked", blocked, "need attention"),
        _tile("Behind Debian", behind,
              f"{counts.get('merge-needed', 0)} merges, "
              f"{counts.get('sync-available', 0)} syncs"),
        _tile("In sync", counts.get("in-sync", 0) + counts.get("ubuntu-only", 0),
              "incl. Ubuntu-only"),
    ])

    return f"""<title>Package life cycle — {_e(team)}</title>
<style>{_CSS}</style>
<div class="uplc"><div class="wrap">
<h1>Ubuntu package life cycle — {_e(team)}</h1>
<div class="meta">{_e(run['series'])} series · snapshot {_e(run['ran_at'])}
 · excuses generated {_e(run['excuses_generated'])}</div>
<div class="tiles">{tiles}</div>
<h2>Funnel</h2>
{_funnel(counts)}
<h2>In proposed-migration ({in_proposed})</h2>
{_pipeline_table(conn, snaps)}
<h2>Behind Debian ({behind})</h2>
{_delta_table(snaps)}
{_blockers_table(snaps)}
<footer>Generated {_e(generated)} by uplc from bulk archive reports
 (update_excuses, package-team-mapping, Sources indexes) — no Launchpad API
 calls. "Seen" ages are measured from local observation history and mature as
 ingest runs accumulate.</footer>
</div></div>
"""
