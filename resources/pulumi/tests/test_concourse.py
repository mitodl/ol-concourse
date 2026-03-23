"""Unit tests for the Concourse Pulumi resource."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pulumi.automation.events import (
    DiffKind,
    EngineEvent,
    OpType,
    PropertyDiff,
    ResourcePreEvent,
    StepEventMetadata,
)

import pulumi_utils
from pulumi_concourse import (
    PulumiResource,
    PulumiVersion,
    _apply_os_env,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_resource(
    stack_name: str = "org.proj.dev",
    project_name: str = "my-project",
    source_dir: str = "infra/myapp",
    env_pulumi: dict | None = None,
    env_os: dict | None = None,
) -> PulumiResource:
    return PulumiResource(
        stack_name=stack_name,
        project_name=project_name,
        source_dir=source_dir,
        env_pulumi=env_pulumi,
        env_os=env_os,
    )


def _make_step_metadata(
    op: OpType = OpType.UPDATE,
    urn: str = "urn:pulumi:dev::proj::aws:s3/bucket:Bucket::my-bucket",
    resource_type: str = "aws:s3/bucket:Bucket",
    diffs: list[str] | None = None,
    detailed_diff: dict | None = None,
) -> StepEventMetadata:
    return StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="",
        diffs=diffs if diffs is not None else ["tags"],
        detailed_diff=detailed_diff
        if detailed_diff is not None
        else {"tags": PropertyDiff(DiffKind.UPDATE, input_diff=True)},
    )


def _make_engine_event(metadata: StepEventMetadata) -> EngineEvent:
    return EngineEvent(
        sequence=1,
        timestamp=0,
        resource_pre_event=ResourcePreEvent(metadata=metadata),
    )


def _make_mock_stack(outputs: dict | None = None) -> MagicMock:
    """Return a mock pulumi Stack with sensible defaults for preview tests."""
    stack = MagicMock()
    stack.outputs.return_value = {
        k: MagicMock(value=v)
        for k, v in (outputs or {"url": "https://example.com"}).items()
    }

    preview_result = MagicMock()
    preview_result.stdout = "preview output"
    preview_result.change_summary = {OpType.UPDATE: 1, OpType.SAME: 5}
    stack.preview.return_value = preview_result

    return stack


# ---------------------------------------------------------------------------
# PulumiVersion
# ---------------------------------------------------------------------------


class TestPulumiVersion:
    def test_default_ref(self) -> None:
        assert PulumiVersion().id == "0"

    def test_custom_ref(self) -> None:
        assert PulumiVersion(id="abc").id == "abc"


# ---------------------------------------------------------------------------
# PulumiResource.__init__ / _resolve_params
# ---------------------------------------------------------------------------


class TestResolveParams:
    def test_source_level_defaults(self) -> None:
        resource = _make_resource(
            stack_name="org.proj.dev",
            source_dir="infra",
            env_pulumi={"PULUMI_CONFIG_PASSPHRASE": "secret"},
            env_os={"AWS_REGION": "us-east-1"},
        )
        result = resource._resolve_params(
            stack_name=None,
            project_name=None,
            source_dir=None,
            env_pulumi=None,
            env_os=None,
        )
        assert result["stack_name"] == "org.proj.dev"
        assert result["source_dir"] == "infra"
        assert result["env_pulumi"] == {"PULUMI_CONFIG_PASSPHRASE": "secret"}
        assert result["env_os"] == {"AWS_REGION": "us-east-1"}

    def test_step_level_overrides_source(self) -> None:
        resource = _make_resource(
            stack_name="org.proj.dev",
            source_dir="infra",
            env_pulumi={"PULUMI_CONFIG_PASSPHRASE": "source-secret"},
        )
        result = resource._resolve_params(
            stack_name="org.proj.staging",
            project_name=None,
            source_dir="other/infra",
            env_pulumi={"PULUMI_CONFIG_PASSPHRASE": "step-secret", "EXTRA": "1"},
            env_os=None,
        )
        assert result["stack_name"] == "org.proj.staging"
        assert result["source_dir"] == "other/infra"
        assert result["env_pulumi"]["PULUMI_CONFIG_PASSPHRASE"] == "step-secret"
        assert result["env_pulumi"]["EXTRA"] == "1"

    def test_env_vars_merged_not_replaced(self) -> None:
        resource = _make_resource(
            env_pulumi={"A": "from-source"},
            env_os={"OS_A": "os-source"},
        )
        result = resource._resolve_params(
            stack_name=None,
            project_name=None,
            source_dir=None,
            env_pulumi={"B": "from-step"},
            env_os={"OS_B": "os-step"},
        )
        assert result["env_pulumi"] == {"A": "from-source", "B": "from-step"}
        assert result["env_os"] == {"OS_A": "os-source", "OS_B": "os-step"}


# ---------------------------------------------------------------------------
# fetch_new_versions (check)
# ---------------------------------------------------------------------------


class TestFetchNewVersions:
    def test_returns_static_version_with_no_previous(self) -> None:
        resource = _make_resource()
        versions = resource.fetch_new_versions(previous_version=None)
        assert versions == [PulumiVersion(id="0")]

    def test_returns_static_version_with_previous(self) -> None:
        resource = _make_resource()
        versions = resource.fetch_new_versions(previous_version=PulumiVersion(id="0"))
        assert versions == [PulumiVersion(id="0")]

    def test_version_is_always_zero(self) -> None:
        resource = _make_resource()
        v1 = resource.fetch_new_versions(None)[0]
        v2 = resource.fetch_new_versions(PulumiVersion(id="99"))[0]
        assert v1.id == "0"
        assert v2.id == "0"


# ---------------------------------------------------------------------------
# _apply_os_env
# ---------------------------------------------------------------------------


class TestApplyOsEnv:
    def test_sets_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_PULUMI_VAR", raising=False)
        _apply_os_env({"TEST_PULUMI_VAR": "hello"})
        assert os.environ["TEST_PULUMI_VAR"] == "hello"

    def test_empty_dict_is_noop(self) -> None:
        before = dict(os.environ)
        _apply_os_env({})
        assert dict(os.environ) == before


# ---------------------------------------------------------------------------
# download_version (get)
# ---------------------------------------------------------------------------


class TestDownloadVersion:
    def test_writes_outputs_json(self, tmp_path: Path) -> None:
        resource = _make_resource(
            stack_name="org.proj.dev",
            source_dir="ol-infra/src/myapp",
        )
        destination_dir = tmp_path / "myapp-stack"
        destination_dir.mkdir()
        work_dir = tmp_path / "ol-infra" / "src" / "myapp"
        work_dir.mkdir(parents=True)
        build_metadata = MagicMock()

        fake_outputs = {"endpoint": "https://example.com", "port": 443}
        with patch("pulumi_utils.read_stack", return_value=fake_outputs):
            _, metadata = resource.download_version(
                PulumiVersion(), destination_dir, build_metadata
            )

        outputs_file = destination_dir / "org.proj.dev_outputs.json"
        assert outputs_file.exists()
        assert json.loads(outputs_file.read_text()) == fake_outputs
        assert metadata["outputs_file"] == str(outputs_file)

    def test_output_key_forwarded_to_read_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="ol-infra")
        destination_dir = tmp_path / "myapp-stack"
        destination_dir.mkdir()
        (tmp_path / "ol-infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.read_stack", return_value={"url": "https://x.com"}) as mock_read:
            resource.download_version(
                PulumiVersion(), destination_dir, build_metadata, output_key="url"
            )

        assert mock_read.call_args.kwargs["output_key"] == "url"

    def test_outputs_written_from_read_stack_return_value(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        destination_dir = tmp_path / "myapp-stack"
        destination_dir.mkdir()
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.read_stack", return_value={"url": "https://x.com"}):
            resource.download_version(
                PulumiVersion(), destination_dir, build_metadata, output_key="url"
            )

        data = json.loads((destination_dir / "org.proj.dev_outputs.json").read_text())
        assert data == {"url": "https://x.com"}

    def test_run_preview_calls_pulumi_utils_run_preview(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        destination_dir = tmp_path / "myapp-stack"
        destination_dir.mkdir()
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.read_stack", return_value={}), \
             patch("pulumi_utils.run_preview") as mock_run_preview:
            _, metadata = resource.download_version(
                PulumiVersion(), destination_dir, build_metadata, run_preview=True
            )

        expected_preview_file = destination_dir / "org.proj.dev_preview.json"
        mock_run_preview.assert_called_once()
        assert mock_run_preview.call_args.kwargs["output_file"] == expected_preview_file
        assert metadata["preview_file"] == str(expected_preview_file)

    def test_env_pulumi_forwarded_to_read_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(
            stack_name="org.proj.dev",
            source_dir="infra",
            env_pulumi={"PULUMI_CONFIG_PASSPHRASE": "secret"},
        )
        destination_dir = tmp_path / "myapp-stack"
        destination_dir.mkdir()
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.read_stack", return_value={}) as mock_read:
            resource.download_version(PulumiVersion(), destination_dir, build_metadata)

        assert mock_read.call_args.kwargs["env_pulumi"] == {"PULUMI_CONFIG_PASSPHRASE": "secret"}

    def test_env_os_applied_before_read_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MY_OS_VAR", raising=False)
        resource = _make_resource(
            stack_name="org.proj.dev",
            source_dir="infra",
            env_os={"MY_OS_VAR": "set-by-resource"},
        )
        destination_dir = tmp_path / "myapp-stack"
        destination_dir.mkdir()
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()
        captured: list[str] = []

        def capturing_read(**kwargs):
            captured.append(os.environ.get("MY_OS_VAR", "NOT_SET"))
            return {}

        with patch("pulumi_utils.read_stack", side_effect=capturing_read):
            resource.download_version(PulumiVersion(), destination_dir, build_metadata)

        assert captured == ["set-by-resource"]


# ---------------------------------------------------------------------------
# publish_new_version (put)
# ---------------------------------------------------------------------------


class TestPublishNewVersion:
    def test_invalid_action_raises(self, tmp_path: Path) -> None:
        resource = _make_resource()
        build_metadata = MagicMock()
        with pytest.raises(ValueError, match="Invalid action 'explode'"):
            resource.publish_new_version(tmp_path, build_metadata, action="explode")

    def test_create_calls_create_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.create_stack", return_value=1) as mock_create, \
             patch("pulumi_utils.update_stack") as mock_update:
            resource.publish_new_version(tmp_path, build_metadata, action="create")

        mock_create.assert_called_once()
        mock_update.assert_not_called()

    def test_update_calls_update_stack_and_refreshes_by_default(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.update_stack", return_value=5) as mock_update, \
             patch("pulumi_utils.create_stack") as mock_create:
            resource.publish_new_version(tmp_path, build_metadata, action="update")

        mock_create.assert_not_called()
        mock_update.assert_called_once()
        assert mock_update.call_args.kwargs["refresh_stack"] is True

    def test_update_skips_refresh_when_disabled(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.update_stack", return_value=5) as mock_update:
            resource.publish_new_version(
                tmp_path, build_metadata, action="update", refresh_stack=False
            )

        assert mock_update.call_args.kwargs["refresh_stack"] is False

    def test_destroy_calls_destroy_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.destroy_stack", return_value=3) as mock_destroy:
            _, metadata = resource.publish_new_version(
                tmp_path, build_metadata, action="destroy"
            )

        mock_destroy.assert_called_once()
        assert mock_destroy.call_args.kwargs["stack_name"] == "org.proj.dev"
        assert metadata["action"] == "destroy"
        assert metadata["result"] == "succeeded"

    def test_destroy_refresh_defaults_to_true(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.destroy_stack", return_value=3) as mock_destroy:
            resource.publish_new_version(tmp_path, build_metadata, action="destroy")

        assert mock_destroy.call_args.kwargs["refresh_stack"] is True

    def test_destroy_skips_refresh_when_disabled(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.destroy_stack", return_value=3) as mock_destroy:
            resource.publish_new_version(
                tmp_path, build_metadata, action="destroy", refresh_stack=False
            )

        assert mock_destroy.call_args.kwargs["refresh_stack"] is False

    def test_preview_true_calls_update_stack_with_preview(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        def fake_update(**kwargs):
            if kwargs.get("preview_file"):
                kwargs["preview_file"].write_text(
                    json.dumps({"change_summary": {"update": 1}, "changes": [], "stdout": ""})
                )
            return 0

        with patch("pulumi_utils.update_stack", side_effect=fake_update) as mock_update:
            _, metadata = resource.publish_new_version(
                tmp_path, build_metadata, action="update", preview=True
            )

        assert mock_update.call_args.kwargs["preview"] is True
        assert metadata["action"] == "update"
        assert "preview_file" in metadata
        assert "changes" in metadata

    def test_stack_config_forwarded_to_update_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.update_stack", return_value=5) as mock_update:
            resource.publish_new_version(
                tmp_path,
                build_metadata,
                action="update",
                stack_config={"aws:region": "us-east-1", "app:env": "staging"},
            )

        assert mock_update.call_args.kwargs["stack_config"] == {
            "aws:region": "us-east-1",
            "app:env": "staging",
        }

    def test_env_vars_from_files_read_and_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PULUMI_CONFIG_PASSPHRASE", raising=False)
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()

        passphrase_file = tmp_path / "secrets" / "passphrase"
        passphrase_file.parent.mkdir()
        passphrase_file.write_text("super-secret\n")
        build_metadata = MagicMock()

        with patch("pulumi_utils.update_stack", return_value=5):
            resource.publish_new_version(
                tmp_path,
                build_metadata,
                action="update",
                env_vars_from_files={"PULUMI_CONFIG_PASSPHRASE": "secrets/passphrase"},
            )

        assert os.environ["PULUMI_CONFIG_PASSPHRASE"] == "super-secret"

    def test_returns_version_from_pulumi_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.update_stack", return_value=5):
            version, _ = resource.publish_new_version(
                tmp_path, build_metadata, action="update"
            )

        assert version == PulumiVersion(id="5")

    def test_metadata_includes_action_and_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.update_stack", return_value=5):
            _, metadata = resource.publish_new_version(
                tmp_path, build_metadata, action="update"
            )

        assert metadata["action"] == "update"
        assert metadata["stack"] == "org.proj.dev"
        assert metadata["result"] == "succeeded"


# ---------------------------------------------------------------------------
# pulumi_utils.serialize_resource_event
# ---------------------------------------------------------------------------


class TestSerializeResourceEvent:
    def test_basic_update_event(self) -> None:
        meta = _make_step_metadata(
            op=OpType.UPDATE,
            urn="urn:pulumi:dev::proj::aws:s3/bucket:Bucket::my-bucket",
            resource_type="aws:s3/bucket:Bucket",
            diffs=["tags", "versioning"],
            detailed_diff={
                "tags": PropertyDiff(DiffKind.UPDATE, input_diff=True),
                "versioning": PropertyDiff(DiffKind.UPDATE, input_diff=False),
            },
        )
        result = pulumi_utils.serialize_resource_event(ResourcePreEvent(metadata=meta))

        assert result["operation"] == "update"
        assert result["urn"] == "urn:pulumi:dev::proj::aws:s3/bucket:Bucket::my-bucket"
        assert result["type"] == "aws:s3/bucket:Bucket"
        assert result["diffs"] == ["tags", "versioning"]
        assert result["detailed_diff"]["tags"] == {"diff_kind": "update", "input_diff": True}
        assert result["detailed_diff"]["versioning"] == {
            "diff_kind": "update",
            "input_diff": False,
        }

    def test_create_event_no_detailed_diff(self) -> None:
        meta = _make_step_metadata(op=OpType.CREATE, diffs=[], detailed_diff={})
        result = pulumi_utils.serialize_resource_event(ResourcePreEvent(metadata=meta))

        assert result["operation"] == "create"
        assert result["diffs"] == []
        assert result["detailed_diff"] == {}

    def test_delete_replace_diff_kind(self) -> None:
        meta = _make_step_metadata(
            op=OpType.DELETE,
            detailed_diff={"id": PropertyDiff(DiffKind.DELETE_REPLACE, input_diff=True)},
        )
        result = pulumi_utils.serialize_resource_event(ResourcePreEvent(metadata=meta))

        assert result["detailed_diff"]["id"]["diff_kind"] == "delete-replace"

    def test_none_diffs_serialized_as_empty_list(self) -> None:
        meta = StepEventMetadata(
            op=OpType.UPDATE,
            urn="urn:x",
            type="aws:ec2:Instance",
            provider="",
            diffs=None,
            detailed_diff=None,
        )
        result = pulumi_utils.serialize_resource_event(ResourcePreEvent(metadata=meta))

        assert result["diffs"] == []
        assert result["detailed_diff"] == {}


# ---------------------------------------------------------------------------
# pulumi_utils._run_preview_on_stack
# ---------------------------------------------------------------------------


class TestRunPreviewOnStack:
    def test_writes_preview_json(self, tmp_path: Path) -> None:
        mock_stack = _make_mock_stack()
        update_meta = _make_step_metadata(op=OpType.UPDATE)
        same_meta = _make_step_metadata(op=OpType.SAME)

        def fake_preview(**kwargs):
            on_event = kwargs.get("on_event")
            if on_event:
                on_event(_make_engine_event(update_meta))
                on_event(_make_engine_event(same_meta))
            return mock_stack.preview.return_value

        mock_stack.preview.side_effect = fake_preview
        output_file = tmp_path / "preview.json"

        pulumi_utils._run_preview_on_stack(mock_stack, output_file)

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "change_summary" in data
        assert "changes" in data
        assert "stdout" in data

    def test_same_ops_excluded_from_changes(self, tmp_path: Path) -> None:
        mock_stack = _make_mock_stack()
        update_meta = _make_step_metadata(op=OpType.UPDATE)
        same_meta = _make_step_metadata(op=OpType.SAME, urn="urn:pulumi:same-resource")

        def fake_preview(**kwargs):
            on_event = kwargs.get("on_event")
            if on_event:
                on_event(_make_engine_event(update_meta))
                on_event(_make_engine_event(same_meta))
            return mock_stack.preview.return_value

        mock_stack.preview.side_effect = fake_preview
        output_file = tmp_path / "preview.json"

        pulumi_utils._run_preview_on_stack(mock_stack, output_file)

        data = json.loads(output_file.read_text())
        assert len(data["changes"]) == 1
        assert data["changes"][0]["operation"] == "update"

    def test_change_summary_serialized_with_string_keys(self, tmp_path: Path) -> None:
        mock_stack = _make_mock_stack()
        mock_stack.preview.return_value.change_summary = {
            OpType.CREATE: 2,
            OpType.SAME: 10,
        }
        output_file = tmp_path / "preview.json"

        pulumi_utils._run_preview_on_stack(mock_stack, output_file)

        data = json.loads(output_file.read_text())
        assert data["change_summary"] == {"create": 2, "same": 10}

    def test_stdout_included_in_output(self, tmp_path: Path) -> None:
        mock_stack = _make_mock_stack()
        mock_stack.preview.return_value.stdout = "  ~ aws:s3:Bucket  my-bucket  update\n"
        output_file = tmp_path / "preview.json"

        pulumi_utils._run_preview_on_stack(mock_stack, output_file)

        data = json.loads(output_file.read_text())
        assert data["stdout"] == "  ~ aws:s3:Bucket  my-bucket  update\n"

    def test_no_output_file_returns_payload_without_writing(self, tmp_path: Path) -> None:
        mock_stack = _make_mock_stack()
        payload = pulumi_utils._run_preview_on_stack(mock_stack, output_file=None)

        assert "change_summary" in payload
        assert "changes" in payload
        assert "stdout" in payload
        assert not list(tmp_path.iterdir())



# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_resource(
    stack_name: str = "org.proj.dev",
    project_name: str = "my-project",
    source_dir: str = "infra/myapp",
    env_pulumi: dict | None = None,
    env_os: dict | None = None,
) -> PulumiResource:
    return PulumiResource(
        stack_name=stack_name,
        project_name=project_name,
        source_dir=source_dir,
        env_pulumi=env_pulumi,
        env_os=env_os,
    )


def _make_step_metadata(
    op: OpType = OpType.UPDATE,
    urn: str = "urn:pulumi:dev::proj::aws:s3/bucket:Bucket::my-bucket",
    resource_type: str = "aws:s3/bucket:Bucket",
    diffs: list[str] | None = None,
    detailed_diff: dict | None = None,
) -> StepEventMetadata:
    return StepEventMetadata(
        op=op,
        urn=urn,
        type=resource_type,
        provider="",
        diffs=diffs if diffs is not None else ["tags"],
        detailed_diff=detailed_diff
        if detailed_diff is not None
        else {"tags": PropertyDiff(DiffKind.UPDATE, input_diff=True)},
    )


def _make_engine_event(metadata: StepEventMetadata) -> EngineEvent:
    return EngineEvent(
        sequence=1,
        timestamp=0,
        resource_pre_event=ResourcePreEvent(metadata=metadata),
    )


def _make_mock_stack(outputs: dict | None = None) -> MagicMock:
    """Return a mock pulumi Stack with sensible defaults."""
    stack = MagicMock()
    stack.outputs.return_value = {
        k: MagicMock(value=v) for k, v in (outputs or {"url": "https://example.com"}).items()
    }

    preview_result = MagicMock()
    preview_result.stdout = "preview output"
    preview_result.change_summary = {OpType.UPDATE: 1, OpType.SAME: 5}
    stack.preview.return_value = preview_result

    up_result = MagicMock()
    up_result.summary.result = "succeeded"
    up_result.summary.resource_changes = {OpType.UPDATE: 1}
    stack.up.return_value = up_result

    destroy_result = MagicMock()
    destroy_result.summary.result = "succeeded"
    stack.destroy.return_value = destroy_result

    return stack


