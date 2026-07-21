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
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as _xml_escape

from . import db, kpi
from .db import OPEN_BUG_STATUSES
from .lpbugs import FIRST_SYNC_SINCE
from .report import STATE_LABELS, _age_str
from .sources import EXCUSES_HTML, bug_verification_status
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
    "sync-available": "good",
    "ready-to-migrate": "good",
    "in-sync": "good",
}

_IMPORTANCE_RANK = {
    "Critical": 5, "High": 4, "Medium": 3, "Low": 2, "Wishlist": 1,
}
_IMPORTANCE_STATUS = {"Critical": "critical", "High": "serious"}

_SRU_STATUS_LABEL = {
    "verified": "verified", "verification-failed": "verification failed",
    "removal-candidate": "removal candidate", "incomplete": "incomplete",
    "broken": "broken", "pending": "needs verification",
}
_SRU_STATUS_ROLE = {
    "verified": "good", "verification-failed": "critical",
    "removal-candidate": "serious", "incomplete": "warning",
    "broken": "critical",
}

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
.uplc .flow svg.diagram { width: 100%; height: auto; display: block;
  overflow: visible; }
.uplc .flow svg.diagram text { font: 11px system-ui, -apple-system, sans-serif;
  fill: var(--ink-2); }
.uplc .flow text.count { font-weight: 700; fill: var(--ink); font-size: 14px; }
.uplc .flow text.node-label { font-size: 11px; }
.uplc .flow text.sub-label { font-size: 9px; fill: var(--muted); }
.uplc .flow .node-shape { fill: var(--surface); stroke: var(--border);
  stroke-width: 1.5; }
.uplc .flow .node-shape.hub { stroke: var(--bar); stroke-width: 2; }
.uplc .flow .node-shape.dashed { stroke-dasharray: 4 3; }
.uplc .flow .node a { cursor: pointer; }
.uplc .flow .node.dim .node-shape, .uplc .flow .node.dim text { opacity: .28; }
.uplc .flow .edge { fill: none; stroke: var(--muted); opacity: .55;
  stroke-width: 1.75; stroke-linecap: round; }
.uplc .flow .edge.dashed { stroke: var(--critical); stroke-width: 2;
  stroke-dasharray: 6 4; opacity: .75; }
.uplc .flow .edge.dim { opacity: .08; }
.uplc .flow .pin rect { fill: var(--ink); }
.uplc .flow .flow-controls { margin-bottom: 14px; }
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
.uplc .fsep { width: 1px; align-self: stretch; background: var(--border); }
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
.uplc .tile .delta { font-size: 12px; margin-top: 2px; }
.uplc .delta.bad, .uplc td .bad { color: var(--critical); }
.uplc .delta.good, .uplc td .good { color: var(--good); }
.uplc .chart svg { width: 100%; height: auto; display: block; }
.uplc .chart svg text { font: 11px system-ui, -apple-system, sans-serif;
  fill: var(--muted); }
.uplc .chart .legend { margin-bottom: 8px; }
.uplc details.tbl { margin-top: 10px; font-size: 12.5px; color: var(--ink-2); }
.uplc details.tbl summary { cursor: pointer; color: var(--muted);
  font-size: 12px; }
.uplc details.tbl table { margin-top: 8px; max-width: 420px; }
.uplc .subscribe { background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 16px; font-size: 13px;
  color: var(--ink-2); margin-bottom: 20px; }
.uplc .digest-md { max-width: 760px; }
.uplc .digest-md h1 { font-size: 18px; margin: 8px 0; }
.uplc .digest-md h2 { font-size: 15px; }
.uplc .digest-md h3 { font-size: 14px; font-weight: 600; margin: 18px 0 6px; }
.uplc .digest-md blockquote { margin: 8px 0; padding: 2px 12px;
  border-left: 3px solid var(--bar2); color: var(--ink-2); }
.uplc .digest-md .scroll { overflow-x: auto; }
.uplc details.digest { margin: 6px 0; }
.uplc details.digest summary { cursor: pointer; font-size: 14px;
  padding: 4px 0; }
.uplc details.digest .digest-md { border-left: 2px solid var(--grid);
  padding-left: 16px; margin: 6px 0 14px; }
