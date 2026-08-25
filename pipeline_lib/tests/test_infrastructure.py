"""Tests for ol_concourse.lib.jobs.infrastructure."""

from pathlib import Path

import pytest

from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    PutStep,
    Resource,
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


def _job(fragment, name_fragment: str):
    for job in fragment.jobs:
        if name_fragment in str(job.name):
            return job
    return None


def _gated_chain(**kw):
    return pulumi_jobs_chain(
        _make_pulumi_code(),
        stack_names=kw.pop("stack_names", ["CI", "QA", "Production"]),
        project_name="ol-substructure-keycloak",
        project_source_path=Path("src/ol_infrastructure/substructure/keycloak"),
        github_issue_repository="org/repo",
        topology="preview-gated",
        auto_deploy_stages=kw.pop("auto_deploy_stages", ["CI"]),
        **kw,
    )


class TestPreviewGatedTopologyIsOptIn:
    """The default topology must be byte-for-byte what it was."""

    def test_default_topology_produces_no_preview_jobs(self):
        fragment = pulumi_jobs_chain(
            _make_pulumi_code(),
            stack_names=["CI", "QA"],
            project_name="ol-application-airbyte",
            project_source_path=Path("src/ol_infrastructure/applications/airbyte"),
            github_issue_repository="org/repo",
        )
        assert all(not str(j.name).startswith("preview-") for j in fragment.jobs)

    def test_auto_deploy_stages_rejected_on_default_topology(self):
        with pytest.raises(ValueError, match="only applies to"):
            pulumi_jobs_chain(
                _make_pulumi_code(),
                stack_names=["CI"],
                project_name="p",
                project_source_path=Path("x"),
                github_issue_repository="org/repo",
                auto_deploy_stages=["CI"],
            )


