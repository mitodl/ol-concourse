"""Concourse resource for PyPI package publishing and version tracking.

Example source configuration:

  resources:
    - name: my-package-pypi
      type: pypi
      source:
        package_name: my-package
        password: ((pypi.token))

  resource_types:
    - name: pypi
      type: registry-image
      source:
        repository: mitodl/concourse-pypi-resource

Example check: returns versions newer than the pinned version.

Example get params (optional):

  get: my-package-pypi
  params:
    download_sdist: true    # download source distribution (default: true)
    download_wheel: false   # download any wheel (default: false)

Example put params:

  put: my-package-pypi
  params:
    glob: dist/my_package-*.tar.gz

"""

import subprocess
from pathlib import Path

import requests
from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import SortableVersionMixin, Version
from packaging.version import InvalidVersion
from packaging.version import Version as PkgVersion

PYPI_INDEX_URL = "https://pypi.org"
PYPI_UPLOAD_URL = "https://upload.pypi.org/legacy/"


class PyPIVersion(Version, SortableVersionMixin):
    """Version type representing a PyPI package version string."""

    def __init__(self, version: str) -> None:
        self.version = version

    def __lt__(self, other: "PyPIVersion") -> bool:
        try:
            return PkgVersion(self.version) < PkgVersion(other.version)
        except InvalidVersion:
            return self.version < other.version


class PyPIResource(ConcourseResource):
    """Concourse resource for check/get/put against a PyPI-compatible index."""

    def __init__(
        self,
        /,
        package_name: str,
        password: str,
        username: str = "__token__",
        repository_url: str = PYPI_UPLOAD_URL,
        index_url: str = PYPI_INDEX_URL,
    ) -> None:
        super().__init__(PyPIVersion)
        self.package_name = package_name
        self.username = username
        self.password = password
        self.repository_url = repository_url
        self.index_url = index_url.rstrip("/")

    def _get_package_metadata(self) -> dict:
        """Query the PyPI JSON API for all package metadata."""
        url = f"{self.index_url}/pypi/{self.package_name}/json"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def _get_version_files(self, version: str) -> list[dict]:
        """Get file metadata for a specific package version from PyPI."""
        url = f"{self.index_url}/pypi/{self.package_name}/{version}/json"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()["urls"]

    def fetch_new_versions(
        self, previous_version: PyPIVersion | None = None
    ) -> set[PyPIVersion]:
        """Return versions newer than the previous one, or only the latest if none."""
        metadata = self._get_package_metadata()
        all_version_strs = list(metadata.get("releases", {}).keys())

        valid_versions: list[str] = []
        for v in all_version_strs:
            try:
                PkgVersion(v)
                valid_versions.append(v)
            except InvalidVersion:
                pass

        if not valid_versions:
            return set()

        if not previous_version:
            latest = max(valid_versions, key=PkgVersion)
            return {PyPIVersion(version=latest)}

        prev = PkgVersion(previous_version.version)
        return {PyPIVersion(version=v) for v in valid_versions if PkgVersion(v) > prev}

    def download_version(
        self,
        version: PyPIVersion,
        destination_dir: str,
        build_metadata: BuildMetadata,
        download_sdist: bool = True,
        download_wheel: bool = False,
    ) -> tuple[PyPIVersion, dict[str, str]]:
        """Download distribution files for a specific version from PyPI."""
        files = self._get_version_files(version.version)
        dest = Path(destination_dir)
        downloaded: list[str] = []

        for file_info in files:
            pkg_type = file_info.get("packagetype", "")
            if (pkg_type == "sdist" and download_sdist) or (
                pkg_type == "bdist_wheel" and download_wheel
            ):
                url = file_info["url"]
                filename = file_info["filename"]
                response = requests.get(url, timeout=120, stream=True)
                response.raise_for_status()
                target = dest / filename
                with target.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=8192):
                        fh.write(chunk)
                downloaded.append(filename)

        metadata = {"files": ", ".join(downloaded)} if downloaded else {}
        return version, metadata

    def publish_new_version(
        self,
        sources_dir: Path,
        build_metadata: BuildMetadata,
        *,
        glob: str = "dist/*",
    ) -> tuple[PyPIVersion, dict[str, str]]:
        """Upload distribution files matching glob to PyPI using twine."""
        matched = sorted(str(p) for p in Path(sources_dir).glob(glob))
        if not matched:
            msg = f"No files matched glob pattern: {glob!r} in {sources_dir}"
            raise FileNotFoundError(msg)

        cmd = [
            "twine",
            "upload",
            "--repository-url",
            self.repository_url,
            "--username",
            self.username,
            "--password",
            self.password,
            "--non-interactive",
            *matched,
        ]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)  # noqa: S603
        print(result.stdout)  # noqa: T201

        version_str = _extract_version_from_filenames(matched)
        uploaded_names = ", ".join(Path(f).name for f in matched)
        return PyPIVersion(version=version_str), {"uploaded_files": uploaded_names}


def _extract_version_from_filenames(filenames: list[str]) -> str:
    """Extract a PEP 440 version string from distribution filenames.

    Handles both sdist (``pkg-1.2.3.tar.gz``) and wheel
    (``pkg-1.2.3-py3-none-any.whl``) naming conventions, including
    hyphenated package names (e.g. ``my-pkg-1.2.3.tar.gz``).

    Iterates through the dash-separated parts (skipping the first, which is the
    package name) and returns the first segment that parses as a valid PEP 440
    version.  Returns ``"unknown"`` if no valid version can be parsed.
    """
    for filename in filenames:
        name = Path(filename).name
        # Strip extensions to get the base stem
        for ext in (".tar.gz", ".tar.bz2", ".zip", ".whl", ".egg"):
            if name.endswith(ext):
                name = name[: -len(ext)]
                break
        parts = name.split("-")
        # Start at index 1 to skip the (possibly multi-word) package name segment
        for part in parts[1:]:
            try:
                PkgVersion(part)
                return part
            except InvalidVersion:
                continue
    return "unknown"


if __name__ == "__main__":
    PyPIResource.check_main()
