"""Concourse pipeline jobs for infrastructure provisioning and management."""

from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    InParallelStep,
    Job,
    PutStep,
    Resource,
    TaskStep,
)
from ol_concourse.lib.notifications import notification
from ol_concourse.lib.resource_types import (
    github_issues_resource,
    packer_build,
    packer_validate,
    pulumi_provisioner_resource,
    slack_notification_resource,
)
from ol_concourse.lib.resources import (
    github_issues,
    pulumi_provisioner,
    slack_notification,
)


# Filename the pulumi-provisioner's implicit get writes the deploy summary to,
# inside that resource's own artifact directory.
DEPLOY_SUMMARY_FILENAME = "deploy_summary.md"


def _pulumi_put_step(fragment: PipelineFragment) -> PutStep:
    """Return the pulumi-provisioner PutStep from a single-job pulumi_job fragment."""
    for step in fragment.jobs[0].plan:
        if isinstance(step, PutStep) and str(step.put).startswith("pulumi-"):
            return step
    msg = "pulumi_job produced no pulumi-provisioner PutStep"
    raise ValueError(msg)


def _summary_artifact_path(pulumi_put: PutStep) -> str:
    """Path to the deploy summary, relative to the job working directory.

    A put step produces no artifacts of its own, so the only way the result of
    the Pulumi run reaches a later step is the put's *implicit get*, which lands
    under the resource's name.
    """
    return f"{pulumi_put.put}/{DEPLOY_SUMMARY_FILENAME}"


def packer_jobs(  # noqa: PLR0913
    dependencies: list[GetStep],
    image_code: Resource,
    packer_template_path: str = "src/bilder/images/.",
    node_types: Iterable[str] | None = None,
    packer_vars: dict[str, str] | None = None,
    env_vars_from_files: dict[str, str] | None = None,
    extra_packer_params: dict[str, str] | None = None,
    job_name_suffix: str = "",
) -> PipelineFragment:
    """Generate a pipeline fragment for building EC2 AMIs with Packer.

    :param dependencies: The list of `Get` steps that should be run at the start of the
        pipeline.  This is used for setting up inputs to the build, as well as for
        triggering on upstream changes (e.g. GitHub releases).
    :param image_code: The Git resource definition that specifies the repository that
        holds the code for building the image, including the Packer template.
    :param packer_template_path: The path in the image_code resource that points to the
        Packer template that you would like to build.
    :param node_types: The node types that should be built for the template and passed
        as vars during the build (e.g. web and worker)
    :param packer_vars: A dictionary of var inputs for the Packer template.
    :param env_vars_from_files: The list of environment variables that should be set
        during the build and the files to load for populating the values (e.g. the
        `version` file from a GitHub resource)
    :param extra_packer_params: A dictionary of parameters to pass to the `packer`
        command line (e.g. `-only` or `-except` when you want to specify a particular
        build target)
    :param job_name_suffix: A string to append to the name of the validate and build
        jobs to allow for ensuring unique names when multiple Packer builds happen in a
        single pipeline.

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a complete pipeline definition.
    """
    packer_validate_type = packer_validate()
    packer_build_type = packer_build()
    packer_build_resource = Resource(name="packer-build", type=packer_build_type.name)
    packer_validate_resource = Resource(
        name="packer-validate", type=packer_validate_type.name
    )
    validate_job = Job(
        name=Identifier(f"validate-packer-template-{job_name_suffix}".strip("-")),
        plan=[
            *dependencies,
            GetStep(get=image_code.name, trigger=True),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        put=packer_validate_resource.name,
                        params={
                            "template": f"{image_code.name}/{packer_template_path}",
                            "objective": "validate",
                            "vars": {
                                **(packer_vars or {}),
                                **{"node_type": node_type},  # noqa: PIE800
                            },
                            **(extra_packer_params or {}),
                        },
                    )
                    for node_type in node_types or ["server"]
                ]
            ),
        ],
    )
    # Make sure that all of the dependencies have passed the validate step before
    # triggering the image build.
    build_deps = [deepcopy(dep) for dep in dependencies]
    for dep in build_deps:
        dep.passed = [validate_job.name]
    build_job = Job(
        name=Identifier(f"build-packer-template-{job_name_suffix}".strip("-")),
        plan=[
            *build_deps,
            GetStep(get=image_code.name, trigger=True, passed=[validate_job.name]),
            InParallelStep(
                in_parallel=[
                    PutStep(
                        attempts=3,
                        put=packer_build_resource.name,
                        params={
                            "template": f"{image_code.name}/{packer_template_path}",
                            "objective": "build",
                            "vars": {
                                **(packer_vars or {}),
                                **{"node_type": node_type},  # noqa: PIE800
                            },
                            "env_vars": {
                                "AWS_REGION": "us-east-1",
                                "PYTHONPATH": f"${{PYTHONPATH}}:{image_code.name}/src",
                                "PACKER_GITHUB_API_TOKEN": "((github.public_repo_access_token))",  # noqa: E501
                            },
                            "env_vars_from_files": env_vars_from_files or {},
                            **(extra_packer_params or {}),
                        },
                    )
                    for node_type in node_types or ["server"]
                ]
            ),
        ],
    )
    return PipelineFragment(
        resource_types=[packer_validate_type, packer_build_type],
        resources=[packer_validate_resource, packer_build_resource],
        jobs=[validate_job, build_job],
    )