"""

_PAGES_NAV = [
    ("index.html", "Overview"),
    ("packages.html", "Packages"),
    ("bugs.html", "Bugs"),
    ("kpi.html", "KPIs"),
    ("digest.html", "Digest"),
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
  var gchips = Array.prototype.slice.call(
    document.querySelectorAll('.fchip[data-group]'));
  var flags = {
    bugs: document.getElementById('f-bugs'),
    high: document.getElementById('f-high'),
    stuck: document.getElementById('f-stuck')
  };

  function apply(updateHash) {
    var states = chips.filter(function (c) {
      return c.classList.contains('on');
    }).map(function (c) { return c.dataset.state; });
    var grps = gchips.filter(function (c) {
      return c.classList.contains('on');
    }).map(function (c) { return c.dataset.group; });
    var text = q.value.trim().toLowerCase();
    var n = 0;
    rows.forEach(function (r) {
      var show = true;
      if (states.length && states.indexOf(r.dataset.state) < 0) show = false;
      if (show && grps.length) {
        var rg = r.dataset.group ? r.dataset.group.split(',') : [];
        if (!grps.some(function (g) { return rg.indexOf(g) >= 0; })) show = false;
      }
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
      if (grps.length) parts.push('g=' + grps.map(encodeURIComponent).join(','));
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
  gchips.forEach(function (c) {
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
      if (k === 'g') {
        var wanted = v.split(',').map(decodeURIComponent);
        gchips.forEach(function (c) {
          if (wanted.indexOf(c.dataset.group) >= 0) c.classList.add('on');
        });
      }
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

# --- Overview flow diagram script: highlight a searched package's node. ---
_FLOW_JS = """
(function () {
  var svg = document.getElementById('flow-svg');
  var input = document.getElementById('flow-search');
  if (!svg || !input) return;
  var idx = JSON.parse(document.getElementById('flow-pkg-index').textContent);
  var nodeFor = JSON.parse(document.getElementById('flow-node-map').textContent);
  var nodes = {};
  Array.prototype.forEach.call(svg.querySelectorAll('[data-node]'), function (g) {
    nodes[g.getAttribute('data-node')] = g;
  });
  var pin = null;

  function clear() {
    Object.keys(nodes).forEach(function (id) { nodes[id].classList.remove('dim'); });
    Array.prototype.forEach.call(svg.querySelectorAll('.edge'), function (e) {
      e.classList.remove('dim');
    });
    if (pin) { pin.remove(); pin = null; }
  }

  function addPin(nodeId, pkg) {
    var n = nodes[nodeId], bb = n.getBBox();
    var x = bb.x + bb.width / 2, y = bb.y - 10;
    var text = '\\u25cf ' + pkg;
    var w = 16 + text.length * 6.3;
    var NS = 'http://www.w3.org/2000/svg';
    var g = document.createElementNS(NS, 'g');
    g.setAttribute('class', 'pin');
    var rect = document.createElementNS(NS, 'rect');
    rect.setAttribute('x', x - w / 2); rect.setAttribute('y', y - 20);
    rect.setAttribute('width', w); rect.setAttribute('height', 18);
    rect.setAttribute('rx', 5);
    var tri = document.createElementNS(NS, 'polygon');
    tri.setAttribute('points',
      (x - 5) + ',' + (y - 2) + ' ' + (x + 5) + ',' + (y - 2) + ' ' + x + ',' + (y + 3));
    tri.style.fill = 'var(--ink)';
    var t = document.createElementNS(NS, 'text');
    t.setAttribute('x', x); t.setAttribute('y', y - 7);
    t.setAttribute('text-anchor', 'middle');
    t.style.fill = 'var(--page)';
    t.textContent = text;
    g.appendChild(rect); g.appendChild(tri); g.appendChild(t);
    svg.appendChild(g);
    pin = g;
  }

  function apply(updateHash) {
    var pkg = input.value.trim();
    clear();
    var state = idx[pkg];
    var nodeId = state ? nodeFor[state] : null;
    if (nodeId && nodes[nodeId]) {
      Object.keys(nodes).forEach(function (id) {
        if (id !== nodeId) nodes[id].classList.add('dim');
      });
      Array.prototype.forEach.call(svg.querySelectorAll('.edge'), function (e) {
        var f = e.getAttribute('data-from'), t = e.getAttribute('data-to');
        if (f !== nodeId && t !== nodeId) e.classList.add('dim');
      });
      addPin(nodeId, pkg);
    }
    if (updateHash) {
      history.replaceState(null, '',
        pkg ? '#pkg=' + encodeURIComponent(pkg) : location.pathname);
    }
  }

  input.addEventListener('input', function () { apply(true); });
  input.addEventListener('change', function () { apply(true); });

  var hash = decodeURIComponent(location.hash.slice(1));
  if (hash.indexOf('pkg=') === 0) {
    input.value = hash.slice(4);
    apply(false);
  }
})();
"""


def _e(text) -> str:
    return html.escape(str(text or ""))


def _tile(label: str, value, hint: str = "", raw: str = "") -> str:
    """One stat tile; `raw` is trusted extra HTML (e.g. a delta line)."""
    hint_html = f'<div class="hint">{_e(hint)}</div>' if hint else ""
    return (f'<div class="tile"><div class="label">{_e(label)}</div>'
            f'<div class="value">{value}</div>{hint_html}{raw}</div>')


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


def _sru_chip(status: str) -> str:
    role = _SRU_STATUS_ROLE.get(status)
    dot = f'<span class="dot" style="background:var(--{role})"></span>' \
        if role else ""
    return f'<span class="chip">{dot}{_e(_SRU_STATUS_LABEL.get(status, status))}</span>'


def _pending_sru_rows(conn: sqlite3.Connection, team: str) -> list[dict]:
    rows = []
    for r in db.latest_pending_sru(conn, team):
        d = dict(r)
        d["bugs"] = json.loads(d.get("bugs") or "[]")
        rows.append(d)
    return rows


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


# --------------------------------------------------------------------------
# Flow diagram (index.html) — a redraw of the team's own hand-sketched
# pipeline (Debian -> Merge/Sync -> Upload -> Build -> Test -> Devel, with
# a Sponsor input and FTBFS/Excuses retry loops back into Upload), drawn
# from the live snapshot. The layout (positions, shapes) is fixed; only
# counts/radii are data-driven, so the diagram's shape stays stable run
# over run even as populations shift. Circle *area* (not radius) scales
# with count so volumes compare fairly at a glance; Upload/Build/Test are
# pass-through process steps with no state of their own, so they're sized
# fixed and carry no count.

_FLOW_NODES = {
    "debian": {"shape": "circle", "x": 70, "y": 300, "r": 30,
               "label": "Debian", "states": None},
    "merge-needed": {"shape": "circle", "x": 250, "y": 170,
                      "states": ["merge-needed"]},
    "sync-available": {"shape": "circle", "x": 250, "y": 430,
                        "states": ["sync-available"]},
    "sponsor": {"shape": "circle", "x": 480, "y": 60, "r": 26,
                "label": "Sponsor", "sub": "(not tracked yet)",
                "states": None, "dashed": True},
    "upload": {"shape": "diamond", "x": 480, "y": 300, "w": 100, "h": 100,
               "label": "Upload", "hub": True, "states": None},
    "build": {"shape": "diamond", "x": 650, "y": 300, "w": 100, "h": 100,
              "label": "Build", "states": None},
    "test": {"shape": "diamond", "x": 820, "y": 300, "w": 100, "h": 100,
             "label": "Test", "sub": "ready + waiting-age",
             "states": ["waiting-age", "ready-to-migrate"]},
    "devel": {"shape": "circle", "x": 960, "y": 300, "label": "Devel",
              "sub": "in-sync + Ubuntu-only",
              "states": ["in-sync", "ubuntu-only"]},
    "ftbfs": {"shape": "circle", "x": 650, "y": 490, "label": "FTBFS",
              "sub": "= blocked-build", "states": ["blocked-build"]},
    "excuses": {"shape": "circle", "x": 820, "y": 490, "label": "Excuses",
                "sub": "tests + deps + other",
                "states": ["blocked-tests", "blocked-depends",
                           "blocked-other"]},
    "not-in-devel": {"shape": "circle", "x": 70, "y": 490,
                      "states": ["not-in-devel"], "dashed": True},
}

# Forward edges (solid, arrowhead) and retry loops (dashed, critical-red).
# The two "-> upload" retry edges use a bespoke loopback curve instead of
# the generic path since they arc back past intervening nodes.
_FLOW_EDGES_SOLID = [
    ("debian", "merge-needed"), ("debian", "sync-available"),
    ("merge-needed", "upload"), ("sync-available", "upload"),
    ("sponsor", "upload"), ("upload", "build"), ("build", "test"),
    ("test", "devel"),
]
_FLOW_EDGES_LOOP = [("build", "ftbfs"), ("test", "excuses")]
_FLOW_EDGES_RETURN = [("ftbfs", "upload"), ("excuses", "upload")]

# The nodes that represent an actual holding pen (as opposed to Debian/
# Devel bookends or the pass-through Upload/Build steps) get two extra
# cues layered on top of size: a fill wash whose strength is volume
# relative to the *other* holding pens (not the whole team, which would
# wash everything out next to Devel's few hundred), and a border weight
# that tracks how long packages have typically been stuck there — so a
# node that's big AND stale reads as more urgent than one that's merely
# big because a batch landed yesterday.
_FLOW_HEAT_NODES = ["merge-needed", "sync-available", "test", "ftbfs", "excuses"]


def _flow_radius(count: int, base: float = 20.0, k: float = 4.2,
                  cap: float = 64.0) -> float:
    return min(cap, base + (count ** 0.5) * k)


def _flow_resolve(counts: Counter) -> dict:
    """Copy _FLOW_NODES with circle radii filled in from live counts."""
    resolved = {}
    for node_id, spec in _FLOW_NODES.items():
        spec = dict(spec)
        if spec["shape"] == "circle" and "r" not in spec:
            states = spec.get("states") or []
            count = sum(counts.get(s, 0) for s in states)
            spec["r"] = _flow_radius(count)
        resolved[node_id] = spec
    return resolved


def _flow_anchor(spec: dict, side: str) -> tuple[float, float]:
    cx, cy = spec["x"], spec["y"]
    rx = ry = spec["r"] if spec["shape"] == "circle" else None
    if spec["shape"] != "circle":
        rx, ry = spec["w"] / 2, spec["h"] / 2
    return {
        "left": (cx - rx, cy), "right": (cx + rx, cy),
        "top": (cx, cy - ry), "bottom": (cx, cy + ry),
    }[side]


def _flow_edge_path(f: dict, t: dict) -> str:
    dx, dy = t["x"] - f["x"], t["y"] - f["y"]
    if abs(dy) > abs(dx) * 1.3:
        x1, y1 = _flow_anchor(f, "bottom" if dy > 0 else "top")
        x2, y2 = _flow_anchor(t, "top" if dy > 0 else "bottom")
        my = (y1 + y2) / 2
        return f"M {x1},{y1} C {x1},{my} {x2},{my} {x2},{y2}"
    x1, y1 = _flow_anchor(f, "right")
    x2, y2 = _flow_anchor(t, "left")
    mx = (x1 + x2) / 2
    return f"M {x1},{y1} C {mx},{y1} {mx},{y2} {x2},{y2}"


def _flow_stage_age(conn: sqlite3.Connection, snaps, states: list[str]) -> float | None:
    """Median days-in-state across packages currently in any of `states`."""
    ages = []
    for s in snaps:
        if s["state"] in states:
            days = _days_since(db.state_entered_at(conn, s["package"]))
            if days is not None:
                ages.append(days)
    if not ages:
        return None
    ages.sort()
    mid = len(ages) // 2
    if len(ages) % 2:
        return ages[mid]
    return (ages[mid - 1] + ages[mid]) / 2


def _flow_loopback_path(f: dict, t: dict) -> str:
    """Bespoke arc for the FTBFS/Excuses -> Upload retry loops, which arc
    back past the Build/Test nodes rather than crossing straight through."""
    r = f["r"]
    x1, y1 = f["x"] - r * 0.6, f["y"] - r * 0.7
    c1x, c1y = f["x"] - 140, f["y"] - 60
    c2x, c2y = t["x"] - 60, t["y"] + 140
    x2, y2 = t["x"] - 10, t["y"] + t["h"] / 2 - 2
    return f"M {x1},{y1} C {c1x},{c1y} {c2x},{c2y} {x2},{y2}"


def _flow_node_svg(node_id: str, spec: dict, counts: Counter,
                    heat: float | None = None,
                    age_days: float | None = None) -> str:
    states = spec.get("states")
    count = sum(counts.get(s, 0) for s in states) if states else None
    label = spec.get("label") or (STATE_LABELS[states[0]] if states else "")
    sub = spec.get("sub", "")
    if age_days is not None:
        age_txt = f"med {age_days:.0f}d"
        sub = f"{sub} · {age_txt}" if sub else age_txt

    if spec.get("hub"):
        color = "var(--bar)"
    elif not states:
        color = "var(--muted)"
    elif node_id == "devel":
        color = "var(--good)"
    else:
        color = _state_color(states[0])

    classes = ["node-shape"]
    if spec.get("hub"):
        classes.append("hub")
    if spec.get("dashed"):
        classes.append("dashed")
    # Only circles carry their status as a stroke color; diamonds (the
    # pass-through process steps) keep the shared neutral card border.
    decls = []
    if spec["shape"] == "circle" and (spec.get("dashed")
                                       or (states and not spec.get("hub"))):
        decls.append(f"stroke:{color}")
    if heat is not None:
        # Volume relative to the other holding pens, as a fill wash — a
        # thin colored outline on an otherwise white circle reads as pale
        # no matter how big the number inside is.
        pct = 10 + 35 * max(0.0, min(1.0, heat))
        decls.append(f"fill:color-mix(in srgb, {color} {pct:.0f}%, var(--surface))")
    if age_days is not None:
        # Longer-stuck holding pens get a heavier border, independent of
        # volume, so "big and stale" outranks "big because a batch landed".
        width = 1.5 + 3.0 * max(0.0, min(1.0, age_days / 30.0))
        decls.append(f"stroke-width:{width:.1f}")
    style = f' style="{";".join(decls)}"' if decls else ""

    cx, cy = spec["x"], spec["y"]
    if spec["shape"] == "diamond":
        w, h = spec["w"], spec["h"]
        points = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
        shape_svg = (f'<polygon points="{points}" '
                     f'class="{" ".join(classes)}"{style}></polygon>')
    else:
        r = spec["r"]
        shape_svg = (f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" '
                     f'class="{" ".join(classes)}"{style}></circle>')

    lines = []
    if count is not None:
        lines.append((count, "count"))
    lines.append((label, "node-label"))
    if sub:
        lines.append((sub, "sub-label"))
    start_y = cy - ((len(lines) - 1) * 13) / 2 + 4
    # html.escape (not _e — its `or ""` would blank out a count of 0)
    text_svg = "".join(
        f'<text x="{cx}" y="{start_y + i * 13:.1f}" text-anchor="middle" '
        f'class="{cls}">{html.escape(str(v))}</text>'
        for i, (v, cls) in enumerate(lines))

    inner = f'<g class="node" data-node="{node_id}">{shape_svg}{text_svg}</g>'
    if states:
        href = f'packages.html#s={",".join(states)}'
        return f'<a href="{href}">{inner}</a>'
    return inner


def _flow_edges_svg(nodes: dict) -> str:
    paths = [
        '<defs><marker id="flow-arrow" markerWidth="8" markerHeight="8" '
        'refX="6" refY="3" orient="auto">'
        '<path d="M0,0 L6,3 L0,6 Z" fill="var(--muted)"></path>'
        '</marker></defs>'
    ]
    for from_id, to_id in _FLOW_EDGES_SOLID:
        d = _flow_edge_path(nodes[from_id], nodes[to_id])
        paths.append(f'<path d="{d}" class="edge" marker-end="url(#flow-arrow)" '
                     f'data-from="{from_id}" data-to="{to_id}"></path>')
    for from_id, to_id in _FLOW_EDGES_LOOP:
        d = _flow_edge_path(nodes[from_id], nodes[to_id])
        paths.append(f'<path d="{d}" class="edge dashed" '
                     f'data-from="{from_id}" data-to="{to_id}"></path>')
    for from_id, to_id in _FLOW_EDGES_RETURN:
        d = _flow_loopback_path(nodes[from_id], nodes[to_id])
        paths.append(f'<path d="{d}" class="edge dashed" '
                     f'data-from="{from_id}" data-to="{to_id}"></path>')
    return "".join(paths)


def _flow(conn: sqlite3.Connection, counts: Counter, snaps) -> str:
    resolved = _flow_resolve(counts)
    edges_svg = _flow_edges_svg(resolved)
    heat_peak = max((sum(counts.get(s, 0) for s in _FLOW_NODES[n]["states"])
                      for n in _FLOW_HEAT_NODES), default=1) or 1
    nodes_svg = "".join(
        _flow_node_svg(
            nid, spec, counts,
            heat=(sum(counts.get(s, 0) for s in spec["states"]) / heat_peak
                  if nid in _FLOW_HEAT_NODES else None),
            age_days=(_flow_stage_age(conn, snaps, spec["states"])
                      if nid in _FLOW_HEAT_NODES else None))
        for nid, spec in resolved.items())
    svg = (f'<svg class="diagram" viewBox="0 0 1000 560" '
           f'id="flow-svg">{edges_svg}{nodes_svg}</svg>')

    pkg_names = sorted(s["package"] for s in snaps)
    pkg_index = {s["package"]: s["state"] for s in snaps}
    node_for_state = {s: nid for nid, spec in _FLOW_NODES.items()
                       for s in (spec.get("states") or [])}
    datalist = "".join(f"<option>{_e(p)}</option>" for p in pkg_names)
    controls = f"""<div class="filters flow-controls">