class TestPreviewGatedTopology:
    """Each stack previews ITSELF and its own preview opens the gate."""

    def test_exempt_stage_keeps_auto_deploy_and_has_no_preview(self):
        fragment = _gated_chain()
        assert _job(fragment, "preview-ol-substructure-keycloak-ci") is None
        deploy_ci = _job(fragment, "deploy-ol-substructure-keycloak-ci")
        code_get = next(
            s for s in deploy_ci.plan if getattr(s, "get", None) == "my-repo"
        )
        assert code_get.trigger is True

    def test_gated_stage_has_preview_and_deploy(self):
        fragment = _gated_chain()
        assert _job(fragment, "preview-ol-substructure-keycloak-qa") is not None
        assert _job(fragment, "deploy-ol-substructure-keycloak-qa") is not None

    def test_deploy_is_passed_constrained_to_its_own_preview(self):
        """The guarantee: nothing reaches an environment unpreviewed."""
        fragment = _gated_chain()
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        code_get = next(s for s in deploy.plan if getattr(s, "get", None) == "my-repo")
        assert code_get.passed == ["preview-ol-substructure-keycloak-qa"]
        assert code_get.trigger is not True, "deploy must wait for the gate, not code"

    def test_deploy_is_triggered_by_the_gate_issue(self):
        fragment = _gated_chain()
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        gate_gets = [
            s
            for s in deploy.plan
            if "gate-trigger" in str(getattr(s, "get", "")) and s.trigger
        ]
        assert len(gate_gets) == 1

    def test_preview_follows_the_previous_stages_deploy(self):
        fragment = _gated_chain()
        preview = _job(fragment, "preview-ol-substructure-keycloak-qa")
        code_get = next(s for s in preview.plan if getattr(s, "get", None) == "my-repo")
        assert code_get.passed == ["deploy-ol-substructure-keycloak-ci"]
        assert code_get.trigger is True

    def test_preview_and_deploy_share_a_serial_group(self):
        """A `pulumi preview` takes the stack lock.

        Splitting one job into two loses the `max_in_flight=1` that kept them
        apart, and lock recovery will not help — it refuses to cancel anything
        under 15 minutes old, so a live preview lock blocks a real deploy.
        """
        fragment = _gated_chain()
        preview = _job(fragment, "preview-ol-substructure-keycloak-qa")
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        assert preview.serial_groups == deploy.serial_groups
        assert preview.serial_groups

    def test_each_stack_has_its_own_serial_group(self):
        """QA's preview must not serialise against Production's deploy."""
        fragment = _gated_chain()
        qa = _job(fragment, "preview-ol-substructure-keycloak-qa").serial_groups
        prod = _job(fragment, "preview-ol-substructure-keycloak-production")
        assert qa != prod.serial_groups

    def test_gate_issue_is_updated_in_place(self):
        """The body must show the diff that will apply, not the first one posted.

        `passed` is set membership, not equality: if a newer commit also passes
        the preview, it is what deploys. Appending a comment would leave the
        stale diff at the top of what a reviewer reads.
        """
        fragment = _gated_chain()
        gate = next(r for r in fragment.resources if "qa-gate-post" in str(r.name))
        assert gate.source["update_in_place"] is True

    def test_gate_issue_auto_closes_for_an_empty_diff(self):
        """An empty preview is nothing to approve -- auto-close the gate for it."""
        fragment = _gated_chain()
        preview = _job(fragment, "preview-ol-substructure-keycloak-qa")
        params = preview.on_success.params or {}
        assert params["close_if_file"] == (
            "pulumi-ol-substructure-keycloak/preview_summary.md.no-changes"
        )

    def test_gate_issue_put_has_no_implicit_get(self):
        """An implicit get would tombstone an auto-closed gate before
        gate-trigger ever polls for it, permanently stalling this stage.
        """
        fragment = _gated_chain()
        preview = _job(fragment, "preview-ol-substructure-keycloak-qa")
        assert preview.on_success.no_get is True

    def test_deploy_still_posts_the_applied_diff_record(self):
        """The gate does not replace the build-158 detector."""
        fragment = _gated_chain()
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        params = deploy.on_success.params or {}
        assert "deployed" in str(deploy.on_success.put)
        assert params["body_files"] == [
            "pulumi-ol-substructure-keycloak/deploy_summary.md"
        ]

    def test_singleton_production_stack_gets_a_gate(self):
        """Today a one-stack chain gets no gate at all — this is the fix."""
        fragment = _gated_chain(stack_names=["Production"])
        assert _job(fragment, "preview-ol-substructure-keycloak-production")
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-production")
        code_get = next(s for s in deploy.plan if getattr(s, "get", None) == "my-repo")
        assert code_get.passed == ["preview-ol-substructure-keycloak-production"]

    def test_exemption_matches_the_trailing_dotted_segment(self):
        """Edxapp stacks are `mitx.CI`, not `CI`."""
        fragment = _gated_chain(stack_names=["mitx.CI"], auto_deploy_stages=["CI"])
        assert _job(fragment, "preview-") is None

    def test_every_job_gets_slack_alerts_when_configured(self):
        fragment = _gated_chain(slack_url_path="slack.url")
        assert all(j.on_failure is not None for j in fragment.jobs)


class TestPreviewGatedRejectsUnsupportedInputs:
    """Silently ignoring a parameter is worse than refusing it."""

    def test_issue_resource_is_required(self):
        with pytest.raises(ValueError, match="enable_github_issue_resource"):
            _gated_chain(enable_github_issue_resource=False)

    def test_every_index_keyed_parameter_is_now_honoured(self):
        """Nothing left to reject — both index-keyed params are supported."""
        fragment = _gated_chain(
            custom_dependencies={1: [GetStep(get=Identifier("an-input"))]},
            additional_post_steps={1: [PutStep(put=Identifier("a-post-step"))]},
        )
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        assert any(
            isinstance(s, PutStep) and str(s.put) == "a-post-step" for s in deploy.plan
        )


