"""Tests for ol_concourse.lib.tasks."""

import subprocess
import sys
import tomllib

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

    def test_shell_script_contains_bump_my_version_invocation(self):
        step = bump_version_task(version_file="release/version", repository="src")
        script = step.config.run.args[1]
        assert 'bump-my-version bump --new-version "$VERSION"' in script
        assert "--no-commit" in script
        assert "--allow-dirty" in script

    def test_shell_script_checks_since_file_for_semver(self):
        step = bump_version_task(version_file="release/version", repository="src")
        script = step.config.run.args[1]
        assert "release/since" in script
        assert "SINCE_SEMVER" in script

    def test_shell_script_uses_python_for_semver_transition(self):
        step = bump_version_task(version_file="release/version", repository="src")
        script = step.config.run.args[1]
        assert "python3 -c" in script
        assert "tomllib" in script
        assert '"$SINCE_SEMVER"' in script

    def test_shell_script_strips_v_prefix_from_since(self):
        step = bump_version_task(version_file="release/version", repository="src")
        script = step.config.run.args[1]
        assert "${SINCE#v}" in script

    def test_shell_script_semver_regex_excludes_calver_years(self):
        step = bump_version_task(version_file="release/version", repository="src")
        script = step.config.run.args[1]
        assert "[0-9]{1,3}" in script

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

    def test_shell_script_falls_back_to_transition_without_current_version(self):
        """A repo with no [tool.bumpversion].current_version must not reach
        bump-my-version: it cannot run without that key, even with
        --new-version.
        """
        step = bump_version_task(version_file="release/version", repository="src")
        script = step.config.run.args[1]
        assert '[ -f pyproject.toml ] && [ -z "$PYPROJECT_VER" ]' in script


def _transition_script() -> str:
    """Extract the Python transition script embedded in the generated bash."""
    args = bump_version_task().config.run.args[1]
    opener = "python3 -c '"
    start = args.index(opener) + len(opener)
    end = args.index('\' "$SINCE_SEMVER" "$VERSION"')
    # Undo shlex.quote's single-quote escaping.
    return args[start:end].replace("'\"'\"'", "'")


BUMPVERSION_CONFIG = """\
[project]
name = "demo"
version = "{project_version}"

[tool.bumpversion]
{current_version_line}\
commit = false
tag = false

[tool.bumpversion.parts.build]
first_value = "1"

[[tool.bumpversion.files]]
filename = "demo/settings.py"
search = 'VERSION = "{{current_version}}"'
replace = 'VERSION = "{{new_version}}"'

[[tool.bumpversion.files]]
filename = "pyproject.toml"
search = 'version = "{{current_version}}"'
replace = 'version = "{{new_version}}"'
"""


class TestTransitionScript:
    """Run the embedded transition script against real pyproject.toml fixtures.

    These assert behavior rather than the presence of substrings, because the
    bug this guards against (a silent no-op that still printed success) is
    invisible to a string check.
    """

    @staticmethod
    def _write_repo(tmp_path, *, current_version, settings_version="0.94.0"):
        current_version_line = (
            f'current_version = "{current_version}"\n' if current_version else ""
        )
        (tmp_path / "pyproject.toml").write_text(
            BUMPVERSION_CONFIG.format(
                project_version=settings_version,
                current_version_line=current_version_line,
            )
        )
        (tmp_path / "demo").mkdir()
        (tmp_path / "demo" / "settings.py").write_text(
            f'VERSION = "{settings_version}"\n'
        )

    @staticmethod
    def _run(tmp_path, since, new_version):
        script = tmp_path / "_transition.py"
        script.write_text(_transition_script())
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(script), since, new_version],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        script.unlink()
        return result

    @staticmethod
    def _tracked_version(tmp_path):
        with (tmp_path / "pyproject.toml").open("rb") as config_file:
            config = tomllib.load(config_file)
        return config["tool"]["bumpversion"].get("current_version")

    def test_inserts_current_version_when_absent(self, tmp_path):
        self._write_repo(tmp_path, current_version=None)
        self._run(tmp_path, "0.94.0", "2026.9.3.1")
        assert self._tracked_version(tmp_path) == "2026.9.3.1"

    def test_rewrites_existing_current_version(self, tmp_path):
        self._write_repo(tmp_path, current_version="0.94.0")
        self._run(tmp_path, "0.94.0", "2026.9.3.1")
        assert self._tracked_version(tmp_path) == "2026.9.3.1"

    def test_no_semver_baseline_still_bumps_and_seeds(self, tmp_path):
        """Once an app has a calver tag the `since` file is calver, so the
        script gets an empty baseline and has to find the version itself.
        """
        self._write_repo(tmp_path, current_version=None)
        self._run(tmp_path, "", "2026.9.3.1")
        assert self._tracked_version(tmp_path) == "2026.9.3.1"
        assert '"2026.9.3.1"' in (tmp_path / "demo" / "settings.py").read_text()

    def test_tracking_field_written_once(self, tmp_path):
        """The [[files]] search templates also contain the literal
        `current_version`; only the table's own key may be rewritten.
        """
        self._write_repo(tmp_path, current_version=None)
        self._run(tmp_path, "0.94.0", "2026.9.3.1")
        content = (tmp_path / "pyproject.toml").read_text()
        assert content.count('current_version = "2026.9.3.1"') == 1
        assert content.count("{current_version}") == 2

    def test_missing_bumpversion_table_warns_without_failing(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        result = self._run(tmp_path, "0.94.0", "2026.9.3.1")
        assert "no [tool.bumpversion] table" in result.stderr
