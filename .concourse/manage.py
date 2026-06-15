#!/usr/bin/env python3
"""Generate manage.yaml: meta-pipeline that keeps docker and PyPI pipelines up to date.

When this file changes, regenerate and commit manage.yaml:
    python .concourse/manage.py > .concourse/manage.yaml

The committed manage.yaml bootstraps and self-updates the Concourse manage pipeline.
Docker and PyPI pipelines are regenerated at CI time by running docker.py and pypi.py
inside the ol-concourse-dsl task image so they always reflect the latest DSL scripts.
"""

import json
import sys
from typing import Any

try:
    import yaml as _yaml  # type: ignore[import-untyped]

    def _serialize(data: dict[str, Any]) -> str:
        return _yaml.dump(
            data, sort_keys=False, allow_unicode=True, default_flow_style=False
        )

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
    Resource,
    SetPipelineStep,
    TaskConfig,
    TaskStep,
)

_OL_CONCOURSE_GIT = Resource(
    name=Identifier("ol-concourse"),
    type="git",
    icon="github",
    source={
        "uri": "https://github.com/mitodl/ol-concourse",
        "branch": "main",
    },
)

_DSL_TASK_IMAGE = AnonymousResource(
    type="registry-image",
    source={"repository": "mitodl/ol-concourse-dsl", "tag": "latest"},
)


def _generate_pipeline_task(script: str) -> TaskStep:
    """Run a pipeline DSL script and write its output to generated/pipeline.yaml."""
    return TaskStep(
        task=Identifier("generate-pipeline"),
        config=TaskConfig(
            platform="linux",
            image_resource=_DSL_TASK_IMAGE,
            inputs=[Input(name=Identifier("ol-concourse"))],
            outputs=[Output(name=Identifier("generated"))],
            run=Command(
                path="/bin/sh",
                args=["-exc", f"python3 {script} > generated/pipeline.yaml"],
            ),
        ),
    )


def build_pipeline() -> Pipeline:
    set_self_job = Job(
        name=Identifier("set-self"),
        public=True,
        plan=[
            GetStep(get=Identifier("ol-concourse"), trigger=True),
            SetPipelineStep(
                set_pipeline="self",
                file="ol-concourse/.concourse/manage.yaml",
            ),
        ],
    )

    set_docker_job = Job(
        name=Identifier("set-docker-pipeline"),
        public=True,
        plan=[
            GetStep(get=Identifier("ol-concourse"), trigger=True),
            _generate_pipeline_task("ol-concourse/.concourse/docker.py"),
            SetPipelineStep(
                set_pipeline=Identifier("build-ol-concourse-images"),
                file="generated/pipeline.yaml",
            ),
        ],
    )

    set_pypi_job = Job(
        name=Identifier("set-pypi-pipeline"),
        public=True,
        plan=[
            GetStep(get=Identifier("ol-concourse"), trigger=True),
            _generate_pipeline_task("ol-concourse/.concourse/pypi.py"),
            SetPipelineStep(
                set_pipeline=Identifier("publish-ol-concourse"),
                file="generated/pipeline.yaml",
            ),
        ],
    )

    return PipelineFragment(
        resources=[_OL_CONCOURSE_GIT],
        jobs=[set_self_job, set_docker_job, set_pypi_job],
    ).to_pipeline()


if __name__ == "__main__":
    sys.stdout.write(
        _serialize(
            build_pipeline().model_dump(mode="json", exclude_none=True, by_alias=True)
        )
    )
