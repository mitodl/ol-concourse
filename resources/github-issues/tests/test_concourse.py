from github.GithubObject import NotSet
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from concourse import (
    ConcourseGithubIssuesResource,
    ConcourseGithubIssuesVersion,
    ISO_8601_FORMAT,
)
from concoursetools import BuildMetadata  # Import the actual class
from concoursetools.testing import SimpleTestResourceWrapper
from github.Issue import Issue


# Helper function to create mock BuildMetadata objects
def mock_build_metadata(**kwargs) -> BuildMetadata:
    """Creates a BuildMetadata object with default values, allowing overrides."""
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
NOW = datetime.now()
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

MOCK_ISSUES = [create_mock_issue(**data) for data in MOCK_ISSUES_DATA]  # type: ignore [arg-type]


@pytest.fixture
def mock_github():
    """Fixture to mock the Github API client and repository."""
    with patch("concourse.Github") as MockGithub:
        mock_gh_instance = MockGithub.return_value
        mock_repo = MagicMock()
        mock_repo.full_name = (
            "test/repo"
        )  # Set the full_name attribute for search queries
        mock_gh_instance.get_repo.return_value = mock_repo
        # Set a default rate limit mock to avoid errors
        mock_rate_limit = MagicMock()
        mock_rate_limit.core.remaining = 5000
        mock_gh_instance.get_rate_limit.return_value = mock_rate_limit
        yield mock_gh_instance, mock_repo


@pytest.mark.parametrize(
    "config_state, config_prefix, expected_issue_numbers",
    [
        ("closed", "[bot]", {1}),  # Only closed issues with prefix
        ("closed", None, {1, 2}),  # All closed issues
        ("open", "[bot]", {3}),  # Only open issues with prefix
        ("open", None, {3, 4}),  # All open issues
    ],
)
def test_fetch_new_versions_no_previous(
    mock_github, config_state, config_prefix, expected_issue_numbers
):
    """Test fetching versions without a previous version."""
    mock_gh_instance, mock_repo = mock_github

    # Filter mock issues based on the expected state for the API call
    api_call_issues = [issue for issue in MOCK_ISSUES if issue.state == config_state]
    mock_repo.get_issues.return_value = api_call_issues

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state=config_state,
        issue_prefix=config_prefix,
    )
    wrapper = SimpleTestResourceWrapper(resource)

    # We pass None as previous_version to fetch_new_versions
    versions = wrapper.fetch_new_versions(None)
    version_numbers = {v.issue_number for v in versions}

    assert version_numbers == expected_issue_numbers
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
    parsed_closed_at = datetime.strptime(
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
    parsed_created_at = datetime.strptime(
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
    parsed_closed_at = datetime.strptime(
        previous_version.issue_closed_at,  # type: ignore [arg-type]
        ISO_8601_FORMAT,
    )
    expected_since = parsed_closed_at + timedelta(seconds=1)

    # Mock API returns issue #2 (closed, no prefix) and potentially others
    # if they existed and matched the 'since' criteria.
    # In our MOCK_ISSUES, only #2 is closed after #1.
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
    """Test that limit_old_versions restricts the number of issues returned."""
    mock_gh_instance, mock_repo = mock_github

    # Create more mock issues to test limiting
    more_mock_issues = [
        create_mock_issue(
            number=i,
            title=f"[bot] Old Issue {i}",
            state="closed",
            created_at=NOW - timedelta(days=10 + i),
            closed_at=NOW - timedelta(days=5 + i),
        )
        for i in range(5, 10)  # Issues 5, 6, 7, 8, 9
    ] + MOCK_ISSUES  # Add existing mocks

    # Sort by closed_at ascending for predictable API return order (PyGithub usually sorts descending)
    # But our internal logic sorts by number ascending after filtering.
    api_call_issues = sorted(
        [issue for issue in more_mock_issues if issue.state == "closed"],
        key=lambda i: i.closed_at,
        reverse=True,  # Simulate typical GitHub API order (newest first)
    )
    mock_repo.get_issues.return_value = api_call_issues

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="closed",
        issue_prefix="[bot]",
        limit_old_versions=2,  # Limit to 2 oldest matching issues
    )
    wrapper = SimpleTestResourceWrapper(resource)

    versions = wrapper.fetch_new_versions(None)  # No previous version
    version_numbers = {v.issue_number for v in versions}

    # Expected: Issues 1, 5, 6, 7, 8, 9 match state and prefix.
    # get_matching_issues sorts by number ascending: 1, 5, 6, 7, 8, 9
    # limit_old_versions=2 takes the first 2: 1, 5
    assert version_numbers == {1, 5}
    mock_repo.get_issues.assert_called_once_with(
        state="closed", labels=[], since=NotSet
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
    # wrapper = SimpleTestResourceWrapper(resource) # Wrapper not needed for download test

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
    # expected_file_path = Path(dest_dir) / "gh_issue.json" # This path wasn't used, just verify open call
    mock_open.assert_called_once_with("w")
    # Check that the file handle's write method was called (actual content check is tricky with mock_open)
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
        issue_title_template="[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed",
        issue_body_template="Build {BUILD_NAME} finished.",
        assignees=["user1"],
        labels=["bot-created"],
    )
    # wrapper = SimpleTestResourceWrapper(resource) # Wrapper not needed for publish tests
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


def test_publish_new_version_comments_on_existing(mock_github):
    """Test publish comments on an existing issue if found."""
    mock_gh_instance, mock_repo = mock_github
    existing_mock_issue = create_mock_issue(
        number=9,
        title="[bot] Pipeline my-pipeline task my-job completed",
        state="open",
        created_at=T_MINUS_1,
    )
    # Mock the create_comment method on the existing issue
    existing_mock_issue.create_comment = MagicMock()
    mock_gh_instance.search_issues.return_value = [
        existing_mock_issue
    ]  # Found existing

    resource = ConcourseGithubIssuesResource(
        repository="test/repo",
        access_token="dummy_token",
        issue_state="open",
        issue_title_template="[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed",
        issue_body_template="Build {BUILD_NAME} finished.",
    )
    # wrapper = SimpleTestResourceWrapper(resource) # Wrapper not needed for publish tests
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

    # Check create_comment was called on the existing issue
    expected_comment_body = "Build b456 finished."
    existing_mock_issue.create_comment.assert_called_once_with(expected_comment_body)

    # Check returned version matches the existing issue
    assert version.issue_number == 9
    assert version.issue_title == expected_title
    assert version.issue_state == "open"
    assert metadata == {}
