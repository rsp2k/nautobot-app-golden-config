"""Unit tests for nautobot_golden_config rancid-style backup helpers."""

import os
import tempfile
import unittest

from nautobot_golden_config.utilities import rancid


class ParseLastChangeTest(unittest.TestCase):
    """Test parsing of the `Last configuration change` line."""

    def test_full_line_with_author(self):
        """A full timestamp + author line yields both an author and a datetime."""
        config = "! Last configuration change at 21:32:14 UTC Tue Jun 24 2025 by jdoe\nhostname x\n"
        author, changed_at = rancid.parse_last_change(config)
        self.assertEqual(author, "jdoe")
        self.assertIsNotNone(changed_at)
        self.assertEqual((changed_at.year, changed_at.month, changed_at.day), (2025, 6, 24))
        self.assertEqual((changed_at.hour, changed_at.minute, changed_at.second), (21, 32, 14))

    def test_full_line_without_author(self):
        """A timestamp line with no `by <user>` yields a datetime but no author."""
        config = "! Last configuration change at 09:05:00 PST Mon Jan 5 2026\nhostname x\n"
        author, changed_at = rancid.parse_last_change(config)
        self.assertIsNone(author)
        self.assertIsNotNone(changed_at)
        self.assertEqual((changed_at.year, changed_at.month, changed_at.day), (2026, 1, 5))

    def test_author_only_line(self):
        """A line that names an author but lacks a parseable timestamp yields author only."""
        config = "! Last configuration change by bob\nhostname x\n"
        author, changed_at = rancid.parse_last_change(config)
        self.assertEqual(author, "bob")
        self.assertIsNone(changed_at)

    def test_missing_line(self):
        """A config without the line (e.g. stripped by ConfigRemove) yields nothing."""
        author, changed_at = rancid.parse_last_change("hostname x\ninterface Gi0/0\n")
        self.assertIsNone(author)
        self.assertIsNone(changed_at)

    def test_empty_config(self):
        """Empty/None config is handled gracefully."""
        self.assertEqual(rancid.parse_last_change(""), (None, None))
        self.assertEqual(rancid.parse_last_change(None), (None, None))

    def test_bad_month_falls_back_to_no_datetime(self):
        """An unparseable month keeps the author but drops the datetime."""
        config = "! Last configuration change at 21:32:14 UTC Tue Xyz 24 2025 by jdoe\n"
        author, changed_at = rancid.parse_last_change(config)
        # The full regex requires a 3-letter month token, so 'Xyz' matches the
        # pattern but maps to no month -> datetime is None, author still captured.
        self.assertEqual(author, "jdoe")
        self.assertIsNone(changed_at)


class ManifestTest(unittest.TestCase):
    """Test the rancid manifest round-trip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_returns_empty(self):
        """A repo with no manifest yields an empty map (every device is new)."""
        self.assertEqual(rancid.load_manifest(self.root), {})

    def test_save_then_load_round_trip(self):
        """A saved map loads back identical."""
        devices = {"uuid-2": "site2/b.cfg", "uuid-1": "site1/a.cfg"}
        rancid.save_manifest(self.root, devices)
        self.assertEqual(rancid.load_manifest(self.root), devices)

    def test_saved_manifest_is_stable(self):
        """Saving the same map twice produces byte-identical output (diff-stable)."""
        devices = {"uuid-2": "site2/b.cfg", "uuid-1": "site1/a.cfg"}
        path = os.path.join(self.root, rancid.MANIFEST_NAME)
        rancid.save_manifest(self.root, devices)
        first = open(path, encoding="utf-8").read()
        rancid.save_manifest(self.root, dict(reversed(list(devices.items()))))
        second = open(path, encoding="utf-8").read()
        self.assertEqual(first, second)

    def test_load_corrupt_returns_empty(self):
        """A corrupt manifest degrades to empty rather than raising."""
        with open(os.path.join(self.root, rancid.MANIFEST_NAME), "w", encoding="utf-8") as handle:
            handle.write("{not valid json")
        self.assertEqual(rancid.load_manifest(self.root), {})
