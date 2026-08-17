# Copyright (c) 2026 Nordic Semiconductor ASA
# SPDX-License-Identifier: Apache-2.0

"""Parse SDK sync configuration from environment variables.

This module is the single source of truth for settings consumed by
``scripts/sdk_sync/sdk_sync.py`` and the ``sdk-sync`` GitHub Actions workflow.
Configuration is supplied through ``SDK_SYNC_*`` environment variables.
In CI those variables are loaded from ``.github/addon.env`` (see also the
``load-addon-config`` composite action).

Configuration layout
--------------------
Project list
    ``SDK_SYNC_PROJECTS`` is a comma-separated list of west manifest project
    names. Each name ``N`` must have a matching ``SDK_SYNC_PROJECT_N`` variable
    describing where to read the revision from::

        SDK_SYNC_PROJECTS=nrf,matter
        SDK_SYNC_PROJECT_nrf=repo=nrfconnect/sdk-nrf branch=main rebase=false
        SDK_SYNC_PROJECT_matter=repo=nrfconnect/sdk-connectedhomeip branch=ncs-sync rebase=true base=master

    Project records use space-separated ``key=value`` pairs:

    ``repo``
        GitHub repository slug (``owner/name``).
    ``branch``
        Branch whose tip SHA is written into ``west.yml``.
    ``rebase`` (optional, default ``false``)
        When ``true``, rebase ``branch`` onto ``base`` and push before resolving
        the revision. Requires ``base``.
    ``base`` (optional)
        Base branch used when ``rebase=true``.

Branch and pull-request settings
    ``SDK_SYNC_MAIN_BRANCH`` (default: ``main``)
        Main development branch of the add-on repository.
    ``SDK_SYNC_INTEGRATION_BRANCH`` (default: ``integration``)
        Branch that tracks synchronized upstream revisions and is the base for
        sync pull requests.
    ``SDK_SYNC_PR_BRANCH`` (default: ``sdk-sync/test``)
        Head branch containing the manifest bump commit.
    ``SDK_SYNC_PR_LABEL`` (default: ``ci-disabled``)
        Label applied to the opened sync PR. Leave empty to skip labeling.

Manifest and PR text
    ``SDK_SYNC_MANIFEST_COMMIT_PREFIX``
        Subject prefix for the single manifest commit pushed to
        ``SDK_SYNC_PR_BRANCH``.
    ``SDK_SYNC_PR_TITLE``
        Title used when opening the sync pull request.
    ``SDK_SYNC_PR_BODY`` (optional)
        Pull-request body template. Supported placeholders:

        * ``{projects}`` — comma-separated project names (markdown code spans)
        * ``{integration_branch}`` — value of ``SDK_SYNC_INTEGRATION_BRANCH``
        * ``{marker}`` — hidden HTML marker identifying automated sync PRs

Build-time settings (read directly by the workflow, not this module)
    ``SDK_SYNC_TWISTER_PATH``, ``SDK_SYNC_TWISTER_SCENARIO``,
    ``SDK_SYNC_TWISTER_ALL``, and ``SDK_SYNC_PIP_REQUIREMENTS`` are defined in
    the same ``addon.env`` file but are not parsed here.

    ``SDK_SYNC_PIP_REQUIREMENTS`` lists comma-separated requirement file paths
    relative to the west workspace root (for example ``nrf/scripts/...``),
    not relative to the manifest repository checkout directory.

Public API
----------
``load_sync_config()``
    Read and validate all ``SDK_SYNC_*`` settings parsed by this module.
``parse_revision_overrides(raw)``
    Parse optional ``name=sha`` pairs passed on the command line or through
    workflow dispatch inputs.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

KV_PAIR_RE = re.compile(r"(\w+)=([^\s]+)")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class SyncProject:
    """One west manifest project whose revision is managed by sdk-sync."""

    name: str
    repo: str
    branch: str
    rebase: bool
    base_branch: str | None


@dataclass(frozen=True)
class SyncConfig:
    """Validated sdk-sync settings loaded from ``SDK_SYNC_*`` environment variables."""

    projects: tuple[SyncProject, ...]
    main_branch: str
    integration_branch: str
    pr_branch: str
    pr_label: str
    manifest_commit_prefix: str
    pr_title: str
    pr_body_template: str
    pip_requirements: tuple[str, ...]


def _parse_kv_record(value: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in KV_PAIR_RE.finditer(value)}


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env(name: str, *, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not value.strip():
        sys.exit(f"Missing required environment variable: {name}")
    return value.strip()


def _optional_env(name: str, *, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if value else default


def load_sync_config() -> SyncConfig:
    """Load sdk-sync configuration from the environment.

    Exits the process with a descriptive error when a required variable is
    missing or malformed.
    """
    project_names = _split_csv(_env("SDK_SYNC_PROJECTS"))
    if not project_names:
        sys.exit("SDK_SYNC_PROJECTS must list at least one project")

    projects: list[SyncProject] = []
    for name in project_names:
        raw = os.environ.get(f"SDK_SYNC_PROJECT_{name}")
        if not raw:
            sys.exit(f"Missing SDK_SYNC_PROJECT_{name}")
        pairs = _parse_kv_record(raw)
        repo = pairs.get("repo", "")
        branch = pairs.get("branch", "")
        rebase = pairs.get("rebase", "false").lower() == "true"
        base_branch = pairs.get("base")

        if not repo or not REPO_RE.fullmatch(repo):
            sys.exit(f"Invalid repo in SDK_SYNC_PROJECT_{name}: {repo!r}")
        if not branch:
            sys.exit(f"Missing branch in SDK_SYNC_PROJECT_{name}")
        if rebase and not base_branch:
            sys.exit(f"rebase=true requires base= in SDK_SYNC_PROJECT_{name}")

        projects.append(
            SyncProject(
                name=name,
                repo=repo,
                branch=branch,
                rebase=rebase,
                base_branch=base_branch,
            )
        )

    return SyncConfig(
        projects=tuple(projects),
        main_branch=_env("SDK_SYNC_MAIN_BRANCH", default="main"),
        integration_branch=_env("SDK_SYNC_INTEGRATION_BRANCH", default="integration"),
        pr_branch=_env("SDK_SYNC_PR_BRANCH", default="sdk-sync/test"),
        pr_label=_optional_env("SDK_SYNC_PR_LABEL", default="ci-disabled"),
        manifest_commit_prefix=_optional_env(
            "SDK_SYNC_MANIFEST_COMMIT_PREFIX",
            default="manifest: Bump manifest revisions for weekly sync",
        ),
        pr_title=_optional_env("SDK_SYNC_PR_TITLE", default="manifest: Weekly manifest sync test"),
        pr_body_template=_optional_env(
            "SDK_SYNC_PR_BODY",
            default=(
                "Automated sync PR testing updated manifest revisions for {projects} "
                "in `west.yml` on branch `{integration_branch}`.\n\n"
                "Do not merge without review.\n\n{marker}"
            ),
        ),
        pip_requirements=_split_csv(_optional_env("SDK_SYNC_PIP_REQUIREMENTS")),
    )


def parse_revision_overrides(raw: str | None) -> dict[str, str]:
    """Parse comma-separated ``project=sha`` revision overrides.

    Returns an empty dict when *raw* is blank. Each SHA must be at least seven
    characters long.
    """
    if not raw or not raw.strip():
        return {}

    overrides: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            sys.exit(f"Invalid revision override (expected name=sha): {item!r}")
        name, sha = item.split("=", 1)
        name = name.strip()
        sha = sha.strip()
        if not name or len(sha) < 7:
            sys.exit(f"Invalid revision override: {item!r}")
        overrides[name] = sha
    return overrides
