"""Tests for the PyPI Concourse resource."""

from typing import Any

import pytest
import responses as resp_lib

from concourse import PyPIResource, PyPIVersion, _extract_version_from_filenames

PACKAGE_NAME = "ol-concourse-lib"
TOKEN = "pypi-test-token-1234"

PYPI_METADATA: dict[str, Any] = {
    "info": {"name": PACKAGE_NAME, "version": "0.3.0"},
    "releases": {
        "0.1.0": [{"packagetype": "sdist", "filename": f"{PACKAGE_NAME}-0.1.0.tar.gz"}],
        "0.2.0": [{"packagetype": "sdist", "filename": f"{PACKAGE_NAME}-0.2.0.tar.gz"}],
        "0.3.0": [
            {
                "packagetype": "sdist",
                "filename": f"{PACKAGE_NAME}-0.3.0.tar.gz",
                "url": f"https://files.pythonhosted.org/{PACKAGE_NAME}-0.3.0.tar.gz",
            },
            {
                "packagetype": "bdist_wheel",
                "filename": f"{PACKAGE_NAME}-0.3.0-py3-none-any.whl",
                "url": f"https://files.pythonhosted.org/{PACKAGE_NAME}-0.3.0-py3-none-any.whl",
            },
        ],
        "0.1.0a1": [
            {"packagetype": "sdist", "filename": f"{PACKAGE_NAME}-0.1.0a1.tar.gz"}
        ],
    },
    "urls": [],
}

PYPI_VERSION_METADATA = {
    "info": {"name": PACKAGE_NAME, "version": "0.3.0"},
    "urls": PYPI_METADATA["releases"]["0.3.0"],
}


@pytest.fixture
def resource():
    return PyPIResource(
        package_name=PACKAGE_NAME,
        password=TOKEN,
    )


# ---------------------------------------------------------------------------
# fetch_new_versions
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_fetch_new_versions_no_previous(resource):
    """With no previous version, only the latest (max) version is returned."""
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/json",
        json=PYPI_METADATA,
    )
    versions = resource.fetch_new_versions(None)
    version_strs = {v.version for v in versions}
    assert version_strs == {"0.3.0"}


@resp_lib.activate
def test_fetch_new_versions_with_previous(resource):
    """Versions strictly newer than the previous version are returned."""
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/json",
        json=PYPI_METADATA,
    )
    previous = PyPIVersion(version="0.1.0")
    versions = resource.fetch_new_versions(previous)
    version_strs = {v.version for v in versions}
    # 0.2.0, 0.3.0 are newer; 0.1.0a1 is *older* than 0.1.0 per PEP 440
    assert version_strs == {"0.2.0", "0.3.0"}


@resp_lib.activate
def test_fetch_new_versions_none_newer(resource):
    """When the previous version is already the latest, returns empty set."""
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/json",
        json=PYPI_METADATA,
    )
    previous = PyPIVersion(version="0.3.0")
    versions = resource.fetch_new_versions(previous)
    assert versions == set()


@resp_lib.activate
def test_fetch_new_versions_empty_package(resource):
    """An empty releases dict returns an empty set."""
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/json",
        json={"releases": {}},
    )
    versions = resource.fetch_new_versions(None)
    assert versions == set()


# ---------------------------------------------------------------------------
# download_version
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_download_version_sdist_only(resource, tmp_path):
    """With default params, only the sdist file is downloaded."""
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/0.3.0/json",
        json=PYPI_VERSION_METADATA,
    )
    tarball_url = f"https://files.pythonhosted.org/{PACKAGE_NAME}-0.3.0.tar.gz"
    resp_lib.add(resp_lib.GET, tarball_url, body=b"fake-sdist-content")

    from concoursetools import BuildMetadata

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )
    version = PyPIVersion(version="0.3.0")
    returned_version, metadata = resource.download_version(
        version, str(tmp_path), build_meta
    )

    assert returned_version.version == "0.3.0"
    assert (tmp_path / f"{PACKAGE_NAME}-0.3.0.tar.gz").exists()
    assert PACKAGE_NAME in metadata.get("files", "")


@resp_lib.activate
def test_download_version_wheel(resource, tmp_path):
    """When download_wheel=True, the wheel is also downloaded."""
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/0.3.0/json",
        json=PYPI_VERSION_METADATA,
    )
    sdist_url = f"https://files.pythonhosted.org/{PACKAGE_NAME}-0.3.0.tar.gz"
    wheel_url = f"https://files.pythonhosted.org/{PACKAGE_NAME}-0.3.0-py3-none-any.whl"
    resp_lib.add(resp_lib.GET, sdist_url, body=b"fake-sdist")
    resp_lib.add(resp_lib.GET, wheel_url, body=b"fake-wheel")

    from concoursetools import BuildMetadata

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )
    version = PyPIVersion(version="0.3.0")
    returned_version, _ = resource.download_version(
        version, str(tmp_path), build_meta, download_wheel=True
    )

    assert (tmp_path / f"{PACKAGE_NAME}-0.3.0.tar.gz").exists()
    assert (tmp_path / f"{PACKAGE_NAME}-0.3.0-py3-none-any.whl").exists()
    assert returned_version.version == "0.3.0"