<input id="flow-search" type="search" list="flow-pkglist"
 placeholder="track a package…" aria-label="track a package">
<datalist id="flow-pkglist">{datalist}</datalist>
</div>"""

    # Package names are archive-sourced, not literal constants — neutralize
    # any accidental "</script>" sequence before inlining as JSON.
    def _json_script(obj):
        return json.dumps(obj).replace("</", "<\\/")

    return f"""<div class="flow">
{controls}
<div class="card">{svg}</div>
</div>
<script type="application/json" id="flow-pkg-index">{_json_script(pkg_index)}</script>
<script type="application/json" id="flow-node-map">{_json_script(node_for_state)}</script>
<script>{_FLOW_JS}</script>"""


def _footer(extra: str = "") -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"<footer>Generated {_e(generated)} by uplc from bulk archive "
            "reports (update_excuses, package-team-mapping, Sources indexes)"
            f".{' ' + extra if extra else ''} \"Seen\" ages are measured from "
            "local observation history and mature as ingest runs accumulate."
            "</footer>")


# --------------------------------------------------------------------------
# Overview page (index.html)

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
<h2>Pipeline flow</h2>
{_flow(conn, counts, snaps)}
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


def render_packages(conn: sqlite3.Connection, team: str,
                     groups: dict[str, list[str]] | None = None) -> str:
    run, snaps = _latest(conn, team)
    counts = Counter(s["state"] for s in snaps)
    bugstats = db.open_bug_counts(conn)
    have_bugs = db.bug_count(conn) > 0
    groups = groups or {}

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
    have_groups = bool(groups)
    all_groups = sorted({g for s in snaps for g in groups.get(s["package"], [])})
    gchips = "".join(
        f'<button class="fchip" data-group="{_e(g)}" type="button">{_e(g)}</button>'
        for g in all_groups)
    group_filter = (f'<span class="fsep"></span>{gchips}' if have_groups else "")
    filters = f"""<div class="filters">
