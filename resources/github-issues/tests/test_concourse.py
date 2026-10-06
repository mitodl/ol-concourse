"""Tests for the github-issues Concourse resource."""

from github.GithubObject import NotSet
import pytest
from unittest.mock import MagicMock, call, patch
from datetime import datetime, timedelta
import json

from concourse import (
    ConcourseGithubIssuesResource,
    ConcourseGithubIssuesVersion,
    ISO_8601_FORMAT,
    SKIPPED_VERSION,
    _merge_checklist_preserving_checked,
)
from concoursetools import BuildMetadata  # Import the actual class
from concoursetools.testing import SimpleTestResourceWrapper
from github.Issue import Issue


# Helper function to create mock BuildMetadata objects
def mock_build_metadata(**kwargs) -> BuildMetadata:
    """Create a BuildMetadata object with default values, allowing overrides."""
    defaults = {
        "BUILD_ID": "12345",
        "BUILD_NAME": "42",
        "BUILD_JOB_NAME": "test-job",
        "BUILD_PIPELINE_NAME": "test-pipeline",
        "BUILD_PIPELINE_INSTANCE_VARS": '{"var": "value"}',
        "BUILD_TEAM_NAME": "main",
        "ATC_EXTERNAL_URL": "http://concourse.example.com",
    }
    # Map simplified kwargs to the expected BuildMetadata keys
    key_map = {
        "pipeline_name": "BUILD_PIPELINE_NAME",
        "job_name": "BUILD_JOB_NAME",
        "build_name": "BUILD_NAME",
        # Add other mappings if needed
    }
    mapped_kwargs = {key_map.get(k, k): v for k, v in kwargs.items()}

    # Override defaults with provided mapped kwargs
    defaults.update(mapped_kwargs)
    # Create BuildMetadata instance using the combined dict
    return BuildMetadata(**defaults)


# Helper function to create mock Issue objects
def create_mock_issue(
    number: int,
    title: str,
    state: str,
    created_at: datetime,
    closed_at: datetime | None = None,
    url: str = "http://example.com/issue",
    labels: list[str] | None = None,
) -> MagicMock:
    mock = MagicMock(spec=Issue)
    mock.number = number
    mock.title = title
    mock.state = state
    mock.created_at = created_at
    mock.closed_at = closed_at
    mock.url = url
    # Mock the labels attribute if needed, PyGithub returns Label objects
    mock_labels = []
    if labels:
        for label_name in labels:
            mock_label = MagicMock()
            mock_label.name = label_name
            mock_labels.append(mock_label)
    mock.labels = mock_labels
    return mock


# Sample datetimes for consistent testing
NOW = datetime.now()  # noqa: DTZ005
T_MINUS_1 = NOW - timedelta(days=1)
T_MINUS_2 = NOW - timedelta(days=2)
T_MINUS_3 = NOW - timedelta(days=3)

# Mock Issues Data
MOCK_ISSUES_DATA = [
    {
        "number": 1,
        "title": "[bot] Issue 1",
        "state": "closed",
        "created_at": T_MINUS_3,
        "closed_at": T_MINUS_2,
        "labels": ["pipeline"],
    },
    {
        "number": 2,
        "title": "User Issue 2",
        "state": "closed",
        "created_at": T_MINUS_2,
        "closed_at": T_MINUS_1,
        "labels": ["bug"],
    },
    {
        "number": 3,
        "title": "[bot] Issue 3",
        "state": "open",
        "created_at": T_MINUS_1,
        "closed_at": None,
        "labels": ["pipeline", "urgent"],
    },
    {
        "number": 4,
        "title": "User Issue 4",
        "state": "open",
        "created_at": NOW,
        "closed_at": None,
        "labels": [],
    },
]

MOCK_ISSUES = [
    create_mock_issue(**data)  # type: ignore[arg-type]
    for data in MOCK_ISSUES_DATA
]


@pytest.fixture
def mock_github():
    """Fixture to mock the Github API client and repository."""
    with patch("concourse.Github") as MockGithub:
        mock_gh_instance = MockGithub.return_value
        mock_repo = MagicMock()
        mock_repo.full_name = (
            "test/repo"  # Set the full_name attribute for search queries
        )
        mock_gh_instance.get_repo.return_value = mock_repo
        yield mock_gh_instance, mock_repo


@pytest.mark.parametrize(
    "config_state, config_prefix, expected_issue_number",
    [
        # GitHub returns issues newest-first, so the mock list is reversed
        # (highest number last → first in the reversed list).
        # [2,1] for closed, [4,3] for open.
        ("closed", "[bot]", 1),  # [2,1]: issue 2 has no prefix, issue 1 matches
        ("closed", None, 2),  # [2,1]: issue 2 is the first (most-recent) match
        ("open", "[bot]", 3),  # [4,3]: issue 4 has no prefix, issue 3 matches
        ("open", None, 4),  # [4,3]: issue 4 is the first (most-recent) match
    ],
)
def test_fetch_new_versions_no_previous(
    mock_github, config_state, config_prefix, expected_issue_number
):
    """First check returns only the single most-recent matching version.

    Concourse only needs the latest version to seed its state on the first
    check, so we stop scanning as soon as we find one match rather than
    returning the entire history.
    """
    mock_gh_instance, mock_repo = mock_github

    # GitHub API returns issues newest-first; simulate that ordering.
    api_call_issues = [
        issue for issue in reversed(MOCK_ISSUES) if issue.state == config_state
    ]
    mock_repo.get_issues.return_value = api_call_issues

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state=config_state,
        issue_prefix=config_prefix,
    )
    wrapper = SimpleTestResourceWrapper(resource)

    versions = wrapper.fetch_new_versions(None)
    version_numbers = {v.issue_number for v in versions}

    # Exactly one version is returned: the most-recent matching issue.
    assert version_numbers == {expected_issue_number}
    # Verify get_issues was called with the correct state and no 'since'
    mock_repo.get_issues.assert_called_once_with(
        state=config_state, labels=[], since=NotSet
    )


