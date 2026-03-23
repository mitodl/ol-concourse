"""Concourse resource type for managing Pulumi stack deployments."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from concoursetools import BuildMetadata, ConcourseResource, TypedVersion

import io_utils
import pulumi_utils


@dataclass
class PulumiVersion(TypedVersion):
    """Static version — Pulumi stacks are not polled for external changes."""

    id: str = "0"


class PulumiResource(ConcourseResource[PulumiVersion]):
    """Concourse resource for running Pulumi stack operations.

    Source configuration maps to __init__ parameters. All fields are
    optional here and can be overridden per-step via params.
    """

    def __init__(
        self,
        stack_name: str = "",
        project_name: str = "",
        source_dir: str = ".",
        env_pulumi: dict[str, str] | None = None,
        env_os: dict[str, str] | None = None,
    ) -> None:
        super().__init__(PulumiVersion)
        self.stack_name = stack_name
        self.project_name = project_name
        self.source_dir = source_dir
        self.env_pulumi: dict[str, str] = env_pulumi or {}
        self.env_os: dict[str, str] = env_os or {}

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------

    def fetch_new_versions(
        self, previous_version: PulumiVersion | None
    ) -> list[PulumiVersion]:
        """Return a static version. Triggering is handled by git resource changes."""
        return [PulumiVersion(id="0")]

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def download_version(
        self,
        version: PulumiVersion,
        destination_dir: Path,
        build_metadata: BuildMetadata,
        *,
        output_key: str | None = None,
        run_preview: bool = False,
        stack_name: str | None = None,
        project_name: str | None = None,
        source_dir: str | None = None,
        env_pulumi: dict[str, str] | None = None,
        env_os: dict[str, str] | None = None,
    ) -> tuple[PulumiVersion, dict[str, str]]:
        """Read stack outputs and optionally run a preview.

        destination_dir is this resource's own output directory; its parent
        is the job working directory that contains all fetched inputs.
        """
        effective = self._resolve_params(
            stack_name=stack_name,
            project_name=project_name,
            source_dir=source_dir,
            env_pulumi=env_pulumi,
            env_os=env_os,
        )

        _apply_os_env(effective["env_os"])

        # job working dir is the parent of this resource's destination dir
        work_dir = destination_dir.parent / effective["source_dir"]

        outputs = pulumi_utils.read_stack(
            stack_name=effective["stack_name"],
            project_name=effective["project_name"],
            source_dir=work_dir,
            env_pulumi=effective["env_pulumi"],
            output_key=output_key,
        )

        outputs_file = destination_dir / f"{effective['stack_name']}_outputs.json"
        outputs_file.write_text(json.dumps(outputs, indent=2))

        metadata: dict[str, str] = {"outputs_file": str(outputs_file)}

        if run_preview:
            preview_file = destination_dir / f"{effective['stack_name']}_preview.json"
            pulumi_utils.run_preview(
                stack_name=effective["stack_name"],
                project_name=effective["project_name"],
                source_dir=work_dir,
                env_pulumi=effective["env_pulumi"],
                output_file=preview_file,
            )
            metadata["preview_file"] = str(preview_file)

        return version, metadata

    # ------------------------------------------------------------------
    # put
    # ------------------------------------------------------------------

    def publish_new_version(
        self,
        sources_dir: Path,
        build_metadata: BuildMetadata,
        *,
        action: str,
        stack_name: str | None = None,
        project_name: str | None = None,
        source_dir: str | None = None,
        stack_config: dict[str, str] | None = None,
        preview: bool = False,
        refresh_stack: bool = True,
        env_pulumi: dict[str, str] | None = None,
        env_os: dict[str, str] | None = None,
        env_vars_from_files: dict[str, str] | None = None,
    ) -> tuple[PulumiVersion, dict[str, str]]:
        """Execute a Pulumi action against a stack.

        sources_dir is the job working directory containing all fetched inputs.
        """
        if action not in ("create", "update", "destroy"):
            raise ValueError(
                f"Invalid action '{action}'. Must be one of: create, update, destroy"
            )

        effective = self._resolve_params(
            stack_name=stack_name,
            project_name=project_name,
            source_dir=source_dir,
            env_pulumi=env_pulumi,
            env_os=env_os,
        )

        if env_vars_from_files:
            for var_name, file_path in env_vars_from_files.items():
                os.environ[var_name] = io_utils.read_value_from_file(
                    file_path, working_dir=str(sources_dir)
                )

        _apply_os_env(effective["env_os"])

        work_dir = sources_dir / effective["source_dir"]
        cfg = stack_config or {}

        metadata: dict[str, str] = {
            "action": action,
            "stack": effective["stack_name"],
        }

        if action == "destroy":
            version_id = pulumi_utils.destroy_stack(
                stack_name=effective["stack_name"],
                project_name=effective["project_name"],
                env_pulumi=effective["env_pulumi"],
                refresh_stack=refresh_stack,
            )
            metadata["result"] = "succeeded"

        elif preview:
            preview_file = work_dir / f"{effective['stack_name']}_preview.json"
            if action == "create":
                pulumi_utils.create_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                    preview=True,
                    preview_file=preview_file,
                )
            else:
                pulumi_utils.update_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                    refresh_stack=refresh_stack,
                    preview=True,
                    preview_file=preview_file,
                )
            version_id = 0
            preview_data = json.loads(preview_file.read_text())
            metadata["preview_file"] = str(preview_file)
            metadata["changes"] = json.dumps(preview_data.get("change_summary", {}))

        else:
            if action == "create":
                version_id = pulumi_utils.create_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                )
            else:
                version_id = pulumi_utils.update_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                    refresh_stack=refresh_stack,
                )
            metadata["result"] = "succeeded"

        return PulumiVersion(id=str(version_id)), metadata

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_params(
        self,
        stack_name: str | None,
        project_name: str | None,
        source_dir: str | None,
        env_pulumi: dict[str, str] | None,
        env_os: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Merge step-level overrides onto source-level defaults."""
        merged_env_pulumi = {**self.env_pulumi, **(env_pulumi or {})}
        merged_env_os = {**self.env_os, **(env_os or {})}
        return {
            "stack_name": stack_name or self.stack_name,
            "project_name": project_name or self.project_name,
            "source_dir": source_dir or self.source_dir,
            "env_pulumi": merged_env_pulumi,
            "env_os": merged_env_os,
        }


def _apply_os_env(env_vars: dict[str, str]) -> None:
    for key, value in env_vars.items():
        os.environ[key] = value
