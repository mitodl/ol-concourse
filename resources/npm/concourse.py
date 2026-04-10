"""Concourse resource for NPM package publishing and version tracking.

Example source configuration:

  resources:
    - name: my-npm-package
      type: npm
      source:
        package_name: my-npm-package
        token: ((npm.token))

  resource_types:
    - name: npm
      type: registry-image
      source:
        repository: mitodl/concourse-npm-resource

Example check: returns versions newer than the pinned version.

Example get: downloads the package tarball to the destination directory.

Example put params:

  put: my-npm-package
  params:
    package_dir: my-repo/dist
    tag: latest       # optional, defaults to latest
    access: public    # optional, defaults to public

Authentication note:
  npm's trusted publisher / provenance mechanism (OIDC) is only available for
  GitHub Actions, GitLab CI, and CircleCI cloud runners.  Concourse is not a
  supported provider.  Use an NPM automation token for authentication.

"""

import json
import subprocess
from pathlib import Path

import requests
import semver
from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import SortableVersionMixin, Version

NPM_REGISTRY_URL = "https://registry.npmjs.org"


class NPMVersion(Version, SortableVersionMixin):
    """Version type representing an NPM package version string."""

    def __init__(self, version: str) -> None:
        self.version = version

    def __lt__(self, other: "NPMVersion") -> bool:
        try:
            return semver.Version.parse(self.version) < semver.Version.parse(
                other.version
            )
        except ValueError:
            return self.version < other.version


class NPMResource(ConcourseResource):
    """Concourse resource for check/get/put against an npm-compatible registry."""

    def __init__(
        self,
        /,
        package_name: str,
        token: str,
        registry: str = NPM_REGISTRY_URL,
    ) -> None:
        super().__init__(NPMVersion)
        self.package_name = package_name
        self.token = token
        self.registry = registry.rstrip("/")

    def _get_package_metadata(self) -> dict:
        """Query the npm registry for all package metadata."""
        url = f"{self.registry}/{self.package_name}"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _get_version_tarball_url(self, version: str) -> str:
        """Return the tarball download URL for a specific package version."""
        url = f"{self.registry}/{self.package_name}/{version}"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["dist"]["tarball"]

    def fetch_new_versions(
        self, previous_version: NPMVersion | None = None
    ) -> set[NPMVersion]:
        """Return versions newer than the previous one, or only the latest if none."""
        metadata = self._get_package_metadata()
        all_version_strs = list(metadata.get("versions", {}).keys())

        valid_versions: list[str] = []
        for v in all_version_strs:
            try:
                semver.Version.parse(v)
                valid_versions.append(v)
            except ValueError:
                pass

        if not valid_versions:
            return set()

        if not previous_version:
            latest = max(valid_versions, key=semver.Version.parse)
            return {NPMVersion(version=latest)}

        prev = semver.Version.parse(previous_version.version)
        return {
            NPMVersion(version=v)
            for v in valid_versions
            if semver.Version.parse(v) > prev
        }

    def download_version(
        self,
        version: NPMVersion,
        destination_dir: str,
        build_metadata: BuildMetadata,
    ) -> tuple[NPMVersion, dict[str, str]]:
        """Download the package tarball for a specific version from the registry."""
        tarball_url = self._get_version_tarball_url(version.version)
        dest = Path(destination_dir)

        pkg_stem = self.package_name.lstrip("@").replace("/", "-")
        filename = f"{pkg_stem}-{version.version}.tgz"
        target = dest / filename

        response = requests.get(tarball_url, timeout=120, stream=True)
        response.raise_for_status()
        with target.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=8192):
                fh.write(chunk)

        return version, {"tarball": filename}

    def publish_new_version(
        self,
        sources_dir: Path,
        build_metadata: BuildMetadata,
        *,
        package_dir: str,
        tag: str = "latest",
        access: str = "public",
    ) -> tuple[NPMVersion, dict[str, str]]:
        """Publish the package to the npm registry.

        :param package_dir: Path (relative to sources_dir) to the directory
            containing ``package.json``.
        :param tag: npm distribution tag (default: ``latest``).
        :param access: ``public`` or ``restricted`` (default: ``public``).
        """
        pkg_path = Path(sources_dir) / package_dir

        # Read version from package.json before publishing
        pkg_json_path = pkg_path / "package.json"
        with pkg_json_path.open() as fh:
            pkg_json = json.load(fh)
        version_str = pkg_json["version"]

        # Write .npmrc with auth token scoped to the registry host
        registry_host = self.registry.removeprefix("https:").removeprefix("http:")
        npmrc_path = pkg_path / ".npmrc"
        npmrc_content = (
            f"registry={self.registry}/\n"
            f"{registry_host}/:_authToken={self.token}\n"
        )
        npmrc_path.write_text(npmrc_content)

        try:
            cmd = [
                "npm",
                "publish",
                "--tag",
                tag,
                "--access",
                access,
                "--registry",
                self.registry,
            ]
            result = subprocess.run(  # noqa: S603
                cmd,
                cwd=str(pkg_path),
                check=True,
                capture_output=True,
                text=True,
            )
            print(result.stdout)  # noqa: T201
        finally:
            # Remove .npmrc so the token is not left on disk
            npmrc_path.unlink(missing_ok=True)

        return NPMVersion(version=version_str), {"package_dir": package_dir, "tag": tag}


if __name__ == "__main__":
    NPMResource.check_main()
