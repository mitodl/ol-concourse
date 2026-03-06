"""
resources:
  - name: my-concourse-github-issues
    type: concourse-github-issues
    source:
      github_auth_token: ((github.auth_token))
      project_key: concourse
      repo: "mitodl/my-project"

"""
from pathlib import Path
import textwrap
import json
import sys
from datetime import datetime, timedelta
from typing import Literal, Optional, Tuple
from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import Version, SortableVersionMixin
from github import Github, Auth, Consts, GithubException
from github.GithubObject import NotSet
from github.Issue import Issue

ISO_8601_FORMAT = "%Y-%m-%dT%H:%M:%S"


def build_metadata_dict(build_metadata: BuildMetadata) -> dict[str, str]:
    return dict(
        BUILD_URL=build_metadata.build_url(),
        BUILD_ID=build_metadata.BUILD_ID,
        BUILD_TEAM_NAME=build_metadata.BUILD_TEAM_NAME,
        BUILD_NAME=build_metadata.BUILD_NAME,
        BUILD_JOB_NAME=build_metadata.BUILD_JOB_NAME,
        BUILD_PIPELINE_NAME=build_metadata.BUILD_PIPELINE_NAME,
        BUILD_PIPELINE_INSTANCE_VARS=build_metadata.BUILD_PIPELINE_INSTANCE_VARS,
        ATC_EXTERNAL_URL=build_metadata.ATC_EXTERNAL_URL,
    )


class ConcourseGithubIssuesVersion(Version, SortableVersionMixin):
    def __init__(
        self,
        issue_created_at: str,
        issue_closed_at: Optional[str],
        issue_number: int,
        issue_state: Literal["open", "closed"],
        issue_title: str,
        issue_url: str,
    ):
        self.issue_created_at = issue_created_at
        self.issue_number = issue_number
        self.issue_state = issue_state
        self.issue_title = issue_title
        self.issue_url = issue_url
        self.issue_closed_at = issue_closed_at

    def __lt__(self, other: "ConcourseGithubIssuesVersion"):
        if self.issue_state == other.issue_state == "closed":
            return datetime.strptime(
                self.issue_closed_at,  # type: ignore[arg-type]
                ISO_8601_FORMAT,
            ) < datetime.strptime(
                other.issue_closed_at,  # type: ignore[arg-type]
                ISO_8601_FORMAT,
            )
        else:
            return int(self.issue_number) < int(other.issue_number)


