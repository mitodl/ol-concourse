"""Tests for ol_concourse.lib.jobs.infrastructure."""

from pathlib import Path


from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.pipeline import Identifier, PutStep, Resource


def _make_pulumi_code(name: str = "my-repo") -> Resource:
    return Resource(
        name=Identifier(name),
        type="git",
        source={"uri": "https://github.com/example/repo"},
    )


class TestPulumiJobsChainGitHubIssueNaming:
    """GitHub issue resource names must be consistent between creation and reference."""

    def test_trigger_resource_names_include_project_name(self):
        """Trigger resources are named github-issues-{project}-{stack}-trigger."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA", "Production"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        resource_names = {str(r.name) for r in fragment.resources}
        assert "github-issues-ol-application-airbyte-ci-trigger" in resource_names
        assert "github-issues-ol-application-airbyte-qa-trigger" in resource_names

    def test_job_get_steps_reference_correct_trigger_names(self):
        """Jobs must get the trigger resource using the same project-qualified name."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA", "Production"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        # Collect all get step names from all jobs
        gets = set()
        for job in fragment.jobs:
            for step in job.plan:
                if hasattr(step, "get"):
                    gets.add(str(step.get))
                if hasattr(step, "do"):
                    for substep in step.do or []:
                        if hasattr(substep, "get"):
                            gets.add(str(substep.get))

        assert "github-issues-ci-trigger" not in gets, (
            "Job referenced bare 'github-issues-ci-trigger' without project name"
        )
        assert "github-issues-qa-trigger" not in gets, (
            "Job referenced bare 'github-issues-qa-trigger' without project name"
        )
        assert "github-issues-ol-application-airbyte-ci-trigger" in gets
        assert "github-issues-ol-application-airbyte-qa-trigger" in gets

    def test_all_get_references_have_matching_resource(self):
        """Every get step referencing a github-issues trigger must have a resource."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA", "Production"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        resource_names = {str(r.name) for r in fragment.resources}
        for job in fragment.jobs:
            for step in job.plan:
                if hasattr(step, "get") and "github-issues" in str(step.get):
                    assert str(step.get) in resource_names, (
                        f"Job '{job.name}' gets '{step.get}' which has no resource"
                    )
                if hasattr(step, "do"):
                    for substep in step.do or []:
                        if hasattr(substep, "get") and "github-issues" in str(
                            substep.get
                        ):
                            assert str(substep.get) in resource_names, (
                                f"Job '{job.name}' gets '{substep.get}' "
                                "which has no resource"
                            )

    def test_no_unused_trigger_resources(self):
        """Every github-issues trigger resource must be referenced by a job."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA", "Production"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        gets = set()
        for job in fragment.jobs:
            for step in job.plan:
                if hasattr(step, "get"):
                    gets.add(str(step.get))
                if hasattr(step, "do"):
                    for substep in step.do or []:
                        if hasattr(substep, "get"):
                            gets.add(str(substep.get))

        for resource in fragment.resources:
            name = str(resource.name)
            if name.endswith("-trigger"):
                assert name in gets, (
                    f"Trigger resource '{name}' is defined but never used by any job"
                )


def _get_pulumi_put_step(fragment, job_index: int = 0) -> PutStep:
    """Return the PutStep that invokes the pulumi-provisioner resource."""
    job = fragment.jobs[job_index]
    for step in job.plan:
        if isinstance(step, PutStep) and str(step.put).startswith("pulumi-"):
            return step
    msg = f"No pulumi PutStep found in job {job_index}"
    raise AssertionError(msg)


class TestRefreshStack:
    """refresh_stack param controls whether pulumi refresh is skipped."""

    def test_refresh_stack_true_by_default(self):
        """When refresh_stack is omitted, refresh_stack key is absent from params."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        put = _get_pulumi_put_step(fragment)
        assert "refresh_stack" not in (put.params or {}), (
            "refresh_stack should be absent when default (True) is used"
        )

    def test_refresh_stack_false_injects_param(self):
        """When refresh_stack=False, params must include refresh_stack: False."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA", "Production"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
            refresh_stack=False,
        )
        for i in range(len(fragment.jobs)):
            put = _get_pulumi_put_step(fragment, i)
            assert (put.params or {}).get("refresh_stack") is False, (
                f"Job {i} missing refresh_stack=False in params"
            )

    def test_refresh_stack_true_explicit(self):
        """When refresh_stack=True explicitly, refresh_stack key is absent."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
            refresh_stack=True,
        )
        put = _get_pulumi_put_step(fragment)
        assert "refresh_stack" not in (put.params or {}), (
            "refresh_stack should be absent when explicitly True"
        )


class TestPulumiJobAttempts:
    """The Pulumi PutStep must NOT retry by default.

    A retried Pulumi put can report a green deploy for an update that failed --
    ol-infrastructure's deploy-ol-substructure-keycloak build 158 -- and that
    green fires the job's on_success, which posts the promotion-gate issue. One
    Pulumi run, one verdict. See pulumi_job's pulumi_put_attempts docstring.
    """

    def test_pulumi_put_step_has_no_attempts_by_default(self):
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        put = _get_pulumi_put_step(fragment)
        assert put.attempts is None, (
            "Pulumi PutStep must not retry by default -- a retry can turn a failed "
            "update into a green promotion-gate signal"
        )

    def test_no_retry_wrapper_in_serialized_plan(self):
        """attempts=None must drop the `retry` wrapper from the emitted plan.

        attempts=1 would still serialize an `attempts: 1` key; None omits the
        field entirely, which is what keeps `retry` out of the build plan.
        """
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        put = _get_pulumi_put_step(fragment)
        assert "attempts" not in put.model_dump(exclude_none=True, by_alias=True)

    def test_all_stacks_in_chain_have_no_attempts(self):
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA", "Production"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        for i in range(len(fragment.jobs)):
            put = _get_pulumi_put_step(fragment, i)
            assert put.attempts is None, f"Job {i} Pulumi PutStep must not retry"

    def test_attempts_are_opt_in(self):
        """A caller that knowingly wants the old behaviour can still ask for it."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
            pulumi_put_attempts=2,
        )
        for i in range(len(fragment.jobs)):
            assert _get_pulumi_put_step(fragment, i).attempts == 2

    def test_gate_issue_stays_on_success_not_ensure(self):
        """The promotion-gate issue must only post when the job actually passed.

        Dropping the retry is only half the guarantee: if the gate put were ever
        moved to `ensure`, it would post on failure too and the deploy signal
        would be worthless again.
        """
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        for job in fragment.jobs:
            assert job.ensure is None, (
                f"Job {job.name} must not post the gate on ensure"
            )
            assert job.on_success is not None, f"Job {job.name} lost its gate put"