def test_fetch_new_versions_with_previous_closed(mock_github):
    """Test fetching closed issues newer than a previous closed version."""
    mock_gh_instance, mock_repo = mock_github

    # Previous version corresponds to issue #1 (closed T_MINUS_2)
    previous_version = ConcourseGithubIssuesVersion(
        issue_number=1,
        issue_title="[bot] Issue 1",
        issue_state="closed",
        issue_created_at=T_MINUS_3.strftime(ISO_8601_FORMAT),
        issue_closed_at=T_MINUS_2.strftime(ISO_8601_FORMAT),
        issue_url="http://example.com/issue/1",
    )

    # API should be called with 'since' = closed_at + 1s
    # Replicate the resource logic: parse the string format which drops microseconds
    parsed_closed_at = datetime.strptime(  # noqa: DTZ007
        previous_version.issue_closed_at,  # type: ignore [arg-type]
        ISO_8601_FORMAT,
    )
    expected_since = parsed_closed_at + timedelta(seconds=1)

    # Mock API to return only issues closed after 'since' (Issue #2)
    api_call_issues = [
        issue
        for issue in MOCK_ISSUES
        if issue.state == "closed" and issue.closed_at >= expected_since
    ]
    mock_repo.get_issues.return_value = api_call_issues

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="closed",
        issue_prefix=None,  # No prefix filtering for this test
    )
    wrapper = SimpleTestResourceWrapper(resource)

    versions = wrapper.fetch_new_versions(previous_version)
    version_numbers = {v.issue_number for v in versions}

    assert version_numbers == {2}  # Only issue #2 should be newer
    mock_repo.get_issues.assert_called_once_with(
        state="closed", labels=[], since=expected_since
    )


def test_fetch_new_versions_with_previous_open(mock_github):
    """Test fetching open issues newer than a previous open version."""
    mock_gh_instance, mock_repo = mock_github

    # Previous version corresponds to issue #3 (created T_MINUS_1)
    previous_version = ConcourseGithubIssuesVersion(
        issue_number=3,
        issue_title="[bot] Issue 3",
        issue_state="open",
        issue_created_at=T_MINUS_1.strftime(ISO_8601_FORMAT),
        issue_closed_at=None,
        issue_url="http://example.com/issue/3",
    )

    # API should be called with 'since' = created_at + 1s
    # Replicate the resource logic: parse the string format which drops microseconds
    parsed_created_at = datetime.strptime(  # noqa: DTZ007
        previous_version.issue_created_at, ISO_8601_FORMAT
    )
    expected_since = parsed_created_at + timedelta(seconds=1)

    # Mock API to return only issues created after 'since' (Issue #4)
    api_call_issues = [
        issue
        for issue in MOCK_ISSUES
        if issue.state == "open" and issue.created_at >= expected_since
    ]
    mock_repo.get_issues.return_value = api_call_issues

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_prefix=None,  # No prefix filtering
    )
    wrapper = SimpleTestResourceWrapper(resource)

    versions = wrapper.fetch_new_versions(previous_version)
    version_numbers = {v.issue_number for v in versions}

    assert version_numbers == {4}  # Only issue #4 should be newer
    mock_repo.get_issues.assert_called_once_with(
        state="open", labels=[], since=expected_since
    )


def test_fetch_new_versions_with_prefix_and_previous(mock_github):
    """Test fetching with prefix and previous version combined."""
    mock_gh_instance, mock_repo = mock_github

    # Previous version is issue #1 (closed T_MINUS_2)
    previous_version = ConcourseGithubIssuesVersion(
        issue_number=1,
        issue_title="[bot] Issue 1",
        issue_state="closed",
        issue_created_at=T_MINUS_3.strftime(ISO_8601_FORMAT),
        issue_closed_at=T_MINUS_2.strftime(ISO_8601_FORMAT),
        issue_url="http://example.com/issue/1",
    )
    # Replicate the resource logic: parse the string format which drops microseconds
    parsed_closed_at = datetime.strptime(  # noqa: DTZ007
        previous_version.issue_closed_at,  # type: ignore [arg-type]
        ISO_8601_FORMAT,
    )
    expected_since = parsed_closed_at + timedelta(seconds=1)

    # Mock API returns issue #2
    api_call_issues = [
        issue
        for issue in MOCK_ISSUES
        if issue.state == "closed" and issue.closed_at >= expected_since
    ]  # This will be just issue #2
    mock_repo.get_issues.return_value = api_call_issues

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="closed",
        issue_prefix="[bot]",  # Prefix filtering IS enabled
    )
    wrapper = SimpleTestResourceWrapper(resource)

    versions = wrapper.fetch_new_versions(previous_version)
    version_numbers = {v.issue_number for v in versions}

    # Issue #2 is returned by API (newer), but filtered out by prefix.
    assert version_numbers == set()
    mock_repo.get_issues.assert_called_once_with(
        state="closed", labels=[], since=expected_since
    )


def test_fetch_new_versions_limit_old(mock_github):
    """limit_old_versions caps results on the incremental (with-previous) path.

    The first-check path always returns a single version regardless of
    limit_old_versions; the cap only applies when there IS a previous version
    and multiple new issues have appeared since then.
    """
    mock_gh_instance, mock_repo = mock_github

    # Five new closed issues, all created/closed after the previous version.
    # GitHub returns newest first, so order them descending by number.
    new_issues = [
        create_mock_issue(
            number=i,
            title=f"[bot] New Issue {i}",
            state="closed",
            created_at=NOW - timedelta(hours=10 - i),
            closed_at=NOW - timedelta(hours=10 - i),
        )
        for i in range(5, 10)  # Issues 5..9
    ]
    # Simulate GitHub newest-first ordering: 9, 8, 7, 6, 5
    api_call_issues = list(reversed(new_issues))
    mock_repo.get_issues.return_value = api_call_issues

    previous_version = ConcourseGithubIssuesVersion(
        issue_number=1,
        issue_title="[bot] Issue 1",
        issue_state="closed",
        issue_created_at=T_MINUS_3.strftime(ISO_8601_FORMAT),
        issue_closed_at=T_MINUS_2.strftime(ISO_8601_FORMAT),
        issue_url="http://example.com/issue/1",
    )
    parsed_closed_at = datetime.strptime(  # noqa: DTZ007
        previous_version.issue_closed_at,  # type: ignore[arg-type]
        ISO_8601_FORMAT,
    )
    expected_since = parsed_closed_at + timedelta(seconds=1)

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="closed",
        issue_prefix="[bot]",
        limit_old_versions=2,
    )
    wrapper = SimpleTestResourceWrapper(resource)

    versions = wrapper.fetch_new_versions(previous_version)
    version_numbers = {v.issue_number for v in versions}

    # get_matching_issues iterates newest-first [9,8,7,6,5], collects the
    # first 2 matches [9,8] (limit_old_versions=2), then sorts ascending.
    assert version_numbers == {8, 9}
    mock_repo.get_issues.assert_called_once_with(
        state="closed", labels=[], since=expected_since
    )


