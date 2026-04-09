"""Tests for the NPM Concourse resource."""

import json

import pytest
import responses as resp_lib

from concourse import NPMResource, NPMVersion

PACKAGE_NAME = "my-npm-package"
TOKEN = "npm-test-token-5678"
REGISTRY = "https://registry.npmjs.org"

NPM_METADATA = {
    "name": PACKAGE_NAME,
    "dist-tags": {"latest": "1.2.0"},
    "versions": {
        "1.0.0": {"name": PACKAGE_NAME, "version": "1.0.0"},
        "1.1.0": {"name": PACKAGE_NAME, "version": "1.1.0"},
        "1.2.0": {
            "name": PACKAGE_NAME,
            "version": "1.2.0",
            "dist": {
                "tarball": f"{REGISTRY}/{PACKAGE_NAME}/-/{PACKAGE_NAME}-1.2.0.tgz"
            },
        },
        "2.0.0-beta.1": {"name": PACKAGE_NAME, "version": "2.0.0-beta.1"},
    },
}

NPM_VERSION_METADATA = {
    "name": PACKAGE_NAME,
    "version": "1.2.0",
    "dist": {"tarball": f"{REGISTRY}/{PACKAGE_NAME}/-/{PACKAGE_NAME}-1.2.0.tgz"},
}


@pytest.fixture
def resource():
    return NPMResource(
        package_name=PACKAGE_NAME,
        token=TOKEN,
    )


# ---------------------------------------------------------------------------
# fetch_new_versions
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_fetch_new_versions_no_previous(resource):
    """With no previous version, only the latest (max) valid version is returned."""
    resp_lib.add(
        resp_lib.GET,
        f"{REGISTRY}/{PACKAGE_NAME}",
        json=NPM_METADATA,
    )
    versions = resource.fetch_new_versions(None)
    version_strs = {v.version for v in versions}
    # 2.0.0-beta.1 > 1.2.0 in PEP 440 ordering (2.x > 1.x even as pre-release)
    assert version_strs == {"2.0.0-beta.1"}


@resp_lib.activate
def test_fetch_new_versions_with_previous(resource):
    """Versions strictly newer than the previous version are returned."""
    resp_lib.add(
        resp_lib.GET,
        f"{REGISTRY}/{PACKAGE_NAME}",
        json=NPM_METADATA,
    )
    previous = NPMVersion(version="1.0.0")
    versions = resource.fetch_new_versions(previous)
    version_strs = {v.version for v in versions}
    # 1.1.0, 1.2.0 are newer; 2.0.0-beta.1 is also newer as a pre-release
    assert "1.1.0" in version_strs
    assert "1.2.0" in version_strs
    assert "1.0.0" not in version_strs


@resp_lib.activate
def test_fetch_new_versions_none_newer(resource):
    """When the previous version is already the latest, returns empty set."""
    resp_lib.add(
        resp_lib.GET,
        f"{REGISTRY}/{PACKAGE_NAME}",
        json=NPM_METADATA,
    )
    previous = NPMVersion(version="1.2.0")
    versions = resource.fetch_new_versions(previous)
    # Only 2.0.0-beta.1 is newer, which is a pre-release
    version_strs = {v.version for v in versions}
    assert "1.0.0" not in version_strs
    assert "1.1.0" not in version_strs
    assert "1.2.0" not in version_strs


@resp_lib.activate
def test_fetch_new_versions_empty_package(resource):
    """An empty versions dict returns an empty set."""
    resp_lib.add(
        resp_lib.GET,
        f"{REGISTRY}/{PACKAGE_NAME}",
        json={"name": PACKAGE_NAME, "versions": {}},
    )
    versions = resource.fetch_new_versions(None)
    assert versions == set()


# ---------------------------------------------------------------------------
# download_version
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_download_version(resource, tmp_path):
    """download_version fetches the tarball and writes it to destination_dir."""
    resp_lib.add(
        resp_lib.GET,
        f"{REGISTRY}/{PACKAGE_NAME}/1.2.0",
        json=NPM_VERSION_METADATA,
    )
    tarball_url = f"{REGISTRY}/{PACKAGE_NAME}/-/{PACKAGE_NAME}-1.2.0.tgz"
    resp_lib.add(resp_lib.GET, tarball_url, body=b"fake-tarball-content")

    from concoursetools import BuildMetadata

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )
    version = NPMVersion(version="1.2.0")
    returned_version, metadata = resource.download_version(
        version, str(tmp_path), build_meta
    )

    assert returned_version.version == "1.2.0"
    expected_filename = f"{PACKAGE_NAME}-1.2.0.tgz"
    assert (tmp_path / expected_filename).exists()
    assert metadata["tarball"] == expected_filename


