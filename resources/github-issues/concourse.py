"""Concourse resource for managing GitHub Issues as pipeline gate signals."""

from pathlib import Path
import re
import textwrap
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import islice
from typing import Literal
from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import Version, SortableVersionMixin
from github import Github, Auth, Consts, UnknownObjectException
from github.GithubObject import NotSet
from github.Issue import Issue

ISO_8601_FORMAT = "%Y-%m-%dT%H:%M:%S"

# GitHub hard-rejects an issue or comment body over this length (422
# "body is too long"). None of the templates or summary files this resource
# is fed enforce that limit themselves -- a large enough Pulumi diff or
# body_files composition can produce one that does -- so this is the last
# point that can actually guarantee the API call succeeds.
GITHUB_BODY_MAX_CHARS = 65536
# How many of the newest issues an exact-title lookup reads to cover the lag of
# GitHub's search index. One page at ``per_page=100``.
_RECENT_ISSUE_SCAN = 100
_TRUNCATION_NOTICE = (
    "\n\n> :warning: **Body truncated** -- this content exceeded GitHub's "
    f"{GITHUB_BODY_MAX_CHARS}-character limit for issues and comments. See "
    "the build log for the full content."
)


def _fit_to_github_limit(body: str, max_chars: int = GITHUB_BODY_MAX_CHARS) -> str:
    """Truncate *body* to fit GitHub's issue/comment size limit, visibly.

    Cutting silently would leave a body that reads as complete while hiding
    whatever came after the cut -- exactly the failure the "Showing N of M"
    messages elsewhere in this pipeline exist to prevent. The notice always
    fits: it is reserved out of the budget before the cut, not appended after.
    """
    if len(body) <= max_chars:
        return body
    keep = max_chars - len(_TRUNCATION_NOTICE)
    return body[:keep] + _TRUNCATION_NOTICE


# Generic checklist-line matcher: "- [ ] <anything>" / "- [x] <anything>".
# Deliberately loose (no assumption about what follows the checkbox) so this
# works for any checklist-style issue body, not just the release resource's
# specific "by <author>" format.
_CHECKLIST_LINE_RE = re.compile(r"^- \[(?P<mark> |x)\](?P<rest>.*)$", re.IGNORECASE)


def _merge_checklist_preserving_checked(old_body: str, new_body: str) -> str:
    """Re-check any new_body checklist line already checked in old_body.

    A put to an already-existing issue regenerates the body from scratch
    (e.g. a pipeline retrigger with no real change to review), and that
    fresh body's checklist starts entirely unchecked. Without this, editing
    the issue in place would silently wipe out whatever a human already
    reviewed and checked off. Matching is by each line's content *after*
    the checkbox mark, so re-ordering or the checkbox state itself doesn't
    break the match -- only genuinely different line content does.
    """
    old_checked_content: set[str] = set()
    for line in old_body.splitlines():
        match = _CHECKLIST_LINE_RE.match(line)
        if match and match.group("mark").lower() == "x":
            old_checked_content.add(match.group("rest"))

    new_lines = []
    for line in new_body.splitlines():
        match = _CHECKLIST_LINE_RE.match(line)
        if match and match.group("rest") in old_checked_content:
            line = f"- [x]{match.group('rest')}"
        new_lines.append(line)
    merged = "\n".join(new_lines)
    if new_body.endswith("\n"):
        merged += "\n"
    return merged


def _resolve_in_workspace(sources_dir: Path, relative_path: str) -> Path:
    """Resolve *relative_path* against sources_dir, refusing to escape it."""
    candidate = Path(relative_path)
    if candidate.is_absolute():
        msg = "path must be relative"
        raise ValueError(msg)
    resolved = (sources_dir / candidate).resolve()
    if not resolved.is_relative_to(sources_dir.resolve()):
        msg = "path must be within the workspace sources directory"
        raise ValueError(msg)
    return resolved


def build_metadata_dict(build_metadata: BuildMetadata) -> dict[str, str]:
    """Return a flat dict of Concourse build metadata for template formatting."""
    return {
        "BUILD_URL": build_metadata.build_url(),
        "BUILD_ID": build_metadata.BUILD_ID,
        "BUILD_TEAM_NAME": build_metadata.BUILD_TEAM_NAME,
        "BUILD_NAME": build_metadata.BUILD_NAME,
        "BUILD_JOB_NAME": build_metadata.BUILD_JOB_NAME,
        "BUILD_PIPELINE_NAME": build_metadata.BUILD_PIPELINE_NAME,
        "BUILD_PIPELINE_INSTANCE_VARS": build_metadata.BUILD_PIPELINE_INSTANCE_VARS,
        "ATC_EXTERNAL_URL": build_metadata.ATC_EXTERNAL_URL,
    }


