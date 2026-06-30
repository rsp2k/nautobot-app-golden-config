"""Unit tests for nautobot_golden_config utilities git."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import ANY, MagicMock, Mock, patch
from urllib.parse import quote

from django.conf import settings
from git import Repo
from git.exc import GitCommandError
from nautobot.extras.datasources.git import get_repo_from_url_to_path_and_from_branch
from packaging import version

from nautobot_golden_config.utilities.git import GitRepo


class GitRepoTest(unittest.TestCase):
    """Test Git Utility."""

    def setUp(self):
        """Setup a reusable mock object to pass into GitRepo."""
        mock_obj = Mock()

        def mock_get_secret_value(  # pylint: disable=unused-argument,inconsistent-return-statements
            access_type, secret_type, **kwargs
        ):
            """Mock SecretsGroup.get_secret_value()."""
            if secret_type == "username":
                return mock_obj.username
            if secret_type == "token":
                return mock_obj._token  # pylint: disable=protected-access

        mock_obj.filesystem_path = "/fake/path"
        mock_obj.remote_url = "https://fake.git/org/repository.git"
        mock_obj._token = "fake token"  # pylint: disable=protected-access
        mock_obj.username = None
        mock_obj.secrets_group = Mock(get_secret_value=mock_get_secret_value)
        self.mock_obj = mock_obj

        # Different behavior of `Repo.clone_from` in different versions of Nautobot. `branch` kwarg added in 2.4.2
        self.clone_from_kwargs = {"to_path": self.mock_obj.filesystem_path, "env": None}
        if version.parse(settings.VERSION) >= version.parse("2.4.2"):
            self.clone_from_kwargs["branch"] = ANY

    @patch("nautobot.core.utils.git.GIT_ENVIRONMENT", None)
    @patch("nautobot.core.utils.git.os.path.isdir", Mock(return_value=False))
    @patch("nautobot.core.utils.git.Repo", autospec=True)
    def test_gitrepo_path_noexist(self, mock_repo):
        """Test Repo is not called when path isn't valid, ensure clone_from is called."""
        git_info = get_repo_from_url_to_path_and_from_branch(self.mock_obj)
        GitRepo(self.mock_obj.filesystem_path, git_info.from_url, base_url=self.mock_obj.remote_url)
        mock_repo.assert_not_called()
        mock_repo.clone_from.assert_called_with(git_info.from_url, **self.clone_from_kwargs)

    @patch("nautobot.core.utils.git.os.path.isdir", Mock(return_value=True))
    @patch("nautobot.core.utils.git.Repo", autospec=True)
    def test_gitrepo_path_exist(self, mock_repo):
        """Test Repo is called when path is valid."""
        git_info = get_repo_from_url_to_path_and_from_branch(self.mock_obj)
        GitRepo(self.mock_obj.filesystem_path, git_info.from_url, base_url=self.mock_obj.remote_url)
        mock_repo.assert_called_once_with(path=self.mock_obj.filesystem_path)

    @patch("nautobot.core.utils.git.GIT_ENVIRONMENT", None)
    @patch("nautobot.core.utils.git.os.path.isdir", Mock(return_value=False))
    @patch("nautobot.core.utils.git.Repo", autospec=True)
    def test_path_noexist_token_and_username_with_symbols(self, mock_repo):
        """Test Repo clone_from is called when path is not valid, with username and token."""
        self.mock_obj.username = "Test User"
        self.mock_obj._token = "Fake Token"  # pylint: disable=protected-access
        git_info = get_repo_from_url_to_path_and_from_branch(self.mock_obj)
        self.assertIn(quote(self.mock_obj.username), git_info.from_url)
        self.assertIn(quote(self.mock_obj._token), git_info.from_url)  # pylint: disable=protected-access
        GitRepo(self.mock_obj.filesystem_path, git_info.from_url, base_url=self.mock_obj.remote_url)
        mock_repo.assert_not_called()
        mock_repo.clone_from.assert_called_with(git_info.from_url, **self.clone_from_kwargs)