def pulumi_jobs_chain(  # noqa: PLR0913, PLR0912, PLR0915
    pulumi_code: Resource,
    stack_names: list[str],
    project_name: str,
    project_source_path: Path,
    enable_github_issue_resource: bool = True,
    custom_dependencies: dict[int, list[GetStep]] | None = None,
    dependencies: list[GetStep] | None = None,
    additional_post_steps: dict[int, list[GetStep | PutStep | TaskStep]] | None = None,
    github_issue_assignees: list[str] | None = None,
    github_issue_labels: list[str] | None = None,
    github_issue_repository: str | None = None,
    additional_env_vars: dict[str, str] | None = None,
    env_vars_from_files: dict[str, str] | None = None,
    slack_url_path: str | None = None,
    refresh_stack: bool = True,
    pulumi_put_attempts: int | None = None,
    max_carried_changes: int | str | None = None,
    topology: Literal["deploy-chained", "preview-gated"] = "deploy-chained",
    auto_deploy_stages: list[str] | None = None,
) -> PipelineFragment:
    """Create a chained sequence of jobs for running Pulumi tasks.

    :param pulumi_code: A git resource that represents the repository for the code being
        executed
    :param stack_names: The list of stack names in sequence that should be chained
        together
    :param project_name: The name of the Pulumi project being executed
    :param project_source_path: The path within the `pulumi_code` resource where the
        code being executed is located
    :param dependencies: A list of `Get` step definitions that are used as inputs or
        triggers for the jobs in the chain
    :param custom_dependencies: A dict of indices and `Get` step definitions that are
        used as inputs or triggers for the jobs in the chain.
    :param github_issue_assignees: A list of GitHub usernames that should be assigned
    :param github_issue_labels: A list of GitHub labels that should be applied
    :param env_vars_from_files: The list of environment variables that should be set
        during the build and the files to load for populating the values (e.g. the
        `version` file from a GitHub resource)
    :param slack_url_path: A Vault secret path containing the Slack webhook URL. When
        provided, failure, error, and abort notifications are sent to that Slack
        channel.
    :param refresh_stack: When ``False``, passes ``refresh_stack: false`` to the
        pulumi-provisioner resource so that ``pulumi refresh`` is skipped before each
        ``pulumi up``.  Defaults to ``True`` (refresh enabled).
    :param pulumi_put_attempts: ``attempts`` for each pulumi-provisioner put step in
        the chain.  Defaults to ``None`` (no retry).  Leave it alone here: every job
        in a chain gates the next environment, which is precisely the case the
        retry can turn into a fabricated green.  See :func:`pulumi_job`.
    :param max_carried_changes: How many per-resource changes the promotion-gate
        issue body lists.  ``0`` means no cap.  Omit for the resource's default
        (200).  See :func:`pulumi_job`.
    :type custom_dependencies: Dict[int, list[GetStep]]

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a full pipeline.
    """
    if enable_github_issue_resource and github_issue_repository is None:
        msg = (
            "github_issue_repository is required when enable_github_issue_resource=True"
        )
        raise ValueError(msg)

    if topology == "preview-gated":
        return _dispatch_preview_gated(
            pulumi_code=pulumi_code,
            stack_names=stack_names,
            project_name=project_name,
            project_source_path=project_source_path,
            github_issue_repository=github_issue_repository,
            auto_deploy_stages=auto_deploy_stages,
            github_issue_assignees=github_issue_assignees,
            github_issue_labels=github_issue_labels,
            dependencies=dependencies,
            additional_env_vars=additional_env_vars,
            env_vars_from_files=env_vars_from_files,
            refresh_stack=refresh_stack,
            pulumi_put_attempts=pulumi_put_attempts,
            max_carried_changes=max_carried_changes,
            slack_url_path=slack_url_path,
            enable_github_issue_resource=enable_github_issue_resource,
            custom_dependencies=custom_dependencies,
            additional_post_steps=additional_post_steps,
        )
    if auto_deploy_stages is not None:
        msg = "auto_deploy_stages only applies to topology='preview-gated'"
        raise ValueError(msg)

    chain_fragment = PipelineFragment(resource_types=[github_issues_resource()])
    previous_job = None
    gh_issues_trigger = None
    for index, stack_name in enumerate(stack_names):
        if index + 1 < len(stack_names) and enable_github_issue_resource:
            gh_issues_trigger = github_issues(
                auth_method="token",
                name=Identifier(
                    f"github-issues-{project_name.lower()}-{stack_name.lower()}-trigger"
                ),
                repository=github_issue_repository,
                issue_title_template=f"[bot] Pulumi {project_name} {stack_name} "
                "deployed.",
                issue_prefix=f"[bot] Pulumi {project_name} {stack_name} deployed.",
                issue_state="closed",
                poll_frequency="15m",
            )

        if enable_github_issue_resource:
            gh_issues_post = github_issues(
                auth_method="token",
                name=Identifier(
                    f"github-issues-{project_name.lower()}-{stack_name.lower()}-post"
                ),
                repository=github_issue_repository,
                issue_title_template=(
                    f"[bot] Pulumi {project_name} {stack_name} deployed."
                ),
                issue_prefix=(f"[bot] Pulumi {project_name} {stack_name} deployed."),
                issue_state="open",
            )

        production_stack = stack_name.lower().endswith("production")
        qa_stack = stack_name.lower().endswith("qa")
        ci_stack = stack_name.lower().endswith("ci")

        passed_param = None
        if index != 0:
            previous_stack = stack_names[index - 1]
            previous_job = chain_fragment.jobs[-1]
            passed_param = [previous_job.name]

        for dependency in dependencies or []:
            # These mutations apply globally if the dependencies aren't copied below
            if hasattr(dependency, "trigger"):
                dependency.trigger = not bool(previous_job or production_stack)
                dependency.passed = passed_param or dependency.passed

        # Need to copy the dependencies because otherwise they are globally mutated
        local_dependencies = [
            dependency_step.model_copy() for dependency_step in (dependencies or [])
        ]
        # Needed to duplicate if conditional because otherwise it messes with the
        # sequencing of dependencies and whether they had to pass previous stacks.
        if index != 0 and enable_github_issue_resource:
            # We don't want the current stage, we want the previous one so that it will
            # trigger the current stack. This ensures that we are triggering on the
            # notification that the previous step has been deployed.
            get_gh_issues = GetStep(
                get=Identifier(
                    f"github-issues-{project_name.lower()}-{previous_stack.lower()}-trigger"
                ),
                trigger=True,
            )
            local_dependencies.append(get_gh_issues)

        if custom_dependency := (custom_dependencies or {}).get(index):
            local_custom_dependencies = [
                custom_dependency_step.model_copy()
                for custom_dependency_step in custom_dependency
            ]
            local_dependencies.extend(local_custom_dependencies)

        step_fragment = pulumi_job(
            pulumi_code,
            stack_name,
            project_name,
            project_source_path,
            local_dependencies,
            (additional_post_steps or {}).get(index, []),
            previous_job,
            additional_env_vars=additional_env_vars,
            env_vars_from_files=env_vars_from_files,
            slack_url_path=slack_url_path,
            refresh_stack=refresh_stack,
            pulumi_put_attempts=pulumi_put_attempts,
            max_carried_changes=max_carried_changes,
        )

        default_github_issue_labels = [
            "product:infrastructure",
            "DevOps",
            "pipeline-workflow",
        ]
        if ci_stack:
            default_github_issue_labels.append("promotion-to-qa")
        elif qa_stack:
            default_github_issue_labels.append("promotion-to-production")
        elif production_stack:
            default_github_issue_labels.append("finalized-deployment")

        if enable_github_issue_resource:
            # The gate issue's body is the Pulumi resource summary, so that
            # closing it -- which is what promotes the change to the next
            # environment -- is a decision made on evidence rather than on the
            # job's colour. The summary reaches here via the Pulumi put's
            # implicit get; see _summary_artifact_path.
            pulumi_put = _pulumi_put_step(step_fragment)
            pulumi_put.no_get = False
            pulumi_put.get_params = {
                "summary_file": DEPLOY_SUMMARY_FILENAME,
                # A normal get exists to read stack outputs. This one exists only
                # to materialize the summary, and re-reading the stack here would
                # put a second Pulumi invocation on the success path of every
                # deploy -- where a failure would redden a deploy that worked.
                "read_outputs": False,
            }
            create_gh_issue = PutStep(
                put=gh_issues_post.name,
                params={
                    "labels": github_issue_labels or default_github_issue_labels,
                    "assignees": github_issue_assignees or [],
                    "body_files": [_summary_artifact_path(pulumi_put)],
                },
            )
            chain_fragment.resources.append(gh_issues_post)
            step_fragment.jobs[0].on_success = create_gh_issue

        chain_fragment.resource_types = (
            chain_fragment.resource_types + step_fragment.resource_types
        )
        chain_fragment.resources = chain_fragment.resources + step_fragment.resources

        if gh_issues_trigger:
            chain_fragment.resources.append(gh_issues_trigger)
        chain_fragment.jobs.extend(step_fragment.jobs)

    return chain_fragment


