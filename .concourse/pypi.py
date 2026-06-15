#!/usr/bin/env python3
"""Generate the PyPI publishing pipeline for the ol-concourse library.

Usage (write to stdout):
    python .concourse/pypi.py
"""

import json
import sys
from typing import Any

try:
    import yaml as _yaml  # type: ignore[import-untyped]

    def _serialize(data: dict[str, Any]) -> str:
        return _yaml.dump(data, sort_keys=False, allow_unicode=True)

except ImportError:

    def _serialize(data: dict[str, Any]) -> str:
        return json.dumps(data, indent=2)


from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    PutStep,
    RegistryImage,
    Resource,
    ResourceType,
    TaskConfig,
    TaskStep,
)

UV_IMAGE = AnonymousResource(
    type="registry-image",
    source={"repository": "ghcr.io/astral-sh/uv", "tag": "python3.13-bookworm"},
)

_PYPI_RESOURCE_TYPE = ResourceType(
    name=Identifier("pypi"),
    type="registry-image",
    source=RegistryImage(repository="mitodl/concourse-pypi-resource"),
)

_OL_CONCOURSE_GIT = Resource(
    name=Identifier("ol-concourse"),
    type="git",
    icon="github",
    source={
        "uri": "https://github.com/mitodl/ol-concourse",
        "branch": "main",
        "paths": [
            "pipeline_lib/pyproject.toml",
            "pipeline_lib/src/**",
        ],
    },
)

_OL_CONCOURSE_PYPI = Resource(
    name=Identifier("ol-concourse-pypi"),
    type="pypi",
    icon="language-python",
    source={
        "package_name": "ol-concourse",
        "username": "((pypi_creds.username))",
        "password": "((pypi_creds.password))",
    },
)


def build_pipeline() -> Pipeline:
    build_task = TaskStep(
        task=Identifier("build-package"),
        config=TaskConfig(
            platform="linux",
            image_resource=UV_IMAGE,
            inputs=[Input(name=Identifier("ol-concourse"))],
            outputs=[Output(name=Identifier("dist"))],
            run=Command(
                path="/bin/sh",
                args=[
                    "-exc",
                    (
                        "uv build --directory ol-concourse/pipeline_lib"
                        ' --out-dir "$(pwd)/dist/"'
                    ),
                ],
            ),
        ),
    )

    publish_job = Job(
        name=Identifier("publish-ol-concourse-lib"),
        public=True,
        plan=[
            GetStep(get=Identifier("ol-concourse"), trigger=True),
            build_task,
            PutStep(
                put=Identifier("ol-concourse-pypi"),
                params={"glob": "dist/ol_concourse-*"},
            ),
        ],
    )

    return PipelineFragment(
        resource_types=[_PYPI_RESOURCE_TYPE],
        resources=[_OL_CONCOURSE_GIT, _OL_CONCOURSE_PYPI],
        jobs=[publish_job],
    ).to_pipeline()


if __name__ == "__main__":
    sys.stdout.write(_serialize(json.loads(build_pipeline().model_dump_json())))
