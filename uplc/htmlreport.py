"""Self-contained HTML dashboard site over the latest snapshot + bug data.

Three pages: index.html (manager overview), packages.html (all team
packages, filterable — lifecycle/roadmap lens) and bugs.html (bug
prioritization). Static output: no external requests, inline CSS, and a
small inline script per table page for client-side filtering/sorting —
without JS the pages degrade to full, unfiltered tables. Cross-links are
relative so the site works from file:// or any static host.

Link conventions: a package name anywhere goes to packages.html#pkg-<name>
(Launchpad/excuses/tracker out-links live on that row); a bug number
anywhere goes straight to Launchpad.
"""

import html
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from . import db
from .db import OPEN_BUG_STATUSES
from .lpbugs import FIRST_SYNC_SINCE
from .report import STATE_LABELS, _age_str
from .sources import EXCUSES_HTML
from .state import BLOCKED_STATES, PROPOSED_STATES, STATES

# Status roles (icon+label always accompany the color). Severity ladder:
# critical = fix it (red), serious = stuck on something (orange),
# warning = action available/pending (yellow), good = healthy (green);
# states with no entry are neutral (no action possible) and get a gray dot.
_STATE_STATUS = {
    "blocked-build": "critical",
    "blocked-tests": "critical",
    "blocked-depends": "serious",
    "blocked-other": "serious",
    "waiting-age": "warning",
    "merge-needed": "warning",
    "sync-available": "warning",
    "ready-to-migrate": "good",
    "in-sync": "good",
}

_IMPORTANCE_RANK = {
    "Critical": 5, "High": 4, "Medium": 3, "Low": 2, "Wishlist": 1,
}
_IMPORTANCE_STATUS = {"Critical": "critical", "High": "serious"}

_STALE_DAYS = 90

_CSS = """
:root { color-scheme: light dark; }
.uplc {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  --bar: #2a78d6; --bar2: #b45309;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page); color: var(--ink);
  margin: 0; padding: 24px; min-height: 100vh;
}
@media (prefers-color-scheme: dark) {
  .uplc {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --bar: #3987e5; --bar2: #d97706;
  }
}
.uplc .wrap { max-width: 1100px; margin: 0 auto; }
.uplc nav { font-size: 14px; margin-bottom: 18px; }
.uplc nav a { color: var(--ink-2); text-decoration: none; margin-right: 18px;
  padding-bottom: 3px; }
.uplc nav a.active { color: var(--ink); font-weight: 600;
  border-bottom: 2px solid var(--bar); }
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
.uplc .tile .value a { color: inherit; text-decoration: none; }
.uplc .tile .hint { font-size: 12px; color: var(--muted); }
.uplc .funnel { display: grid; grid-template-columns: max-content 1fr;
  gap: 6px 12px; align-items: center; }
.uplc .funnel .name { font-size: 13px; color: var(--ink-2);
  text-align: right; white-space: nowrap; }
.uplc .funnel .track { position: relative; height: 20px; white-space: nowrap; }
.uplc .funnel .fill { height: 20px; background: var(--bar);
  border-radius: 0 4px 4px 0; display: inline-block; vertical-align: top; }
.uplc .funnel .val { font-size: 13px; color: var(--ink);
  margin-left: 8px; vertical-align: top; line-height: 20px; }
.uplc .trend { display: grid; grid-template-columns: max-content 1fr;
  gap: 8px 12px; align-items: center; margin-top: 10px; }
.uplc .trend .name { font-size: 13px; color: var(--ink-2);
  text-align: right; white-space: nowrap; }
.uplc .trend .track { position: relative; height: 14px; white-space: nowrap; }
.uplc .trend .track + .track { margin-top: 2px; }
.uplc .trend .fill { height: 14px; background: var(--bar);
  border-radius: 0 4px 4px 0; display: inline-block; vertical-align: top; }
.uplc .trend .fill.f2 { background: var(--bar2); }
.uplc .trend .val { font-size: 12px; color: var(--ink);
  margin-left: 6px; vertical-align: top; line-height: 14px; }
.uplc .legend { display: flex; gap: 18px; font-size: 12.5px;
  color: var(--ink-2); }
.uplc .sw { display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; margin-right: 6px; }
.uplc .filters { display: flex; flex-wrap: wrap; gap: 8px 12px;
  align-items: center; margin: 0 0 12px; }
.uplc .filters input[type=search] { background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 5px 10px;
  color: var(--ink); font: inherit; font-size: 13px; min-width: 190px; }
.uplc .fchip { border: 1px solid var(--border); background: var(--surface);
  color: var(--ink-2); border-radius: 999px; padding: 3px 11px;
  font: inherit; font-size: 12.5px; cursor: pointer; }
.uplc .fchip.on { border-color: var(--bar); color: var(--ink);
  background: rgba(58,127,222,0.16); }
.uplc .flag { font-size: 12.5px; color: var(--ink-2); white-space: nowrap;
  display: inline-flex; gap: 5px; align-items: center; cursor: pointer; }
.uplc .fcount { margin-left: auto; color: var(--muted); font-size: 12.5px; }
.uplc .scroll { overflow-x: auto; }
.uplc table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
.uplc th { text-align: left; color: var(--muted); font-weight: 500;
  font-size: 12px; padding: 6px 12px 6px 0; border-bottom: 1px solid var(--grid); }
.uplc th.sort { cursor: pointer; user-select: none; white-space: nowrap; }
.uplc td { padding: 7px 12px 7px 0; border-bottom: 1px solid var(--grid);
  vertical-align: top; }
.uplc tr:last-child td { border-bottom: none; }
.uplc td.num { font-variant-numeric: tabular-nums; }
.uplc .pkg { font-weight: 600; white-space: nowrap; }
.uplc .pkg a { color: inherit; text-decoration: none;
  border-bottom: 1px dotted var(--muted); }
.uplc .chip { white-space: nowrap; font-size: 12.5px; color: var(--ink-2); }
.uplc .agehint { color: var(--muted); font-size: 11.5px; white-space: nowrap; }
.uplc td.bugs { white-space: nowrap; }
.uplc .dot { display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 6px; }
.uplc .ver { font-family: ui-monospace, monospace; font-size: 12px;
  color: var(--ink-2); overflow-wrap: anywhere; }
.uplc .why { color: var(--ink-2); max-width: 420px; }
.uplc .title { color: var(--ink); max-width: 460px; }
.uplc .tag { font-size: 11px; border: 1px solid var(--border);
  border-radius: 999px; padding: 1px 7px; color: var(--muted);
  margin-left: 6px; white-space: nowrap; }
.uplc tr.detail td { background: rgba(127,127,127,0.06); font-size: 13px;
  color: var(--ink-2); padding: 10px 12px; }
.uplc tr.detail .links a { color: var(--bar); text-decoration: none;
  margin-right: 16px; font-size: 12.5px; }
.uplc.js tbody.pkgrow tr.detail { display: none; }
.uplc.js tbody.pkgrow.open tr.detail,
.uplc tbody.pkgrow:target tr.detail { display: table-row; }
.uplc a.buglink, .uplc .cell a { color: var(--bar); text-decoration: none; }
.uplc .empty { color: var(--muted); font-size: 13.5px; }
.uplc footer { margin-top: 28px; color: var(--muted); font-size: 12px; }
"""

