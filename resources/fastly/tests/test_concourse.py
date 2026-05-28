"""Tests for the Fastly Concourse resource."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from concoursetools import BuildMetadata

from concourse import FastlyResource, FastlyVersion

API_TOKEN = "test-fastly-token-abc123"
SERVICE_ID = "SU1Z0isxPaozGVKXdv0eY"
DOMAIN = "www.example.com"


def mock_build_metadata() -> BuildMetadata:
    """Return a BuildMetadata instance with placeholder values."""
    return BuildMetadata(
        BUILD_ID="1",
        BUILD_NAME="1",
        BUILD_JOB_NAME="test-job",
        BUILD_PIPELINE_NAME="test-pipeline",
        BUILD_PIPELINE_INSTANCE_VARS="{}",
        BUILD_TEAM_NAME="main",
        ATC_EXTERNAL_URL="http://concourse.example.com",
    )


def make_version_response(
    number: int,
    active: bool = False,
    updated_at: str = "2024-01-15T10:30:00+00:00",
) -> MagicMock:
    """Return a mock VersionResponse object."""
    from datetime import datetime

    mock = MagicMock()
    mock.number = number
    mock.active = active
    mock.updated_at = datetime.fromisoformat(updated_at)
    return mock


@pytest.fixture
def resource() -> FastlyResource:
    """Return a FastlyResource with a fixed API token and service ID."""
    return FastlyResource(api_token=API_TOKEN, service_id=SERVICE_ID)


@pytest.fixture
def resource_by_domain() -> FastlyResource:
    """Return a FastlyResource configured with a domain instead of a service ID."""
    return FastlyResource(api_token=API_TOKEN, domain=DOMAIN)


@pytest.fixture
def resource_no_service() -> FastlyResource:
    """Return a FastlyResource with no default service ID."""
    return FastlyResource(api_token=API_TOKEN)


# ---------------------------------------------------------------------------
# FastlyVersion
# ---------------------------------------------------------------------------


def test_fastly_version_default() -> None:
    """FastlyVersion defaults to service_version='0'."""
    v = FastlyVersion()
    assert v.service_version == "0"


def test_fastly_version_hashable() -> None:
    """FastlyVersion instances must be hashable for use in sets."""
    v = FastlyVersion(service_version="42")
    assert hash(v) == hash(FastlyVersion(service_version="42"))
    assert {v, FastlyVersion(service_version="42")} == {v}


# ---------------------------------------------------------------------------
# _resolve_service_id / _get_service_id
# ---------------------------------------------------------------------------


def test_resolve_service_id_uses_source(resource: FastlyResource) -> None:
    """Without an override, the source-level service_id is returned."""
    assert resource._resolve_service_id(None) == SERVICE_ID


def test_resolve_service_id_override(resource: FastlyResource) -> None:
    """A step-level override takes precedence over the source service_id."""
    assert resource._resolve_service_id("OTHER_SVC") == "OTHER_SVC"


def test_resolve_service_id_missing_raises(
    resource_no_service: FastlyResource,
) -> None:
    """Raises ValueError when neither source, override, nor domain is set."""
    with pytest.raises(ValueError, match="service_id or domain must be set"):
        resource_no_service._resolve_service_id(None)


def test_get_service_id_prefers_override(resource_by_domain: FastlyResource) -> None:
    """_get_service_id returns the step-level override even when domain is set."""
    mock_client = MagicMock()
    result = resource_by_domain._get_service_id("EXPLICIT_OVERRIDE", mock_client)
    assert result == "EXPLICIT_OVERRIDE"


def test_get_service_id_prefers_source_service_id() -> None:
    """_get_service_id returns source service_id over domain when both are set."""
    r = FastlyResource(api_token=API_TOKEN, service_id=SERVICE_ID, domain=DOMAIN)
    mock_client = MagicMock()
    result = r._get_service_id(None, mock_client)
    assert result == SERVICE_ID


def test_get_service_id_falls_back_to_domain_lookup(
    resource_by_domain: FastlyResource,
) -> None:
    """_get_service_id calls domain lookup when no service_id is available."""
    mock_client = MagicMock()
    with patch.object(
        resource_by_domain,
        "_lookup_service_id_by_domain",
        return_value=SERVICE_ID,
    ) as mock_lookup:
        result = resource_by_domain._get_service_id(None, mock_client)
    mock_lookup.assert_called_once_with(mock_client, DOMAIN)
    assert result == SERVICE_ID


# ---------------------------------------------------------------------------
# fetch_new_versions (check)
# ---------------------------------------------------------------------------


def _patch_active(resource: FastlyResource, active_number: int) -> Any:
    """Patch _active_version to return a version with *active_number*."""
    mock_ver = make_version_response(active_number, active=True)
    return patch.object(resource, "_active_version", return_value=mock_ver)


def test_check_no_previous_returns_current(resource: FastlyResource) -> None:
    """With no previous version, check returns only the current active version."""
    with _patch_active(resource, 5), patch.object(resource, "_api_client"):
        versions = resource.fetch_new_versions(None)
    assert versions == [FastlyVersion(service_version="5")]


def test_check_unchanged_returns_previous(resource: FastlyResource) -> None:
    """When the active version has not changed, check returns the previous version."""
    previous = FastlyVersion(service_version="5")
    with _patch_active(resource, 5), patch.object(resource, "_api_client"):
        versions = resource.fetch_new_versions(previous)
    assert versions == [previous]


def test_check_new_version_returns_current(resource: FastlyResource) -> None:
    """When a newer version is active, check returns that version."""
    previous = FastlyVersion(service_version="4")
    with _patch_active(resource, 7), patch.object(resource, "_api_client"):
        versions = resource.fetch_new_versions(previous)
    assert versions == [FastlyVersion(service_version="7")]


def test_check_requires_service_id(resource_no_service: FastlyResource) -> None:
    """Check raises ValueError when no service_id is configured."""
    with pytest.raises(ValueError, match="service_id or domain must be set"):
        resource_no_service.fetch_new_versions(None)


# ---------------------------------------------------------------------------
# download_version (get)
# ---------------------------------------------------------------------------


def _make_vcl_response(
    content: str, name: str = "main_vcl", main: bool = True
) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.content = content
    m.main = main
    return m


def test_get_writes_metadata_files(resource: FastlyResource, tmp_path: Path) -> None:
    """Get always writes service_version and updated_at files."""
    mock_ver = make_version_response(12, active=True)

    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        version, meta = resource.download_version(
            FastlyVersion(service_version="12"),
            tmp_path,
            mock_build_metadata(),
        )

    assert (tmp_path / "service_version").read_text() == "12"
    assert (tmp_path / "updated_at").read_text() != ""
    assert meta["service_version"] == "12"
    assert meta["service_id"] == SERVICE_ID


def test_get_fetch_vcl_generated(resource: FastlyResource, tmp_path: Path) -> None:
    """Get with fetch_vcl='generated' writes vcl/generated.vcl."""
    mock_ver = make_version_response(12, active=True)
    mock_generated = MagicMock()
    mock_generated.content = "sub vcl_recv { pass; }"

    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.vcl_api.VclApi") as MockVclApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        MockVclApi.return_value.get_custom_vcl_generated.return_value = mock_generated
        resource.download_version(
            FastlyVersion(service_version="12"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl="generated",
        )

    assert (tmp_path / "vcl" / "generated.vcl").read_text() == "sub vcl_recv { pass; }"


def test_get_fetch_vcl_custom(resource: FastlyResource, tmp_path: Path) -> None:
    """Get with fetch_vcl='custom' writes per-file VCL and a 'main' pointer."""
    mock_ver = make_version_response(12, active=True)
    vcl_file = _make_vcl_response("sub vcl_recv {}", name="custom_vcl", main=True)

    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.vcl_api.VclApi") as MockVclApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        MockVclApi.return_value.list_custom_vcl.return_value = [vcl_file]
        resource.download_version(
            FastlyVersion(service_version="12"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl="custom",
        )

    assert (tmp_path / "vcl" / "custom_vcl.vcl").read_text() == "sub vcl_recv {}"
    assert (tmp_path / "vcl" / "main").read_text() == "custom_vcl"


def test_get_fetch_vcl_both(resource: FastlyResource, tmp_path: Path) -> None:
    """Get with fetch_vcl='both' writes generated.vcl and custom files."""
    mock_ver = make_version_response(12, active=True)
    mock_generated = MagicMock()
    mock_generated.content = "# generated"
    vcl_file = _make_vcl_response("# custom", name="my_vcl", main=True)

    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.vcl_api.VclApi") as MockVclApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        MockVclApi.return_value.get_custom_vcl_generated.return_value = mock_generated
        MockVclApi.return_value.list_custom_vcl.return_value = [vcl_file]
        resource.download_version(
            FastlyVersion(service_version="12"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl="both",
        )

    assert (tmp_path / "vcl" / "generated.vcl").read_text() == "# generated"
    assert (tmp_path / "vcl" / "my_vcl.vcl").read_text() == "# custom"


def test_get_custom_vcl_dir(resource: FastlyResource, tmp_path: Path) -> None:
    """The vcl_dir param controls where VCL files are written."""
    mock_ver = make_version_response(3, active=True)
    mock_generated = MagicMock()
    mock_generated.content = "# g"

    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.vcl_api.VclApi") as MockVclApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        MockVclApi.return_value.get_custom_vcl_generated.return_value = mock_generated
        resource.download_version(
            FastlyVersion(service_version="3"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl="generated",
            vcl_dir="output",
        )

    assert (tmp_path / "output" / "generated.vcl").exists()


# ---------------------------------------------------------------------------
# publish_new_version (put)
# ---------------------------------------------------------------------------


def _mock_purge_client() -> MagicMock:
    mock = MagicMock()
    mock.purge_all.return_value = MagicMock(status="ok")
    mock.purge_tag.return_value = MagicMock(status="ok", id="abc123")
    mock.bulk_purge_tag.return_value = {"key1": "abc", "key2": "def"}
    mock.purge_single_url.return_value = MagicMock(status="ok", id="xyz789")
    return mock


def test_put_purge_all(resource: FastlyResource, tmp_path: Path) -> None:
    """Put with mode='purge_all' calls PurgeApi.purge_all with the service ID."""
    purge_mock = _mock_purge_client()
    with (
        patch("concourse.purge_api.PurgeApi", return_value=purge_mock),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        version, meta = resource.publish_new_version(
            tmp_path, mock_build_metadata(), mode="purge_all"
        )
    purge_mock.purge_all.assert_called_once_with(SERVICE_ID)
    assert meta["mode"] == "purge_all"
    assert meta["soft"] == "false"
    assert version == FastlyVersion(service_version="0")


def test_put_purge_all_service_id_override(
    resource_no_service: FastlyResource, tmp_path: Path
) -> None:
    """Put accepts a step-level service_id override."""
    purge_mock = _mock_purge_client()
    with (
        patch("concourse.purge_api.PurgeApi", return_value=purge_mock),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        resource_no_service.publish_new_version(
            tmp_path,
            mock_build_metadata(),
            mode="purge_all",
            service_id="OVERRIDE_SVC",
        )
    purge_mock.purge_all.assert_called_once_with("OVERRIDE_SVC")


def test_put_surrogate_key(resource: FastlyResource, tmp_path: Path) -> None:
    """Put with mode='surrogate_key' calls purge_tag with correct arguments."""
    purge_mock = _mock_purge_client()
    with (
        patch("concourse.purge_api.PurgeApi", return_value=purge_mock),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        _, meta = resource.publish_new_version(
            tmp_path,
            mock_build_metadata(),
            mode="surrogate_key",
            surrogate_key="html-pages",
        )
    purge_mock.purge_tag.assert_called_once_with(
        SERVICE_ID, "html-pages", fastly_soft_purge=None
    )
    assert meta["surrogate_key"] == "html-pages"


def test_put_surrogate_key_soft(resource: FastlyResource, tmp_path: Path) -> None:
    """Put with mode='surrogate_key' and soft=True passes fastly_soft_purge=1."""
    purge_mock = _mock_purge_client()
    with (
        patch("concourse.purge_api.PurgeApi", return_value=purge_mock),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        resource.publish_new_version(
            tmp_path,
            mock_build_metadata(),
            mode="surrogate_key",
            surrogate_key="html-pages",
            soft=True,
        )
    purge_mock.purge_tag.assert_called_once_with(
        SERVICE_ID, "html-pages", fastly_soft_purge=1
    )


def test_put_surrogate_keys_batch(resource: FastlyResource, tmp_path: Path) -> None:
    """Put with mode='surrogate_keys' calls bulk_purge_tag with space-joined keys."""
    purge_mock = _mock_purge_client()
    with (
        patch("concourse.purge_api.PurgeApi", return_value=purge_mock),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        _, meta = resource.publish_new_version(
            tmp_path,
            mock_build_metadata(),
            mode="surrogate_keys",
            surrogate_keys=["key1", "key2", "key3"],
        )
    purge_mock.bulk_purge_tag.assert_called_once_with(
        SERVICE_ID,
        surrogate_key="key1 key2 key3",
        fastly_soft_purge=None,
    )
    assert meta["surrogate_keys"] == "key1 key2 key3"


def test_put_url_purge(resource: FastlyResource, tmp_path: Path) -> None:
    """Put with mode='url' calls purge_single_url."""
    purge_mock = _mock_purge_client()
    target_url = "https://www.example.com/path/to/page"
    with (
        patch("concourse.purge_api.PurgeApi", return_value=purge_mock),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        _, meta = resource.publish_new_version(
            tmp_path,
            mock_build_metadata(),
            mode="url",
            url=target_url,
        )
    purge_mock.purge_single_url.assert_called_once_with(
        target_url, fastly_soft_purge=None
    )
    assert meta["url"] == target_url


def test_put_soft_purge_all_raises(resource: FastlyResource, tmp_path: Path) -> None:
    """soft=True with mode='purge_all' raises ValueError."""
    with pytest.raises(
        ValueError,
        match="soft=true is not supported with mode='purge_all'",
    ):
        resource.publish_new_version(
            tmp_path, mock_build_metadata(), mode="purge_all", soft=True
        )


def test_put_surrogate_key_missing_key_raises(
    resource: FastlyResource, tmp_path: Path
) -> None:
    """mode='surrogate_key' without surrogate_key param raises ValueError."""
    with (
        patch("concourse.purge_api.PurgeApi"),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
        pytest.raises(ValueError, match="surrogate_key param is required"),
    ):
        resource.publish_new_version(
            tmp_path, mock_build_metadata(), mode="surrogate_key"
        )


def test_put_surrogate_keys_missing_keys_raises(
    resource: FastlyResource, tmp_path: Path
) -> None:
    """mode='surrogate_keys' without surrogate_keys param raises ValueError."""
    with (
        patch("concourse.purge_api.PurgeApi"),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
        pytest.raises(ValueError, match="surrogate_keys param is required"),
    ):
        resource.publish_new_version(
            tmp_path, mock_build_metadata(), mode="surrogate_keys"
        )


def test_put_url_missing_url_raises(resource: FastlyResource, tmp_path: Path) -> None:
    """mode='url' without url param raises ValueError."""
    with (
        patch("concourse.purge_api.PurgeApi"),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
        pytest.raises(ValueError, match="url param is required"),
    ):
        resource.publish_new_version(tmp_path, mock_build_metadata(), mode="url")


def test_put_missing_service_id_raises(
    resource_no_service: FastlyResource, tmp_path: Path
) -> None:
    """Put without a service_id raises ValueError for service-requiring modes."""
    with pytest.raises(ValueError, match="service_id or domain must be set"):
        resource_no_service.publish_new_version(
            tmp_path, mock_build_metadata(), mode="purge_all"
        )


def test_put_surrogate_keys_over_limit_raises(
    resource: FastlyResource, tmp_path: Path
) -> None:
    """Passing more than 256 surrogate keys raises ValueError before the API call."""
    too_many = [f"key-{i}" for i in range(257)]
    with pytest.raises(ValueError, match="at most 256 keys"):
        resource.publish_new_version(
            tmp_path,
            mock_build_metadata(),
            mode="surrogate_keys",
            surrogate_keys=too_many,
        )


def test_check_rollback_returns_current(resource: FastlyResource) -> None:
    """Check emits the current (lower) version when an older version is re-activated."""
    previous = FastlyVersion(service_version="10")
    with _patch_active(resource, 7), patch.object(resource, "_api_client"):
        versions = resource.fetch_new_versions(previous)
    assert versions == [FastlyVersion(service_version="7")]


def test_get_fetch_vcl_true_raises(resource: FastlyResource, tmp_path: Path) -> None:
    """fetch_vcl=True raises ValueError; only the named literals are accepted."""
    mock_ver = make_version_response(5, active=True)
    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
        pytest.raises(ValueError, match="fetch_vcl must be one of"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        resource.download_version(
            FastlyVersion(service_version="5"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl=True,  # type: ignore[arg-type]
        )


def test_get_vcl_dir_absolute_raises(resource: FastlyResource, tmp_path: Path) -> None:
    """vcl_dir as an absolute path raises ValueError."""
    mock_ver = make_version_response(5, active=True)
    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
        pytest.raises(ValueError, match="vcl_dir must be a relative path"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        resource.download_version(
            FastlyVersion(service_version="5"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl="generated",
            vcl_dir="/var/evil",
        )


def test_get_vcl_dir_traversal_raises(resource: FastlyResource, tmp_path: Path) -> None:
    """vcl_dir containing '..' raises ValueError."""
    mock_ver = make_version_response(5, active=True)
    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
        pytest.raises(ValueError, match="vcl_dir must be a relative path"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        resource.download_version(
            FastlyVersion(service_version="5"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl="generated",
            vcl_dir="../../etc",
        )


def test_get_vcl_unsafe_name_raises(resource: FastlyResource, tmp_path: Path) -> None:
    """A VCL file whose API name contains a path separator raises ValueError."""
    mock_ver = make_version_response(5, active=True)
    evil_file = _make_vcl_response("# evil", name="../escape", main=False)
    with (
        patch("concourse.version_api.VersionApi") as MockVersionApi,
        patch("concourse.vcl_api.VclApi") as MockVclApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
        pytest.raises(ValueError, match="Unsafe VCL name"),
    ):
        MockVersionApi.return_value.get_service_version.return_value = mock_ver
        MockVclApi.return_value.list_custom_vcl.return_value = [evil_file]
        resource.download_version(
            FastlyVersion(service_version="5"),
            tmp_path,
            mock_build_metadata(),
            fetch_vcl="custom",
        )


def test_validate_vcl_name_valid() -> None:
    """_validate_vcl_name returns the name unchanged for safe names."""
    from concourse import _validate_vcl_name

    assert _validate_vcl_name("my-vcl") == "my-vcl"
    assert _validate_vcl_name("recv_handler") == "recv_handler"


def test_validate_vcl_name_slash_raises() -> None:
    """_validate_vcl_name raises ValueError for names containing '/'."""
    from concourse import _validate_vcl_name

    with pytest.raises(ValueError, match="Unsafe VCL name"):
        _validate_vcl_name("dir/traversal")


def test_validate_vcl_name_dotdot_raises() -> None:
    """_validate_vcl_name raises ValueError for names starting with '.'."""
    from concourse import _validate_vcl_name

    with pytest.raises(ValueError, match="Unsafe VCL name"):
        _validate_vcl_name(".hidden")


# ---------------------------------------------------------------------------
# _lookup_service_id_by_domain
# ---------------------------------------------------------------------------


def _make_service(svc_id: str) -> MagicMock:
    """Return a mock service list entry."""
    s = MagicMock()
    s.id = svc_id
    return s


def _make_domain_entry(name: str) -> MagicMock:
    """Return a mock DomainResponse entry."""
    d = MagicMock()
    d.name = name
    return d


def test_lookup_service_id_by_domain_found(
    resource_by_domain: FastlyResource,
) -> None:
    """Domain lookup returns the service ID when the domain is found."""
    mock_svc = _make_service(SERVICE_ID)
    mock_domain = _make_domain_entry(DOMAIN)

    with (
        patch("concourse.service_api.ServiceApi") as MockServiceApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockServiceApi.return_value.list_services.return_value = [mock_svc]
        MockServiceApi.return_value.list_service_domains.return_value = [mock_domain]
        with resource_by_domain._api_client() as client:
            result = resource_by_domain._lookup_service_id_by_domain(client, DOMAIN)

    assert result == SERVICE_ID


def test_lookup_service_id_by_domain_not_found(
    resource_by_domain: FastlyResource,
) -> None:
    """Domain lookup raises ValueError when no service claims the domain."""
    mock_svc = _make_service(SERVICE_ID)
    mock_domain = _make_domain_entry("other.example.com")

    with (
        patch("concourse.service_api.ServiceApi") as MockServiceApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockServiceApi.return_value.list_services.return_value = [mock_svc]
        MockServiceApi.return_value.list_service_domains.return_value = [mock_domain]
        with (
            resource_by_domain._api_client() as client,
            pytest.raises(ValueError, match="No Fastly service found"),
        ):
            resource_by_domain._lookup_service_id_by_domain(client, DOMAIN)


def test_lookup_service_id_paginates(resource_by_domain: FastlyResource) -> None:
    """Domain lookup fetches subsequent pages until the domain is found."""
    page1 = [_make_service(f"SVC{i:03d}") for i in range(100)]
    page2 = [_make_service("TARGET_SVC")]
    target_domain = _make_domain_entry(DOMAIN)
    other_domain = _make_domain_entry("other.example.com")

    def list_services_side_effect(**kwargs: object) -> list[MagicMock]:
        return page1 if kwargs.get("page") == 1 else page2

    def list_service_domains_side_effect(svc_id: str) -> list[MagicMock]:
        return [target_domain] if svc_id == "TARGET_SVC" else [other_domain]

    with (
        patch("concourse.service_api.ServiceApi") as MockServiceApi,
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        MockServiceApi.return_value.list_services.side_effect = (
            list_services_side_effect
        )
        MockServiceApi.return_value.list_service_domains.side_effect = (
            list_service_domains_side_effect
        )
        with resource_by_domain._api_client() as client:
            result = resource_by_domain._lookup_service_id_by_domain(client, DOMAIN)

    assert result == "TARGET_SVC"


def test_check_uses_domain_lookup(resource_by_domain: FastlyResource) -> None:
    """Check resolves the service ID via domain when service_id is not set."""
    mock_ver = make_version_response(3, active=True)
    with (
        patch.object(
            resource_by_domain, "_lookup_service_id_by_domain", return_value=SERVICE_ID
        ),
        patch.object(resource_by_domain, "_active_version", return_value=mock_ver),
        patch.object(resource_by_domain, "_api_client"),
    ):
        versions = resource_by_domain.fetch_new_versions(None)
    assert versions == [FastlyVersion(service_version="3")]


def test_put_uses_domain_lookup(
    resource_by_domain: FastlyResource, tmp_path: Path
) -> None:
    """Put resolves the service ID via domain for purge operations."""
    purge_mock = _mock_purge_client()
    with (
        patch("concourse.purge_api.PurgeApi", return_value=purge_mock),
        patch.object(
            resource_by_domain, "_lookup_service_id_by_domain", return_value=SERVICE_ID
        ),
        patch("concourse.fastly.ApiClient"),
        patch("concourse.fastly.Configuration"),
    ):
        resource_by_domain.publish_new_version(
            tmp_path, mock_build_metadata(), mode="purge_all"
        )
    purge_mock.purge_all.assert_called_once_with(SERVICE_ID)
