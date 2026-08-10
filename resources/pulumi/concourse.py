"""Concourse resource type for managing Pulumi stack deployments."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from concoursetools import BuildMetadata, ConcourseResource, TypedVersion

import io_utils
import pulumi_utils


@dataclass
class PulumiVersion(TypedVersion):
    """Static version — Pulumi stacks are not polled for external changes.

    ``summary`` is the JSON-encoded ``StackUpdate`` produced by the put that
    emitted this version, and is empty for versions from ``check``.  It rides on
    the version rather than on metadata because metadata is only ever shown in
    the Concourse UI, and the point of carrying it is to hand it to a *later
    step* -- the implicit get writes it to a file that the promotion-gate issue
    put reads as its body.
    """

    id: str = "0"
    summary: str = ""


class PulumiResource(ConcourseResource[PulumiVersion]):
    """Concourse resource for running Pulumi stack operations.

    Source configuration maps to __init__ parameters. All fields are
    optional here and can be overridden per-step via params.
    """

    def __init__(  # noqa: PLR0913
        self,
        stack_name: str = "",
        project_name: str = "",
        source_dir: str = ".",
        env_pulumi: dict[str, str] | None = None,
        env_os: dict[str, str] | None = None,
        action: str
        | None = None,  # accepted for backwards compatibility; use put params instead
        max_carried_changes: int | str = pulumi_utils.DEFAULT_MAX_CARRIED_CHANGES,
    ) -> None:
        super().__init__(PulumiVersion)
        self.stack_name = stack_name
        self.project_name = project_name
        self.source_dir = source_dir
        self.env_pulumi: dict[str, str] = env_pulumi or {}
        self.env_os: dict[str, str] = env_os or {}
        self.action = action
        # How many per-resource changes ride on the emitted version. Settable in
        # the pipeline's resource `source` (or overridden per-put), so tuning it
        # is a pipeline re-set rather than a release of this image. 0 = no cap.
        self.max_carried_changes = int(max_carried_changes)

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------

    def fetch_new_versions(
        self, previous_version: PulumiVersion | None
    ) -> list[PulumiVersion]:
        """Return a static version. Triggering is handled by git resource changes."""
        return [PulumiVersion(id="0")]

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def download_version(  # noqa: PLR0913
        self,
        version: PulumiVersion,
        destination_dir: Path,
        build_metadata: BuildMetadata,
        *,
        output_key: str | None = None,
        run_preview: bool = False,
        stack_name: str | None = None,
        project_name: str | None = None,
        source_dir: str | None = None,
        env_pulumi: dict[str, str] | None = None,
        env_os: dict[str, str] | None = None,
        summary_file: str | None = None,
        read_outputs: bool = True,
    ) -> tuple[PulumiVersion, dict[str, str]]:
        """Read stack outputs and optionally run a preview.

        destination_dir is this resource's own output directory; its parent
        is the job working directory that contains all fetched inputs.

        *summary_file* writes the deploy summary carried on *version* to that
        filename inside destination_dir.  This is how the result of a put escapes
        the put: a put step produces no artifacts, but its implicit get does, so
        a pipeline sets ``no_get: false`` plus ``get_params: {summary_file: ...}``
        and a later step -- the promotion-gate issue put -- reads the file.

        *read_outputs* is True for a normal get, which exists to fetch stack
        outputs.  Set it False on that implicit get: re-reading the stack there
        would mean a second Pulumi invocation on the success path of every
        deploy, and a failure in it would redden a deploy that actually worked.
        """
        if summary_file:
            (destination_dir / summary_file).write_text(
                _render_summary(version, build_metadata)
            )

        metadata: dict[str, str] = {}
        if summary_file:
            metadata["summary_file"] = str(destination_dir / summary_file)

        if not read_outputs:
            return version, metadata

        effective = self._resolve_params(
            stack_name=stack_name,
            project_name=project_name,
            source_dir=source_dir,
            env_pulumi=env_pulumi,
            env_os=env_os,
        )

        _apply_os_env(effective["env_os"])

        # job working dir is the parent of this resource's destination dir
        work_dir = destination_dir.parent / effective["source_dir"]

        outputs = pulumi_utils.read_stack(
            stack_name=effective["stack_name"],
            project_name=effective["project_name"],
            source_dir=work_dir,
            env_pulumi=effective["env_pulumi"],
            output_key=output_key,
        )

        outputs_file = destination_dir / f"{effective['stack_name']}_outputs.json"
        outputs_file.write_text(json.dumps(outputs, indent=2))

        metadata["outputs_file"] = str(outputs_file)

        if run_preview:
            preview_file = destination_dir / f"{effective['stack_name']}_preview.json"
            pulumi_utils.run_preview(
                stack_name=effective["stack_name"],
                project_name=effective["project_name"],
                source_dir=work_dir,
                env_pulumi=effective["env_pulumi"],
                output_file=preview_file,
            )
            metadata["preview_file"] = str(preview_file)

        return version, metadata

    # ------------------------------------------------------------------
    # put
    # ------------------------------------------------------------------

    def publish_new_version(  # noqa: PLR0912, PLR0913
        self,
        sources_dir: Path,
        build_metadata: BuildMetadata,
        *,
        action: str | None = None,
        stack_name: str | None = None,
        project_name: str | None = None,
        source_dir: str | None = None,
        stack_config: dict[str, str] | None = None,
        preview: bool = False,
        refresh_stack: bool = True,
        env_pulumi: dict[str, str] | None = None,
        env_os: dict[str, str] | None = None,
        env_vars_from_files: dict[str, str] | None = None,
        max_carried_changes: int | str | None = None,
    ) -> tuple[PulumiVersion, dict[str, str]]:
        """Execute a Pulumi action against a stack.

        sources_dir is the job working directory containing all fetched inputs.
        """
        effective_action = action or self.action
        if effective_action not in ("cancel", "create", "update", "destroy"):
            raise ValueError(
                f"Invalid action '{effective_action}'."
                " Must be one of: cancel, create, update, destroy"
            )

        if effective_action == "cancel":
            effective = self._resolve_params(
                stack_name=stack_name,
                project_name=project_name,
                source_dir=source_dir,
                env_pulumi=env_pulumi,
                env_os=env_os,
            )
            if env_vars_from_files:
                for var_name, file_path in env_vars_from_files.items():
                    os.environ[var_name] = io_utils.read_value_from_file(
                        file_path, working_dir=str(sources_dir)
                    )
            _apply_os_env(effective["env_os"])
            pulumi_utils.cancel_stack_lock(
                stack_name=effective["stack_name"],
                project_name=effective["project_name"],
                env_pulumi=effective["env_pulumi"],
            )
            return PulumiVersion(id="0"), {
                "action": "cancel",
                "stack": effective["stack_name"],
                "result": "cancelled",
            }

        effective = self._resolve_params(
            stack_name=stack_name,
            project_name=project_name,
            source_dir=source_dir,
            env_pulumi=env_pulumi,
            env_os=env_os,
            max_carried_changes=max_carried_changes,
        )

        if env_vars_from_files:
            for var_name, file_path in env_vars_from_files.items():
                os.environ[var_name] = io_utils.read_value_from_file(
                    file_path, working_dir=str(sources_dir)
                )

        _apply_os_env(effective["env_os"])

        work_dir = sources_dir / effective["source_dir"]
        cfg = stack_config or {}

        metadata: dict[str, str] = {
            "action": effective_action,
            "stack": effective["stack_name"],
        }

        stack_update: pulumi_utils.StackUpdate | None = None

        if effective_action == "destroy":
            version_id = pulumi_utils.destroy_stack(
                stack_name=effective["stack_name"],
                project_name=effective["project_name"],
                env_pulumi=effective["env_pulumi"],
                refresh_stack=refresh_stack,
            )
            metadata["result"] = "succeeded"

        elif preview:
            preview_file = work_dir / f"{effective['stack_name']}_preview.json"
            if effective_action == "create":
                pulumi_utils.create_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                    preview=True,
                    preview_file=preview_file,
                )
            else:
                pulumi_utils.update_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                    refresh_stack=refresh_stack,
                    preview=True,
                    preview_file=preview_file,
                )
            version_id = 0
            preview_data = json.loads(preview_file.read_text())
            metadata["preview_file"] = str(preview_file)
            metadata["changes"] = json.dumps(preview_data.get("change_summary", {}))

        else:
            if effective_action == "create":
                stack_update = pulumi_utils.create_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                    max_carried_changes=effective["max_carried_changes"],
                )
            else:
                stack_update = pulumi_utils.update_stack(
                    stack_name=effective["stack_name"],
                    project_name=effective["project_name"],
                    source_dir=work_dir,
                    stack_config=cfg,
                    env_pulumi=effective["env_pulumi"],
                    refresh_stack=refresh_stack,
                    max_carried_changes=effective["max_carried_changes"],
                )
            version_id = stack_update.version
            # Pulumi's own summary.result carries the same succeeded/failed
            # semantics, so let it be the authority rather than asserting it here.
            metadata.update(stack_update.to_flat_dict())

        summary_json = json.dumps(stack_update.to_flat_dict()) if stack_update else ""
        return PulumiVersion(id=str(version_id), summary=summary_json), metadata

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_params(  # noqa: PLR0913
        self,
        stack_name: str | None,
        project_name: str | None,
        source_dir: str | None,
        env_pulumi: dict[str, str] | None,
        env_os: dict[str, str] | None,
        max_carried_changes: int | str | None = None,
    ) -> dict[str, Any]:
        """Merge step-level overrides onto source-level defaults."""
        merged_env_pulumi = {**self.env_pulumi, **(env_pulumi or {})}
        merged_env_os = {**self.env_os, **(env_os or {})}
        return {
            "stack_name": stack_name or self.stack_name,
            "project_name": project_name or self.project_name,
            "source_dir": source_dir or self.source_dir,
            "env_pulumi": merged_env_pulumi,
            "env_os": merged_env_os,
            # `or` would swallow an explicit 0, which is how a caller asks for
            # no cap at all.
            "max_carried_changes": (
                self.max_carried_changes
                if max_carried_changes is None
                else int(max_carried_changes)
            ),
        }


def _apply_os_env(env_vars: dict[str, str]) -> None:
    for key, value in env_vars.items():
        os.environ[key] = value


# Ops worth calling out individually; anything else Pulumi reports falls through
# to the catch-all loop below so a new op type is never silently dropped.
_NOTABLE_OPS = ("create", "update", "replace", "delete")


def _render_summary(version: PulumiVersion, build_metadata: BuildMetadata) -> str:
    """Render the deploy summary carried on *version* as Markdown.

    This ends up as the body of the `[bot] Pulumi <project> <stack> deployed.`
    issue, and closing that issue promotes the change to the next environment.
    So the reader is a human deciding whether to promote, and the two things
    they need are what changed and whether anything failed.

    The no-summary branch matters as much as the normal one.  A green Pulumi job
    that carries no summary is the shape of ol-infrastructure's
    deploy-ol-substructure-keycloak build 158, where a retried put reported
    success having run no Pulumi at all.  An issue body that just omitted the
    counts would read as "nothing to report"; it has to read as "do not trust
    this".
    """
    build_link = f"[build {build_metadata.BUILD_NAME}]({build_metadata.build_url()})"

    if not version.summary:
        return (
            "## :warning: No Pulumi resource summary was recorded\n\n"
            f"This deploy was reported as succeeding by {build_link}, but the job "
            "produced no Pulumi run summary.\n\n"
            "**Do not close this issue to promote the change until you have "
            "confirmed from the build log that Pulumi actually ran.** A job that "
            "reports success while emitting no summary has not been shown to have "
            "deployed anything -- check the log for `Updating`, `Resources:` and "
            "`Duration:` lines before treating this as a real deploy.\n"
        )

    summary = json.loads(version.summary)
    changes: dict[str, int] = json.loads(summary.get("resource_changes", "{}"))

    lines = [
        "## Pulumi resource summary",
        "",
        f"- **Result:** `{summary.get('result', 'unknown')}`",
        f"- **Stack version:** `{summary.get('version', 'unknown')}`",
    ]
    if "duration_seconds" in summary:
        lines.append(f"- **Duration:** {summary['duration_seconds']}s")
    lines.extend(["", "| Change | Count |", "| --- | --- |"])

    # Pulumi omits zero-count ops entirely, so report the notable ones as 0
    # rather than leaving the reader to wonder whether the key was dropped or
    # the op genuinely did not happen.
    for op in _NOTABLE_OPS:
        lines.append(f"| {op} | {changes.get(op, 0)} |")
    for op in sorted(set(changes) - set(_NOTABLE_OPS)):
        lines.append(f"| {op} | {changes[op]} |")

    errored = changes.get("errored", 0)
    if errored or summary.get("result") not in ("succeeded", "preview"):
        lines.extend(
            [
                "",
                f":rotating_light: **This update did not complete cleanly "
                f"(`result={summary.get('result')}`, errored={errored}).** "
                "Do not promote it.",
            ]
        )

    lines.extend(
        _render_changes(
            json.loads(summary.get("changes", "[]")),
            int(summary.get("changes_total", 0)),
        )
    )
    lines.extend(["", f"Deployed by {build_link}.", ""])
    return "\n".join(lines)


def _resource_name(urn: str) -> str:
    """Pull the resource name off a URN.

    ``urn:pulumi:<stack>::<project>::<type-chain>::<name>`` -- the name is the
    last ``::`` segment. Parent types are packed into the type chain with ``$``,
    so the resource's own type is the last ``$`` segment of the second-to-last.
    """
    return urn.rsplit("::", 1)[-1] if urn else "?"


def _resource_type(event: dict[str, Any]) -> str:
    """Return the resource's own Pulumi type, e.g. ``keycloak:openid/client:Client``."""
    declared = event.get("type")
    if declared:
        return str(declared)
    parts = str(event.get("urn", "")).split("::")
    return parts[-2].split("$")[-1] if len(parts) > 2 else "?"  # noqa: PLR2004


