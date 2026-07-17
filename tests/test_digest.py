import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uplc import db, digest, lpbugs

API = lpbugs.LP_API


def _message(owner="jdoe", date="2026-07-17T10:00:00+00:00",
             subject="Re: a bug", content="some analysis"):
    return {
        "owner_link": f"{API}/~{owner}",
        "date_created": date,
        "subject": subject,
        "content": content,
    }


def _activity(person="jdoe", date="2026-07-17T09:00:00+00:00",
              what="glibc (Ubuntu): status", old="New", new="Triaged"):
    return {
        "person_link": f"{API}/~{person}",
        "datechanged": date,
        "whatchanged": what,
        "oldvalue": old,
        "newvalue": new,
    }


def _snap(package, state="in-sync", ubuntu="1.0-1", debian="1.0-1",
          proposed="", regressions=(), missing_builds=(), age=None):
    return {
        "package": package,
        "state": state,
        "summary": state,
        "ubuntu_version": ubuntu,
        "debian_version": debian,
        "proposed_version": proposed,
        "age_days": age,
        "detail": {
            "blocked_by": [],
            "regressions": list(regressions),
            "missing_builds": list(missing_builds),
            "bugs": [],
        },
    }


def _bug_row(bug_id, package="glibc", series="", status="New",
             importance="Undecided", assignee="",
             created="2026-07-10T00:00:00+00:00",
             updated="2026-07-17T10:00:00+00:00", date_closed=""):
    return {
        "id": bug_id,
        "title": f"bug {bug_id}",
        "tags": "[]",
        "date_created": created,
        "date_last_updated": updated,
        "heat": 6,
        "origin": "subscription",
        "package": package,
        "series": series,
        "status": status,
        "importance": importance,
        "assignee": assignee,
        "task_created": created,
        "date_closed": date_closed,
    }


SINCE = "2026-07-17T00:00:00+00:00"


class TestParsers(unittest.TestCase):
    def test_person_from_link(self):
        self.assertEqual(digest.person_from_link(f"{API}/~seb128"), "seb128")
        self.assertEqual(digest.person_from_link(""), "")

    def test_parse_message(self):
        msg = digest.parse_message(_message(owner="phd", content="text"))
        self.assertEqual(msg["owner"], "phd")
        self.assertEqual(msg["content"], "text")

    def test_parse_activity(self):
        act = digest.parse_activity(_activity(old="New", new="Fix Released"))
        self.assertEqual(act["person"], "jdoe")
        self.assertEqual(act["newvalue"], "Fix Released")

    def test_parse_bug_stats(self):
        stats = digest.parse_bug_stats({
            "message_count": 21, "users_affected_count": 7,
            "number_of_duplicates": 1})
        self.assertEqual(stats["message_count"], 21)
        self.assertEqual(stats["users_affected"], 7)
        self.assertEqual(stats["duplicates"], 1)

    def test_in_window_normalizes_z_suffix(self):
        self.assertTrue(digest.in_window("2026-07-17T01:00:00Z", SINCE))
        self.assertFalse(digest.in_window("2026-07-16T23:59:59+00:00", SINCE))
        self.assertFalse(digest.in_window("", SINCE))

    def test_closed_in_window(self):
        acts = [digest.parse_activity(_activity(new="Fix Released"))]
        self.assertTrue(digest.closed_in_window(acts, SINCE))
        acts = [digest.parse_activity(
            _activity(new="Fix Released", date="2026-07-16T09:00:00+00:00"))]
        self.assertFalse(digest.closed_in_window(acts, SINCE))
        acts = [digest.parse_activity(_activity(new="Triaged"))]
        self.assertFalse(digest.closed_in_window(acts, SINCE))


