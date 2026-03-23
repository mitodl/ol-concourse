"""File I/O helpers for the Concourse Pulumi resource."""

from __future__ import annotations

import os


def read_value_from_file(file_path: str, working_dir: str | None = None) -> str:
    """Read a file's contents, stripping trailing whitespace.

    Temporarily changes to working_dir (if given) so relative file_path values
    resolve correctly regardless of the process's current working directory.
    """
    original_dir = os.getcwd()  # noqa: PTH109
    if working_dir:
        os.chdir(working_dir)
    try:
        with open(file_path) as fh:  # noqa: PTH123
            return fh.read().rstrip("\n")
    finally:
        if os.getcwd() != original_dir:  # noqa: PTH109
            os.chdir(original_dir)
