"""Concourse resource for git release orchestration.

resources:
  - name: app-release
    type: release
    check_every: never
    source:
      uri: git@github.com:mitodl/my-app.git
      branch: main
      private_key: ((github.private_key))
      access_token: ((github.token))
      repository: mitodl/my-app
      changelog_style: cumulative   # or "per_release", or omit to disable
      changelog_file: CHANGELOG.md  # only used when changelog_style=cumulative
      changelog_dir: releases        # only used when changelog_style=per_release
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

from concoursetools import BuildMetadata, ConcourseResource, TypedVersion
from github import Auth, Github

VERSION_PATTERN = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})\.(\d+)$")

# Default minimum clone depth.  Configurable via the ``clone_depth`` source
# param.  Increase if the previous release tag is more than this many commits
# back — a full clone (``clone_depth: 0``) is the safest option for busy repos.
_DEFAULT_CLONE_DEPTH = 200

CHANGELOG_HEADER = """\
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
"""


# ---------------------------------------------------------------------------
# Version type
# ---------------------------------------------------------------------------


@dataclass
class ReleaseVersion(TypedVersion):
    """Version emitted by the release resource.

    All fields are strings to satisfy the Concourse version protocol.

    ``head_sha`` binds the version to an immutable commit so that subsequent
    ``in`` and ``out`` steps operate on exactly the same code that was
    evaluated during ``check``, eliminating race conditions from new commits
    landing between steps.

    ``commit_count`` and ``authors`` are lightweight metadata included so
    that the Slack release bot can surface a human-readable summary from the
    Concourse version object alone (e.g. ``/release-notes``) without needing
    to trigger the full pipeline.  They are not used for version ordering.
    """

    version: str  # YYYY.M.D.N (no leading zeros — PEP 440 compliant)
    head_sha: str  # full SHA of HEAD at check time
    since: str  # previous tag (YYYY.M.D.N or empty string when no prior tags)
    commit_count: str  # number of commits since last release tag
    authors: str  # comma-separated sorted author email list


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


class ReleaseResource(ConcourseResource[ReleaseVersion]):
    """Concourse resource for git release orchestration.

    Implements:
    - check: Detect unreleased commits and compute the next YYYY.M.D.N version.
    - in:    Write version, commits.json, checklist.md, and changelog_entry.md.
    - out:   Create the release branch/tag and optional changelog file (action=create),
             or merge the release branch back to main (action=finish).
    """

    def __init__(  # noqa: PLR0913
        self,
        /,
        uri: str,
        branch: str = "main",
        private_key: str | None = None,
        access_token: str | None = None,
        repository: str | None = None,
        git_user_name: str = "Concourse CI",
        git_user_email: str = "concourse@example.com",
        changelog_style: Literal["cumulative", "per_release"] | None = None,
        changelog_file: str = "CHANGELOG.md",
        changelog_dir: str = "releases",
        clone_depth: int = _DEFAULT_CLONE_DEPTH,
    ) -> None:
        """Initialize the release resource with git and GitHub configuration."""
        super().__init__(ReleaseVersion)
        self.uri = uri
        self.branch = branch
        self.private_key = private_key
        self.access_token = access_token
        self.repository = repository
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.changelog_style = changelog_style
        self.changelog_file = changelog_file
        self.changelog_dir = changelog_dir
        self.clone_depth = clone_depth

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------

    def fetch_new_versions(
        self, previous_version: ReleaseVersion | None
    ) -> list[ReleaseVersion]:
        """Return the next release version if unreleased commits exist.

        Returns a single-element list containing the next YYYY.MM.DD.N
        version when HEAD of the tracked branch has moved past the latest
        release tag, or the existing latest version when no new commits exist.
        """
        with (
            _git_ssh_env(self.private_key) as env,
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            repo_path = Path(tmpdir) / "repo"
            _clone(self.uri, repo_path, env=env, depth=self.clone_depth)
            return self._compute_versions(repo_path, env=env)

    def _compute_versions(self, repo_path: Path, env: dict) -> list[ReleaseVersion]:
        tags = _get_release_tags(repo_path, env=env)
        latest_tag = tags[-1] if tags else None

        head_sha = _run(
            ["git", "rev-parse", f"origin/{self.branch}"],
            cwd=repo_path,
            env=env,
        ).strip()

        if latest_tag:
            tag_sha = _run(
                ["git", "rev-list", "-n1", latest_tag],
                cwd=repo_path,
                env=env,
            ).strip()
            prev_tag = tags[-2] if len(tags) >= 2 else ""  # noqa: PLR2004

            if tag_sha == head_sha:
                # HEAD is already tagged — no new commits
                count, authors = _commit_info_range(
                    repo_path, prev_tag, head_sha, env=env
                )
                return [
                    ReleaseVersion(
                        version=latest_tag,
                        head_sha=head_sha,
                        since=prev_tag,
                        commit_count=str(count),
                        authors=authors,
                    )
                ]

            count, authors = _commit_info_range(
                repo_path, latest_tag, head_sha, env=env
            )
        else:
            count, authors = _commit_info_all(repo_path, head_sha, env=env)

        next_version = _compute_next_version(tags)
        return [
            ReleaseVersion(
                version=next_version,
                head_sha=head_sha,
                since=latest_tag or "",
                commit_count=str(count),
                authors=authors,
            )
        ]

    # ------------------------------------------------------------------
    # in (get)
    # ------------------------------------------------------------------

    def download_version(
        self,
        version: ReleaseVersion,
        destination_dir: Path,
        build_metadata: BuildMetadata,
    ) -> tuple[ReleaseVersion, dict[str, str]]:
        """Write release metadata to destination_dir.

        Outputs:
        - version            plain version string
        - commits.json       structured commit list
        - checklist.md       GitHub Issue body (for use as body_file)
        - changelog_entry.md single Keep a Changelog entry for this version
        """
        with (
            _git_ssh_env(self.private_key) as env,
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            repo_path = Path(tmpdir) / "repo"
            _clone(self.uri, repo_path, env=env, depth=self.clone_depth)
            commits = self._collect_commits(repo_path, version, env=env)

        destination_dir.mkdir(parents=True, exist_ok=True)
        (destination_dir / "version").write_text(version.version)
        (destination_dir / "commits.json").write_text(json.dumps(commits, indent=2))
        (destination_dir / "checklist.md").write_text(
            _build_checklist(version.version, commits)
        )
        (destination_dir / "changelog_entry.md").write_text(
            _build_changelog_entry(version.version, commits)
        )

        return version, {
            "version": version.version,
            "commit_count": version.commit_count,
        }

    def _collect_commits(
        self, repo_path: Path, version: ReleaseVersion, env: dict
    ) -> list[dict]:
        """Return enriched commit list between the previous tag and head_sha."""
        if version.since:
            range_spec = f"{version.since}..{version.head_sha}"
        else:
            range_spec = version.head_sha
        output = _run(
            ["git", "log", "--format=%H|%ae|%s", range_spec],
            cwd=repo_path,
            env=env,
        )

        commits: list[dict] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:  # noqa: PLR2004
                continue
            sha, author_email, message = parts
            commits.append(
                {
                    "sha": sha.strip(),
                    "author": author_email.strip(),
                    "message": message.strip(),
                    "pr_number": None,
                    "pr_title": None,
                }
            )

        if self.access_token and self.repository:
            commits = _enrich_with_github(commits, self.access_token, self.repository)

        return commits

    def publish_new_version(  # noqa: PLR0913
        self,
        sources_dir: Path,
        build_metadata: BuildMetadata,
        *,
        action: Literal["create", "finish"],
        repo_dir: str,
        version_file: str,
        commit_hash: str | None = None,
    ) -> tuple[ReleaseVersion, dict[str, str]]:
        """Create or finish a release.

        action=create:
          1. Record the pre-bumpver HEAD SHA (this becomes the release tag).
          2. Create release/YYYY.MM.DD.N branch.
          3. Optionally cherry-pick commit_hash (hotfix) first.
          4. Stage + commit dirty files (version bump from bump_version_task)
             and the changelog update in a single "Release YYYY.MM.DD.N" commit.
          5. Push branch and tag.

        action=finish:
          1. Fetch the configured branch and the release branch.
          2. Merge release/YYYY.MM.DD.N into the configured branch (--no-ff).
          3. Push.

        ``repo_dir`` is the path within the Concourse workspace to the
        checked-out git repository (i.e. the ``get`` output directory name).
        This is intentionally distinct from the source-level ``repository``
        field (which holds ``owner/repo`` for GitHub API calls).
        """
        if action not in ("create", "finish"):
            msg = f"Invalid action '{action}'. Must be 'create' or 'finish'."
            raise ValueError(msg)

        version_str = (sources_dir / version_file).read_text().strip()
        repo_path = sources_dir / repo_dir

        with _git_ssh_env(self.private_key) as env:
            _configure_git_identity(
                repo_path, self.git_user_name, self.git_user_email, env=env
            )
            if self.access_token:
                _configure_https_auth(repo_path, self.access_token, env=env)

            if action == "create":
                head_sha = self._create_release(
                    repo_path, version_str, commit_hash, env=env
                )
            else:
                head_sha = self._finish_release(repo_path, version_str, env=env)

        return (
            ReleaseVersion(
                version=version_str,
                head_sha=head_sha,
                since="",
                commit_count="0",
                authors="",
            ),
            {"version": version_str, "action": action},
        )

    def _create_release(
        self,
        repo_path: Path,
        version: str,
        commit_hash: str | None,
        env: dict,
    ) -> str:
        """Create the release branch and version tag.

        Returns the pre-bumpver HEAD SHA that was used as the release tag.

        Commit ordering:
          1. Cherry-pick hotfix (if any) onto the branch first so it is
             included in the release notes range.
          2. Stage version-bump file changes (from bump_version_task) and
             the changelog update in a single "Release {version}" commit so
             that the entire release is one atomic unit.
        """
        branch_name = f"release/{version}"

        _run(["git", "fetch", "origin", self.branch, "--tags"], cwd=repo_path, env=env)

        # Ensure we are on the correct branch and up-to-date with the remote
        # before capturing the pre-bump SHA; avoids tagging the wrong commit
        # if the workspace is in a detached-HEAD state.
        _run(["git", "checkout", self.branch], cwd=repo_path, env=env)
        _run(
            ["git", "reset", "--hard", f"origin/{self.branch}"],
            cwd=repo_path,
            env=env,
        )

        # The tag marks the last commit of the source that was cut for this
        # release — the HEAD of the tracked branch before any release machinery
        # runs.  This matches what was built and pushed to the container registry.
        pre_bump_sha = _run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, env=env
        ).strip()

        _run(
            ["git", "checkout", "-b", branch_name],
            cwd=repo_path,
            env=env,
        )

        # Cherry-pick hotfix commit first so it is included in the release
        # note range and committed before the release metadata files.
        if commit_hash:
            _run(["git", "cherry-pick", commit_hash], cwd=repo_path, env=env)

        # Collect commits for changelog (between last tag and pre-bump HEAD,
        # plus the hotfix commit if present).
        prior_tags = _get_release_tags(repo_path, env=env)
        since_ref = prior_tags[-1] if prior_tags else ""
        until_ref = (
            _run(["git", "rev-parse", "HEAD"], cwd=repo_path, env=env).strip()
            if commit_hash
            else pre_bump_sha
        )
        commits = self._collect_commits_range(repo_path, since_ref, until_ref, env=env)

        # Stage only already-tracked modified files (from bump_version_task).
        # Using `git add -u` instead of `git add -A` avoids accidentally
        # staging untracked build artefacts or temporary files.
        staged_any = False
        if _run(["git", "status", "--porcelain"], cwd=repo_path, env=env).strip():
            _run(["git", "add", "-u"], cwd=repo_path, env=env)
            staged_any = True

        if self.changelog_style:
            self._stage_changelog(repo_path, version, commits, env=env)
            staged_any = True

        if staged_any:
            _run(
                ["git", "commit", "-m", f"Release {version}"],
                cwd=repo_path,
                env=env,
            )

        _run(["git", "push", "origin", branch_name], cwd=repo_path, env=env)
        _run(
            ["git", "tag", "-a", version, "-m", f"Release {version}", pre_bump_sha],
            cwd=repo_path,
            env=env,
        )
        _run(
            ["git", "push", "origin", f"refs/tags/{version}"],
            cwd=repo_path,
            env=env,
        )
        return pre_bump_sha

    def _finish_release(self, repo_path: Path, version: str, env: dict) -> str:
        """Merge the release branch into the configured tracked branch.

        Returns the merge commit SHA.
        """
        branch_name = f"release/{version}"

        _run(
            ["git", "fetch", "origin", self.branch, branch_name],
            cwd=repo_path,
            env=env,
        )
        _run(["git", "checkout", self.branch], cwd=repo_path, env=env)
        _run(
            ["git", "reset", "--hard", f"origin/{self.branch}"],
            cwd=repo_path,
            env=env,
        )
        _run(
            [
                "git",
                "merge",
                "--no-ff",
                f"origin/{branch_name}",
                "-m",
                f"Merge release/{version}",
            ],
            cwd=repo_path,
            env=env,
        )
        _run(["git", "push", "origin", self.branch], cwd=repo_path, env=env)
        return _run(["git", "rev-parse", "HEAD"], cwd=repo_path, env=env).strip()

    def _collect_commits_range(
        self, repo_path: Path, since_ref: str, until_ref: str, env: dict
    ) -> list[dict]:
        """Return enriched commit list between two refs."""
        range_spec = f"{since_ref}..{until_ref}" if since_ref else until_ref
        output = _run(
            ["git", "log", "--format=%H|%ae|%s", range_spec],
            cwd=repo_path,
            env=env,
        )

        commits: list[dict] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:  # noqa: PLR2004
                continue
            sha, author_email, message = parts
            commits.append(
                {
                    "sha": sha.strip(),
                    "author": author_email.strip(),
                    "message": message.strip(),
                    "pr_number": None,
                    "pr_title": None,
                }
            )

        if self.access_token and self.repository:
            commits = _enrich_with_github(commits, self.access_token, self.repository)

        return commits

    def _stage_changelog(
        self, repo_path: Path, version: str, commits: list[dict], env: dict
    ) -> None:
        """Write / update the changelog file and stage it for the release commit."""
        entry = _build_changelog_entry(version, commits)

        if self.changelog_style == "cumulative":
            changelog_path = repo_path / self.changelog_file
            _update_cumulative_changelog(changelog_path, entry)
            _run(["git", "add", str(changelog_path)], cwd=repo_path, env=env)
        else:
            # per_release: write releases/RELEASE_<version>.md
            releases_dir = repo_path / self.changelog_dir
            releases_dir.mkdir(parents=True, exist_ok=True)
            release_file = releases_dir / f"RELEASE_{version}.md"
            release_file.write_text(entry)
            _run(["git", "add", str(release_file)], cwd=repo_path, env=env)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


@contextmanager
def _git_ssh_env(private_key: str | None) -> Iterator[dict]:
    """Yield an env dict with GIT_SSH_COMMAND set for the given private key.

    When private_key is None, yields a copy of the current environment
    unchanged so callers can unconditionally use the env dict.
    """
    base_env = os.environ.copy()
    if not private_key:
        yield base_env
        return

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(private_key)
        key_file = f.name

    Path(key_file).chmod(0o600)
    try:
        env = {
            **base_env,
            "GIT_SSH_COMMAND": f"ssh -i {key_file} -o StrictHostKeyChecking=accept-new",
        }
        yield env
    finally:
        Path(key_file).unlink(missing_ok=True)


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    redact: str | None = None,
) -> str:
    """Run a subprocess command and return stdout as a string.

    Raises subprocess.CalledProcessError on non-zero exit, with stderr
    included in the error message.

    *redact* — if provided, this value is replaced with ``***`` in the
    CalledProcessError message so that secrets do not leak via exceptions.
    """
    result = subprocess.run(  # noqa: S603
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stdout = result.stdout
        stderr = result.stderr
        if redact:
            stdout = stdout.replace(redact, "***")
            stderr = stderr.replace(redact, "***")
        raise subprocess.CalledProcessError(
            result.returncode, cmd, stdout, stderr
        )
    return result.stdout


def _clone(
    uri: str, target: Path, *, env: dict, depth: int = _DEFAULT_CLONE_DEPTH
) -> None:
    """Shallow-clone uri into target, fetching all tags.

    *depth* defaults to ``_DEFAULT_CLONE_DEPTH``.  Pass ``depth=0`` for a
    full clone, which is more reliable when the previous release tag is older
    than the shallow history.
    """
    depth_args = [f"--depth={depth}"] if depth > 0 else []
    _run(
        [
            "git",
            "clone",
            *depth_args,
            "--no-single-branch",
            uri,
            str(target),
        ],
        env=env,
    )
    tag_depth_args = [f"--depth={depth}"] if depth > 0 else []
    _run(["git", "fetch", "--tags", *tag_depth_args], cwd=target, env=env)


def _get_release_tags(repo_path: Path, *, env: dict) -> list[str]:
    """Return all YYYY.MM.DD.N tags in the repository, sorted oldest-first."""
    output = _run(["git", "tag", "--list"], cwd=repo_path, env=env)
    tags = [t.strip() for t in output.splitlines() if VERSION_PATTERN.match(t.strip())]
    return sorted(tags, key=_parse_version_tuple)


def _commit_info_range(
    repo_path: Path, since_ref: str, until_ref: str, *, env: dict
) -> tuple[int, str]:
    """Return (commit_count, comma-separated unique author emails) for a range."""
    range_spec = f"{since_ref}..{until_ref}" if since_ref else until_ref
    output = _run(["git", "log", "--format=%ae", range_spec], cwd=repo_path, env=env)
    emails = [e.strip() for e in output.splitlines() if e.strip()]
    return len(emails), ",".join(sorted(set(emails)))


def _commit_info_all(repo_path: Path, until_ref: str, *, env: dict) -> tuple[int, str]:
    """Return (commit_count, comma-separated unique author emails) for all commits."""
    output = _run(["git", "log", "--format=%ae", until_ref], cwd=repo_path, env=env)
    emails = [e.strip() for e in output.splitlines() if e.strip()]
    return len(emails), ",".join(sorted(set(emails)))


def _configure_git_identity(
    repo_path: Path, name: str, email: str, *, env: dict
) -> None:
    _run(["git", "config", "user.name", name], cwd=repo_path, env=env)
    _run(["git", "config", "user.email", email], cwd=repo_path, env=env)


def _configure_https_auth(repo_path: Path, access_token: str, *, env: dict) -> None:
    """Configure git HTTPS authentication via http.extraheader.

    Uses ``http.extraheader`` rather than embedding the token in the remote
    URL so that the credential never appears in ``git remote -v``, process
    listings, or CalledProcessError messages.

    Only applied when the remote URL is HTTPS; SSH remotes are authenticated
    via the private key passed through ``_git_ssh_env``.
    """
    current_url = _run(
        ["git", "remote", "get-url", "origin"], cwd=repo_path, env=env
    ).strip()
    if not current_url.startswith("https://"):
        return  # SSH remote — auth is handled by _git_ssh_env private key
    _run(
        [
            "git",
            "config",
            "--local",
            "http.extraheader",
            f"Authorization: token {access_token}",
        ],
        cwd=repo_path,
        env=env,
        redact=access_token,
    )


# ---------------------------------------------------------------------------
# GitHub API enrichment
# ---------------------------------------------------------------------------


def _enrich_with_github(
    commits: list[dict], access_token: str, repository: str, *, max_commits: int = 50
) -> list[dict]:
    """Query the GitHub API to fill in pr_number and pr_title for each commit.

    Only the first *max_commits* entries are queried to avoid exhausting the
    GitHub API rate limit on repositories with many commits since the last
    release.  Commits beyond the cap retain ``pr_number=None``.
    """
    gh = Github(auth=Auth.Token(access_token))
    repo = gh.get_repo(repository)
    for commit in commits[:max_commits]:
        try:
            pulls = list(repo.get_commit(commit["sha"]).get_pulls())
            if pulls:
                pr = pulls[0]
                commit["pr_number"] = pr.number
                commit["pr_title"] = pr.title
        except Exception:  # noqa: S110
            pass
    return commits


# ---------------------------------------------------------------------------
# Version computation
# ---------------------------------------------------------------------------


def _parse_version_tuple(tag: str) -> tuple[int, int, int, int]:
    """Parse a YYYY.MM.DD.N tag into a sortable integer tuple."""
    m = VERSION_PATTERN.match(tag)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return (0, 0, 0, 0)


def _compute_next_version(existing_tags: list[str]) -> str:
    """Compute the next YYYY.M.D.N version string.

    Increments the patch counter N for today's date.  Resets to 1 when the
    date component changes.  Month and day are not zero-padded to comply with
    PEP 440 (required by uv and other modern Python tooling).
    """
    today = datetime.now(tz=UTC).date()
    prefix = f"{today.year}.{today.month}.{today.day}"
    max_n = 0
    for tag in existing_tags:
        m = VERSION_PATTERN.match(tag)
        if m and tag.startswith(prefix):
            max_n = max(max_n, int(m.group(4)))
    return f"{prefix}.{max_n + 1}"


# ---------------------------------------------------------------------------
# Changelog / checklist generation
# ---------------------------------------------------------------------------


def _build_checklist(version: str, commits: list[dict]) -> str:
    """Build a GitHub Issue body with a markdown task checklist."""
    lines = [
        f"## Release {version}",
        "",
        "### Changes",
        "",
    ]
    for commit in commits:
        sha_short = commit["sha"][:7]
        author = commit["author"]
        if commit.get("pr_number"):
            title = commit.get("pr_title") or commit["message"]
            lines.append(f"- [ ] **{title}** (#{commit['pr_number']}) by {author}")
        else:
            lines.append(f"- [ ] `{sha_short}` {commit['message']} by {author}")

    lines += [
        "",
        "---",
        "*This release was automatically generated by the Concourse release pipeline.*",
        "*Closing this issue will trigger the production deployment.*",
    ]
    return "\n".join(lines)


def _build_changelog_entry(version: str, commits: list[dict]) -> str:
    """Build a single Keep a Changelog entry for the given version."""
    today_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    lines = [
        f"## [{version}] - {today_str}",
        "",
        "### Changes",
        "",
    ]
    for commit in commits:
        sha_short = commit["sha"][:7]
        author = commit["author"]
        if commit.get("pr_number"):
            title = commit.get("pr_title") or commit["message"]
            lines.append(f"- **{title}** (#{commit['pr_number']}) by {author}")
        else:
            lines.append(f"- `{sha_short}` {commit['message']} by {author}")

    lines.append("")
    return "\n".join(lines)


def _update_cumulative_changelog(changelog_path: Path, entry: str) -> None:
    """Prepend entry to the cumulative changelog file.

    Creates the file with a standard header if it does not yet exist.
    The entry is inserted after the header block and before any existing entries.
    """
    if changelog_path.exists():
        existing = changelog_path.read_text()
        # Insert the new entry after the header (two blank lines after the last
        # header paragraph) and before the first existing ## version entry.
        # Check for a version header at the very start of the file too, since
        # find("\n## ") would return -1 for headerless changelogs that begin
        # directly with a version entry.
        if existing.startswith("## "):
            new_content = entry + "\n\n" + existing
        else:
            header_end = existing.find("\n## ")
            if header_end == -1:
                # No existing version entries — append after header
                new_content = existing.rstrip("\n") + "\n\n" + entry
            else:
                new_content = (
                    existing[:header_end] + "\n\n" + entry + existing[header_end:]
                )
    else:
        new_content = CHANGELOG_HEADER + "\n" + entry

    changelog_path.write_text(new_content)
