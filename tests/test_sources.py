import unittest

from uplc import sources

SRU_YAML = b"""
focal:
- age: 5
  bugs:
  - cls: verified blockproposed
    description: fix segfault
    id: 100
    tags: [verification-done, verification-done-focal]
    url: https://launchpad.net/bugs/100
  pkg: flashrom
  proposed_version: 1.2-1ubuntu1
  release_version: 1.1-1
  update_version: ''
  uploaders: someone
  url: https://launchpad.net/ubuntu/+source/flashrom/1.2-1ubuntu1
jammy:
- age: 20
  bugs:
  - cls: removal
    description: needs testing
    id: 200
    tags: [block-proposed-jammy]
    url: https://launchpad.net/bugs/200
  pkg: knot
  proposed_version: 2.7.8-1ubuntu0.1
  release_version: 2.7.8-1
  update_version: ''
  uploaders: ddstreet
  url: https://launchpad.net/ubuntu/+source/knot/2.7.8-1ubuntu0.1
"""


class TestBugVerificationStatus(unittest.TestCase):
    def test_verified(self):
        self.assertEqual(
            sources.bug_verification_status("verified blockproposed"),
            "verified")

    def test_verification_failed_wins_over_verified_token(self):
        # sru-report never emits both, but failed must still take priority
        # over a stale/unrelated "verified" substring match.
        self.assertEqual(
            sources.bug_verification_status("verificationfailed"),
            "verification-failed")

    def test_removal_candidate(self):
        self.assertEqual(
            sources.bug_verification_status("removal"), "removal-candidate")

    def test_incomplete(self):
        self.assertEqual(
            sources.bug_verification_status("incomplete"), "incomplete")

    def test_no_tokens_is_pending(self):
        self.assertEqual(sources.bug_verification_status(""), "pending")
        self.assertEqual(sources.bug_verification_status("blockproposed"),
                         "pending")


class TestParseSruReport(unittest.TestCase):
    def test_flattens_per_series_package_rows(self):
        rows = sources.parse_sru_report(SRU_YAML)
        by_key = {(r["package"], r["series"]): r for r in rows}
        self.assertEqual(set(by_key), {("flashrom", "focal"), ("knot", "jammy")})

        flashrom = by_key[("flashrom", "focal")]
        self.assertEqual(flashrom["proposed_version"], "1.2-1ubuntu1")
        self.assertEqual(flashrom["age_days"], 5)
        self.assertEqual(len(flashrom["bugs"]), 1)
        self.assertEqual(flashrom["bugs"][0]["id"], 100)
        self.assertEqual(
            sources.bug_verification_status(flashrom["bugs"][0]["cls"]),
            "verified")

        knot = by_key[("knot", "jammy")]
        self.assertEqual(
            sources.bug_verification_status(knot["bugs"][0]["cls"]),
            "removal-candidate")

    def test_empty_document(self):
        self.assertEqual(sources.parse_sru_report(b""), [])


if __name__ == "__main__":
    unittest.main()
