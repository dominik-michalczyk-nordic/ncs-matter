# Copyright (c) 2026 Nordic Semiconductor ASA
# SPDX-License-Identifier: Apache-2.0

"""SDK sync automation: rebase, resolve revisions, update west.yml, open PR."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from addon_config import load_sync_config, parse_revision_overrides  # noqa: E402
from common import (  # noqa: E402
    SDK_SYNC_PR_MARKER,
    branch_tip,
    commits_ahead,
    configure_git_user,
    display_revision,
    find_open_pr,
    git_sign_off_line,
    github_headers,
    github_token,
    parse_env_file,
    read_json,
    run_git,
    write_json,
)

REVISION_LINE = re.compile(r"^(\s*)revision:\s*(.+?)\s*$")


def cmd_load_env(*, config_file: Path) -> None:
    """Print the SDK_SYNC_* entries from an add-on config file as KEY=VALUE lines.

    Intended to be redirected into $GITHUB_ENV so later workflow steps (and later
    invocations of this script, which read the same variables via os.environ) see
    the add-on's configuration:

        python sdk_sync.py load-env --config-file .github/addon.env >> "$GITHUB_ENV"
    """
    if not config_file.exists():
        sys.exit(f"Missing {config_file}")

    entries = {key: value for key, value in parse_env_file(config_file).items() if key.startswith("SDK_SYNC_")}
    if "SDK_SYNC_PROJECTS" not in entries:
        sys.exit(f"Missing SDK_SYNC_PROJECTS in {config_file}")

    for key, value in entries.items():
        print(f"{key}={value}")


def cmd_rebase() -> None:
    config = load_sync_config()
    rebase_projects = [project for project in config.projects if project.rebase]
    if not rebase_projects:
        print("No projects configured with rebase=true; skipping")
        return

    token = github_token()
    configure_git_user()
    for project in rebase_projects:
        if not project.base_branch:
            sys.exit(f"Project {project.name} has rebase=true but no base branch")

        workdir = Path(tempfile.mkdtemp(prefix="sdk-sync-rebase-"))
        clone_url = f"https://x-access-token:{token}@github.com/{project.repo}.git"
        try:
            clone = run_git(["clone", clone_url, str(workdir)], check=False)
            if clone.returncode != 0:
                print(clone.stderr, file=sys.stderr)
                sys.exit(f"Failed to clone {project.repo}")

            run_git(["fetch", "origin", project.base_branch, project.branch], cwd=workdir)
            checkout = run_git(["checkout", project.branch], cwd=workdir, check=False)
            if checkout.returncode != 0:
                run_git(["checkout", "-B", project.branch, f"origin/{project.branch}"], cwd=workdir)

            rebase = run_git(["rebase", f"origin/{project.base_branch}"], cwd=workdir, check=False)
            if rebase.returncode != 0:
                run_git(["rebase", "--abort"], cwd=workdir, check=False)
                print(rebase.stderr, file=sys.stderr)
                sys.exit(f"Rebase of {project.repo}:{project.branch} onto {project.base_branch} failed")

            push = run_git(["push", "--force-with-lease", "origin", project.branch], cwd=workdir, check=False)
            if push.returncode != 0:
                print(push.stderr, file=sys.stderr)
                sys.exit(f"Failed to push rebased {project.branch} to {project.repo}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        print(f"Rebased {project.repo}:{project.branch} onto {project.base_branch} and pushed")


def cmd_resolve(*, state_file: Path, revisions: str) -> None:
    config = load_sync_config()
    overrides = parse_revision_overrides(revisions)
    unknown = set(overrides) - {project.name for project in config.projects}
    if unknown:
        sys.exit(f"Unknown revision override projects: {sorted(unknown)}")

    resolved = {
        project.name: overrides.get(project.name) or branch_tip(repo=project.repo, branch=project.branch)
        for project in config.projects
    }

    state = read_json(state_file) if state_file.exists() else {}
    state["revisions"] = resolved
    write_json(state_file, state)

    for name, sha in resolved.items():
        print(f"{name}: {sha}")


def update_manifest_text(*, text: str, revisions: dict[str, str]) -> str:
    current_project: str | None = None
    updated_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("- name: "):
            current_project = stripped.removeprefix("- name: ").strip()

        match = REVISION_LINE.match(line.rstrip("\n"))
        if match and current_project in revisions:
            indent = match.group(1)
            newline = "\n" if line.endswith("\n") else ""
            updated_lines.append(f"{indent}revision: {revisions[current_project]}{newline}")
            continue

        updated_lines.append(line)

    return "".join(updated_lines)


def patch_west_yml(*, west_yml: Path, revisions: dict[str, str]) -> bool:
    original = west_yml.read_text(encoding="utf-8")
    current: dict[str, str] = {}
    current_project: str | None = None
    for line in original.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("- name: "):
            current_project = stripped.removeprefix("- name: ").strip()
            continue
        match = REVISION_LINE.match(line)
        if match and current_project in revisions:
            current[current_project] = match.group(2).strip()

    if all(current.get(name) == sha for name, sha in revisions.items()):
        return False

    west_yml.write_text(update_manifest_text(text=original, revisions=revisions), encoding="utf-8")
    return True


def manifest_commit_message(*, prefix: str, revisions: dict[str, str]) -> str:
    lines = [prefix, ""]
    for name, sha in revisions.items():
        lines.append(f"{name} revision: {display_revision(sha)}")
    lines.extend(["", git_sign_off_line()])
    return "\n".join(lines) + "\n"


def is_manifest_tip(*, repo_root: Path, base_branch: str, prefix: str) -> bool:
    tip_subject = run_git(["log", "-1", "--format=%s"], cwd=repo_root).stdout.strip()
    if not tip_subject.startswith(prefix):
        return False

    parent = run_git(["rev-parse", "HEAD^"], cwd=repo_root, check=False)
    if parent.returncode != 0:
        return False

    base_sha = run_git(["rev-parse", f"origin/{base_branch}"], cwd=repo_root).stdout.strip()
    return parent.stdout.strip() == base_sha


def commit_manifest(
    *,
    west_yml: Path,
    repo_root: Path,
    branch: str,
    revisions: dict[str, str],
    amend: bool,
    commit_prefix: str,
) -> None:
    configure_git_user()
    message = manifest_commit_message(prefix=commit_prefix, revisions=revisions)
    run_git(["add", str(west_yml.relative_to(repo_root))], cwd=repo_root)
    if amend:
        run_git(["commit", "--amend", "-m", message], cwd=repo_root)
        action = "Amended manifest commit on"
    else:
        run_git(["commit", "-m", message], cwd=repo_root)
        action = "Created manifest commit on"

    push = run_git(
        ["push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=repo_root,
        check=False,
    )
    if push.returncode != 0:
        print(push.stderr, file=sys.stderr)
        sys.exit(f"Failed to push manifest update to {branch}")
    print(f"{action} {branch}")


def cmd_patch(*, state_file: Path, west_yml: Path) -> None:
    revisions = read_json(state_file).get("revisions", {})
    if not revisions:
        sys.exit("No revisions in state file")

    if patch_west_yml(west_yml=west_yml, revisions=revisions):
        print(f"Updated {west_yml}")
    else:
        print(f"{west_yml} already points at the requested revisions")


def cmd_push(*, repo: str, state_file: Path, west_yml_name: str) -> None:
    config = load_sync_config()
    state = read_json(state_file)
    revisions = state.get("revisions", {})
    if not revisions:
        sys.exit("No revisions in state file")

    existing_pr = find_open_pr(
        repo=repo,
        head_branch=config.pr_branch,
        base_branch=config.integration_branch,
    )
    if existing_pr is not None:
        print(f"Open sync PR {existing_pr} found; will replace manifest commit on {config.pr_branch}")
        state["pr_number"] = existing_pr
        write_json(state_file, state)

    configure_git_user()
    run_git(["fetch", "origin", config.integration_branch])
    run_git(["fetch", "origin", config.pr_branch], check=False)

    worktree = Path(tempfile.mkdtemp(prefix="sdk-sync-manifest-"))
    try:
        if existing_pr is not None:
            checkout = run_git(
                ["worktree", "add", "-B", config.pr_branch, str(worktree), f"origin/{config.pr_branch}"],
                check=False,
            )
            if checkout.returncode != 0:
                print(checkout.stderr, file=sys.stderr)
                sys.exit(f"Failed to check out existing branch {config.pr_branch}")

            west_yml = worktree / west_yml_name
            if not patch_west_yml(west_yml=west_yml, revisions=revisions):
                print("west.yml already points at the requested revisions")
                return

            amend = is_manifest_tip(
                repo_root=worktree,
                base_branch=config.integration_branch,
                prefix=config.manifest_commit_prefix,
            )
            if not amend:
                reset = run_git(
                    ["reset", "--hard", f"origin/{config.integration_branch}"],
                    cwd=worktree,
                    check=False,
                )
                if reset.returncode != 0:
                    sys.exit(f"Failed to reset {config.pr_branch} onto {config.integration_branch}")
                if not patch_west_yml(west_yml=west_yml, revisions=revisions):
                    sys.exit("Manifest update missing after reset onto base branch")

            commit_manifest(
                west_yml=west_yml,
                repo_root=worktree,
                branch=config.pr_branch,
                revisions=revisions,
                amend=amend,
                commit_prefix=config.manifest_commit_prefix,
            )
            return

        print(f"Creating {config.pr_branch} from {config.integration_branch} with one manifest commit")
        add = run_git(
            ["worktree", "add", "-B", config.pr_branch, str(worktree), f"origin/{config.integration_branch}"],
            check=False,
        )
        if add.returncode != 0:
            print(add.stderr, file=sys.stderr)
            sys.exit(f"Failed to create branch {config.pr_branch}")

        west_yml = worktree / west_yml_name
        if not patch_west_yml(west_yml=west_yml, revisions=revisions):
            print("west.yml on base branch already points at the requested revisions")
            return

        commit_manifest(
            west_yml=west_yml,
            repo_root=worktree,
            branch=config.pr_branch,
            revisions=revisions,
            amend=False,
            commit_prefix=config.manifest_commit_prefix,
        )
    finally:
        run_git(["worktree", "remove", "--force", str(worktree)], check=False)
        shutil.rmtree(worktree, ignore_errors=True)


def cmd_sync_integration() -> None:
    config = load_sync_config()
    configure_git_user()
    run_git(["fetch", "origin", config.main_branch, config.integration_branch])
    refspec = f"refs/remotes/origin/{config.main_branch}:refs/heads/{config.integration_branch}"
    push = run_git(["push", "--force-with-lease", "origin", refspec], check=False)
    if push.returncode != 0:
        print(push.stderr, file=sys.stderr)
        sys.exit(f"Failed to push synchronized {config.integration_branch}")
    print(f"Synchronized {config.integration_branch} with origin/{config.main_branch}")


def ensure_pr_branch_signed_off(*, branch: str) -> None:
    configure_git_user()
    fetch = run_git(["fetch", "origin", branch], check=False)
    if fetch.returncode != 0:
        return

    ref = run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"], check=False)
    if ref.returncode != 0:
        return

    sign_off = git_sign_off_line()
    body = run_git(["log", "-1", "--format=%B", f"origin/{branch}"]).stdout
    if sign_off in body:
        return

    worktree = Path(tempfile.mkdtemp(prefix="sdk-sync-signoff-"))
    try:
        checkout = run_git(
            ["worktree", "add", "-B", branch, str(worktree), f"origin/{branch}"],
            check=False,
        )
        if checkout.returncode != 0:
            print(checkout.stderr, file=sys.stderr)
            sys.exit(f"Failed to check out {branch} for Signed-off-by fix")

        amend = run_git(["commit", "--amend", "--no-edit", "-s"], cwd=worktree, check=False)
        if amend.returncode != 0:
            print(amend.stderr, file=sys.stderr)
            sys.exit("Failed to amend commit with Signed-off-by")

        push = run_git(
            ["push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=worktree,
            check=False,
        )
        if push.returncode != 0:
            print(push.stderr, file=sys.stderr)
            sys.exit(f"Failed to push Signed-off-by fix to {branch}")
        print(f"Added {sign_off} to tip commit on {branch}")
    finally:
        run_git(["worktree", "remove", "--force", str(worktree)], check=False)
        shutil.rmtree(worktree, ignore_errors=True)


def ensure_pr_label(*, repo: str, pr_number: int, label: str) -> None:
    encoded = requests.utils.quote(label, safe="")
    response = requests.get(
        f"https://api.github.com/repos/{repo}/labels/{encoded}",
        headers=github_headers(),
        timeout=60,
    )
    if response.status_code != 200:
        create = requests.post(
            f"https://api.github.com/repos/{repo}/labels",
            headers=github_headers(),
            json={"name": label, "color": "0E8A16", "description": "Skip CI for automated sdk-sync pull requests"},
            timeout=60,
        )
        if create.status_code >= 400 and create.status_code != 422:
            sys.exit(f"Failed to create label {label!r}")

    response = requests.post(
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/labels",
        headers=github_headers(),
        json=[label],
        timeout=60,
    )
    if response.status_code >= 400:
        sys.exit(f"Failed to add label {label!r} to PR #{pr_number}")


def cmd_open_pr(*, repo: str, state_file: Path) -> None:
    config = load_sync_config()
    state = read_json(state_file)
    revisions = state.get("revisions", {})
    if not revisions:
        sys.exit("No revisions in state file")

    ensure_pr_branch_signed_off(branch=config.pr_branch)

    pr_number = state.get("pr_number")
    if isinstance(pr_number, int):
        print(f"Keeping open sync PR #{pr_number}")
        if config.pr_label:
            ensure_pr_label(repo=repo, pr_number=pr_number, label=config.pr_label)
        return

    existing = find_open_pr(
        repo=repo,
        head_branch=config.pr_branch,
        base_branch=config.integration_branch,
    )
    if existing is not None:
        print(f"Keeping open sync PR #{existing}")
        state["pr_number"] = existing
        write_json(state_file, state)
        if config.pr_label:
            ensure_pr_label(repo=repo, pr_number=existing, label=config.pr_label)
        return

    ahead = commits_ahead(
        repo=repo,
        base_branch=config.integration_branch,
        head_branch=config.pr_branch,
    )
    if ahead == 0:
        print(f"No commits between {config.integration_branch} and {config.pr_branch}; skipping PR")
        return

    project_names = ", ".join(f"`{name}`" for name in revisions)
    body = config.pr_body_template.format(
        projects=project_names,
        integration_branch=config.integration_branch,
        marker=SDK_SYNC_PR_MARKER,
    )
    response = requests.post(
        f"https://api.github.com/repos/{repo}/pulls",
        headers=github_headers(),
        json={
            "title": config.pr_title,
            "head": config.pr_branch,
            "base": config.integration_branch,
            "body": body,
        },
        timeout=60,
    )
    if response.status_code >= 400:
        sys.exit(f"Failed to create PR: {response.status_code} {response.text}")

    number = int(response.json()["number"])
    state["pr_number"] = number
    write_json(state_file, state)
    print(f"Created PR #{number} ({config.pr_branch} -> {config.integration_branch})")
    if config.pr_label:
        ensure_pr_label(repo=repo, pr_number=number, label=config.pr_label)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_repo = os.environ.get("GITHUB_REPOSITORY")
    parser.add_argument("--repo", default=default_repo, required=default_repo is None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_env = subparsers.add_parser(
        "load-env", help="Print SDK_SYNC_* entries from a config file as KEY=VALUE lines"
    )
    load_env.add_argument("--config-file", type=Path, required=True)

    subparsers.add_parser("rebase", help="Rebase projects configured with rebase=true")

    resolve = subparsers.add_parser("resolve", help="Resolve manifest project revisions")
    resolve.add_argument("--state-file", type=Path, required=True)
    resolve.add_argument("--revisions", default="", help="Optional name=sha overrides")

    patch = subparsers.add_parser("patch", help="Patch west.yml in place")
    patch.add_argument("--state-file", type=Path, required=True)
    patch.add_argument("--west-yml", type=Path, required=True)

    push = subparsers.add_parser("push", help="Commit and push west.yml to the sync PR branch")
    push.add_argument("--state-file", type=Path, required=True)
    push.add_argument("--west-yml", default="west.yml")

    subparsers.add_parser("sync-integration", help="Force-sync integration branch from main")

    open_pr = subparsers.add_parser("open-pr", help="Open or reuse the sync pull request")
    open_pr.add_argument("--state-file", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "load-env":
        cmd_load_env(config_file=args.config_file)
    elif args.command == "rebase":
        cmd_rebase()
    elif args.command == "resolve":
        cmd_resolve(state_file=args.state_file, revisions=args.revisions)
    elif args.command == "patch":
        cmd_patch(state_file=args.state_file, west_yml=args.west_yml)
    elif args.command == "push":
        cmd_push(repo=args.repo, state_file=args.state_file, west_yml_name=args.west_yml)
    elif args.command == "sync-integration":
        cmd_sync_integration()
    elif args.command == "open-pr":
        cmd_open_pr(repo=args.repo, state_file=args.state_file)


if __name__ == "__main__":
    main()
