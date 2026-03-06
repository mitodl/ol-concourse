"""Load pulumi resource modules under unique sys.modules names.

Because both packer and pulumi resources use the same filenames (concourse.py,
io_utils.py), we load each resource's modules under unique names to avoid
conflicts when running the full test suite.
"""

import importlib.util
import sys
from pathlib import Path

_PULUMI_DIR = Path(__file__).parent.parent


def _load(unique_name: str, filename: str):
    """Load a module from _PULUMI_DIR and register it under unique_name."""
    if unique_name in sys.modules:
        return sys.modules[unique_name]
    spec = importlib.util.spec_from_file_location(unique_name, _PULUMI_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load helper modules with unique names first.
pulumi_io_utils = _load("pulumi_io_utils", "io_utils.py")
pulumi_utils_mod = _load("pulumi_utils_mod", "pulumi_utils.py")

# Alias the short names that concourse.py uses internally.
# Use setdefault so we don't override packer's io_utils if already registered.
sys.modules.setdefault("io_utils", pulumi_io_utils)
sys.modules["pulumi_utils"] = pulumi_utils_mod

# Load concourse.py last (depends on io_utils + pulumi_utils already in sys.modules)
_load("pulumi_concourse", "concourse.py")
