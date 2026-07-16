import unittest
from pathlib import Path

from uplc import db


def _bug(bug_id, **kw):
    base = {"id": bug_id, "title": "t", "tags": [],
            "date_created": "2026-01-01T00:00:00+00:00",
            "date_last_updated": "2026-06-01T00:00:00+00:00",
            "heat": 0, "origin": "subscription"}
    base.update(kw)
    return base


class TestBugPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(":memory:"))

    def test_record_bug_replaces_tasks(self):
        db.record_bug(self.conn, _bug(1), [
            {"package": "glibc", "series": "", "status": "New",
             "importance": "High"},
            {"package": "glibc", "series": "noble", "status": "Triaged",
             "importance": "High"},
        ])
        db.record_bug(self.conn, _bug(1, title="renamed"), [
            {"package": "glibc", "series": "", "status": "Fix Released",
             "importance": "High"},
        ])
        rows = self.conn.execute(
            "SELECT * FROM bug_tasks WHERE bug_id = 1").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "Fix Released")
        title = self.conn.execute(
            "SELECT title FROM bugs WHERE id = 1").fetchone()["title"]
        self.assertEqual(title, "renamed")

    def test_subscription_origin_is_sticky(self):
        db.record_bug(self.conn, _bug(2, origin="subscription"), [])
        db.record_bug(self.conn, _bug(2, origin="pipeline"), [])
        row = self.conn.execute("SELECT origin FROM bugs WHERE id = 2").fetchone()
        self.assertEqual(row["origin"], "subscription")
        # But a pipeline-first bug upgrades when the subscription sees it.
        db.record_bug(self.conn, _bug(3, origin="pipeline"), [])
        db.record_bug(self.conn, _bug(3, origin="subscription"), [])
        row = self.conn.execute("SELECT origin FROM bugs WHERE id = 3").fetchone()
        self.assertEqual(row["origin"], "subscription")

    def test_open_bug_counts_distinct_per_package(self):
        # One bug, two series tasks on the same package: counts once.
        db.record_bug(self.conn, _bug(4), [
            {"package": "grub2", "series": "", "status": "Triaged",
             "importance": "Critical"},
            {"package": "grub2", "series": "noble", "status": "In Progress",
             "importance": "Critical"},
        ])
        # A closed bug does not count.
        db.record_bug(self.conn, _bug(5), [
            {"package": "grub2", "series": "", "status": "Fix Released",
             "importance": "High"},
        ])
        self.assertEqual(db.open_bug_counts(self.conn),
                         {"grub2": {"open": 1, "high": 1}})
        self.assertEqual(db.open_bug_total(self.conn), 1)
        self.assertEqual(db.bug_count(self.conn), 2)


if __name__ == "__main__":
    unittest.main()
