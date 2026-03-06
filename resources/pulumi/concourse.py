"""Concourse resource for running Pulumi deployments.

Example source configuration:
  resources:
  - name: pulumi-stack
    type: pulumi
    source:
      stack_name: applications.myapp.production
      project_name: ol-infrastructure-myapp
      source_dir: src/ol_infrastructure/applications/myapp

Example put step params:
  params:
    action: update         # "create", "update", or "destroy"
    preview: false
    refresh_stack: true
    stack_config:
      aws:region: us-east-1
    env_pulumi:
      AWS_REGION: us-east-1
    env_os:
      PATH: "${PATH}:/usr/local/bin"
    env_vars_from_files:
      PULUMI_CONFIG_PASSPHRASE: path/to/passphrase

Example get step params (in):
  params:
    skip_implicit_get: false
    output_key: my_output_key   # optional: fetch a single output value
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import TypedVersion

import io_utils
import pulumi_utils


@dataclass
class PulumiVersion(TypedVersion):
    """Version type for the Pulumi resource."""

    id: str = "0"


class PulumiResource(ConcourseResource[PulumiVersion]):
    """Concourse resource for creating, updating, and destroying Pulumi stacks."""

    def __init__(
        self,
        /,
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
        self.env_pulumi = env_pulumi or {}
        self.env_os = env_os or {}

    def fetch_new_versions(
        self, previous_version: PulumiVersion | None
    ) -> list[PulumiVersion]:
        """Return a static version — Pulumi stacks don't have a meaningful version."""
        return [PulumiVersion(id="0")]

    def download_version(
        self,
        version: PulumiVersion,
        destination_dir: Path,
        build_metadata: BuildMetadata,
        *,
        skip_implicit_get: bool = False,
        stack_name: str | None = None,
        project_name: str | None = None,
        source_dir: str | None = None,
        env_pulumi: dict[str, str] | None = None,
        env_os: dict[str, str] | None = None,
        output_key: str | None = None,
    ) -> tuple[PulumiVersion, dict[str, str]]:
        """Fetch stack outputs and write them to a JSON file in destination_dir.

        :param skip_implicit_get: If True, skip stack output fetch entirely.
        :param stack_name: Override stack name from source config.
        :param project_name: Override project name from source config.
        :param source_dir: Override source directory from source config.
        :param env_pulumi: Pulumi environment variables.
        :param env_os: OS environment variables to merge.
        :param output_key: If set, fetch only this single output key.
        """
        if skip_implicit_get:
            return PulumiVersion(id="0"), {}

        resolved_stack = stack_name or self.stack_name
        resolved_project = project_name or self.project_name
        resolved_source = source_dir or self.source_dir
        resolved_env = {**self.env_pulumi, **(env_pulumi or {})}

        if env_os:
            os.environ.update(env_os)
        elif self.env_os:
            os.environ.update(self.env_os)

        stack_source_dir = destination_dir / resolved_source
        env_config = {"env_pulumi": resolved_env}

        outputs = pulumi_utils.read_stack(
            stack_name=resolved_stack,
            project_name=resolved_project,
            source_dir=stack_source_dir,
            env=env_config,
            output_key=output_key,
        )

        outputs_path = stack_source_dir / f"{resolved_stack}_outputs.json"
        outputs_path.write_text(json.dumps(outputs, indent=2))

        return PulumiVersion(id="0"), {}

    def publish_new_version(  # noqa: PLR0913
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
        """Create, update, or destroy a Pulumi stack.

        :param action: One of "create", "update", or "destroy".
        :param stack_name: Override stack name from source config.
        :param project_name: Override project name from source config.
        :param source_dir: Override source directory path from source config.
        :param stack_config: Dict of Pulumi config key-value pairs.
        :param preview: If True, run a preview only (no changes applied).
        :param refresh_stack: If True, refresh stack state before update/destroy.
        :param env_pulumi: Pulumi-specific environment variables.
        :param env_os: OS environment variables to merge.
        :param env_vars_from_files: Map of env var name → file path.
        """
        resolved_stack = stack_name or self.stack_name
        resolved_project = project_name or self.project_name
        resolved_source = source_dir or self.source_dir
        resolved_env = {**self.env_pulumi, **(env_pulumi or {})}

        if env_os:
            os.environ.update(env_os)
        elif self.env_os:
            os.environ.update(self.env_os)

        if env_vars_from_files:
            for var_name, file_path in env_vars_from_files.items():
                os.environ[var_name] = io_utils.read_value_from_file(
                    file_path, working_dir=str(sources_dir)
                )

        stack_source_dir = sources_dir / resolved_source
        env_config = {"env_pulumi": resolved_env}
        cfg = stack_config or {}

        if action == "create":
            version_id = pulumi_utils.create_stack(
                stack_name=resolved_stack,
                project_name=resolved_project,
                source_dir=stack_source_dir,
                stack_config=cfg,
                env=env_config,
                preview=preview,
            )
        elif action == "update":
            version_id = pulumi_utils.update_stack(
                stack_name=resolved_stack,
                project_name=resolved_project,
                source_dir=stack_source_dir,
                stack_config=cfg,
                env=env_config,
                refresh_stack=refresh_stack,
                preview=preview,
            )
        elif action == "destroy":
            version_id = pulumi_utils.destroy_stack(
                stack_name=resolved_stack,
                project_name=resolved_project,
                env=env_config,
                refresh_stack=refresh_stack,
            )
        else:
            msg = f'Invalid action "{action}": must be "create", "update", or "destroy"'
            raise ValueError(msg)

        return PulumiVersion(id=str(version_id)), {}


if __name__ == "__main__":
    PulumiResource.check_main()
