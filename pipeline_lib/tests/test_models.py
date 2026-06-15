"""Tests for ol_concourse.lib models and builders."""

import pytest
from pydantic import ValidationError

from ol_concourse.lib.constants import GH_ISSUES_DEFAULT_REPOSITORY, REGISTRY_IMAGE
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AcrossVar,
    AnonymousResource,
    BuildLogRetentionPolicy,
    Command,
    DisplayConfig,
    Duration,
    GetStep,
    GroupConfig,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Number,
    Output,
    Pipeline,
    Platform,
    PutStep,
    Resource,
    ResourceType,
    SetPipelineStep,
    TaskConfig,
    TaskStep,
    Value,
)
from ol_concourse.lib.models.resource import Git
from ol_concourse.lib.resource_types import (
    github_deployments_resource,
    github_issues_resource,
    release_resource_type,
)


def _minimal_job(name: str = "test-job") -> Job:
    return Job(name=Identifier(name), plan=[])


def _minimal_pipeline(**kwargs) -> Pipeline:
    kwargs.setdefault("jobs", [_minimal_job()])
    return Pipeline(**kwargs)


def _minimal_task_config(**kwargs) -> TaskConfig:
    kwargs.setdefault("platform", Platform.linux)
    kwargs.setdefault(
        "image_resource",
        AnonymousResource(type="registry-image", source={"repository": "busybox"}),
    )
    kwargs.setdefault("run", Command(path="echo"))
    return TaskConfig(**kwargs)


class TestConstants:
    def test_registry_image_constant(self):
        assert REGISTRY_IMAGE == "registry-image"

    def test_gh_issues_default_repository(self):
        assert "/" in GH_ISSUES_DEFAULT_REPOSITORY


class TestIdentifier:
    def test_valid_identifier(self):
        ident = Identifier("my-resource")
        assert str(ident.root) == "my-resource"

    def test_identifier_with_numbers(self):
        ident = Identifier("resource-123")
        assert str(ident.root) == "resource-123"


class TestDuration:
    @pytest.mark.parametrize(
        "value",
        ["0", "1s", "30m", "1h", "1h30m", "1h30m5s", "300ms", "1.5h", ".5s"],
    )
    def test_valid_duration(self, value: str):
        d = Duration(value)
        assert d.root == value

    @pytest.mark.parametrize(
        "value",
        ["30 minutes", "1 hour", "2d", "never", "abc", ""],
    )
    def test_invalid_duration(self, value: str):
        with pytest.raises(ValidationError):
            Duration(value)


class TestDisplayConfig:
    def test_valid_http_url(self):
        dc = DisplayConfig(background_image="http://example.com/img.png")
        assert dc.background_image == "http://example.com/img.png"

    def test_valid_https_url(self):
        dc = DisplayConfig(background_image="https://example.com/img.png")
        assert dc.background_image == "https://example.com/img.png"

    def test_valid_relative_url(self):
        dc = DisplayConfig(background_image="/images/bg.png")
        assert dc.background_image == "/images/bg.png"

    def test_none_background_image(self):
        dc = DisplayConfig()
        assert dc.background_image is None

    def test_invalid_scheme_rejected(self):
        with pytest.raises(ValidationError, match="scheme"):
            DisplayConfig(background_image="ftp://example.com/img.png")


class TestTaskConfig:
    def test_valid_task_config(self):
        cfg = _minimal_task_config()
        assert cfg.platform == Platform.linux

    def test_missing_platform_raises(self):
        with pytest.raises(ValidationError, match="platform"):
            TaskConfig(run=Command(path="echo"))

    def test_input_without_name_raises(self):
        with pytest.raises(ValidationError, match="missing a name"):
            _minimal_task_config(inputs=[Input()])

    def test_output_without_name_raises(self):
        with pytest.raises(ValidationError, match="missing a name"):
            _minimal_task_config(outputs=[Output()])

    def test_named_inputs_and_outputs_valid(self):
        cfg = _minimal_task_config(
            inputs=[Input(name=Identifier("src"))],
            outputs=[Output(name=Identifier("out"))],
        )
        assert len(cfg.inputs) == 1
        assert len(cfg.outputs) == 1


class TestTaskStep:
    def test_config_only_valid(self):
        step = TaskStep(task=Identifier("my-task"), config=_minimal_task_config())
        assert step.config is not None

    def test_file_only_valid(self):
        step = TaskStep(task=Identifier("my-task"), file="ci/tasks/foo.yml")
        assert step.file == "ci/tasks/foo.yml"

    def test_neither_config_nor_file_raises(self):
        with pytest.raises(ValidationError, match=r"config.*file|file.*config"):
            TaskStep(task=Identifier("my-task"))

    def test_both_config_and_file_raises(self):
        with pytest.raises(ValidationError, match="both"):
            TaskStep(
                task=Identifier("my-task"),
                config=_minimal_task_config(),
                file="ci/tasks/foo.yml",
            )