@patch("pathlib.Path.open")
def test_download_version_tombstones(mock_open, mock_github, tmp_path):
    """Test that download_version tombstones the issue and writes the file."""
    mock_gh_instance, mock_repo = mock_github
    mock_issue = create_mock_issue(
        number=5,
        title="[bot] Ready Issue",
        state="closed",
        created_at=T_MINUS_2,
        closed_at=T_MINUS_1,
    )
    mock_repo.get_issue.return_value = mock_issue

    resource = ConcourseGithubIssuesResource(
        repository="test/repo", access_token="dummy_token", issue_state="closed"
    )

    version_to_download = ConcourseGithubIssuesVersion(
        issue_number=5,
        issue_title="[bot] Ready Issue",
        issue_state="closed",
        issue_created_at=T_MINUS_2.strftime(ISO_8601_FORMAT),
        issue_closed_at=T_MINUS_1.strftime(ISO_8601_FORMAT),
        issue_url="http://example.com/issue/5",
    )

    build_meta = mock_build_metadata()  # Use default build meta here
    dest_dir = str(tmp_path)

    # Call download_version directly on the resource instance
    returned_version, returned_metadata = resource.download_version(
        version=version_to_download,
        destination_dir=dest_dir,
        build_metadata=build_meta,
    )

    # Check tombstoning
    # Need to update the expected title based on the default build_meta name '42'
    mock_repo.get_issue.assert_called_once_with(5)
    # Calculate the expected title exactly how the resource does it
    current_title_from_build = resource.get_title_from_build(build_meta)
    expected_tombstone_title = (
        f"[CONSUMED #{build_meta.BUILD_NAME}]" + current_title_from_build
    )
    mock_issue.edit.assert_called_once_with(title=expected_tombstone_title)
    # Check file writing
    mock_open.assert_called_once_with("w")
    # Verify the file handle's write method was called
    mock_open.return_value.__enter__.return_value.write.assert_called_once()

    # Check return values
    assert returned_version == version_to_download
    assert returned_metadata == {}


def test_publish_new_version_creates_new_issue(mock_github):
    """Test publish creates a new issue when none exists."""
    mock_gh_instance, mock_repo = mock_github
    mock_gh_instance.search_issues.return_value = []  # No existing issue found
    created_mock_issue = create_mock_issue(
        number=10,
        title="[bot] Pipeline my-pipeline task my-job completed",
        state="open",
        created_at=NOW,
    )
    mock_repo.create_issue.return_value = created_mock_issue

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",  # Important for publish logic
        issue_title_template=(
            "[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed"
        ),
        issue_body_template="Build {BUILD_NAME} finished.",
        assignees=["user1"],
        labels=["bot-created"],
    )
    build_meta = mock_build_metadata(
        pipeline_name="my-pipeline", job_name="my-job", build_name="b123"
    )

    # Use resource directly for publish, wrapper doesn't have it
    version, metadata = resource.publish_new_version(
        sources_dir="dummy",
        build_metadata=build_meta,
        assignees=["user1"],  # Pass explicitly if needed by method
        labels=["bot-created"],
    )

    # Check search was called
    expected_title = "[bot] Pipeline my-pipeline task my-job completed"
    expected_query = f'repo:test/repo state:open "{expected_title}" in:title is:issue'
    mock_gh_instance.search_issues.assert_called_once_with(expected_query)

    # Check create_issue was called
    expected_body = "Build b123 finished."
    mock_repo.create_issue.assert_called_once_with(
        title=expected_title,
        assignees=["user1"],
        labels=["bot-created"],
        body=expected_body,
    )

    # Check returned version
    assert version.issue_number == 10
    assert version.issue_title == expected_title
    assert version.issue_state == "open"
    assert metadata == {}


def test_publish_new_version_truncates_oversized_body(mock_github):
    """An oversized body must be shortened, not sent to GitHub to 422 on.

    GitHub rejects any issue/comment body over 65536 characters. A large
    enough Pulumi diff or body_files composition can produce one; this
    resource has to guarantee the API call it makes stays within that limit
    regardless of what template or artifact produced the body.
    """
    mock_gh_instance, mock_repo = mock_github
    mock_gh_instance.search_issues.return_value = []
    mock_repo.create_issue.return_value = create_mock_issue(
        number=11, title="[bot] oversized", state="open", created_at=NOW
    )

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template="[bot] oversized",
        issue_body_template="x" * 100_000,
    )

    resource.publish_new_version(
        sources_dir="dummy", build_metadata=mock_build_metadata()
    )

    _, kwargs = mock_repo.create_issue.call_args
    assert len(kwargs["body"]) <= 65536
    assert "truncated" in kwargs["body"]


def test_publish_new_version_comments_on_existing_by_default(mock_github):
    """Without update_in_place, publish comments on an existing issue.

    This is the useful default for most consumers of this resource: a fresh
    comment on an already-open issue is itself a signal (e.g. "this gate has
    been hit again -- deploys stacking up -- before anyone closed the last
    one"). update_in_place is an opt-in for the specific case (like the
    release resource's checklist) where re-showing the same content is noise
    rather than signal.
    """
    mock_gh_instance, mock_repo = mock_github
    existing_mock_issue = create_mock_issue(
        number=9,
        title="[bot] Pipeline my-pipeline task my-job completed",
        state="open",
        created_at=T_MINUS_1,
    )
    existing_mock_issue.create_comment = MagicMock()
    existing_mock_issue.edit = MagicMock()
    mock_gh_instance.search_issues.return_value = [existing_mock_issue]

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template=(
            "[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed"
        ),
        issue_body_template="Build {BUILD_NAME} finished.",
    )
    build_meta = mock_build_metadata(
        pipeline_name="my-pipeline", job_name="my-job", build_name="b456"
    )

    resource.publish_new_version(sources_dir="dummy", build_metadata=build_meta)

    existing_mock_issue.create_comment.assert_called_once_with("Build b456 finished.")
    existing_mock_issue.edit.assert_not_called()