# ---------------------------------------------------------------------------
# publish_new_version
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_publish_new_version(resource, tmp_path):
    """publish_new_version calls twine and returns the version from the filename."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    sdist_file = dist_dir / f"{PACKAGE_NAME}-1.0.0.tar.gz"
    sdist_file.write_bytes(b"fake-sdist")

    from concoursetools import BuildMetadata
    from unittest.mock import patch

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    # Version has never been published -- PyPI's per-version endpoint 404s.
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/1.0.0/json",
        status=404,
    )

    with patch("concourse.subprocess.run") as mock_run:
        mock_run.return_value.stdout = (
            "Uploading distributions to https://upload.pypi.org/legacy/"
        )
        mock_run.return_value.returncode = 0

        version, metadata = resource.publish_new_version(
            tmp_path,
            build_meta,
            glob=f"dist/{PACKAGE_NAME}-*.tar.gz",
        )

    assert version.version == "1.0.0"
    assert PACKAGE_NAME in metadata["uploaded_files"]
    assert "skipped" not in metadata
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert "twine" in call_args
    assert "upload" in call_args
    assert str(sdist_file) in call_args


@resp_lib.activate
def test_publish_new_version_already_published_skips_upload(resource, tmp_path):
    """A version whose files are all already on PyPI is skipped, not re-uploaded.

    This is the retriggered-build case: a commit touching this resource's
    watched paths without bumping pyproject.toml's version reproduces an
    already-published artifact. twine would crash on PyPI's "already exists"
    rejection with no visible error text; this should be a clean no-op.
    """
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    sdist_file = dist_dir / f"{PACKAGE_NAME}-0.3.0.tar.gz"
    sdist_file.write_bytes(b"fake-sdist")

    from concoursetools import BuildMetadata
    from unittest.mock import patch

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/0.3.0/json",
        json=PYPI_VERSION_METADATA,
    )

    with patch("concourse.subprocess.run") as mock_run:
        version, metadata = resource.publish_new_version(
            tmp_path,
            build_meta,
            glob=f"dist/{PACKAGE_NAME}-*.tar.gz",
        )

    assert version.version == "0.3.0"
    assert metadata["skipped"] == "already published"
    mock_run.assert_not_called()


@resp_lib.activate
def test_publish_new_version_partial_upload_only_uploads_missing_files(
    resource, tmp_path
):
    """A version with some but not all files already on PyPI uploads only the rest.

    A prior twine upload can succeed for one file (e.g. the sdist) and fail
    before reaching another (e.g. the wheel). Retrying must upload only the
    missing file -- uploading everything would fail again on the file that
    already succeeded, and skipping entirely would leave the wheel
    unpublished forever.
    """
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    sdist_file = dist_dir / f"{PACKAGE_NAME}-0.3.0.tar.gz"
    wheel_file = dist_dir / f"{PACKAGE_NAME}-0.3.0-py3-none-any.whl"
    sdist_file.write_bytes(b"fake-sdist")
    wheel_file.write_bytes(b"fake-wheel")

    from concoursetools import BuildMetadata
    from unittest.mock import patch

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    # Only the sdist made it to PyPI on the prior (partially-failed) attempt.
    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/0.3.0/json",
        json={
            "info": {"name": PACKAGE_NAME, "version": "0.3.0"},
            "urls": [PYPI_METADATA["releases"]["0.3.0"][0]],  # sdist only
        },
    )

    with patch("concourse.subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.returncode = 0

        version, metadata = resource.publish_new_version(
            tmp_path,
            build_meta,
            glob=f"dist/{PACKAGE_NAME}-*",
        )

    assert version.version == "0.3.0"
    assert "skipped" not in metadata
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert str(wheel_file) in call_args
    assert str(sdist_file) not in call_args


@resp_lib.activate
def test_publish_new_version_twine_failure_surfaces_output(resource, tmp_path):
    """A genuine twine failure raises with the captured stdout/stderr visible.

    Previously used check=True, which raised CalledProcessError without ever
    printing the captured output -- the real failure reason never made it
    into the Concourse build log.
    """
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    sdist_file = dist_dir / f"{PACKAGE_NAME}-1.0.0.tar.gz"
    sdist_file.write_bytes(b"fake-sdist")

    from concoursetools import BuildMetadata
    from unittest.mock import patch

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    resp_lib.add(
        resp_lib.GET,
        f"https://pypi.org/pypi/{PACKAGE_NAME}/1.0.0/json",
        status=404,
    )

    with patch("concourse.subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "403 Forbidden: Invalid or non-existent token"
        mock_run.return_value.returncode = 1

        with pytest.raises(RuntimeError, match="403 Forbidden"):
            resource.publish_new_version(
                tmp_path,
                build_meta,
                glob=f"dist/{PACKAGE_NAME}-*.tar.gz",
            )


def test_publish_new_version_no_match_raises(resource, tmp_path):
    """publish_new_version raises FileNotFoundError when no files match the glob."""
    from concoursetools import BuildMetadata

    build_meta = BuildMetadata(
        BUILD_ID="1",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://ci.example.com",
    )

    with pytest.raises(FileNotFoundError, match="No files matched"):
        resource.publish_new_version(
            tmp_path,
            build_meta,
            glob="dist/nonexistent-*.tar.gz",
        )


# ---------------------------------------------------------------------------
# _extract_version_from_filenames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filenames, expected",
    [
        (["ol_concourse_lib-1.2.3.tar.gz"], "1.2.3"),
        (["my_pkg-0.1.0a1-py3-none-any.whl"], "0.1.0a1"),
        (["my_pkg-2.0.0.tar.gz", "my_pkg-2.0.0-py3-none-any.whl"], "2.0.0"),
        (["no-version-here.tar.gz"], "unknown"),
        ([], "unknown"),
    ],
)
def test_extract_version_from_filenames(filenames, expected):
    assert _extract_version_from_filenames(filenames) == expected