class TestSruActivityEvents(unittest.TestCase):
    """The fallback approximation, used only when pending-SRU data is absent."""

    def test_series_task_status_change_is_sru(self):
        bug = {
            "id": 42,
            "activity": [digest.parse_activity(_activity(
                what="flashrom (Ubuntu Noble): status",
                old="Triaged", new="Invalid"))],
        }
        events = digest.sru_activity_events([bug], SINCE)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "sru")
        self.assertEqual(events[0]["package"], "flashrom")
        self.assertIn("noble task: Triaged → Invalid", events[0]["detail"])

    def test_devel_task_and_out_of_window_ignored(self):
        bug = {
            "id": 42,
            "activity": [
                digest.parse_activity(
                    _activity(what="flashrom (Ubuntu): status")),
                digest.parse_activity(_activity(
                    what="flashrom (Ubuntu Noble): status",
                    date="2026-07-10T00:00:00+00:00")),
            ],
        }
        self.assertEqual(digest.sru_activity_events([bug], SINCE), [])


def _sru_row(package, series="noble", proposed="1.2-1ubuntu1", release="1.1-1",
             age=5.0, bugs=()):
    return {
        "package": package, "series": series, "proposed_version": proposed,
        "release_version": release, "update_version": "", "uploaders": "someone",
        "age_days": age, "url": "", "bugs": list(bugs),
    }


def _sru_bug(bug_id, cls="", description="fix something"):
    return {"id": bug_id, "description": description, "cls": cls,
            "tags": [], "url": ""}


class TestPendingSruEvents(unittest.TestCase):
    def test_new_row_is_sru_proposed(self):
        cur = [_sru_row("flashrom", bugs=[_sru_bug(1)])]
        events = digest.pending_sru_events([], cur)
        self.assertEqual([e["kind"] for e in events], ["sru-proposed"])
        self.assertIn("1 verification bug", events[0]["detail"])

    def test_row_dropped_is_sru_released(self):
        prev = [_sru_row("flashrom")]
        events = digest.pending_sru_events(prev, [])
        self.assertEqual([e["kind"] for e in events], ["sru-released"])

    def test_new_bug_needs_verification(self):
        prev = [_sru_row("flashrom", bugs=[])]
        cur = [_sru_row("flashrom", bugs=[_sru_bug(1)])]
        events = digest.pending_sru_events(prev, cur)
        self.assertEqual([e["kind"] for e in events],
                         ["sru-verification-needed"])

    def test_bug_becomes_verified(self):
        prev = [_sru_row("flashrom", bugs=[_sru_bug(1, cls="")])]
        cur = [_sru_row("flashrom", bugs=[_sru_bug(1, cls="verified")])]
        events = digest.pending_sru_events(prev, cur)
        self.assertEqual([e["kind"] for e in events], ["sru-verified"])

    def test_bug_becomes_verification_failed(self):
        prev = [_sru_row("flashrom", bugs=[_sru_bug(1, cls="")])]
        cur = [_sru_row("flashrom",
                        bugs=[_sru_bug(1, cls="verificationfailed")])]
        events = digest.pending_sru_events(prev, cur)
        self.assertEqual([e["kind"] for e in events],
                         ["sru-verification-failed"])

    def test_bug_becomes_removal_candidate(self):
        prev = [_sru_row("flashrom", age=20, bugs=[_sru_bug(1, cls="")])]
        cur = [_sru_row("flashrom", age=20,
                        bugs=[_sru_bug(1, cls="removal")])]
        events = digest.pending_sru_events(prev, cur)
        self.assertEqual([e["kind"] for e in events],
                         ["sru-removal-candidate"])
        self.assertIn("20 days", events[0]["detail"])

    def test_unchanged_status_is_not_an_event(self):
        prev = [_sru_row("flashrom", bugs=[_sru_bug(1, cls="verified")])]
        cur = [_sru_row("flashrom", bugs=[_sru_bug(1, cls="verified")])]
        self.assertEqual(digest.pending_sru_events(prev, cur), [])