def pulumi_job(  # noqa: PLR0913
    pulumi_code: Resource,
    stack_name: str,
    project_name: str,
    project_source_path: Path,
    dependencies: list[GetStep] | None = None,
    additional_post_steps: list[GetStep | PutStep | TaskStep] | None = None,
    previous_job: Job | None = None,
    additional_env_vars: dict[str, str] | None = None,
    env_vars_from_files: dict[str, str] | None = None,
    slack_url_path: str | None = None,
    refresh_stack: bool = True,
    pulumi_put_attempts: int | None = None,
    max_carried_changes: int | str | None = None,
) -> PipelineFragment:
    """Create a job definition for running a Pulumi task.

    :param pulumi_code: A git resource that represents the repository for the code being
        executed
    :param stack_name: The stack name to use while executing the Pulumi task
    :param project_name: The name of the Pulumi project being executed
    :param project_source_path: The path within the `pulumi_code` resource where the
        code being executed is located
    :param dependencies: A list of `Get` step definitions that are used as inputs or
        triggers for the jobs in the chain
    :param previous_job: The job object that should be added as a `passed` dependency
        for the `get` step input for this job definition.
    :param slack_url_path: A Vault secret path containing the Slack webhook URL. When
        provided, failure, error, and abort notifications are sent to that Slack
        channel.
    :param refresh_stack: When ``False``, passes ``refresh_stack: false`` to the
        pulumi-provisioner resource so that ``pulumi refresh`` is skipped before
        ``pulumi up``.  Defaults to ``True`` (refresh enabled).
    :param pulumi_put_attempts: ``attempts`` for the pulumi-provisioner put step.
        Defaults to ``None`` -- no ``retry`` wrapper is emitted at all, so one
        Pulumi run produces one verdict.

        **Do not set this on a job whose success gates a promotion.** This
        previously defaulted to ``2`` (added as layer 3 of the orphaned-lock
        recovery in #45), and that retry is what let a failed update be reported
        as a green deploy.  Observed in ol-infrastructure's
        ``deploy-ol-substructure-keycloak`` build 158: attempt 1 genuinely
        failed (``2 errored``, ``update failed``), attempt 2 never ran Pulumi at
        all because the worker holding its input volume had gone away, and
        Concourse then re-executed the first attempt's step id, which exited 0
        having emitted no Pulumi output whatsoever -- no ``Refreshing``, no
        ``Updating``, no ``Resources:``.  The job's ``on_success`` fired and
        posted the ``[bot] Pulumi <project> <stack> deployed.`` issue that is
        the gate to the next environment.  The retry wrapper is what creates
        that code path; worker loss during input-volume streaming triggers it.

        Layer 1 of #45 -- ``pulumi_utils._with_lock_recovery``, which cancels a
        provably-orphaned lock inside the resource itself -- is unaffected by
        this default and still clears a stale lock on the next run.  What is
        lost is only the automatic same-build second attempt; the recovery now
        costs a re-trigger instead of costing trust in the deploy signal.

    :param max_carried_changes: How many per-resource changes ride on the version
        the Pulumi put emits, and therefore how many the promotion-gate issue body
        lists before it says "Showing N of M". ``0`` means no cap; omit for the
        resource's own default (200).

        The cap exists because that list is persisted by Concourse per-resource
        and carried through every later step, so an unbounded one would put
        megabytes there on a large refactor.  It is exposed here, and lands in the
        resource's ``source``, so that changing it is a pipeline re-set -- not an
        edit to the resource image followed by a release and a dependency bump.
        A pipeline can equally point it at a Concourse var.

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a full pipeline.
    """
    pulumi_provisioner_resource_type = pulumi_provisioner_resource()
    pulumi_resource = pulumi_provisioner(
        name=Identifier(f"pulumi-{project_name}"),
        project_name=project_name,
        project_path=f"{pulumi_code.name}/{project_source_path}",
        max_carried_changes=max_carried_changes,
    )
    passed_job = [previous_job.name] if previous_job else None
    pulumi_job_object = Job(
        name=Identifier(f"deploy-{project_name}-{stack_name.lower()}"),
        max_in_flight=1,  # Only allow 1 Pulumi task at a time since they lock anyway.
        plan=(dependencies or [])
        + [
            GetStep(
                get=pulumi_code.name,
                trigger=passed_job is None
                and not stack_name.lower().endswith("production"),
                passed=passed_job,
            ),
            PutStep(
                inputs="all",
                put=pulumi_resource.name,
                no_get=True,
                attempts=pulumi_put_attempts,
                params={
                    "env_os": {
                        "AWS_DEFAULT_REGION": "us-east-1",
                        "PYTHONPATH": (
                            f"/usr/lib/:/tmp/build/put/{pulumi_code.name}/src/"
                        ),
                        "GITHUB_TOKEN": "((github.public_repo_access_token))",
                        **(additional_env_vars or {}),
                    },
                    "stack_name": stack_name,
                    "env_vars_from_files": env_vars_from_files or {},
                    **({"refresh_stack": False} if not refresh_stack else {}),
                },
            ),
        ]
        + (additional_post_steps or []),
    )
    extra_resources: list[Resource] = []
    extra_resource_types = [pulumi_provisioner_resource_type]

    if slack_url_path:
        slack_resource = slack_notification(
            name=Identifier(f"slack-alert-{project_name}"),
            url=f"(({slack_url_path}))",
        )
        extra_resources.append(slack_resource)
        extra_resource_types.append(slack_notification_resource())
        notification_body = (
            f"Pulumi job deploy-{project_name}-{stack_name.lower()} encountered a"
            " problem. Check the pipeline for details."
        )
        pulumi_job_object.on_failure = notification(
            resource=slack_resource,
            title=f"Pulumi {project_name} {stack_name} failed",
            body=notification_body,
            alert_type="failed",
        )
        pulumi_job_object.on_error = notification(
            resource=slack_resource,
            title=f"Pulumi {project_name} {stack_name} errored",
            body=notification_body,
            alert_type="errored",
        )
        pulumi_job_object.on_abort = notification(
            resource=slack_resource,
            title=f"Pulumi {project_name} {stack_name} aborted",
            body=notification_body,
            alert_type="aborted",
        )

    return PipelineFragment(
        resources=[pulumi_resource, *extra_resources],
        resource_types=extra_resource_types,
        jobs=[pulumi_job_object],
    )


