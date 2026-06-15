#!/usr/bin/env python3
"""Generate the docker image build pipeline for ol-concourse resource types.

Resource builds are auto-discovered by scanning the resources/ directory.
Each subdirectory containing a Dockerfile and pyproject.toml produces one
build job. Additional images can be produced by adding further Dockerfiles
named Dockerfile.{suffix} (e.g. Dockerfile.provisioner); the suffix becomes
the tail of the image name (e.g. concourse-pulumi-resource-provisioner).

Usage (write to stdout):
    python .concourse/docker.py
"""

import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
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
_REPO_ROOT = Path(__file__).parent.parent

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


def _discover_resource_builds() -> list[ResourceBuild]:
    """Scan resources/ and auto-derive a ResourceBuild for each subdirectory.

    A subdirectory is included when it contains both a Dockerfile and a
    pyproject.toml.  Additional Dockerfiles named Dockerfile.{suffix} produce
    extra ImageBuild entries whose repository and resource names are derived
    from the directory name and the suffix.
    """
    resources_dir = _REPO_ROOT / "resources"
    builds: list[ResourceBuild] = []
    for resource_dir in sorted(resources_dir.iterdir()):
        if not resource_dir.is_dir():
            continue
        if not (resource_dir / "Dockerfile").exists():
            continue
        if not (resource_dir / "pyproject.toml").exists():
            continue

        name = resource_dir.name
        pyproject = tomllib.loads((resource_dir / "pyproject.toml").read_text())
        package: str = pyproject["project"]["name"]

        images = [
            ImageBuild(
                repository=f"mitodl/concourse-{name}-resource",
                resource_name=f"concourse-{name}-resource",
            )
        ]
        for extra in sorted(resource_dir.glob("Dockerfile.*")):
            suffix = extra.name[len("Dockerfile.") :]
            images.append(
                ImageBuild(
                    repository=f"mitodl/concourse-{name}-resource-{suffix}",
                    resource_name=f"concourse-{name}-resource-{suffix}",
                    task_name=f"build-image-{suffix}",
                    dockerfile=extra.name,
                )
            )

        builds.append(
            ResourceBuild(
                name=name,
                package=package,
                resource_dir=f"resources/{name}",
                images=images,
            )
        )
    return builds


# pipeline_lib is the DSL library itself, not a Concourse resource; its image
# naming convention differs so it cannot be auto-discovered with the same rules.
_PIPELINE_LIB_BUILD = ResourceBuild(
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
)

RESOURCE_BUILDS: list[ResourceBuild] = [
    *_discover_resource_builds(),
    _PIPELINE_LIB_BUILD,
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
