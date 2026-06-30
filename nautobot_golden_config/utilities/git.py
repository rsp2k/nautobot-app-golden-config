"""Git helper methods and class."""

import logging
import os

from git.exc import GitCommandError
from nautobot.apps.utils import GitRepo as _GitRepo
from nautobot.core.utils.git import GIT_ENVIRONMENT

LOGGER = logging.getLogger(__name__)

_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "failed to push some refs",
    "fetch first",
    "[rejected]",
)


def _is_non_fast_forward(exc: GitCommandError) -> bool:
    """Return True when a push failure looks like a non-fast-forward rejection."""
    stderr = getattr(exc, "stderr", None) or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    stderr = stderr.lower()
    return any(marker in stderr for marker in _NON_FAST_FORWARD_MARKERS)


class GitRepo(_GitRepo):  # pylint: disable=too-many-instance-attributes
    """Git Repo object to help with git actions."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        path,
        url,
        clone_initially=True,
        base_url=None,
        nautobot_repo_obj=None,
    ):
        """Set attributes to easily interact with Git Repositories."""
        super().__init__(path, url, clone_initially)
        self.base_url = base_url
        self.nautobot_repo_obj = nautobot_repo_obj

    def commit_with_added(self, commit_description):
        """Stage all changes and commit, unless there is nothing to commit.

        Args:
            commit_description (str): the description of commit

        Returns:
            bool: True if a commit was created, False if there were no changes to commit.
        """
        LOGGER.debug("Committing with message `%s`", commit_description)
        self.repo.git.add(self.repo.untracked_files)
        self.repo.git.add(update=True)
        if not self.repo.is_dirty():
            LOGGER.debug("No changes to commit; skipping commit")
            return False
        self.repo.index.commit(commit_description)
        LOGGER.debug("Commit completed")
        return True

    def commit_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        rel_path,
        author_name,
        author_email,
        commit_datetime,
        message,
        previous_path=None,
        device_id=None,
    ) -> bool:
        """Commit a single file as its own commit, rancid-style.

        Optionally ``git mv`` from ``previous_path`` first so a hostname rename is
        recorded as a Git rename (``git log --follow`` then traces the device).
        Only ``rel_path`` (and the rename source) is staged; if that produced no
        staged change the commit is skipped. The commit is authored and committed
        as ``author_name``/``author_email`` with author and committer dates
        backdated to ``commit_datetime``.

        When ``device_id`` is given, a ``Golden-Config-Device-Id`` trailer is
        appended to the commit message so the device-identity map can be rebuilt
        from ``git log`` alone (no shared manifest file). Trailers survive rebase,
        so the map reconstructs correctly even after a concurrent-push recovery.

        Args:
            rel_path (str): repo-relative path of the file to commit.
            author_name (str): commit author/committer name.
            author_email (str): commit author/committer email.
            commit_datetime (datetime): timestamp for GIT_AUTHOR_DATE/GIT_COMMITTER_DATE.
            message (str): commit message.
            previous_path (str, optional): prior repo-relative path to ``git mv`` from.
            device_id (str, optional): Nautobot Device UUID to record as a trailer.

        Returns:
            bool: True if a commit was created, False if there was nothing to commit.
        """
        paths = [rel_path]
        root = self.repo.working_tree_dir
        abs_path = os.path.join(root, rel_path)
        if previous_path and previous_path != rel_path and os.path.exists(os.path.join(root, previous_path)):
            # The new config is already on disk at rel_path (written by the backup
            # play), so a plain `git mv` would fail on the existing destination.
            # Stash the new content, move the old file into place so Git records a
            # rename, then restore the new content on top.
            LOGGER.debug("Renaming `%s` -> `%s` before commit", previous_path, rel_path)
            new_content = None
            if os.path.exists(abs_path):
                with open(abs_path, "rb") as handle:
                    new_content = handle.read()
                os.remove(abs_path)
            self.repo.git.mv(previous_path, rel_path)
            if new_content is not None:
                with open(abs_path, "wb") as handle:
                    handle.write(new_content)
            paths.append(previous_path)

        self.repo.git.add(rel_path)
        if not self.repo.git.status("--porcelain", "--", *paths).strip():
            LOGGER.debug("No staged change for `%s`; skipping commit", rel_path)
            return False

        if device_id:
            message = f"{message}\n\nGolden-Config-Device-Id: {device_id}"

        date_str = commit_datetime.isoformat() if hasattr(commit_datetime, "isoformat") else str(commit_datetime)
        env = {
            **GIT_ENVIRONMENT,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_AUTHOR_DATE": date_str,
            "GIT_COMMITTER_DATE": date_str,
        }
        with self.repo.git.custom_environment(**env):
            self.repo.git.commit("-m", message)
        LOGGER.debug("Committed `%s` as %s <%s> @ %s", rel_path, author_name, author_email, date_str)
        return True

    def _identity_environment(self) -> dict:
        """Retrieve identity environment variables derived from the HEAD commit."""
        head = self.repo.head.commit
        return {
            **GIT_ENVIRONMENT,
            "GIT_AUTHOR_NAME": head.author.name,
            "GIT_AUTHOR_EMAIL": head.author.email,
            "GIT_COMMITTER_NAME": head.committer.name,
            "GIT_COMMITTER_EMAIL": head.committer.email,
        }

    def push(self, max_retries: int = 3) -> None:
        """Push latest to the git repo, recovering from concurrent-update rejections.

        When a push is rejected as non-fast-forward (another worker pushed first),
        fetch from origin, rebase the local branch onto the remote tip, and retry
        the push up to ``max_retries`` times. Other ``GitCommandError`` failures
        (auth, network, etc.) are re-raised immediately. If a rebase produces a
        true conflict, the rebase is aborted and the error is surfaced.

        Args:
            max_retries (int): Maximum push attempts before giving up.
        """
        for attempt in range(1, max_retries + 1):
            try:
                LOGGER.debug("Push changes to repo (attempt %d/%d)", attempt, max_retries)
                self.repo.remotes.origin.push().raise_if_error()
                return
            except GitCommandError as exc:
                if not _is_non_fast_forward(exc) or attempt == max_retries:
                    raise
                LOGGER.debug(
                    "Push attempt %d/%d rejected as non-fast-forward; fetching and rebasing onto remote.",
                    attempt,
                    max_retries,
                )
                self.fetch()
                branch = self.repo.active_branch.name
                try:
                    with self.repo.git.custom_environment(**self._identity_environment()):
                        self.repo.git.rebase(f"origin/{branch}")
                except GitCommandError:
                    LOGGER.debug("Rebase onto origin/%s failed; aborting and surfacing error.", branch)
                    self.repo.git.rebase("--abort")
                    raise
