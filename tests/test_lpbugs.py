import unittest
from pathlib import Path
from unittest import mock

from uplc import db, lpbugs
from uplc.fetch import SourceUnavailable
from uplc.state import PackageState

API = lpbugs.LP_API


def _task_entry(bug_id, target, status="Triaged", importance="High",
                assignee=None, date_closed=None):
    return {
        "bug_link": f"{API}/bugs/{bug_id}",
        "target_link": f"{API}/{target}",
        "status": status,
        "importance": importance,
        "assignee_link": f"{API}/~{assignee}" if assignee else None,
        "date_created": "2026-02-01T00:00:00+00:00",
        "date_closed": date_closed,
    }


def _bug_entry(bug_id, updated, title="a bug", tags=()):
    return {
        "id": bug_id,
        "title": title,
        "tags": list(tags),
        "date_created": "2026-01-15T00:00:00+00:00",
        "date_last_updated": updated,
        "heat": 6,
    }


class TestParsing(unittest.TestCase):
    def test_bug_id_from_link(self):
        self.assertEqual(lpbugs.bug_id_from_link(f"{API}/bugs/2043210"), 2043210)
        self.assertIsNone(lpbugs.bug_id_from_link(f"{API}/bugs/"))

    def test_parse_target_devel_task(self):
        self.assertEqual(
            lpbugs.parse_target(f"{API}/ubuntu/+source/glibc"), ("glibc", ""))

    def test_parse_target_series_task(self):
        self.assertEqual(
            lpbugs.parse_target(f"{API}/ubuntu/noble/+source/glibc"),
            ("glibc", "noble"))

    def test_parse_target_distribution_task(self):
        self.assertEqual(lpbugs.parse_target(f"{API}/ubuntu"), ("", ""))

    def test_parse_target_non_ubuntu(self):
        self.assertIsNone(lpbugs.parse_target(f"{API}/glibc"))
        self.assertIsNone(lpbugs.parse_target(f"{API}/debian/+source/glibc"))

    def test_parse_task(self):
        task = lpbugs.parse_task(
            _task_entry(7, "ubuntu/+source/grub2", assignee="jdoe"))
        self.assertEqual(task["bug_id"], 7)
        self.assertEqual(task["package"], "grub2")
        self.assertEqual(task["series"], "")
        self.assertEqual(task["assignee"], "jdoe")
        self.assertEqual(task["date_closed"], "")

    def test_parse_task_skips_non_ubuntu(self):
        entry = _task_entry(7, "grub2")
        self.assertIsNone(lpbugs.parse_task(entry))

    def test_watermark_query_date_overlaps(self):
        since = lpbugs._watermark_query_date("2026-07-01T12:00:00+00:00")
        self.assertEqual(since, "2026-07-01T11:00:00+00:00")


