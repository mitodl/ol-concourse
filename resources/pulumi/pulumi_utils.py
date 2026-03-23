"""Pulumi Automation API operations for the Concourse Pulumi resource."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pulumi import automation as auto
from pulumi.automation import LocalWorkspaceOptions
from pulumi.automation.events import EngineEvent, OpType, ResourcePreEvent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_stack(
    stack_name: str,
    project_name: str,
    source_dir: str | Path,
    env_pulumi: dict[str, str],
    *,
    output_key: str | None = None,
) -> dict[str, Any]:
    """Select a stack and return its outputs.

    Returns a dict of all outputs, or a single-key dict when output_key is set.
    Raises StackNotFoundError (with a descriptive message) if the stack does not exist.
    """
    try:
        stack = auto.select_stack(
            stack_name=stack_name,
            project_name=project_name,
            work_dir=str(source_dir),
            opts=_workspace_opts(env_pulumi),
        )
    except auto.StackNotFoundError as exc:
        raise auto.StackNotFoundError(f"Stack '{stack_name}' not found") from exc

    outputs = stack.outputs()
    if output_key:
        return {output_key: outputs[output_key].value if output_key in outputs else None}
    return {k: v.value for k, v in outputs.items()}


def run_preview(
    stack_name: str,
    project_name: str,
    source_dir: str | Path,
    env_pulumi: dict[str, str],
    output_file: Path,
) -> dict:
    """Select a stack, run a preview, write JSON to output_file, and return the payload."""
    try:
        stack = auto.select_stack(
            stack_name=stack_name,
            project_name=project_name,
            work_dir=str(source_dir),
            opts=_workspace_opts(env_pulumi),
        )
    except auto.StackNotFoundError as exc:
        raise auto.StackNotFoundError(f"Stack '{stack_name}' not found") from exc

    return _run_preview_on_stack(stack, output_file)


def create_stack(
    stack_name: str,
    project_name: str,
    source_dir: str | Path,
    stack_config: dict[str, str],
    env_pulumi: dict[str, str],
    *,
    preview: bool = False,
    preview_file: Path | None = None,
) -> int:
    """Create a new stack and run pulumi up (or preview).

    Returns the Pulumi stack version number, or 0 for a preview run.
    Raises StackAlreadyExistsError if the stack already exists.
    """
    try:
        stack = auto.create_stack(
            stack_name=stack_name,
            project_name=project_name,
            work_dir=str(source_dir),
            opts=_workspace_opts(env_pulumi),
        )
    except auto.StackAlreadyExistsError as exc:
        raise auto.StackAlreadyExistsError(
            f"Stack '{stack_name}' already exists"
        ) from exc

    _apply_stack_config(stack, stack_config)

    if preview:
        _run_preview_on_stack(stack, preview_file)
        return 0

    result = stack.up(on_output=print)
    return result.summary.version


def update_stack(
    stack_name: str,
    project_name: str,
    source_dir: str | Path,
    stack_config: dict[str, str],
    env_pulumi: dict[str, str],
    *,
    refresh_stack: bool = True,
    preview: bool = False,
    preview_file: Path | None = None,
) -> int:
    """Select an existing stack, optionally refresh, then run pulumi up (or preview).

    Returns the Pulumi stack version number, or 0 for a preview run.
    Raises StackNotFoundError or ConcurrentUpdateError as appropriate.
    """
    try:
        stack = auto.select_stack(
            stack_name=stack_name,
            project_name=project_name,
            work_dir=str(source_dir),
            opts=_workspace_opts(env_pulumi),
        )
    except auto.StackNotFoundError as exc:
        raise auto.StackNotFoundError(f"Stack '{stack_name}' not found") from exc
    except auto.ConcurrentUpdateError as exc:
        raise auto.ConcurrentUpdateError(
            f"Stack '{stack_name}' already has an update in progress"
        ) from exc

    _apply_stack_config(stack, stack_config)

    if refresh_stack:
        stack.refresh(on_output=print)

    if preview:
        _run_preview_on_stack(stack, preview_file)
        return 0

    try:
        result = stack.up(on_output=print)
    except auto.ConcurrentUpdateError as exc:
        raise auto.ConcurrentUpdateError(
            f"Stack '{stack_name}' already has an update in progress"
        ) from exc

    return result.summary.version


def destroy_stack(
    stack_name: str,
    project_name: str,
    env_pulumi: dict[str, str],
    *,
    refresh_stack: bool = True,
) -> int:
    """Select a stack, run pulumi destroy, then remove it from the backend.

    Uses a no-op program so no source directory is required.
    Returns the Pulumi stack version number.
    Raises StackNotFoundError or ConcurrentUpdateError as appropriate.
    """
    try:
        stack = auto.select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=lambda *args: None,
            opts=_workspace_opts(env_pulumi),
        )
    except auto.StackNotFoundError as exc:
        raise auto.StackNotFoundError(f"Stack '{stack_name}' not found") from exc
    except auto.ConcurrentUpdateError as exc:
        raise auto.ConcurrentUpdateError(
            f"Stack '{stack_name}' already has an update in progress"
        ) from exc

    if refresh_stack:
        stack.refresh(on_output=print)

    try:
        result = stack.destroy(on_output=print)
    except auto.ConcurrentUpdateError as exc:
        raise auto.ConcurrentUpdateError(
            f"Stack '{stack_name}' already has an update in progress"
        ) from exc

    stack.workspace.remove_stack(stack_name)
    return result.summary.version


def serialize_resource_event(event: ResourcePreEvent) -> dict[str, Any]:
    """Convert a ResourcePreEvent into a JSON-serialisable dict."""
    meta = event.metadata
    detailed: dict[str, Any] = {}
    if meta.detailed_diff:
        detailed = {
            path: {
                "diff_kind": diff.diff_kind.value,
                "input_diff": diff.input_diff,
            }
            for path, diff in meta.detailed_diff.items()
        }
    return {
        "operation": meta.op.value,
        "urn": meta.urn,
        "type": meta.type,
        "diffs": meta.diffs or [],
        "detailed_diff": detailed,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _workspace_opts(env_pulumi: dict[str, str]) -> LocalWorkspaceOptions:
    return LocalWorkspaceOptions(env_vars=env_pulumi)


def _apply_stack_config(stack: auto.Stack, config: dict[str, str]) -> None:
    for key, value in config.items():
        stack.set_config(key, auto.ConfigValue(value=str(value)))


def _run_preview_on_stack(stack: auto.Stack, output_file: Path | None) -> dict:
    """Run pulumi preview on an already-selected stack.

    Writes structured JSON to output_file (if given) and returns the payload.
    """
    resource_events: list[ResourcePreEvent] = []

    def on_event(event: EngineEvent) -> None:
        if event.resource_pre_event and event.resource_pre_event.metadata:
            resource_events.append(event.resource_pre_event)

    result = stack.preview(
        diff=True,
        on_output=print,
        on_event=on_event,
    )

    changes = [
        serialize_resource_event(evt)
        for evt in resource_events
        if evt.metadata and evt.metadata.op != OpType.SAME
    ]

    payload = {
        "change_summary": {
            k.value: v for k, v in (result.change_summary or {}).items()
        },
        "changes": changes,
        "stdout": result.stdout,
    }

    if output_file is not None:
        output_file.write_text(json.dumps(payload, indent=2))

    return payload