_PAGES_NAV = [
    ("index.html", "Overview"),
    ("packages.html", "Packages"),
    ("bugs.html", "Bugs"),
]

# --- Packages page script: filter/sort the tbody-per-package table. -------
_PKG_JS = """
(function () {
  var root = document.querySelector('.uplc');
  root.classList.add('js');
  var rows = Array.prototype.slice.call(
    document.querySelectorAll('tbody.pkgrow'));
  var table = document.getElementById('ptable');
  var q = document.getElementById('fq');
  var chips = Array.prototype.slice.call(
    document.querySelectorAll('.fchip[data-state]'));
  var flags = {
    bugs: document.getElementById('f-bugs'),
    high: document.getElementById('f-high'),
    stuck: document.getElementById('f-stuck')
  };

  function apply(updateHash) {
    var states = chips.filter(function (c) {
      return c.classList.contains('on');
    }).map(function (c) { return c.dataset.state; });
    var text = q.value.trim().toLowerCase();
    var n = 0;
    rows.forEach(function (r) {
      var show = true;
      if (states.length && states.indexOf(r.dataset.state) < 0) show = false;
      if (show && text && r.dataset.text.indexOf(text) < 0) show = false;
      if (show && flags.bugs.checked && +r.dataset.bugs === 0) show = false;
      if (show && flags.high.checked && +r.dataset.high === 0) show = false;
      if (show && flags.stuck.checked && +r.dataset.days < 7) show = false;
      r.style.display = show ? '' : 'none';
      if (show) n++;
    });
    document.getElementById('fcount').textContent =
      n + ' of ' + rows.length + ' shown';
    if (updateHash) {
      var parts = [];
      if (text) parts.push('q=' + encodeURIComponent(text));
      if (states.length) parts.push('s=' + states.join(','));
      if (flags.bugs.checked) parts.push('b=1');
      if (flags.high.checked) parts.push('h=1');
      if (flags.stuck.checked) parts.push('k=1');
      history.replaceState(null, '',
        parts.length ? '#' + parts.join('&') : location.pathname);
    }
  }

  q.addEventListener('input', function () { apply(true); });
  chips.forEach(function (c) {
    c.addEventListener('click', function () {
      c.classList.toggle('on'); apply(true);
    });
  });
  Object.keys(flags).forEach(function (k) {
    flags[k].addEventListener('change', function () { apply(true); });
  });

  rows.forEach(function (r) {
    var a = r.querySelector('.pkg a');
    if (a) a.addEventListener('click', function (ev) {
      ev.preventDefault(); r.classList.toggle('open');
    });
  });

  document.querySelectorAll('#ptable th.sort').forEach(function (th) {
    th.addEventListener('click', function () {
      var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
      document.querySelectorAll('#ptable th.sort').forEach(function (o) {
        delete o.dataset.dir;
        o.textContent = o.textContent.replace(/ [\\u25b2\\u25bc]$/, '');
      });
      th.dataset.dir = dir;
      th.textContent += dir === 'asc' ? ' \\u25b2' : ' \\u25bc';
      var mul = dir === 'asc' ? 1 : -1, key = th.dataset.key;
      var num = th.hasAttribute('data-num');
      rows.sort(function (a, b) {
        var x = a.dataset[key], y = b.dataset[key];
        if (num) { x = +x; y = +y; }
        return (x < y ? -1 : x > y ? 1 : 0) * mul;
      });
      rows.forEach(function (r) { table.appendChild(r); });
    });
  });

  var hash = decodeURIComponent(location.hash.slice(1));
  if (hash && hash.indexOf('=') >= 0) {
    hash.split('&').forEach(function (kv) {
      var k = kv.split('=')[0], v = kv.split('=').slice(1).join('=');
      if (k === 'q') q.value = decodeURIComponent(v);
      if (k === 's') chips.forEach(function (c) {
        if (v.split(',').indexOf(c.dataset.state) >= 0)
          c.classList.add('on');
      });
      if (k === 'b') flags.bugs.checked = true;
      if (k === 'h') flags.high.checked = true;
      if (k === 'k') flags.stuck.checked = true;
    });
  }
  apply(false);
})();
"""

