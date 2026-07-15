"""uplc command line interface."""

import argparse
import logging
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


def _wrap_page(body: str) -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,"
            " initial-scale=1'></head><body style='margin:0'>"
            f"{body}</body></html>")


def _cmd_html(args) -> int:
    from .htmlreport import render
    conn = db.connect(args.db)
    args.output.write_text(_wrap_page(render(conn, args.team)))
    print(f"wrote {args.output}")
    return 0


def _cmd_serve(args) -> int:
    import http.server
    import tempfile

    from .htmlreport import render

    conn = db.connect(args.db)
    tmpdir = Path(tempfile.mkdtemp(prefix="uplc-"))
    (tmpdir / "index.html").write_text(_wrap_page(render(conn, args.team)))

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

    p = sub.add_parser("html", help="write the self-contained dashboard")
    _add_common(p)
    p.add_argument("-o", "--output", type=Path,
                   default=Path("dashboard.html"))
    p.set_defaults(func=_cmd_html)

    p = sub.add_parser("serve", help="serve the dashboard over local HTTP")
    _add_common(p)
    p.add_argument("-p", "--port", type=int, default=8321)
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
