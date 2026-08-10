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
import sys
import time
from pathlib import Path
from typing import Any

import requests
from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import SortableVersionMixin, Version
from packaging.version import InvalidVersion
from packaging.version import Version as PkgVersion

PYPI_INDEX_URL = "https://pypi.org"
PYPI_UPLOAD_URL = "https://upload.pypi.org/legacy/"
_HTTP_NOT_FOUND = 404

# How long to keep retrying a 404 while PyPI's JSON index catches up with an
# upload that has already landed. Overridable per-resource in the pipeline's
# `source`, so tuning it is a pipeline re-set rather than a release of this
# image. 0 disables the wait entirely.
_DEFAULT_INDEX_LAG_TIMEOUT_SECONDS = 300.0
# Cap on any single sleep, so a long budget does not become one huge final
# sleep that overshoots the moment the index actually catches up.
_MAX_RETRY_DELAY_SECONDS = 30.0


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

    def __init__(  # noqa: PLR0913
        self,
        /,
        package_name: str,
        password: str,
        username: str = "__token__",
        repository_url: str = PYPI_UPLOAD_URL,
        index_url: str = PYPI_INDEX_URL,
        index_lag_timeout_seconds: float | str = _DEFAULT_INDEX_LAG_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(PyPIVersion)
        self.package_name = package_name
        self.username = username
        self.password = password
        self.repository_url = repository_url
        self.index_url = index_url.rstrip("/")
        # Values arriving from pipeline YAML can be strings; coerce once here
        # rather than assuming an int and failing later inside the arithmetic.
        self.index_lag_timeout_seconds = float(index_lag_timeout_seconds)

    def _get_package_metadata(self) -> dict[str, Any]:
        """Query the PyPI JSON API for all package metadata."""
        url = f"{self.index_url}/pypi/{self.package_name}/json"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def _get_version_files(self, version: str) -> list[dict[str, Any]]:
        """Get file metadata for a specific package version from PyPI."""
        url = f"{self.index_url}/pypi/{self.package_name}/{version}/json"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()["urls"]

    def _get_version_files_with_retry(
        self,
        version: str,
        *,
        max_attempts: int | None = None,
        base_delay_seconds: float = 1.0,
        timeout_seconds: float | None = None,
        max_delay_seconds: float = _MAX_RETRY_DELAY_SECONDS,
    ) -> list[dict[str, Any]]:
        """Fetch version file metadata, retrying a 404 while the index catches up.

        PyPI's JSON index lags behind an upload actually landing. Concourse runs
        an implicit `get` right after a `put` succeeds, and that get races the
        lag: the build goes red reporting a failed publish for a package that is
        already live on pypi.org and installable. Confirmed on
        `publish-ol-concourse-lib` builds 38, 39 and 40, each of which logged
        `View at: https://pypi.org/project/ol-concourse/<version>/` and then
        404'd on `/pypi/<pkg>/<version>/json`.

        The retry is bounded by TIME, not by an attempt count. The previous
        1+2+4+8 exponential gave up after ~15s of waiting, which real lag
        exceeded. Delay is capped at *max_delay_seconds* so a long budget does
        not turn into one enormous final sleep that overshoots the moment the
        index actually catches up.

        Only 404 is retried -- that is the specific "not indexed yet" signal, and
        it is genuinely a miss rather than a cached negative (PyPI returns the
        404 with no `cache-control` and `x-cache: MISS`). Any other status is a
        real error and propagates immediately.

        *max_attempts* is accepted for callers that want to bound iterations
        instead of wall-clock; the two bounds compose, whichever trips first.
        """
        if max_attempts is not None and max_attempts < 1:
            msg = f"max_attempts must be >= 1, got {max_attempts}"
            raise ValueError(msg)
        if base_delay_seconds < 0:
            msg = f"base_delay_seconds must be >= 0, got {base_delay_seconds}"
            raise ValueError(msg)
        budget = (
            self.index_lag_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if budget < 0:
            msg = f"timeout_seconds must be >= 0, got {budget}"
            raise ValueError(msg)

        deadline = time.monotonic() + budget
        attempt = 0
        while True:
            try:
                return self._get_version_files(version)
            except requests.HTTPError as exc:
                is_not_found = (
                    exc.response is not None
                    and exc.response.status_code == _HTTP_NOT_FOUND
                )
                if not is_not_found:
                    raise
                attempt += 1
                if max_attempts is not None and attempt >= max_attempts:
                    raise
                delay = min(
                    base_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds
                )
                if time.monotonic() + delay >= deadline:
                    raise
                print(  # noqa: T201
                    f"{version} not indexed yet; retrying in {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)

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
        files = self._get_version_files_with_retry(version.version)
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

        version_str = _extract_version_from_filenames(matched)
        files_to_upload = self._files_not_yet_published(version_str, matched)

        if not files_to_upload:
            # Every matched file is already on PyPI -- e.g. a commit touching
            # this resource's watched paths didn't bump pyproject.toml's
            # version, so `uv build` reproduced an already-uploaded artifact.
            # PyPI rejects re-uploading an existing file outright (twine
            # surfaces this as a bare non-zero exit with no visible error
            # text, since the failure isn't printed before the exception
            # propagates); treat it as already done instead.
            uploaded_names = ", ".join(Path(f).name for f in matched)
            return PyPIVersion(version=version_str), {
                "uploaded_files": uploaded_names,
                "skipped": "already published",
            }

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
            *files_to_upload,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        print(result.stdout)  # noqa: T201
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)  # noqa: T201
            msg = (
                f"twine upload failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            raise RuntimeError(msg)

        uploaded_names = ", ".join(Path(f).name for f in files_to_upload)
        return PyPIVersion(version=version_str), {"uploaded_files": uploaded_names}

    def _files_not_yet_published(
        self, version: str, matched_files: list[str]
    ) -> list[str]:
        """Return the subset of matched_files not already published to PyPI.

        A version can be partially published if a prior twine upload
        succeeded for some files (e.g. the sdist) but failed before reaching
        others (e.g. the wheel). Retrying should only upload what's actually
        missing -- skipping the whole version would leave the missing file
        unpublished forever, and re-uploading everything would fail again on
        the files that already succeeded.
        """
        try:
            existing_files = self._get_version_files(version)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == _HTTP_NOT_FOUND:
                return matched_files
            raise
        existing_names = {f["filename"] for f in existing_files}
        return [f for f in matched_files if Path(f).name not in existing_names]


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