class ConcourseGithubIssuesVersion(Version, SortableVersionMixin):
    """Concourse version representing a single GitHub Issue."""

    def __init__(  # noqa: PLR0913
        self,
        issue_created_at: str,
        issue_closed_at: str | None,
        issue_number: int,
        issue_state: Literal["open", "closed"],
        issue_title: str,
        issue_url: str,
    ):
        """Initialize a version from GitHub Issue fields."""
        self.issue_created_at = issue_created_at
        self.issue_number = issue_number
        self.issue_state = issue_state
        self.issue_title = issue_title
        self.issue_url = issue_url
        self.issue_closed_at = issue_closed_at

    def __lt__(self, other: "ConcourseGithubIssuesVersion"):
        """Return True if this version is older than *other*."""
        if self.issue_state == other.issue_state == "closed":
            return datetime.strptime(  # noqa: DTZ007
                self.issue_closed_at,  # type: ignore[arg-type]
                ISO_8601_FORMAT,
            ) < datetime.strptime(  # noqa: DTZ007
                other.issue_closed_at,  # type: ignore[arg-type]
                ISO_8601_FORMAT,
            )
        else:
            return int(self.issue_number) < int(other.issue_number)


#: What a put skipped by ``skip_if_file`` returns. It names no issue (number
#: 0, empty title), is constant so Concourse records it once and never sees
#: it as new again, and is ``open`` so the implicit get never tombstones.
SKIPPED_VERSION = ConcourseGithubIssuesVersion(
    issue_created_at="1970-01-01T00:00:00",
    issue_closed_at=None,
    issue_number=0,
    issue_state="open",
    issue_title="",
    issue_url="",
)