# Filename the preview job's implicit get writes its diff to.
PREVIEW_SUMMARY_FILENAME = "preview_summary.md"

# Written alongside PREVIEW_SUMMARY_FILENAME when the preview found nothing
# worth reviewing. The gate-post put checks for its presence to skip opening
# a fresh gate issue for an empty diff. Must match
# `NO_CHANGES_MARKER_SUFFIX` in resources/pulumi/concourse.py.
PREVIEW_NO_CHANGES_MARKER = f"{PREVIEW_SUMMARY_FILENAME}.no-changes"


def _stage_inputs(
    deps: list[GetStep] | None,
    *,
    allow_trigger: bool,
    correlate_with: str | None = None,
) -> list[GetStep]:
    """Copy a stage's extra inputs for one job, neutering triggers where unsafe.

    Stage inputs are not merely triggers -- callers pass artifacts the Pulumi run
    consumes (k8s_apps hands in a `deployment.json`, kubewatch a `passed`-gated
    build). Both the preview and the deploy run Pulumi over the same tree, so
    both need them.

    ★ BUT `trigger: true` MUST NOT REACH A GATED DEPLOY. That job is supposed to
    start only when a human closes the gate; a triggering input would fire it on
    a new image or upstream build and walk straight past the approval the whole
    topology exists to require. Copies are deep so a caller's step object is
    never mutated -- the existing chain mutates shared dependencies in place and
    it is a long-standing trap.
    """
    inputs: list[GetStep] = []
    for dep in deps or []:
        step = dep.model_copy(deep=True)
        if not allow_trigger and getattr(step, "trigger", None):
            step.trigger = False
        if correlate_with:
            # ★ Constrain the input to a version the PREVIEW actually saw.
            # Without this only the Pulumi code get is tied to the preview, so
            # a newer image landing while the gate is open would be applied by
            # the deploy even though the approved diff was rendered against the
            # old one -- edxapp passes the image digest straight into Pulumi as
            # EDXAPP_DOCKER_IMAGE_DIGEST, so that is a different deploy than the
            # one on the issue. Still set membership rather than equality, but
            # it bounds the set to versions the preview ran with.
            step.passed = [*(step.passed or []), correlate_with]
        inputs.append(step)
    return inputs