class TestRecordDeployments:
    """The "deployed" issue is an audit record, not a gate -- it can be dropped."""

    def test_record_deployments_false_rejected_on_default_topology(self):
        with pytest.raises(ValueError, match="only applies to"):
            pulumi_jobs_chain(
                _make_pulumi_code(),
                stack_names=["CI"],
                project_name="p",
                project_source_path=Path("x"),
                github_issue_repository="org/repo",
                record_deployments=False,
            )

    def test_default_still_posts_the_record_on_both_stage_kinds(self):
        fragment = _gated_chain()
        exempt_deploy = _job(fragment, "deploy-ol-substructure-keycloak-ci")
        gated_deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        assert "deployed" in str(exempt_deploy.on_success.put)
        assert "deployed" in str(gated_deploy.on_success.put)

    def test_record_deployments_false_drops_the_on_success_put(self):
        fragment = _gated_chain(record_deployments=False)
        exempt_deploy = _job(fragment, "deploy-ol-substructure-keycloak-ci")
        gated_deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        assert exempt_deploy.on_success is None
        assert gated_deploy.on_success is None

    def test_record_deployments_false_drops_the_issue_resources(self):
        fragment = _gated_chain(record_deployments=False)
        assert not any("deployed" in str(r.name) for r in fragment.resources)

    def test_record_deployments_false_keeps_the_gate(self):
        """Dropping the record must not touch the actual promotion gate."""
        fragment = _gated_chain(record_deployments=False)
        assert _job(fragment, "preview-ol-substructure-keycloak-qa") is not None
        assert any("gate-post" in str(r.name) for r in fragment.resources)
        assert any("gate-trigger" in str(r.name) for r in fragment.resources)


class TestPreviewGatedStageInputs:
    """Stage inputs are artifacts the Pulumi run consumes, not just triggers.

    k8s_apps hands in a `deployment.json`; kubewatch a `passed`-constrained
    build; simple_pulumi a cross-environment gate issue. Preview and deploy both
    run Pulumi over the same tree, so both need them — but a triggering input on
    the gated deploy would start it without anyone closing the gate.
    """

    @staticmethod
    def _dep(name="some-image", **kw):
        return GetStep(get=Identifier(name), trigger=True, **kw)

    def _gets(self, job, name):
        return [s for s in job.plan if str(getattr(s, "get", "")) == name]

    def test_chain_dependencies_reach_every_job(self):
        fragment = _gated_chain(dependencies=[self._dep()])
        for job_name in (
            "deploy-ol-substructure-keycloak-ci",
            "preview-ol-substructure-keycloak-qa",
            "deploy-ol-substructure-keycloak-qa",
        ):
            job = _job(fragment, job_name)
            assert self._gets(job, "some-image"), f"{job_name} lost the input"

    def test_triggering_input_cannot_start_a_gated_deploy(self):
        """The regression that would silently defeat the entire topology."""
        fragment = _gated_chain(dependencies=[self._dep()])
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        dep = self._gets(deploy, "some-image")[0]
        assert dep.trigger is not True, (
            "a triggering input on a gated deploy bypasses the gate entirely"
        )

    def test_the_same_input_still_triggers_the_preview(self):
        fragment = _gated_chain(dependencies=[self._dep()])
        preview = _job(fragment, "preview-ol-substructure-keycloak-qa")
        assert self._gets(preview, "some-image")[0].trigger is True

    def test_custom_dependencies_land_on_their_own_stage_only(self):
        fragment = _gated_chain(
            custom_dependencies={1: [self._dep("qa-only-artifact")]}
        )
        assert self._gets(
            _job(fragment, "preview-ol-substructure-keycloak-qa"), "qa-only-artifact"
        )
        assert not self._gets(
            _job(fragment, "deploy-ol-substructure-keycloak-ci"), "qa-only-artifact"
        )
        assert not self._gets(
            _job(fragment, "preview-ol-substructure-keycloak-production"),
            "qa-only-artifact",
        )

    def test_custom_dependencies_reach_both_jobs_of_their_stage(self):
        """The deploy runs Pulumi too — it needs the artifact, not just the preview."""
        fragment = _gated_chain(
            custom_dependencies={1: [self._dep("qa-only-artifact")]}
        )
        deploy = _job(fragment, "deploy-ol-substructure-keycloak-qa")
        dep = self._gets(deploy, "qa-only-artifact")[0]
        assert dep.trigger is not True

    def test_custom_dependencies_on_an_exempt_stage_keep_their_trigger(self):
        fragment = _gated_chain(custom_dependencies={0: [self._dep("ci-artifact")]})
        deploy_ci = _job(fragment, "deploy-ol-substructure-keycloak-ci")
        assert self._gets(deploy_ci, "ci-artifact")[0].trigger is True

    def test_passed_constraints_on_inputs_are_preserved(self):
        fragment = _gated_chain(
            custom_dependencies={1: [self._dep("built", passed=["some-upstream-job"])]}
        )
        preview = self._gets(
            _job(fragment, "preview-ol-substructure-keycloak-qa"), "built"
        )[0]
        assert preview.passed == ["some-upstream-job"]

        # The deploy keeps the caller's constraint and adds the preview, so it
        # cannot apply a version the approved diff was not rendered against.
        deploy = self._gets(
            _job(fragment, "deploy-ol-substructure-keycloak-qa"), "built"
        )[0]
        assert deploy.passed == [
            "some-upstream-job",
            "preview-ol-substructure-keycloak-qa",
        ]

    def test_callers_step_objects_are_not_mutated(self):
        """The existing chain mutates shared dependencies in place; do not."""
        dep = self._dep()
        _gated_chain(dependencies=[dep])
        assert dep.trigger is True


