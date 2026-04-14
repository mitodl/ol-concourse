"""Tests for the GitHub Deployments Concourse resource."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from concourse import (
    VALID_FINISH_STATES,
    ConcourseGithubDeploymentsResource,
    GithubDeploymentVersion,
    ISO_8601_FORMAT,
)
from concoursetools import BuildMetadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2025, 4, 14, 12, 0, 0)
EARLIER = datetime(2025, 4, 13, 10, 0, 0)
EARLIEST = datetime(2025, 4, 12, 8, 0, 0)


def mock_build_metadata(**kwargs) -> BuildMetadata:
    defaults = {
        "BUILD_ID": "99",
        "BUILD_NAME": "1",
        "BUILD_JOB_NAME": "deploy-rc",
        "BUILD_PIPELINE_NAME": "mit-learn",
        "BUILD_PIPELINE_INSTANCE_VARS": "{}",
        "BUILD_TEAM_NAME": "main",
        "ATC_EXTERNAL_URL": "https://concourse.example.com",
    }
    defaults.update(kwargs)
    return BuildMetadata(**defaults)


def make_mock_deployment(
    dep_id: int,
    sha: str = "abc1234",
    ref: str = "release/2025.04.14.1",
    environment: str = "RC",
    description: str = "RC deploy",
    created_at: datetime = NOW,
    statuses: list | None = None,
) -> MagicMock:
    dep = MagicMock()
    dep.id = dep_id
    dep.sha = sha
    dep.ref = ref
    dep.environment = environment
    dep.description = description
    dep.created_at = created_at
    dep.get_statuses.return_value = statuses or []
    return dep


def make_mock_status(
    status_id: int,
    state: str,
    environment_url: str = "",
    log_url: str = "",
    description: str = "",
) -> MagicMock:
    s = MagicMock()
    s.id = status_id
    s.state = state
    s.environment_url = environment_url
    s.log_url = log_url
    s.description = description
    return s


@pytest.fixture
def mock_github():
    with patch("concourse.Github") as MockGithub:
        mock_gh = MockGithub.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        yield mock_gh, mock_repo


def make_resource(
    mock_github, environment: str = "RC"
) -> ConcourseGithubDeploymentsResource:
    return ConcourseGithubDeploymentsResource(
        repository="mitodl/my-app",
        environment=environment,
        access_token="dummy",
    )


# ---------------------------------------------------------------------------
# Version tests
# ---------------------------------------------------------------------------


def test_version_identity_is_stable():
    """Two versions with the same deployment_id are equal regardless of other fields."""
    v1 = GithubDeploymentVersion("42", "RC", "abc1234", "2025-04-14T12:00:00")
    v2 = GithubDeploymentVersion("42", "RC", "abc1234", "2025-04-14T12:00:00")
    assert v1 == v2
    assert hash(v1) == hash(v2)


def test_version_different_ids_are_unequal():
    v1 = GithubDeploymentVersion("41", "RC", "abc1234", "2025-04-14T12:00:00")
    v2 = GithubDeploymentVersion("42", "RC", "abc1234", "2025-04-14T12:00:00")
    assert v1 != v2
    assert v1 < v2


# ---------------------------------------------------------------------------
# check (fetch_new_versions) tests
# ---------------------------------------------------------------------------


def test_fetch_no_previous_returns_latest(mock_github):
    _, mock_repo = mock_github
    deps = [
        make_mock_deployment(100, created_at=NOW),
        make_mock_deployment(99, created_at=EARLIER),
    ]
    mock_repo.get_deployments.return_value = iter(deps)

    resource = make_resource(mock_github)
    versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    assert next(iter(versions)).deployment_id == "100"


def test_fetch_no_previous_empty_returns_empty_set(mock_github):
    _, mock_repo = mock_github
    mock_repo.get_deployments.return_value = iter([])

    resource = make_resource(mock_github)
    versions = resource.fetch_new_versions(None)

    assert versions == set()


def test_fetch_with_previous_returns_newer(mock_github):
    _, mock_repo = mock_github
    deps = [
        make_mock_deployment(102, created_at=NOW),
        make_mock_deployment(101, created_at=EARLIER),
        make_mock_deployment(100, created_at=EARLIEST),  # == previous
    ]
    mock_repo.get_deployments.return_value = iter(deps)

    previous = GithubDeploymentVersion(
        "100", "RC", "abc", NOW.strftime(ISO_8601_FORMAT)
    )
    resource = make_resource(mock_github)
    versions = resource.fetch_new_versions(previous)

    ids = {v.deployment_id for v in versions}
    assert ids == {"101", "102"}


def test_fetch_with_previous_nothing_newer_returns_empty(mock_github):
    _, mock_repo = mock_github
    deps = [make_mock_deployment(100, created_at=NOW)]
    mock_repo.get_deployments.return_value = iter(deps)

    previous = GithubDeploymentVersion(
        "100", "RC", "abc", NOW.strftime(ISO_8601_FORMAT)
    )
    resource = make_resource(mock_github)
    versions = resource.fetch_new_versions(previous)

    assert versions == set()


def test_fetch_passes_environment_to_api(mock_github):
    _, mock_repo = mock_github
    mock_repo.get_deployments.return_value = iter([])

    resource = make_resource(mock_github, environment="Production")
    resource.fetch_new_versions(None)

    mock_repo.get_deployments.assert_called_once_with(environment="Production")


# ---------------------------------------------------------------------------
# in (download_version) tests
# ---------------------------------------------------------------------------


def test_download_version_writes_deployment_json(mock_github, tmp_path):
    _, mock_repo = mock_github
    status = make_mock_status(1, "in_progress", environment_url="https://rc.example.com")
    dep = make_mock_deployment(42, statuses=[status])
    mock_repo.get_deployment.return_value = dep

    resource = make_resource(mock_github)
    version = GithubDeploymentVersion(
        "42", "RC", dep.sha, NOW.strftime(ISO_8601_FORMAT)
    )
    returned_version, metadata = resource.download_version(
        version, str(tmp_path), mock_build_metadata()
    )

    assert returned_version == version
    assert metadata == {}

    data = json.loads((tmp_path / "deployment.json").read_text())
    assert data["deployment_id"] == "42"
    assert data["sha"] == dep.sha
    assert data["ref"] == dep.ref
    assert data["environment"] == dep.environment
    assert data["state"] == "in_progress"
    assert data["environment_url"] == "https://rc.example.com"


def test_download_version_no_statuses(mock_github, tmp_path):
    _, mock_repo = mock_github
    dep = make_mock_deployment(10, statuses=[])
    mock_repo.get_deployment.return_value = dep

    resource = make_resource(mock_github)
    version = GithubDeploymentVersion(
        "10", "RC", dep.sha, NOW.strftime(ISO_8601_FORMAT)
    )
    resource.download_version(version, str(tmp_path), mock_build_metadata())

    data = json.loads((tmp_path / "deployment.json").read_text())
    assert data["state"] == "pending"
    assert data["environment_url"] == ""
    assert data["log_url"] == ""


def test_download_version_picks_latest_status_by_id(mock_github, tmp_path):
    _, mock_repo = mock_github
    # Statuses returned in arbitrary order; latest by id should win
    statuses = [
        make_mock_status(3, "success"),
        make_mock_status(1, "pending"),
        make_mock_status(2, "in_progress"),
    ]
    dep = make_mock_deployment(55, statuses=statuses)
    mock_repo.get_deployment.return_value = dep

    resource = make_resource(mock_github)
    version = GithubDeploymentVersion(
        "55", "RC", dep.sha, NOW.strftime(ISO_8601_FORMAT)
    )
    resource.download_version(version, str(tmp_path), mock_build_metadata())

    data = json.loads((tmp_path / "deployment.json").read_text())
    assert data["state"] == "success"


# ---------------------------------------------------------------------------
# out (publish_new_version) - action=start tests
# ---------------------------------------------------------------------------


def test_publish_start_creates_deployment_and_status(mock_github):
    _, mock_repo = mock_github
    dep = make_mock_deployment(200)
    mock_repo.create_deployment.return_value = dep

    resource = make_resource(mock_github)
    version, metadata = resource.publish_new_version(
        sources_dir="/workspace",
        build_metadata=mock_build_metadata(),
        action="start",
        ref="release/2025.04.14.1",
        description="RC deploy",
        environment_url="https://rc.example.com",
    )

    assert version.deployment_id == "200"
    assert metadata == {}

    mock_repo.create_deployment.assert_called_once()
    call_kwargs = mock_repo.create_deployment.call_args.kwargs
    assert call_kwargs["ref"] == "release/2025.04.14.1"
    assert call_kwargs["environment"] == "RC"

    dep.create_status.assert_called_once()
    status_kwargs = dep.create_status.call_args.kwargs
    assert status_kwargs["state"] == "in_progress"
    assert status_kwargs["environment"] == "RC"
    assert status_kwargs["environment_url"] == "https://rc.example.com"


def test_publish_start_requires_ref(mock_github):
    resource = make_resource(mock_github)
    with pytest.raises(ValueError, match="ref is required"):
        resource.publish_new_version(
            sources_dir="/workspace",
            build_metadata=mock_build_metadata(),
            action="start",
        )


def test_publish_start_omits_required_contexts_when_not_given(mock_github):
    """required_contexts should not default to [] which bypasses all status checks."""
    from github.GithubObject import NotSet

    _, mock_repo = mock_github
    mock_repo.create_deployment.return_value = make_mock_deployment(300)

    resource = make_resource(mock_github)
    resource.publish_new_version(
        sources_dir="/workspace",
        build_metadata=mock_build_metadata(),
        action="start",
        ref="main",
    )

    call_kwargs = mock_repo.create_deployment.call_args.kwargs
    assert call_kwargs["required_contexts"] is NotSet


def test_publish_start_passes_required_contexts_when_given(mock_github):
    _, mock_repo = mock_github
    mock_repo.create_deployment.return_value = make_mock_deployment(301)

    resource = make_resource(mock_github)
    resource.publish_new_version(
        sources_dir="/workspace",
        build_metadata=mock_build_metadata(),
        action="start",
        ref="main",
        required_contexts=["ci/tests"],
    )

    call_kwargs = mock_repo.create_deployment.call_args.kwargs
    assert call_kwargs["required_contexts"] == ["ci/tests"]


# ---------------------------------------------------------------------------
# out (publish_new_version) - action=finish tests
# ---------------------------------------------------------------------------


def test_publish_finish_updates_status(mock_github, tmp_path):
    _, mock_repo = mock_github
    dep = make_mock_deployment(200)
    mock_repo.get_deployment.return_value = dep

    dep_file = tmp_path / "started" / "deployment.json"
    dep_file.parent.mkdir()
    dep_file.write_text(json.dumps({"deployment_id": "200"}))

    resource = make_resource(mock_github)
    version, metadata = resource.publish_new_version(
        sources_dir=str(tmp_path),
        build_metadata=mock_build_metadata(),
        action="finish",
        deployment_file="started/deployment.json",
        state="success",
        environment_url="https://rc.example.com",
    )

    assert version.deployment_id == "200"
    assert metadata == {}

    mock_repo.get_deployment.assert_called_once_with(200)
    dep.create_status.assert_called_once()
    call_kwargs = dep.create_status.call_args.kwargs
    assert call_kwargs["state"] == "success"
    assert call_kwargs["environment_url"] == "https://rc.example.com"


def test_publish_finish_requires_deployment_file(mock_github):
    resource = make_resource(mock_github)
    with pytest.raises(ValueError, match="deployment_file is required"):
        resource.publish_new_version(
            sources_dir="/workspace",
            build_metadata=mock_build_metadata(),
            action="finish",
            state="success",
        )


def test_publish_finish_requires_state(mock_github, tmp_path):
    dep_file = tmp_path / "deployment.json"
    dep_file.write_text(json.dumps({"deployment_id": "1"}))

    resource = make_resource(mock_github)
    with pytest.raises(ValueError, match="state is required"):
        resource.publish_new_version(
            sources_dir=str(tmp_path),
            build_metadata=mock_build_metadata(),
            action="finish",
            deployment_file="deployment.json",
        )


@pytest.mark.parametrize("bad_state", ["in_progress", "queued", "pending", "unknown"])
def test_publish_finish_rejects_non_terminal_states(mock_github, tmp_path, bad_state):
    dep_file = tmp_path / "deployment.json"
    dep_file.write_text(json.dumps({"deployment_id": "1"}))

    resource = make_resource(mock_github)
    with pytest.raises(ValueError, match="state must be one of"):
        resource.publish_new_version(
            sources_dir=str(tmp_path),
            build_metadata=mock_build_metadata(),
            action="finish",
            deployment_file="deployment.json",
            state=bad_state,
        )


@pytest.mark.parametrize("valid_state", sorted(VALID_FINISH_STATES))
def test_publish_finish_accepts_all_terminal_states(mock_github, tmp_path, valid_state):
    _, mock_repo = mock_github
    dep = make_mock_deployment(99)
    mock_repo.get_deployment.return_value = dep

    dep_file = tmp_path / "deployment.json"
    dep_file.write_text(json.dumps({"deployment_id": "99"}))

    resource = make_resource(mock_github)
    version, _ = resource.publish_new_version(
        sources_dir=str(tmp_path),
        build_metadata=mock_build_metadata(),
        action="finish",
        deployment_file="deployment.json",
        state=valid_state,
    )
    assert version.deployment_id == "99"


def test_publish_finish_raises_if_file_missing(mock_github, tmp_path):
    resource = make_resource(mock_github)
    with pytest.raises(FileNotFoundError):
        resource.publish_new_version(
            sources_dir=str(tmp_path),
            build_metadata=mock_build_metadata(),
            action="finish",
            deployment_file="nonexistent.json",
            state="success",
        )


def test_publish_finish_raises_if_deployment_id_missing(mock_github, tmp_path):
    dep_file = tmp_path / "deployment.json"
    dep_file.write_text(json.dumps({"sha": "abc"}))  # no deployment_id

    resource = make_resource(mock_github)
    with pytest.raises(ValueError, match="deployment_id"):
        resource.publish_new_version(
            sources_dir=str(tmp_path),
            build_metadata=mock_build_metadata(),
            action="finish",
            deployment_file="deployment.json",
            state="success",
        )


# ---------------------------------------------------------------------------
# out (publish_new_version) - invalid action
# ---------------------------------------------------------------------------


def test_publish_invalid_action(mock_github):
    resource = make_resource(mock_github)
    with pytest.raises(ValueError, match="action must be"):
        resource.publish_new_version(
            sources_dir="/workspace",
            build_metadata=mock_build_metadata(),
            action="deploy",
        )