class ConcourseGithubIssuesResource(ConcourseResource):
    def __init__(
        self,
        /,
        repository: str,
        gh_host: str = Consts.DEFAULT_BASE_URL,
        access_token: Optional[str] = None,
        app_id: Optional[int] = None,
        app_installation_id: Optional[int] = None,
        assignees: Optional[list[str]] = None,
        issue_prefix: Optional[str] = None,
        labels: Optional[list[str]] = None,
        private_ssh_key: Optional[str] = None,
        limit_old_versions: Optional[int] = None,
        auth_method: Literal["token", "app"] = "token",
        issue_state: Literal["open", "closed"] = "closed",
        issue_title_template: str = "[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed",
        issue_body_template: str = textwrap.dedent(
            """\
        The task {BUILD_JOB_NAME} in pipeline {BUILD_PIPELINE_NAME} has completed build number {BUILD_NAME}.
        Please refer to [the build log]({BUILD_URL}) for details of what changes this includes.
        Closing this issue will trigger the next job in the pipeline {BUILD_PIPELINE_NAME}.
        """
        ),
    ):
        super().__init__(ConcourseGithubIssuesVersion)
        if auth_method == "token":
            auth = self.auth_token(access_token)
        else:
            auth = self.auth_app(app_id, app_installation_id, private_ssh_key)
        self.gh = Github(base_url=gh_host, auth=auth, per_page=100)
        try:
            curr_limit = self.gh.get_rate_limit()
            if curr_limit.core.remaining == 0:
                sys.exit(1)
        except GithubException:
            # Rate limiting is not enabled
            curr_limit = None

        self.repo = self.gh.get_repo(repository)
        self.issue_state = issue_state
        self.issue_prefix = issue_prefix
        self.found_pipeline_issues: list[Issue] = []
        self.issue_labels = labels
        self.assignees = assignees
        self.issue_title_template = issue_title_template
        self.issue_body_template = issue_body_template
        self.limit_old_versions = limit_old_versions

    def auth_token(self, access_token):
        return Auth.Token(access_token)

    def auth_app(self, app_id, app_installation_id, private_ssh_key):
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
        issue_state: Optional[Literal["open", "closed"]] = None,
        since: Optional[datetime] = None,
    ) -> list[Issue]:
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
        all_pipeline_issues = self.get_all_issues(issue_state=state)

        unsorted = [
            issue
            for issue in all_pipeline_issues
            if (issue.title == title or "") and (issue.state == state)
        ]

        sorted_issues = sorted(unsorted, key=lambda issue: issue.number, reverse=True)
        return sorted_issues

    def get_matching_issues(self, since: Optional[datetime] = None) -> list[Issue]:
        all_pipeline_issues = self.get_all_issues(since=since)

        matching_issues = []
        for issue in all_pipeline_issues:
            if issue.title.startswith(self.issue_prefix or ""):
                matching_issues.append(issue)
                if (
                    self.limit_old_versions
                    and len(matching_issues) == self.limit_old_versions
                ):
                    break
        # Sort by number ascending to process oldest first if limited
        matching_issues.sort(key=lambda issue: issue.number)
        return matching_issues

    def fetch_new_versions(
        self, previous_version: Optional[ConcourseGithubIssuesVersion] = None
    ) -> set[ConcourseGithubIssuesVersion]:
        """Fetch new versions since the previous one."""
        since_datetime: Optional[datetime] = None
        if previous_version:
            timestamp_str: Optional[str] = None
            if self.issue_state == "closed":
                timestamp_str = previous_version.issue_closed_at
            elif self.issue_state == "open":
                timestamp_str = previous_version.issue_created_at

            if timestamp_str:
                try:
                    # Add a small buffer (1 second) to avoid potential clock skew issues
                    # or fetching the exact same event again.
                    since_datetime = datetime.strptime(
                        timestamp_str, ISO_8601_FORMAT
                    ) + timedelta(seconds=1)
                except ValueError:
                    # Handle cases where the timestamp might be invalid
                    print(f"Warning: Could not parse timestamp {timestamp_str}")
                    pass  # Proceed without 'since' if parsing fails

        matching_issues = self.get_matching_issues(since=since_datetime)
        versions = {self._to_version(issue) for issue in matching_issues}
        # Filter out the previous_version itself if it happens to be included
        if previous_version and previous_version in versions:
            versions.remove(previous_version)
        return versions

    def tombstone_version(
        self, version: ConcourseGithubIssuesVersion, build_metadata: BuildMetadata
    ):
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
    ) -> Tuple[ConcourseGithubIssuesVersion, dict[str, str]]:
        with Path(destination_dir).joinpath("gh_issue.json").open("w") as issue_file:
            issue_file.write(json.dumps(version.to_flat_dict() or {}))
        # We've triggered a deploy and consumed this issue. Set a tombstone in the title
        # so we'll ignore it in future and avoid duplicate triggering.
        self.tombstone_version(version, build_metadata)
        return version, {}

    def get_issue_body_from_build(self, build_metadata: BuildMetadata) -> str:
        return self.issue_body_template.format(**build_metadata_dict(build_metadata))

    def get_title_from_build(self, build_metadata: BuildMetadata) -> str:
        return self.issue_title_template.format(**build_metadata_dict(build_metadata))

    def publish_new_version(
        self,
        sources_dir,
        build_metadata: BuildMetadata,
        assignees: Optional[list[str]] = None,
        labels: Optional[list[str]] = None,
    ) -> Tuple[ConcourseGithubIssuesVersion, dict[str, str]]:
        # Assume that: title is enough uniqueness to discern whether the issue
        # already exists

        # Use GitHub Search API for efficiency instead of listing all issues
        candidate_issue_title = self.get_title_from_build(build_metadata)
        # Ensure title is properly quoted for the search query
        safe_title = candidate_issue_title.replace('"', '\\"')
        query = (
            f'repo:{self.repo.full_name} state:open "{safe_title}" in:title is:issue'
        )
        search_results = self.gh.search_issues(query)
        already_exists = list(search_results)  # Evaluate the PaginatedList

        if len(already_exists) > 1:
            print("Warning: There are multiple matches for the desired issue title!")

        if not already_exists:
            # Pass label names (strings) directly, avoid fetching Label objects
            working_issue = self.repo.create_issue(
                title=candidate_issue_title,
                assignees=assignees or [],
                labels=labels or [],  # Pass list of strings
                body=self.get_issue_body_from_build(build_metadata),
            )
            print(f"created issue: {working_issue=}")
        else:
            working_issue = already_exists[0]
            comment_body = self.get_issue_body_from_build(build_metadata)
            print(f"about to comment on {working_issue=} with {comment_body=}")
            working_issue.create_comment(comment_body)

        return self._to_version(working_issue), {}
