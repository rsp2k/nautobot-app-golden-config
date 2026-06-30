"""Rancid-style per-device commit logic for Golden Config backups.

Upstream Golden Config writes one commit per backup-job-run, authored by the
automation user and stamped with the job run time. This module turns that into
the rancid model: one commit *per device per snapshot*, authored by the engineer
named on the config's ``Last configuration change`` line and dated to the real
backup time, with hostname renames recorded as Git renames so
``git log --follow`` traces a physical device across its names.

Physical-device identity is the Nautobot Device UUID (``GoldenConfig.device``),
which is stable across renames. The Device-UUID -> last-committed-path map is
rebuilt from ``Golden-Config-Device-Id`` git commit trailers on each run, so the
feature keeps no out-of-band state and no shared manifest file to merge-conflict
when concurrent backup jobs rebase-during-push.
"""

import logging
import os
import re
from datetime import datetime

from django.utils.timezone import make_aware

from nautobot_golden_config.models import GoldenConfig
from nautobot_golden_config.utilities import constant
from nautobot_golden_config.utilities.config_parsers import get_parser
from nautobot_golden_config.utilities.helper import render_jinja_template

LOGGER = logging.getLogger(__name__)

DEVICE_ID_TRAILER = "Golden-Config-Device-Id"

FALLBACK_AUTHOR_NAME = "Golden Config"

# Matches the device-id trailer anywhere in a commit body.
_DEVICE_ID_TRAILER_RE = re.compile(rf"^{re.escape(DEVICE_ID_TRAILER)}:\s*(\S+)\s*$", re.MULTILINE)


def parse_last_change(config_text, network_driver=None):
    """Parse the ``Last configuration change`` line of a config.

    Args:
        config_text (str): the (possibly cleaned) device configuration.
        network_driver (str, optional): the network driver name for
            platform-specific parsing. ``None`` selects the generic parser that
            tries every registered platform parser in turn.

    Returns:
        tuple[str | None, datetime | None]: ``(author, changed_at)``. Either may
        be None when the line is missing or only partially present (e.g. the
        author is named but no timestamp, or the line was stripped by a
        ConfigRemove regex).
    """
    parser = get_parser(network_driver)
    return parser.parse(config_text)