def test_publish_new_version_updates_existing_when_update_in_place(mock_github):
    """With update_in_place=True, publish edits an existing issue's body."""
    mock_gh_instance, mock_repo = mock_github
    existing_mock_issue = create_mock_issue(
        number=9,
        title="[bot] Pipeline my-pipeline task my-job completed",
        state="open",
        created_at=T_MINUS_1,
    )
    existing_mock_issue.body = ""
    existing_mock_issue.edit = MagicMock()
    mock_gh_instance.search_issues.return_value = [
        existing_mock_issue
    ]  # Found existing

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template=(
            "[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed"
        ),
        issue_body_template="Build {BUILD_NAME} finished.",
        update_in_place=True,
    )
    build_meta = mock_build_metadata(
        pipeline_name="my-pipeline", job_name="my-job", build_name="b456"
    )

    # Use resource directly for publish
    version, metadata = resource.publish_new_version(
        sources_dir="dummy", build_metadata=build_meta
    )

    # Check search was called
    expected_title = "[bot] Pipeline my-pipeline task my-job completed"
    expected_query = f'repo:test/repo state:open "{expected_title}" in:title is:issue'
    mock_gh_instance.search_issues.assert_called_once_with(expected_query)

    # Check create_issue was NOT called
    mock_repo.create_issue.assert_not_called()

    # Check the existing issue's body was edited in place, not commented on
    expected_body = "Build b456 finished."
    existing_mock_issue.edit.assert_called_once_with(body=expected_body)
    existing_mock_issue.create_comment.assert_not_called()

    # Check returned version matches the existing issue
    assert version.issue_number == 9
    assert version.issue_title == expected_title
    assert version.issue_state == "open"
    assert metadata == {}


def test_publish_new_version_title_template_overrides_source_default(mock_github):
    """A per-call title_template (e.g. Concourse-resolved with a version) wins."""
    mock_gh_instance, mock_repo = mock_github
    mock_gh_instance.search_issues.return_value = []
    created_mock_issue = create_mock_issue(
        number=11, title="Release my-app 2026.7.22.1", state="open", created_at=NOW
    )
    mock_repo.create_issue.return_value = created_mock_issue

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template="Release {BUILD_PIPELINE_NAME}",
    )
    build_meta = mock_build_metadata(pipeline_name="my-app")

    resource.publish_new_version(
        sources_dir="dummy",
        build_metadata=build_meta,
        # Stands in for what Concourse would have already resolved from
        # "Release {BUILD_PIPELINE_NAME} ((.:image_tag))" via a put step's
        # params -- this resource never sees the ((.:var)) syntax itself.
        title_template="Release my-app 2026.7.22.1",
    )

    expected_title = "Release my-app 2026.7.22.1"
    expected_query = f'repo:test/repo state:open "{expected_title}" in:title is:issue'
    mock_gh_instance.search_issues.assert_called_once_with(expected_query)
    mock_repo.create_issue.assert_called_once()
    _, kwargs = mock_repo.create_issue.call_args
    assert kwargs["title"] == expected_title


# ---------------------------------------------------------------------------
# skip_if_labeled (issue #15)
# ---------------------------------------------------------------------------


def test_get_matching_issues_skips_labeled(mock_github):
    """Issues with a label in skip_if_labeled are excluded from results."""
    mock_gh_instance, mock_repo = mock_github
    skip_label_issue = create_mock_issue(
        number=5,
        title="[bot] Issue 5",
        state="open",
        created_at=T_MINUS_1,
        labels=["deployed"],
    )
    normal_issue = create_mock_issue(
        number=6,
        title="[bot] Issue 6",
        state="open",
        created_at=T_MINUS_1,
        labels=[],
    )
    mock_repo.get_issues.return_value = [skip_label_issue, normal_issue]

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_prefix="[bot]",
        skip_if_labeled=["deployed"],
    )

    results = resource.get_matching_issues()

    assert len(results) == 1
    assert results[0].number == 6


def test_get_matching_issues_no_skip_when_label_absent(mock_github):
    """Issues are kept when they do not carry any skip label."""
    mock_gh_instance, mock_repo = mock_github
    issue = create_mock_issue(
        number=7,
        title="[bot] Issue 7",
        state="open",
        created_at=T_MINUS_1,
        labels=["other-label"],
    )
    mock_repo.get_issues.return_value = [issue]

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_prefix="[bot]",
        skip_if_labeled=["deployed"],
    )

    results = resource.get_matching_issues()

    assert len(results) == 1
    assert results[0].number == 7


def test_skip_if_labeled_defaults_to_empty(mock_github):
    """skip_if_labeled defaults to empty list — all issues pass through."""
    mock_gh_instance, mock_repo = mock_github
    issue = create_mock_issue(
        number=8,
        title="[bot] Issue 8",
        state="open",
        created_at=T_MINUS_1,
        labels=["anything"],
    )
    mock_repo.get_issues.return_value = [issue]

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_prefix="[bot]",
    )

    results = resource.get_matching_issues()
    assert len(results) == 1


# ---------------------------------------------------------------------------
# body_file (issue #14)
# ---------------------------------------------------------------------------


def test_publish_new_version_body_file_creates_with_file_contents(
    mock_github, tmp_path
):
    """When body_file is set, issue body is read from the workspace file."""
    mock_gh_instance, mock_repo = mock_github
    mock_gh_instance.search_issues.return_value = []  # No existing issue
    expected_body = "Release notes from file.\n"
    body_path = tmp_path / "checklist.md"
    body_path.write_text(expected_body)

    created_issue = create_mock_issue(
        number=20, title="Release 2026.04.14.1", state="open", created_at=NOW
    )
    mock_repo.create_issue.return_value = created_issue

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template="Release {BUILD_PIPELINE_NAME}",
    )
    build_meta = mock_build_metadata(pipeline_name="2026.04.14.1")

    version, metadata = resource.publish_new_version(
        sources_dir=tmp_path,
        build_metadata=build_meta,
        body_file="checklist.md",
    )

    mock_repo.create_issue.assert_called_once()
    _, kwargs = mock_repo.create_issue.call_args
    assert kwargs["body"] == expected_body
    assert version.issue_number == 20


def test_publish_new_version_body_file_updates_existing_when_update_in_place(
    mock_github, tmp_path
):
    """With update_in_place=True, body_file content also edits the body."""
    mock_gh_instance, mock_repo = mock_github
    expected_body = "Updated release notes.\n"
    body_path = tmp_path / "notes.md"
    body_path.write_text(expected_body)

    existing_issue = create_mock_issue(
        number=19, title="Release test-pipeline", state="open", created_at=T_MINUS_1
    )
    existing_issue.body = ""
    existing_issue.edit = MagicMock()
    mock_gh_instance.search_issues.return_value = [existing_issue]

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template="Release {BUILD_PIPELINE_NAME}",
        update_in_place=True,
    )
    build_meta = mock_build_metadata(pipeline_name="test-pipeline")

    resource.publish_new_version(
        sources_dir=tmp_path,
        build_metadata=build_meta,
        body_file="notes.md",
    )

    existing_issue.edit.assert_called_once_with(body=expected_body)
    existing_issue.create_comment.assert_not_called()