<input id="fq" type="search" placeholder="filter packages…" aria-label="filter packages">
{chips}{group_filter}
<label class="flag"><input id="f-bugs" type="checkbox">has open bugs</label>
<label class="flag"><input id="f-high" type="checkbox">Critical/High bugs</label>
<label class="flag"><input id="f-stuck" type="checkbox">≥ 7d in state</label>
<span class="fcount" id="fcount"></span>
</div>"""

    group_header = '<th>Group</th>' if have_groups else ""
    header = ('<thead><tr>'
              '<th class="sort" data-key="pkg">Package</th>'
              '<th class="sort" data-key="statei" data-num>State</th>'
              '<th class="sort" data-key="bugs" data-num>Bugs</th>'
              f'{group_header}'
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
        pkg_groups = groups.get(pkg, [])
        text = " ".join([pkg, STATE_LABELS[s["state"]],
                         s["summary"] or "", " ".join(pkg_groups)]).lower()
        group_cell = (f'<td class="cell">{_e(", ".join(pkg_groups)) or "—"}</td>'
                      if have_groups else "")
        group_attr = (f' data-group="{_e(",".join(pkg_groups))}"'
                      if have_groups else "")
        bodies.append(
            f'<tbody class="pkgrow" id="pkg-{_e(pkg)}" data-pkg="{_e(pkg)}"'
            f' data-state="{_e(s["state"])}"'
            f' data-statei="{STATES.index(s["state"])}"'
            f' data-days="{-1 if days is None else round(days, 2)}"'
            f' data-bugs="{stats["open"]}" data-high="{stats["high"]}"'
            f'{group_attr}'
            f' data-text="{_e(text)}">'
            "<tr>"
            f'<td class="pkg"><a href="#pkg-{_e(pkg)}">{_e(pkg)}</a></td>'
            f"<td>{_chip(s['state'])}{agehint}</td>"
            f'<td class="num cell bugs">{bugcell}</td>'
            f'{group_cell}'
            f'<td class="ver">{_e(s["ubuntu_version"]) or "—"}</td>'
            f'<td class="ver">{_e(s["debian_version"]) or "—"}</td>'
            f'<td class="ver">{_e(s["proposed_version"]) or ""}</td>'
            "</tr>"
            + _pkg_detail_row(s, detail, 7 if have_groups else 6)
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


def _sru_table(rows: list[dict]) -> str:
    if not rows:
        return ""
    body = []
    for r in sorted(rows, key=lambda r: (-(r["age_days"] or 0), r["package"])):
        bugs = " ".join(
            f'{_bug_link(b["id"])} {_sru_chip(bug_verification_status(b["cls"]))}'
            for b in r["bugs"]) or '<span class="empty">—</span>'
        age = f'{r["age_days"]:.0f}d' if r["age_days"] is not None else "—"
        body.append(
            "<tr>"
            f'<td class="pkg">{_pkg_anchor(r["package"])}</td>'
            f'<td>{_e(r["series"])}</td>'
            f'<td class="ver">{_e(r["proposed_version"])}</td>'
            f'<td class="num">{age}</td>'
            f'<td class="cell">{bugs}</td>'
            "</tr>")
    return ("<h2>Pending SRU verification "
            f"({len(rows)})</h2>"
            '<p class="empty">Team packages with an upload sitting in a '
            "stable series' -proposed pocket, from the archive's "
            "pending-sru report.</p>"
            '<div class="card scroll"><table>'
            "<tr><th>Package</th><th>Series</th><th>Proposed version</th>"
            "<th>Age</th><th>Verification bugs</th></tr>"
            + "".join(body) + "</table></div>")


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
        assignees = bug["assignees"] if bug else []
        assigned = ", ".join(assignees[:2]) if assignees else \
            '<span class="empty">unassigned</span>'
        if len(assignees) > 2:
            assigned += f" +{len(assignees) - 2}"
        rows.append(
            f'<tr><td class="num">{_bug_link(bug_id)}</td>'
            f'<td class="title">{title}</td><td>{status}</td><td>{imp}</td>'
            f'<td>{assigned}</td>'
            f'<td class="cell">{pkgs}</td></tr>')
    return (f"<h2>Gating the pipeline ({len(refs)})</h2>"
            '<p class="empty">Bugs referenced by proposed-migration '
            "(block-proposed / update-excuse) — fixing these directly "
            "unblocks migrations.</p>"
            '<div class="card scroll"><table>'
            "<tr><th>Bug</th><th>Title</th><th>Status</th><th>Importance</th>"
            "<th>Assigned</th><th>Holds up</th></tr>"
            + "".join(rows) + "</table></div>")


def render_bugs(conn: sqlite3.Connection, team: str) -> str:
    sync = db.bug_sync_state(conn, team)
    bugs = _aggregate_bugs(conn)
    refs = _pipeline_refs(conn, team)
    sru_rows = _pending_sru_rows(conn, team)

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
{_sru_table(sru_rows)}
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
{_sru_table(sru_rows)}
<h2>All bugs ({len(bugs)})</h2>
{filters}
<div class="card scroll"><table id="btable">{header}<tbody>{''.join(rows)}</tbody></table></div>
{_footer("Bug data from the anonymous Launchpad API (watermarked sync); "
         "private bugs are not visible.")}
</div></div>
<script>{_BUGS_JS}</script>
"""