class TestPreviewGatedSideEffects:
    """Nothing that writes to the outside world may fire from a preview.

    `k8s_apps` brackets its Pulumi run with a GitHub Deployment — `action:
    start` in custom_dependencies, `action: finish` in additional_post_steps.
    Running `start` from a preview would open a Deployment for a promotion
    nobody has approved, leaving it `pending` until someone closes the gate, or
    forever if they never do.
    """

    @staticmethod
    def _chain():
        return _gated_chain(
            custom_dependencies={
                1: [
                    GetStep(get=Identifier("release-gate"), trigger=True),
                    PutStep(
                        put=Identifier("deployment-rc"), params={"action": "start"}
                    ),
                ]
            },
            additional_post_steps={
                1: [
                    PutStep(put=Identifier("fastly-purge"), no_get=True),
                    PutStep(
                        put=Identifier("deployment-rc"), params={"action": "finish"}
                    ),
                ]
            },
        )

    @staticmethod
    def _puts(job):
        return [str(s.put) for s in job.plan if isinstance(s, PutStep)]

    def test_preview_runs_no_side_effects(self):
        preview = _job(self._chain(), "preview-ol-substructure-keycloak-qa")
        assert self._puts(preview) == ["pulumi-ol-substructure-keycloak"]

    def test_preview_still_gets_read_only_inputs(self):
        preview = _job(self._chain(), "preview-ol-substructure-keycloak-qa")
        gets = [str(s.get) for s in preview.plan if isinstance(s, GetStep)]
        assert "release-gate" in gets

    def test_deploy_brackets_the_apply_with_its_side_effects(self):
        """Start → up → purge → finish, in that order."""
        deploy = _job(self._chain(), "deploy-ol-substructure-keycloak-qa")
        puts = self._puts(deploy)
        assert puts == [
            "deployment-rc",
            "pulumi-ol-substructure-keycloak",
            "fastly-purge",
            "deployment-rc",
        ]

    def test_start_precedes_the_apply_and_finish_follows_it(self):
        deploy = _job(self._chain(), "deploy-ol-substructure-keycloak-qa")
        actions = [
            (str(s.put), (s.params or {}).get("action"))
            for s in deploy.plan
            if isinstance(s, PutStep)
        ]
        apply_at = next(
            i for i, (name, _) in enumerate(actions) if name.startswith("pulumi-")
        )
        start_at = next(i for i, (_, a) in enumerate(actions) if a == "start")
        finish_at = next(i for i, (_, a) in enumerate(actions) if a == "finish")
        assert start_at < apply_at < finish_at

    def test_post_steps_never_reach_the_preview(self):
        preview = _job(self._chain(), "preview-ol-substructure-keycloak-qa")
        assert "fastly-purge" not in self._puts(preview)

    def test_post_steps_are_scoped_to_their_own_stage(self):
        fragment = self._chain()
        prod = _job(fragment, "deploy-ol-substructure-keycloak-production")
        assert "fastly-purge" not in self._puts(prod)

    def test_exempt_stage_gets_its_side_effects_and_post_steps(self):
        fragment = _gated_chain(
            custom_dependencies={0: [PutStep(put=Identifier("ci-effect"))]},
            additional_post_steps={0: [PutStep(put=Identifier("ci-post"))]},
        )
        puts = self._puts(_job(fragment, "deploy-ol-substructure-keycloak-ci"))
        assert puts == [
            "ci-effect",
            "pulumi-ol-substructure-keycloak",
            "ci-post",
        ]