def test_timeout_default(mock_github):
    """Github is instantiated with the 30-second default timeout."""
    with patch("concourse.Github") as MockGithub:
        MockGithub.return_value.get_repo.return_value = MagicMock()
        ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
        )
        _, kwargs = MockGithub.call_args
        assert kwargs["timeout"] == 30


def test_timeout_configurable(mock_github):
    """Github is instantiated with the caller-supplied timeout."""
    with patch("concourse.Github") as MockGithub:
        MockGithub.return_value.get_repo.return_value = MagicMock()
        ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            timeout=60,
        )
        _, kwargs = MockGithub.call_args
        assert kwargs["timeout"] == 60


OLD_MERGE_BODY = """## Release 1.2.3

### Changes

- [x] `abc1234` chore: bump version by bot@example.com
- [ ] **fix: real bug** (#5) by human@example.com
"""

NEW_MERGE_BODY = """## Release 1.2.3

### Changes

- [ ] `abc1234` chore: bump version by bot@example.com
- [ ] **fix: real bug** (#5) by human@example.com
- [ ] **new: another change** (#6) by human@example.com
"""


def test_merge_checklist_preserving_checked_keeps_already_checked_lines():
    """A line checked in the old body stays checked in the merged result."""
    merged = _merge_checklist_preserving_checked(OLD_MERGE_BODY, NEW_MERGE_BODY)

    lines = merged.splitlines()
    assert "- [x] `abc1234` chore: bump version by bot@example.com" in lines


def test_merge_checklist_preserving_checked_leaves_unchecked_lines_unchecked():
    """A line that wasn't checked before stays unchecked after merging."""
    merged = _merge_checklist_preserving_checked(OLD_MERGE_BODY, NEW_MERGE_BODY)

    lines = merged.splitlines()
    assert "- [ ] **fix: real bug** (#5) by human@example.com" in lines


def test_merge_checklist_preserving_checked_adds_genuinely_new_lines_unchecked():
    """A line with no match in the old body is a new item -- stays unchecked."""
    merged = _merge_checklist_preserving_checked(OLD_MERGE_BODY, NEW_MERGE_BODY)

    lines = merged.splitlines()
    assert "- [ ] **new: another change** (#6) by human@example.com" in lines


def test_merge_checklist_preserving_checked_empty_old_body_is_passthrough():
    """No prior body (e.g. body attribute was empty) -- new body passes through."""
    merged = _merge_checklist_preserving_checked("", NEW_MERGE_BODY)

    assert merged == NEW_MERGE_BODY


def test_merge_checklist_preserving_checked_preserves_trailing_newline():
    """Trailing newline follows new_body's own ending, not old_body's."""
    assert NEW_MERGE_BODY.endswith("\n")

    merged = _merge_checklist_preserving_checked(OLD_MERGE_BODY, NEW_MERGE_BODY)

    assert merged.endswith("\n")
    assert not merged.endswith("\n\n")


class TestBodyFilesComposition:
    """A promotion-gate body is assembled from more than one artifact.

    A Concourse put emits no artifact of its own, so "what this deploy did" and
    "what promoting it will do next" arrive via two separate implicit gets. The
    issue body has to stitch them back together.
    """

    @staticmethod
    def _resource() -> ConcourseGithubIssuesResource:
        with patch("concourse.Github"):
            return ConcourseGithubIssuesResource(
                repository="test/repo",
                access_token="dummy_token",
                issue_prefix="[bot] ",
            )

    def test_files_are_joined_in_order(self, tmp_path):
        """Order is meaningful: what happened, then what will happen."""
        (tmp_path / "applied.md").write_text("## What this deploy did")
        (tmp_path / "preview.md").write_text("## What promoting will do")

        body = self._resource().get_issue_body_from_build(
            mock_build_metadata(),
            sources_dir=tmp_path,
            body_files=["applied.md", "preview.md"],
        )

        assert body.index("What this deploy did") < body.index("What promoting will do")

    def test_missing_file_is_reported_not_silently_skipped(self, tmp_path):
        """The step that writes the preview may fail without failing the deploy.

        Dropping the section silently would leave a body that reads as complete
        while omitting the half a reviewer might be relying on.
        """
        (tmp_path / "applied.md").write_text("## What this deploy did")

        body = self._resource().get_issue_body_from_build(
            mock_build_metadata(),
            sources_dir=tmp_path,
            body_files=["applied.md", "preview.md"],
        )

        assert "What this deploy did" in body
        assert "was not" in body
        assert "preview.md" in body

    def test_single_body_file_still_works(self, tmp_path):
        (tmp_path / "only.md").write_text("just this")
        body = self._resource().get_issue_body_from_build(
            mock_build_metadata(), body_file="only.md", sources_dir=tmp_path
        )
        assert body == "just this"

    def test_traversal_is_refused_for_every_file(self, tmp_path):
        """The path check must apply to each entry, not just the first."""
        (tmp_path / "ok.md").write_text("fine")
        with pytest.raises(ValueError, match="within the workspace"):
            self._resource().get_issue_body_from_build(
                mock_build_metadata(),
                sources_dir=tmp_path,
                body_files=["ok.md", "../escape.md"],
            )

    def test_template_still_used_when_no_files_given(self, tmp_path):
        body = self._resource().get_issue_body_from_build(mock_build_metadata())
        assert "has completed build number" in body

    def test_single_body_file_still_raises_when_missing(self, tmp_path):
        """`body_file` is the whole body, not an optional fragment.

        Tolerating a missing one would turn a typo'd path into a published gate
        issue containing nothing but a warning. Only `body_files` entries are
        allowed to be absent.
        """
        with pytest.raises(FileNotFoundError):
            self._resource().get_issue_body_from_build(
                mock_build_metadata(), body_file="typo.md", sources_dir=tmp_path
            )


