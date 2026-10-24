import sys
import plistlib
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clean_lantai_launchservices import (
    DEFAULT_MAX_PASSES,
    DEFAULT_SETTLE_SECONDS,
    discover_app_bundles,
    find_stale_app_copies,
    parse_registrations,
    remove_validated_app_copy,
)


class ParseRegistrationsTest(unittest.TestCase):
    def test_cleanup_defaults_allow_launchservices_to_converge(self) -> None:
        self.assertEqual(DEFAULT_MAX_PASSES, 12)
        self.assertGreater(DEFAULT_SETTLE_SECONDS, 0)

    def test_only_returns_root_lantai_apps(self) -> None:
        dump = """
--------------------------------------------------------------------------------
path: /tmp/澜台.app (0x1234)
identifier: com.lantai.app
versionString: 1.0.45
--------------------------------------------------------------------------------
path: /tmp/澜台.app/Contents/Frameworks/Helper.app (0xabcd)
identifier: com.lantai.app.helper
versionString: 1.0.45
--------------------------------------------------------------------------------
path: /tmp/澜台.app.archived (0x5678)
identifier: com.lantai.app
versionString: 1.0.44
"""
        self.assertEqual(
            parse_registrations(dump),
            [
                {"path": "/tmp/澜台.app", "version": "1.0.45"},
                {"path": "/tmp/澜台.app.archived", "version": "1.0.44"},
            ],
        )


class StaleAppCopiesTest(unittest.TestCase):
    def write_app(self, target: Path, bundle_id: str, version: str) -> Path:
        plist_path = target / "Contents" / "Info.plist"
        plist_path.parent.mkdir(parents=True)
        with plist_path.open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleShortVersionString": version,
                },
                handle,
            )
        return target

    def test_discovers_nested_and_backup_named_app_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            top = self.write_app(root / "澜台.app.backup-1", "com.lantai.app", "1.0.43")
            nested = self.write_app(root / "交付包" / "澜台.app", "com.lantai.app", "1.0.45")
            self.assertEqual(set(discover_app_bundles(root)), {top, nested})

    def test_only_reports_and_removes_verified_stale_lantai_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self.write_app(root / "澜台.app", "com.lantai.app", "1.0.48")
            stale = self.write_app(root / "old" / "澜台.app", "com.lantai.app", "1.0.43")
            foreign = self.write_app(root / "other.app", "example.other", "9.0")

            copies = find_stale_app_copies(current, [root])
            self.assertEqual([item["path"] for item in copies], [str(stale)])

            remove_validated_app_copy(stale, current, [root])
            self.assertFalse(stale.exists())
            self.assertTrue(current.exists())
            self.assertTrue(foreign.exists())

    def test_refuses_to_remove_current_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self.write_app(root / "澜台.app", "com.lantai.app", "1.0.48")
            with self.assertRaisesRegex(RuntimeError, "current Lantai"):
                remove_validated_app_copy(current, current, [root])


if __name__ == "__main__":
    unittest.main()