def _render_changes(events: list[dict[str, Any]], total: int = 0) -> list[str]:
    """Render the per-resource diff -- *what* changed, not just how many.

    The counts above answer "did anything change"; a human deciding whether to
    promote needs "what changed, and does it look like what I intended". A
    Keycloak client losing a redirect URI and a Keycloak client gaining a
    description are both `update: 1`, and only one of them should be promoted
    without a second look.

    Property paths come from ``detailed_diff`` where the provider supplies one,
    falling back to the coarser ``diffs`` list. Both can be empty (a create or
    delete has no property-level diff), in which case just the resource is
    named.
    """
    if not events:
        return []

    by_op: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_op.setdefault(str(event.get("operation", "?")), []).append(event)

    lines = ["", "### What changed", ""]
    # Ordered so the destructive ops a reviewer most needs to see come first,
    # then anything Pulumi reports that this list does not anticipate.
    ordered = [op for op in ("delete", "replace", "create", "update") if op in by_op]
    ordered += sorted(set(by_op) - set(ordered))

    for op in ordered:
        entries = by_op[op]
        lines.append(f"<details open><summary><b>{op}</b> ({len(entries)})</summary>")
        lines.append("")
        for event in sorted(entries, key=lambda e: str(e.get("urn", ""))):
            name = _resource_name(str(event.get("urn", "")))
            lines.append(f"- `{name}` — `{_resource_type(event)}`")
            for prop in _changed_properties(event):
                lines.append(f"  - {prop}")
        lines.extend(["", "</details>", ""])

    if total > len(events):
        # Never truncate silently -- a shortened list that reads as complete is
        # exactly the kind of thing this whole feature exists to prevent. The
        # cap is applied upstream, when the changes are put on the version; this
        # only reports it.
        lines.append(
            f"> :warning: Showing {len(events)} of {total} changed resources. "
            "See the build log for the full diff."
        )
    return lines


def _changed_properties(event: dict[str, Any]) -> list[str]:
    """Return the changed property paths, preferring the provider's detailed diff."""
    detailed = event.get("detailed_diff") or {}
    if detailed:
        return [
            f"`{path}` ({info.get('diff_kind', 'changed')})"
            for path, info in sorted(detailed.items())
        ]
    return [f"`{prop}`" for prop in sorted(event.get("diffs") or [])]
