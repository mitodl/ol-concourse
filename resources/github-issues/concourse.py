"""Concourse resource for managing GitHub Issues as pipeline gate signals."""

from pathlib import Path
import re
import textwrap
import json
from datetime import datetime, timedelta
from typing import Literal
from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import Version, SortableVersionMixin
from github import Github, Auth, Consts
from github.GithubObject import NotSet
from github.Issue import Issue

ISO_8601_FORMAT = "%Y-%m-%dT%H:%M:%S"


# GitHub rejects a create/edit whose body exceeds this with a 422, and a put has
# no way to recover from that: by the time the gate issue is written the deploy
# it is reporting on has already happened, so the build fails red having done
# the work. A pulumi preview of a large stack clears this comfortably.
GITHUB_MAX_BODY_CHARS = 65536


def _close_unbalanced_markup(text: str) -> str:
    """Return the closers *text* needs so a cut does not swallow what follows.

    A body cut mid-``<details>`` or mid-code-fence leaves the block open, and
    GitHub then renders everything appended after it -- including the notice
    saying the body was truncated -- as part of that block. The notice is the
    one line a reviewer most needs to actually see.
    """
    closers = ""
    if text.count("```") % 2:
        closers += "```\n"
    unclosed_details = text.count("<details") - text.count("</details>")
    closers += "</details>\n" * max(unclosed_details, 0)
    return closers


def _truncate_body(text: str, max_chars: int = GITHUB_MAX_BODY_CHARS) -> str:
    """Cut *text* to *max_chars*, saying so in the text itself.

    Never truncate silently: a shortened diff that reads as complete is worse
    than no diff at all, because a reviewer approves a gate on the strength of
    what the body shows. The cut lands on a line boundary so the last thing
    shown is a whole entry rather than half a resource name.
    """
    if len(text) <= max_chars:
        return text

    notice = (
        "\n> :warning: This content was truncated to fit GitHub's "
        f"{GITHUB_MAX_BODY_CHARS}-character issue body limit "
        f"({len(text)} characters of content). "
        "See the build log for the full output.\n"
    )
    if max_chars <= len(notice):
        return text[:max_chars]

    head = text[: max_chars - len(notice)]
    boundary = head.rfind("\n")
    if boundary > 0:
        head = head[: boundary + 1]
    # Closing the open markup costs characters of its own, and trimming to pay
    # for them can expose further unclosed markup, so settle it by iteration.
    while True:
        closers = _close_unbalanced_markup(head)
        if len(head) + len(closers) + len(notice) <= max_chars:
            return head + closers + notice
        head = head[: max_chars - len(notice) - len(closers)]