# --------------------------------------------------------------------------
# KPI page (kpi.html)

def _fmt_pct(p: float | None) -> str:
    return f"{p:.0f}%" if p is not None else "—"


def _nice_ceil(v: float) -> int:
    """Smallest 1/2/5 × 10^k that is >= v — clean axis maximums."""
    if v <= 1:
        return 1
    mag = 1
    while True:
        for step in (1, 2, 5):
            if step * mag >= v:
                return step * mag
        mag *= 10


def _delta_line(delta: int, period: str, up_is_bad: bool = True) -> str:
    """Signed change vs a named period; color = direction × goodness."""
    if delta == 0:
        return f'<div class="delta">no change vs {_e(period)}</div>'
    arrow, sign = ("▲", "+") if delta > 0 else ("▼", "−")
    bad = (delta > 0) == up_is_bad
    cls = "bad" if bad else "good"
    return (f'<div class="delta {cls}">{arrow} {sign}{abs(delta)} '
            f"vs {_e(period)}</div>")


def _day_label(day) -> str:
    return f"{day.strftime('%b')} {day.day}"


def _column_path(x: float, y: float, w: float, h: float, color: str) -> str:
    """A column with a 3px rounded data-end, square at the baseline."""
    if h <= 0:
        return ""
    r = min(3.0, w / 2, h)
    return (f'<path d="M{x:.1f} {y + h:.1f} V{y + r:.1f}'
            f" Q{x:.1f} {y:.1f} {x + r:.1f} {y:.1f}"
            f" H{x + w - r:.1f}"
            f" Q{x + w:.1f} {y:.1f} {x + w:.1f} {y + r:.1f}"
            f' V{y + h:.1f} Z" fill="{color}"/>')


