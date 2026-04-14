"""Resource factory functions for the ol-concourse pipeline DSL."""

from typing import Any, Literal

from ol_concourse.lib.models.pipeline import Duration, Identifier, Resource
from ol_concourse.lib.models.resource import Git


def git_repo(  # noqa: PLR0913
    name: Identifier,
    uri: str,
    branch: str = "main",
    check_every: str = "60s",
    paths: list[str] | None = None,
    depth: int | None = None,
    fetch_tags: bool = False,
    tag_regex: str | None = None,
    **kwargs,
) -> Resource:
    """Generate a git resource for the given repository.

    :param name: Resource name used across pipeline steps.
    :param uri: Git repository URI (SSH or HTTPS).
    :param branch: Branch to track (default: ``main``).
    :param check_every: How often Concourse polls for new versions (default: ``60s``).
    :param paths: Restrict change detection to these paths.
    :param depth: Shallow clone depth.
    :param fetch_tags: Whether to fetch git tags.
    :param tag_regex: Filter tags by regex when ``fetch_tags`` is true.
    :returns: A configured Concourse git resource.
    """
    return Resource(
        name=name,
        type="git",
        icon="git",
        check_every=check_every,
        source=Git(
            uri=uri,
            branch=branch,
            paths=paths,
            version_depth=depth,
            fetch_tags=fetch_tags,
            tag_regex=tag_regex,
        ).model_dump(exclude_none=True),
        **kwargs,
    )


def ssh_git_repo(
    name: Identifier,
    uri: str,
    private_key: str,
    branch: str = "main",
    paths: list[str] | None = None,
) -> Resource:
    """Generate a git resource authenticated with an SSH private key.

    :param name: Resource name used across pipeline steps.
    :param uri: Git repository SSH URI.
    :param private_key: PEM-encoded SSH private key for authentication.
    :param branch: Branch to track (default: ``main``).
    :param paths: Restrict change detection to these paths.
    :returns: A configured Concourse git resource.
    """
    return Resource(
        name=name,
        type="git",
        icon="git",
        source=Git(
            uri=uri, branch=branch, paths=paths, private_key=private_key
        ).model_dump(exclude_none=True),
    )


def github_release(  # noqa: PLR0913
    name: Identifier,
    owner: str,
    repository: str,
    github_token: str = "((github.public_repo_access_token))",  # noqa: S107
    tag_filter: str | None = None,
    order_by: Literal["time", "version"] | None = None,
    check_frequency="24h",
) -> Resource:
    """Generate a github-release resource for the given owner/repository.

    :param name: The name of the resource.  This will get used across subsequent
        pipeline steps that reference this resource.
    :param owner: The owner of the repository (e.g. the GitHub user or organization)
    :param repository: The name of the repository as it appears in GitHub
    :param github_token: A personal access token with `public_repo` scope to increase
        the rate limit for checking versions.
    :param tag_filter: A regular expression used to filter the repository tags to
        include in the version results.
    :param order_by: Indicate whether to order by version number or time.  Primarily
        useful when in combination with `tag_filter`.

    :returns: A configured Concourse resource object that can be used in a pipeline.

    :rtype: Resource
    """
    release_config = {
        "repository": repository,
        "owner": owner,
        "release": True,
    }
    if tag_filter:
        release_config["tag_filter"] = tag_filter
    if github_token:
        release_config["access_token"] = github_token
    if order_by:
        release_config["order_by"] = order_by
    return Resource(
        name=name,
        type="github-release",
        icon="github",
        check_every=check_frequency,
        source=release_config,
    )


