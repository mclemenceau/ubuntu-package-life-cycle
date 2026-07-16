import unittest
from pathlib import Path

from uplc import db, htmlreport
from uplc.state import PackageState


def _seed_pipeline(conn):
    states = [
        PackageState(package="glibc", state="blocked-tests",
                     ubuntu_version="2.42-1", debian_version="2.42-3",
                     proposed_version="2.42-2",
                     regressions=["systemd/258 (amd64)"],
                     summary="1 autopkgtest regression(s)"),
        PackageState(package="grub2", state="blocked-other", bugs=["555"],
                     ubuntu_version="2.12-5", debian_version="2.12-5",
                     summary="blocked: block-bugs"),
        PackageState(package="openssl", state="in-sync",
                     ubuntu_version="3.5.1-1", debian_version="3.5.1-1",
                     summary="devel is current vs Debian unstable"),
    ]
    db.record_run(conn, states, team="foundations-bugs", series="devel",
                  excuses_generated="2026-07-16")


def _seed_bugs(conn):
    db.record_bug(conn, {
        "id": 555, "title": "grub2 breaks on riscv",
        "tags": ["block-proposed"],
        "date_created": "2026-02-01T00:00:00+00:00",
        "date_last_updated": "2026-07-01T00:00:00+00:00",
        "heat": 10, "origin": "pipeline",
    }, [{"package": "grub2", "series": "", "status": "Triaged",
         "importance": "Critical"}])
    db.record_bug(conn, {
        "id": 600, "title": "glibc build failure",
        "tags": [],
        "date_created": "2025-12-01T00:00:00+00:00",
        "date_last_updated": "2026-03-01T00:00:00+00:00",
        "heat": 4, "origin": "subscription",
    }, [{"package": "glibc", "series": "", "status": "Fix Released",
         "importance": "High",
         "date_closed": "2026-03-01T00:00:00+00:00"}])
    db.set_bug_sync_state(conn, "foundations-bugs",
                          "2026-07-01T00:00:00+00:00")


class TestRenderPages(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(":memory:"))
        _seed_pipeline(self.conn)

    def test_pages_registry_covers_the_site(self):
        self.assertEqual(list(htmlreport.PAGES),
                         ["index.html", "packages.html", "bugs.html"])

    def test_index_links_packages_internally(self):
        page = htmlreport.render_index(self.conn, "foundations-bugs")
        self.assertIn('href="packages.html#pkg-glibc"', page)
        self.assertIn("https://launchpad.net/bugs/555", page)

    def test_packages_page_rows_and_anchors(self):
        _seed_bugs(self.conn)
        page = htmlreport.render_packages(self.conn, "foundations-bugs")
        self.assertIn('id="pkg-openssl"', page)
        self.assertIn('data-state="blocked-tests"', page)
        # Bug stats join: grub2 has one open Critical bug.
        self.assertIn('data-bugs="1" data-high="1"', page)
        # Out-links live on the row detail.
        self.assertIn("https://launchpad.net/ubuntu/+source/glibc", page)
        self.assertIn("tracker.debian.org/pkg/glibc", page)

    def test_packages_page_without_bug_data(self):
        page = htmlreport.render_packages(self.conn, "foundations-bugs")
        self.assertIn("bugs-sync", page)

    def test_bugs_page_gating_and_trend(self):
        _seed_bugs(self.conn)
        page = htmlreport.render_bugs(self.conn, "foundations-bugs")
        self.assertIn("Gating the pipeline", page)
        self.assertIn("https://launchpad.net/bugs/555", page)
        self.assertIn('href="packages.html#pkg-grub2"', page)
        self.assertIn("Opened vs closed", page)
        # Bug 600 closed in March 2026 shows up as closed this year.
        self.assertIn("Closed 2026", page)
        self.assertIn('data-open="0"', page)

    def test_bugs_page_empty_state(self):
        page = htmlreport.render_bugs(self.conn, "foundations-bugs")
        self.assertIn("No bug data yet", page)


if __name__ == "__main__":
    unittest.main()