class TestCloseIfFile:
    """A preview with nothing to review must not leave a gate for a human.

    The gate issue is still created/updated exactly as normal -- a downstream
    `-gate-trigger` resource only ever advances on a *closed* issue, so
    skipping creation entirely would leave that stage (and everything chained
    after it) waiting forever. `close_if_file` closes it immediately instead.
    """

    def test_creates_open_then_closes_when_marker_present_and_no_existing_issue(
        self, mock_github, tmp_path
    ):
        mock_gh_instance, mock_repo = mock_github
        mock_gh_instance.search_issues.return_value = []
        (tmp_path / "preview.md.no-changes").touch()
        created = create_mock_issue(
            number=12,
            title="[bot] Pulumi proj QA ready to deploy.",
            state="open",
            created_at=NOW,
        )
        mock_repo.create_issue.return_value = created

        resource = ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            issue_state="open",
            issue_title_template="[bot] Pulumi proj QA ready to deploy.",
        )

        resource.publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            close_if_file="preview.md.no-changes",
        )

        mock_repo.create_issue.assert_called_once()
        created.edit.assert_called_once_with(state="closed")

    def test_creates_and_leaves_open_when_marker_absent(self, mock_github, tmp_path):
        mock_gh_instance, mock_repo = mock_github
        mock_gh_instance.search_issues.return_value = []
        created = create_mock_issue(
            number=12,
            title="[bot] Pulumi proj QA ready to deploy.",
            state="open",
            created_at=NOW,
        )
        mock_repo.create_issue.return_value = created

        resource = ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            issue_state="open",
            issue_title_template="[bot] Pulumi proj QA ready to deploy.",
        )

        resource.publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            close_if_file="preview.md.no-changes",
        )

        mock_repo.create_issue.assert_called_once()
        created.edit.assert_not_called()

    def test_closes_an_already_open_gate_when_marker_present(
        self, mock_github, tmp_path
    ):
        """A stale open gate must not be left showing a diff that no longer applies."""
        mock_gh_instance, _ = mock_github
        (tmp_path / "preview.md.no-changes").touch()
        issue = create_mock_issue(
            number=9,
            title="[bot] Pulumi proj QA ready to deploy.",
            state="open",
            created_at=T_MINUS_1,
        )
        issue.body = ""
        mock_gh_instance.search_issues.return_value = [issue]

        resource = ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            issue_state="open",
            issue_title_template="[bot] Pulumi proj QA ready to deploy.",
            issue_body_template="no changes",
            update_in_place=True,
        )
        resource.publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            close_if_file="preview.md.no-changes",
        )

        assert call(state="closed") in issue.edit.call_args_list

    def test_missing_marker_file_leaves_the_gate_open(self, mock_github, tmp_path):
        mock_gh_instance, mock_repo = mock_github
        mock_gh_instance.search_issues.return_value = []
        created = create_mock_issue(
            number=13,
            title="[bot] Pulumi proj QA ready to deploy.",
            state="open",
            created_at=NOW,
        )
        mock_repo.create_issue.return_value = created

        resource = ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            issue_state="open",
            issue_title_template="[bot] Pulumi proj QA ready to deploy.",
        )

        resource.publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            close_if_file="never-written.no-changes",
        )

        mock_repo.create_issue.assert_called_once()
        created.edit.assert_not_called()


class TestSkipIfFile:
    """Nothing to approve must mean no issue at all, not a pre-approved one.

    For a release gate, closing the issue ships a release, so `close_if_file`'s
    auto-close would ship one nobody approved. `skip_if_file` posts nothing.
    """

    TITLE = "Release my-app 2026.9.22.1"

    def _resource(self) -> ConcourseGithubIssuesResource:
        return ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            issue_state="open",
            issue_prefix="Release my-app",
            issue_title_template=self.TITLE,
            update_in_place=True,
        )

    def test_marker_present_touches_nothing_on_github(self, mock_github, tmp_path):
        mock_gh_instance, mock_repo = mock_github
        (tmp_path / "nothing-to-approve").touch()

        version, metadata = self._resource().publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            body_file="checklist.md",  # absent: a skipped put must not read it
            skip_if_file="nothing-to-approve",
        )

        assert version == SKIPPED_VERSION
        assert metadata == {"skipped_by": "nothing-to-approve"}
        mock_gh_instance.search_issues.assert_not_called()
        mock_repo.create_issue.assert_not_called()

    def test_marker_wins_over_close_if_file(self, mock_github, tmp_path):
        mock_gh_instance, mock_repo = mock_github
        (tmp_path / "nothing-to-approve").touch()
        (tmp_path / "preview.md.no-changes").touch()

        version, _ = self._resource().publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            close_if_file="preview.md.no-changes",
            skip_if_file="nothing-to-approve",
        )

        assert version == SKIPPED_VERSION
        mock_gh_instance.search_issues.assert_not_called()
        mock_repo.create_issue.assert_not_called()

    def test_marker_absent_posts_as_normal(self, mock_github, tmp_path):
        mock_gh_instance, mock_repo = mock_github
        mock_gh_instance.search_issues.return_value = []
        mock_repo.create_issue.return_value = create_mock_issue(
            number=12, title=self.TITLE, state="open", created_at=NOW
        )

        version, _ = self._resource().publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            skip_if_file="nothing-to-approve",
        )

        mock_repo.create_issue.assert_called_once()
        assert version.issue_number == 12

    def test_skipped_version_is_constant(self, mock_github, tmp_path):
        """Two skipped puts emit the same version, so the second is not new."""
        (tmp_path / "nothing-to-approve").touch()
        versions = [
            self._resource()
            .publish_new_version(
                sources_dir=tmp_path,
                build_metadata=mock_build_metadata(BUILD_NAME=str(build)),
                skip_if_file="nothing-to-approve",
            )[0]
            .to_flat_dict()
            for build in (1, 2)
        ]
        assert versions[0] == versions[1]

    def test_check_handed_the_skipped_version_reseeds_from_the_latest(
        self, mock_github
    ):
        """It must not become a 1970 `since` filter that replays all history."""
        _, mock_repo = mock_github
        mock_repo.get_issues.return_value = [
            create_mock_issue(number=7, title=self.TITLE, state="open", created_at=NOW),
            create_mock_issue(
                number=3, title=self.TITLE, state="open", created_at=T_MINUS_1
            ),
        ]
        # Concourse hands check the version JSON back, all strings.
        previous = ConcourseGithubIssuesVersion(**SKIPPED_VERSION.to_flat_dict())

        versions = SimpleTestResourceWrapper(self._resource()).fetch_new_versions(
            previous
        )

        assert {v.issue_number for v in versions} == {7}
        assert mock_repo.get_issues.call_args.kwargs["since"] is NotSet

    def test_implicit_get_of_the_skipped_version_does_not_tombstone(
        self, mock_github, tmp_path
    ):
        """The put's implicit get must not rename (consume) any real issue."""
        _, mock_repo = mock_github
        dest = tmp_path / "get"
        dest.mkdir()

        self._resource().download_version(
            SKIPPED_VERSION, str(dest), mock_build_metadata()
        )

        mock_repo.get_issue.assert_not_called()
        assert json.loads((dest / "gh_issue.json").read_text())["issue_number"] == "0"


