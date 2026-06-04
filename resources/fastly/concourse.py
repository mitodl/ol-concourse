"""Concourse resource type for Fastly cache management.

Provides ``check``/``get`` against a Fastly service's active VCL version and a
``put`` step that performs one of four instant-purge operations.

Example source configuration:

  resources:
    - name: my-fastly-service
      type: fastly
      source:
        api_token: ((fastly.api_token))
        service_id: ((fastly.service_id))   # use service_id OR domain, not both

  # Alternatively, identify the service by the domain it serves:

  resources:
    - name: my-fastly-service
      type: fastly
      source:
        api_token: ((fastly.api_token))
        domain: www.example.com

  resource_types:
    - name: fastly
      type: registry-image
      source:
        repository: mitodl/concourse-fastly-resource

Example check: returns a new version whenever the active VCL version number
changes on the service.

Example get params (all optional):

  get: my-fastly-service
  params:
    fetch_vcl: generated    # "generated" | "custom" | "both" | false (default)
    vcl_dir: vcl            # subdirectory within destination_dir (default: "vcl")

Files always written to destination_dir:
  - ``service_version``  integer string of the active VCL version
  - ``updated_at``       ISO-8601 timestamp of when that version was last updated

When fetch_vcl is set:
  - ``vcl/generated.vcl``  compiled VCL Fastly actually executes (generated/both)
  - ``vcl/{name}.vcl``     one file per custom VCL file authored on the service
                            (custom/both)
  - ``vcl/main``           name of the VCL file marked as main (custom/both)

Example put params:

  put: my-fastly-service
  params:
    mode: purge_all           # "purge_all" (default) | "surrogate_key"
                              # | "surrogate_keys" | "url"

  # surrogate_key mode — purge a single tag:
  put: my-fastly-service
  params:
    mode: surrogate_key
    surrogate_key: html-pages
    soft: true

  # surrogate_keys mode — purge up to 256 tags in one request:
  put: my-fastly-service
  params:
    mode: surrogate_keys
    surrogate_keys:
      - html-pages
      - api-responses
    soft: true

  # url mode — purge a single cached URL:
  put: my-fastly-service
  params:
    mode: url
    url: https://www.example.com/path/to/page
    soft: true
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

import fastly
from concoursetools import BuildMetadata, ConcourseResource, TypedVersion
from fastly.api import purge_api, service_api, vcl_api, version_api

PurgeMode = Literal["purge_all", "surrogate_key", "surrogate_keys", "url"]
FetchVcl = Literal["generated", "custom", "both", False]

_VALID_FETCH_VCL: frozenset[object] = frozenset({"generated", "custom", "both", False})
_MAX_SURROGATE_KEYS = 256
_SERVICE_PAGE_SIZE = 100


@dataclass(unsafe_hash=True)
class FastlyVersion(TypedVersion):
    """Version representing the active VCL version number on a Fastly service."""

    service_version: str = "0"


class FastlyResource(ConcourseResource[FastlyVersion]):
    """Concourse resource for Fastly service version tracking and cache purging.

    Source configuration maps to ``__init__`` parameters.  The ``service_id``
    may be supplied here (for single-service pipelines) or overridden per step
    via params.
    """

    def __init__(
        self,
        /,
        api_token: str,
        service_id: str = "",
        domain: str = "",
    ) -> None:
        """Initialise the Fastly resource.

        Args:
            api_token: Fastly API token with at least ``purge_select`` scope for
                purge-only pipelines, or ``global:read`` scope if VCL fetching is used.
            service_id: Alphanumeric Fastly service ID.  Required for all
                operations except ``url``-mode purges; may also be supplied as a
                put param to override this default.  Takes precedence over
                *domain* when both are set.
            domain: Hostname served by the target Fastly service (e.g.
                ``"www.example.com"``).  Used to look up the service ID
                automatically when *service_id* is not supplied.  The lookup
                paginates ``GET /service`` and inspects ``GET /service/{id}/domain``
                until a match is found.  Ignored when *service_id* is set.
        """
        super().__init__(FastlyVersion)
        self.api_token = api_token
        self.service_id = service_id
        self.domain = domain

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _api_client(self) -> Generator[fastly.ApiClient]:
        """Yield an authenticated Fastly API client."""
        config = fastly.Configuration()
        config.api_token = self.api_token
        with fastly.ApiClient(config) as client:
            yield client

    def _active_version(
        self, client: fastly.ApiClient, service_id: str
    ) -> fastly.model.version_response.VersionResponse:
        """Return the currently active ``VersionResponse`` for *service_id*.

        Raises:
            RuntimeError: if no active version can be found.
        """
        versions = version_api.VersionApi(client).list_service_versions(service_id)
        active = [v for v in versions if v.active and v.number is not None]
        if not active:
            msg = f"No active version found for Fastly service {service_id!r}"
            raise RuntimeError(msg)
        # The list is ordered oldest-to-newest; return the highest number.
        return max(active, key=lambda v: v.number)

    def _lookup_service_id_by_domain(
        self, client: fastly.ApiClient, domain: str
    ) -> str:
        """Return the service ID of the Fastly service that serves *domain*.

        Paginates ``ServiceApi.list_services`` and calls
        ``ServiceApi.list_service_domains`` for each service until a domain
        entry whose ``name`` matches *domain* is found.

        Args:
            client: An authenticated Fastly API client.
            domain: Exact hostname to search for (e.g. ``"www.example.com"``).

        Returns:
            The alphanumeric Fastly service ID.

        Raises:
            ValueError: when no service in the account has *domain* configured.
        """
        svc_api = service_api.ServiceApi(client)
        page = 1
        while True:
            services = svc_api.list_services(per_page=100, page=page)
            if not services:
                break
            for svc in services:
                svc_id = svc.id
                if not svc_id:
                    continue
                for d in svc_api.list_service_domains(svc_id):
                    if d.name == domain:
                        return svc_id
            if len(services) < _SERVICE_PAGE_SIZE:
                break
            page += 1
        msg = f"No Fastly service found with domain {domain!r}"
        raise ValueError(msg)

    def _get_service_id(self, override: str | None, client: fastly.ApiClient) -> str:
        """Resolve the effective service ID.

        Priority: step-level *override* → source-level ``service_id`` →
        domain lookup via ``_lookup_service_id_by_domain``.

        Args:
            override: Step-level ``service_id`` param, or ``None``.
            client: An authenticated Fastly API client (required for domain
                lookup; unused when ``service_id`` is already known).

        Raises:
            ValueError: when no service ID can be determined.
        """
        resolved = override or self.service_id
        if resolved:
            return resolved
        if self.domain:
            return self._lookup_service_id_by_domain(client, self.domain)
        msg = (
            "One of service_id or domain must be set in the resource source "
            "configuration, or service_id must be supplied as a step param"
        )
        raise ValueError(msg)

    def _resolve_service_id(self, override: str | None) -> str:
        """Return *override* if provided, otherwise fall back to source service_id.

        Does not perform domain lookup.  Use :meth:`_get_service_id` when a
        ``fastly.ApiClient`` is already open.

        Raises:
            ValueError: when neither the source nor the override supplies an ID
                and no domain is configured.
        """
        resolved = override or self.service_id
        if not resolved and not self.domain:
            msg = (
                "One of service_id or domain must be set in the resource source "
                "configuration, or service_id must be supplied as a step param"
            )
            raise ValueError(msg)
        return resolved  # may be "" when domain lookup is deferred

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------

    def fetch_new_versions(
        self, previous_version: FastlyVersion | None = None
    ) -> list[FastlyVersion]:
        """Return a new version whenever the active VCL version number changes.

        If no previous version is known, returns only the current active version.
        If the active version number is greater than the previous, returns a
        single-element list with the new version so Concourse triggers exactly
        once per activation.
        """
        service_id = self._resolve_service_id(None)
        with self._api_client() as client:
            if not service_id:
                service_id = self._lookup_service_id_by_domain(client, self.domain)
            active = self._active_version(client, service_id)

        current = FastlyVersion(service_version=str(active.number))

        if previous_version is None:
            return [current]

        # Emit the current version whenever it differs from the previous one,
        # including rollbacks (re-activation of an older version), so the cursor
        # always reflects the actual active version and doesn't get stuck.
        if current.service_version != previous_version.service_version:
            return [current]

        return [previous_version]

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def download_version(  # noqa: PLR0913
        self,
        version: FastlyVersion,
        destination_dir: Path,
        build_metadata: BuildMetadata,
        fetch_vcl: FetchVcl = False,
        vcl_dir: str = "vcl",
        service_id: str | None = None,
    ) -> tuple[FastlyVersion, dict[str, str]]:
        """Write service metadata (and optionally VCL) to *destination_dir*.

        Args:
            version: The version being fetched.
            destination_dir: Output directory for this resource step.
            build_metadata: Concourse build context (unused but required by the
                interface).
            fetch_vcl: Controls which VCL content is downloaded.  One of
                ``"generated"``, ``"custom"``, ``"both"``, or ``False``
                (default — skip VCL fetching entirely).  Passing ``True`` is
                not valid and will raise ``ValueError``.
            vcl_dir: Relative subdirectory of *destination_dir* into which VCL
                files are written.  Must not be an absolute path or contain
                ``..`` components.  Defaults to ``"vcl"``.
            service_id: Override the source-level service ID for this step.
        """
        with self._api_client() as client:
            resolved_service_id = self._get_service_id(service_id, client)
            version_number = int(version.service_version)
            ver = version_api.VersionApi(client).get_service_version(
                resolved_service_id, version_number
            )

            updated_at = ver.updated_at.isoformat() if ver.updated_at else ""

            metadata: dict[str, str] = {
                "service_id": resolved_service_id,
                "service_version": str(version_number),
                "updated_at": updated_at,
            }

            # Always write the two plain-text metadata files.
            (destination_dir / "service_version").write_text(
                str(version_number), encoding="utf-8"
            )
            (destination_dir / "updated_at").write_text(updated_at, encoding="utf-8")

            # Optionally download VCL content.
            if fetch_vcl is True or (fetch_vcl and fetch_vcl not in _VALID_FETCH_VCL):
                msg = (
                    f"fetch_vcl must be one of 'generated', 'custom', 'both', "
                    f"or False; got {fetch_vcl!r}"
                )
                raise ValueError(msg)

            if fetch_vcl:
                _vcl_path = destination_dir.__class__(vcl_dir)
                if _vcl_path.is_absolute() or ".." in _vcl_path.parts:
                    msg = (
                        "vcl_dir must be a relative path with no '..' components; "
                        f"got {vcl_dir!r}"
                    )
                    raise ValueError(msg)
                vcl_root = destination_dir / vcl_dir
                vcl_root.mkdir(parents=True, exist_ok=True)
                vcl_client = vcl_api.VclApi(client)

                if fetch_vcl in ("generated", "both"):
                    result = vcl_client.get_custom_vcl_generated(
                        resolved_service_id, version_number
                    )
                    content = result.content or ""
                    (vcl_root / "generated.vcl").write_text(content, encoding="utf-8")
                    metadata["vcl_generated_bytes"] = str(len(content))

                if fetch_vcl in ("custom", "both"):
                    custom_files = vcl_client.list_custom_vcl(
                        resolved_service_id, version_number
                    )
                    main_name: str | None = None
                    written: list[str] = []
                    for vcl_file in custom_files:
                        name = vcl_file.name or ""
                        content = vcl_file.content or ""
                        if name:
                            _safe = _validate_vcl_name(name)
                            (vcl_root / f"{_safe}.vcl").write_text(
                                content, encoding="utf-8"
                            )
                            written.append(_safe)
                        if vcl_file.main:
                            main_name = name
                    if main_name:
                        (vcl_root / "main").write_text(main_name, encoding="utf-8")
                    metadata["vcl_custom_files"] = ", ".join(written)

        return version, metadata

    # ------------------------------------------------------------------
    # put
    # ------------------------------------------------------------------

    def publish_new_version(  # noqa: PLR0913
        self,
        sources_dir: Path,
        build_metadata: BuildMetadata,
        *,
        mode: PurgeMode = "purge_all",
        service_id: str | None = None,
        surrogate_key: str | None = None,
        surrogate_keys: list[str] | None = None,
        url: str | None = None,
        soft: bool = False,
    ) -> tuple[FastlyVersion, dict[str, str]]:
        """Perform a Fastly instant purge.

        Args:
            sources_dir: Job working directory (unused; required by the interface).
            build_metadata: Concourse build context (unused; required by the
                interface).
            mode: Which purge endpoint to call.  One of ``"purge_all"`` (default),
                ``"surrogate_key"``, ``"surrogate_keys"``, or ``"url"``.
            service_id: Override the source-level service ID for this step.
                Required for all modes except ``"url"``.
            surrogate_key: Single surrogate key to purge.  Required when
                ``mode="surrogate_key"``.
            surrogate_keys: List of surrogate keys to purge (up to 256).
                Required when ``mode="surrogate_keys"``.
            url: Absolute URL of the cached object to purge.  Required when
                ``mode="url"``.
            soft: When ``True``, issue a soft purge (marks objects stale rather
                than immediately inaccessible).  Not supported with
                ``mode="purge_all"``.

        Returns:
            A ``FastlyVersion`` pinned to ``"0"`` (purges do not advance the
            version cursor) and a metadata dict describing the operation.

        Raises:
            ValueError: on missing required params or invalid ``mode`` /
                ``soft`` combinations.
        """
        soft_header: int = 1 if soft else 0

        if soft and mode == "purge_all":
            msg = (
                "soft=true is not supported with mode='purge_all'. "
                "To soft-purge-all, tag all objects with a common surrogate key "
                "(e.g. 'all') and use mode='surrogate_key'."
            )
            raise ValueError(msg)

        purge_kind = "soft" if soft else "hard"
        metadata: dict[str, str] = {"mode": mode, "soft": str(soft).lower()}

        with self._api_client() as client:
            purge_client = purge_api.PurgeApi(client)

            match mode:
                case "purge_all":
                    resolved = self._get_service_id(service_id, client)
                    purge_client.purge_all(resolved)
                    metadata["service_id"] = resolved
                    metadata["purged"] = f"{purge_kind} purge-all on service {resolved}"

                case "surrogate_key":
                    if not surrogate_key:
                        msg = (
                            "surrogate_key param is required when mode='surrogate_key'"
                        )
                        raise ValueError(msg)
                    resolved = self._get_service_id(service_id, client)
                    purge_client.purge_tag(
                        resolved,
                        surrogate_key,
                        fastly_soft_purge=soft_header,
                    )
                    metadata["service_id"] = resolved
                    metadata["surrogate_key"] = surrogate_key
                    metadata["purged"] = (
                        f"{purge_kind} surrogate-key purge of '{surrogate_key}'"
                        f" on service {resolved}"
                    )

                case "surrogate_keys":
                    if not surrogate_keys:
                        msg = (
                            "surrogate_keys param is required "
                            "when mode='surrogate_keys'"
                        )
                        raise ValueError(msg)
                    if len(surrogate_keys) > _MAX_SURROGATE_KEYS:
                        msg = (
                            f"surrogate_keys must contain at most "
                            f"{_MAX_SURROGATE_KEYS} keys; got {len(surrogate_keys)}"
                        )
                        raise ValueError(msg)
                    resolved = self._get_service_id(service_id, client)
                    purge_client.bulk_purge_tag(
                        resolved,
                        surrogate_key=" ".join(surrogate_keys),
                        fastly_soft_purge=soft_header,
                    )
                    metadata["service_id"] = resolved
                    metadata["surrogate_keys"] = " ".join(surrogate_keys)
                    metadata["purged"] = (
                        f"{purge_kind} bulk surrogate-key purge of"
                        f" {len(surrogate_keys)} key(s) on service {resolved}"
                    )

                case "url":
                    if not url:
                        msg = "url param is required when mode='url'"
                        raise ValueError(msg)
                    purge_client.purge_single_url(url, fastly_soft_purge=soft_header)
                    metadata["url"] = url
                    metadata["purged"] = f"{purge_kind} URL purge of {url}"

                case _:
                    msg = f"Unknown purge mode: {mode!r}"
                    raise ValueError(msg)

        return FastlyVersion(service_version="0"), metadata


def _validate_vcl_name(name: str) -> str:
    """Return *name* if it is a safe VCL filename, otherwise raise ``ValueError``.

    Rejects names containing path separators (``/``, ``\\``) or that start with
    ``.`` to prevent directory traversal when the name is used to build a file
    path inside the VCL output directory.

    Args:
        name: The VCL filename returned by the Fastly API.

    Returns:
        The original *name* string, unchanged.

    Raises:
        ValueError: if *name* contains unsafe path components.
    """
    if "/" in name or "\\" in name or name.startswith("."):
        msg = (
            f"Unsafe VCL name returned by Fastly API: {name!r}. "
            "Names must not contain path separators or start with '.'"
        )
        raise ValueError(msg)
    return name


if __name__ == "__main__":
    FastlyResource.check_main()