# ---------------------------------------------------------------------------
# publish_new_version
# ---------------------------------------------------------------------------


def test_publish_new_version(resource, tmp_path):
    """publish_new_version writes .npmrc, calls npm publish, cleans up .npmrc."""
    pkg_dir = tmp_path / "dist"
    pkg_dir.mkdir()
    pkg_json = {"name": PACKAGE_NAME, "version": "1.3.0"}
    (pkg_dir / "package.json").write_text(json.dumps(pkg_json))

    from concoursetools import BuildMetadata
    from unittest.mock import patch

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    with patch("concourse.subprocess.run") as mock_run:
        mock_run.return_value.stdout = "npm notice published v1.3.0"
        mock_run.return_value.returncode = 0

        version, metadata = resource.publish_new_version(
            tmp_path,
            build_meta,
            package_dir="dist",
            tag="latest",
            access="public",
        )

    assert version.version == "1.3.0"
    assert metadata["tag"] == "latest"
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert "npm" in call_args
    assert "publish" in call_args
    # .npmrc should be cleaned up after publish
    assert not (pkg_dir / ".npmrc").exists()


def test_publish_new_version_cleans_npmrc_on_error(resource, tmp_path):
    """publish_new_version removes .npmrc even when npm publish fails."""
    pkg_dir = tmp_path / "dist"
    pkg_dir.mkdir()
    pkg_json = {"name": PACKAGE_NAME, "version": "1.3.0"}
    (pkg_dir / "package.json").write_text(json.dumps(pkg_json))

    from concoursetools import BuildMetadata
    from unittest.mock import patch
    from subprocess import CalledProcessError

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    with patch("concourse.subprocess.run") as mock_run:
        mock_run.side_effect = CalledProcessError(1, "npm publish")

        with pytest.raises(CalledProcessError):
            resource.publish_new_version(
                tmp_path,
                build_meta,
                package_dir="dist",
            )

    # .npmrc must be gone even after the error
    assert not (pkg_dir / ".npmrc").exists()


def test_publish_new_version_npmrc_content(resource, tmp_path):
    """The .npmrc written during publish contains the correct auth token line."""
    pkg_dir = tmp_path / "dist"
    pkg_dir.mkdir()
    pkg_json = {"name": PACKAGE_NAME, "version": "1.3.0"}
    (pkg_dir / "package.json").write_text(json.dumps(pkg_json))

    captured_npmrc: list[str] = []

    from concoursetools import BuildMetadata
    from unittest.mock import patch

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    def capture_and_succeed(*args, **kwargs):
        npmrc = pkg_dir / ".npmrc"
        if npmrc.exists():
            captured_npmrc.append(npmrc.read_text())
        from unittest.mock import MagicMock
        m = MagicMock()
        m.stdout = ""
        return m

    with patch("concourse.subprocess.run", side_effect=capture_and_succeed):
        resource.publish_new_version(
            tmp_path,
            build_meta,
            package_dir="dist",
        )

    assert len(captured_npmrc) == 1
    assert TOKEN in captured_npmrc[0]
    assert "registry.npmjs.org" in captured_npmrc[0]


# ---------------------------------------------------------------------------
# semver correctness: numeric pre-release identifiers
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_fetch_new_versions_numeric_prerelease_is_not_newer_than_final(resource):
    """1.0.0-0 is a semver pre-release and must NOT be treated as newer than 1.0.0.

    packaging.version.Version misclassifies '1.0.0-0' as a post-release (1.0.0.post0),
    which would incorrectly report it as newer than 1.0.0.  The semver package handles
    this correctly: 1.0.0-0 < 1.0.0.
    """
    metadata = {
        "name": PACKAGE_NAME,
        "versions": {
            "1.0.0": {},
            "1.0.0-0": {},   # canary / pre-release published by semantic-release
        },
    }
    resp_lib.add(resp_lib.GET, f"{REGISTRY}/{PACKAGE_NAME}", json=metadata)

    # With previous=1.0.0, nothing should be returned (1.0.0-0 is older)
    previous = NPMVersion(version="1.0.0")
    versions = resource.fetch_new_versions(previous)
    assert versions == set(), (
        "1.0.0-0 is a pre-release and must not appear as newer than 1.0.0"
    )