class TestGateAndRecordLabels:
    """A gate describes what closing it will DO; a record describes what happened.

    The deploy-chained labels describe the NEXT stage, because there the issue is
    posted after a stage deploys. Reusing them here labelled the QA gate
    `promotion-to-production` and the Production gate `finalized-deployment`
    before anything had deployed at all.
    """

    @staticmethod
    def _labels(fragment, job_name):
        return ((_job(fragment, job_name).on_success.params) or {}).get("labels")

    def test_gate_names_the_stage_it_authorises(self):
        fragment = _gated_chain()
        assert "promotion-to-qa" in self._labels(
            fragment, "preview-ol-substructure-keycloak-qa"
        )
        assert "promotion-to-production" in self._labels(
            fragment, "preview-ol-substructure-keycloak-production"
        )

    def test_gate_is_not_labelled_finalized_before_deploying(self):
        fragment = _gated_chain()
        gate = self._labels(fragment, "preview-ol-substructure-keycloak-production")
        assert "finalized-deployment" not in gate

    def test_record_carries_completion_labels(self):
        fragment = _gated_chain()
        qa = self._labels(fragment, "deploy-ol-substructure-keycloak-qa")
        assert qa is not None, "the gated deploy record had no labels at all"
        assert "deployed" in qa

    def test_production_record_is_the_finalized_one(self):
        fragment = _gated_chain()
        assert "finalized-deployment" in self._labels(
            fragment, "deploy-ol-substructure-keycloak-production"
        )

    def test_exempt_stage_record_is_labelled_too(self):
        fragment = _gated_chain()
        assert "deployed" in self._labels(
            fragment, "deploy-ol-substructure-keycloak-ci"
        )

    def test_explicit_labels_still_override_everything(self):
        fragment = _gated_chain(github_issue_labels=["mine"])
        assert self._labels(fragment, "preview-ol-substructure-keycloak-qa") == ["mine"]
        assert self._labels(fragment, "deploy-ol-substructure-keycloak-qa") == ["mine"]


class TestStageInputsAreCorrelatedWithThePreview:
    """The deploy must not apply a version the approved diff was not rendered from.

    Only the Pulumi code get was tied to the preview. A newer image landing while
    the gate was open would be picked up by the deploy — and edxapp feeds that
    digest straight into Pulumi, so it would be a different deploy than the one
    on the issue.
    """

    @staticmethod
    def _chain():
        return _gated_chain(
            dependencies=[
                GetStep(
                    get=Identifier("app-image"),
                    trigger=True,
                    passed=["build-image"],
                )
            ]
        )

    def test_deploy_input_requires_the_preview(self):
        deploy = _job(self._chain(), "deploy-ol-substructure-keycloak-qa")
        dep = next(s for s in deploy.plan if str(getattr(s, "get", "")) == "app-image")
        assert "preview-ol-substructure-keycloak-qa" in (dep.passed or [])

    def test_existing_passed_constraints_are_kept(self):
        deploy = _job(self._chain(), "deploy-ol-substructure-keycloak-qa")
        dep = next(s for s in deploy.plan if str(getattr(s, "get", "")) == "app-image")
        assert "build-image" in (dep.passed or [])

    def test_preview_input_is_not_self_correlated(self):
        """The preview cannot require having passed itself."""
        preview = _job(self._chain(), "preview-ol-substructure-keycloak-qa")
        dep = next(s for s in preview.plan if str(getattr(s, "get", "")) == "app-image")
        assert dep.passed == ["build-image"]

    def test_exempt_stage_input_is_untouched(self):
        deploy = _job(self._chain(), "deploy-ol-substructure-keycloak-ci")
        dep = next(s for s in deploy.plan if str(getattr(s, "get", "")) == "app-image")
        assert dep.passed == ["build-image"]
        assert dep.trigger is True
