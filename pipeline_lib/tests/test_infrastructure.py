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


class TestDeploySummaryInGateIssue:
    """The promotion-gate issue body must carry the Pulumi resource summary.

    Closing the `[bot] Pulumi <project> <stack> deployed.` issue is what promotes
    a change to the next environment, and the issue used to carry only a title --
    so the human closing it was trusting the job's colour rather than evidence.
    """

    def test_gate_issue_body_reads_the_summary_artifact(self):
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        for job in fragment.jobs:
            params = job.on_success.params or {}
            assert (
                params["body_file"] == "pulumi-ol-application-airbyte/deploy_summary.md"
            )

    def test_put_enables_implicit_get_to_produce_the_artifact(self):
        """A put produces no artifacts; only its implicit get does.

        If no_get stayed True the summary would never reach the issue put, and
        the body_file above would point at a path that does not exist.
        """
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        for i in range(len(fragment.jobs)):
            put = _get_pulumi_put_step(fragment, i)
            assert put.no_get is False
            assert put.get_params["summary_file"] == "deploy_summary.md"

    def test_implicit_get_does_not_re_read_the_stack(self):
        """read_outputs must be False on the implicit get.

        This get runs on the success path of every deploy. Leaving the stack read
        on would add a second Pulumi invocation there, and a failure in it would
        redden a deploy that actually applied.
        """
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        assert _get_pulumi_put_step(fragment).get_params["read_outputs"] is False

    def test_body_file_path_matches_the_put_resource_name(self):
        """The artifact is named after the resource, so the two must not drift."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-substructure-keycloak",
            project_source_path=Path("src/ol_infrastructure/substructure/keycloak"),
            github_issue_repository="org/repo",
        )
        put = _get_pulumi_put_step(fragment)
        body_file = (fragment.jobs[0].on_success.params or {})["body_file"]
        assert body_file.split("/")[0] == str(put.put)

    def test_no_summary_wiring_when_issues_are_disabled(self):
        """Nothing to feed, so don't pay for the extra implicit get."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            enable_github_issue_resource=False,
        )
        put = _get_pulumi_put_step(fragment)
        assert put.no_get is True
        assert put.get_params is None


def _pulumi_resource(fragment):
    """Return the pulumi-provisioner Resource from a fragment."""
    for resource in fragment.resources:
        if resource.type == "pulumi-provisioner":
            return resource
    msg = "no pulumi-provisioner resource in fragment"
    raise AssertionError(msg)


class TestMaxCarriedChangesReachesThePipeline:
    """The cap must be settable without editing and releasing the resource image.

    It lands in the resource's `source`, so an operator who finds 200 too few
    (or too many) after seeing a real gate issue changes a pipeline and re-sets
    it, rather than waiting on an image release and a dependency bump.
    """

    def test_absent_from_source_by_default(self):
        """Don't pin a value the resource already defaults to."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        assert "max_carried_changes" not in _pulumi_resource(fragment).source

    def test_value_lands_in_resource_source(self):
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
            max_carried_changes=500,
        )
        assert _pulumi_resource(fragment).source["max_carried_changes"] == 500

    def test_zero_is_emitted_not_dropped_as_falsy(self):
        """0 means "no cap" and must survive to the YAML."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
            max_carried_changes=0,
        )
        assert _pulumi_resource(fragment).source["max_carried_changes"] == 0

    def test_accepts_a_concourse_var_reference(self):
        """Deferring to a var is what makes this tunable with no code at all."""
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
            max_carried_changes="((pulumi.max_carried_changes))",
        )
        source = _pulumi_resource(fragment).source
        assert source["max_carried_changes"] == "((pulumi.max_carried_changes))"
