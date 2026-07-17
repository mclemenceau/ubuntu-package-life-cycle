"""uplc command line interface."""

import argparse
import logging
import os
import sys
from pathlib import Path

from . import db, report

DEFAULT_TEAM = "foundations-bugs"


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--team", default=DEFAULT_TEAM,
                        help=f"package team (default: {DEFAULT_TEAM})")
    parser.add_argument("--db", type=Path, default=None,
                        help="sqlite database path (default: XDG data dir)")


def _cmd_ingest(args) -> int:
    from .ingest import run_ingest
    result = run_ingest(args.team, args.db)
    print(f"run #{result.run_id}: {len(result.states)} packages, "
          f"series {result.series}, excuses {result.excuses_generated}")
    if not result.mom_available:
        print("note: Merge-o-Matic unreachable, used Sources comparison only")
    if not result.sru_available:
        print("note: pending-SRU report unreachable, digest SRU events will "
              "use the stable-series-task approximation")
    return 0


def _cmd_bugs_sync(args) -> int:
    from .lpbugs import sync
    conn = db.connect(args.db)
    result = sync(conn, args.team)
    kind = "first sync" if result.first_sync else "incremental sync"
    print(f"{kind}: {result.bugs_synced} bugs updated, "
          f"{result.pipeline_fetched} pipeline bugs fetched, "
          f"watermark {result.watermark or '(not set)'}")
    if result.failed:
        print(f"warning: {len(result.failed)} bugs failed to fetch; "
              "they will be retried on the next sync")
    if not result.search_complete:
        print("warning: search was interrupted by Launchpad; partial "
              "results kept, the next sync re-covers the gap")
    return 0


def _cmd_status(args) -> int:
    conn = db.connect(args.db)
    print(report.status(conn, args.team))
    return 0


def _cmd_stuck(args) -> int:
    conn = db.connect(args.db)
    print(report.stuck(conn, args.team, args.days))
    return 0


def _cmd_blockers(args) -> int:
    conn = db.connect(args.db)
    print(report.blockers(conn, args.team))
    return 0


def _cmd_digest(args) -> int:
    from . import digest
    conn = db.connect(args.db)
    result = digest.generate(conn, args.team, since=args.since,
                             no_llm=args.no_llm, llm_cmd=args.llm_cmd)
    how = "LLM narrative" if result.used_llm else "deterministic fallback"
    if args.stdout:
        print(result.body)
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        path = args.output / f"digest-{result.date}.md"
        path.write_text(result.body)
        print(f"wrote {path} ({result.bug_count} bugs, {how})")
    return 0


def _wrap_page(body: str, *, feeds: bool = True) -> str:
    links = ""
    if feeds:
        links = ("<link rel='alternate' type='application/feed+json' "
                 "title='digest' href='feed.json'>"
                 "<link rel='alternate' type='application/atom+xml' "
                 "title='digest' href='feed.xml'>")
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,"
            f" initial-scale=1'>{links}</head><body style='margin:0'>"
            f"{body}</body></html>")


def _write_site(conn, team: str, outdir: Path, base_url: str = "") -> list[Path]:
    from .htmlreport import PAGES, render_feed_atom, render_feed_json
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, renderer in PAGES.items():
        path = outdir / name
        path.write_text(_wrap_page(renderer(conn, team)))
        written.append(path)
    for name, renderer in (("feed.json", render_feed_json),
                           ("feed.xml", render_feed_atom)):
        path = outdir / name
        path.write_text(renderer(conn, team, base_url))
        written.append(path)
    return written


def _cmd_html(args) -> int:
    conn = db.connect(args.db)
    base_url = args.base_url or os.environ.get("UPLC_BASE_URL", "")
    if args.output.suffix == ".html":
        # Single-file compatibility mode: just the overview page.
        from .htmlreport import render_index
        args.output.write_text(
            _wrap_page(render_index(conn, args.team), feeds=False))
        print(f"wrote {args.output} (overview only; "
              "use a directory for all pages)")
        return 0
    for path in _write_site(conn, args.team, args.output, base_url):
        print(f"wrote {path}")
    return 0


def _cmd_serve(args) -> int:
    import http.server
    import tempfile

    conn = db.connect(args.db)
    tmpdir = Path(tempfile.mkdtemp(prefix="uplc-"))
    _write_site(conn, args.team, tmpdir)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmpdir), **kw)

    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as srv:
        print(f"serving dashboard on http://127.0.0.1:{args.port}/ (Ctrl-C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="uplc",
        description="Observe a team's Ubuntu packages across the "
                    "development pipeline without hammering Launchpad.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="fetch sources, snapshot current state")
    _add_common(p)
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("bugs-sync",
                       help="sync team bugs from Launchpad (watermarked; "
                            "first run fetches everything touched this "
                            "year)")
    _add_common(p)
    p.set_defaults(func=_cmd_bugs_sync)

    p = sub.add_parser("digest",
                       help="write the daily curated digest (LLM narrative "
                            "with deterministic fallback)")
    _add_common(p)
    p.add_argument("-o", "--output", type=Path, default=Path("."),
                   help="directory for digest-YYYY-MM-DD.md (default: .)")
    p.add_argument("--since", default=None,
                   help="ISO window start (default: where the previous "
                        "digest ended, or today 00:00 UTC)")
    p.add_argument("--no-llm", action="store_true",
                   help="skip the LLM, render the deterministic digest")
    p.add_argument("--llm-cmd", default=None,
                   help="narrative command (default: $UPLC_LLM_CMD or "
                        "'claude -p'; gets the prompt as last argument, "
                        "facts JSON on stdin, prints Markdown)")
    p.add_argument("--stdout", action="store_true",
                   help="print the digest instead of writing a file")
    p.set_defaults(func=_cmd_digest)

    p = sub.add_parser("status", help="funnel summary + proposed pipeline")
    _add_common(p)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("stuck", help="blocked packages, oldest first")
    _add_common(p)
    p.add_argument("--days", type=float, default=0,
                   help="only show items stuck at least this many days")
    p.set_defaults(func=_cmd_stuck)

    p = sub.add_parser("blockers", help="migrations blocking the most packages")
    _add_common(p)
    p.set_defaults(func=_cmd_blockers)

    p = sub.add_parser("html", help="write the self-contained dashboard site")
    _add_common(p)
    p.add_argument("-o", "--output", type=Path, default=Path("dashboard"),
                   help="output directory for all pages, or a .html file "
                        "for the overview page only (default: dashboard/)")
    p.add_argument("--base-url", default=None,
                   help="public URL of the site, used for links in the "
                        "digest feeds (default: $UPLC_BASE_URL)")
    p.set_defaults(func=_cmd_html)

    p = sub.add_parser("serve", help="serve the dashboard over local HTTP")
    _add_common(p)
    p.add_argument("-p", "--port", type=int, default=8321)
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        # Step-level ingest progress stays visible so long fetches don't
        # look like a hang; -v adds per-URL fetch and parse detail.
        logging.getLogger("uplc.ingest").setLevel(logging.INFO)
        logging.getLogger("uplc.lpbugs").setLevel(logging.INFO)
        logging.getLogger("uplc.digest").setLevel(logging.INFO)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
