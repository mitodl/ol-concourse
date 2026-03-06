"""Concourse resource for running Packer builds.

Example source configuration:
  resources:
  - name: packer-build
    type: packer
    source: {}

Example put step params:
  params:
    objective: build        # "validate" (default) or "build"
    template: path/to/template.pkr.hcl
    var_files: [vars.pkrvars.hcl]
    vars:
      my_var: my_value
    vars_from_files:
      ami_name: path/to/file
    env_vars:
      AWS_DEFAULT_REGION: us-east-1
    env_vars_from_files:
      AWS_SESSION_TOKEN: path/to/token_file
    only: [amazon-ebs.web]
    excepts: []
    force: false
    debug: false
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from concoursetools import BuildMetadata
from concoursetools.additional import OutOnlyConcourseResource
from concoursetools.version import TypedVersion

import io_utils
import packer as packer_lib


@dataclass
class PackerVersion(TypedVersion):
    """Version type for the packer resource.

    Packer is an out-only resource; version is static unless a build produces
    a real artifact ID.
    """

    id: str = "0"


class PackerResource(OutOnlyConcourseResource[PackerVersion]):
    """Concourse resource for running Packer validate and build operations."""

    def __init__(self) -> None:
        super().__init__(PackerVersion)

    def publish_new_version(  # noqa: PLR0913
        self,
        sources_dir: Path,
        build_metadata: BuildMetadata,
        *,
        objective: str = "validate",
        template: str,
        var_files: list[str] | None = None,
        vars: dict[str, str] | None = None,  # noqa: A002
        vars_from_files: dict[str, str] | None = None,
        env_vars: dict[str, str] | None = None,
        env_vars_from_files: dict[str, str] | None = None,
        only: list[str] | None = None,
        excepts: list[str] | None = None,
        force: bool = False,
        debug: bool = False,
    ) -> tuple[PackerVersion, dict[str, str]]:
        """Run a Packer validate or build operation.

        :param objective: "validate" (default) or "build".
        :param template: Path (relative to sources_dir) to the Packer template.
        :param var_files: Paths to Packer var files.
        :param vars: Dictionary of Packer variables.
        :param vars_from_files: Map of var name → file path to read value from.
        :param env_vars: Environment variables to set before running Packer.
        :param env_vars_from_files: Map of env var name → file path.
        :param only: List of source names to build exclusively.
        :param excepts: List of source names to skip.
        :param force: Pass -force to Packer build.
        :param debug: Enable verbose argument dumping to stderr.
        """
        working_dir = str(sources_dir)

        if env_vars:
            os.environ.update(env_vars)

        if env_vars_from_files:
            for var_name, file_path in env_vars_from_files.items():
                os.environ[var_name] = io_utils.read_value_from_file(
                    file_path, working_dir=working_dir
                )

        packer_lib.version()
        packer_lib.init(working_dir, template)

        if objective == "validate":
            packer_lib.validate(
                working_dir,
                template,
                var_file_paths=var_files,
                template_vars=vars,
                vars_from_files=vars_from_files,
                only=only,
                excepts=excepts,
                debug=debug,
            )
            packer_lib.format_packer_cmd(working_dir, template)
            return PackerVersion(id="0"), {}

        if objective == "build":
            build_manifest = packer_lib.build(
                working_dir,
                template,
                var_file_paths=var_files,
                template_vars=vars,
                vars_from_files=vars_from_files,
                only=only,
                excepts=excepts,
                debug=debug,
                force=force,
            )
            version, metadata = _manifest_to_version_and_metadata(build_manifest)
            return version, metadata

        msg = f'Invalid objective "{objective}": must be "validate" or "build"'
        raise ValueError(msg)


def _manifest_to_version_and_metadata(
    build_manifest: dict[str, Any],
) -> tuple[PackerVersion, dict[str, str]]:
    """Convert a Packer build manifest to a concoursetools version + metadata pair."""
    version = PackerVersion(id="0")
    metadata: dict[str, str] = {}

    for artifact_name, artifacts in build_manifest.get("artifacts", {}).items():
        for artifact_index, artifact in artifacts.items():
            if artifact_index == "0" and "id" in artifact:
                version = PackerVersion(id=artifact["id"])
            for key, value in artifact.items():
                meta_key = f"{artifact_name}::{artifact_index}::{key}"
                metadata[meta_key] = str(value) if value is not None else ""

    return version, metadata


if __name__ == "__main__":
    PackerResource.check_main()