class ConcourseGithubIssuesResource(ConcourseResource):
    """Concourse resource that uses GitHub Issues as pipeline gate signals."""

    def __init__(  # noqa: PLR0913
        self,
        /,
        repository: str,
        gh_host: str = Consts.DEFAULT_BASE_URL,
        access_token: str | None = None,
        app_id: int | None = None,
        app_installation_id: int | None = None,
        assignees: list[str] | None = None,
        issue_prefix: str | None = None,
        labels: list[str] | None = None,
        private_ssh_key: str | None = None,
        limit_old_versions: int | None = None,
        auth_method: Literal["token", "app"] = "token",
        issue_state: Literal["open", "closed"] = "closed",
        issue_title_template: str = (
            "[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed"
        ),
        issue_body_template: str = textwrap.dedent(
            """\
        The task {BUILD_JOB_NAME} in pipeline {BUILD_PIPELINE_NAME} has completed build number {BUILD_NAME}.
        Please refer to [the build log]({BUILD_URL}) for details of what changes this includes.
        Closing this issue will trigger the next job in the pipeline {BUILD_PIPELINE_NAME}.
        """  # noqa: E501
        ),
        skip_if_labeled: list[str] | None = None,
        timeout: int = 30,
        update_in_place: bool = False,
        consume_with_label: str | None = None,
    ):
        """Initialize with GitHub API credentials and issue configuration.

        *consume_with_label* names the label ``in`` adds to an issue it
        consumes, in place of the ``[CONSUMED #n]`` title rewrite. The title,
        which is part of the version and may carry a release version a put's
        ``title_template`` set, is then left alone. ``check`` skips an issue
        carrying the label, as it skips one carrying a ``skip_if_labeled``
        label: adding it bumps the issue's ``updated_at``, so the issue comes
        back inside the ``since`` window and its title still matches
        ``issue_prefix``. ``in`` does not refuse it, so a retried build can
        fetch an issue it already consumed.
        """
        super().__init__(ConcourseGithubIssuesVersion)
        if auth_method == "token":
            auth = self.auth_token(access_token)
        else:
            auth = self.auth_app(app_id, app_installation_id, private_ssh_key)
        self.gh = Github(base_url=gh_host, auth=auth, per_page=100, timeout=timeout)
        self.repo = self.gh.get_repo(repository)
        self.issue_state = issue_state
        self.issue_prefix = issue_prefix
        self.found_pipeline_issues: list[Issue] = []
        self.issue_labels = labels
        self.assignees = assignees
        self.issue_title_template = issue_title_template
        self.issue_body_template = issue_body_template
        self.limit_old_versions = limit_old_versions
        self.skip_if_labeled: list[str] = skip_if_labeled or []
        self.update_in_place = update_in_place
        self.consume_with_label = consume_with_label
        self._check_skip_labels = set(self.skip_if_labeled) | (
            {consume_with_label} if consume_with_label else set()
        )

    def auth_token(self, access_token):
        """Return a token-based GitHub Auth object."""
        return Auth.Token(access_token)

    def auth_app(self, app_id, app_installation_id, private_ssh_key):
        """Return an app installation-based GitHub Auth object."""
        return Auth.AppAuth(app_id, private_ssh_key).get_installation_auth(
            app_installation_id
        )

    def _to_version(self, gh_issue: Issue) -> ConcourseGithubIssuesVersion:
        if gh_issue.state == "closed":
            issue_closed_time = gh_issue.closed_at.strftime(ISO_8601_FORMAT)
        else:
            issue_closed_time = None
        return ConcourseGithubIssuesVersion(
            issue_number=gh_issue.number,
            issue_title=gh_issue.title,
            issue_state=gh_issue.state,
            issue_created_at=gh_issue.created_at.strftime(ISO_8601_FORMAT),
            issue_url=gh_issue.url,
            issue_closed_at=issue_closed_time,
        )

    def _from_version(self, version: ConcourseGithubIssuesVersion) -> Issue:
        return self.repo.get_issue(int(version.issue_number))

    def get_all_issues(
        self,
        issue_state: Literal["open", "closed"] | None = None,
        since: datetime | None = None,
    ) -> list[Issue]:
        """Return all issues from the repository matching the configured filters."""
        if not issue_state:
            issue_state = self.issue_state
        # Pass NotSet if since is None, as PyGithub expects this sentinel value
        since_param = since if since is not None else NotSet
        return self.repo.get_issues(
            state=issue_state, labels=self.issue_labels or [], since=since_param
        )

    def get_exact_title_match(
        self, title: str, state: Literal["open", "closed"]
    ) -> list[Issue]:
        """Return issues whose title exactly matches *title* and are in *state*."""
        all_pipeline_issues = self.get_all_issues(issue_state=state)

        unsorted = [
            issue
            for issue in all_pipeline_issues
            if (issue.title == title or "") and (issue.state == state)
        ]

        sorted_issues = sorted(unsorted, key=lambda issue: issue.number, reverse=True)
        return sorted_issues

    def get_matching_issues(self, since: datetime | None = None) -> list[Issue]:
        """Return issues matching the configured prefix and skip-label filters."""
        all_pipeline_issues = self.get_all_issues(since=since)

        matching_issues = []
        for issue in all_pipeline_issues:
            if not issue.title.startswith(self.issue_prefix or ""):
                continue
            if self._check_skip_labels:
                issue_label_names = {lbl.name for lbl in issue.labels}
                if issue_label_names.intersection(self._check_skip_labels):
                    continue
            matching_issues.append(issue)
            if (
                self.limit_old_versions
                and len(matching_issues) == self.limit_old_versions
            ):
                break
        # Sort by number ascending to process oldest first if limited
        matching_issues.sort(key=lambda issue: issue.number)
        return matching_issues

    def _get_latest_matching_issue(self) -> "Issue | None":
        """Return the single most-recent matching issue, or None.

        Iterates the GitHub API PaginatedList (newest first by default) and
        stops as soon as the first qualifying issue is found, so at most one
        page of results is fetched rather than the entire history.
        """
        for issue in self.get_all_issues():
            if not issue.title.startswith(self.issue_prefix or ""):
                continue
            if self._check_skip_labels:
                issue_label_names = {lbl.name for lbl in issue.labels}
                if issue_label_names.intersection(self._check_skip_labels):
                    continue
            return issue
        return None

    def fetch_new_versions(
        self, previous_version: ConcourseGithubIssuesVersion | None = None
    ) -> set[ConcourseGithubIssuesVersion]:
        """Fetch new versions since the previous one.

        On the first check (no previous version), Concourse only needs the
        current latest version to seed its state.  Scanning the full issue
        history is expensive, so we stop at the first (most-recent) match
        instead of exhausting the paginated API.

        :data:`SKIPPED_VERSION` (issue 0) is treated the same way. A skipped put
        saves it as this resource's latest version, so it can come back here,
        and its epoch timestamp would otherwise mean "every issue since 1970".
        """
        if previous_version is None or int(previous_version.issue_number) == 0:
            latest = self._get_latest_matching_issue()
            if latest:
                return {self._to_version(latest)}
            return set()

        since_datetime: datetime | None = None
        timestamp_str: str | None = None
        if self.issue_state == "closed":
            timestamp_str = previous_version.issue_closed_at
        elif self.issue_state == "open":
            timestamp_str = previous_version.issue_created_at

        if timestamp_str:
            try:
                # Add a small buffer (1 second) to avoid potential clock skew
                # issues or fetching the exact same event again.
                since_datetime = datetime.strptime(  # noqa: DTZ007
                    timestamp_str, ISO_8601_FORMAT
                ) + timedelta(seconds=1)
            except ValueError:
                print(f"Warning: Could not parse timestamp {timestamp_str}")  # noqa: T201

        matching_issues = self.get_matching_issues(since=since_datetime)
        versions = {self._to_version(issue) for issue in matching_issues}
        # Filter out the previous_version itself if it happens to be included
        if previous_version in versions:
            versions.remove(previous_version)
        return versions

    def tombstone_version(
        self, version: ConcourseGithubIssuesVersion, build_metadata: BuildMetadata
    ):
        """Mark the issue consumed so it is not re-triggered.

        With ``consume_with_label`` the issue gets that label and keeps its
        title. Adding a label is idempotent, so a retried build that fetches
        the same issue again changes nothing. Without it, the title is
        rewritten with a ``[CONSUMED #n]`` prefix, which stops ``check``
        matching ``issue_prefix``.
        """
        # Check state from the version data first
        if version.issue_state != "closed":
            return
        # Fetch the issue object only when we know we need to edit it
        issue = self.repo.get_issue(int(version.issue_number))  # API Call 1
        if self.consume_with_label:
            issue.add_to_labels(self.consume_with_label)
            return
        current_title = self.get_title_from_build(build_metadata)
        job_number = build_metadata.BUILD_NAME
        issue.edit(title=f"[CONSUMED #{job_number}]" + current_title)

    def _refuse_if_skip_labeled(self, version: ConcourseGithubIssuesVersion) -> None:
        """Raise if the version's issue carries a ``skip_if_labeled`` label now."""
        if not self.skip_if_labeled or int(version.issue_number) == 0:
            return
        issue = self.repo.get_issue(int(version.issue_number))
        skipped = sorted(
            {label.name for label in issue.labels}.intersection(self.skip_if_labeled)
        )
        if skipped:
            msg = (
                f"Refusing issue #{version.issue_number} ({version.issue_title!r}): "
                f"it is labelled {skipped}, which this resource skips. It was "
                "discovered before the label was applied, and must not trigger "
                "anything."
            )
            raise RuntimeError(msg)

    def download_version(
        self,
        version: ConcourseGithubIssuesVersion,
        destination_dir: str,
        build_metadata: BuildMetadata,
    ) -> tuple[ConcourseGithubIssuesVersion, dict[str, str]]:
        """Write issue metadata to disk and tombstone the issue.

        Refuses, before writing or renaming anything, an issue that now carries
        a ``skip_if_labeled`` label. ``check`` only filters on labels when it
        discovers a version, so one it already handed to Concourse can be
        abandoned afterwards (see ``close_with_labels``) and would otherwise
        still be fetched here and trigger the job that consumes it.
        :data:`SKIPPED_VERSION` names no issue and is never refused.
        """
        self._refuse_if_skip_labeled(version)
        with Path(destination_dir).joinpath("gh_issue.json").open("w") as issue_file:
            issue_file.write(json.dumps(version.to_flat_dict() or {}))
        # We've triggered a deploy and consumed this issue. Tombstone it (a label,
        # or a title prefix) so we'll ignore it in future and avoid duplicate
        # triggering.
        self.tombstone_version(version, build_metadata)
        return version, {}

    def get_issue_body_from_build(
        self,
        build_metadata: BuildMetadata,
        body_file: str | None = None,
        sources_dir: Path | None = None,
        body_files: list[str] | None = None,
    ) -> str:
        """Return the issue body from workspace file(s) or the rendered template.

        *body_files* composes one body from several artifacts, in the order
        given. A promotion gate wants "what this deploy did" and "what promoting
        it will do to the next environment" in one issue, and those are produced
        by two different put steps in two different artifacts -- a put step emits
        no artifact of its own, so each arrives via its own implicit get.

        A *body_files* entry that is missing is reported in the body rather than
        skipped. The upstream step that writes such a fragment is allowed to fail
        without failing the deploy, so absence is an expected state -- but
        silently dropping a section would leave a body that reads as complete
        while omitting the half a reviewer may be relying on.

        A missing *body_file* still raises, as it always has. That one is the
        entire body rather than an optional fragment, so tolerating it would turn
        a typo'd path into a published gate issue containing nothing but a
        warning.
        """
        if body_files:
            return "\n".join(
                self._read_body_file(f, sources_dir, required=False) for f in body_files
            )
        if body_file is not None:
            return self._read_body_file(body_file, sources_dir)
        return self.issue_body_template.format(**build_metadata_dict(build_metadata))

    def _read_body_file(
        self, body_file: str, sources_dir: Path | None, *, required: bool = True
    ) -> str:
        """Read one body fragment, refusing to escape the workspace.

        *required* False substitutes a visible warning for a missing file instead
        of raising -- correct for an optional composed fragment, wrong for a sole
        body, where absence means the caller got the path wrong.
        """
        if sources_dir is None:
            msg = "sources_dir is required when body_file is provided"
            raise ValueError(msg)
        resolved = _resolve_in_workspace(sources_dir, body_file)
        if required or resolved.exists():
            return resolved.read_text()
        return (
            f"\n> :warning: Expected content from `{body_file}` was not "
            "produced by this build.\n"
        )

    def get_title_from_build(
        self, build_metadata: BuildMetadata, title_template: str | None = None
    ) -> str:
        """Return the issue title rendered from a template.

        *title_template* overrides the source-level ``issue_title_template``
        for this call. This is how a caller embeds a value the resource has
        no other way to know, like a release version: Concourse resolves any
        ``((.:var))`` reference in a put step's *params* (e.g. a version
        loaded via ``load_var`` earlier in the same job) before this script
        ever runs, so the override arrives here as a plain, fully-resolved
        string -- ``.format()`` only touches ``{BUILD_*}`` placeholders that
        remain, so this is safe to call even when the override has none.
        """
        template = title_template or self.issue_title_template
        return template.format(**build_metadata_dict(build_metadata))

    def _close_with_labels(
        self,
        build_metadata: BuildMetadata,
        labels: list[str],
        title_template: str | None,
    ) -> tuple[ConcourseGithubIssuesVersion, dict[str, str]]:
        """Label, and close if open, every issue titled exactly as rendered."""
        title = self.get_title_from_build(build_metadata, title_template=title_template)
        # Any state: a reviewer may have closed the issue after the release was
        # cut but before this build ran. A gate that has not polled yet would
        # still fire on it, and labelling it is what stops that. An issue a
        # gate already consumed can no longer fire it, but is labelled too, so
        # `carry_checks_unless_labeled` does not hand its ticks to a re-cut of
        # the same version. One renamed `[CONSUMED ...]` never matches the
        # exact title; one labelled `consume_with_label` keeps its title, and
        # only the newest of those is labelled, since a title without a version
        # would otherwise match every release ever consumed under it. An
        # abandon can also land moments after the QA put that opened the gate
        # issue, before search has indexed it; finding nothing would leave that
        # issue open and able to ship the release, which is why the lookup
        # reads the issues API too.
        issues = self._issues_titled(title, open_only=False)
        if consumed := [issue for issue in issues if self._is_consumed(issue)]:
            newest = max(consumed, key=lambda issue: issue.number)
            issues = [i for i in issues if i not in consumed or i is newest]
        for issue in issues:
            if issue.state == "open":
                print(f"about to label {issue=} with {labels=} and close it")  # noqa: T201
                issue.create_comment(
                    f"Closed by [build {build_metadata.BUILD_NAME}]"
                    f"({build_metadata.build_url()}) and labelled "
                    f"{', '.join(f'`{label}`' for label in labels)}."
                )
                # One PATCH, so no reader sees either half on its own: closed
                # but unlabelled would ship the release.
                current = [label.name for label in issue.labels]
                issue.edit(
                    state="closed",
                    labels=[
                        *current,
                        *(name for name in labels if name not in current),
                    ],
                )
            else:
                print(f"about to label already-closed {issue=} with {labels=}")  # noqa: T201
                issue.add_to_labels(*labels)
            self._ensure_labels(issue.number, labels)
        if not issues:
            print(f"no issue titled {title!r} -- nothing to close")  # noqa: T201
        return SKIPPED_VERSION, {
            "retired_issues": ", ".join(f"#{issue.number}" for issue in issues)
            or "none"
        }

    def _branch_exists(self, branch: str) -> bool:
        """Return whether *branch* exists in the repository right now.

        Only a 404 means absent; any other error propagates. ``get_branch``
        requests the branch by exact name and raises on a 404 immediately.
        ``get_git_ref`` looks like the narrower call but returns a lazy object
        that makes no request until an attribute is read, so it never raises
        here and would report every branch as present.
        """
        try:
            self.repo.get_branch(branch)
        except UnknownObjectException:
            return False
        return True

    def _tag_exists(self, tag: str) -> bool:
        """Return whether the tag *tag* exists in the repository right now.

        Reads the matching-refs API, which returns ``[]`` rather than a 404
        when nothing matches and so cannot mistake an outage for an absence:
        any error propagates. It matches by prefix (``2026.10.8`` matches
        ``2026.10.8.1``), so only an exact ``refs/tags/<tag>`` counts.
        ``get_git_ref`` is lazy, for the reason given in :meth:`_branch_exists`.
        """
        wanted = f"refs/tags/{tag}"
        return any(
            ref.ref == wanted for ref in self.repo.get_git_matching_refs(f"tags/{tag}")
        )

    def _issues_titled(self, title: str, *, open_only: bool) -> list[Issue]:
        """Return every issue titled exactly *title*, read fresh, oldest first.

        Search matches on tokens, not on the whole title, so only exact matches
        count: acting on a neighbour's issue is worse than acting on none. Its
        index also lags issue state in both directions. An issue created
        seconds ago can be missing from it, so the newest issues are read from
        the issues API as well. An issue closed seconds ago can still match
        ``state:open``, so every match is re-read from the issues API, which is
        consistent, and the state it reports is the one returned. With
        *open_only*, the search asks for open issues and any match the issues
        API calls closed is dropped.
        """
        safe_title = title.replace('"', '\\"')
        state = "state:open " if open_only else ""
        query = f'repo:{self.repo.full_name} {state}"{safe_title}" in:title is:issue'
        numbers = {
            issue.number
            for issue in self.gh.search_issues(query)
            if issue.title == title
        }
        numbers.update(
            issue.number
            for issue in self._recent_issues()
            if issue.title == title and not issue.pull_request
        )
        issues = [self.repo.get_issue(number) for number in sorted(numbers)]
        issues = [issue for issue in issues if issue.title == title]
        if open_only:
            issues = [issue for issue in issues if issue.state == "open"]
        return issues

    def _recent_issues(self) -> Iterable[Issue]:
        """Return the newest issues in either state, newest first.

        One page (``per_page`` is 100). A gate issue the search index has not
        caught up with was created minutes ago, so it is always in this page;
        older ones are in the index. Not filtered by ``issue_labels``: this
        resource's own filters describe the issues it polls for, and an issue
        carrying a label a reviewer removed is exactly the one to retire.
        """
        return islice(
            self.repo.get_issues(state="all", sort="created", direction="desc"),
            _RECENT_ISSUE_SCAN,
        )

    def _is_consumed(self, issue: Issue) -> bool:
        """Return whether a get labelled *issue* with ``consume_with_label``."""
        return bool(self.consume_with_label) and self.consume_with_label in {
            label.name for label in issue.labels
        }

    def _ensure_labels(self, issue_number: int, labels: list[str]) -> None:
        """Re-add any of *labels* another writer removed from an issue.

        This resource never removes a label, but a human or another tool can
        still replace them. Re-reading after the close is the only point at
        which the label is known to have survived. A gate that polled in the
        gap between that write and the re-add is the residual window.
        """
        issue = self.repo.get_issue(issue_number)
        missing = [
            label
            for label in labels
            if label not in {existing.name for existing in issue.labels}
        ]
        if missing:
            print(f"{missing=} were removed from #{issue_number} -- restoring")  # noqa: T201
            issue.add_to_labels(*missing)

    def _reconcile_labels(self, issue_number: int, desired: list[str]) -> None:
        """Add any of *desired* an open issue is missing. Never remove one.

        Labels record that something happened -- a bot marked the issue
        ready, a gate consumed it -- and other writers add them. A label this
        put did not name is not stale, so nothing is removed. Every write is a
        plain add by name, which is idempotent and needs no ordering with any
        other writer.

        Reads the issue from the issues API, not search, whose index can still
        call a retired issue open. A closed issue is left alone: it has been
        approved or retired (``close_with_labels``), and either way its labels
        are no longer this put's to correct.
        """
        issue = self.repo.get_issue(issue_number)
        if issue.state != "open":
            print(f"#{issue_number} is {issue.state} -- leaving its labels alone")  # noqa: T201
            return
        current = {label.name for label in issue.labels}
        if missing := [name for name in desired if name not in current]:
            print(f"about to add {missing=} to #{issue_number}")  # noqa: T201
            issue.add_to_labels(*missing)

    def _carry_checks(
        self, title: str, body: str, unless_labeled: list[str], labels: list[str]
    ) -> str:
        """Re-tick *body*'s lines ticked on the newest closed issue for *title*.

        Only an issue carrying one of this put's *labels* can give its ticks.
        Anyone who can open an issue in a public repository can open and close
        one with a predictable title and a ticked body, but only a
        collaborator can label it, so an unlabelled issue is not a review.
        With no *labels*, nothing is carried.

        Of those, only the newest closed issue is read. Falling back to an
        older one when the newest is excluded would revive ticks from before a
        release was abandoned. Ticking only ever replaces ``[ ]`` with
        ``[x]``, so the result is no longer than *body*.
        """
        if isinstance(unless_labeled, str):
            # A bare string would be read as a set of single characters,
            # excluding nothing.
            msg = "carry_checks_unless_labeled must be a list of label names"
            raise TypeError(msg)
        closed = [
            issue
            for issue in self._issues_titled(title, open_only=False)
            if issue.state == "closed"
            and {label.name for label in issue.labels}.intersection(labels)
        ]
        if not closed:
            return body
        donor = max(closed, key=lambda issue: issue.number)
        excluded = sorted(
            {label.name for label in donor.labels}.intersection(unless_labeled)
        )
        if excluded:
            print(f"not carrying checks from #{donor.number}: labelled {excluded}")  # noqa: T201
            return body
        print(f"carrying checked checklist lines over from #{donor.number}")  # noqa: T201
        return _merge_checklist_preserving_checked(donor.body or "", body)

    def publish_new_version(  # noqa: PLR0913
        self,
        sources_dir,
        build_metadata: BuildMetadata,
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
        body_file: str | None = None,
        title_template: str | None = None,
        body_files: list[str] | None = None,
        close_if_file: str | None = None,
        skip_if_file: str | None = None,
        close_with_labels: list[str] | None = None,
        skip_if_branch_missing: str | None = None,
        skip_if_tag_missing: str | None = None,
        carry_checks_unless_labeled: list[str] | None = None,
    ) -> tuple[ConcourseGithubIssuesVersion, dict[str, str]]:
        """Create or comment on a GitHub Issue and return its version.

        *carry_checks_unless_labeled* keeps review progress when a put has to
        open a new issue because the last one for the same title was closed.
        A QA re-run after an issue was approved does that, and its checklist
        would otherwise start all unchecked. The newest closed issue whose
        title is the rendered title, and which carries one of this put's
        *labels*, gives its ticks to the new body, matched
        line by line as ``update_in_place`` does. An issue a gate consumed
        with ``consume_with_label`` keeps its title and so counts; one renamed
        ``[CONSUMED #n]`` does not. Nothing is carried when that issue has any
        of the listed labels (e.g. ``ops:abandoned``: a retired release's
        ticks are not a review of its re-cut), or when there is no closed
        issue. An empty list excludes nothing. Only a new issue is affected.

        *skip_if_tag_missing* is *skip_if_branch_missing* for a tag, and is the
        one to use when only an *abandoned* release should post nothing: the
        release resource deletes the version tag when it abandons a release,
        but keeps it when it finishes one, where it deletes only the branch. A
        finished release can be waiting for approval again (Production rolled
        back to an older release), and a branch check would withhold its
        issue. A re-cut release recreates the tag, so it still gets its issue.
        It does not apply to *close_with_labels*, any lookup error fails the
        put, and *skip_if_file* wins, all as for *skip_if_branch_missing*.
        When both are set, either one missing skips the put.

        *skip_if_branch_missing* names a branch in the repository; when it is
        not there, nothing is posted. It is for a put that can run again after
        the thing it announces is over -- a release issue opened by a QA deploy
        that an unrelated infrastructure merge re-runs once its release has been
        abandoned or finished, and whose branch is therefore gone. Without it
        that run searches for an *open* issue, finds none (the old one was
        closed), and opens a new one for a release that can no longer ship; the
        gate would then deploy it when someone closed it. Whether the branch
        exists is read from the branches API, not inferred from the issues, so a
        release that is later re-cut under the same version -- abandoning
        deletes the tag, and the next release reuses the number -- is not
        mistaken for the abandoned one. It does not apply to *close_with_labels*,
        which has to run after the branch is deleted. A lookup that fails for any
        reason other than the branch being absent fails the put rather than
        guessing, since posting a stale issue and withholding a live one are
        both worse than a red build that a retrigger clears. Returns
        :data:`SKIPPED_VERSION` like *skip_if_file*, which wins when both are set.

        *close_with_labels* retires an issue instead of posting one: every issue
        whose title is exactly the rendered title gets these labels. An open one
        also gets a comment linking this build and is closed; one already
        closed is only labelled. Of the issues a gate consumed with
        ``consume_with_label``, only the newest is labelled; one renamed
        ``[CONSUMED #n]`` never matches. It never creates an issue, and finding
        none is not an error. An open issue is labelled and closed in one edit:
        a gate polling for *closed* issues skips one by ``skip_if_labeled``, so
        it must never see the issue closed but unlabelled. The labels are
        checked again afterwards, in case another writer replaced them. Like
        *skip_if_file* it returns :data:`SKIPPED_VERSION`, so the implicit get
        has nothing to tombstone. *skip_if_file* wins when both are set.

        *skip_if_file* names a workspace-relative file whose presence means
        there is nothing to post: no issue is searched for, created, edited,
        commented on or closed. It is the opposite of
        *close_if_file*, for a gate where a pre-approved issue would be wrong
        -- e.g. a release issue, where closing ships a release, so "nothing to
        approve" must mean no issue at all. A put still has to return a version,
        so a skipped put returns :data:`SKIPPED_VERSION`, which is constant:
        Concourse records it once and never sees it as new again, and it is
        ``open``, so the implicit get never tombstones anything. Takes
        precedence over *close_if_file*. A missing file is the same as not
        passing this param at all.

        *close_if_file* names a workspace-relative file whose presence means
        there is nothing to review -- e.g. an empty Pulumi preview diff. The
        issue is still created/updated exactly as normal, then immediately
        closed via the API so no human has to act on it. Closing rather than
        skipping creation matters: a downstream ``-gate-trigger`` resource
        polls for a *closed* issue to advance the pipeline, so an empty diff
        still needs one -- just pre-approved, so the gate can't hang forever
        waiting for a human to close an issue that was never opened. A
        missing file is the same as not passing this param at all.
        """
        if skip_if_file and _resolve_in_workspace(sources_dir, skip_if_file).exists():
            print(f"{skip_if_file!r} says there is nothing to post -- skipping")  # noqa: T201
            return SKIPPED_VERSION, {"skipped_by": skip_if_file}

        if close_with_labels is not None:
            if not close_with_labels:
                # Falling through would take the create-an-issue path below,
                # the one thing a retirement must never do.
                msg = "close_with_labels must name at least one label"
                raise ValueError(msg)
            return self._close_with_labels(
                build_metadata, close_with_labels, title_template
            )

        if skip_if_branch_missing and not self._branch_exists(skip_if_branch_missing):
            print(  # noqa: T201
                f"branch {skip_if_branch_missing!r} is not in {self.repo.full_name} "
                "-- its release is over, so there is nothing to post"
            )
            return SKIPPED_VERSION, {
                "skipped_by": f"branch {skip_if_branch_missing} is missing"
            }

        if skip_if_tag_missing and not self._tag_exists(skip_if_tag_missing):
            print(  # noqa: T201
                f"tag {skip_if_tag_missing!r} is not in {self.repo.full_name} "
                "-- its release was abandoned, so there is nothing to post"
            )
            return SKIPPED_VERSION, {
                "skipped_by": f"tag {skip_if_tag_missing} is missing"
            }

        # Assume that: title is enough uniqueness to discern whether the issue
        # already exists

        issue_body = _fit_to_github_limit(
            self.get_issue_body_from_build(
                build_metadata,
                body_file=body_file,
                sources_dir=sources_dir,
                body_files=body_files,
            )
        )

        candidate_issue_title = self.get_title_from_build(
            build_metadata, title_template=title_template
        )
        # Only an issue the issues API calls open counts. Search can still
        # return one closed seconds ago, and editing that would rewrite an
        # approval that was already given -- for a release gate, a QA re-run
        # replacing the body of a closed Production gate. A closed issue means
        # this put needs a new one. The newest open match is the one used.
        already_exists = self._issues_titled(candidate_issue_title, open_only=True)

        if len(already_exists) > 1:
            print("Warning: There are multiple matches for the desired issue title!")  # noqa: T201

        if not already_exists:
            if carry_checks_unless_labeled is not None:
                issue_body = self._carry_checks(
                    candidate_issue_title,
                    issue_body,
                    carry_checks_unless_labeled,
                    labels or [],
                )
            # Pass label names (strings) directly, avoid fetching Label objects
            working_issue = self.repo.create_issue(
                title=candidate_issue_title,
                assignees=assignees or [],
                labels=labels or [],  # Pass list of strings
                body=issue_body,
            )
            print(f"created issue: {working_issue=}")  # noqa: T201
        elif self.update_in_place:
            working_issue = already_exists[-1]
            # Edit in place rather than commenting -- a retrigger with no
            # real change to review (e.g. an unrelated upstream pipeline
            # commit) would otherwise post a second, freshly-unchecked
            # checklist as a new comment, which reads as the issue
            # reopening even though nothing changed. Re-checking any line
            # already checked in the current body preserves review
            # progress across the edit. Opt-in only: for most consumers of
            # this resource, a fresh comment on an already-open issue *is*
            # the useful signal -- it means this gate has been hit again
            # (e.g. deploys stacking up) before anyone closed the last one.
            merged_body = _merge_checklist_preserving_checked(
                working_issue.body or "", issue_body
            )
            print(f"about to update {working_issue=} with {merged_body=}")  # noqa: T201
            working_issue.edit(body=merged_body)
            # ★ Labels are only applied at creation, so an issue outlives any
            # later change to them -- a gate opened before the labels were
            # corrected keeps the wrong ones for its whole life, and a label is
            # what routing and queries actually read. update_in_place already
            # means "this resource owns this issue", so add any it is missing.
            #
            # Only add: other writers label the issue too (a bot marking it
            # ready to promote, say), and a label is a record that something
            # happened, not a state this put owns. Removing one it did not name
            # would erase that record and make its writer act again. Assignees
            # are deliberately NOT reconciled -- someone assigning themselves to
            # review a gate is a human workflow, and overwriting that would
            # fight them.
            desired_labels = labels or []
            if not set(desired_labels).issubset(
                {label.name for label in working_issue.labels}
            ):
                self._reconcile_labels(working_issue.number, desired_labels)
        else:
            working_issue = already_exists[-1]
            print(f"about to comment on {working_issue=} with {issue_body=}")  # noqa: T201
            working_issue.create_comment(issue_body)

        if close_if_file and _resolve_in_workspace(sources_dir, close_if_file).exists():
            print(  # noqa: T201
                f"{close_if_file!r} says there is nothing to review -- "
                f"auto-closing {working_issue=}"
            )
            working_issue.edit(state="closed")

        return self._to_version(working_issue), {}