class TestPipelineEvents(unittest.TestCase):
    def test_upload_to_proposed(self):
        events = digest.pipeline_events(
            [_snap("apt", proposed="")],
            [_snap("apt", proposed="3.3.1")], [])
        self.assertEqual([e["kind"] for e in events], ["upload"])
        self.assertIn("3.3.1 uploaded to proposed", events[0]["detail"])

    def test_migration_matches_previous_proposed(self):
        events = digest.pipeline_events(
            [_snap("apt", ubuntu="3.2.0", proposed="3.3.1")],
            [_snap("apt", ubuntu="3.3.1", proposed="")], [])
        self.assertEqual([e["kind"] for e in events], ["migrated"])
        self.assertIn("3.3.1 migrated", events[0]["detail"])

    def test_release_pocket_change_without_proposed_match(self):
        events = digest.pipeline_events(
            [_snap("tzdata", ubuntu="2026a-1")],
            [_snap("tzdata", ubuntu="2026b-1")], [])
        self.assertEqual([e["kind"] for e in events], ["upload"])
        self.assertIn("landed in the release pocket", events[0]["detail"])

    def test_ftbfs_and_regression_deltas(self):
        prev = [_snap("glibc", regressions=["a/1 (amd64)"])]
        cur = [_snap("glibc", regressions=["a/1 (amd64)", "b/2 (s390x)"],
                     missing_builds=["i386"])]
        events = digest.pipeline_events(prev, cur, [])
        kinds = {e["kind"]: e for e in events}
        self.assertIn("ftbfs", kinds)
        self.assertIn("i386", kinds["ftbfs"]["detail"])
        self.assertIn("excuse-regression", kinds)
        # only the delta is reported
        self.assertIn("b/2 (s390x)", kinds["excuse-regression"]["detail"])
        self.assertNotIn("a/1", kinds["excuse-regression"]["detail"])

    def test_unblocked(self):
        events = digest.pipeline_events(
            [_snap("glibc", regressions=["a/1 (amd64)"])],
            [_snap("glibc")], [])
        self.assertEqual([e["kind"] for e in events], ["unblocked"])

    def test_transition_kinds(self):
        trs = [
            {"package": "p1", "from_state": "merge-needed",
             "to_state": "waiting-age", "last_seen_old": "x",
             "first_seen_new": "y"},
            {"package": "p2", "from_state": "in-sync",
             "to_state": "sync-available", "last_seen_old": "x",
             "first_seen_new": "y"},
            {"package": "p3", "from_state": "waiting-age",
             "to_state": "blocked-build", "last_seen_old": "x",
             "first_seen_new": "y"},
            {"package": "p4", "from_state": "ready-to-migrate",
             "to_state": "in-sync", "last_seen_old": "x",
             "first_seen_new": "y"},
            {"package": "p5", "from_state": "in-sync",
             "to_state": "not-in-devel", "last_seen_old": "x",
             "first_seen_new": "y"},
        ]
        events = digest.pipeline_events([], [], trs)
        by_pkg = {e["package"]: e["kind"] for e in events}
        self.assertEqual(by_pkg, {
            "p1": "merged", "p2": "merge-needed", "p3": "ftbfs",
            "p4": "migrated", "p5": "state-change"})

    def test_snapshot_diff_wins_over_transition_duplicate(self):
        events = digest.pipeline_events(
            [_snap("apt", ubuntu="3.2.0", proposed="3.3.1",
                   state="waiting-age")],
            [_snap("apt", ubuntu="3.3.1", proposed="")],
            [{"package": "apt", "from_state": "waiting-age",
              "to_state": "in-sync", "last_seen_old": "x",
              "first_seen_new": "y"}])
        self.assertEqual([e["kind"] for e in events], ["migrated"])