@patch("nautobot.core.utils.git.os.path.isdir", Mock(return_value=True))
@patch("nautobot.core.utils.git.Repo", autospec=True)
class GitRepoCommitTest(unittest.TestCase):
    """Test GitRepo.commit_with_added() empty-commit handling (issue #848)."""

    PATH = "/fake/path"
    URL = "https://fake.git/org/repository.git"

    def test_commit_with_added_skips_when_clean(self, _mock_repo_cls):
        """When there are no changes to commit, no commit is created and False is returned."""
        git_repo = GitRepo(self.PATH, self.URL, base_url=self.URL)
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        git_repo.repo = mock_repo

        committed = git_repo.commit_with_added("Test commit")

        self.assertFalse(committed)
        mock_repo.index.commit.assert_not_called()

    def test_commit_with_added_commits_when_dirty(self, _mock_repo_cls):
        """When there are changes to commit, a commit is created and True is returned."""
        git_repo = GitRepo(self.PATH, self.URL, base_url=self.URL)
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        git_repo.repo = mock_repo

        committed = git_repo.commit_with_added("Test commit")

        self.assertTrue(committed)
        mock_repo.index.commit.assert_called_once_with("Test commit")


class GitRepoCommitFileTest(unittest.TestCase):
    """Test GitRepo.commit_file() per-device rancid commits against a real Git repo."""

    def setUp(self):
        """Create a real temporary Git repo with a seed commit."""
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.root = self.tmp.name
        self.repo = Repo.init(self.root, initial_branch="main")
        with self.repo.config_writer() as config:
            config.set_value("user", "name", "Seed")
            config.set_value("user", "email", "seed@example.com")
        self._write("README.md", "seed\n")
        self.repo.git.add("README.md")
        self.repo.index.commit("seed")

        # Bypass GitRepo.__init__ (which would clone); commit_file only needs .repo.
        self.git_repo = GitRepo.__new__(GitRepo)
        self.git_repo.repo = self.repo

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel_path, content):
        abs_path = os.path.join(self.root, rel_path)
        os.makedirs(os.path.dirname(abs_path) or self.root, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_commit_file_new_file_backdated_author(self):
        """A new file is committed with the given author and backdated author/committer date."""
        self._write("site1/rtr-a.cfg", "hostname rtr-a\n")
        when = datetime(2025, 6, 24, 21, 32, 14, tzinfo=timezone.utc)

        created = self.git_repo.commit_file("site1/rtr-a.cfg", "jdoe", "jdoe@example.com", when, "rtr-a: backup")

        self.assertTrue(created)
        head = self.repo.head.commit
        self.assertEqual(head.author.name, "jdoe")
        self.assertEqual(head.author.email, "jdoe@example.com")
        self.assertEqual(head.authored_datetime, when)
        self.assertEqual(head.message.strip(), "rtr-a: backup")

    def test_commit_file_skips_when_unchanged(self):
        """Re-committing identical content is a no-op that returns False."""
        self._write("site1/rtr-a.cfg", "hostname rtr-a\n")
        when = datetime(2025, 6, 24, tzinfo=timezone.utc)
        self.assertTrue(self.git_repo.commit_file("site1/rtr-a.cfg", "jdoe", "jdoe@example.com", when, "first"))
        before = self.repo.head.commit.hexsha

        created = self.git_repo.commit_file("site1/rtr-a.cfg", "jdoe", "jdoe@example.com", when, "second")

        self.assertFalse(created)
        self.assertEqual(self.repo.head.commit.hexsha, before)

    def test_commit_file_rename_is_followable(self):
        """A rename (both files on disk) records a Git rename so log --follow traces history.

        Real configs are large and mostly identical across a hostname change, which is
        what lets Git's similarity-based rename detection connect the two files; the
        content here mirrors that (only the hostname line changes).
        """
        original = "hostname rtr-a\n!\ninterface Gi0/0\n description uplink\n ip address 192.0.2.1 255.255.255.0\n!\n"
        self._write("site1/rtr-a.cfg", original)
        self.git_repo.commit_file(
            "site1/rtr-a.cfg", "jdoe", "jdoe@example.com", datetime(2025, 1, 1, tzinfo=timezone.utc), "rtr-a: backup"
        )
        # Device renamed: the play wrote the new path; the old path is still tracked.
        self._write("site1/rtr-a-new.cfg", original.replace("hostname rtr-a", "hostname rtr-a-new"))
        when = datetime(2025, 1, 2, tzinfo=timezone.utc)

        created = self.git_repo.commit_file(
            "site1/rtr-a-new.cfg",
            "jdoe",
            "jdoe@example.com",
            when,
            "rtr-a-new (renamed from site1/rtr-a.cfg): backup",
            previous_path="site1/rtr-a.cfg",
        )

        self.assertTrue(created)
        self.assertFalse(os.path.exists(os.path.join(self.root, "site1/rtr-a.cfg")))
        self.assertIn("rtr-a-new", open(os.path.join(self.root, "site1/rtr-a-new.cfg"), encoding="utf-8").read())
        follow = self.repo.git.log("--follow", "--pretty=%s", "--", "site1/rtr-a-new.cfg").splitlines()
        self.assertEqual(len(follow), 2, f"--follow did not trace through the rename: {follow}")

    def test_commit_file_appends_device_id_trailer(self):
        """A device_id parameter is appended as a trailer in the commit message."""
        self._write("site1/rtr-a.cfg", "hostname rtr-a\n")
        device_id = "12345678-1234-5678-1234-567812345678"
        when = datetime(2025, 6, 24, tzinfo=timezone.utc)

        self.git_repo.commit_file(
            "site1/rtr-a.cfg", "jdoe", "jdoe@example.com", when, "rtr-a: backup", device_id=device_id
        )

        commit = self.repo.head.commit
        self.assertIn(f"Golden-Config-Device-Id: {device_id}", commit.message)

    def test_commit_file_without_device_id_has_no_trailer(self):
        """Omitting device_id (default None) leaves the message free of the trailer."""
        self._write("site1/rtr-a.cfg", "hostname rtr-a\n")
        when = datetime(2025, 6, 24, tzinfo=timezone.utc)

        self.git_repo.commit_file("site1/rtr-a.cfg", "jdoe", "jdoe@example.com", when, "rtr-a: backup")

        self.assertNotIn("Golden-Config-Device-Id", self.repo.head.commit.message)

    def test_commit_file_missing_previous_path_treated_as_new(self):
        """A stale previous_path (old file gone) does not error; the file commits as new."""
        self._write("site1/rtr-b.cfg", "hostname rtr-b\n")
        when = datetime(2025, 3, 3, tzinfo=timezone.utc)

        created = self.git_repo.commit_file(
            "site1/rtr-b.cfg", "jdoe", "jdoe@example.com", when, "rtr-b: backup", previous_path="site1/gone.cfg"
        )

        self.assertTrue(created)
        self.assertEqual(self.repo.head.commit.message.strip(), "rtr-b: backup")


@patch("nautobot.core.utils.git.os.path.isdir", Mock(return_value=True))
@patch("nautobot.core.utils.git.Repo", autospec=True)
class GitRepoPushTest(unittest.TestCase):
    """Test GitRepo.push() concurrent-push handling (issue #968)."""

    PATH = "/fake/path"
    URL = "https://fake.git/org/repository.git"

    @staticmethod
    def _non_fast_forward_error():
        return GitCommandError(
            "git push",
            1,
            stderr=b"! [rejected]        main -> main (non-fast-forward)\nerror: failed to push some refs to 'origin'\n",
        )

    def test_push_retries_on_non_fast_forward_then_succeeds(self, _mock_repo_cls):
        """Regression test for #968.

        When the remote rejects the initial push as non-fast-forward, push() must fetch from origin,
        rebase the local branch onto the remote tip, and retry the push so that two concurrent jobs
        can both succeed.
        """
        git_repo = GitRepo(self.PATH, self.URL, base_url=self.URL)
        repo = git_repo.repo
        repo.active_branch.name = "main"

        push_result = Mock()
        push_result.raise_if_error.side_effect = [self._non_fast_forward_error(), None]
        repo.remotes.origin.push.return_value = push_result

        with patch.object(GitRepo, "fetch") as mock_fetch:
            git_repo.push()

        self.assertEqual(repo.remotes.origin.push.call_count, 2)
        self.assertEqual(push_result.raise_if_error.call_count, 2)
        mock_fetch.assert_called_once_with()
        repo.git.rebase.assert_called_once_with("origin/main")

    def test_push_succeeds_first_try_no_fetch_or_rebase(self, _mock_repo_cls):
        """Happy path: a successful push must not fetch or rebase."""
        git_repo = GitRepo(self.PATH, self.URL, base_url=self.URL)
        repo = git_repo.repo

        push_result = Mock()
        push_result.raise_if_error.return_value = None
        repo.remotes.origin.push.return_value = push_result

        with patch.object(GitRepo, "fetch") as mock_fetch:
            git_repo.push()

        repo.remotes.origin.push.assert_called_once_with()
        push_result.raise_if_error.assert_called_once_with()
        mock_fetch.assert_not_called()
        repo.git.rebase.assert_not_called()

    def test_push_rebase_conflict_aborts_and_reraises(self, _mock_repo_cls):
        """A rebase that itself fails must abort and surface the original error, not loop."""
        git_repo = GitRepo(self.PATH, self.URL, base_url=self.URL)
        repo = git_repo.repo
        repo.active_branch.name = "main"

        push_result = Mock()
        push_result.raise_if_error.side_effect = self._non_fast_forward_error()
        repo.remotes.origin.push.return_value = push_result

        rebase_conflict = GitCommandError("git rebase", 1, stderr=b"CONFLICT (content): Merge conflict in foo.cfg")
        repo.git.rebase.side_effect = [rebase_conflict, None]

        with patch.object(GitRepo, "fetch"), self.assertRaises(GitCommandError):
            git_repo.push()

        self.assertEqual(repo.remotes.origin.push.call_count, 1)
        self.assertEqual(repo.git.rebase.call_count, 2)
        repo.git.rebase.assert_any_call("origin/main")
        repo.git.rebase.assert_any_call("--abort")

    def test_push_non_retryable_error_fails_fast(self, _mock_repo_cls):
        """Auth/network errors must propagate immediately without fetch or rebase."""
        git_repo = GitRepo(self.PATH, self.URL, base_url=self.URL)
        repo = git_repo.repo

        auth_error = GitCommandError("git push", 128, stderr=b"fatal: Authentication failed for 'origin'")
        push_result = Mock()
        push_result.raise_if_error.side_effect = auth_error
        repo.remotes.origin.push.return_value = push_result

        with patch.object(GitRepo, "fetch") as mock_fetch, self.assertRaises(GitCommandError):
            git_repo.push()

        self.assertEqual(repo.remotes.origin.push.call_count, 1)
        mock_fetch.assert_not_called()
        repo.git.rebase.assert_not_called()

    def test_push_exhausts_retries(self, _mock_repo_cls):
        """If every attempt is rejected as non-fast-forward, the error propagates after max_retries."""
        git_repo = GitRepo(self.PATH, self.URL, base_url=self.URL)
        repo = git_repo.repo
        repo.active_branch.name = "main"

        push_result = Mock()
        push_result.raise_if_error.side_effect = [
            self._non_fast_forward_error(),
            self._non_fast_forward_error(),
            self._non_fast_forward_error(),
        ]
        repo.remotes.origin.push.return_value = push_result

        with patch.object(GitRepo, "fetch") as mock_fetch, self.assertRaises(GitCommandError):
            git_repo.push(max_retries=3)

        self.assertEqual(repo.remotes.origin.push.call_count, 3)
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(repo.git.rebase.call_count, 2)