def github_issues(  # noqa: PLR0913
    name: Identifier,
    repository: str,
    issue_prefix: str,
    auth_method: Literal["token", "app"] = "token",
    gh_host: str | None = "https://github.mit.edu/api/v3",
    access_token: str = "((github.issues_resource_access_token))",  # noqa: S107
    app_id: str | None = None,
    app_installation_id: str | None = None,
    private_ssh_key: str | None = None,
    issue_state: Literal["open", "closed"] = "closed",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    issue_title_template: str | None = None,
    issue_body_template: str | None = None,
    skip_if_labeled: list[str] | None = None,
    poll_frequency: Duration = Duration("60m"),
) -> Resource:
    """Generate a github-issue resource for the given owner/repository.

    :param name: The name of the resource.  This will get used across subsequent
        pipeline steps that reference this resource.
    :param repository: The name of the repository as it appears in GitHub
    :param github_token: A personal access token with `public_repo` scope to increase
        the rate limit for checking versions.
    :param issue_prefix: A string tobe used to match an issue in the repository for
     the workflow to detect or act upon.
    :param skip_if_labeled: Optional list of label names. Issues closed with any of
        these labels will be skipped by ``check`` (not emitted as new versions).
        Use this to allow release abandonment without triggering a production deploy.

    :returns: A configured Concourse issue object that can be used in a pipeline.

    :rtype: Resource
    """
    issue_config = {
        "auth_method": auth_method,
        "assignees": assignees,
        "issue_body_template": issue_body_template,
        "issue_prefix": issue_prefix,
        "issue_state": issue_state,
        "issue_title_template": issue_title_template,
        "labels": labels,
        "repository": repository,
        "skip_if_labeled": skip_if_labeled,
    }
    if gh_host:
        issue_config["gh_host"] = gh_host
    if auth_method == "token":
        issue_config["access_token"] = access_token
    else:
        issue_config["app_id"] = app_id
        issue_config["app_installation_id"] = app_installation_id
        issue_config["private_ssh_key"] = private_ssh_key
    return Resource(
        name=name,
        type="github-issues",
        icon="github",
        check_every=poll_frequency,
        expose_build_created_by=True,
        source={k: v for k, v in issue_config.items() if v is not None},
    )


def hashicorp_release(name: Identifier, project: str) -> Resource:
    """Generate a hashicorp-release resource for the given application.  # noqa: DAR201

    :param name: The name of the resourc. This will get used across subsequent
        pipeline steps taht reference this resource.
    :type name: Identifier
    :param project: The name of the hashicorp project to check for a release of.
    :type project: str
    """
    return Resource(
        name=name,
        type="hashicorp-release",
        icon="lock-check",
        check_every="24h",
        source={"project": project},
    )


def amazon_ami(
    name: Identifier,
    filters: dict[str, str | bool],
    region: str = "us-east-1",
) -> Resource:
    """Generate an Amazon AMI resource for the given filters.

    :param name: Resource name used across pipeline steps.
    :param filters: AWS ``describe-images`` filter key-value pairs.
    :param region: AWS region to search (default: ``us-east-1``).
    :returns: A configured Concourse amazon-ami resource.
    """
    return Resource(
        name=name,
        type="amazon-ami",
        icon="server",
        check_every="30m",
        source={
            "region": region,
            "filters": filters,
        },
    )


def pulumi_provisioner(
    name: Identifier, project_name: str, project_path: str
) -> Resource:
    """Generate a Pulumi provisioner resource for the given project.

    :param name: Resource name used across pipeline steps.
    :param project_name: Pulumi project name.
    :param project_path: Path to the Pulumi project directory within the workspace.
    :returns: A configured Concourse pulumi-provisioner resource.
    """
    return Resource(
        name=name,
        type="pulumi-provisioner",
        icon="cloud-braces",
        source={
            "env_pulumi": {"AWS_SHARED_CREDENTIALS_FILE": "aws_creds/credentials"},
            "action": "update",
            "project_name": project_name,
            "source_dir": project_path,
        },
    )


def pypi(
    name: Identifier,
    package_name: str,
    username: str = "((pypi_creds.username))",
    password: str = "((pypi_creds.password))",  # noqa: S107
    check_every: str = "24h",
) -> Resource:
    """Generate a PyPI resource for the given package.

    :param name: Resource name used across pipeline steps.
    :param package_name: PyPI package name to watch/publish.
    :param username: PyPI upload username (default: ``((pypi_creds.username))``).
    :param password: PyPI upload password (default: ``((pypi_creds.password))``).
    :param check_every: How often to check for new versions (default: ``24h``).
    :returns: A configured Concourse pypi resource.
    """
    return Resource(
        name=name,
        type="pypi",
        icon="language-python",
        check_every=check_every,
        source={
            "name": package_name,
            "packaging": "any",
            "repository": {
                "username": username,
                "password": password,
            },
        },
    )


