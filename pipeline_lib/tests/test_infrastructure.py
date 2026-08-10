"""Tests for ol_concourse.lib.jobs.infrastructure."""

from pathlib import Path


from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.pipeline import (
    Identifier,
    PutStep,
    Resource,
    TryStep,
)


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
            assert params["body_files"] == [
                "pulumi-ol-application-airbyte/deploy_summary.md"
            ]

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
        body_files = (fragment.jobs[0].on_success.params or {})["body_files"]
        assert body_files[0].split("/")[0] == str(put.put)

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


def _preview_try(fragment, job_index: int = 0):
    """Return the TryStep wrapping the next-stack preview, or None if absent."""
    for step in fragment.jobs[job_index].plan:
        if isinstance(step, TryStep) and str(getattr(step.try_, "put", "")).endswith(
            "-next-preview"
        ):
            return step
    return None


def _preview_put(fragment, job_index: int = 0):
    """Return the next-stack preview PutStep, or None if absent."""
    wrapper = _preview_try(fragment, job_index)
    return wrapper.try_ if wrapper else None


class TestPreviewNextStack:
    """Opt-in preview of what promoting will apply to the next environment."""

    def _chain(self, **kw):
        return pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA", "Production"],
            project_name="ol-substructure-keycloak",
            project_source_path=Path("src/ol_infrastructure/substructure/keycloak"),
            github_issue_repository="org/repo",
            **kw,
        )

    def test_off_by_default(self):
        """It runs real Pulumi against another environment -- never implicitly."""
        fragment = self._chain()
        for i in range(len(fragment.jobs)):
            assert _preview_put(fragment, i) is None

    def test_each_job_previews_the_following_stack(self):
        fragment = self._chain(preview_next_stack=True)
        assert _preview_put(fragment, 0).params["stack_name"] == "QA"
        assert _preview_put(fragment, 1).params["stack_name"] == "Production"

    def test_last_stack_has_no_preview(self):
        """Production has no next environment; there is nothing to preview."""
        fragment = self._chain(preview_next_stack=True)
        assert _preview_put(fragment, 2) is None
        body_files = (fragment.jobs[2].on_success.params or {})["body_files"]
        assert len(body_files) == 1, (
            "production gate should carry only the applied diff"
        )

    def test_preview_cannot_fail_the_deploy(self):
        """The deploy has already applied by the time this runs."""
        fragment = self._chain(preview_next_stack=True)
        assert _preview_put(fragment, 0).params["fail_on_error"] is False

    def test_preview_is_relabelled_onto_the_same_resource(self):
        """Two puts to one resource name would collide on the implicit-get artifact.

        `put` names the artifact, `resource` names what to run -- so the preview
        gets its own artifact without needing a second resource definition.
        """
        fragment = self._chain(preview_next_stack=True)
        main, preview = _get_pulumi_put_step(fragment), _preview_put(fragment)
        assert preview.resource == str(main.put)
        assert str(preview.put) != str(main.put)
        provisioner_names = {
            str(r.name) for r in fragment.resources if r.type == "pulumi-provisioner"
        }
        assert provisioner_names == {str(main.put)}, (
            "the preview must not introduce a second provisioner resource"
        )

    def test_gate_body_composes_applied_diff_then_preview(self):
        """Order matters: what happened, then what will happen."""
        fragment = self._chain(preview_next_stack=True)
        body_files = (fragment.jobs[0].on_success.params or {})["body_files"]
        assert body_files == [
            "pulumi-ol-substructure-keycloak/deploy_summary.md",
            "pulumi-ol-substructure-keycloak-next-preview/deploy_summary.md",
        ]

    def test_preview_get_is_told_which_stack_it_previewed(self):
        """The rendered body names the target, so the reviewer knows the scope."""
        fragment = self._chain(preview_next_stack=True)
        assert _preview_put(fragment, 0).get_params["preview_stack"] == "QA"

    def test_preview_get_does_not_re_read_the_stack(self):
        fragment = self._chain(preview_next_stack=True)
        assert _preview_put(fragment, 0).get_params["read_outputs"] is False

    def test_applied_diff_is_still_present_when_preview_is_on(self):
        """Additive, not a replacement -- the fabricated-green evidence stays."""
        fragment = self._chain(preview_next_stack=True)
        main = _get_pulumi_put_step(fragment)
        assert main.params.get("preview") is not True
        assert (fragment.jobs[0].on_success.params or {})["body_files"][0].startswith(
            str(main.put)
        )

    def test_preview_is_wrapped_in_try(self):
        """`fail_on_error` alone is not enough to keep the deploy green.

        It only catches exceptions raised INSIDE the resource script. A
        Concourse-level failure -- container creation, image pull, worker loss,
        or the implicit get -- happens outside it and would fail the job,
        suppress the on_success gate, and report red on a deploy that already
        applied. `try` is what makes the guarantee actually hold.
        """
        fragment = self._chain(preview_next_stack=True)
        wrapper = _preview_try(fragment, 0)
        assert wrapper is not None, "preview put must be wrapped in a try step"
        assert str(wrapper.try_.put).endswith("-next-preview")

    def test_main_deploy_put_is_not_wrapped_in_try(self):
        """The deploy itself must still be able to fail the job."""
        fragment = self._chain(preview_next_stack=True)
        main = _get_pulumi_put_step(fragment)
        assert any(
            isinstance(step, PutStep) and step is main for step in fragment.jobs[0].plan
        ), "the real deploy put must remain a bare plan step"