# --- Bugs page script: same idea over one tr-per-bug table. ---------------
_BUGS_JS = """
(function () {
  var root = document.querySelector('.uplc');
  root.classList.add('js');
  var table = document.getElementById('btable');
  var rows = Array.prototype.slice.call(
    table.querySelectorAll('tbody tr'));
  var q = document.getElementById('fq');
  var chips = Array.prototype.slice.call(
    document.querySelectorAll('.fchip[data-imp]'));
  var flags = {
    unt: document.getElementById('f-unt'),
    una: document.getElementById('f-una'),
    stale: document.getElementById('f-stale'),
    gate: document.getElementById('f-gate')
  };

  function scope() {
    var r = document.querySelector('input[name=scope]:checked');
    return r ? r.value : 'open';
  }

  function apply(updateHash) {
    var imps = chips.filter(function (c) {
      return c.classList.contains('on');
    }).map(function (c) { return c.dataset.imp; });
    var text = q.value.trim().toLowerCase(), sc = scope();
    var n = 0;
    rows.forEach(function (r) {
      var show = true;
      if (sc === 'open' && r.dataset.open !== '1') show = false;
      if (sc === 'closed' && r.dataset.open === '1') show = false;
      if (show && imps.length && imps.indexOf(r.dataset.imp) < 0) show = false;
      if (show && text && r.dataset.text.indexOf(text) < 0) show = false;
      if (show && flags.unt.checked && r.dataset.unt !== '1') show = false;
      if (show && flags.una.checked && r.dataset.una !== '1') show = false;
      if (show && flags.stale.checked && +r.dataset.idle < 90) show = false;
      if (show && flags.gate.checked && r.dataset.gate !== '1') show = false;
      r.style.display = show ? '' : 'none';
      if (show) n++;
    });
    document.getElementById('fcount').textContent =
      n + ' of ' + rows.length + ' shown';
    if (updateHash) {
      var parts = [];
      if (text) parts.push('q=' + encodeURIComponent(text));
      if (sc !== 'open') parts.push('sc=' + sc);
      if (imps.length) parts.push('i=' + imps.join(','));
      if (flags.unt.checked) parts.push('u=1');
      if (flags.una.checked) parts.push('a=1');
      if (flags.stale.checked) parts.push('st=1');
      if (flags.gate.checked) parts.push('g=1');
      history.replaceState(null, '',
        parts.length ? '#' + parts.join('&') : location.pathname);
    }
  }

  q.addEventListener('input', function () { apply(true); });
  chips.forEach(function (c) {
    c.addEventListener('click', function () {
      c.classList.toggle('on'); apply(true);
    });
  });
  Object.keys(flags).forEach(function (k) {
    flags[k].addEventListener('change', function () { apply(true); });
  });
  document.querySelectorAll('input[name=scope]').forEach(function (r) {
    r.addEventListener('change', function () { apply(true); });
  });

  document.querySelectorAll('#btable th.sort').forEach(function (th) {
    th.addEventListener('click', function () {
      var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
      document.querySelectorAll('#btable th.sort').forEach(function (o) {
        delete o.dataset.dir;
        o.textContent = o.textContent.replace(/ [\\u25b2\\u25bc]$/, '');
      });
      th.dataset.dir = dir;
      th.textContent += dir === 'asc' ? ' \\u25b2' : ' \\u25bc';
      var mul = dir === 'asc' ? 1 : -1, key = th.dataset.key;
      var num = th.hasAttribute('data-num');
      rows.sort(function (a, b) {
        var x = a.dataset[key], y = b.dataset[key];
        if (num) { x = +x; y = +y; }
        return (x < y ? -1 : x > y ? 1 : 0) * mul;
      });
      var tbody = table.querySelector('tbody');
      rows.forEach(function (r) { tbody.appendChild(r); });
    });
  });

  var hash = decodeURIComponent(location.hash.slice(1));
  if (hash && hash.indexOf('=') >= 0) {
    hash.split('&').forEach(function (kv) {
      var k = kv.split('=')[0], v = kv.split('=').slice(1).join('=');
      if (k === 'q') q.value = decodeURIComponent(v);
      if (k === 'sc') {
        var r = document.querySelector('input[name=scope][value=' + v + ']');
        if (r) r.checked = true;
      }
      if (k === 'i') chips.forEach(function (c) {
        if (v.split(',').indexOf(c.dataset.imp) >= 0) c.classList.add('on');
      });
      if (k === 'u') flags.unt.checked = true;
      if (k === 'a') flags.una.checked = true;
      if (k === 'st') flags.stale.checked = true;
      if (k === 'g') flags.gate.checked = true;
    });
  }
  apply(false);
})();
"""