def schedule(
    name: Identifier,
    interval: str | None = None,
    start: str | None = None,
    stop: str | None = None,
    days: list[str] | None = None,
) -> Resource:
    """Generate a time/schedule resource for triggering pipeline jobs on a schedule.

    :param name: Resource name used across pipeline steps.
    :param interval: Trigger interval (e.g. ``"1h"``).
    :param start: Start of the daily window in ``HH:MM`` format.
    :param stop: End of the daily window in ``HH:MM`` format.
    :param days: Days of the week to allow triggers (e.g. ``["Monday", "Tuesday"]``).
    :returns: A configured Concourse time resource.
    """
    return Resource(
        name=name,
        type="time",
        icon="clock",
        source={
            "interval": interval,
            "start": start,
            "stop": stop,
            "days": days,
        },
    )


def registry_image(  # noqa: PLR0913
    name: Identifier,
    image_repository: str,
    image_tag: str | None = "latest",
    variant: str | None = None,
    tag_regex: str | None = None,
    sort_by_creation: bool | None = None,
    username=None,
    password=None,
    check_every: str | None = None,
    ecr_region: str | None = None,
) -> Resource:
    """Generate a registry-image resource for the given container image.

    :param name: Resource name used across pipeline steps.
    :param image_repository: OCI image repository (e.g. ``ghcr.io/mitodl/myapp``).
    :param image_tag: Tag to track (default: ``latest``).
    :param variant: Platform variant (e.g. ``linux/arm64``).
    :param tag_regex: Filter tags by regex instead of tracking a fixed tag.
    :param sort_by_creation: When using ``tag_regex``, sort by image creation time.
    :param username: Registry username for private images.
    :param password: Registry password for private images.
    :param check_every: Override how often Concourse checks for new image versions.
    :param ecr_region: AWS region for ECR authentication.
    :returns: A configured Concourse registry-image resource.
    """
    image_source: dict[str, Any] = {"repository": image_repository, "tag": image_tag}
    if username and password:
        image_source["username"] = username
        image_source["password"] = password
    if variant:
        image_source["variant"] = variant
    if tag_regex is not None:
        image_source["tag_regex"] = tag_regex
    if sort_by_creation is not None:
        image_source["created_at_sort"] = sort_by_creation
    if ecr_region is not None:
        image_source["aws_region"] = ecr_region
    return Resource(
        name=name,
        type="registry-image",
        check_every=check_every,
        source=image_source,
    )


# https://github.com/arbourd/concourse-slack-alert-resource
def slack_notification(name: Identifier, url: str) -> Resource:
    """Generate a Slack notification resource for the given webhook URL.

    :param name: Resource name used across pipeline steps.
    :param url: Slack incoming webhook URL.
    :returns: A configured Concourse slack-notification resource.
    """
    return Resource(
        name=name, type="slack-notification", source={"url": url, "disabled": False}
    )


def s3_object(
    name: Identifier,
    bucket: str,
    object_path: str | None = None,
    object_regex: str | None = None,
):
    """Generate an S3 resource for the given bucket and object.

    :param name: Resource name used across pipeline steps.
    :param bucket: S3 bucket name.
    :param object_path: Fixed versioned file path within the bucket.
    :param object_regex: Regex to match object keys (use instead of ``object_path``).
    :returns: A configured Concourse s3 resource.
    """
    return Resource(
        name=name,
        type="s3",
        icon="bucket",
        source={
            "bucket": bucket,
            "regexp": object_regex,
            "versioned_file": object_path,
            "enable_aws_creds_provider": True,
        },
    )


