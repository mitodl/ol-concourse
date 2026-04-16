"""Tests for ol_concourse.lib models and builders."""

from ol_concourse.lib.constants import GH_ISSUES_DEFAULT_REPOSITORY, REGISTRY_IMAGE
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    Identifier,
    Job,
    Pipeline,
    Resource,
    ResourceType,
)
from ol_concourse.lib.models.resource import Git
from ol_concourse.lib.resource_types import (
    github_deployments_resource,
    github_issues_resource,
    release_resource_type,
)


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


class TestPipelineFragment:
    def test_empty_fragment(self):
        fragment = PipelineFragment()
        assert fragment.resources == []
        assert fragment.resource_types == []
        assert fragment.jobs == []

    def test_to_pipeline(self):
        fragment = PipelineFragment()
        pipeline = fragment.to_pipeline()
        assert isinstance(pipeline, Pipeline)

    def test_combine_fragments(self):
        f1 = PipelineFragment()
        f2 = PipelineFragment()
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
        pipeline = Pipeline()
        json_output = pipeline.model_dump_json()
        assert isinstance(json_output, str)

    def test_pipeline_excludes_none_values(self):
        pipeline = Pipeline()
        json_output = pipeline.model_dump_json()
        assert "null" not in json_output

    def test_job_serialization(self):
        job = Job(name=Identifier("my-job"), plan=[])
        pipeline = Pipeline(jobs=[job])
        json_output = pipeline.model_dump_json()
        assert "my-job" in json_output


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
