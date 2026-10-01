"""Tests for Packer command argument logging."""

from unittest.mock import patch

import pytest

import packer_lib


@pytest.mark.parametrize("command", ["validate", "build"])
@pytest.mark.parametrize("selection", ["only", "excepts"])
def test_debug_redacts_variable_values(command, selection, tmp_path, capsys):
    sentinel = "REVIEW_SENTINEL=private-value"
    value_file = tmp_path / "secret.txt"
    value_file.write_text(f"{sentinel}\n")
    command_options = (
        {"syntax_only": True} if command == "validate" else {"force": True}
    )
    selection_arg = "-only=source" if selection == "only" else "-except=source"
    mode_arg = "-syntax-only" if command == "validate" else "-force"
    with patch("packer_lib._packer", return_value=[]) as mock_packer:
        getattr(packer_lib, command)(
            str(tmp_path),
            "template.pkr.hcl",
            var_file_paths=["vars.pkrvars.hcl"],
            template_vars={"ssh_password": sentinel, "empty": ""},
            vars_from_files={"file_secret": value_file.name},
            debug=True,
            **{selection: ["source"]},
            **command_options,
        )

    stderr = capsys.readouterr().err
    assert sentinel not in stderr
    assert "private-value" not in stderr
    for arg in (
        "-var=ssh_password=***",
        "-var=empty=***",
        "-var=file_secret=***",
        "-var-file=vars.pkrvars.hcl",
        selection_arg,
        mode_arg,
    ):
        assert arg in stderr
    mock_packer.assert_called_once_with(
        command,
        "-var-file=vars.pkrvars.hcl",
        f"-var=ssh_password={sentinel}",
        "-var=empty=",
        f"-var=file_secret={sentinel}",
        selection_arg,
        mode_arg,
        "template.pkr.hcl",
        working_dir=str(tmp_path),
    )
