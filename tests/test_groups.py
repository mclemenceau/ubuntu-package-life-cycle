import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from uplc.groups import load_groups


class TestLoadGroups(unittest.TestCase):
    def test_no_path_returns_empty(self):
        self.assertEqual(load_groups(None), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_groups("/nonexistent/groups.yaml"), {})

    def test_reverses_group_to_package_mapping(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.yaml"
            path.write_text("Boot:\n  - grub2\n  - shim\nCrypto:\n  - openssl\n")
            self.assertEqual(load_groups(path), {
                "grub2": ["Boot"], "shim": ["Boot"], "openssl": ["Crypto"],
            })

    def test_package_in_multiple_groups_is_sorted(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.yaml"
            path.write_text("System:\n  - elfutils\nRuntimes:\n  - elfutils\n")
            self.assertEqual(load_groups(path), {"elfutils": ["Runtimes", "System"]})

    def test_empty_file_returns_empty(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.yaml"
            path.write_text("")
            self.assertEqual(load_groups(path), {})


if __name__ == "__main__":
    unittest.main()