def _split_stage_steps(
    steps: list[Any],
) -> tuple[list[GetStep], list[Any]]:
    """Split a stage's steps into read-only inputs and side effects.

    ★ A `GetStep` is an INPUT: it fetches an artifact, changes nothing outside
    the build, and both the preview and the deploy need it because both run
    Pulumi over the same tree.

    ★ ANYTHING ELSE IS A SIDE EFFECT and belongs to the deploy alone. Despite
    the `dict[int, list[GetStep]]` annotation, callers put `PutStep`s in
    `custom_dependencies` -- k8s_apps opens a GitHub Deployment with
    `action: start` there, pairing it with the `action: finish` in
    `additional_post_steps`. Running that from a preview would open a
    Deployment for a promotion nobody has approved yet, leaving it `pending`
    until someone closes the gate, or forever if they never do.

    A non-Get step that a preview genuinely needed would fail the preview and
    so block the gate -- fail-closed and visible, rather than a silent unwanted
    write. No current caller does that.
    """
    inputs = [step for step in steps if isinstance(step, GetStep)]
    effects = [step for step in steps if not isinstance(step, GetStep)]
    return inputs, effects


def _stack_serial_group(project_name: str, stack_name: str) -> str:
    """Return the serial group shared by a stack's preview and deploy jobs.

    ★ A `pulumi preview` TAKES THE STACK LOCK, so the preview and the deploy of
    one stack must never run concurrently. Splitting them into two jobs loses
    the `max_in_flight=1` that protected the single combined job, and lock
    recovery will NOT save us: `_is_recoverable_lock` only cancels locks older
    than 15 minutes, so a live preview's lock blocks a real deploy outright.
    """
    return f"{project_name}-{stack_name}".lower().replace(".", "-")


