"""Tests for ol_concourse.lib.tasks."""

import pytest

from ol_concourse.lib.models.pipeline import AnonymousResource
from ol_concourse.lib.tasks import TASK_IMAGE, bump_version_task


class TestBumpVersionTask:
    def test_default_parameters(self):
        step = bump_version_task()
        assert step.task == "bump-version"
        assert step.privileged is False
        assert step.config is not None
        assert step.config.platform == "linux"

    def test_default_image_is_task_image(self):
        step = bump_version_task()
        assert step.config.image_resource == TASK_IMAGE

    def test_custom_image_overrides_default(self):
        custom_image = AnonymousResource(
            type="registry-image",
            source={"repository": "custom/image", "tag": "1.2.3"},
        )
        step = bump_version_task(image=custom_image)
        assert step.config.image_resource == custom_image

    def test_version_input_derived_from_version_file(self):
        step = bump_version_task(version_file="release/version")
        input_names = [str(inp.name) for inp in step.config.inputs]
        assert "release" in input_names

    def test_custom_version_file_sets_correct_input(self):
        step = bump_version_task(version_file="my-resource/path/to/version.txt")
        input_names = [str(inp.name) for inp in step.config.inputs]
        assert "my-resource" in input_names

    def test_repository_is_both_input_and_output(self):
        step = bump_version_task(repository="app-source")
        input_names = [str(inp.name) for inp in step.config.inputs]
        output_names = [str(out.name) for out in step.config.outputs]
        assert "app-source" in input_names
        assert "app-source" in output_names

    def test_custom_repository_name(self):
        step = bump_version_task(repository="my-app")
        input_names = [str(inp.name) for inp in step.config.inputs]
        output_names = [str(out.name) for out in step.config.outputs]
        assert "my-app" in input_names
        assert "my-app" in output_names

    def test_shell_script_contains_bumpver_invocation(self):
        step = bump_version_task(version_file="release/version", repository="src")
        script = step.config.run.args[1]
        assert "bumpver update --set-version" in script
        assert "--no-commit" in script
        assert "--no-fetch" in script

    def test_shell_script_reads_version_from_file(self):
        step = bump_version_task(version_file="release/version")
        script = step.config.run.args[1]
        assert "cat release/version" in script

    def test_shell_script_configures_git_identity(self):
        step = bump_version_task(git_user="Bot", git_email="bot@example.com")
        script = step.config.run.args[1]
        # shlex.quote leaves safe characters unquoted; verify the values appear
        assert "user.email bot@example.com" in script
        assert "user.name Bot" in script

    def test_shell_script_quotes_special_chars(self):
        """Values with spaces/metacharacters are safely quoted by shlex.quote."""
        step = bump_version_task(git_user="CI Bot", git_email="ci@example.com")
        script = step.config.run.args[1]
        assert "user.name 'CI Bot'" in script

    def test_shell_script_runs_in_repository_dir(self):
        step = bump_version_task(repository="app-src")
        script = step.config.run.args[1]
        assert "cd app-src" in script

    def test_invalid_version_file_no_slash(self):
        with pytest.raises(ValueError, match="input-name/path"):
            bump_version_task(version_file="versionfile")

    def test_invalid_version_file_absolute(self):
        with pytest.raises(ValueError, match="input-name/path"):
            bump_version_task(version_file="/release/version")

    def test_invalid_version_file_dot_relative(self):
        with pytest.raises(ValueError, match="input-name/path"):
            bump_version_task(version_file="./release/version")

    def test_invalid_version_file_parent_relative(self):
        with pytest.raises(ValueError, match="input-name/path"):
            bump_version_task(version_file="../release/version")

    def test_no_duplicate_inputs_when_version_file_in_repo_dir(self):
        """When version_file lives inside the repo input, emit only one input."""
        step = bump_version_task(
            version_file="app-source/version", repository="app-source"
        )
        input_names = [str(inp.name) for inp in step.config.inputs]
        assert input_names.count("app-source") == 1

    def test_two_inputs_when_version_file_in_separate_dir(self):
        """When version_file is in a different input, both inputs are emitted."""
        step = bump_version_task(
            version_file="release/version", repository="app-source"
        )
        input_names = [str(inp.name) for inp in step.config.inputs]
        assert "release" in input_names
        assert "app-source" in input_names
        assert len(input_names) == 2