class TestFetchExtras(unittest.TestCase):
    def test_fetches_recent_messages_and_paginated_activity(self):
        pages = {
            f"{API}/bugs/7": {"message_count": 10,
                              "users_affected_count": 2,
                              "number_of_duplicates": 0},
            f"{API}/bugs/7/messages?ws.start=4&ws.size=6": {
                "entries": [_message()]},
            f"{API}/bugs/7/activity": {
                "entries": [_activity()],
                "next_collection_link": f"{API}/bugs/7/activity?ws.start=75",
            },
            f"{API}/bugs/7/activity?ws.start=75": {
                "entries": [_activity(new="Fix Released")]},
        }
        with mock.patch.object(lpbugs, "_cached_json",
                               side_effect=lambda url: pages[url]):
            extras = digest.fetch_bug_extras(7)
        self.assertEqual(extras["stats"]["users_affected"], 2)
        self.assertEqual(len(extras["messages"]), 1)
        self.assertEqual(len(extras["activity"]), 2)

    def test_low_message_count_starts_at_zero(self):
        pages = {
            f"{API}/bugs/8": {"message_count": 2},
            f"{API}/bugs/8/messages?ws.start=0&ws.size=6": {"entries": []},
            f"{API}/bugs/8/activity": {"entries": []},
        }
        with mock.patch.object(lpbugs, "_cached_json",
                               side_effect=lambda url: pages[url]):
            extras = digest.fetch_bug_extras(8)
        self.assertEqual(extras["messages"], [])

    def test_messages_failure_degrades_to_empty(self):
        def fake(url):
            if url == f"{API}/bugs/9":
                return {"message_count": 3}
            if url.endswith("/activity"):
                return {"entries": [_activity()]}
            raise lpbugs.SourceUnavailable(url)
        with mock.patch.object(lpbugs, "_cached_json", side_effect=fake):
            extras = digest.fetch_bug_extras(9)
        self.assertEqual(extras["messages"], [])
        self.assertEqual(len(extras["activity"]), 1)


def _build(bug_rows, extras=None, snaps=(), prev=(), trs=()):
    return digest.build_facts(
        bug_rows, extras or {}, list(snaps), list(prev), list(trs),
        {"id": 4, "ran_at": "2026-07-17T06:00:00Z", "series": "stonking"},
        {"watermark": "2026-07-17T11:00:00+00:00",
         "last_synced": "2026-07-17T11:05:00Z"},
        team="foundations-bugs", since=SINCE,
        until="2026-07-17T11:00:00+00:00", date="2026-07-17")


class TestBuildFacts(unittest.TestCase):
    def _facts(self, bug_rows, extras=None, snaps=(), prev=(), trs=()):
        return _build(bug_rows, extras, snaps, prev, trs)

    def test_counts_flags_and_pipeline_crossref(self):
        rows = [
            _bug_row(1, created="2026-07-17T08:00:00+00:00"),  # new today
            _bug_row(2, package="apt",
                     date_closed="2026-07-17T09:00:00+00:00",
                     status="Fix Released"),
            _bug_row(3, package="apt", importance="Critical",
                     assignee="jdoe"),
        ]
        facts = self._facts(
            rows, snaps=[_snap("apt", state="blocked-tests", age=58.0)])
        d = facts["digest"]
        self.assertEqual(d["bug_count"], 3)
        self.assertEqual(d["package_count"], 2)
        self.assertEqual(d["new_today"], 1)
        self.assertEqual(d["closed_today"], 1)
        self.assertEqual(d["critical"], 1)
        self.assertEqual(d["unassigned"], 1)  # bug 1: open, no assignee
        bugs = {b["id"]: b for b in facts["bugs"]}
        self.assertTrue(bugs[1]["is_new_today"])
        self.assertTrue(bugs[2]["is_closed_today"])
        self.assertEqual(bugs[3]["pipeline"][0]["state"], "blocked-tests")
        self.assertEqual(bugs[1]["pipeline"], [])

    def test_window_filtering_and_truncation(self):
        extras = {1: {
            "stats": {"message_count": 5, "users_affected": 1,
                      "duplicates": 0},
            "messages": [
                digest.parse_message(_message(content="x" * 5000)),
                digest.parse_message(
                    _message(date="2026-07-16T10:00:00+00:00")),
            ],
            "activity": [
                digest.parse_activity(_activity(new="y" * 2000)),
                digest.parse_activity(
                    _activity(date="2026-07-16T09:00:00+00:00")),
            ],
        }}
        facts = self._facts([_bug_row(1)], extras)
        bug = facts["bugs"][0]
        self.assertEqual(len(bug["messages"]), 1)
        self.assertEqual(len(bug["messages"][0]["content"]),
                         digest.MESSAGE_TRUNCATE)
        self.assertEqual(len(bug["activity"]), 1)
        self.assertEqual(len(bug["activity"][0]["newvalue"]),
                         digest.ACTIVITY_TRUNCATE)

    def test_events_included(self):
        facts = self._facts(
            [], prev=[_snap("apt", proposed="")],
            snaps=[_snap("apt", proposed="3.3.1")])
        self.assertEqual(facts["digest"]["event_count"], 1)
        self.assertEqual(facts["pipeline_events"][0]["kind"], "upload")


