"""Tests for the Concourse Pulumi resource.

Imports use the unique module name "pulumi_concourse" (registered by conftest.py)
to avoid colliding with the packer resource's identically-named concourse.py.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pulumi_concourse import PulumiResource, PulumiVersion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_metadata():
    from concoursetools import BuildMetadata
    return BuildMetadata(
        BUILD_ID="1",
        BUILD_NAME="1",
        BUILD_JOB_NAME="test-job",
        BUILD_PIPELINE_NAME="test-pipeline",
        BUILD_PIPELINE_INSTANCE_VARS="{}",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://concourse.example.com",
    )


@pytest.fixture
def metadata():
    return _build_metadata()


@pytest.fixture
def sources_dir(tmp_path):
    (tmp_path / "myapp").mkdir()
    return tmp_path


@pytest.fixture
def resource():
    return PulumiResource(
        stack_name="applications.myapp.production",
        project_name="ol-infrastructure-myapp",
        source_dir="myapp",
    )


# ---------------------------------------------------------------------------
# PulumiVersion
# ---------------------------------------------------------------------------

class TestPulumiVersion:
    def test_default_id(self):
        v = PulumiVersion()
        assert v.id == "0"

    def test_custom_id(self):
        v = PulumiVersion(id="42")
        assert v.id == "42"


# ---------------------------------------------------------------------------
# fetch_new_versions (check)
# ---------------------------------------------------------------------------

class TestFetchNewVersions:
    def test_always_returns_static_version(self, resource):
        versions = resource.fetch_new_versions(previous_version=None)
        assert versions == [PulumiVersion(id="0")]

    def test_ignores_previous_version(self, resource):
        versions = resource.fetch_new_versions(previous_version=PulumiVersion(id="5"))
        assert versions == [PulumiVersion(id="0")]


# ---------------------------------------------------------------------------
# download_version (get / in)
# ---------------------------------------------------------------------------

class TestDownloadVersion:
    def test_skip_implicit_get_returns_immediately(
        self, resource, sources_dir, metadata
    ):
        with patch("pulumi_concourse.pulumi_utils.read_stack") as mock_read:
            version, meta = resource.download_version(
                PulumiVersion(id="0"),
                sources_dir,
                metadata,
                skip_implicit_get=True,
            )
        mock_read.assert_not_called()
        assert version == PulumiVersion(id="0")
        assert meta == {}

    def test_reads_stack_and_writes_outputs_json(
        self, resource, sources_dir, metadata
    ):
        fake_outputs = {"vpc_id": "vpc-123", "subnet_id": "sub-456"}
        with patch("pulumi_concourse.pulumi_utils.read_stack", return_value=fake_outputs) as mock_read:
            version, meta = resource.download_version(
                PulumiVersion(id="0"),
                sources_dir,
                metadata,
            )
        mock_read.assert_called_once()
        outputs_file = sources_dir / "myapp" / "applications.myapp.production_outputs.json"
        assert outputs_file.exists()
        assert json.loads(outputs_file.read_text()) == fake_outputs
        assert version == PulumiVersion(id="0")

    def test_param_overrides_source_stack_name(
        self, resource, sources_dir, metadata
    ):
        fake_outputs = {}
        with patch("pulumi_concourse.pulumi_utils.read_stack", return_value=fake_outputs) as mock_read:
            resource.download_version(
                PulumiVersion(id="0"),
                sources_dir,
                metadata,
                stack_name="applications.myapp.staging",
            )
        call_kwargs = mock_read.call_args.kwargs
        assert call_kwargs["stack_name"] == "applications.myapp.staging"

    def test_env_os_applied_to_environ(
        self, resource, sources_dir, metadata
    ):
        with patch("pulumi_concourse.pulumi_utils.read_stack", return_value={}):
            resource.download_version(
                PulumiVersion(id="0"),
                sources_dir,
                metadata,
                env_os={"PULUMI_TEST_VAR": "test-value"},
            )
        assert os.environ.get("PULUMI_TEST_VAR") == "test-value"

    def test_env_pulumi_merged_with_source_env(
        self, resource_with_env, sources_dir, metadata
    ):
        """env_pulumi from params should merge with env_pulumi from source."""
        with patch("pulumi_concourse.pulumi_utils.read_stack", return_value={}) as mock_read:
            resource_with_env.download_version(
                PulumiVersion(id="0"),
                sources_dir,
                metadata,
                env_pulumi={"EXTRA_KEY": "extra-value"},
            )
        call_kwargs = mock_read.call_args.kwargs
        env_passed = call_kwargs["env"]["env_pulumi"]
        assert env_passed.get("BASE_KEY") == "base-value"
        assert env_passed.get("EXTRA_KEY") == "extra-value"


@pytest.fixture
def resource_with_env():
    return PulumiResource(
        stack_name="applications.myapp.production",
        project_name="ol-infrastructure-myapp",
        source_dir="myapp",
        env_pulumi={"BASE_KEY": "base-value"},
    )


# ---------------------------------------------------------------------------
# publish_new_version (put / out)
# ---------------------------------------------------------------------------

class TestPublishNewVersionCreate:
    @patch("pulumi_concourse.pulumi_utils.create_stack", return_value=1)
    def test_create_calls_create_stack(
        self, mock_create, resource, sources_dir, metadata
    ):
        version, meta = resource.publish_new_version(
            sources_dir, metadata,
            action="create",
        )
        mock_create.assert_called_once()
        assert version == PulumiVersion(id="1")
        assert meta == {}

    @patch("pulumi_concourse.pulumi_utils.create_stack", return_value=1)
    def test_create_passes_stack_config(
        self, mock_create, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="create",
            stack_config={"aws:region": "us-east-1"},
        )
        kwargs = mock_create.call_args.kwargs
        assert kwargs["stack_config"] == {"aws:region": "us-east-1"}

    @patch("pulumi_concourse.pulumi_utils.create_stack", return_value=0)
    def test_create_preview_flag_forwarded(
        self, mock_create, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="create",
            preview=True,
        )
        kwargs = mock_create.call_args.kwargs
        assert kwargs["preview"] is True


class TestPublishNewVersionUpdate:
    @patch("pulumi_concourse.pulumi_utils.update_stack", return_value=5)
    def test_update_calls_update_stack(
        self, mock_update, resource, sources_dir, metadata
    ):
        version, meta = resource.publish_new_version(
            sources_dir, metadata,
            action="update",
        )
        mock_update.assert_called_once()
        assert version == PulumiVersion(id="5")

    @patch("pulumi_concourse.pulumi_utils.update_stack", return_value=5)
    def test_update_refresh_defaults_to_true(
        self, mock_update, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="update",
        )
        kwargs = mock_update.call_args.kwargs
        assert kwargs["refresh_stack"] is True

    @patch("pulumi_concourse.pulumi_utils.update_stack", return_value=5)
    def test_update_refresh_can_be_disabled(
        self, mock_update, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="update",
            refresh_stack=False,
        )
        kwargs = mock_update.call_args.kwargs
        assert kwargs["refresh_stack"] is False

    @patch("pulumi_concourse.pulumi_utils.update_stack", return_value=5)
    def test_update_stack_name_override(
        self, mock_update, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="update",
            stack_name="applications.myapp.staging",
        )
        kwargs = mock_update.call_args.kwargs
        assert kwargs["stack_name"] == "applications.myapp.staging"


class TestPublishNewVersionDestroy:
    @patch("pulumi_concourse.pulumi_utils.destroy_stack", return_value=3)
    def test_destroy_calls_destroy_stack(
        self, mock_destroy, resource, sources_dir, metadata
    ):
        version, meta = resource.publish_new_version(
            sources_dir, metadata,
            action="destroy",
        )
        mock_destroy.assert_called_once()
        assert version == PulumiVersion(id="3")

    @patch("pulumi_concourse.pulumi_utils.destroy_stack", return_value=3)
    def test_destroy_passes_refresh_flag(
        self, mock_destroy, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="destroy",
            refresh_stack=True,
        )
        kwargs = mock_destroy.call_args.kwargs
        assert kwargs["refresh_stack"] is True


class TestPublishNewVersionEnvHandling:
    @patch("pulumi_concourse.pulumi_utils.update_stack", return_value=1)
    def test_env_os_applied_to_environ(
        self, mock_update, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="update",
            env_os={"PULUMI_PUT_VAR": "put-value"},
        )
        assert os.environ.get("PULUMI_PUT_VAR") == "put-value"

    @patch("pulumi_concourse.io_utils.read_value_from_file", return_value="passphrase123")
    @patch("pulumi_concourse.pulumi_utils.update_stack", return_value=1)
    def test_env_vars_from_files_reads_and_sets(
        self, mock_update, mock_read, resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            action="update",
            env_vars_from_files={"PULUMI_CONFIG_PASSPHRASE": "path/to/passphrase"},
        )
        mock_read.assert_called_once_with(
            "path/to/passphrase", working_dir=str(sources_dir)
        )
        assert os.environ.get("PULUMI_CONFIG_PASSPHRASE") == "passphrase123"

    @patch("pulumi_concourse.pulumi_utils.update_stack", return_value=1)
    def test_env_pulumi_merges_source_and_params(
        self, mock_update, resource_with_env, sources_dir, metadata
    ):
        resource_with_env.publish_new_version(
            sources_dir, metadata,
            action="update",
            env_pulumi={"EXTRA": "value"},
        )
        kwargs = mock_update.call_args.kwargs
        env_passed = kwargs["env"]["env_pulumi"]
        assert env_passed.get("BASE_KEY") == "base-value"
        assert env_passed.get("EXTRA") == "value"


class TestPublishNewVersionInvalidAction:
    def test_invalid_action_raises(self, resource, sources_dir, metadata):
        with pytest.raises(ValueError, match="Invalid action"):
            resource.publish_new_version(
                sources_dir, metadata,
                action="rebuild",
            )