def _daily_bugs_chart(series: list[tuple]) -> str:
    """Grouped columns: bugs opened vs closed per day (SVG, no JS)."""
    if not series:
        return ""
    width, height, left, right, top, bottom = 960, 200, 36, 8, 12, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    ymax = _nice_ceil(max(max(o, c) for _, o, c in series))
    slot = plot_w / len(series)
    bar_w = min(10.0, max(3.0, (slot - 6) / 2))

    parts = []
    ticks = [0, ymax] if ymax % 2 else [0, ymax // 2, ymax]
    for tv in ticks:
        y = top + plot_h * (1 - tv / ymax)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}"'
                     f' y2="{y:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}"'
                     f' text-anchor="end">{tv}</text>')

    label_step = max(1, (len(series) + 5) // 6)
    for i, (day, opened, closed) in enumerate(series):
        x0 = left + i * slot
        cx = x0 + (slot - (2 * bar_w + 2)) / 2
        group = [f"<title>{_e(_day_label(day))}: {opened} opened, "
                 f"{closed} closed</title>",
                 f'<rect x="{x0:.1f}" y="{top}" width="{slot:.1f}"'
                 f' height="{plot_h}" fill="transparent"/>']
        for offset, value, color in (
                (0, opened, "var(--bar)"),
                (bar_w + 2, closed, "var(--bar2)")):
            h = plot_h * value / ymax
            group.append(_column_path(cx + offset, top + plot_h - h,
                                      bar_w, h, color))
        parts.append(f"<g>{''.join(group)}</g>")
        if i % label_step == 0:
            parts.append(f'<text x="{x0 + slot / 2:.1f}" y="{height - 8}"'
                         f' text-anchor="middle">{_e(_day_label(day))}</text>')

    rows = "".join(
        f'<tr><td>{_e(_day_label(d))}</td><td class="num">{o}</td>'
        f'<td class="num">{c}</td></tr>'
        for d, o, c in series if o or c)
    table = ('<details class="tbl"><summary>Data table (days with '
             "activity)</summary><table><tr><th>Day</th><th>Opened</th>"
             f"<th>Closed</th></tr>{rows}</table></details>") if rows else ""
    return f"""<div class="card chart">
<div class="legend">
<span><span class="sw" style="background:var(--bar)"></span>opened</span>
<span><span class="sw" style="background:var(--bar2)"></span>closed</span>
</div>
<svg viewBox="0 0 {width} {height}" role="img"
 aria-label="Bugs opened and closed per day">{''.join(parts)}</svg>
{table}</div>"""


def _backlog_chart(series: list[tuple]) -> str:
    """Open-backlog line over time (SVG, no JS)."""
    if not series:
        return ""
    width, height, left, right, top, bottom = 960, 200, 44, 46, 14, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [v for _, v in series]
    lo, hi = min(values), max(values)
    step = _nice_ceil(max(1, (hi - lo + 2) / 4))
    y0 = (lo // step) * step
    y1 = y0 + step * max(1, -(-(hi - y0) // step))
    if y1 == hi:
        y1 += step  # headroom so the line never rides the frame

    def sx(i):
        return left + plot_w * i / max(1, len(series) - 1)

    def sy(v):
        return top + plot_h * (1 - (v - y0) / (y1 - y0))

    parts = []
    for tv in range(y0, y1 + 1, step):
        y = sy(tv)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}"'
                     f' y2="{y:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}"'
                     f' text-anchor="end">{tv}</text>')

    pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}"
                   for i, (_, v) in enumerate(series))
    if y0 == 0:
        parts.append(f'<polygon points="{left},{top + plot_h} {pts} '
                     f'{width - right},{top + plot_h}" fill="var(--bar)"'
                     ' opacity="0.1"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--bar)"'
                 ' stroke-width="2" stroke-linejoin="round"'
                 ' stroke-linecap="round"/>')

    label_step = max(1, (len(series) + 4) // 5)
    for i, (day, v) in enumerate(series):
        parts.append(f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="10"'
                     ' fill="transparent">'
                     f"<title>{_e(_day_label(day))}: {v} open</title>"
                     "</circle>")
        if i % label_step == 0 and i < len(series) - 1:
            parts.append(f'<text x="{sx(i):.1f}" y="{height - 8}"'
                         f' text-anchor="middle">{_e(_day_label(day))}</text>')

    end_day, end_v = series[-1]
    parts.append(f'<circle cx="{sx(len(series) - 1):.1f}"'
                 f' cy="{sy(end_v):.1f}" r="4.5" fill="var(--bar)"'
                 ' stroke="var(--surface)" stroke-width="2"/>')
    parts.append(f'<text x="{sx(len(series) - 1) + 8:.1f}"'
                 f' y="{sy(end_v) + 4:.1f}" fill="var(--ink)"'
                 f' font-weight="600">{end_v}</text>')

    weekly = series[::-1][::7][::-1]  # every 7th day, ending today
    rows = "".join(f'<tr><td>{_e(_day_label(d))}</td>'
                   f'<td class="num">{v}</td></tr>' for d, v in weekly)
    return f"""<div class="card chart">
<svg viewBox="0 0 {width} {height}" role="img"
 aria-label="Open bug backlog over time">{''.join(parts)}</svg>
<details class="tbl"><summary>Data table (weekly)</summary>
<table><tr><th>Day</th><th>Open bugs</th></tr>{rows}</table></details></div>"""


def _rates_table(bugs: list[dict], transitions: list[dict],
                 now: datetime, history_start: str) -> str:
    rows = []
    for label, days in kpi.WINDOWS:
        br = kpi.bug_window_rates(bugs, now, days) if bugs else None
        tr = kpi.transition_window_rates(transitions, now, days)

        def _per_day(n):
            if days == 1:
                return f'<td class="num">{n}</td>'
            return (f'<td class="num">{n} <span class="agehint">'
                    f"({n / days:.1f}/d)</span></td>")

        if br is None:
            bugcells = '<td class="num">—</td>' * 3
        else:
            net = br["net"]
            if net > 0:
                netcell = f'<span class="bad">▲ +{net}</span>'
            elif net < 0:
                netcell = f'<span class="good">▼ −{-net}</span>'
            else:
                netcell = "0"
            bugcells = (_per_day(br["opened"]) + _per_day(br["closed"])
                        + f'<td class="num">{netcell}</td>')
        rows.append(
            f'<tr><td>{_e(label)} <span class="agehint">last {days}d</span>'
            f"</td>{bugcells}"
            f'<td class="num">{tr["changes"]}</td>'
            f'<td class="num">{tr["migrated"]}</td>'
            f'<td class="num">{tr["entered"]}</td></tr>')
    note = ("Bug columns come from Launchpad's own dates, so they are "
            "complete. Pipeline columns count state changes observed "
            f"between ingest runs — history begins {history_start[:10]} "
            "and these rates mature as runs accumulate.")
    return ('<div class="card scroll"><table>'
            "<tr><th>Window</th><th>Bugs opened</th><th>Bugs closed</th>"
            "<th>Net backlog</th><th>Pipeline changes</th>"
            "<th>Migrated out</th><th>Entered -proposed</th></tr>"
            + "".join(rows) + "</table></div>"
            f'<p class="empty">{_e(note)}</p>')


def _levels_table(conn: sqlite3.Connection, team: str,
                  bugs: list[dict], now: datetime) -> str:
    """Stock levels now vs 1/7/30 days ago (missing history shows —)."""
    columns = [("now", 0), ("1d ago", 1), ("7d ago", 7), ("30d ago", 30)]
    per_col = []
    for _, days in columns:
        when = now - timedelta(days=days)
        iso = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        run = db.run_at_or_before(conn, team, iso)
        pk = None
        if run is not None:
            counts = Counter(
                s["state"] for s in db.snapshots_for_run(conn, run["id"]))
            pk = kpi.package_kpis(counts)
        backlog = kpi.open_backlog_at(bugs, when) if bugs else None
        per_col.append((pk, backlog))

    def row(label, getter):
        cells = ""
        for pk, backlog in per_col:
            v = getter(pk, backlog)
            cells += f'<td class="num">{v if v is not None else "—"}</td>'
        return f"<tr><td>{_e(label)}</td>{cells}</tr>"

    rows = [
        row("In -proposed", lambda pk, b: pk and pk["in_proposed"]),
        row("Blocked", lambda pk, b: pk and pk["blocked"]),
        row("Behind Debian", lambda pk, b: pk and pk["behind"]),
        row("Current vs Debian", lambda pk, b: pk and pk["current"]),
    ]
    if bugs:
        rows.append(row("Open bugs", lambda pk, b: b))
    heads = "".join(f"<th>{_e(label)}</th>" for label, _ in columns)
    return ('<div class="card scroll"><table>'
            f"<tr><th></th>{heads}</tr>" + "".join(rows) + "</table></div>"
            '<p class="empty">Pipeline levels come from stored snapshots; '
            "columns older than the local history show —. Open bugs are "
            "reconstructed from Launchpad dates.</p>")


def _sru_kpi_section(rows: list[dict]) -> str:
    if not rows:
        return ""
    sk = kpi.sru_kpis(rows)
    med = sk["median_age_days"]
    tiles = "".join([
        _tile("Needs verification", sk["needs_verification"],
              f"of {sk['pending_rows']} pending-SRU rows"),
        _tile("Verified", sk["verified"], "all bugs verified"),
        _tile("Verification failed", sk["verification_failed"],
              "" if not sk["verification_failed"] else "needs a re-upload"),
        _tile("Removal candidates", sk["removal_candidates"],
              "" if not sk["removal_candidates"] else
              ">16 days unverified"),
        _tile("Median age awaiting verification",
              f"{med:.0f}d" if med is not None else "—",
              "of rows still needing verification"),
    ])
    oldest = []
    for r in sk["oldest"]:
        bugs = " ".join(_bug_link(b["id"]) for b in r["bugs"]) or "—"
        age = f'{r["age_days"]:.0f}d' if r["age_days"] is not None else "—"
        oldest.append(
            "<tr>"
            f'<td class="pkg">{_pkg_anchor(r["package"])}</td>'
            f'<td>{_e(r["series"])}</td>'
            f'<td class="num">{age}</td>'
            f'<td class="cell">{bugs}</td>'
            "</tr>")
    oldest_table = ("<details class=\"tbl\"><summary>Oldest rows still "
                    "awaiting verification</summary><table><tr><th>Package"
                    "</th><th>Series</th><th>Age</th><th>Bugs</th></tr>"
                    + "".join(oldest) + "</table></details>") if oldest else ""
    return (f"<h2>SRU verification queue</h2>\n<div class=\"tiles\">{tiles}"
            f"</div>\n{oldest_table}\n"
            '<p class="empty">From the archive\'s pending-sru report '
            "(team packages only); age is time since the upload entered "
            "-proposed. See <a href=\"bugs.html\">bugs.html</a> for the "
            "full per-package table.</p>")


def render_kpi(conn: sqlite3.Connection, team: str) -> str:
    run, snaps = _latest(conn, team)
    now = datetime.now(timezone.utc)
    counts = Counter(s["state"] for s in snaps)
    pk = kpi.package_kpis(counts)

    bugs = _aggregate_bugs(conn)
    transitions = [dict(t) for t in db.all_transitions(conn)]
    history_start = db.first_run_at(conn, team) or run["ran_at"]
    sru_section = _sru_kpi_section(_pending_sru_rows(conn, team))

    stuck = sum(
        1 for s in snaps if s["state"] in PROPOSED_STATES
        and (d := _days_since(db.state_entered_at(conn, s["package"])))
        is not None and d >= 7)
    pkg_tiles = "".join([
        _tile("Current vs Debian", _fmt_pct(pk["current_pct"]),
              f"{pk['current']} of {pk['total']}, incl. Ubuntu-only"),
        _tile("Behind Debian", _fmt_pct(pk["behind_pct"]),
              f"{counts.get('merge-needed', 0)} merges, "
              f"{counts.get('sync-available', 0)} syncs"),
        _tile("In -proposed", _fmt_pct(pk["proposed_pct"]),
              f"{pk['in_proposed']} packages"),
        _tile("Blocked share of -proposed",
              _fmt_pct(pk["blocked_of_proposed_pct"]),
              f"{pk['blocked']} of {pk['in_proposed']} in -proposed"),
        _tile("Stuck ≥ 7d in -proposed", stuck,
              f"{_fmt_pct(kpi.pct(stuck, pk['in_proposed']))} of -proposed"),
    ])

    if bugs:
        bk = kpi.bug_kpis(bugs, now)
        conc = kpi.bug_concentration(bugs)
        month = kpi.bug_window_rates(bugs, now, 30)
        backlog_7d = kpi.open_backlog_at(bugs, now - timedelta(days=7))
        fix_rate = kpi.pct(month["closed"], month["opened"])
        team_pkgs = {s["package"] for s in snaps}
        pkgs_with = sum(1 for p, _ in db.open_bug_counts(conn).items()
                        if p in team_pkgs)
        med = bk["median_age_days"]
        bug_tiles = "".join([
            _tile("Open bugs", bk["open"], "the synced backlog",
                  _delta_line(bk["open"] - backlog_7d, "7d ago")),
            _tile("Triaged", _fmt_pct(bk["triaged_pct"]),
                  f"{bk['triaged']} of {bk['open']} open"),
            _tile("Assigned", _fmt_pct(bk["assigned_pct"]),
                  f"{bk['assigned']} of {bk['open']} open"),
            _tile("Critical / High", _fmt_pct(bk["high_pct"]),
                  f"{bk['high']} of {bk['open']} open"),
            _tile("Active last 30d", _fmt_pct(bk["active_pct"]),
                  f"{bk['active']} of {bk['open']} open touched"),
            _tile("Fix rate, 30d", _fmt_pct(fix_rate),
                  f"{month['closed']} closed / {month['opened']} opened"),
            _tile("Median open age",
                  f"{med:.0f}d" if med is not None else "—",
                  "of open bugs, from LP created dates"),
            _tile("Packages with open bugs",
                  _fmt_pct(kpi.pct(pkgs_with, pk["total"])),
                  f"{pkgs_with} of {pk['total']} team packages"),
            _tile("Top 5 packages hold", _fmt_pct(conc["share_pct"]),
                  "of open bugs — " + ", ".join(
                      p for p, _ in conc["leaders"][:3])),
        ])
        bug_section = f"<h2>Bug health</h2>\n<div class=\"tiles\">{bug_tiles}</div>"
        charts = (
            "<h2>Bugs opened vs closed — last 30 days</h2>\n"
            + _daily_bugs_chart(kpi.daily_bug_series(bugs, 30, now))
            + "\n<h2>Open bug backlog — last 90 days</h2>\n"
            + _backlog_chart(kpi.backlog_series(bugs, 90, now))
            + '\n<p class="empty">Backlog counts the synced set (bugs '
              f"touched since {FIRST_SYNC_SINCE}); dormant older bugs are "
              "deliberately out of scope.</p>")
    else:
        bug_section = ('<h2>Bug health</h2>\n<div class="card">'
                       '<p class="empty">No bug data yet — run '
                       "<code>uplc bugs-sync</code> to add bug KPIs, rates "
                       "and trend charts to this page.</p></div>")
        charts = ""

    return f"""<title>KPIs — {_e(team)}</title>
<style>{_CSS}</style>
<div class="uplc"><div class="wrap">
{_nav('kpi.html')}
<h1>KPIs — {_e(team)}</h1>
<div class="meta">{_e(run['series'])} series · snapshot {_e(run['ran_at'])}
 · pipeline history since {_e(history_start[:10])} — rates sharpen as
 ingest runs accumulate</div>
<h2>Package set health</h2>
<div class="tiles">{pkg_tiles}</div>
{bug_section}
{sru_section}
<h2>Rate of change</h2>
{_rates_table(bugs, transitions, now, history_start)}
<h2>Levels — now vs then</h2>
{_levels_table(conn, team, bugs, now)}
{charts}
{_footer()}
</div></div>
"""


# --------------------------------------------------------------------------
# Digest page (digest.html) — blog-style archive of stored digests + feeds

_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_LIST_ITEM = re.compile(r"(?:[-*]|\d+\.) (.*)")
_MD_TABLE_SEP = re.compile(r":?-+:?")


def _md_inline(text: str) -> str:
    out = _e(text)
    out = _MD_CODE.sub(r"<code>\1</code>", out)
    out = _MD_BOLD.sub(r"<strong>\1</strong>", out)
    out = _MD_ITALIC.sub(r"<em>\1</em>", out)
    out = _MD_LINK.sub(r'<a href="\2">\1</a>', out)
    return out


def _md_html(md: str) -> str:
    """Markdown → HTML for the digest subset (pure, stdlib only).

    Headings, hr, blockquotes, pipe tables, flat lists, inline
    bold/italic/code/links. Anything unrecognized degrades to a
    paragraph — content is never dropped. Input is escaped before the
    inline pass, so only generated tags reach the page.
    """
    out: list[str] = []
    para: list[str] = []
    lines = md.splitlines()

    def flush():
        if para:
            out.append("<p>" + " ".join(_md_inline(x) for x in para) + "</p>")
            para.clear()

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            flush()
            i += 1
        elif s.startswith("### "):
            flush()
            out.append(f"<h3>{_md_inline(s[4:])}</h3>")
            i += 1
        elif s.startswith("## "):
            flush()
            out.append(f"<h2>{_md_inline(s[3:])}</h2>")
            i += 1
        elif s.startswith("# "):
            flush()
            out.append(f"<h1>{_md_inline(s[2:])}</h1>")
            i += 1
        elif s in ("---", "***"):
            flush()
            out.append("<hr>")
            i += 1
        elif s.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            inner = " ".join(_md_inline(q) for q in quote if q)
            out.append(f"<blockquote><p>{inner}</p></blockquote>")
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip()
                         for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            header = None
            body = rows
            if len(rows) >= 2 and all(
                    _MD_TABLE_SEP.fullmatch(c) for c in rows[1]):
                header, body = rows[0], rows[2:]
            parts = ['<div class="scroll"><table>']
            if header:
                parts.append("<tr>" + "".join(
                    f"<th>{_md_inline(c)}</th>" for c in header) + "</tr>")
            for r in body:
                parts.append("<tr>" + "".join(
                    f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>")
            parts.append("</table></div>")
            out.append("".join(parts))
        elif _MD_LIST_ITEM.match(s):
            flush()
            tag = "ol" if s[0].isdigit() else "ul"
            items = []
            while i < len(lines):
                m = _MD_LIST_ITEM.match(lines[i].strip())
                if not m:
                    break
                items.append(f"<li>{_md_inline(m.group(1))}</li>")
                i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
        else:
            para.append(s)
            i += 1
    flush()
    return "".join(out)


# Open the <details> a feed permalink points at; degrades to plain anchors.
_DIGEST_JS = """
(function () {
  function reveal() {
    var el = document.getElementById(location.hash.slice(1));
    if (el && el.tagName === 'DETAILS') el.open = true;
  }
  window.addEventListener('hashchange', reveal);
  reveal();
})();
"""


def _digest_summary(row) -> str:
    d = datetime.strptime(row["date"], "%Y-%m-%d")
    label = f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"
    note = "" if row["used_llm"] else " · deterministic"
    return f"{label} — {row['bug_count'] or 0} bugs{note}"


def render_digest(conn: sqlite3.Connection, team: str) -> str:
    rows = db.digest_runs(conn, team)
    if not rows:
        body = ('<p class="empty">no digests yet — run '
                "<code>uplc digest</code> after a bugs-sync</p>")
    else:
        chunks = []
        month = None
        for idx, row in enumerate(rows):
            if row["date"][:7] != month:
                month = row["date"][:7]
                label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
                chunks.append(f"<h2>{_e(label)}</h2>")
            anchor = f"digest-{_e(row['date'])}"
            article = (f'<article class="digest-md">'
                       f"{_md_html(row['body'] or '')}</article>")
            if idx == 0:
                chunks.append(f'<div id="{anchor}">{article}</div>')
            else:
                chunks.append(
                    f'<details class="digest" id="{anchor}">'
                    f"<summary>{_e(_digest_summary(row))}</summary>"
                    f"{article}</details>")
        body = "".join(chunks)
    return f"""<title>Digest — {_e(team)}</title>
<style>{_CSS}</style>
<div class="uplc"><div class="wrap">
{_nav('digest.html')}
<h1>Daily digest — {_e(team)}</h1>
<div class="subscribe">Subscribe: point a feed reader at
 <a href="feed.json">feed.json</a> (JSON&nbsp;Feed) or
 <a href="feed.xml">feed.xml</a> (Atom); new digests appear on every
 publish. Each entry links back to its spot on this page.</div>
{body}
{_footer("Digest narratives are LLM-assisted from a facts document; "
         "the summary tables and links come straight from the data.")}
</div></div>
<script>{_DIGEST_JS}</script>
"""


_FEED_LIMIT = 20


def _tag_uri(team: str, date: str) -> str:
    # Base-URL-independent, stable entry ids: feeds validate even before
    # the dashboard has a public URL.
    return f"tag:uplc.local,2026:{team}:digest-{date}"


def render_feed_json(conn: sqlite3.Connection, team: str,
                     base_url: str = "") -> str:
    base = base_url.rstrip("/")
    items = []
    for row in db.digest_runs(conn, team, limit=_FEED_LIMIT):
        item = {
            "id": _tag_uri(team, row["date"]),
            "title": f"{team} bugs digest — {row['date']}",
            "date_published": row["ran_at"],
            "content_html": _md_html(row["body"] or ""),
        }
        if base:
            item["url"] = f"{base}/digest.html#digest-{row['date']}"
        items.append(item)
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": f"{team} bugs digest",
        "description": "Daily curated digest of team bug activity and "
                       "package pipeline events, generated by uplc.",
        "items": items,
    }
    if base:
        feed["home_page_url"] = f"{base}/digest.html"
        feed["feed_url"] = f"{base}/feed.json"
    return json.dumps(feed, indent=1)


def render_feed_atom(conn: sqlite3.Connection, team: str,
                     base_url: str = "") -> str:
    base = base_url.rstrip("/")
    rows = db.digest_runs(conn, team, limit=_FEED_LIMIT)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = rows[0]["ran_at"] if rows else now
    entries = []
    for row in rows:
        title = f"{team} bugs digest — {row['date']}"
        link = (f'<link href="{_xml_escape(base)}/digest.html'
                f'#digest-{row["date"]}"/>' if base else "")
        entries.append(
            "<entry>"
            f"<id>{_xml_escape(_tag_uri(team, row['date']))}</id>"
            f"<title>{_xml_escape(title)}</title>"
            f"<updated>{_xml_escape(row['ran_at'])}</updated>"
            f"{link}"
            f'<content type="html">'
            f"{_xml_escape(_md_html(row['body'] or ''))}</content>"
            "</entry>")
    self_link = (f'<link rel="self" href="{_xml_escape(base)}/feed.xml"/>'
                 if base else "")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<id>{_xml_escape(_tag_uri(team, 'feed'))}</id>"
        f"<title>{_xml_escape(team)} bugs digest</title>"
        f"<updated>{_xml_escape(updated)}</updated>"
        f"{self_link}"
        f"{''.join(entries)}"
        "</feed>")


PAGES = {
    "index.html": render_index,
    "packages.html": render_packages,
    "bugs.html": render_bugs,
    "kpi.html": render_kpi,
    "digest.html": render_digest,
}

# Backwards-compatible name for the single-page overview.
render = render_index