class TestRenderFallback(unittest.TestCase):
    def test_full_digest_mentions_everything(self):
        facts = _build(
            [
                _bug_row(101, created="2026-07-17T08:00:00+00:00"),
                _bug_row(102, package="apt", assignee="jdoe"),
            ],
            prev=[_snap("apt", proposed="")],
            snaps=[_snap("apt", proposed="3.3.1")])
        md = digest.render_fallback(facts)
        self.assertIn("# 🐛 foundations-bugs bugs digest — Friday 17 July "
                      "2026", md)
        self.assertIn("#101", md)
        self.assertIn("#102", md)
        self.assertIn("## Pipeline", md)
        self.assertIn("3.3.1 uploaded to proposed", md)
        self.assertIn("| Bug | Package | Importance | Status | Assignee |",
                      md)

    def test_empty_window(self):
        md = digest.render_fallback(_build([]))
        self.assertIn("No team bug activity", md)


class TestValidation(unittest.TestCase):
    def test_cited_bug_ids(self):
        md = ("see [#2160614](https://bugs.launchpad.net/bugs/2160614) "
              "and https://bugs.launchpad.net/bugs/2152142 but not #42")
        self.assertEqual(digest.cited_bug_ids(md), {2160614, 2152142})

    def test_validate_accepts_clean_output(self):
        facts = {"bugs": [{"id": 2160614}]}
        md = "# digest\n[#2160614](https://bugs.launchpad.net/bugs/2160614)"
        self.assertEqual(digest.validate_output(md, facts), [])

    def test_validate_rejects_invented_and_empty(self):
        facts = {"bugs": [{"id": 2160614}]}
        self.assertEqual(digest.validate_output("  ", facts),
                         ["empty output"])
        problems = digest.validate_output("# d\n#9999999", facts)
        self.assertTrue(any("9999999" in p for p in problems))
        problems = digest.validate_output("no heading", facts)
        self.assertTrue(any("heading" in p for p in problems))


class TestRunLLM(unittest.TestCase):
    FACTS = {"digest": {"team": "t", "date": "2026-07-17",
                        "bug_count": 0, "event_count": 0}, "bugs": [],
             "pipeline_events": []}

    def _script(self, tmpdir, body):
        path = Path(tmpdir) / "runner"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_success_reads_stdin_prints_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._script(
                tmp, 'read line\necho "# digest for $1" | head -c 200\n')
            out = digest.run_llm(self.FACTS, cmd=cmd, timeout=10)
        self.assertTrue(out.startswith("# digest"))

    def test_env_var_used_when_no_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._script(tmp, 'echo "# from env"\n')
            with mock.patch.dict(os.environ, {"UPLC_LLM_CMD": cmd}):
                out = digest.run_llm(self.FACTS, timeout=10)
        self.assertEqual(out, "# from env")

    def test_nonzero_exit_returns_none(self):
        self.assertIsNone(
            digest.run_llm(self.FACTS, cmd="/bin/false", timeout=10))

    def test_missing_binary_returns_none(self):
        self.assertIsNone(digest.run_llm(
            self.FACTS, cmd="/nonexistent/llm-runner", timeout=10))

    def test_timeout_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._script(tmp, "sleep 5\n")
            self.assertIsNone(
                digest.run_llm(self.FACTS, cmd=cmd, timeout=0.3))


