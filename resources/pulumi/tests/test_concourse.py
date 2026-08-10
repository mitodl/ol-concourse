"""Unit tests for the Concourse Pulumi resource."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from pulumi import automation as auto

if TYPE_CHECKING:
    from pathlib import Path
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
    _changed_properties as _render_changed_properties,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _stack_update(
    version: int = 5,
    result: str = "succeeded",
    resource_changes: dict[str, int] | None = None,
    duration_seconds: int | None = 12,
    changes: list[dict[str, Any]] | None = None,
    changes_total: int | None = None,
) -> pulumi_utils.StackUpdate:
    return pulumi_utils.StackUpdate(
        version=version,
        result=result,
        resource_changes=(
            {"same": 40, "update": 2} if resource_changes is None else resource_changes
        ),
        duration_seconds=duration_seconds,
        changes=changes or [],
        changes_total=len(changes or []) if changes_total is None else changes_total,
    )


def _make_resource_with_cap(cap: int) -> PulumiResource:
    return PulumiResource(
        stack_name="org.proj.dev",
        project_name="my-project",
        source_dir="infra",
        max_carried_changes=cap,
    )


def _make_resource(
    stack_name: str = "org.proj.dev",
    project_name: str = "my-project",
    source_dir: str = "infra/myapp",
    env_pulumi: dict[str, str] | None = None,
    env_os: dict[str, str] | None = None,
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
    detailed_diff: dict[str, Any] | None = None,
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


def _make_mock_stack(outputs: dict[str, Any] | None = None) -> MagicMock:
    """Return a mock pulumi Stack with sensible defaults."""
    stack = MagicMock()
    stack.outputs.return_value = {
        k: MagicMock(value=v)
        for k, v in (outputs or {"url": "https://example.com"}).items()
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

        with patch(
            "pulumi_utils.read_stack", return_value={"url": "https://x.com"}
        ) as mock_read:
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

        with (
            patch("pulumi_utils.read_stack", return_value={}),
            patch("pulumi_utils.run_preview") as mock_run_preview,
        ):
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

        assert mock_read.call_args.kwargs["env_pulumi"] == {
            "PULUMI_CONFIG_PASSPHRASE": "secret"
        }

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

        with (
            patch(
                "pulumi_utils.create_stack", return_value=_stack_update(1)
            ) as mock_create,
            patch("pulumi_utils.update_stack") as mock_update,
        ):
            resource.publish_new_version(tmp_path, build_metadata, action="create")

        mock_create.assert_called_once()
        mock_update.assert_not_called()

    def test_update_calls_update_stack_and_refreshes_by_default(
        self, tmp_path: Path
    ) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with (
            patch(
                "pulumi_utils.update_stack", return_value=_stack_update(5)
            ) as mock_update,
            patch("pulumi_utils.create_stack") as mock_create,
        ):
            resource.publish_new_version(tmp_path, build_metadata, action="update")

        mock_create.assert_not_called()
        mock_update.assert_called_once()
        assert mock_update.call_args.kwargs["refresh_stack"] is True

    def test_update_skips_refresh_when_disabled(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch(
            "pulumi_utils.update_stack", return_value=_stack_update(5)
        ) as mock_update:
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
                    json.dumps(
                        {"change_summary": {"update": 1}, "changes": [], "stdout": ""}
                    )
                )
            return pulumi_utils._preview_stack_update(
                {"change_summary": {"update": 1}, "changes": []}
            )

        with patch("pulumi_utils.update_stack", side_effect=fake_update) as mock_update:
            _, metadata = resource.publish_new_version(
                tmp_path, build_metadata, action="update", preview=True
            )

        assert mock_update.call_args.kwargs["preview"] is True
        assert metadata["action"] == "update"
        assert "preview_file" in metadata
        assert metadata["result"] == "preview"
        assert json.loads(metadata["resource_changes"]) == {"update": 1}

    def test_stack_config_forwarded_to_update_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch(
            "pulumi_utils.update_stack", return_value=_stack_update(5)
        ) as mock_update:
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

        with patch("pulumi_utils.update_stack", return_value=_stack_update(5)):
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

        with patch("pulumi_utils.update_stack", return_value=_stack_update(5)):
            version, _ = resource.publish_new_version(
                tmp_path, build_metadata, action="update"
            )

        assert version.id == "5"
        assert json.loads(version.summary)["version"] == "5"

    def test_metadata_includes_action_and_stack(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()
        build_metadata = MagicMock()

        with patch("pulumi_utils.update_stack", return_value=_stack_update(5)):
            _, metadata = resource.publish_new_version(
                tmp_path, build_metadata, action="update"
            )

        assert metadata["action"] == "update"
        assert metadata["stack"] == "org.proj.dev"
        assert metadata["result"] == "succeeded"

    def test_cancel_action_calls_cancel_stack_lock(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        build_metadata = MagicMock()

        with patch("pulumi_utils.cancel_stack_lock") as mock_cancel:
            version, metadata = resource.publish_new_version(
                tmp_path, build_metadata, action="cancel"
            )

        mock_cancel.assert_called_once_with(
            stack_name="org.proj.dev",
            project_name="my-project",
            env_pulumi={},
        )
        assert version == PulumiVersion(id="0")
        assert metadata["action"] == "cancel"
        assert metadata["result"] == "cancelled"

    def test_cancel_action_does_not_call_update_or_create(self, tmp_path: Path) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        build_metadata = MagicMock()

        with (
            patch("pulumi_utils.cancel_stack_lock"),
            patch("pulumi_utils.update_stack") as mock_update,
            patch("pulumi_utils.create_stack") as mock_create,
        ):
            resource.publish_new_version(tmp_path, build_metadata, action="cancel")

        mock_update.assert_not_called()
        mock_create.assert_not_called()

    def test_cancel_action_applies_env_vars_from_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PULUMI_ACCESS_TOKEN", raising=False)
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")

        token_file = tmp_path / "token"
        token_file.write_text("s-my-token\n")
        build_metadata = MagicMock()

        with patch("pulumi_utils.cancel_stack_lock"):
            resource.publish_new_version(
                tmp_path,
                build_metadata,
                action="cancel",
                env_vars_from_files={"PULUMI_ACCESS_TOKEN": "token"},
            )

        assert os.environ["PULUMI_ACCESS_TOKEN"] == "s-my-token"


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
        assert result["detailed_diff"]["tags"] == {
            "diff_kind": "update",
            "input_diff": True,
        }
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
            detailed_diff={
                "id": PropertyDiff(DiffKind.DELETE_REPLACE, input_diff=True)
            },
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
        mock_stack.preview.return_value.stdout = (
            "  ~ aws:s3:Bucket  my-bucket  update\n"
        )
        output_file = tmp_path / "preview.json"

        pulumi_utils._run_preview_on_stack(mock_stack, output_file)

        data = json.loads(output_file.read_text())
        assert data["stdout"] == "  ~ aws:s3:Bucket  my-bucket  update\n"

    def test_no_output_file_returns_payload_without_writing(
        self, tmp_path: Path
    ) -> None:
        mock_stack = _make_mock_stack()
        payload = pulumi_utils._run_preview_on_stack(mock_stack, output_file=None)

        assert "change_summary" in payload
        assert "changes" in payload
        assert "stdout" in payload
        assert not list(tmp_path.iterdir())


class TestDeploySummary:
    """The deploy summary is what makes a promotion gate judgeable on evidence.

    Closing the `[bot] Pulumi <project> <stack> deployed.` issue promotes the
    change to the next environment, and until now that issue carried only a
    title -- so the human closing it was trusting the job's colour. These pin
    the summary's trip from the Pulumi run to the issue body.
    """

    def test_publish_carries_summary_on_the_version(self, tmp_path: Path) -> None:
        """The put's version carries the summary; metadata alone cannot.

        Concourse metadata is only ever displayed in its own UI. Riding on the
        version is what lets the implicit get hand the summary to a later step.
        """
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()

        with patch(
            "pulumi_utils.update_stack",
            return_value=_stack_update(9, resource_changes={"update": 2, "same": 40}),
        ):
            version, metadata = resource.publish_new_version(
                tmp_path, MagicMock(), action="update"
            )

        summary = json.loads(version.summary)
        assert summary["version"] == "9"
        assert summary["result"] == "succeeded"
        assert json.loads(summary["resource_changes"]) == {"update": 2, "same": 40}
        # ...and in metadata too, so it is visible in the Concourse UI as well.
        assert metadata["result"] == "succeeded"
        assert json.loads(metadata["resource_changes"]) == {"update": 2, "same": 40}

    def test_get_writes_summary_file_without_reading_the_stack(
        self, tmp_path: Path
    ) -> None:
        """read_outputs=False must not invoke Pulumi.

        This get runs on the success path of every deploy. A stack read here
        would be a second Pulumi invocation whose failure would redden a deploy
        that actually worked.
        """
        resource = _make_resource()
        version = PulumiVersion(
            id="9", summary=json.dumps(_stack_update(9).to_flat_dict())
        )

        with patch("pulumi_utils.read_stack") as mock_read:
            _, metadata = resource.download_version(
                version,
                tmp_path,
                MagicMock(),
                summary_file="deploy_summary.md",
                read_outputs=False,
            )

        mock_read.assert_not_called()
        body = (tmp_path / "deploy_summary.md").read_text()
        assert "Pulumi resource summary" in body
        assert "| update | 2 |" in body
        assert "| same | 40 |" in body
        assert metadata["summary_file"].endswith("deploy_summary.md")

    def test_zero_count_ops_are_reported_as_zero(self, tmp_path: Path) -> None:
        """Pulumi omits zero-count ops, so absence must render as 0, not vanish."""
        resource = _make_resource()
        version = PulumiVersion(
            id="9",
            summary=json.dumps(
                _stack_update(9, resource_changes={"same": 40}).to_flat_dict()
            ),
        )

        resource.download_version(
            version,
            tmp_path,
            MagicMock(),
            summary_file="deploy_summary.md",
            read_outputs=False,
        )

        body = (tmp_path / "deploy_summary.md").read_text()
        for op in ("create", "update", "replace", "delete"):
            assert f"| {op} | 0 |" in body

    def test_missing_summary_renders_a_do_not_promote_warning(
        self, tmp_path: Path
    ) -> None:
        """A green job with no summary is build 158's shape -- it must scream.

        deploy-ol-substructure-keycloak build 158 reported success having run no
        Pulumi at all. A body that merely omitted the counts would read as
        "nothing to report"; it has to read as "do not trust this".
        """
        resource = _make_resource()

        resource.download_version(
            PulumiVersion(id="0"),
            tmp_path,
            MagicMock(),
            summary_file="deploy_summary.md",
            read_outputs=False,
        )

        body = (tmp_path / "deploy_summary.md").read_text()
        assert "No Pulumi resource summary was recorded" in body
        assert "Do not close this issue" in body

    def test_errored_update_says_do_not_promote(self, tmp_path: Path) -> None:
        """The exact case build 158 hid: `2 errored` must be visible in the body."""
        resource = _make_resource()
        version = PulumiVersion(
            id="0",
            summary=json.dumps(
                _stack_update(
                    0, result="failed", resource_changes={"errored": 2}
                ).to_flat_dict()
            ),
        )

        resource.download_version(
            version,
            tmp_path,
            MagicMock(),
            summary_file="deploy_summary.md",
            read_outputs=False,
        )

        body = (tmp_path / "deploy_summary.md").read_text()
        assert "| errored | 2 |" in body
        assert "Do not promote it." in body

    def test_normal_get_still_reads_outputs(self, tmp_path: Path) -> None:
        """The default get is unchanged -- it exists to fetch stack outputs."""
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")

        with patch("pulumi_utils.read_stack", return_value={"key": "value"}) as mock:
            _, metadata = resource.download_version(
                PulumiVersion(id="9"), tmp_path, MagicMock()
            )

        mock.assert_called_once()
        assert metadata["outputs_file"].endswith("org.proj.dev_outputs.json")


class TestSummarizeUpResult:
    """`UpdateSummary.start_time`/`end_time` are datetimes, NOT strings.

    The automation API parses the wire format's RFC 3339 timestamps before we
    see them (`start_time: datetime`, `end_time: Optional[datetime]` in
    pulumi 3.253's `UpdateSummary.__init__`). Feeding these tests strings would
    let a string-parsing implementation pass here and then raise TypeError in
    production on the success path of every deploy -- after `pulumi up` has
    already applied. Keep them as real datetimes.
    """

    def test_duration_computed_from_start_and_end(self) -> None:
        result = MagicMock()
        result.summary.version = 4
        result.summary.result = "succeeded"
        result.summary.resource_changes = {"create": 1}
        result.summary.start_time = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
        result.summary.end_time = datetime(2020, 1, 1, 0, 2, 30, tzinfo=UTC)

        assert pulumi_utils.summarize_up_result(result).duration_seconds == 150

    def test_real_pulumi_update_summary_type_is_accepted(self) -> None:
        """Pin against the actual SDK class, not a MagicMock of it.

        A MagicMock accepts any attribute type, so it cannot catch the
        wrong-type assumption on its own -- constructing the genuine
        UpdateSummary is what makes this test load-bearing.
        """
        summary = auto.UpdateSummary(
            kind="update",
            start_time=datetime(2026, 8, 6, 22, 0, 0, tzinfo=UTC),
            message="",
            environment={},
            config={},
            result="succeeded",
            end_time=datetime(2026, 8, 6, 22, 1, 34, tzinfo=UTC),
            version=42,
            resource_changes={"update": 3, "same": 118},
        )
        result = MagicMock()
        result.summary = summary

        stack_update = pulumi_utils.summarize_up_result(result)
        assert stack_update.duration_seconds == 94
        assert stack_update.version == 42
        assert stack_update.result == "succeeded"
        assert stack_update.resource_changes == {"update": 3, "same": 118}

    def test_missing_end_time_leaves_duration_unset(self) -> None:
        """end_time is Optional -- an update still in progress has none."""
        result = MagicMock()
        result.summary.version = 4
        result.summary.result = "succeeded"
        result.summary.resource_changes = {"create": 1}
        result.summary.start_time = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
        result.summary.end_time = None

        summary = pulumi_utils.summarize_up_result(result)
        assert summary.duration_seconds is None
        assert "duration_seconds" not in summary.to_flat_dict()

    def test_missing_timestamps_leave_duration_unset(self) -> None:
        result = MagicMock()
        result.summary.version = 4
        result.summary.result = "succeeded"
        result.summary.resource_changes = {"create": 1}
        result.summary.start_time = None
        result.summary.end_time = None

        summary = pulumi_utils.summarize_up_result(result)
        assert summary.duration_seconds is None
        assert "duration_seconds" not in summary.to_flat_dict()


def _event(
    operation: str = "update",
    name: str = "witan-vmcp",
    resource_type: str = "keycloak:openid/client:Client",
    diffs: list[str] | None = None,
    detailed_diff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "urn": f"urn:pulumi:CI::ol-substructure-keycloak::{resource_type}::{name}",
        "type": resource_type,
        "diffs": diffs if diffs is not None else [],
        "detailed_diff": detailed_diff if detailed_diff is not None else {},
    }


class TestRenderedDiff:
    """The gate issue must say *what* changed, not only how many things did.

    `update: 1` is the same number whether a Keycloak client gained a
    description or lost a redirect URI, and only one of those should be
    promoted without a second look.
    """

    def _body(self, tmp_path: Path, **kw: Any) -> str:
        update = _stack_update(**kw)
        version = PulumiVersion(id="42", summary=json.dumps(update.to_flat_dict()))
        _make_resource().download_version(
            version,
            tmp_path,
            MagicMock(),
            summary_file="deploy_summary.md",
            read_outputs=False,
        )
        return (tmp_path / "deploy_summary.md").read_text()

    def test_names_each_changed_resource_and_its_type(self, tmp_path: Path) -> None:
        body = self._body(
            tmp_path,
            changes=[_event(operation="create", name="lakekeeper-api")],
        )
        assert "### What changed" in body
        assert "`lakekeeper-api`" in body
        assert "`keycloak:openid/client:Client`" in body

    def test_detailed_diff_property_paths_are_shown(self, tmp_path: Path) -> None:
        """The specific properties are the actual review material."""
        body = self._body(
            tmp_path,
            changes=[
                _event(
                    detailed_diff={
                        "validRedirectUris[1]": {
                            "diff_kind": "delete",
                            "input_diff": True,
                        }
                    }
                )
            ],
        )
        assert "`validRedirectUris[1]` (delete)" in body

    def test_falls_back_to_coarse_diffs_when_no_detailed_diff(
        self, tmp_path: Path
    ) -> None:
        """Not every provider supplies a detailed_diff; don't lose the field name."""
        body = self._body(tmp_path, changes=[_event(diffs=["tags"], detailed_diff={})])
        assert "`tags`" in body

    def test_destructive_operations_are_listed_first(self, tmp_path: Path) -> None:
        """A reviewer scanning the body should hit deletes before creates."""
        body = self._body(
            tmp_path,
            changes=[
                _event(operation="create", name="new-client"),
                _event(operation="delete", name="legacy-admin"),
                _event(operation="update", name="edited"),
            ],
        )
        assert body.index("<b>delete</b>") < body.index("<b>create</b>")
        assert body.index("<b>create</b>") < body.index("<b>update</b>")

    def test_operations_are_grouped_with_counts(self, tmp_path: Path) -> None:
        body = self._body(
            tmp_path,
            changes=[
                _event(operation="update", name="a"),
                _event(operation="update", name="b"),
            ],
        )
        assert "<b>update</b> (2)" in body

    def test_unanticipated_operation_is_still_rendered(self, tmp_path: Path) -> None:
        """A new Pulumi op must not be silently dropped from the body."""
        body = self._body(
            tmp_path, changes=[_event(operation="import", name="adopted")]
        )
        assert "<b>import</b>" in body
        assert "`adopted`" in body

    def test_no_changes_section_when_there_are_no_changes(self, tmp_path: Path) -> None:
        body = self._body(tmp_path, changes=[])
        assert "### What changed" not in body
        assert "Pulumi resource summary" in body

    def test_long_change_list_is_capped_and_says_so(self, tmp_path: Path) -> None:
        """Truncation must be visible.

        A shortened list that reads as complete is exactly the failure this
        feature exists to prevent, and a 65536-char issue body would fail the
        put outright.
        """
        body = self._body(
            tmp_path,
            changes=[_event(name=f"client-{i}") for i in range(200)],
            changes_total=250,
        )
        assert "Showing 200 of 250 changed resources" in body


class TestChangeCaptureFromUp:
    def test_same_resources_are_filtered_out(self) -> None:
        """`same` is the bulk of any update and is noise in a review."""
        result = MagicMock()
        result.summary.version = 4
        result.summary.result = "succeeded"
        result.summary.resource_changes = {"update": 1, "same": 118}
        result.summary.start_time = None
        result.summary.end_time = None

        changed = MagicMock()
        changed.metadata.op = OpType.UPDATE
        changed.metadata.urn = "urn:pulumi:CI::proj::aws:s3/bucket:Bucket::keep"
        changed.metadata.type = "aws:s3/bucket:Bucket"
        changed.metadata.diffs = ["tags"]
        changed.metadata.detailed_diff = None

        unchanged = MagicMock()
        unchanged.metadata.op = OpType.SAME

        stack_update = pulumi_utils.summarize_up_result(result, [changed, unchanged])

        assert len(stack_update.changes) == 1
        assert stack_update.changes[0]["operation"] == "update"
        assert stack_update.changes[0]["urn"].endswith("::keep")

    def test_collector_callback_accumulates_resource_events(self) -> None:
        events, on_event = pulumi_utils._collect_resource_events()

        with_resource = MagicMock()
        with_resource.resource_pre_event.metadata = MagicMock()
        without_resource = MagicMock()
        without_resource.resource_pre_event = None

        on_event(with_resource)
        on_event(without_resource)

        assert events == [with_resource.resource_pre_event]

    def test_changes_absent_from_flat_dict_when_empty(self) -> None:
        """Don't put an empty `changes` key on the version for nothing."""
        assert "changes" not in _stack_update(changes=[]).to_flat_dict()

    def test_carried_changes_are_capped_but_total_is_honest(self) -> None:
        """The change list rides on the Concourse *version*, which is persisted.

        Concourse stores versions per-resource in its database and carries them
        through every later step, so an unbounded list would put megabytes there
        on a large refactor. Cap what is carried, but keep the true count so the
        rendered body can say how much it is not showing.
        """
        result = MagicMock()
        result.summary.version = 4
        result.summary.result = "succeeded"
        result.summary.resource_changes = {"update": 500}
        result.summary.start_time = None
        result.summary.end_time = None

        events = []
        for i in range(500):
            evt = MagicMock()
            evt.metadata.op = OpType.UPDATE
            evt.metadata.urn = f"urn:pulumi:CI::proj::aws:s3/bucket:Bucket::b{i}"
            evt.metadata.type = "aws:s3/bucket:Bucket"
            evt.metadata.diffs = []
            evt.metadata.detailed_diff = None
            events.append(evt)

        stack_update = pulumi_utils.summarize_up_result(result, events)

        assert len(stack_update.changes) == pulumi_utils.DEFAULT_MAX_CARRIED_CHANGES
        assert stack_update.changes_total == 500
        assert json.loads(stack_update.to_flat_dict()["changes_total"]) == 500


class TestMaxCarriedChangesIsConfigurable:
    """The cap must be tunable from the pipeline, not only from this code.

    Baked into a constant, changing it would mean editing this image, cutting a
    release, and bumping the dependency in every consumer. As a `source` field
    (or per-put param) it is a pipeline re-set instead, which is the difference
    between "we can try a bigger number tomorrow" and "we can try it next
    release".
    """

    def _events(self, count: int) -> list[MagicMock]:
        events = []
        for i in range(count):
            evt = MagicMock()
            evt.metadata.op = OpType.UPDATE
            evt.metadata.urn = f"urn:pulumi:CI::proj::aws:s3/bucket:Bucket::b{i}"
            evt.metadata.type = "aws:s3/bucket:Bucket"
            evt.metadata.diffs = []
            evt.metadata.detailed_diff = None
            events.append(evt)
        return events

    def _up_result(self) -> MagicMock:
        result = MagicMock()
        result.summary.version = 4
        result.summary.result = "succeeded"
        result.summary.resource_changes = {"update": 10}
        result.summary.start_time = None
        result.summary.end_time = None
        return result

    def test_source_level_value_is_used(self, tmp_path: Path) -> None:
        resource = _make_resource_with_cap(3)
        (tmp_path / "infra").mkdir(exist_ok=True)

        captured: dict[str, Any] = {}

        def fake_update(**kwargs: Any) -> pulumi_utils.StackUpdate:
            captured.update(kwargs)
            return _stack_update()

        with patch("pulumi_utils.update_stack", side_effect=fake_update):
            resource.publish_new_version(tmp_path, MagicMock(), action="update")

        assert captured["max_carried_changes"] == 3

    def test_put_param_overrides_source(self, tmp_path: Path) -> None:
        resource = _make_resource_with_cap(3)
        (tmp_path / "infra").mkdir(exist_ok=True)

        captured: dict[str, Any] = {}

        def fake_update(**kwargs: Any) -> pulumi_utils.StackUpdate:
            captured.update(kwargs)
            return _stack_update()

        with patch("pulumi_utils.update_stack", side_effect=fake_update):
            resource.publish_new_version(
                tmp_path, MagicMock(), action="update", max_carried_changes=50
            )

        assert captured["max_carried_changes"] == 50

    def test_zero_means_no_cap_and_is_not_swallowed_as_falsy(
        self, tmp_path: Path
    ) -> None:
        """`or`-style defaulting would turn an explicit 0 back into 200."""
        resource = _make_resource_with_cap(200)
        (tmp_path / "infra").mkdir(exist_ok=True)

        captured: dict[str, Any] = {}

        def fake_update(**kwargs: Any) -> pulumi_utils.StackUpdate:
            captured.update(kwargs)
            return _stack_update()

        with patch("pulumi_utils.update_stack", side_effect=fake_update):
            resource.publish_new_version(
                tmp_path, MagicMock(), action="update", max_carried_changes=0
            )

        assert captured["max_carried_changes"] == 0

    def test_zero_carries_every_change(self) -> None:
        stack_update = pulumi_utils.summarize_up_result(
            self._up_result(), self._events(10), max_carried_changes=0
        )
        assert len(stack_update.changes) == 10
        assert stack_update.changes_total == 10

    def test_custom_cap_truncates_but_keeps_total(self) -> None:
        stack_update = pulumi_utils.summarize_up_result(
            self._up_result(), self._events(10), max_carried_changes=4
        )
        assert len(stack_update.changes) == 4
        assert stack_update.changes_total == 10

    def test_string_from_pipeline_yaml_is_coerced(self) -> None:
        """Concourse source/params values can arrive as strings."""
        resource = PulumiResource(
            stack_name="org.proj.dev",
            project_name="p",
            source_dir="infra",
            max_carried_changes="7",
        )
        assert resource.max_carried_changes == 7


class TestNextEnvironmentPreview:
    """The gate body's second half: what promoting will do to the next stack.

    Additive to the applied diff, never a replacement — a preview of QA says
    nothing about whether the CI deploy actually happened, which is the failure
    (build 158) this whole feature exists to catch.
    """

    def _render(self, update: Any, tmp_path: Path, stack: str = "QA") -> str:
        version = PulumiVersion(id="0", summary=json.dumps(update.to_flat_dict()))
        _make_resource().download_version(
            version,
            tmp_path,
            MagicMock(),
            summary_file="preview.md",
            read_outputs=False,
            preview_stack=stack,
        )
        return (tmp_path / "preview.md").read_text()

    def _preview(self, changes: list[dict[str, Any]], summary: dict[str, int]) -> Any:
        return pulumi_utils._preview_stack_update(
            {"change_summary": summary, "changes": changes}
        )

    def test_reads_as_a_prediction_not_a_record(self, tmp_path: Path) -> None:
        """A gate can sit open for days; false confidence is worse than silence."""
        body = self._render(self._preview([], {}), tmp_path=tmp_path)
        assert "prediction, not a guarantee" in body

    def test_names_the_target_stack(self, tmp_path: Path) -> None:
        body = self._render(self._preview([], {}), tmp_path=tmp_path)
        assert "`QA`" in body

    def test_shows_drift_the_applied_diff_cannot(self, tmp_path: Path) -> None:
        """The whole point: a QA-only resource the CI deploy never touched."""
        drift = {
            "operation": "delete",
            "urn": "urn:pulumi:QA::proj::keycloak:index/role:Role::stale-qa-only-role",
            "type": "keycloak:index/role:Role",
            "diffs": [],
            "detailed_diff": {},
        }
        body = self._render(self._preview([drift], {"delete": 1}), tmp_path=tmp_path)
        assert "stale-qa-only-role" in body
        assert "<b>delete</b>" in body

    def test_no_changes_says_so_explicitly(self, tmp_path: Path) -> None:
        body = self._render(self._preview([], {}), tmp_path=tmp_path)
        assert "No changes" in body

    def test_failed_preview_does_not_impugn_the_deploy(self, tmp_path: Path) -> None:
        """The deploy already applied. The body must not imply it is suspect."""
        failed = pulumi_utils.preview_failed("connection to sso-qa timed out")
        body = self._render(failed, tmp_path=tmp_path)
        assert "Could not preview" in body
        assert "does not mean anything is wrong with what was just deployed" in body
        assert "connection to sso-qa timed out" in body

    def test_preview_carries_its_changes_on_the_version(self) -> None:
        """Same transport as the applied diff -- the version, not metadata."""
        update = self._preview(
            [{"operation": "update", "urn": "urn:a::b::t::n", "type": "t"}],
            {"update": 1},
        )
        flat = update.to_flat_dict()
        assert flat["result"] == "preview"
        assert json.loads(flat["changes"])[0]["urn"] == "urn:a::b::t::n"
        assert update.version == 0, "a preview creates no stack version"

    def test_preview_respects_the_carried_change_cap(self) -> None:
        many = [
            {"operation": "update", "urn": f"urn:a::b::t::n{i}", "type": "t"}
            for i in range(50)
        ]
        update = pulumi_utils._preview_stack_update(
            {"change_summary": {"update": 50}, "changes": many}, max_carried_changes=10
        )
        assert len(update.changes) == 10
        assert update.changes_total == 50

    def test_no_op_preview_reporting_only_same_says_no_changes(
        self, tmp_path: Path
    ) -> None:
        """Pulumi reports a genuine no-op as {"same": N}, not an empty summary.

        Counting `same` as material would leave the reader a table of zeros to
        interpret instead of being told plainly there is nothing to do.
        """
        body = self._render(self._preview([], {"same": 118}), tmp_path=tmp_path)
        assert "No changes" in body

    def test_zero_valued_ops_do_not_count_as_material(self, tmp_path: Path) -> None:
        body = self._render(
            self._preview([], {"same": 118, "update": 0}), tmp_path=tmp_path
        )
        assert "No changes" in body

    def test_real_changes_still_suppress_the_no_changes_message(
        self, tmp_path: Path
    ) -> None:
        body = self._render(
            self._preview([], {"same": 118, "update": 2}), tmp_path=tmp_path
        )
        assert "No changes" not in body


class TestPreviewFailureTolerance:
    """A gate preview runs AFTER the deploy applied. It must never report red.

    This is the same hazard `read_outputs: false` exists to avoid: a failure on
    the success path would say the deploy is broken when the infrastructure is
    live and correct.
    """

    def test_fail_on_error_false_degrades_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()

        with patch("pulumi_utils.update_stack", side_effect=RuntimeError("boom")):
            version, metadata = resource.publish_new_version(
                tmp_path,
                MagicMock(),
                action="update",
                preview=True,
                fail_on_error=False,
            )

        assert json.loads(version.summary)["result"] == "preview-failed"
        assert "boom" in json.loads(version.summary)["error"]
        assert metadata["result"] == "preview-failed"

    def test_fail_on_error_true_still_raises(self, tmp_path: Path) -> None:
        """A preview a pipeline genuinely gates on must still be able to fail."""
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()

        with (
            patch("pulumi_utils.update_stack", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            resource.publish_new_version(
                tmp_path, MagicMock(), action="update", preview=True
            )

    def test_a_real_update_is_unaffected_by_the_flag(self, tmp_path: Path) -> None:
        """fail_on_error must not accidentally swallow a failed deploy."""
        resource = _make_resource(stack_name="org.proj.dev", source_dir="infra")
        (tmp_path / "infra").mkdir()

        with (
            patch(
                "pulumi_utils.update_stack", side_effect=RuntimeError("real failure")
            ),
            pytest.raises(RuntimeError, match="real failure"),
        ):
            resource.publish_new_version(
                tmp_path, MagicMock(), action="update", fail_on_error=False
            )


class TestOpMapKeysAreStrings:
    """`SummaryEvent.resource_changes` is annotated OpMap but yields plain strings.

    Assuming the annotation crashed a live EKS gate preview with
    `'str' object has no attribute 'value'`. The automation API does NOT coerce
    these keys, unlike StepEventMetadata.op and PropertyDiff.diff_kind, which
    genuinely are parsed into enums.
    """

    def test_string_keys_do_not_crash(self) -> None:
        assert pulumi_utils._op_counts({"same": 10, "update": 2}) == {
            "same": 10,
            "update": 2,
        }

    def test_enum_keys_still_work(self) -> None:
        assert pulumi_utils._op_counts({OpType.CREATE: 1, OpType.SAME: 5}) == {
            "create": 1,
            "same": 5,
        }

    def test_none_summary_is_empty(self) -> None:
        assert pulumi_utils._op_counts(None) == {}

    def test_preview_payload_with_string_keys_renders(self, tmp_path: Path) -> None:
        """The end-to-end shape that failed: preview -> version -> rendered body."""
        update = pulumi_utils._preview_stack_update(
            {"change_summary": {"same": 10, "update": 2}, "changes": []}
        )
        version = PulumiVersion(id="0", summary=json.dumps(update.to_flat_dict()))
        _make_resource().download_version(
            version,
            tmp_path,
            MagicMock(),
            summary_file="p.md",
            read_outputs=False,
            preview_stack="residential.Production",
        )
        body = (tmp_path / "p.md").read_text()
        assert "residential.Production" in body
        assert "| update | 2 |" in body


class TestDiffValues:
    """A property name alone is thin review material.

    `version (update)` does not say whether a patch bump or a major downgrade is
    about to be promoted. The old -> new values are the actual review.
    """

    @staticmethod
    def _event(detailed, old_inputs, new_inputs):
        meta = MagicMock()
        meta.op = OpType.UPDATE
        meta.urn = "urn:pulumi:QA::proj::eks:index/nodeGroup:NodeGroup::ng"
        meta.type = "eks:index/nodeGroup:NodeGroup"
        meta.diffs = list(detailed)
        meta.detailed_diff = {
            k: MagicMock(diff_kind=v, input_diff=True) for k, v in detailed.items()
        }
        meta.old.inputs = old_inputs
        meta.new.inputs = new_inputs
        evt = MagicMock()
        evt.metadata = meta
        return evt

    def test_scalar_change_shows_both_sides(self) -> None:
        ser = pulumi_utils.serialize_resource_event(
            self._event(
                {"version": DiffKind.UPDATE}, {"version": "1.31"}, {"version": "1.32"}
            )
        )
        assert ser["detailed_diff"]["version"]["old"] == "1.31"
        assert ser["detailed_diff"]["version"]["new"] == "1.32"

    def test_rendered_as_an_arrow(self, tmp_path: Path) -> None:
        ser = pulumi_utils.serialize_resource_event(
            self._event(
                {"version": DiffKind.UPDATE}, {"version": "1.31"}, {"version": "1.32"}
            )
        )
        line = _render_changed_properties(ser)[0]
        assert "`1.31` → `1.32`" in line

    def test_nested_and_indexed_paths_resolve(self) -> None:
        ser = pulumi_utils.serialize_resource_event(
            self._event(
                {"labels.tier": DiffKind.ADD, "uris[1]": DiffKind.UPDATE},
                {"labels": {}, "uris": ["a", "b"]},
                {"labels": {"tier": "spot"}, "uris": ["a", "c"]},
            )
        )
        assert ser["detailed_diff"]["labels.tier"]["new"] == "spot"
        assert ser["detailed_diff"]["uris[1]"]["old"] == "b"
        assert ser["detailed_diff"]["uris[1]"]["new"] == "c"

    def test_add_has_no_old_side(self) -> None:
        ser = pulumi_utils.serialize_resource_event(
            self._event({"newProp": DiffKind.ADD}, {}, {"newProp": "x"})
        )
        assert "old" not in ser["detailed_diff"]["newProp"]
        assert ser["detailed_diff"]["newProp"]["new"] == "x"

    def test_pulumi_filtered_secret_is_shown_as_redacted(self) -> None:
        """Values reach a GitHub issue, so a leaked secret would be published."""
        ser = pulumi_utils.serialize_resource_event(
            self._event(
                {"apiToken": DiffKind.UPDATE},
                {"apiToken": "[secret]"},
                {"apiToken": "[secret]"},
            )
        )
        assert ser["detailed_diff"]["apiToken"]["new"] == "«redacted by Pulumi»"

    def test_long_values_are_truncated(self) -> None:
        ser = pulumi_utils.serialize_resource_event(
            self._event(
                {"policy": DiffKind.UPDATE},
                {"policy": "X" * 900},
                {"policy": "Y" * 900},
            )
        )
        new = ser["detailed_diff"]["policy"]["new"]
        assert new.endswith("…(truncated)")
        assert len(new) < 200

    def test_backticks_and_newlines_cannot_break_the_markdown(
        self, tmp_path: Path
    ) -> None:
        """A value is untrusted text inside an inline-code span in an issue body."""
        ser = pulumi_utils.serialize_resource_event(
            self._event(
                {"script": DiffKind.UPDATE},
                {"script": "a`b"},
                {"script": "line1\nline2"},
            )
        )
        line = _render_changed_properties(ser)[0]
        assert "`" in line
        assert "\n" not in line
        assert "a'b" in line


class TestPropertyPathParsing:
    """Pulumi quotes a property key precisely when it contains dots or slashes.

    `labels["app.kubernetes.io/name"]` is the everyday Kubernetes case, and it
    is exactly what the EKS gates will see. Splitting on dots shredded it into
    meaningless fragments and the value vanished from the diff — worse than an
    error, because the gate still rendered and just quietly said less.
    """

    def test_dotted_kubernetes_label_key_resolves(self) -> None:
        inputs = {"labels": {"app.kubernetes.io/name": "omnigraph"}}
        got = pulumi_utils._resolve_property_path(
            inputs, 'labels["app.kubernetes.io/name"]'
        )
        assert got == "omnigraph"

    def test_plain_dotted_path_still_resolves(self) -> None:
        assert (
            pulumi_utils._resolve_property_path(
                {"labels": {"tier": "spot"}}, "labels.tier"
            )
            == "spot"
        )

    def test_numeric_index_stays_a_list_index(self) -> None:
        assert (
            pulumi_utils._resolve_property_path({"uris": ["a", "b"]}, "uris[1]") == "b"
        )

    def test_escaped_quote_inside_a_key(self) -> None:
        inputs = {"q": {'we"ird': 1}}
        assert pulumi_utils._resolve_property_path(inputs, 'q["we\\"ird"]') == 1

    def test_quoted_key_followed_by_index(self) -> None:
        inputs = {"m": {"a.b": ["x", "y"]}}
        assert pulumi_utils._resolve_property_path(inputs, 'm["a.b"][1]') == "y"

    def test_missing_path_is_missing_not_an_error(self) -> None:
        assert (
            pulumi_utils._resolve_property_path({}, "nope.missing")
            is pulumi_utils._MISSING
        )

    def test_index_past_the_end_is_missing(self) -> None:
        assert (
            pulumi_utils._resolve_property_path({"uris": ["a"]}, "uris[9]")
            is pulumi_utils._MISSING
        )

    def test_dotted_label_value_reaches_the_rendered_diff(self) -> None:
        """End-to-end: the regression case Copilot asked to pin."""
        meta = MagicMock()
        meta.op = OpType.UPDATE
        meta.urn = "urn:pulumi:QA::p::k8s::deploy"
        meta.type = "kubernetes:apps/v1:Deployment"
        meta.diffs = ['labels["app.kubernetes.io/version"]']
        meta.detailed_diff = {
            'labels["app.kubernetes.io/version"]': MagicMock(
                diff_kind=DiffKind.UPDATE, input_diff=True
            )
        }
        meta.old.inputs = {"labels": {"app.kubernetes.io/version": "1.2.3"}}
        meta.new.inputs = {"labels": {"app.kubernetes.io/version": "1.2.4"}}
        evt = MagicMock()
        evt.metadata = meta

        line = _render_changed_properties(pulumi_utils.serialize_resource_event(evt))[0]
        assert "`1.2.3` → `1.2.4`" in line


class TestPropertyPathIsSanitized:
    """The path is as untrusted as the value.

    A key is quoted because it holds structural characters; nothing stops one
    holding a backtick or newline, which would terminate the inline-code span
    and inject Markdown into the published issue body.
    """

    @staticmethod
    def _line(path: str) -> str:
        event = {
            "operation": "update",
            "urn": "urn:pulumi:QA::p::t::n",
            "type": "t",
            "diffs": [path],
            "detailed_diff": {path: {"diff_kind": "update", "new": "x"}},
        }
        return _render_changed_properties(event)[0]

    def test_backtick_in_path_cannot_break_the_span(self) -> None:
        line = self._line('labels["a`b"]')
        assert "a'b" in line
        assert "`a`b`" not in line

    def test_newline_in_path_cannot_break_the_list_item(self) -> None:
        assert "\n" not in self._line('labels["a\nb"]')
