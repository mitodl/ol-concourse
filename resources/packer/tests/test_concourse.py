"""Tests for the Concourse Packer resource.

Imports use the unique module name "packer_concourse" (registered by conftest.py)
to avoid colliding with the pulumi resource's identically-named concourse.py.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from packer_concourse import PackerResource, PackerVersion, _manifest_to_version_and_metadata


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


EMPTY_MANIFEST = {"artifacts": {}}

REAL_MANIFEST = {
    "artifacts": {
        "amazon-ebs.web": {
            "0": {
                "id": "ami-abc123",
                "region": "us-east-1",
                "type": "Amazon AMI",
            }
        }
    }
}


# ---------------------------------------------------------------------------
# PackerVersion
# ---------------------------------------------------------------------------

class TestPackerVersion:
    def test_default_id(self):
        v = PackerVersion()
        assert v.id == "0"

    def test_custom_id(self):
        v = PackerVersion(id="ami-abc123")
        assert v.id == "ami-abc123"


# ---------------------------------------------------------------------------
# _manifest_to_version_and_metadata
# ---------------------------------------------------------------------------

class TestManifestToVersionAndMetadata:
    def test_empty_manifest_returns_default_version(self):
        version, metadata = _manifest_to_version_and_metadata(EMPTY_MANIFEST)
        assert version == PackerVersion(id="0")
        assert metadata == {}

    def test_artifact_id_becomes_version(self):
        version, metadata = _manifest_to_version_and_metadata(REAL_MANIFEST)
        assert version == PackerVersion(id="ami-abc123")

    def test_artifact_fields_become_metadata(self):
        _, metadata = _manifest_to_version_and_metadata(REAL_MANIFEST)
        assert metadata["amazon-ebs.web::0::id"] == "ami-abc123"
        assert metadata["amazon-ebs.web::0::region"] == "us-east-1"
        assert metadata["amazon-ebs.web::0::type"] == "Amazon AMI"

    def test_none_values_become_empty_strings(self):
        manifest = {
            "artifacts": {
                "amazon-ebs.web": {
                    "0": {"id": "ami-x", "description": None}
                }
            }
        }
        _, metadata = _manifest_to_version_and_metadata(manifest)
        assert metadata["amazon-ebs.web::0::description"] == ""

    def test_multiple_artifacts(self):
        manifest = {
            "artifacts": {
                "amazon-ebs.web": {"0": {"id": "ami-first"}},
                "amazon-ebs.worker": {"0": {"id": "ami-second"}},
            }
        }
        version, metadata = _manifest_to_version_and_metadata(manifest)
        # version comes from the first artifact with id key
        assert version.id in ("ami-first", "ami-second")
        assert "amazon-ebs.web::0::id" in metadata
        assert "amazon-ebs.worker::0::id" in metadata


# ---------------------------------------------------------------------------
# PackerResource.publish_new_version
# ---------------------------------------------------------------------------

@pytest.fixture
def sources_dir(tmp_path):
    return tmp_path


@pytest.fixture
def metadata():
    return _build_metadata()


@pytest.fixture
def resource():
    return PackerResource()


class TestPublishNewVersionValidate:
    @patch("packer_concourse.packer_lib.format_packer_cmd")
    @patch("packer_concourse.packer_lib.validate")
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_validate_calls_packer_steps(
        self, mock_version, mock_init, mock_validate, mock_fmt,
        resource, sources_dir, metadata
    ):
        version, meta = resource.publish_new_version(
            sources_dir, metadata,
            objective="validate",
            template="template.pkr.hcl",
        )
        mock_version.assert_called_once()
        mock_init.assert_called_once_with(str(sources_dir), "template.pkr.hcl")
        mock_validate.assert_called_once()
        mock_fmt.assert_called_once()
        assert version == PackerVersion(id="0")
        assert meta == {}

    @patch("packer_concourse.packer_lib.format_packer_cmd")
    @patch("packer_concourse.packer_lib.validate")
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_validate_passes_vars_to_packer(
        self, mock_version, mock_init, mock_validate, mock_fmt,
        resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            objective="validate",
            template="template.pkr.hcl",
            vars={"region": "us-east-1"},
            var_files=["vars.pkrvars.hcl"],
            only=["amazon-ebs.web"],
        )
        _, kwargs = mock_validate.call_args
        assert kwargs.get("template_vars") == {"region": "us-east-1"}
        assert kwargs.get("var_file_paths") == ["vars.pkrvars.hcl"]
        assert kwargs.get("only") == ["amazon-ebs.web"]

    @patch("packer_concourse.packer_lib.format_packer_cmd")
    @patch("packer_concourse.packer_lib.validate")
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_validate_sets_env_vars(
        self, mock_version, mock_init, mock_validate, mock_fmt,
        resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            objective="validate",
            template="template.pkr.hcl",
            env_vars={"MY_VAR": "hello"},
        )
        assert os.environ.get("MY_VAR") == "hello"

    @patch("packer_concourse.io_utils.read_value_from_file", return_value="secret-token")
    @patch("packer_concourse.packer_lib.format_packer_cmd")
    @patch("packer_concourse.packer_lib.validate")
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_validate_reads_env_vars_from_files(
        self, mock_version, mock_init, mock_validate, mock_fmt, mock_read,
        resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            objective="validate",
            template="template.pkr.hcl",
            env_vars_from_files={"AWS_SESSION_TOKEN": "path/to/token"},
        )
        mock_read.assert_called_once_with("path/to/token", working_dir=str(sources_dir))
        assert os.environ.get("AWS_SESSION_TOKEN") == "secret-token"


class TestPublishNewVersionBuild:
    @patch("packer_concourse.packer_lib.build", return_value=REAL_MANIFEST)
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_build_returns_version_from_manifest(
        self, mock_version, mock_init, mock_build,
        resource, sources_dir, metadata
    ):
        version, meta = resource.publish_new_version(
            sources_dir, metadata,
            objective="build",
            template="template.pkr.hcl",
        )
        assert version == PackerVersion(id="ami-abc123")
        assert "amazon-ebs.web::0::id" in meta

    @patch("packer_concourse.packer_lib.build", return_value=EMPTY_MANIFEST)
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_build_with_empty_manifest(
        self, mock_version, mock_init, mock_build,
        resource, sources_dir, metadata
    ):
        version, meta = resource.publish_new_version(
            sources_dir, metadata,
            objective="build",
            template="template.pkr.hcl",
        )
        assert version == PackerVersion(id="0")
        assert meta == {}

    @patch("packer_concourse.packer_lib.build", return_value=REAL_MANIFEST)
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_build_passes_force_and_debug(
        self, mock_version, mock_init, mock_build,
        resource, sources_dir, metadata
    ):
        resource.publish_new_version(
            sources_dir, metadata,
            objective="build",
            template="template.pkr.hcl",
            force=True,
            debug=True,
        )
        _, kwargs = mock_build.call_args
        assert kwargs.get("force") is True
        assert kwargs.get("debug") is True


class TestPublishNewVersionInvalidObjective:
    @patch("packer_concourse.packer_lib.init")
    @patch("packer_concourse.packer_lib.version")
    def test_invalid_objective_raises(
        self, mock_version, mock_init, resource, sources_dir, metadata
    ):
        with pytest.raises(ValueError, match="Invalid objective"):
            resource.publish_new_version(
                sources_dir, metadata,
                objective="deploy",
                template="template.pkr.hcl",
            )