class TestSetPipelineStep:
    def test_valid_with_file(self):
        step = SetPipelineStep(
            set_pipeline=Identifier("my-pipeline"),
            file="ci/pipeline.yml",
        )
        assert step.file == "ci/pipeline.yml"

    def test_missing_file_raises(self):
        with pytest.raises(ValidationError, match="file"):
            SetPipelineStep(set_pipeline=Identifier("my-pipeline"))


class TestLoadVarStep:
    def test_valid_with_file(self):
        step = LoadVarStep(
            load_var=Identifier("my-var"),
            file="ci/vars.yml",
        )
        assert step.file == "ci/vars.yml"

    def test_missing_file_raises(self):
        with pytest.raises(ValidationError, match="file"):
            LoadVarStep(load_var=Identifier("my-var"))


class TestAcrossVar:
    def test_valid_max_in_flight_number(self):
        av = AcrossVar(
            var=Identifier("x"), values=[Value("a")], max_in_flight=Number(2)
        )
        assert av.max_in_flight.root == 2

    def test_valid_max_in_flight_all(self):
        av = AcrossVar(var=Identifier("x"), values=[Value("a")], max_in_flight="all")
        assert av.max_in_flight == "all"

    def test_zero_max_in_flight_raises(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            AcrossVar(var=Identifier("x"), values=[Value("a")], max_in_flight=Number(0))

    def test_negative_max_in_flight_raises(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            AcrossVar(
                var=Identifier("x"), values=[Value("a")], max_in_flight=Number(-1)
            )


class TestJobValidation:
    def test_valid_job(self):
        job = _minimal_job()
        assert str(job.name) == "test-job"

    def test_build_log_retention_and_retain_raises(self):
        with pytest.raises(ValidationError, match="both"):
            Job(
                name=Identifier("j"),
                plan=[],
                build_log_retention=BuildLogRetentionPolicy(builds=Number(10)),
                build_logs_to_retain=Number(5),
            )

    def test_negative_builds_raises(self):
        with pytest.raises(ValidationError, match="negative"):
            Job(
                name=Identifier("j"),
                plan=[],
                build_log_retention=BuildLogRetentionPolicy(builds=Number(-1)),
            )

    def test_negative_days_raises(self):
        with pytest.raises(ValidationError, match="negative"):
            Job(
                name=Identifier("j"),
                plan=[],
                build_log_retention=BuildLogRetentionPolicy(days=Number(-1)),
            )

    def test_negative_min_succeeded_raises(self):
        with pytest.raises(ValidationError, match="negative"):
            Job(
                name=Identifier("j"),
                plan=[],
                build_log_retention=BuildLogRetentionPolicy(
                    minimum_succeeded_builds=Number(-1)
                ),
            )

    def test_min_succeeded_exceeds_builds_raises(self):
        with pytest.raises(ValidationError, match="exceed"):
            Job(
                name=Identifier("j"),
                plan=[],
                build_log_retention=BuildLogRetentionPolicy(
                    builds=Number(3), minimum_succeeded_builds=Number(5)
                ),
            )

    def test_valid_build_log_retention(self):
        job = Job(
            name=Identifier("j"),
            plan=[],
            build_log_retention=BuildLogRetentionPolicy(
                builds=Number(10), days=Number(2), minimum_succeeded_builds=Number(1)
            ),
        )
        assert job.build_log_retention.builds.root == 10


class TestPipelineFragment:
    def test_empty_fragment(self):
        fragment = PipelineFragment()
        assert fragment.resources == []
        assert fragment.resource_types == []
        assert fragment.jobs == []

    def test_to_pipeline(self):
        fragment = PipelineFragment(jobs=[_minimal_job()])
        pipeline = fragment.to_pipeline()
        assert isinstance(pipeline, Pipeline)

    def test_combine_fragments(self):
        f1 = PipelineFragment(jobs=[_minimal_job("job-1")])
        f2 = PipelineFragment(jobs=[_minimal_job("job-2")])
        combined = PipelineFragment.combine_fragments(f1, f2)
        assert isinstance(combined, PipelineFragment)

    def test_deduplication_of_resources(self):
        resource = Resource(
            name=Identifier("my-repo"),
            type="git",
            source={"uri": "https://github.com/org/repo"},
        )
        fragment = PipelineFragment(resources=[resource, resource])
        assert len(fragment.resources) == 1

    def test_deduplication_of_resource_types(self):
        rt = ResourceType(
            name=Identifier("custom-type"),
            type="registry-image",
            source={"repository": "myorg/myimage"},
        )
        fragment = PipelineFragment(resource_types=[rt, rt])
        assert len(fragment.resource_types) == 1


class TestGitModel:
    def test_git_defaults(self):
        git = Git(uri="https://github.com/org/repo")
        assert git.branch == "main"
        assert git.paths is None
        assert git.private_key is None

    def test_git_with_all_fields(self):
        git = Git(
            uri="https://github.com/org/repo",
            branch="develop",
            paths=["src/"],
        )
        assert git.branch == "develop"


class TestPipelineSerialization:
    def test_pipeline_serializes_to_json(self):
        pipeline = _minimal_pipeline()
        json_output = pipeline.model_dump_json()
        assert isinstance(json_output, str)

    def test_pipeline_excludes_none_values(self):
        pipeline = _minimal_pipeline()
        json_output = pipeline.model_dump_json()
        assert "null" not in json_output

    def test_job_serialization(self):
        job = Job(name=Identifier("my-job"), plan=[])
        pipeline = Pipeline(jobs=[job])
        json_output = pipeline.model_dump_json()
        assert "my-job" in json_output


class TestPipelineValidation:
    def test_empty_jobs_raises(self):
        with pytest.raises(ValidationError, match="at least one job"):
            Pipeline(jobs=[])

    def test_none_jobs_raises(self):
        with pytest.raises(ValidationError, match="at least one job"):
            Pipeline()

    def test_duplicate_job_names_raises(self):
        with pytest.raises(ValidationError, match="more than once"):
            Pipeline(jobs=[_minimal_job("dupe"), _minimal_job("dupe")])

    def test_duplicate_resource_names_raises(self):
        resource = Resource(name=Identifier("my-repo"), type="git")
        with pytest.raises(ValidationError, match="more than once"):
            _minimal_pipeline(resources=[resource, resource])

    def test_duplicate_resource_type_names_raises(self):
        rt = ResourceType(name=Identifier("custom"), type="registry-image")
        with pytest.raises(ValidationError, match="more than once"):
            _minimal_pipeline(resource_types=[rt, rt])

    def test_group_with_nonexistent_job_raises(self):
        with pytest.raises(ValidationError, match="no jobs match"):
            _minimal_pipeline(
                groups=[GroupConfig(name=Identifier("g"), jobs=["nonexistent-job"])]
            )

    def test_ungrouped_job_raises_when_groups_defined(self):
        with pytest.raises(ValidationError, match="belongs to no group"):
            Pipeline(
                jobs=[_minimal_job("grouped-job"), _minimal_job("orphan-job")],
                groups=[GroupConfig(name=Identifier("g"), jobs=["grouped-job"])],
            )

    def test_duplicate_group_names_raises(self):
        with pytest.raises(ValidationError, match="more than once"):
            Pipeline(
                jobs=[_minimal_job("my-job")],
                groups=[
                    GroupConfig(name=Identifier("g"), jobs=["my-job"]),
                    GroupConfig(name=Identifier("g"), jobs=["my-job"]),
                ],
            )

    def test_valid_pipeline_with_groups(self):
        pipeline = Pipeline(
            jobs=[_minimal_job("job-a"), _minimal_job("job-b")],
            groups=[
                GroupConfig(name=Identifier("group-1"), jobs=["job-a"]),
                GroupConfig(name=Identifier("group-2"), jobs=["job-b"]),
            ],
        )
        assert len(pipeline.jobs) == 2

    def test_valid_pipeline_with_glob_group(self):
        pipeline = Pipeline(
            jobs=[_minimal_job("test-unit"), _minimal_job("test-integration")],
            groups=[GroupConfig(name=Identifier("tests"), jobs=["test-*"])],
        )
        assert len(pipeline.jobs) == 2


class TestStepModifierAcross:
    def test_across_none_is_valid(self):
        step = GetStep(get="repo")
        assert step.across is None

    def test_across_with_var_is_valid(self):
        step = GetStep(
            get="repo",
            across=[AcrossVar(var=Identifier("env"), values=[Value("prod")])],
        )
        assert len(step.across) == 1

    def test_across_empty_list_raises(self):
        with pytest.raises(ValidationError, match="at least one var"):
            GetStep(get="repo", across=[])

    def test_across_empty_list_on_put_raises(self):
        with pytest.raises(ValidationError, match="at least one var"):
            PutStep(put="repo", across=[])


class TestPipelineResourceValidation:
    def test_unused_resource_raises(self):
        resource = Resource(name=Identifier("repo"), type="git")
        with pytest.raises(ValidationError, match="is not used"):
            Pipeline(jobs=[_minimal_job()], resources=[resource])

    def test_used_resource_via_get_valid(self):
        pipeline = Pipeline(
            jobs=[Job(name=Identifier("j"), plan=[GetStep(get="repo")])],
            resources=[Resource(name=Identifier("repo"), type="git")],
        )
        assert len(pipeline.resources) == 1

    def test_used_resource_via_put_valid(self):
        pipeline = Pipeline(
            jobs=[Job(name=Identifier("j"), plan=[PutStep(put="repo")])],
            resources=[Resource(name=Identifier("repo"), type="git")],
        )
        assert len(pipeline.resources) == 1

    def test_resource_aliased_via_get_resource_field(self):
        pipeline = Pipeline(
            jobs=[
                Job(
                    name=Identifier("j"),
                    plan=[GetStep(get="code", resource="repo")],
                )
            ],
            resources=[Resource(name=Identifier("repo"), type="git")],
        )
        assert len(pipeline.resources) == 1

    def test_resource_aliased_via_put_resource_field(self):
        pipeline = Pipeline(
            jobs=[
                Job(
                    name=Identifier("j"),
                    plan=[PutStep(put="artifact", resource="repo")],
                )
            ],
            resources=[Resource(name=Identifier("repo"), type="git")],
        )
        assert len(pipeline.resources) == 1

    def test_unknown_resource_in_get_raises(self):
        with pytest.raises(ValidationError, match="unknown resource"):
            Pipeline(
                jobs=[Job(name=Identifier("j"), plan=[GetStep(get="typo")])],
                resources=[Resource(name=Identifier("repo"), type="git")],
            )

    def test_unknown_resource_in_put_raises(self):
        with pytest.raises(ValidationError, match="unknown resource"):
            Pipeline(
                jobs=[Job(name=Identifier("j"), plan=[PutStep(put="typo")])],
                resources=[Resource(name=Identifier("repo"), type="git")],
            )

    def test_resource_used_in_job_hook_valid(self):
        pipeline = Pipeline(
            jobs=[
                Job(
                    name=Identifier("j"),
                    plan=[TaskStep(task=Identifier("t"), file="ci/task.yml")],
                    on_failure=PutStep(put="notify"),
                )
            ],
            resources=[Resource(name=Identifier("notify"), type="slack-notification")],
        )
        assert len(pipeline.resources) == 1

    def test_passed_job_not_exist_raises(self):
        with pytest.raises(ValidationError, match="no matching job"):
            Pipeline(
                jobs=[
                    Job(
                        name=Identifier("consumer"),
                        plan=[GetStep(get="repo", passed=["nonexistent"])],
                    )
                ],
                resources=[Resource(name=Identifier("repo"), type="git")],
            )

    def test_passed_job_exists_valid(self):
        pipeline = Pipeline(
            jobs=[
                Job(name=Identifier("producer"), plan=[PutStep(put="repo")]),
                Job(
                    name=Identifier("consumer"),
                    plan=[GetStep(get="repo", passed=["producer"])],
                ),
            ],
            resources=[Resource(name=Identifier("repo"), type="git")],
        )
        assert len(pipeline.jobs) == 2

    def test_passed_job_glob_valid(self):
        pipeline = Pipeline(
            jobs=[
                Job(name=Identifier("build-unit"), plan=[PutStep(put="repo")]),
                Job(
                    name=Identifier("deploy"),
                    plan=[GetStep(get="repo", passed=["build-*"])],
                ),
            ],
            resources=[Resource(name=Identifier("repo"), type="git")],
        )
        assert len(pipeline.jobs) == 2

    def test_no_resources_and_no_get_put_valid(self):
        pipeline = Pipeline(
            jobs=[
                Job(
                    name=Identifier("j"),
                    plan=[TaskStep(task=Identifier("t"), file="ci/task.yml")],
                )
            ],
        )
        assert pipeline.resources is None


class TestResourceTypes:
    def test_github_issues_resource_type_name(self):
        rt = github_issues_resource()
        assert str(rt.name) == "github-issues"

    def test_github_issues_resource_type_is_registry_image(self):
        rt = github_issues_resource()
        assert rt.type == REGISTRY_IMAGE

    def test_github_deployments_resource_type_name(self):
        rt = github_deployments_resource()
        assert str(rt.name) == "github-deployments"

    def test_github_deployments_resource_type_repository(self):
        rt = github_deployments_resource()
        assert rt.source.repository == "mitodl/concourse-github-deployments-resource"

    def test_release_resource_type_name(self):
        rt = release_resource_type()
        assert str(rt.name) == "release"

    def test_release_resource_type_is_registry_image(self):
        rt = release_resource_type()
        assert rt.type == REGISTRY_IMAGE

    def test_release_resource_type_repository(self):
        rt = release_resource_type()
        assert rt.source.repository == "mitodl/concourse-release-resource"

    def test_release_resource_type_returns_new_instance_each_call(self):
        rt1 = release_resource_type()
        rt2 = release_resource_type()
        assert rt1 is not rt2
        assert rt1 == rt2
