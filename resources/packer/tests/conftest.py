"""Load packer resource modules under unique sys.modules names.

Because both packer and pulumi resources use the same filenames (concourse.py,
io_utils.py), we load each resource's modules under unique names to avoid
conflicts when running the full test suite.
"""

import importlib.util
import sys
from pathlib import Path

_PACKER_DIR = Path(__file__).parent.parent


def _load(unique_name: str, filename: str):
    """Load a module from _PACKER_DIR and register it under unique_name."""
    if unique_name in sys.modules:
        return sys.modules[unique_name]
    spec = importlib.util.spec_from_file_location(unique_name, _PACKER_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load helper modules with unique names, then alias under the short names that
# concourse.py uses internally so its `import io_utils` / `import packer` resolve.
packer_io_utils = _load("packer_io_utils", "io_utils.py")
packer_lib = _load("packer_lib", "packer.py")

sys.modules.setdefault("io_utils", packer_io_utils)
sys.modules.setdefault("packer", packer_lib)

# Load concourse.py last (depends on io_utils + packer already in sys.modules)
_load("packer_concourse", "concourse.py")
