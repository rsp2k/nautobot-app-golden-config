"""Unit tests for nautobot_golden_config rancid-style backup helpers."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.utils.timezone import make_aware
from git import Repo
from nautobot.apps.testing import TransactionTestCase

from nautobot_golden_config.models import GoldenConfig
from nautobot_golden_config.tests.conftest import create_device
from nautobot_golden_config.utilities import rancid
from nautobot_golden_config.utilities.git import GitRepo


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


# Realistic config: most lines identical across a rename so Git's similarity-based
# rename detection connects the files (a single-line file would defeat --follow).
def _config(hostname, author="jdoe", when="21:32:14 UTC Tue Jun 24 2025"):
    return (
        f"! Last configuration change at {when} by {author}\n"
        f"hostname {hostname}\n"
        "!\n"
        "interface GigabitEthernet0/0\n"
        " description uplink\n"
        " ip address 192.0.2.1 255.255.255.0\n"
        "!\n"
        "end\n"
    )


class RancidCommitAndPushTest(TransactionTestCase):
    """Integration test for build_snapshots + rancid_commit_and_push against a real repo."""

    databases = ("default", "job_logs")

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.root = self.tmp.name
        self.repo = Repo.init(self.root, initial_branch="main")
        with self.repo.config_writer() as config:
            config.set_value("user", "name", "Automation")
            config.set_value("user", "email", "automation@example.com")
        with open(os.path.join(self.root, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("seed\n")
        self.repo.git.add("README.md")
        self.repo.index.commit("seed")

        # GitRepo whose backing on-disk repo is the temp repo, with a mocked
        # nautobot_repo_obj so we don't need Nautobot's GIT_ROOT filesystem layout.
        self.repo_id = uuid4()
        self.git_repo = GitRepo.__new__(GitRepo)
        self.git_repo.repo = self.repo
        self.git_repo.nautobot_repo_obj = MagicMock(
            id=self.repo_id,
            name="backup-repo",
            filesystem_path=self.root,
            provided_contents=["nautobot_golden_config.backupconfigs"],
        )

        self.device = create_device(name="rtr-a")
        self.golden = GoldenConfig.objects.create(
            device=self.device,
            backup_config=_config("rtr-a"),
            backup_last_success_date=make_aware(datetime(2025, 6, 24, 21, 32, 14)),
        )
        self.settings = MagicMock(backup_repository_id=self.repo_id, backup_path_template="{{obj.name}}.cfg")
        self.job = MagicMock(device_to_settings_map={self.device.id: self.settings}, logger=MagicMock())

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def _repo(self):
        return {"repo_obj": self.git_repo, "to_commit": True}

    def _write_backup(self, rel_path, content):
        with open(os.path.join(self.root, rel_path), "w", encoding="utf-8") as handle:
            handle.write(content)

    def _commit_by_author(self, name):
        for commit in self.repo.iter_commits():
            if commit.author.name == name:
                return commit
        return None

    @patch.object(GitRepo, "push")
    def test_per_device_commit_authored_and_dated_from_config(self, mock_push):
        """A backed-up device produces one commit authored/dated from its config."""
        self._write_backup("rtr-a.cfg", self.golden.backup_config)

        count = rancid.rancid_commit_and_push(self.job, self._repo())

        self.assertEqual(count, 1)
        mock_push.assert_called_once()
        commit = self._commit_by_author("jdoe")
        self.assertIsNotNone(commit, "no commit was authored by the config's named engineer")
        self.assertEqual(commit.author.email, "jdoe@example.com")
        self.assertEqual(commit.authored_datetime, datetime(2025, 6, 24, 21, 32, 14, tzinfo=timezone.utc))
        # Manifest now records the device's path.
        manifest = rancid.load_manifest(self.root)
        self.assertEqual(manifest.get(str(self.device.id)), "rtr-a.cfg")

    @patch.object(GitRepo, "push")
    def test_unchanged_backup_creates_no_commit(self, mock_push):
        """Running twice with identical content yields no second device commit."""
        self._write_backup("rtr-a.cfg", self.golden.backup_config)
        self.assertEqual(rancid.rancid_commit_and_push(self.job, self._repo()), 1)

        # Second run, nothing changed on disk.
        count = rancid.rancid_commit_and_push(self.job, self._repo())

        self.assertEqual(count, 0)
        self.assertEqual(mock_push.call_count, 1)  # no push on the no-op run

    @patch.object(GitRepo, "push")
    def test_rename_is_committed_and_followable(self, mock_push):
        """A device renamed in Nautobot moves its file via git mv, traceable with --follow."""
        self._write_backup("rtr-a.cfg", self.golden.backup_config)
        rancid.rancid_commit_and_push(self.job, self._repo())

        # Rename the device; the path template now renders to a new file.
        self.device.name = "rtr-a-new"
        self.device.save()
        self.golden.backup_config = _config("rtr-a-new", author="asmith")
        self.golden.save()
        self._write_backup("rtr-a-new.cfg", self.golden.backup_config)

        count = rancid.rancid_commit_and_push(self.job, self._repo())

        self.assertEqual(count, 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, "rtr-a.cfg")))
        self.assertEqual(rancid.load_manifest(self.root).get(str(self.device.id)), "rtr-a-new.cfg")
        follow = self.repo.git.log("--follow", "--pretty=%s", "--", "rtr-a-new.cfg").splitlines()
        self.assertEqual(len(follow), 2, f"--follow did not trace through the rename: {follow}")
        self.assertEqual(mock_push.call_count, 2)
