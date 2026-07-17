import json
import unittest
from pathlib import Path

from uplc import db
from uplc.state import PackageState


def _row(package, series="focal", proposed="1.2-1ubuntu1", age=5.0, bugs=()):
    return {
        "package": package, "series": series, "proposed_version": proposed,
        "release_version": "1.1-1", "update_version": "", "uploaders": "x",
        "age_days": age, "url": "", "bugs": list(bugs),
    }


class TestPendingSruPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(":memory:"))
        self.run_id = db.record_run(
            self.conn, [PackageState(package="flashrom", state="in-sync")],
            team="foundations-bugs", series="devel")

    def test_record_and_fetch_for_run(self):
        db.record_pending_sru(self.conn, self.run_id, [
            _row("flashrom", bugs=[{"id": 1, "description": "d",
                                    "cls": "verified", "tags": [], "url": ""}]),
        ])
        rows = db.pending_sru_for_run(self.conn, self.run_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["package"], "flashrom")
        bugs = json.loads(rows[0]["bugs"])
        self.assertEqual(bugs[0]["id"], 1)

    def test_latest_pending_sru_uses_latest_run(self):
        db.record_pending_sru(self.conn, self.run_id, [_row("flashrom")])
        run2 = db.record_run(
            self.conn, [PackageState(package="flashrom", state="in-sync")],
            team="foundations-bugs", series="devel")
        db.record_pending_sru(self.conn, run2, [_row("knot", series="jammy")])
        rows = db.latest_pending_sru(self.conn, "foundations-bugs")
        self.assertEqual([r["package"] for r in rows], ["knot"])

    def test_latest_pending_sru_no_runs(self):
        conn = db.connect(Path(":memory:"))
        self.assertEqual(db.latest_pending_sru(conn, "nope"), [])

    def test_multiple_series_same_package(self):
        db.record_pending_sru(self.conn, self.run_id, [
            _row("flashrom", series="focal"),
            _row("flashrom", series="jammy"),
        ])
        rows = db.pending_sru_for_run(self.conn, self.run_id)
        self.assertEqual({r["series"] for r in rows}, {"focal", "jammy"})


if __name__ == "__main__":
    unittest.main()
