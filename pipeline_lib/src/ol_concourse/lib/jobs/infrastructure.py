"""Concourse pipeline jobs for infrastructure provisioning and management."""

from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path

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


def _next_stack_preview_step(pulumi_put: PutStep, next_stack: str) -> PutStep:
    """Build the advisory `pulumi preview` of the next environment.

    Re-labelled rather than given its own resource: ``put`` names the *artifact*
    and ``resource`` names what to actually run, so this reuses the same
    pulumi-provisioner while landing its implicit get under a distinct name.
    Two puts to one resource name in a single job would otherwise collide on
    that artifact.

    ``fail_on_error: false`` is the load-bearing part. This runs after the
    deploy has already applied, so a preview that cannot reach the next
    environment must degrade to a note in the issue body -- never report red on
    infrastructure that is live and correct.
    """
    return PutStep(
        put=Identifier(f"{pulumi_put.put}-next-preview"),
        resource=str(pulumi_put.put),
        inputs="all",
        no_get=False,
        get_params={
            "summary_file": DEPLOY_SUMMARY_FILENAME,
            "read_outputs": False,
            "preview_stack": next_stack,
        },
        params={
            **(pulumi_put.params or {}),
            "stack_name": next_stack,
            "preview": True,
            "fail_on_error": False,
        },
    )


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
    preview_next_stack: bool = False,
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
    :param preview_next_stack: When ``True``, each gate issue also carries a
        ``pulumi preview`` of the NEXT stack in the chain -- what closing the
        issue will actually apply.  Defaults to ``False``.

        This is additive, not a replacement for the applied diff.  The applied
        diff is evidence the deploy *happened*; a preview of the next
        environment says nothing about that, and the last stack in a chain has
        no next environment to preview.  What it adds is the one thing the
        applied diff structurally cannot show: drift in the target environment,
        which is precisely the surprise a promotion gate exists to catch.

        Off by default because it is not free -- it runs a real ``pulumi
        preview`` against the next environment on the success path of every
        deploy, with the API load on that environment's control plane that
        implies.  The preview is failure-tolerant and cannot fail the deploy.
    :type custom_dependencies: Dict[int, list[GetStep]]

    :returns: A `PipelineFragment` object that can be composed with other fragments to
              build a full pipeline.
    """
    if enable_github_issue_resource and github_issue_repository is None:
        msg = (
            "github_issue_repository is required when enable_github_issue_resource=True"
        )
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
            body_files = [_summary_artifact_path(pulumi_put)]

            next_stack = (
                stack_names[index + 1] if index + 1 < len(stack_names) else None
            )
            if preview_next_stack and next_stack:
                preview_put = _next_stack_preview_step(pulumi_put, next_stack)
                # Appended to the job's own plan rather than to on_success: the
                # gate issue is what must read its artifact, and on_success is a
                # single step. The put cannot fail the job -- see fail_on_error.
                step_fragment.jobs[0].plan.append(preview_put)
                body_files.append(_summary_artifact_path(preview_put))

            create_gh_issue = PutStep(
                put=gh_issues_post.name,
                params={
                    "labels": github_issue_labels or default_github_issue_labels,
                    "assignees": github_issue_assignees or [],
                    # A single body_file would only ever be the applied diff.
                    # body_files composes the applied diff and, when enabled, the
                    # preview of what promoting will do to the next environment.
                    "body_files": body_files,
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
