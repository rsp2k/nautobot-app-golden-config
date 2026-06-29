"""Rancid-style per-device commit logic for Golden Config backups.

Upstream Golden Config writes one commit per backup-job-run, authored by the
automation user and stamped with the job run time. This module turns that into
the rancid model: one commit *per device per snapshot*, authored by the engineer
named on the config's ``Last configuration change`` line and dated to the real
backup time, with hostname renames recorded as Git renames so
``git log --follow`` traces a physical device across its names.

Physical-device identity is the Nautobot Device UUID (``GoldenConfig.device``),
which is stable across renames. The Device-UUID -> last-committed-path map lives
in a ``.golden-rancid-manifest.json`` file inside the backup repo, so the feature
keeps no out-of-band state.
"""

import json
import logging
import os
import re
from datetime import datetime

from django.utils.timezone import make_aware

from nautobot_golden_config.models import GoldenConfig
from nautobot_golden_config.utilities import constant
from nautobot_golden_config.utilities.helper import render_jinja_template

LOGGER = logging.getLogger(__name__)

MANIFEST_NAME = ".golden-rancid-manifest.json"
MANIFEST_VERSION = 1

FALLBACK_AUTHOR_NAME = "Golden Config"

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# `! Last configuration change at 21:32:14 UTC Tue Jun 24 2025 by jdoe`
# The timezone abbreviation is captured but intentionally ignored (Python can't
# resolve arbitrary tz abbreviations reliably); the naive datetime is made aware
# with Django's active timezone.
_LAST_CHANGE_FULL_RE = re.compile(
    r"Last configuration change at\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+"
    r"\S+\s+"  # tz abbreviation, ignored
    r"\w{3}\s+"  # day-of-week, ignored
    r"(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})"
    r"(?:\s+by\s+(?P<user>\S+))?",
    re.IGNORECASE,
)
# Author-only fallback for platforms that omit the full timestamp.
_AUTHOR_ONLY_RE = re.compile(r"Last configuration change.*\bby\s+(?P<user>\S+)", re.IGNORECASE)


def _build_datetime(match):
    """Build a timezone-aware datetime from a full ``_LAST_CHANGE_FULL_RE`` match."""
    month = _MONTHS.get(match.group("mon").lower())
    if not month:
        return None
    try:
        naive = datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
        )
    except ValueError:
        return None
    return make_aware(naive)


def parse_last_change(config_text):
    """Parse the ``Last configuration change`` line of a config.

    Args:
        config_text (str): the (possibly cleaned) device configuration.

    Returns:
        tuple[str | None, datetime | None]: ``(author, changed_at)``. Either may
        be None when the line is missing or only partially present (e.g. the
        author is named but no timestamp, or the line was stripped by a
        ConfigRemove regex).
    """
    if not config_text:
        return None, None
    match = _LAST_CHANGE_FULL_RE.search(config_text)
    if match:
        return match.group("user"), _build_datetime(match)
    author_match = _AUTHOR_ONLY_RE.search(config_text)
    if author_match:
        return author_match.group("user"), None
    return None, None


def _manifest_path(repo_root):
    return os.path.join(repo_root, MANIFEST_NAME)


def load_manifest(repo_root):
    """Load the Device-UUID -> last-committed-path map from the backup repo.

    Returns an empty dict if the manifest is missing or unreadable, so a fresh
    repo (or a corrupted manifest) degrades to "every device is new".
    """
    try:
        with open(_manifest_path(repo_root), encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, ValueError):
        return {}
    if isinstance(data, dict):
        devices = data.get("devices", data)
        if isinstance(devices, dict):
            return dict(devices)
    return {}


def save_manifest(repo_root, devices):
    """Write the Device-UUID -> path map back to the backup repo.

    ``sort_keys`` keeps the file diff-stable so an unchanged map produces no Git
    change (and therefore no needless commit).
    """
    with open(_manifest_path(repo_root), "w", encoding="utf-8") as handle:
        json.dump({"version": MANIFEST_VERSION, "devices": devices}, handle, indent=2, sort_keys=True)
        handle.write("\n")


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
        author, changed_at = parse_last_change(golden.backup_config)
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

    manifest = load_manifest(repo_root)
    snapshots = build_snapshots(job, repo_obj, logger)

    committed = 0
    for snap in snapshots:
        recorded = manifest.get(snap["device_id"])
        rename_from = None
        if recorded and recorded != snap["rel_path"]:
            # Only treat it as a rename if the old file is actually present;
            # a stale manifest entry (manual deletion, etc.) falls back to "new".
            if os.path.exists(os.path.join(repo_root, recorded)):
                rename_from = recorded

        created = repo_obj.commit_file(
            rel_path=snap["rel_path"],
            author_name=snap["author_name"],
            author_email=snap["author_email"],
            commit_datetime=snap["commit_time"],
            message=_commit_message(snap, rename_from),
            previous_path=rename_from,
        )
        manifest[snap["device_id"]] = snap["rel_path"]
        if created:
            committed += 1

    # Persist the manifest; commit_file no-ops if the content is unchanged.
    save_manifest(repo_root, manifest)
    now = make_aware(datetime.now())
    fallback_name, fallback_email = _fallback_identity(repo_obj.repo, constant.RANCID_EMAIL_DOMAIN)
    manifest_committed = repo_obj.commit_file(
        rel_path=MANIFEST_NAME,
        author_name=fallback_name,
        author_email=fallback_email,
        commit_datetime=now,
        message="golden-config: update rancid device manifest",
    )

    if committed or manifest_committed:
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