# This resource type also supports s3, gcs and others. We can create those later.
def git_semver(  # noqa: PLR0913
    name: str,
    uri: str,
    branch: str,
    file: str,
    private_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    git_user: str | None = None,
    depth: int | None = None,
    skip_ssl_verification: bool = False,
    commit_message: str | None = None,
    initial_version: str = "0.0.0",
) -> Resource:
    """Generate a semver resource backed by a git repository.

    :param name: Resource name used across pipeline steps.
    :param uri: Git repository URI.
    :param branch: Branch where the version file lives.
    :param file: Path to the version file within the repository.
    :param private_key: SSH private key for git authentication.
    :param username: Git HTTP username.
    :param password: Git HTTP password.
    :param git_user: Git committer identity string (e.g. ``"Bot <bot@example.com>"``).
    :param depth: Shallow clone depth.
    :param skip_ssl_verification: Skip TLS verification for self-signed certs.
    :param commit_message: Template for version-bump commit messages.
    :param initial_version: Seed version when the file does not yet exist
        (default: ``0.0.0``).
    :returns: A configured Concourse semver resource using the git driver.
    """
    return Resource(
        name=name,
        type="semver",
        icon="version",
        source={
            "initial_version": initial_version,
            "driver": "git",
            "uri": uri,
            "branch": branch,
            "file": file,
            "private_key": private_key,
            "username": username,
            "password": password,
            "git_user": git_user,
            "depth": depth,
            "skip_ssl_verification": skip_ssl_verification,
            "commit_message": commit_message,
        },
    )


def release_resource(  # noqa: PLR0913
    name: Identifier,
    uri: str,
    branch: str = "main",
    private_key: str | None = None,
    access_token: str | None = None,
    repository: str | None = None,
    git_user_name: str = "Concourse CI",
    git_user_email: str = "concourse@example.com",
    changelog_style: Literal["cumulative", "per_release"] | None = None,
    changelog_file: str = "CHANGELOG.md",
    changelog_dir: str = "releases",
    webhook_token: str | None = None,
) -> Resource:
    """Generate a release resource for the given git repository.

    The resource handles the full release lifecycle: version detection,
    release branch/tag creation, commit checklist and changelog generation,
    and release branch merging.

    :param name: Resource name used across pipeline steps.
    :param uri: Git repository URI (SSH or HTTPS).
    :param branch: Branch to track for unreleased commits (default: ``main``).
    :param private_key: SSH private key for git operations.
    :param access_token: GitHub token; enables PR number/title enrichment in
        ``in`` output files.
    :param repository: ``owner/repo`` string; required when ``access_token``
        is set.
    :param git_user_name: Git committer name written to release commits.
    :param git_user_email: Git committer email written to release commits.
    :param changelog_style: ``"cumulative"`` (prepend to a single file) or
        ``"per_release"`` (write a per-version file).  ``None`` disables
        changelog management.
    :param changelog_file: Filename for cumulative changelog (default:
        ``CHANGELOG.md``).
    :param changelog_dir: Directory for per-release files (default:
        ``releases``).
    :param webhook_token: Concourse webhook token; used by the Slack release
        bot to trigger ``check`` explicitly.  Defaults ``check_every`` to
        ``never`` so the resource is not polled.

    :returns: A configured Concourse resource object.
    :rtype: Resource
    """
    source: dict[str, Any] = {
        "uri": uri,
        "branch": branch,
        "git_user_name": git_user_name,
        "git_user_email": git_user_email,
        "changelog_file": changelog_file,
        "changelog_dir": changelog_dir,
    }
    if private_key is not None:
        source["private_key"] = private_key
    if access_token is not None:
        source["access_token"] = access_token
    if repository is not None:
        source["repository"] = repository
    if changelog_style is not None:
        source["changelog_style"] = changelog_style
    if webhook_token is not None:
        source["webhook_token"] = webhook_token

    return Resource(
        name=name,
        type="release",
        icon="tag",
        # Default to never polling; the Slack release bot triggers check via webhook.
        check_every="never",
        webhook_token=webhook_token,
        source=source,
    )
