import shutil
import subprocess
import unittest

from uplc.debversion import compare, has_ubuntu_delta

CASES = [
    ("1.0", "1.0", 0),
    ("1.0", "1.1", -1),
    ("1.2-1", "1.2-1ubuntu1", -1),
    ("1.2-1ubuntu1", "1.2-2", -1),
    ("2.0~rc1", "2.0", -1),
    ("2.0~rc1-1", "2.0-1", -1),
    ("1:1.0", "2.0", 1),
    ("0:1.0", "1.0", 0),
    ("1.0-1", "1.0", 1),
    ("1.10", "1.9", 1),
    ("1.0a", "1.0", 1),
    ("1.0~", "1.0", -1),
    ("3.5.5-1ubuntu3", "3.5.5-1ubuntu4", -1),
    ("2.46.50.20260608-1ubuntu1", "2.46.90.20260712-1", -1),
    ("1:27.3.4.12+dfsg-1", "1:29.0.3+dfsg-1", -1),
    ("4.8+nmu2", "4.8+nmu3", -1),
    ("1.11.1-3", "1.11.1-4ubuntu1", -1),
    ("5.10+dfsg-2", "5.10+really5.9-1", -1),
]


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


class TestCompare(unittest.TestCase):
    def test_known_cases(self):
        for v1, v2, expected in CASES:
            with self.subTest(v1=v1, v2=v2):
                self.assertEqual(_sign(compare(v1, v2)), expected)
                self.assertEqual(_sign(compare(v2, v1)), -expected)

    @unittest.skipUnless(shutil.which("dpkg"), "dpkg not available")
    def test_matches_dpkg(self):
        for v1, v2, _ in CASES:
            with self.subTest(v1=v1, v2=v2):
                for op, want in (("lt", -1), ("eq", 0), ("gt", 1)):
                    dpkg_says = subprocess.run(
                        ["dpkg", "--compare-versions", v1, op, v2],
                        capture_output=True).returncode == 0
                    self.assertEqual(_sign(compare(v1, v2)) == want, dpkg_says)


class TestDelta(unittest.TestCase):
    def test_delta_detection(self):
        self.assertTrue(has_ubuntu_delta("1.2-1ubuntu1"))
        self.assertFalse(has_ubuntu_delta("1.2-1"))


if __name__ == "__main__":
    unittest.main()
