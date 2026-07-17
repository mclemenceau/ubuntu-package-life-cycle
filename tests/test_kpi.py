import unittest
from collections import Counter
from datetime import datetime, timezone

from uplc import kpi

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _bug(created, closed="", open_=True, status="Triaged",
         importance="Medium", assignees=(), updated=None, packages=("pkg",)):
    return {
        "created": created, "closed": closed, "open": open_,
        "status": status, "importance": importance,
        "assignees": list(assignees),
        "updated": updated or (closed or created),
        "packages": list(packages),
    }


class TestPackageKpis(unittest.TestCase):
    def test_percentages_and_counts(self):
        counts = Counter({
            "in-sync": 5, "ubuntu-only": 1, "merge-needed": 2,
            "sync-available": 1, "blocked-tests": 2, "waiting-age": 1,
        })
        pk = kpi.package_kpis(counts)
        self.assertEqual(pk["total"], 12)
        self.assertEqual(pk["current"], 6)
        self.assertAlmostEqual(pk["current_pct"], 50.0)
        self.assertEqual(pk["behind"], 3)
        self.assertEqual(pk["in_proposed"], 3)
        self.assertEqual(pk["blocked"], 2)
        self.assertAlmostEqual(pk["blocked_of_proposed_pct"], 100 * 2 / 3)

    def test_empty_set_yields_none_percentages(self):
        pk = kpi.package_kpis(Counter())
        self.assertIsNone(pk["current_pct"])
        self.assertIsNone(pk["blocked_of_proposed_pct"])


class TestBugKpis(unittest.TestCase):
    def test_triage_assignment_and_activity(self):
        bugs = [
            _bug("2026-01-01T00:00:00+00:00", status="New",
                 importance="Undecided",
                 updated="2026-07-10T00:00:00+00:00"),
            _bug("2026-02-01T00:00:00+00:00", importance="High",
                 assignees=["dev"], updated="2026-03-01T00:00:00+00:00"),
            _bug("2026-03-01T00:00:00+00:00",
                 closed="2026-04-01T00:00:00+00:00", open_=False),
        ]
        bk = kpi.bug_kpis(bugs, NOW)
        self.assertEqual(bk["open"], 2)
        self.assertEqual(bk["triaged"], 1)
        self.assertAlmostEqual(bk["triaged_pct"], 50.0)
        self.assertEqual(bk["assigned"], 1)
        self.assertEqual(bk["high"], 1)
        self.assertEqual(bk["active"], 1)  # only the July-touched bug
        # ages: Jan 1 (197d) and Feb 1 (166d) -> median between them
        self.assertAlmostEqual(bk["median_age_days"], (197 + 166) / 2)

    def test_no_bugs(self):
        bk = kpi.bug_kpis([], NOW)
        self.assertEqual(bk["open"], 0)
        self.assertIsNone(bk["triaged_pct"])
        self.assertIsNone(bk["median_age_days"])


class TestWindows(unittest.TestCase):
    def test_bug_window_rates(self):
        bugs = [
            _bug("2026-07-17T01:00:00+00:00"),                # today
            _bug("2026-07-12T00:00:00+00:00"),                # this week
            _bug("2026-06-25T00:00:00+00:00",                 # this month
                 closed="2026-07-16T00:00:00+00:00", open_=False),
            _bug("2026-01-05T00:00:00+00:00"),                # old
        ]
        day = kpi.bug_window_rates(bugs, NOW, 1)
        self.assertEqual((day["opened"], day["closed"]), (1, 0))
        week = kpi.bug_window_rates(bugs, NOW, 7)
        self.assertEqual((week["opened"], week["closed"]), (2, 1))
        month = kpi.bug_window_rates(bugs, NOW, 30)
        self.assertEqual((month["opened"], month["closed"], month["net"]),
                         (3, 1, 2))

    def test_transition_window_rates(self):
        transitions = [
            {"from_state": "waiting-age", "to_state": "in-sync",
             "first_seen_new": "2026-07-17T05:00:00+00:00"},   # migrated
            {"from_state": "merge-needed", "to_state": "blocked-tests",
             "first_seen_new": "2026-07-16T00:00:00+00:00"},   # entered
            {"from_state": "blocked-tests", "to_state": "waiting-age",
             "first_seen_new": "2026-07-15T00:00:00+00:00"},   # internal
            {"from_state": "waiting-age", "to_state": "not-in-devel",
             "first_seen_new": "2026-07-15T00:00:00+00:00"},   # removal
        ]
        day = kpi.transition_window_rates(transitions, NOW, 1)
        self.assertEqual(day, {"changes": 1, "migrated": 1, "entered": 0})
        week = kpi.transition_window_rates(transitions, NOW, 7)
        self.assertEqual(week["changes"], 4)
        self.assertEqual(week["migrated"], 1)  # not-in-devel is no migration
        self.assertEqual(week["entered"], 1)


class TestBacklog(unittest.TestCase):
    def test_backlog_reconstruction(self):
        bugs = [
            _bug("2026-06-01T00:00:00+00:00"),
            _bug("2026-06-10T00:00:00+00:00",
                 closed="2026-07-01T00:00:00+00:00", open_=False),
            # Closed with no close date: excluded at every instant.
            _bug("2026-06-15T00:00:00+00:00", open_=False),
        ]
        june20 = datetime(2026, 6, 20, tzinfo=timezone.utc)
        self.assertEqual(kpi.open_backlog_at(bugs, june20), 2)
        self.assertEqual(kpi.open_backlog_at(bugs, NOW), 1)

    def test_backlog_series_shape(self):
        series = kpi.backlog_series([_bug("2026-07-10T00:00:00+00:00")],
                                    5, NOW)
        self.assertEqual(len(series), 5)
        self.assertEqual(series[-1][0], NOW.date())
        self.assertTrue(all(v == 1 for _, v in series))


class TestSeriesAndConcentration(unittest.TestCase):
    def test_daily_series_counts_and_order(self):
        bugs = [
            _bug("2026-07-16T08:00:00+00:00"),
            _bug("2026-07-16T09:00:00+00:00"),
            _bug("2026-07-01T00:00:00+00:00",
                 closed="2026-07-15T00:00:00+00:00", open_=False),
        ]
        series = kpi.daily_bug_series(bugs, 3, NOW)
        self.assertEqual([d.day for d, _, _ in series], [15, 16, 17])
        self.assertEqual([(o, c) for _, o, c in series],
                         [(0, 1), (2, 0), (0, 0)])

    def test_concentration(self):
        bugs = ([_bug("2026-01-01T00:00:00+00:00", packages=("a",))] * 3
                + [_bug("2026-01-01T00:00:00+00:00", packages=("b",))]
                + [_bug("2026-01-01T00:00:00+00:00", packages=(),)])
        conc = kpi.bug_concentration(bugs, top=1)
        self.assertEqual(conc["leaders"], [("a", 3)])
        self.assertAlmostEqual(conc["share_pct"], 75.0)
        self.assertEqual(conc["packages_with_bugs"], 2)


if __name__ == "__main__":
    unittest.main()
