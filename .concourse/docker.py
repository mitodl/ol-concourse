#!/usr/bin/env python3
"""Generate the docker image build pipeline for ol-concourse resource types.

Add a new entry to RESOURCE_BUILDS to automatically produce the git source
resource, Docker image resource(s), and build job for a new resource type.

Usage (write to stdout):
    python .concourse/docker.py
"""

import json
import sys
from dataclasses import dataclass
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
    Resource,
    TaskConfig,
    TaskStep,
)

REPO_URI = "https://github.com/mitodl/ol-concourse"

UV_IMAGE = AnonymousResource(
    type="registry-image",
    source={"repository": "ghcr.io/astral-sh/uv", "tag": "python3.13-bookworm"},
)

OCI_BUILD_IMAGE = AnonymousResource(
    type="registry-image",
    source={"repository": "concourse/oci-build-task"},
)


@dataclass
class ImageBuild:
    """A single Docker image produced from a resource build."""

    repository: str
    resource_name: str
    task_name: str = "build-image"
    dockerfile: str = "Dockerfile"


@dataclass
class ResourceBuild:
    """All Docker images built for one ol-concourse resource."""

    name: str
    package: str
    resource_dir: str
    images: list[ImageBuild]
    git_resource_name: str = ""

    def __post_init__(self) -> None:
        if not self.git_resource_name:
            self.git_resource_name = f"ol-concourse-{self.name}-src"


# To add a new resource type, append a ResourceBuild entry here.
RESOURCE_BUILDS: list[ResourceBuild] = [
    ResourceBuild(
        name="packer",
        package="ol-concourse-packer",
        resource_dir="resources/packer",
        images=[
            ImageBuild(
                repository="mitodl/concourse-packer-resource",
                resource_name="concourse-packer-resource",
            ),
        ],
    ),
    ResourceBuild(
        name="pulumi",
        package="ol-concourse-pulumi",
        resource_dir="resources/pulumi",
        images=[
            ImageBuild(
                repository="mitodl/concourse-pulumi-resource",
                resource_name="concourse-pulumi-resource",
            ),
            ImageBuild(
                repository="mitodl/concourse-pulumi-resource-provisioner",
                resource_name="concourse-pulumi-resource-provisioner",
                task_name="build-image-provisioner",
                dockerfile="Dockerfile.mitol_provision",
            ),
        ],
    ),
    ResourceBuild(
        name="github-issues",
        package="ol-concourse-github-issues",
        resource_dir="resources/github-issues",
        images=[
            ImageBuild(
                repository="mitodl/concourse-github-issues-resource",
                resource_name="concourse-github-issues-resource",
            ),
        ],
    ),
    ResourceBuild(
        name="pypi",
        package="ol-concourse-pypi",
        resource_dir="resources/pypi",
        images=[
            ImageBuild(
                repository="mitodl/concourse-pypi-resource",
                resource_name="concourse-pypi-resource",
            ),
        ],
    ),
    ResourceBuild(
        name="npm",
        package="ol-concourse-npm",
        resource_dir="resources/npm",
        images=[
            ImageBuild(
                repository="mitodl/concourse-npm-resource",
                resource_name="concourse-npm-resource",
            ),
        ],
    ),
    ResourceBuild(
        name="task",
        package="ol-concourse",
        resource_dir="pipeline_lib",
        git_resource_name="ol-concourse-task-src",
        images=[
            ImageBuild(
                repository="mitodl/ol-concourse-dsl",
                resource_name="ol-concourse-task-image",
            ),
        ],
    ),
    ResourceBuild(
        name="github-deployments",
        package="ol-concourse-github-deployments",
        resource_dir="resources/github-deployments",
        images=[
            ImageBuild(
                repository="mitodl/concourse-github-deployments-resource",
                resource_name="concourse-github-deployments-resource",
            ),
        ],
    ),
    ResourceBuild(
        name="fastly",
        package="ol-concourse-fastly",
        resource_dir="resources/fastly",
        images=[
            ImageBuild(
                repository="mitodl/concourse-fastly-resource",
                resource_name="concourse-fastly-resource",
            ),
        ],
    ),
    ResourceBuild(
        name="release",
        package="ol-concourse-release",
        resource_dir="resources/release",
        images=[
            ImageBuild(
                repository="mitodl/concourse-release-resource",
                resource_name="concourse-release-resource",
            ),
        ],
    ),
]


def _git_resource(rb: ResourceBuild) -> Resource:
    return Resource(
        name=Identifier(rb.git_resource_name),
        type="git",
        icon="github",
        source={
            "uri": REPO_URI,
            "branch": "main",
            "paths": [rb.resource_dir, "uv.lock", "pyproject.toml"],
        },
    )


def _image_resource(image: ImageBuild) -> Resource:
    return Resource(
        name=Identifier(image.resource_name),
        type="registry-image",
        icon="docker",
        source={
            "tag": "latest",
            "repository": image.repository,
            "username": "((dockerhub.username))",
            "password": "((dockerhub.password))",
        },
    )


def _generate_requirements_task(rb: ResourceBuild) -> TaskStep:
    return TaskStep(
        task=Identifier("generate-requirements"),
        config=TaskConfig(
            platform="linux",
            image_resource=UV_IMAGE,
            inputs=[Input(name=Identifier(rb.git_resource_name))],
            outputs=[Output(name=Identifier("build-context"))],
            run=Command(
                path="/bin/sh",
                args=[
                    "-exc",
                    (
                        f"cp -a {rb.git_resource_name}/. build-context/\n"
                        f"cd build-context\n"
                        f"uv build --package {rb.package}"
                        f" --out-dir {rb.resource_dir}/dist/"
                    ),
                ],
            ),
        ),
    )


def _image_build_task(rb: ResourceBuild, image: ImageBuild) -> TaskStep:
    return TaskStep(
        task=Identifier(image.task_name),
        privileged=True,
        config=TaskConfig(
            platform="linux",
            image_resource=OCI_BUILD_IMAGE,
            inputs=[Input(name=Identifier("build-context"))],
            outputs=[Output(name=Identifier("image"))],
            params={
                "CONTEXT": f"build-context/{rb.resource_dir}",
                "DOCKERFILE": f"build-context/{rb.resource_dir}/{image.dockerfile}",
            },
            run=Command(path="build"),
        ),
    )


def _image_put_step(rb: ResourceBuild, image: ImageBuild) -> PutStep:
    return PutStep(
        put=Identifier(image.resource_name),
        params={
            "image": "image/image.tar",
            "additional_tags": f"{rb.git_resource_name}/.git/ref",
        },
    )


def _build_job(rb: ResourceBuild) -> Job:
    plan = [
        GetStep(get=Identifier(rb.git_resource_name), trigger=True),
        _generate_requirements_task(rb),
    ]
    for image in rb.images:
        plan.append(_image_build_task(rb, image))
        plan.append(_image_put_step(rb, image))
    return Job(
        name=Identifier(f"build-and-publish-{rb.name}-image"),
        public=True,
        plan=plan,
    )


def build_pipeline() -> Pipeline:
    resources = []
    jobs = []
    for rb in RESOURCE_BUILDS:
        resources.append(_git_resource(rb))
        for image in rb.images:
            resources.append(_image_resource(image))
        jobs.append(_build_job(rb))
    return PipelineFragment(resources=resources, jobs=jobs).to_pipeline()


if __name__ == "__main__":
    sys.stdout.write(_serialize(json.loads(build_pipeline().model_dump_json())))
