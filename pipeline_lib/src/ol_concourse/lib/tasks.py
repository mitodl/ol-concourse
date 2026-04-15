"""Task factory functions for ol-concourse pipeline DSL."""

import shlex

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    Identifier,
    Input,
    Output,
    TaskConfig,
    TaskStep,
)

# Default image for pipeline_lib task steps. Bundles ol-concourse, bumpver, and git.
# Tag is kept as "latest" until a versioned release of the task image is published.
# Once the first image is built and pushed, pin this to a specific digest or tag
# (e.g. "2026.04.15") to ensure reproducible pipeline behavior.
TASK_IMAGE = AnonymousResource(
    type=REGISTRY_IMAGE,
    source={"repository": "ghcr.io/mitodl/ol-concourse-task", "tag": "latest"},
)


def bump_version_task(
    version_file: str = "release/version",
    repository: str = "app-source",
    git_user: str = "CI",
    git_email: str = "odl-devops@mit.edu",
    image: AnonymousResource | None = None,
) -> TaskStep:
    """Generate a TaskStep that runs bumpver to update version strings in-place.

    Reads the target version from ``version_file``, then runs
    ``bumpver update --set-version <version> --no-commit --no-fetch`` inside
    ``repository``.  The modified files remain in the workspace for a subsequent
    ``put: release`` step to commit onto the release branch.

    :param version_file: Workspace-relative path to the file containing the
        version string, in ``input-name/relative/path`` form (default:
        ``release/version``).  The leading path component must be the name of a
        Concourse input resource in the build plan.
    :param repository: Name of the Concourse input/output resource directory
        containing the application source and its ``[bumpver]`` config in
        ``pyproject.toml`` or ``setup.cfg`` (default: ``app-source``).
    :param git_user: Git committer name used when bumpver writes version files
        (default: ``CI``).
    :param git_email: Git committer email (default: ``odl-devops@mit.edu``).
    :param image: Container image for the task.  Defaults to
        ``ghcr.io/mitodl/ol-concourse-task:latest`` via :data:`TASK_IMAGE`.

    :raises ValueError: If ``version_file`` is not in ``input-name/path`` form
        (i.e. has no ``/``, or starts with ``/``, ``./``, or ``../``).

    :returns: A configured Concourse
        :class:`~ol_concourse.lib.models.pipeline.TaskStep`.
    """
    if (
        "/" not in version_file
        or version_file.startswith(("/", "./", "../"))
    ):
        msg = (
            f"version_file must be workspace-relative in 'input-name/path' form "
            f"(e.g. 'release/version'), got: {version_file!r}"
        )
        raise ValueError(msg)

    version_file = version_file.strip()
    version_input = version_file.split("/")[0]
    # Normalize repository name via Identifier so the input/output names and the
    # shell script refer to exactly the same directory.
    repo_id = str(Identifier(repository))

    # De-duplicate: when version_file lives inside the repo input, don't emit
    # the same input name twice (Concourse treats duplicate input names as invalid).
    inputs = [Input(name=Identifier(version_input))]
    if version_input != repo_id:
        inputs.append(Input(name=Identifier(repo_id)))

    return TaskStep(
        task=Identifier("bump-version"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource=image or TASK_IMAGE,
            inputs=inputs,
            outputs=[
                Output(name=Identifier(repo_id)),
            ],
            run=Command(
                path="bash",
                args=[
                    "-ec",
                    f"""VERSION=$(cat {shlex.quote(version_file)})
git -C {shlex.quote(repo_id)} config user.email {shlex.quote(git_email)}
git -C {shlex.quote(repo_id)} config user.name {shlex.quote(git_user)}
cd {shlex.quote(repo_id)}
bumpver update --set-version "$VERSION" --no-commit --no-fetch""",
                ],
            ),
        ),
    )


# Generates a TaskStep to perform an instance refresh from a given set
# of filters and queires. The combination of filters and queries should
# be trusted to return one, and only one, autoscale group name.
def instance_refresh_task(
    filters: str,
    queries: str,
) -> TaskStep:
    """Generate a TaskStep that triggers an EC2 Auto Scaling instance refresh.

    :param filters: AWS CLI filter expression passed to
        ``describe-auto-scaling-groups --filters``.
    :param queries: JMESPath query expression that resolves to a single ASG name.
    :returns: A configured Concourse
        :class:`~ol_concourse.lib.models.pipeline.TaskStep`.
    """
    return TaskStep(
        task=Identifier("instance-refresh"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource={
                "type": REGISTRY_IMAGE,
                "source": {"repository": "amazon/aws-cli"},
            },
            params={},
            run=Command(
                path="bash",
                args=[
                    "-ec",
                    f"""ASG_NAME=$(aws autoscaling describe-auto-scaling-groups --color on --no-cli-auto-prompt --no-cli-pager --filters {filters} --query "{queries}" --output text);
                    aws autoscaling start-instance-refresh --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name "$ASG_NAME" --preferences MinHealthyPercentage=50,InstanceWarmup=120""",  # noqa: E501
                ],
            ),
        ),
    )


# Generates a TaskStep that can be used to block a job from completing until
# the most recent instance refresh is completed. If no instance refresh is
# found, the task finishes immediately. The combination filters + queries is
# expected to return one and only one autoscale group name.
def block_for_instance_refresh_task(
    filters: str,
    queries: str,
    check_freq: int = 10,
) -> TaskStep:
    """Generate a TaskStep that blocks until the latest EC2 instance refresh completes.

    Polls ``describe-instance-refreshes`` every ``check_freq`` seconds until the
    refresh leaves ``InProgress``, ``Pending``, or ``Canceling`` state.  If no
    refresh is found the task exits immediately.

    :param filters: AWS CLI filter expression passed to
        ``describe-auto-scaling-groups --filters``.
    :param queries: JMESPath query that resolves to a single ASG name.
    :param check_freq: Polling interval in seconds (default: ``10``).
    :returns: A configured Concourse
        :class:`~ol_concourse.lib.models.pipeline.TaskStep`.
    """
    return TaskStep(
        task=Identifier("block-for-instance-refresh"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource={
                "type": REGISTRY_IMAGE,
                "source": {"repository": "amazon/aws-cli"},
            },
            params={},
            run=Command(
                path="bash",
                args=[
                    "-evc",
                    f""" ASG_NAME=$(aws autoscaling describe-auto-scaling-groups --color on --no-cli-auto-prompt --no-cli-pager --filters {filters} --query "{queries}" --output text);
                    status="InProgress"
                    while [ "$status" = "InProgress" ] || [ "$status" == "Pending" ] || [ "$status" == "Canceling" ]
                    do
                        sleep {check_freq}
                        status=$(aws autoscaling describe-instance-refreshes --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name "$ASG_NAME" --query "sort_by(InstanceRefreshes, &StartTime)[].{{Status: Status}}" --output text | tail -n 1)
                        aws autoscaling describe-instance-refreshes --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name $ASG_NAME --query "sort_by(InstanceRefreshes, &StartTime)[].{{InstanceRefreshId: InstanceRefreshId, StartTime: StartTime, Status: Status}}" --output text | tail -n 1
                    done""",  # noqa: E501
                ],
            ),
        ),
    )