class TestCloseWithLabels:
    """Abandoning a release must neutralise its gate issue, never create one."""

    TITLE = "Release my-app 2026.9.22.1"

    def _resource(self) -> ConcourseGithubIssuesResource:
        return ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            issue_state="open",
            issue_prefix="Release my-app",
            issue_title_template="Release my-app",
        )

    def _publish(self, tmp_path, **kwargs):
        return self._resource().publish_new_version(
            sources_dir=tmp_path,
            build_metadata=mock_build_metadata(),
            title_template=self.TITLE,
            close_with_labels=["abandoned"],
            **kwargs,
        )

    def _issue(self, number, title=None, state="open", calls=None):
        """Build an issue whose add_to_labels really adds, so labels are observable."""
        issue = create_mock_issue(
            number=number,
            title=title or self.TITLE,
            state=state,
            created_at=NOW,
            closed_at=NOW if state == "closed" else None,
        )

        def add_to_labels(*names):
            for name in names:
                label = MagicMock()
                label.name = name
                issue.labels.append(label)
            if calls is not None:
                calls.append(("label", number))

        def edit(**kwargs):
            if calls is not None:
                calls.append(("edit", number, kwargs))

        issue.add_to_labels.side_effect = add_to_labels
        issue.edit.side_effect = edit
        return issue

    def _found(self, mock_github, *issues):
        mock_gh_instance, mock_repo = mock_github
        mock_gh_instance.search_issues.return_value = list(issues)
        mock_repo.get_issue.side_effect = lambda number: next(
            issue for issue in issues if issue.number == number
        )

    def test_labels_then_closes_the_exact_match(self, mock_github, tmp_path):
        _, mock_repo = mock_github
        calls = []
        issue = self._issue(31, calls=calls)
        self._found(mock_github, issue)

        version, metadata = self._publish(tmp_path)

        issue.add_to_labels.assert_called_once_with("abandoned")
        issue.create_comment.assert_called_once()
        assert "abandoned" in issue.create_comment.call_args.args[0]
        # The gate polls for closed issues and skips by label, so the label
        # has to be on before the close is.
        assert calls == [("label", 31), ("edit", 31, {"state": "closed"})]
        assert version == SKIPPED_VERSION
        assert metadata == {"retired_issues": "#31"}
        mock_repo.create_issue.assert_not_called()

    def test_labels_an_issue_closed_before_the_build_ran(self, mock_github, tmp_path):
        """A reviewer closed it first; a gate yet to poll would still fire on it."""
        issue = self._issue(34, state="closed")
        self._found(mock_github, issue)

        _, metadata = self._publish(tmp_path)

        issue.add_to_labels.assert_called_once_with("abandoned")
        issue.edit.assert_not_called()
        issue.create_comment.assert_not_called()
        assert metadata == {"retired_issues": "#34"}

    def test_restores_a_label_a_concurrent_put_removed(self, mock_github, tmp_path):
        """update_in_place relabels to its own list, which can drop ours."""
        calls = []
        issue = self._issue(35, calls=calls)
        plain_edit = issue.edit.side_effect

        def edit_then_lose_label(**kwargs):
            plain_edit(**kwargs)
            issue.labels.clear()  # the racing QA put's labels=["release"]

        issue.edit.side_effect = edit_then_lose_label
        self._found(mock_github, issue)

        self._publish(tmp_path)

        assert calls == [
            ("label", 35),
            ("edit", 35, {"state": "closed"}),
            ("label", 35),
        ]
        assert {label.name for label in issue.labels} == {"abandoned"}

    def test_does_not_relabel_when_the_label_survived(self, mock_github, tmp_path):
        issue = self._issue(36)
        self._found(mock_github, issue)

        self._publish(tmp_path)

        issue.add_to_labels.assert_called_once_with("abandoned")

    def test_leaves_a_neighbouring_title_alone(self, mock_github, tmp_path):
        """Search is token based; only the exact title may be closed."""
        neighbour = self._issue(32, title=self.TITLE + ".1")
        infra = self._issue(33, title="Release my-app infrastructure @ 2026.9.22.1")
        consumed = self._issue(37, title="[CONSUMED #9]Release my-app", state="closed")
        self._found(mock_github, neighbour, infra, consumed)

        version, metadata = self._publish(tmp_path)

        for issue in (neighbour, infra, consumed):
            issue.add_to_labels.assert_not_called()
            issue.edit.assert_not_called()
        assert version == SKIPPED_VERSION
        assert metadata == {"retired_issues": "none"}

    def test_no_issue_is_not_an_error_and_creates_nothing(self, mock_github, tmp_path):
        _, mock_repo = mock_github
        self._found(mock_github)

        version, metadata = self._publish(tmp_path)

        assert version == SKIPPED_VERSION
        assert metadata == {"retired_issues": "none"}
        mock_repo.create_issue.assert_not_called()

    def test_retires_every_duplicate(self, mock_github, tmp_path):
        """Title is the only key, so a duplicate would still ship the release."""
        issues = [self._issue(40), self._issue(41)]
        self._found(mock_github, *issues)

        _, metadata = self._publish(tmp_path)

        for issue in issues:
            issue.edit.assert_called_once_with(state="closed")
        assert metadata == {"retired_issues": "#40, #41"}

    def test_searches_both_states_in_this_repo_by_exact_title(
        self, mock_github, tmp_path
    ):
        mock_gh_instance, mock_repo = mock_github
        mock_repo.full_name = "test/repo"
        self._found(mock_github)

        self._publish(tmp_path)

        query = mock_gh_instance.search_issues.call_args.args[0]
        assert "repo:test/repo" in query
        assert "state:" not in query
        assert f'"{self.TITLE}"' in query

    def test_empty_label_list_is_rejected_not_treated_as_absent(
        self, mock_github, tmp_path
    ):
        """Falling through would create an issue, the opposite of a retirement."""
        mock_gh_instance, mock_repo = mock_github
        mock_gh_instance.search_issues.return_value = []

        with pytest.raises(ValueError, match="at least one label"):
            self._resource().publish_new_version(
                sources_dir=tmp_path,
                build_metadata=mock_build_metadata(),
                title_template=self.TITLE,
                close_with_labels=[],
            )

        mock_repo.create_issue.assert_not_called()

    def test_skip_if_file_wins(self, mock_github, tmp_path):
        mock_gh_instance, _ = mock_github
        (tmp_path / "nothing-to-approve").touch()

        _, metadata = self._publish(tmp_path, skip_if_file="nothing-to-approve")

        assert metadata == {"skipped_by": "nothing-to-approve"}
        mock_gh_instance.search_issues.assert_not_called()

    def test_implicit_get_of_the_returned_version_does_not_tombstone(
        self, mock_github, tmp_path
    ):
        _, mock_repo = mock_github
        self._found(mock_github)
        dest = tmp_path / "get"
        dest.mkdir()
        version, _ = self._publish(tmp_path)

        self._resource().download_version(version, str(dest), mock_build_metadata())

        mock_repo.get_issue.assert_not_called()