def _e(text) -> str:
    return html.escape(str(text or ""))


def _tile(label: str, value, hint: str = "") -> str:
    hint_html = f'<div class="hint">{_e(hint)}</div>' if hint else ""
    return (f'<div class="tile"><div class="label">{_e(label)}</div>'
            f'<div class="value">{value}</div>{hint_html}</div>')


def _state_color(state: str) -> str:
    status = _STATE_STATUS.get(state)
    return f"var(--{status})" if status else "var(--muted)"


def _chip(state: str) -> str:
    dot = f'<span class="dot" style="background:{_state_color(state)}"></span>'
    return f'<span class="chip">{dot}{_e(STATE_LABELS[state])}</span>'


def _imp_chip(importance: str) -> str:
    status = _IMPORTANCE_STATUS.get(importance)
    dot = (f'<span class="dot" style="background:var(--{status})"></span>'
           if status else "")
    return f'<span class="chip">{dot}{_e(importance or "Undecided")}</span>'


def _nav(active: str) -> str:
    links = []
    for name, label in _PAGES_NAV:
        cls = ' class="active"' if name == active else ""
        links.append(f'<a href="{name}"{cls}>{label}</a>')
    return f"<nav>{''.join(links)}</nav>"


def _pkg_anchor(package: str) -> str:
    return (f'<a href="packages.html#pkg-{_e(package)}">'
            f"{_e(package)}</a>")


def _bug_link(bug_id) -> str:
    return (f'<a class="buglink" href="https://launchpad.net/bugs/{_e(bug_id)}">'
            f"#{_e(bug_id)}</a>")


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400


def _funnel(counts: Counter) -> str:
    rows = [(STATE_LABELS[s], counts[s]) for s in STATES if counts.get(s)]
    if not rows:
        return '<p class="empty">no data</p>'
    peak = max(n for _, n in rows)
    cells = []
    for name, n in rows:
        # Cap at 92% so the value label always fits beside the bar.
        width = max(2.0, 92.0 * n / peak)
        cells.append(f'<div class="name">{_e(name)}</div>')
        cells.append(
            f'<div class="track"><span class="fill" style="width:{width:.1f}%">'
            f'</span><span class="val">{n}</span></div>')
    return f'<div class="card funnel">{"".join(cells)}</div>'


def _footer(extra: str = "") -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"<footer>Generated {_e(generated)} by uplc from bulk archive "
            "reports (update_excuses, package-team-mapping, Sources indexes)"
            f".{' ' + extra if extra else ''} \"Seen\" ages are measured from "
            "local observation history and mature as ingest runs accumulate."
            "</footer>")


# --------------------------------------------------------------------------
# Overview page (index.html)