def build_manifest_from_git(repo_obj):
    """Build the Device-UUID -> path map from git commit trailers.

    Scans all commits in the repository for the ``Golden-Config-Device-Id``
    trailer, extracting the device UUID and the config file it touched. For each
    device UUID encountered while scanning newest-first, only the first (newest)
    occurrence is kept, so the map reflects each device's current path.

    Trailers survive ``git rebase`` (the commit message is copied verbatim), so
    this map is reconstructed correctly even after a concurrent push forced a
    rebase, with no shared manifest file to conflict on.

    Args:
        repo_obj: The GitRepo instance.

    Returns:
        dict[str, str]: {device_uuid: relative_path, ...}
    """
    # Sentinel-delimited records so config/message text can't desync the parse:
    #   \x01 starts a commit record, \x02 ends the SHA, \x03 ends the body; the
    #   --name-only file list follows the body up to the next \x01. The device id
    #   is read from the full body (%B) by regex, which sidesteps the trailing
    #   newline quirks of git's %(trailers:...,valueonly) atom.
    try:
        # --name-status (not --name-only) so a rename commit yields the CURRENT
        # path: a detected rename is "R<score>\told\tnew" (take new), an add/modify
        # is "A|M\tpath", and a delete "D\tpath" is skipped. Picking files[0] from
        # --name-only would grab the deleted old path when git doesn't fold the
        # rename into a single entry.
        output = repo_obj.repo.git.log(
            "--all",
            "--name-status",
            "--pretty=format:\x01%H\x02%B\x03",
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        LOGGER.debug("Could not build manifest from git log: %s", error)
        return {}

    manifest = {}
    for record in output.split("\x01"):
        if "\x02" not in record or "\x03" not in record:
            continue
        _sha, rest = record.split("\x02", 1)
        body, files_part = rest.split("\x03", 1)

        match = _DEVICE_ID_TRAILER_RE.search(body)
        if not match:
            continue
        device_id = match.group(1)
        if device_id in manifest:
            # git log is newest-first; keep the first (current) path seen.
            continue

        current_path = _current_path_from_status(files_part)
        if current_path:
            manifest[device_id] = current_path

    return manifest


def _current_path_from_status(files_part):
    """Return the device's current path from a ``--name-status`` file block.

    Picks the rename target for ``R`` entries and the path for ``A``/``M``, while
    skipping ``D`` (deleted) entries. Returns the first such path (each device
    commit touches exactly one config file).
    """
    for line in files_part.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("D"):
            continue
        if status.startswith("R") and len(parts) >= 3:
            return parts[2]
        if len(parts) >= 2:
            return parts[1]
    return None


def _fallback_identity(repo, domain):
    """Resolve the automation identity to use when a config names no author."""
    name = FALLBACK_AUTHOR_NAME
    email = f"golden-config@{domain}"
    try:
        reader = repo.config_reader()
        name = reader.get_value("user", "name", FALLBACK_AUTHOR_NAME) or name
        email = reader.get_value("user", "email", email) or email
    except Exception as error:  # pylint: disable=broad-exception-caught
        LOGGER.debug("Could not read git identity from repo config; using defaults: %s", error)
    return str(name), str(email)


def build_snapshots(job, repo_obj, logger):
    """Build the ordered list of per-device commit specs for one backup repo.

    Only devices whose Golden Config settings point at ``repo_obj`` are included,
    and only those with a stored backup config. Snapshots are sorted by commit
    time so the resulting history is chronological.
    """
    repo_id = repo_obj.nautobot_repo_obj.id
    domain = constant.RANCID_EMAIL_DOMAIN
    fallback_name, fallback_email = _fallback_identity(repo_obj.repo, domain)

    snapshots = []
    for device_id, settings in job.device_to_settings_map.items():
        if getattr(settings, "backup_repository_id", None) != repo_id:
            continue
        golden = GoldenConfig.objects.filter(device_id=device_id).first()
        if not golden or not golden.backup_config:
            continue

        device = golden.device
        rel_path = render_jinja_template(device, logger, settings.backup_path_template)
        network_driver = getattr(device.platform, "network_driver", None) if device.platform else None
        author, changed_at = parse_last_change(golden.backup_config, network_driver=network_driver)
        commit_time = changed_at or golden.backup_last_success_date or make_aware(datetime.now())
        if author:
            author_name, author_email = author, f"{author}@{domain}"
        else:
            author_name, author_email = fallback_name, fallback_email

        snapshots.append(
            {
                "device_id": str(device_id),
                "device_name": device.name,
                "rel_path": rel_path,
                "author_name": author_name,
                "author_email": author_email,
                "commit_time": commit_time,
            }
        )

    snapshots.sort(key=lambda snap: snap["commit_time"])

    # Per-device identity assumes one file per device. If two devices render the
    # same backup_path_template output they would clobber each other on disk and
    # both claim the same path in history, so surface it loudly rather than
    # corrupting the identity map silently.
    by_path = {}
    for snap in snapshots:
        by_path.setdefault(snap["rel_path"], []).append(snap["device_name"])
    for path, names in by_path.items():
        if len(names) > 1:
            logger.error(
                f"`E3028:` Multiple devices resolve to the same backup path `{path}`: "
                f"{', '.join(sorted(names))}. Fix backup_path_template so each device is unique.",
                extra={"grouping": "GC Repo Commit and Push"},
            )

    return snapshots


def _commit_message(snap, renamed_from):
    """Compose the per-device commit subject."""
    when = snap["commit_time"].strftime("%Y-%m-%d %H:%M %Z").strip()
    if renamed_from:
        return f"{snap['device_name']} (renamed from {renamed_from}): backup @ {when}"
    return f"{snap['device_name']}: backup @ {when}"


def rancid_commit_and_push(job, repo):
    """Commit one Git commit per changed device, then push, for one backup repo.

    Args:
        job (Job): the running Golden Config job (provides logger + settings map).
        repo (dict): an entry from ``current_repos`` (``repo_obj`` + ``to_commit``).

    Returns:
        int: number of per-device commits created.
    """
    repo_obj = repo["repo_obj"]
    repo_root = repo_obj.nautobot_repo_obj.filesystem_path
    logger = job.logger

    manifest = build_manifest_from_git(repo_obj)
    snapshots = build_snapshots(job, repo_obj, logger)

    committed = 0
    for snap in snapshots:
        recorded = manifest.get(snap["device_id"])
        rename_from = None
        if recorded and recorded != snap["rel_path"]:
            # Only treat it as a rename if the old file is actually present;
            # a stale trailer entry (manual deletion, etc.) falls back to "new".
            if os.path.exists(os.path.join(repo_root, recorded)):
                rename_from = recorded

        created = repo_obj.commit_file(
            rel_path=snap["rel_path"],
            author_name=snap["author_name"],
            author_email=snap["author_email"],
            commit_datetime=snap["commit_time"],
            message=_commit_message(snap, rename_from),
            previous_path=rename_from,
            device_id=snap["device_id"],
        )
        if created:
            committed += 1

    if committed:
        logger.info(
            f"{repo_obj.nautobot_repo_obj.name}: created {committed} per-device backup commit(s).",
            extra={"grouping": "GC Repo Commit and Push", "object": repo_obj.nautobot_repo_obj},
        )
        repo_obj.push()
    else:
        logger.info(
            f"{repo_obj.nautobot_repo_obj.name}: no configuration changes to commit.",
            extra={"grouping": "GC Repo Commit and Push", "object": repo_obj.nautobot_repo_obj},
        )
    return committed