class TestGetRefusesSkipLabeledIssue:
    """A version `check` already discovered can be abandoned before it is fetched."""

    def _resource(self, skip_if_labeled=("abandoned",)):
        return ConcourseGithubIssuesResource(
            repository="test/repo",
            access_token="dummy_token",
            issue_state="closed",
            issue_prefix="Release my-app",
            issue_title_template="Release my-app",
            skip_if_labeled=list(skip_if_labeled),
        )

    def _version(self, number=7):
        return ConcourseGithubIssuesVersion(
            issue_created_at="2026-10-01T00:00:00",
            issue_closed_at="2026-10-02T00:00:00",
            issue_number=number,
            issue_state="closed",
            issue_title="Release my-app 2026.10.1.1",
            issue_url="http://example.com/issue",
        )

    def test_labelled_after_discovery_fails_and_changes_nothing(
        self, mock_github, tmp_path
    ):
        _, mock_repo = mock_github
        issue = create_mock_issue(
            number=7,
            title="Release my-app 2026.10.1.1",
            state="closed",
            created_at=NOW,
            closed_at=NOW,
            labels=["release", "abandoned"],
        )
        mock_repo.get_issue.return_value = issue

        with pytest.raises(RuntimeError, match="abandoned"):
            self._resource().download_version(
                self._version(), str(tmp_path), mock_build_metadata()
            )

        assert not (tmp_path / "gh_issue.json").exists()
        issue.edit.assert_not_called()

    def test_unlabelled_issue_is_fetched_and_tombstoned(self, mock_github, tmp_path):
        _, mock_repo = mock_github
        issue = create_mock_issue(
            number=7,
            title="Release my-app 2026.10.1.1",
            state="closed",
            created_at=NOW,
            closed_at=NOW,
            labels=["release"],
        )
        mock_repo.get_issue.return_value = issue

        self._resource().download_version(
            self._version(), str(tmp_path), mock_build_metadata()
        )

        assert (tmp_path / "gh_issue.json").exists()
        issue.edit.assert_called_once()

    def test_no_extra_api_call_without_skip_if_labeled(self, mock_github, tmp_path):
        """Resources that configure no skip labels (the open-issue writers)."""
        _, mock_repo = mock_github
        mock_repo.get_issue.return_value = create_mock_issue(
            number=7,
            title="Release my-app 2026.10.1.1",
            state="closed",
            created_at=NOW,
            closed_at=NOW,
        )

        self._resource(skip_if_labeled=()).download_version(
            self._version(), str(tmp_path), mock_build_metadata()
        )

        mock_repo.get_issue.assert_called_once_with(7)  # the tombstone's, only

    def test_skipped_version_is_never_refused(self, mock_github, tmp_path):
        _, mock_repo = mock_github

        self._resource().download_version(
            SKIPPED_VERSION, str(tmp_path), mock_build_metadata()
        )

        mock_repo.get_issue.assert_not_called()


def _update_in_place_resource() -> ConcourseGithubIssuesResource:
    return ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template="[bot] Pulumi proj QA ready to deploy.",
        issue_body_template="diff",
        update_in_place=True,
    )


def _publish_onto_existing(mock_github, existing_labels, desired_labels):
    mock_gh_instance, _ = mock_github
    issue = create_mock_issue(
        number=9,
        title="[bot] Pulumi proj QA ready to deploy.",
        state="open",
        created_at=T_MINUS_1,
        labels=existing_labels,
    )
    issue.body = ""
    issue.edit = MagicMock()
    mock_gh_instance.search_issues.return_value = [issue]
    _update_in_place_resource().publish_new_version(
        sources_dir="dummy",
        build_metadata=mock_build_metadata(
            pipeline_name="p", job_name="j", build_name="b1"
        ),
        labels=desired_labels,
    )
    return issue


def test_update_in_place_reconciles_stale_labels(mock_github):
    """An issue outlives a change to its labels.

    A gate opened before the label semantics were corrected kept
    `promotion-to-production` on a QA gate for the issue's whole life, because
    labels were applied at creation only -- and a label is what routing and
    queries actually read.
    """
    issue = _publish_onto_existing(
        mock_github,
        existing_labels=["DevOps", "promotion-to-production"],
        desired_labels=["DevOps", "promotion-to-qa"],
    )
    relabels = [c for c in issue.edit.call_args_list if "labels" in c.kwargs]
    assert relabels, "the stale label was never corrected"
    assert relabels[0].kwargs["labels"] == ["DevOps", "promotion-to-qa"]


def test_update_in_place_does_not_rewrite_labels_that_already_match(mock_github):
    """Every re-preview edits the body; it must not also write labels each time."""
    issue = _publish_onto_existing(
        mock_github,
        existing_labels=["DevOps", "promotion-to-qa"],
        desired_labels=["promotion-to-qa", "DevOps"],  # same set, different order
    )
    assert not [c for c in issue.edit.call_args_list if "labels" in c.kwargs]


def test_update_in_place_still_edits_the_body(mock_github):
    issue = _publish_onto_existing(
        mock_github, existing_labels=[], desired_labels=["DevOps"]
    )
    assert [c for c in issue.edit.call_args_list if "body" in c.kwargs]


def test_update_in_place_leaves_assignees_alone(mock_github):
    """Someone assigning themselves to review a gate is a human workflow."""
    issue = _publish_onto_existing(
        mock_github, existing_labels=[], desired_labels=["DevOps"]
    )
    assert not [c for c in issue.edit.call_args_list if "assignees" in c.kwargs]


def test_comment_path_does_not_touch_labels(mock_github):
    """Without update_in_place the resource does not own the issue."""
    mock_gh_instance, _ = mock_github
    issue = create_mock_issue(
        number=9,
        title="[bot] Pulumi proj QA ready to deploy.",
        state="open",
        created_at=T_MINUS_1,
        labels=["promotion-to-production"],
    )
    issue.body = ""
    issue.edit = MagicMock()
    issue.create_comment = MagicMock()
    mock_gh_instance.search_issues.return_value = [issue]
    ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template="[bot] Pulumi proj QA ready to deploy.",
        issue_body_template="diff",
    ).publish_new_version(
        sources_dir="dummy",
        build_metadata=mock_build_metadata(
            pipeline_name="p", job_name="j", build_name="b1"
        ),
        labels=["promotion-to-qa"],
    )
    issue.edit.assert_not_called()
    issue.create_comment.assert_called_once()