class TestGenerate(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(":memory:"))
        for patch in (mock.patch.object(digest.time, "sleep"),
                      mock.patch.object(
                          digest, "fetch_bug_extras",
                          return_value={"stats": {}, "messages": [],
                                        "activity": []})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_requires_bug_sync(self):
        with self.assertRaises(SystemExit):
            digest.generate(self.conn, "foundations-bugs", no_llm=True)

    def test_no_llm_end_to_end_and_watermark_tiling(self):
        db.record_bug(self.conn, {
            "id": 500, "title": "t", "tags": [],
            "date_created": "2026-07-01T00:00:00+00:00",
            "date_last_updated": "2026-07-17T10:00:00+00:00", "heat": 2,
        }, [{"package": "glibc", "series": "", "status": "New",
             "importance": "High"}])
        db.set_bug_sync_state(self.conn, "foundations-bugs",
                              "2026-07-17T10:00:00+00:00")
        self.conn.commit()

        first = digest.generate(self.conn, "foundations-bugs",
                                since=SINCE, no_llm=True)
        self.assertEqual(first.bug_count, 1)
        self.assertFalse(first.used_llm)
        self.assertIn("#500", first.body)
        row = db.latest_digest_run(self.conn, "foundations-bugs")
        self.assertEqual(row["bug_count"], 1)
        self.assertEqual(row["used_llm"], 0)
        self.assertEqual(row["until_iso"], "2026-07-17T10:00:00+00:00")

        db.set_bug_sync_state(self.conn, "foundations-bugs",
                              "2026-07-17T12:00:00+00:00")
        second = digest.generate(self.conn, "foundations-bugs", no_llm=True)
        self.assertEqual(second.since, first.until)  # tiling, no gap
        self.assertEqual(second.bug_count, 0)
        self.assertIn("No team bug activity", second.body)

    def test_rejected_llm_output_falls_back(self):
        db.set_bug_sync_state(self.conn, "foundations-bugs",
                              "2026-07-17T10:00:00+00:00")
        self.conn.commit()
        with mock.patch.object(digest, "run_llm",
                               return_value="# d\ninvented [#9999999](x)"):
            result = digest.generate(self.conn, "foundations-bugs",
                                     since=SINCE)
        self.assertFalse(result.used_llm)
        self.assertNotIn("9999999", result.body)


class TestDbHelpers(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(":memory:"))

    def test_bugs_modified_since_filters(self):
        for bug_id, updated in ((1, "2026-07-17T05:00:00+00:00"),
                                (2, "2026-07-16T05:00:00+00:00")):
            db.record_bug(self.conn, {
                "id": bug_id, "title": "t", "tags": [],
                "date_created": "2026-07-01T00:00:00+00:00",
                "date_last_updated": updated, "heat": 0,
            }, [{"package": "glibc"}])
        rows = db.bugs_modified_since(self.conn, SINCE)
        self.assertEqual([r["id"] for r in rows], [1])

    def test_transitions_between(self):
        self.conn.execute(
            "INSERT INTO transitions (package, from_state, to_state,"
            " last_seen_old, first_seen_new) VALUES"
            " ('a', 'x', 'y', '2026-07-16T00:00:00Z', '2026-07-16T06:00:00Z'),"
            " ('b', 'x', 'y', '2026-07-16T06:00:00Z', '2026-07-17T06:00:00Z')")
        rows = db.transitions_between(
            self.conn, "2026-07-16T06:00:00Z", "2026-07-17T06:00:00Z")
        self.assertEqual([r["package"] for r in rows], ["b"])

    def test_digest_runs_dedupe_and_order(self):
        for date, body in (("2026-07-16", "old"), ("2026-07-17", "first"),
                           ("2026-07-17", "rerun")):
            db.record_digest_run(
                self.conn, team="foundations-bugs", since_iso="s",
                until_iso="u", date=date, bug_count=1, used_llm=False,
                body=body)
        rows = db.digest_runs(self.conn, "foundations-bugs")
        self.assertEqual([r["date"] for r in rows],
                         ["2026-07-17", "2026-07-16"])
        self.assertEqual(rows[0]["body"], "rerun")  # last same-day run wins
        self.assertEqual(
            len(db.digest_runs(self.conn, "foundations-bugs", limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