def _fit_fragments(fragments: list[str], max_chars: int = GITHUB_MAX_BODY_CHARS) -> str:
    """Join *fragments* into one body of at most *max_chars* characters.

    Each fragment gets an equal share of the budget, and whatever a short
    fragment leaves unused is redistributed to the ones that overflow. Trimming
    the tail of the joined body would instead drop whole sections, and the
    sections are the reason ``body_files`` exists: a promotion gate composes
    "what this deploy did" with "what promoting it will do next" precisely
    because a reviewer needs both. Both, marked where they were cut, beats one
    of them in full.
    """
    separators = len(fragments) - 1
    joined = "\n".join(fragments)
    if len(joined) <= max_chars:
        return joined

    budget = max_chars - separators
    budgets = [0] * len(fragments)
    # Shortest first, so a fragment that cannot spend its share hands the
    # remainder on to the ones that can.
    by_length = sorted(range(len(fragments)), key=lambda i: len(fragments[i]))
    for position, index in enumerate(by_length):
        share = budget // (len(by_length) - position)
        budgets[index] = min(len(fragments[index]), share)
        budget -= budgets[index]

    return "\n".join(
        _truncate_body(fragment, allowance)
        for fragment, allowance in zip(fragments, budgets, strict=True)
    )


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
    ):
        """Initialize with GitHub API credentials and issue configuration."""
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
            if self.skip_if_labeled:
                issue_label_names = {lbl.name for lbl in issue.labels}
                if issue_label_names.intersection(self.skip_if_labeled):
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
            if self.skip_if_labeled:
                issue_label_names = {lbl.name for lbl in issue.labels}
                if issue_label_names.intersection(self.skip_if_labeled):
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
        """
        if previous_version is None:
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
        """Rename the issue with a CONSUMED prefix so it is not re-triggered."""
        current_title = self.get_title_from_build(build_metadata)
        job_number = build_metadata.BUILD_NAME
        new_title = f"[CONSUMED #{job_number}]" + current_title

        # Check state from the version data first
        if version.issue_state == "closed":
            # Fetch the issue object only when we know we need to edit it
            issue = self.repo.get_issue(int(version.issue_number))  # API Call 1
            issue.edit(title=new_title)

    def download_version(
        self,
        version: ConcourseGithubIssuesVersion,
        destination_dir: str,
        build_metadata: BuildMetadata,
    ) -> tuple[ConcourseGithubIssuesVersion, dict[str, str]]:
        """Write issue metadata to disk and tombstone the issue."""
        with Path(destination_dir).joinpath("gh_issue.json").open("w") as issue_file:
            issue_file.write(json.dumps(version.to_flat_dict() or {}))
        # We've triggered a deploy and consumed this issue. Set a tombstone in the title
        # so we'll ignore it in future and avoid duplicate triggering.
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

        Whatever the source, the result is capped at GitHub's issue body limit
        and says where it was cut. Overrunning it fails the put with a 422 after
        the deploy has already happened, which loses the whole gate rather than
        the tail of a diff.
        """
        if body_files:
            return _fit_fragments(
                [
                    self._read_body_file(f, sources_dir, required=False)
                    for f in body_files
                ]
            )
        if body_file is not None:
            return _truncate_body(self._read_body_file(body_file, sources_dir))
        return _truncate_body(
            self.issue_body_template.format(**build_metadata_dict(build_metadata))
        )

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
        body_path = Path(body_file)
        if body_path.is_absolute():
            msg = "body_file must be a relative path"
            raise ValueError(msg)
        resolved = (sources_dir / body_path).resolve()
        if not resolved.is_relative_to(sources_dir.resolve()):
            msg = "body_file must be within the workspace sources directory"
            raise ValueError(msg)
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

    def publish_new_version(  # noqa: PLR0913
        self,
        sources_dir,
        build_metadata: BuildMetadata,
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
        body_file: str | None = None,
        title_template: str | None = None,
        body_files: list[str] | None = None,
    ) -> tuple[ConcourseGithubIssuesVersion, dict[str, str]]:
        """Create or comment on a GitHub Issue and return its version."""
        # Assume that: title is enough uniqueness to discern whether the issue
        # already exists

        issue_body = self.get_issue_body_from_build(
            build_metadata,
            body_file=body_file,
            sources_dir=sources_dir,
            body_files=body_files,
        )

        # Use GitHub Search API for efficiency instead of listing all issues
        candidate_issue_title = self.get_title_from_build(
            build_metadata, title_template=title_template
        )
        # Ensure title is properly quoted for the search query
        safe_title = candidate_issue_title.replace('"', '\\"')
        query = (
            f'repo:{self.repo.full_name} state:open "{safe_title}" in:title is:issue'
        )
        search_results = self.gh.search_issues(query)
        already_exists = list(search_results)  # Evaluate the PaginatedList

        if len(already_exists) > 1:
            print("Warning: There are multiple matches for the desired issue title!")  # noqa: T201

        if not already_exists:
            # Pass label names (strings) directly, avoid fetching Label objects
            working_issue = self.repo.create_issue(
                title=candidate_issue_title,
                assignees=assignees or [],
                labels=labels or [],  # Pass list of strings
                body=issue_body,
            )
            print(f"created issue: {working_issue=}")  # noqa: T201
        elif self.update_in_place:
            working_issue = already_exists[0]
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
            # means "this resource owns this issue", so reconcile them here.
            #
            # This is a replace, unlike the body, which is merged to preserve a
            # reviewer's ticked checkboxes. There is no equivalent of a ticked
            # box for labels: nothing distinguishes one a human added from a
            # stale one this resource wrote, and leaving a contradictory label
            # in place is worse than dropping a hand-added one. Assignees are
            # deliberately NOT reconciled -- someone assigning themselves to
            # review a gate is a human workflow, and overwriting that would
            # fight them.
            desired_labels = labels or []
            if set(desired_labels) != {label.name for label in working_issue.labels}:
                print(f"about to relabel {working_issue=} with {desired_labels=}")  # noqa: T201
                working_issue.edit(labels=desired_labels)
        else:
            working_issue = already_exists[0]
            print(f"about to comment on {working_issue=} with {issue_body=}")  # noqa: T201
            working_issue.create_comment(issue_body)

        return self._to_version(working_issue), {}