class TestSync(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(":memory:"))
        patcher = mock.patch.object(lpbugs.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_sync_then_incremental(self):
        touched_entries = [
            _task_entry(100, "ubuntu/+source/glibc"),
            _task_entry(100, "ubuntu/noble/+source/glibc",
                        status="Fix Committed"),
            _task_entry(101, "ubuntu/+source/grub2", status="Fix Released",
                        date_closed="2026-03-01T00:00:00+00:00"),
        ]
        bugs = {
            f"{API}/bugs/100": _bug_entry(
                100, "2026-06-01T00:00:00+00:00", tags=["rls-nn-incoming"]),
            f"{API}/bugs/101": _bug_entry(101, "2026-03-01T00:00:00+00:00"),
        }
        with mock.patch.object(
                lpbugs, "search_tasks",
                return_value=(touched_entries, True)) as search, \
             mock.patch.object(lpbugs, "_cached_json",
                               side_effect=lambda url: bugs[url]):
            result = lpbugs.sync(self.conn, "foundations-bugs")

        self.assertTrue(result.first_sync)
        self.assertEqual(result.bugs_synced, 2)
        self.assertEqual(result.watermark, "2026-06-01T00:00:00+00:00")
        # One search only: touched-since window, all statuses. Dormant
        # open bugs are deliberately not swept.
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.kwargs["modified_since"],
                         lpbugs.FIRST_SYNC_SINCE)
        self.assertEqual(search.call_args.kwargs["statuses"],
                         lpbugs.ALL_BUG_STATUSES)

        rows = self.conn.execute(
            "SELECT * FROM bug_tasks WHERE bug_id = 100").fetchall()
        self.assertEqual({(r["package"], r["series"]) for r in rows},
                         {("glibc", ""), ("glibc", "noble")})
        self.assertEqual(db.open_bug_counts(self.conn),
                         {"glibc": {"open": 1, "high": 1}})

        # Incremental: watermark (minus overlap) drives the search.
        newer = _bug_entry(101, "2026-07-01T00:00:00+00:00")
        with mock.patch.object(
                lpbugs, "search_tasks",
                return_value=([_task_entry(101, "ubuntu/+source/grub2")],
                              True)) as search, \
             mock.patch.object(lpbugs, "_cached_json", return_value=newer):
            result = lpbugs.sync(self.conn, "foundations-bugs")
        self.assertFalse(result.first_sync)
        self.assertEqual(search.call_args.kwargs["modified_since"],
                         "2026-05-31T23:00:00+00:00")
        self.assertEqual(result.watermark, "2026-07-01T00:00:00+00:00")

    def test_pipeline_bugs_fetched_by_id(self):
        run_states = [PackageState(package="openssl", state="blocked-other",
                                   bugs=["555"])]
        db.record_run(self.conn, run_states, team="foundations-bugs",
                      series="devel")
        resources = {
            f"{API}/bugs/555": _bug_entry(555, "2025-11-01T00:00:00+00:00"),
            f"{API}/bugs/555/bug_tasks": {
                "entries": [_task_entry(555, "ubuntu/+source/openssl")]},
        }
        with mock.patch.object(lpbugs, "search_tasks",
                               return_value=([], True)), \
             mock.patch.object(lpbugs, "_cached_json",
                               side_effect=lambda url: resources[url]):
            result = lpbugs.sync(self.conn, "foundations-bugs")
        self.assertEqual(result.pipeline_fetched, 1)
        row = self.conn.execute(
            "SELECT origin FROM bugs WHERE id = 555").fetchone()
        self.assertEqual(row["origin"], "pipeline")
        # Pipeline fetches never advance the watermark stream.
        self.assertEqual(result.watermark, "")
        self.assertIsNone(db.bug_sync_state(self.conn, "foundations-bugs"))

    def test_failed_fetch_holds_watermark(self):
        db.set_bug_sync_state(self.conn, "foundations-bugs",
                              "2026-05-01T00:00:00+00:00")
        with mock.patch.object(
                lpbugs, "search_tasks",
                return_value=([_task_entry(9, "ubuntu/+source/glibc")], True)), \
             mock.patch.object(lpbugs, "_cached_json",
                               side_effect=SourceUnavailable("boom")):
            result = lpbugs.sync(self.conn, "foundations-bugs")
        self.assertEqual(result.failed, [9])
        state = db.bug_sync_state(self.conn, "foundations-bugs")
        self.assertEqual(state["watermark"], "2026-05-01T00:00:00+00:00")

    def test_interrupted_search_keeps_partial_and_holds_watermark(self):
        db.set_bug_sync_state(self.conn, "foundations-bugs",
                              "2026-05-01T00:00:00+00:00")
        bug = _bug_entry(9, "2026-07-01T00:00:00+00:00")
        with mock.patch.object(
                lpbugs, "search_tasks",
                return_value=([_task_entry(9, "ubuntu/+source/glibc")],
                              False)), \
             mock.patch.object(lpbugs, "_cached_json", return_value=bug):
            result = lpbugs.sync(self.conn, "foundations-bugs")
        self.assertFalse(result.search_complete)
        # The bugs from completed pages are still ingested...
        self.assertEqual(result.bugs_synced, 1)
        self.assertEqual(db.bug_count(self.conn), 1)
        # ...but the watermark holds so the gap is re-covered next run.
        state = db.bug_sync_state(self.conn, "foundations-bugs")
        self.assertEqual(state["watermark"], "2026-05-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