def _pipeline_table(conn: sqlite3.Connection, snaps) -> str:
    rows = []
    for s in sorted(snaps, key=lambda r: (STATES.index(r["state"]), r["package"])):
        if s["state"] not in PROPOSED_STATES:
            continue
        detail = json.loads(s["detail"] or "{}")
        bugs = " ".join(_bug_link(b) for b in detail.get("bugs", []))
        rows.append(
            "<tr>"
            f'<td class="pkg">{_pkg_anchor(s["package"])}</td>'
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
            f'<td class="pkg">{_pkg_anchor(s["package"])}</td>'
            f"<td>{_chip(s['state'])}</td>"
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
    team = {s["package"] for s in snaps}
    fan: Counter = Counter()
    victims: dict[str, list[str]] = {}
    for s in snaps:
        for blocker in json.loads(s["detail"] or "{}").get("blocked_by", []):
            fan[blocker] += 1
            victims.setdefault(blocker, []).append(s["package"])
    if not fan:
        return ""
    rows = []
    for blocker, n in fan.most_common(10):
        name = _pkg_anchor(blocker) if blocker in team else _e(blocker)
        links = ", ".join(_pkg_anchor(v) for v in sorted(victims[blocker]))
        rows.append(f'<tr><td class="pkg">{name}</td><td class="num">{n}</td>'
                    f'<td class="why cell">{links}</td></tr>')
    return ("<h2>Biggest unblock opportunities</h2>"
            '<div class="card scroll"><table>'
            "<tr><th>Blocking item</th><th>Blocks</th><th>Team packages</th></tr>"
            + "".join(rows) + "</table></div>")


def _latest(conn: sqlite3.Connection, team: str):
    run = db.latest_run(conn, team)
    if run is None:
        raise SystemExit(f"no data for team {team!r} — run `uplc ingest` first")
    return run, db.snapshots_for_run(conn, run["id"])


def render_index(conn: sqlite3.Connection, team: str) -> str:
    run, snaps = _latest(conn, team)
    counts = Counter(s["state"] for s in snaps)

    blocked = sum(counts.get(s, 0) for s in BLOCKED_STATES)
    in_proposed = sum(counts.get(s, 0) for s in PROPOSED_STATES)
    behind = counts.get("merge-needed", 0) + counts.get("sync-available", 0)

    tiles = [
        _tile("Team packages", run["package_count"]),
        _tile("In -proposed", in_proposed, f"{blocked} blocked"),
        _tile("Blocked", blocked, "need attention"),
        _tile("Behind Debian", behind,
              f"{counts.get('merge-needed', 0)} merges, "
              f"{counts.get('sync-available', 0)} syncs"),
        _tile("In sync", counts.get("in-sync", 0) + counts.get("ubuntu-only", 0),
              "incl. Ubuntu-only"),
    ]
    if db.bug_count(conn):
        tiles.append(_tile(
            "Open bugs", f'<a href="bugs.html">{db.open_bug_total(conn)}</a>',
            "see bugs page"))

    return f"""<title>Package life cycle — {_e(team)}</title>
<style>{_CSS}</style>
<div class="uplc"><div class="wrap">
{_nav('index.html')}
<h1>Ubuntu package life cycle — {_e(team)}</h1>
<div class="meta">{_e(run['series'])} series · snapshot {_e(run['ran_at'])}
 · excuses generated {_e(run['excuses_generated'])}</div>
<div class="tiles">{''.join(tiles)}</div>
<h2>Funnel</h2>
{_funnel(counts)}
<h2>In proposed-migration ({in_proposed})</h2>
{_pipeline_table(conn, snaps)}
<h2>Behind Debian ({behind})</h2>
{_delta_table(snaps)}
{_blockers_table(snaps)}
{_footer("No Launchpad API calls beyond the watermarked bug sync.")}
</div></div>
"""


# --------------------------------------------------------------------------
# Packages page (packages.html)

def _pkg_detail_row(s, detail: dict, colspan: int) -> str:
    pkg = s["package"]
    lines = [f"<div>{_e(s['summary'])}</div>"]
    if detail.get("blocked_by"):
        lines.append("<div>blocked by: "
                     + _e(", ".join(detail["blocked_by"])) + "</div>")
    if detail.get("regressions"):
        regs = detail["regressions"]
        shown = "; ".join(regs[:8]) + (f" (+{len(regs) - 8} more)"
                                       if len(regs) > 8 else "")
        lines.append(f"<div>regressions: {_e(shown)}</div>")
    if detail.get("missing_builds"):
        lines.append("<div>missing builds: "
                     + _e(", ".join(detail["missing_builds"])) + "</div>")
    if detail.get("bugs"):
        links = " ".join(_bug_link(b) for b in detail["bugs"])
        lines.append(f"<div>pipeline bugs: {links}</div>")

    links = [f'<a href="https://launchpad.net/ubuntu/+source/{_e(pkg)}">'
             "Launchpad</a>",
             f'<a href="https://bugs.launchpad.net/ubuntu/+source/{_e(pkg)}">'
             "LP bugs</a>",
             f'<a href="bugs.html#q={_e(pkg)}">team bugs</a>']
    if s["state"] in PROPOSED_STATES:
        links.append(f'<a href="{EXCUSES_HTML}#{_e(pkg)}">excuses</a>')
    if s["debian_version"]:
        links.append(f'<a href="https://tracker.debian.org/pkg/{_e(pkg)}">'
                     "Debian tracker</a>")
    lines.append('<div class="links">' + "".join(links) + "</div>")
    return (f'<tr class="detail"><td colspan="{colspan}">'
            + "".join(lines) + "</td></tr>")


def render_packages(conn: sqlite3.Connection, team: str) -> str:
    run, snaps = _latest(conn, team)
    counts = Counter(s["state"] for s in snaps)
    bugstats = db.open_bug_counts(conn)
    have_bugs = db.bug_count(conn) > 0

    blocked = sum(counts.get(s, 0) for s in BLOCKED_STATES)
    in_proposed = sum(counts.get(s, 0) for s in PROPOSED_STATES)
    behind = counts.get("merge-needed", 0) + counts.get("sync-available", 0)
    tiles = "".join([
        _tile("Team packages", run["package_count"]),
        _tile("In -proposed", in_proposed, f"{blocked} blocked"),
        _tile("Behind Debian", behind),
        _tile("Open bugs",
              f'<a href="bugs.html">{db.open_bug_total(conn)}</a>'
              if have_bugs else "—",
              "" if have_bugs else "run `uplc bugs-sync`"),
    ])

    chips = "".join(
        f'<button class="fchip" data-state="{_e(s)}" type="button">'
        f'<span class="dot" style="background:{_state_color(s)}"></span>'
        f"{_e(STATE_LABELS[s])} ({counts[s]})</button>"
        for s in STATES if counts.get(s))
    filters = f"""<div class="filters">
<input id="fq" type="search" placeholder="filter packages…" aria-label="filter packages">
{chips}
<label class="flag"><input id="f-bugs" type="checkbox">has open bugs</label>
<label class="flag"><input id="f-high" type="checkbox">Critical/High bugs</label>
<label class="flag"><input id="f-stuck" type="checkbox">≥ 7d in state</label>
<span class="fcount" id="fcount"></span>
</div>"""

    header = ('<thead><tr>'
              '<th class="sort" data-key="pkg">Package</th>'
              '<th class="sort" data-key="statei" data-num>State</th>'
              '<th class="sort" data-key="bugs" data-num>Bugs</th>'
              '<th>Ubuntu devel</th><th>Debian unstable</th><th>Proposed</th>'
              "</tr></thead>")
    bodies = []
    for s in sorted(snaps, key=lambda r: (STATES.index(r["state"]), r["package"])):
        pkg = s["package"]
        detail = json.loads(s["detail"] or "{}")
        days = _days_since(db.state_entered_at(conn, pkg))
        stats = bugstats.get(pkg, {"open": 0, "high": 0})
        if not have_bugs:
            bugcell = '<span class="empty">—</span>'
        elif stats["open"]:
            dot = ('<span class="dot" style="background:var(--critical)">'
                   "</span>" if stats["high"] else "")
            label = str(stats["open"])
            if stats["high"]:
                label += f" · {stats['high']} high"
            bugcell = (f'{dot}<a href="bugs.html#q={_e(pkg)}">{_e(label)}</a>')
        else:
            bugcell = '<span class="empty">0</span>'
        # Time-in-state only earns ink once it signals "stuck" (≥ 7d, the
        # same threshold as the filter) — fresher ages live in data-days.
        agehint = (f' <span class="agehint">{_e(_age_str(db.state_entered_at(conn, pkg)))}'
                   "</span>" if days is not None and days >= 7 else "")
        text = " ".join([pkg, STATE_LABELS[s["state"]],
                         s["summary"] or ""]).lower()
        bodies.append(
            f'<tbody class="pkgrow" id="pkg-{_e(pkg)}" data-pkg="{_e(pkg)}"'
            f' data-state="{_e(s["state"])}"'
            f' data-statei="{STATES.index(s["state"])}"'
            f' data-days="{-1 if days is None else round(days, 2)}"'
            f' data-bugs="{stats["open"]}" data-high="{stats["high"]}"'
            f' data-text="{_e(text)}">'
            "<tr>"
            f'<td class="pkg"><a href="#pkg-{_e(pkg)}">{_e(pkg)}</a></td>'
            f"<td>{_chip(s['state'])}{agehint}</td>"
            f'<td class="num cell bugs">{bugcell}</td>'
            f'<td class="ver">{_e(s["ubuntu_version"]) or "—"}</td>'
            f'<td class="ver">{_e(s["debian_version"]) or "—"}</td>'
            f'<td class="ver">{_e(s["proposed_version"]) or ""}</td>'
            "</tr>"
            + _pkg_detail_row(s, detail, 6)
            + "</tbody>")

    return f"""<title>Packages — {_e(team)}</title>
<style>{_CSS}</style>
<div class="uplc"><div class="wrap">
{_nav('packages.html')}
<h1>Packages — {_e(team)}</h1>
<div class="meta">{_e(run['series'])} series · snapshot {_e(run['ran_at'])}
 · click a package for detail and links · filters combine</div>
<div class="tiles">{tiles}</div>
<h2>All packages ({len(snaps)})</h2>
{filters}
<div class="card scroll"><table id="ptable">{header}{''.join(bodies)}</table></div>
{_footer()}
</div></div>
<script>{_PKG_JS}</script>
"""


# --------------------------------------------------------------------------
# Bugs page (bugs.html)

def _aggregate_bugs(conn: sqlite3.Connection) -> list[dict]:
    """Fold task rows into one dict per bug for the report."""
    bugs: dict[int, dict] = {}
    for r in db.bugs_with_tasks(conn):
        b = bugs.setdefault(r["id"], {
            "id": r["id"], "title": r["title"],
            "tags": json.loads(r["tags"] or "[]"),
            "created": r["date_created"], "updated": r["date_last_updated"],
            "origin": r["origin"], "tasks": [],
        })
        b["tasks"].append(r)
    for b in bugs.values():
        tasks = b["tasks"]
        open_tasks = [t for t in tasks
                      if t["status"] in OPEN_BUG_STATUSES]
        b["open"] = bool(open_tasks)
        b["packages"] = sorted({t["package"] for t in tasks if t["package"]})
        b["importance"] = max(
            (t["importance"] for t in tasks),
            key=lambda i: _IMPORTANCE_RANK.get(i, 0), default="")
        pick = open_tasks or tasks
        devel = [t for t in pick if not t["series"]]
        b["status"] = (devel or pick)[0]["status"] if pick else ""
        b["assignees"] = sorted(
            {t["assignee"] for t in open_tasks if t["assignee"]})
        b["closed"] = ("" if b["open"] else
                       max((t["date_closed"] for t in tasks
                            if t["date_closed"]), default=""))
        b["age_days"] = _days_since(b["created"]) or 0
        b["idle_days"] = _days_since(b["updated"]) or 0
    return list(bugs.values())


def _pipeline_refs(conn: sqlite3.Connection, team: str) -> dict[int, list]:
    """{bug_id: [(package, state), ...]} from the latest snapshot."""
    run = db.latest_run(conn, team)
    if run is None:
        return {}
    refs: dict[int, list] = {}
    for s in db.snapshots_for_run(conn, run["id"]):
        for b in json.loads(s["detail"] or "{}").get("bugs", []):
            if str(b).isdigit():
                refs.setdefault(int(b), []).append((s["package"], s["state"]))
    return refs


def _gating_table(refs: dict[int, list], by_id: dict[int, dict]) -> str:
    if not refs:
        return ""
    rows = []
    for bug_id in sorted(refs):
        bug = by_id.get(bug_id)
        pkgs = " · ".join(
            f"{_pkg_anchor(pkg)} {_chip(state)}" for pkg, state in refs[bug_id])
        title = _e(bug["title"]) if bug else \
            '<span class="empty">(not yet synced)</span>'
        status = _e(bug["status"]) if bug else ""
        imp = _imp_chip(bug["importance"]) if bug else ""
        rows.append(
            f'<tr><td class="num">{_bug_link(bug_id)}</td>'
            f'<td class="title">{title}</td><td>{status}</td><td>{imp}</td>'
            f'<td class="cell">{pkgs}</td></tr>')
    return (f"<h2>Gating the pipeline ({len(refs)})</h2>"
            '<p class="empty">Bugs referenced by proposed-migration '
            "(block-proposed / update-excuse) — fixing these directly "
            "unblocks migrations.</p>"
            '<div class="card scroll"><table>'
            "<tr><th>Bug</th><th>Title</th><th>Status</th><th>Importance</th>"
            "<th>Holds up</th></tr>"
            + "".join(rows) + "</table></div>")


def _trend_chart(bugs: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    opened: Counter = Counter()
    closed: Counter = Counter()
    prefix = f"{now.year}-"
    for b in bugs:
        if (b["created"] or "").startswith(prefix):
            opened[int(b["created"][5:7])] += 1
        if (b["closed"] or "").startswith(prefix):
            closed[int(b["closed"][5:7])] += 1
    months = range(1, now.month + 1)
    peak = max([opened[m] for m in months] + [closed[m] for m in months] + [1])
    cells = []
    for m in months:
        name = datetime(now.year, m, 1).strftime("%b")
        cells.append(f'<div class="name">{name}</div><div>')
        for cls, n in (("", opened[m]), (" f2", closed[m])):
            width = 92.0 * n / peak
            cells.append(
                f'<div class="track"><span class="fill{cls}"'
                f' style="width:{width:.1f}%"></span>'
                f'<span class="val">{n}</span></div>')
        cells.append("</div>")
    return (f"<h2>Opened vs closed, {now.year}</h2>"
            '<div class="card">'
            '<div class="legend">'
            '<span><span class="sw" style="background:var(--bar)"></span>'
            "opened</span>"
            '<span><span class="sw" style="background:var(--bar2)"></span>'
            "closed</span></div>"
            f'<div class="trend">{"".join(cells)}</div>'
            "<p class=\"empty\">Counted from Launchpad bug dates, so the "
            "history is complete even before uplc started syncing.</p></div>")


def render_bugs(conn: sqlite3.Connection, team: str) -> str:
    sync = db.bug_sync_state(conn, team)
    bugs = _aggregate_bugs(conn)
    refs = _pipeline_refs(conn, team)

    if not bugs:
        return f"""<title>Bugs — {_e(team)}</title>
<style>{_CSS}</style>
<div class="uplc"><div class="wrap">
{_nav('bugs.html')}
<h1>Bugs — {_e(team)}</h1>
<div class="card"><p class="empty">No bug data yet. Run
<code>uplc bugs-sync</code> — the first sync fetches every bug touched
this year (heavy, one-time); after that each sync is a small watermarked
increment.</p></div>
{_footer()}
</div></div>
"""

    by_id = {b["id"]: b for b in bugs}
    year = datetime.now(timezone.utc).year
    open_bugs = [b for b in bugs if b["open"]]
    untriaged = [b for b in open_bugs
                 if b["status"] == "New" or not _IMPORTANCE_RANK.get(b["importance"])]
    high = [b for b in open_bugs if b["importance"] in ("Critical", "High")]
    opened_y = [b for b in bugs if (b["created"] or "").startswith(f"{year}-")]
    closed_y = [b for b in bugs if (b["closed"] or "").startswith(f"{year}-")]
    stale = [b for b in open_bugs if b["idle_days"] > _STALE_DAYS]

    tiles = "".join([
        _tile("Open", len(open_bugs)),
        _tile("Untriaged", len(untriaged), "New or no importance"),
        _tile("Critical / High", len(high)),
        _tile(f"Opened {year}", len(opened_y)),
        _tile(f"Closed {year}", len(closed_y)),
        _tile(f"Stale > {_STALE_DAYS}d", len(stale), "open, no activity"),
    ])

    imps = [i for i in ("Critical", "High", "Medium", "Low", "Wishlist",
                        "Undecided")
            if any((b["importance"] or "Undecided") == i for b in bugs)]
    chips = "".join(
        f'<button class="fchip" data-imp="{_e(i)}" type="button">{_e(i)}'
        "</button>" for i in imps)
    filters = f"""<div class="filters">
<input id="fq" type="search" placeholder="filter bugs (title, package, tag)…" aria-label="filter bugs">
<label class="flag"><input type="radio" name="scope" value="open" checked>open</label>
<label class="flag"><input type="radio" name="scope" value="closed">closed</label>
<label class="flag"><input type="radio" name="scope" value="all">all</label>
{chips}
<label class="flag"><input id="f-unt" type="checkbox">untriaged</label>
<label class="flag"><input id="f-una" type="checkbox">unassigned</label>
<label class="flag"><input id="f-stale" type="checkbox">stale &gt; {_STALE_DAYS}d</label>
<label class="flag"><input id="f-gate" type="checkbox">gating only</label>
<span class="fcount" id="fcount"></span>
</div>"""

    header = ('<thead><tr>'
              '<th class="sort" data-key="id" data-num>Bug</th>'
              '<th>Title</th>'
              '<th class="sort" data-key="pkg">Package</th>'
              '<th class="sort" data-key="status">Status</th>'
              '<th class="sort" data-key="impn" data-num>Importance</th>'
              "<th>Assignee</th>"
              '<th class="sort" data-key="age" data-num>Age</th>'
              '<th class="sort" data-key="idle" data-num>Activity</th>'
              "</tr></thead>")
    # Default order: most important first, coldest first within a level —
    # "important and going quiet" floats to the top.
    ordered = sorted(bugs, key=lambda b: (
        -_IMPORTANCE_RANK.get(b["importance"], 0), -b["idle_days"]))
    rows = []
    for b in ordered:
        title = b["title"] or ""
        shown = title if len(title) <= 90 else title[:87] + "…"
        tags = "".join(f'<span class="tag">{_e(t)}</span>'
                       for t in b["tags"][:3])
        pkgs = ", ".join(_pkg_anchor(p) for p in b["packages"]) or "—"
        assignees = ", ".join(b["assignees"][:2])
        if len(b["assignees"]) > 2:
            assignees += f" +{len(b['assignees']) - 2}"
        imp = b["importance"] or "Undecided"
        text = " ".join([str(b["id"]), title] + b["packages"] + b["tags"]
                        + b["assignees"]).lower()
        gating = b["id"] in refs
        gate_tag = ' <span class="tag">gating</span>' if gating else ""
        untri = (b["open"] and
                 (b["status"] == "New" or not _IMPORTANCE_RANK.get(b["importance"])))
        rows.append(
            f'<tr data-id="{b["id"]}" data-open="{int(b["open"])}"'
            f' data-imp="{_e(imp)}"'
            f' data-impn="{_IMPORTANCE_RANK.get(b["importance"], 0)}"'
            f' data-pkg="{_e(b["packages"][0] if b["packages"] else "")}"'
            f' data-status="{_e(b["status"])}"'
            f' data-unt="{int(untri)}"'
            f' data-una="{int(b["open"] and not b["assignees"])}"'
            f' data-idle="{b["idle_days"]:.1f}" data-age="{b["age_days"]:.1f}"'
            f' data-gate="{int(gating)}" data-text="{_e(text)}">'
            f'<td class="num">{_bug_link(b["id"])}</td>'
            f'<td class="title" title="{_e(title)}">{_e(shown)}{tags}'
            f"{gate_tag}</td>"
            f'<td class="cell">{pkgs}</td>'
            f'<td>{_e(b["status"])}</td>'
            f"<td>{_imp_chip(b['importance'])}</td>"
            f"<td>{_e(assignees)}</td>"
            f'<td class="num">{_e(_age_str(b["created"]))}</td>'
            f'<td class="num">{_e(_age_str(b["updated"]))}</td>'
            "</tr>")

    synced = (f" · synced {_e(sync['last_synced'])}" if sync else
              " · sync incomplete — rerun `uplc bugs-sync`")
    return f"""<title>Bugs — {_e(team)}</title>
<style>{_CSS}</style>
<div class="uplc"><div class="wrap">
{_nav('bugs.html')}
<h1>Bugs — {_e(team)}</h1>
<div class="meta">{len(bugs)} bugs touched since {FIRST_SYNC_SINCE}{synced}
 · "Activity" is time since the last change on the bug</div>
<div class="tiles">{tiles}</div>
{_gating_table(refs, by_id)}
{_trend_chart(bugs)}
<h2>All bugs ({len(bugs)})</h2>
{filters}
<div class="card scroll"><table id="btable">{header}<tbody>{''.join(rows)}</tbody></table></div>
{_footer("Bug data from the anonymous Launchpad API (watermarked sync); "
         "private bugs are not visible.")}
</div></div>
<script>{_BUGS_JS}</script>
"""


PAGES = {
    "index.html": render_index,
    "packages.html": render_packages,
    "bugs.html": render_bugs,
}

# Backwards-compatible name for the single-page overview.
render = render_index