def _dispatch_preview_gated(
    *,
    enable_github_issue_resource: bool,
    github_issue_repository: str | None,
    **kwargs: Any,
) -> PipelineFragment:
    """Validate preview-gated inputs, then build.

    Rejects inputs this topology cannot honour rather than accepting and
    silently ignoring them -- a dropped setting is invisible until the thing it
    configured fails to happen.
    """
    if not enable_github_issue_resource:
        msg = (
            "topology='preview-gated' requires enable_github_issue_resource=True: "
            "the gate issue is what triggers each deploy"
        )
        raise ValueError(msg)
    if github_issue_repository is None:
        msg = "github_issue_repository is required for topology='preview-gated'"
        raise ValueError(msg)
    return _preview_gated_chain(
        github_issue_repository=github_issue_repository, **kwargs
    )


def _preview_gated_chain(  # noqa: PLR0913, PLR0915
    pulumi_code: Resource,
    stack_names: list[str],
    project_name: str,
    project_source_path: Path,
    github_issue_repository: str,
    auto_deploy_stages: list[str] | None = None,
    github_issue_assignees: list[str] | None = None,
    github_issue_labels: list[str] | None = None,
    dependencies: list[GetStep] | None = None,
    custom_dependencies: dict[int, list[GetStep]] | None = None,
    additional_post_steps: dict[int, list[GetStep | PutStep | TaskStep]] | None = None,
    additional_env_vars: dict[str, str] | None = None,
    env_vars_from_files: dict[str, str] | None = None,
    refresh_stack: bool = True,
    pulumi_put_attempts: int | None = None,
    max_carried_changes: int | str | None = None,
    slack_url_path: str | None = None,
) -> PipelineFragment:
    """Build the ``preview-gated`` topology: gate each stack on a preview OF ITSELF.

    Every gated stack becomes two jobs:

        preview-<project>-<stack>   runs `pulumi preview` and OPENS the gate
                                    issue with that stack's own diff
        deploy-<project>-<stack>    runs `pulumi up`, triggered by the gate
                                    issue being closed

    Why this beats previewing the *next* stack from the current stack's deploy
    job, which is what the retired `preview_next_stack` option did:

    - IT WORKS FOR SINGLETONS. A one-stack chain -- a production-only stack, or
      a `default` stack -- has no "next" to preview, so it got no gate at all.
      Here every gated stack previews itself.
    - IT WORKS ACROSS CHAINS. edxapp splits one deployment across Open edX
      releases, so mitx CI and mitx QA live in different `pulumi_jobs_chain`
      calls and a next-stack preview cannot span them. Self-preview does not
      care.
    - THE DIFF IS OF THE THING BEING APPROVED, and taken immediately before the
      deploy it authorises, rather than one stage and possibly days earlier.

    ★ `passed=[preview_job]` ON THE DEPLOY'S CODE GET is what stops a change
    reaching an environment without having been previewed. Note it is set
    MEMBERSHIP, not equality: if two commits both pass the preview, closing the
    gate deploys the newer one. The gate issue is therefore updated in place, so
    its body always shows the diff that will actually apply rather than the
    first one posted.

    ★ THIS IS FAIL-CLOSED, where the retired next-stack preview was advisory
    and could never fail a deploy. Here the preview generates the gate, so a
    preview that fails means no gate opens and nothing deploys. That is correct
    for a promotion gate, but it puts preview reliability on the deploy path:
    anything the Pulumi *program* does at construction time that can fail --
    notably an imperative AWS call -- now blocks the environment rather than
    printing a warning. (This is not hypothetical: edxapp `xpro.Production`
    was blocked on exactly that within hours of adoption, by a boto3
    ModifyDBInstance call made while building the program.)

    *auto_deploy_stages* names the stages that keep today's behaviour: no gate,
    auto-deploy on code change. Typically ``["CI"]`` -- gating CI would destroy
    the fast feedback loop it exists to provide. Matched case-insensitively
    against both the full stack name and its trailing dotted segment, so both
    ``CI`` and ``mitx.CI`` work.
    """
    exempt = {s.lower() for s in (auto_deploy_stages or [])}

    def is_exempt(stack: str) -> bool:
        return stack.lower() in exempt or stack.rsplit(".", 1)[-1].lower() in exempt

    chain = PipelineFragment(
        resource_types=[github_issues_resource(), pulumi_provisioner_resource()]
    )
    pulumi_resource = pulumi_provisioner(
        name=Identifier(f"pulumi-{project_name}"),
        project_name=project_name,
        project_path=f"{pulumi_code.name}/{project_source_path}",
        max_carried_changes=max_carried_changes,
    )
    chain.resources.append(pulumi_resource)

    if slack_url_path:
        slack_resource = slack_notification(
            name=Identifier(f"slack-alert-{project_name}"),
            url=f"(({slack_url_path}))",
        )
        chain.resources.append(slack_resource)
        chain.resource_types.append(slack_notification_resource())
    else:
        slack_resource = None

    common_params: dict[str, Any] = {
        "env_os": {
            "AWS_DEFAULT_REGION": "us-east-1",
            "PYTHONPATH": f"/usr/lib/:/tmp/build/put/{pulumi_code.name}/src/",
            **(additional_env_vars or {}),
        },
        "env_vars_from_files": env_vars_from_files or {},
        **({"refresh_stack": False} if not refresh_stack else {}),
    }

    _BASE_LABELS = ["product:infrastructure", "DevOps", "pipeline-workflow"]

    def _gate_labels(stack: str) -> list[str]:
        """Labels for the "ready to deploy" gate.

        ★ A GATE DESCRIBES WHAT CLOSING IT WILL DO, which under this topology is
        deploying THIS stage. The deploy-chained labels describe the NEXT stage,
        because there the issue is posted *after* a stage deploys and closing it
        promotes onward. Reusing them here labelled the QA gate
        `promotion-to-production` and the Production gate `finalized-deployment`
        before anything had been deployed at all.
        """
        if github_issue_labels is not None:
            return github_issue_labels
        target = stack.rsplit(".", 1)[-1].lower()
        return [*_BASE_LABELS, f"promotion-to-{target}"]

    def _record_labels(stack: str) -> list[str]:
        """Labels for the "deployed" record -- completion, not approval."""
        if github_issue_labels is not None:
            return github_issue_labels
        base = [*_BASE_LABELS, "deployed"]
        if stack.rsplit(".", 1)[-1].lower() == "production":
            base.append("finalized-deployment")
        return base

    def _alerts(job: Job, stack: str, kind: str) -> None:
        """Attach Slack failure/error/abort hooks, matching pulumi_job."""
        if not slack_resource:
            return
        body = (
            f"Pulumi {kind} {project_name} {stack} encountered a problem."
            " Check the pipeline for details."
        )
        job.on_failure = notification(
            resource=slack_resource,
            title=f"Pulumi {kind} {project_name} {stack} failed",
            body=body,
            alert_type="failed",
        )
        job.on_error = notification(
            resource=slack_resource,
            title=f"Pulumi {kind} {project_name} {stack} errored",
            body=body,
            alert_type="errored",
        )

    previous_deploy: Job | None = None
    for index, stack_name in enumerate(stack_names):
        # Chain-wide dependencies plus this stage's index-keyed custom ones.
        stage_inputs, stage_effects = _split_stage_steps(
            [
                *(dependencies or []),
                *((custom_dependencies or {}).get(index) or []),
            ]
        )
        post_steps = list((additional_post_steps or {}).get(index) or [])
        slug = stack_name.lower().replace(".", "-")
        serial_group = _stack_serial_group(project_name, stack_name)
        passed_from = [previous_deploy.name] if previous_deploy else None

        record_issue = github_issues(
            auth_method="token",
            name=Identifier(f"gh-{project_name.lower()}-{slug}-deployed"),
            repository=github_issue_repository,
            issue_title_template=f"[bot] Pulumi {project_name} {stack_name} deployed.",
            issue_prefix=f"[bot] Pulumi {project_name} {stack_name} deployed.",
            issue_state="open",
        )
        chain.resources.append(record_issue)

        if is_exempt(stack_name):
            deploy = Job(
                name=Identifier(f"deploy-{project_name}-{slug}"),
                serial_groups=[Identifier(serial_group)],
                plan=[
                    # Entry point for an exempt stage: triggers are wanted here.
                    *_stage_inputs(stage_inputs, allow_trigger=True),
                    *stage_effects,
                    GetStep(get=pulumi_code.name, trigger=True, passed=passed_from),
                    PutStep(
                        inputs="all",
                        put=pulumi_resource.name,
                        attempts=pulumi_put_attempts,
                        no_get=False,
                        get_params={
                            "summary_file": DEPLOY_SUMMARY_FILENAME,
                            "read_outputs": False,
                        },
                        params={**common_params, "stack_name": stack_name},
                    ),
                    *post_steps,
                ],
                on_success=PutStep(
                    put=record_issue.name,
                    params={
                        "assignees": github_issue_assignees or [],
                        "labels": _record_labels(stack_name),
                        "body_files": [
                            f"{pulumi_resource.name}/{DEPLOY_SUMMARY_FILENAME}"
                        ],
                    },
                ),
            )
            _alerts(deploy, stack_name, "deploy")
            chain.jobs.append(deploy)
            previous_deploy = deploy
            continue

        gate_post = github_issues(
            auth_method="token",
            name=Identifier(f"gh-{project_name.lower()}-{slug}-gate-post"),
            repository=github_issue_repository,
            issue_title_template=(
                f"[bot] Pulumi {project_name} {stack_name} ready to deploy."
            ),
            issue_prefix=f"[bot] Pulumi {project_name} {stack_name} ready to deploy.",
            issue_state="open",
            # The body must show the diff that will ACTUALLY apply. A newer
            # commit re-previews; appending a comment would leave the stale
            # first diff at the top of what a reviewer reads.
            update_in_place=True,
        )
        gate_trigger = github_issues(
            auth_method="token",
            name=Identifier(f"gh-{project_name.lower()}-{slug}-gate-trigger"),
            repository=github_issue_repository,
            issue_title_template=(
                f"[bot] Pulumi {project_name} {stack_name} ready to deploy."
            ),
            issue_prefix=f"[bot] Pulumi {project_name} {stack_name} ready to deploy.",
            issue_state="closed",
            poll_frequency="15m",
        )
        chain.resources.extend([gate_post, gate_trigger])

        preview = Job(
            name=Identifier(f"preview-{project_name}-{slug}"),
            serial_groups=[Identifier(serial_group)],
            plan=[
                # The preview IS the entry point of a gated stage, so this is
                # where upstream triggers belong.
                *_stage_inputs(stage_inputs, allow_trigger=True),
                GetStep(get=pulumi_code.name, trigger=True, passed=passed_from),
                PutStep(
                    inputs="all",
                    put=pulumi_resource.name,
                    attempts=pulumi_put_attempts,
                    no_get=False,
                    get_params={
                        "summary_file": PREVIEW_SUMMARY_FILENAME,
                        "read_outputs": False,
                        "preview_stack": stack_name,
                    },
                    params={
                        **common_params,
                        "stack_name": stack_name,
                        "preview": True,
                    },
                ),
            ],
            on_success=PutStep(
                put=gate_post.name,
                params={
                    "assignees": github_issue_assignees or [],
                    "labels": _gate_labels(stack_name),
                    "body_files": [
                        f"{pulumi_resource.name}/{PREVIEW_SUMMARY_FILENAME}"
                    ],
                    # No diff means nothing to approve -- skip opening a
                    # fresh gate. An already-open gate is still updated (see
                    # `update_in_place` above), never left stale.
                    "skip_if_file": (
                        f"{pulumi_resource.name}/{PREVIEW_NO_CHANGES_MARKER}"
                    ),
                },
            ),
        )
        _alerts(preview, stack_name, "preview")

        deploy = Job(
            name=Identifier(f"deploy-{project_name}-{slug}"),
            serial_groups=[Identifier(serial_group)],
            plan=[
                # Same inputs, triggers stripped so only the gate starts this
                # job, and correlated with the preview so the deploy cannot
                # apply a version the approved diff was not rendered against.
                *_stage_inputs(
                    stage_inputs, allow_trigger=False, correlate_with=str(preview.name)
                ),
                GetStep(get=gate_trigger.name, trigger=True),
                # `passed` is the guarantee: only code that went through this
                # stack's own preview is eligible to deploy to it.
                GetStep(get=pulumi_code.name, trigger=False, passed=[preview.name]),
                # Side effects run here, after the gate and before the apply --
                # e.g. opening a GitHub Deployment with `action: start`, which
                # must bracket the Pulumi run and must never fire off a preview.
                *stage_effects,
                PutStep(
                    inputs="all",
                    put=pulumi_resource.name,
                    no_get=False,
                    get_params={
                        "summary_file": DEPLOY_SUMMARY_FILENAME,
                        "read_outputs": False,
                    },
                    params={**common_params, "stack_name": stack_name},
                ),
                *post_steps,
            ],
            on_success=PutStep(
                put=record_issue.name,
                params={
                    "assignees": github_issue_assignees or [],
                    "labels": _record_labels(stack_name),
                    "body_files": [f"{pulumi_resource.name}/{DEPLOY_SUMMARY_FILENAME}"],
                },
            ),
        )

        _alerts(deploy, stack_name, "deploy")

        chain.jobs.extend([preview, deploy])
        previous_deploy = deploy

    return chain
