import json
import unittest
import xml.etree.ElementTree as ET
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
                         ["index.html", "packages.html", "bugs.html",
                          "kpi.html", "digest.html"])

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

    def test_kpi_page_with_bug_data(self):
        _seed_bugs(self.conn)
        page = htmlreport.render_kpi(self.conn, "foundations-bugs")
        self.assertIn("Package set health", page)
        self.assertIn("Bug health", page)
        # 1 of 3 packages is in sync -> 33%.
        self.assertIn("33%", page)
        self.assertIn("Rate of change", page)
        self.assertIn("now vs then", page)
        # Both SVG charts render with their table twins.
        self.assertEqual(page.count("<svg"), 2)
        self.assertIn("Data table", page)

    def test_kpi_page_without_bug_data(self):
        page = htmlreport.render_kpi(self.conn, "foundations-bugs")
        self.assertIn("bugs-sync", page)
        self.assertIn("Rate of change", page)
        self.assertNotIn("<svg", page)


class TestMdHtml(unittest.TestCase):
    def test_headings_hr_and_inline(self):
        out = htmlreport._md_html(
            "# Top\n\n## Sub\n\n---\n\n**bold** and *it* and `code` and "
            "[a link](https://example.com/x)")
        self.assertIn("<h1>Top</h1>", out)
        self.assertIn("<h2>Sub</h2>", out)
        self.assertIn("<hr>", out)
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>it</em>", out)
        self.assertIn("<code>code</code>", out)
        self.assertIn('<a href="https://example.com/x">a link</a>', out)

    def test_table_blockquote_list(self):
        out = htmlreport._md_html(
            "| Bug | Status |\n|---|---|\n| #1 | New |\n\n"
            "> a note\n> continued\n\n- one\n- two")
        self.assertIn("<th>Bug</th>", out)
        self.assertIn("<td>New</td>", out)
        self.assertNotIn("---", out)  # separator row consumed
        self.assertIn("<blockquote><p>a note continued</p></blockquote>", out)
        self.assertIn("<li>one</li>", out)

    def test_html_is_escaped(self):
        out = htmlreport._md_html("hello <script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_unknown_lines_degrade_to_paragraphs(self):
        out = htmlreport._md_html("just text\nmore text")
        self.assertIn("<p>just text more text</p>", out)


def _seed_digests(conn):
    for date, month_body in (("2026-06-30", "june digest"),
                             ("2026-07-16", "# 🐛 digest\nold day"),
                             ("2026-07-17", "# 🐛 digest\n**newest**")):
        db.record_digest_run(
            conn, team="foundations-bugs", since_iso="s", until_iso="u",
            date=date, bug_count=3, used_llm=True, body=month_body)


class TestDigestPage(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(":memory:"))

    def test_empty_state(self):
        page = htmlreport.render_digest(self.conn, "foundations-bugs")
        self.assertIn("no digests yet", page)

    def test_archive_grouping_anchors_and_subscribe(self):
        _seed_digests(self.conn)
        page = htmlreport.render_digest(self.conn, "foundations-bugs")
        self.assertIn("<h2>July 2026</h2>", page)
        self.assertIn("<h2>June 2026</h2>", page)
        # Newest is open, older days collapse behind <details>.
        self.assertIn('<div id="digest-2026-07-17">', page)
        self.assertIn('<details class="digest" id="digest-2026-07-16">', page)
        self.assertIn("Thursday 16 July — 3 bugs", page)
        self.assertIn("<strong>newest</strong>", page)
        self.assertIn('href="feed.json"', page)
        self.assertIn('href="feed.xml"', page)

    def test_feed_json_valid_and_stable_ids(self):
        _seed_digests(self.conn)
        feed = json.loads(htmlreport.render_feed_json(
            self.conn, "foundations-bugs", "https://example.com/dash"))
        self.assertEqual(feed["version"], "https://jsonfeed.org/version/1.1")
        self.assertEqual(len(feed["items"]), 3)
        newest = feed["items"][0]
        self.assertEqual(
            newest["id"],
            "tag:uplc.local,2026:foundations-bugs:digest-2026-07-17")
        self.assertEqual(
            newest["url"],
            "https://example.com/dash/digest.html#digest-2026-07-17")
        self.assertIn("<strong>newest</strong>", newest["content_html"])

    def test_feed_json_without_base_url(self):
        _seed_digests(self.conn)
        feed = json.loads(
            htmlreport.render_feed_json(self.conn, "foundations-bugs"))
        self.assertNotIn("url", feed["items"][0])
        self.assertNotIn("feed_url", feed)

    def test_feed_atom_parses_and_escapes(self):
        db.record_digest_run(
            self.conn, team="foundations-bugs", since_iso="s", until_iso="u",
            date="2026-07-17", bug_count=1, used_llm=False,
            body="# d\nx < y & z")
        xml = htmlreport.render_feed_atom(
            self.conn, "foundations-bugs", "https://example.com/dash")
        root = ET.fromstring(xml)
        ns = "{http://www.w3.org/2005/Atom}"
        entries = root.findall(f"{ns}entry")
        self.assertEqual(len(entries), 1)
        self.assertIn("x &lt; y &amp; z", entries[0].find(f"{ns}content").text)


if __name__ == "__main__":
    unittest.main()
