"""ResourceType factory functions for the ol-concourse pipeline DSL."""

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import Identifier, RegistryImage, ResourceType


def semver_resource() -> ResourceType:
    """Return the ResourceType definition for the Concourse semver resource."""
    return ResourceType(
        name=Identifier("semver"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="concourse/semver-resource"),
    )


def github_issues_resource() -> ResourceType:
    """Return the ResourceType definition for ``mitodl/ol-concourse-github-issues``."""
    return ResourceType(
        name=Identifier("github-issues"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/ol-concourse-github-issues"),
    )


def github_deployments_resource() -> ResourceType:
    """Generate the ``github-deployments`` custom resource type.

    :returns: A :class:`ResourceType` for the GitHub Deployments resource
        hosted on Docker Hub as ``mitodl/concourse-github-deployments-resource``.
    """
    return ResourceType(
        name=Identifier("github-deployments"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(
            repository="mitodl/concourse-github-deployments-resource"
        ),
    )


def release_resource_type() -> ResourceType:
    """Generate the ``release`` custom resource type.

    The resource handles the full release lifecycle: version detection via
    ``check``, release branch/tag creation and checklist generation via ``in``
    and ``out``.  Pair with :func:`~ol_concourse.lib.resources.release_resource`
    to create a matching resource instance.

    :returns: A :class:`ResourceType` for the release resource hosted on
        Docker Hub as ``mitodl/concourse-release-resource``.
    """
    return ResourceType(
        name=Identifier("release"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-release-resource"),
    )


def hashicorp_resource() -> ResourceType:
    """Return the ResourceType definition for the Hashicorp release resource."""
    return ResourceType(
        name=Identifier("hashicorp-release"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/hashicorp-release-resource"),
    )


def rclone() -> ResourceType:
    """Return the ResourceType definition for the rclone sync resource."""
    return ResourceType(
        name=Identifier("rclone"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-rclone-resource"),
    )


def packer_validate() -> ResourceType:
    """Return the ResourceType definition for the Packer validation resource."""
    return ResourceType(
        name=Identifier("packer-validator"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-packer-resource"),
    )


def packer_build() -> ResourceType:
    """Return the ResourceType definition for the Packer image builder resource."""
    return ResourceType(
        name=Identifier("packer-builder"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-packer-resource-builder"),
    )


def ami_resource() -> ResourceType:
    """Return the ResourceType definition for the Amazon AMI resource."""
    return ResourceType(
        name=Identifier("amazon-ami"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="jdub/ami-resource"),
    )


def s3_sync() -> ResourceType:
    """Return the ResourceType definition for the S3 sync resource."""
    return ResourceType(
        name=Identifier("s3-sync"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-s3-sync-resource"),
    )


def pypi_resource() -> ResourceType:
    """Return the ResourceType definition for the PyPI publish resource."""
    return ResourceType(
        name=Identifier("pypi"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="cfplatformeng/concourse-pypi-resource"),
    )


def pulumi_provisioner_resource() -> ResourceType:
    """Return the ResourceType definition for the Pulumi provisioner resource."""
    return ResourceType(
        name=Identifier("pulumi-provisioner"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-pulumi-resource-provisioner"),
    )


# https://github.com/arbourd/concourse-slack-alert-resource
def slack_notification_resource() -> ResourceType:
    """Return the ResourceType definition for the Slack alert notification resource."""
    return ResourceType(
        name=Identifier("slack-notification"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(
            repository="arbourd/concourse-slack-alert-resource", tag="v0.15.0"
        ),
    )
