import unittest

from uplc.sources import Excuse
from uplc.state import derive_state


class TestDeriveState(unittest.TestCase):
    def test_in_sync(self):
        ps = derive_state("pkg", None, "1.0-1", "1.0-1")
        self.assertEqual(ps.state, "in-sync")

    def test_merge_needed(self):
        ps = derive_state("pkg", None, "1.0-1ubuntu2", "1.1-1")
        self.assertEqual(ps.state, "merge-needed")

    def test_sync_available(self):
        ps = derive_state("pkg", None, "1.0-1", "1.1-1")
        self.assertEqual(ps.state, "sync-available")

    def test_ubuntu_delta_not_outdated(self):
        # Ubuntu revision on the same Debian base is not "behind".
        ps = derive_state("pkg", None, "1.1-1ubuntu1", "1.1-1")
        self.assertEqual(ps.state, "in-sync")

    def test_ubuntu_only(self):
        ps = derive_state("apport", None, "2.30", None)
        self.assertEqual(ps.state, "ubuntu-only")

    def test_not_in_devel(self):
        ps = derive_state("pkg", None, None, "1.0-1")
        self.assertEqual(ps.state, "not-in-devel")

    def test_blocked_tests(self):
        exc = Excuse(source="pkg", new_version="2.0-1", is_candidate=False,
                     reasons={"autopkgtest"},
                     regressions=["other/1.0 (amd64)"])
        ps = derive_state("pkg", exc, "1.0-1", "2.0-1")
        self.assertEqual(ps.state, "blocked-tests")
        self.assertIn("autopkgtest", ps.summary)

    def test_missing_build_wins_over_tests(self):
        exc = Excuse(source="pkg", is_candidate=False,
                     reasons={"autopkgtest", "missingbuild"},
                     missing_builds=["riscv64"])
        ps = derive_state("pkg", exc, "1.0-1", "2.0-1")
        self.assertEqual(ps.state, "blocked-build")

    def test_blocked_depends(self):
        exc = Excuse(source="pkg", is_candidate=False,
                     reasons={"depends"}, blocked_by={"glibc"})
        ps = derive_state("pkg", exc, "1.0-1", "2.0-1")
        self.assertEqual(ps.state, "blocked-depends")
        self.assertEqual(ps.blocked_by, ["glibc"])

    def test_candidate(self):
        exc = Excuse(source="pkg", is_candidate=True)
        ps = derive_state("pkg", exc, "1.0-1", "2.0-1")
        self.assertEqual(ps.state, "ready-to-migrate")


if __name__ == "__main__":
    unittest.main()
