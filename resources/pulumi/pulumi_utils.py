"""Pulumi Automation API operations for the Concourse Pulumi resource."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from pulumi import automation as auto
from pulumi.automation import LocalWorkspaceOptions
from pulumi.automation.events import EngineEvent, OpType, ResourcePreEvent

# Matches Concourse worker container hostnames, which are random UUIDs.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# Parses "created by <user>@<host> (pid <N>) at <timestamp>" from lock error messages.
_LOCK_HOLDER_RE = re.compile(
    r"created by (?P<user>[^@]+)@(?P<host>\S+) \(pid (?P<pid>\d+)\) at (?P<at>\S+)"
)


@dataclass
class StackUpdate:
    """The outcome of a ``pulumi up``, as much of it as is worth carrying forward.

    ``pulumi up`` already prints all of this to the build log, but the log is not
    something a later step can read.  A promotion-gate issue is decided by a human
    who is not reading the log, so the counts have to travel out of here as data.
    """

    version: int
    result: str
    resource_changes: dict[str, int]
    duration_seconds: int | None = None

    def to_flat_dict(self) -> dict[str, str]:
        """Flatten to strings, for a Concourse version or metadata payload."""
        flat = {
            "version": str(self.version),
            "result": self.result,
            "resource_changes": json.dumps(self.resource_changes, sort_keys=True),
        }
        if self.duration_seconds is not None:
            flat["duration_seconds"] = str(self.duration_seconds)
        return flat


def summarize_up_result(result: auto.UpResult) -> StackUpdate:
    """Extract the resource summary from an UpResult.

    ``resource_changes`` is keyed by Pulumi's own op names (``create``, ``update``,
    ``delete``, ``replace``, ``same``).  Pulumi omits keys with a zero count rather
    than reporting them as 0, so absence means none of that op happened.
    """
    summary = result.summary
    duration: int | None = None
    # UpdateSummary.start_time/end_time are datetimes, not the RFC 3339 strings
    # the wire format uses -- the automation API parses them before we see them.
    # end_time is Optional; an update still in progress has none.
    if summary.start_time and summary.end_time:
        duration = int((summary.end_time - summary.start_time).total_seconds())
    return StackUpdate(
        version=summary.version,
        result=summary.result,
        resource_changes={
            k: int(v) for k, v in (summary.resource_changes or {}).items()
        },
        duration_seconds=duration,
    )


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
        return {
            output_key: outputs[output_key].value if output_key in outputs else None
        }
    return {k: v.value for k, v in outputs.items()}


def run_preview(
    stack_name: str,
    project_name: str,
    source_dir: str | Path,
    env_pulumi: dict[str, str],
    output_file: Path,
) -> dict[str, Any]:
    """Select a stack, run a preview, write JSON to output_file, and return it."""
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


def create_stack(  # noqa: PLR0913
    stack_name: str,
    project_name: str,
    source_dir: str | Path,
    stack_config: dict[str, str],
    env_pulumi: dict[str, str],
    *,
    preview: bool = False,
    preview_file: Path | None = None,
) -> StackUpdate:
    """Create a new stack and run pulumi up (or preview).

    Returns the update's StackUpdate summary, or an empty one for a preview run.
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
        return _preview_stack_update()

    result = _with_lock_recovery(lambda: stack.up(on_output=print), stack, stack_name)
    return summarize_up_result(result)


def _preview_stack_update() -> StackUpdate:
    """Return the StackUpdate for a preview run, which applies nothing."""
    return StackUpdate(version=0, result="preview", resource_changes={})


def update_stack(  # noqa: PLR0913
    stack_name: str,
    project_name: str,
    source_dir: str | Path,
    stack_config: dict[str, str],
    env_pulumi: dict[str, str],
    *,
    refresh_stack: bool = True,
    preview: bool = False,
    preview_file: Path | None = None,
) -> StackUpdate:
    """Select an existing stack, optionally refresh, then run pulumi up (or preview).

    Returns the update's StackUpdate summary, or an empty one for a preview run.
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
        _with_lock_recovery(lambda: stack.refresh(on_output=print), stack, stack_name)

    if preview:
        _run_preview_on_stack(stack, preview_file)
        return _preview_stack_update()

    result = _with_lock_recovery(lambda: stack.up(on_output=print), stack, stack_name)
    return summarize_up_result(result)


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
        _with_lock_recovery(lambda: stack.refresh(on_output=print), stack, stack_name)

    result = _with_lock_recovery(
        lambda: stack.destroy(on_output=print), stack, stack_name
    )
    stack.workspace.remove_stack(stack_name)
    return result.summary.version


def cancel_stack_lock(
    stack_name: str,
    project_name: str,
    env_pulumi: dict[str, str],
) -> None:
    """Unconditionally cancel any pending operation on the named stack.

    Uses an inline no-op program so no source directory is required.
    Intended for explicit pipeline ``ensure``/``on_abort`` cancel steps.
    """
    opts = _workspace_opts(env_pulumi)
    opts.project_settings = auto.ProjectSettings(name=project_name, runtime="python")
    stack = auto.select_stack(
        stack_name=stack_name,
        program=lambda *_args: None,
        opts=opts,
    )
    stack.cancel()


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


def _parse_lock_holders(exc: auto.ConcurrentUpdateError) -> list[dict[str, str]]:
    """Extract lock holder records from a ConcurrentUpdateError message."""
    return [m.groupdict() for m in _LOCK_HOLDER_RE.finditer(str(exc))]


_LOCK_MIN_AGE_MINUTES = 15


def _is_recoverable_lock(
    holders: list[dict[str, str]],
    *,
    min_age_minutes: int = _LOCK_MIN_AGE_MINUTES,
) -> bool:
    """Return True only when every lock holder is a stale Concourse worker lock.

    Two conditions must both hold for every holder:
    - ``user == "root"`` and hostname matches the UUID pattern used by Concourse
      worker containers (developer machines have human-readable hostnames).
    - The lock is older than ``min_age_minutes`` (default 15 min) to avoid
      cancelling a legitimately running deployment on another Concourse worker
      that also happens to have a UUID hostname.
    """
    if not holders:
        return False
    now = datetime.now(UTC)
    for h in holders:
        if h["user"] != "root" or not _UUID_RE.match(h["host"]):
            return False
        if min_age_minutes > 0:
            try:
                lock_time = datetime.fromisoformat(h["at"].replace("Z", "+00:00"))
                age_minutes = (now - lock_time).total_seconds() / 60
                if age_minutes < min_age_minutes:
                    return False
            except ValueError:
                return False
    return True


def _with_lock_recovery(
    operation: Callable[[], Any], stack: auto.Stack, stack_name: str
) -> Any:
    """Run *operation*, recovering automatically from a stale Concourse lock.

    If ConcurrentUpdateError is raised and every lock holder looks like a
    Concourse worker container (UUID hostname, root user), cancel the lock and
    retry once.  Any other lock holder pattern is left to the caller.
    """
    try:
        return operation()
    except auto.ConcurrentUpdateError as exc:
        holders = _parse_lock_holders(exc)
        if not _is_recoverable_lock(holders):
            raise
        sys.stderr.write(
            f"Stale Concourse lock detected for '{stack_name}', cancelling...\n"
        )
        for h in holders:
            sys.stderr.write(
                f"  Was held by {h['user']}@{h['host']}"
                f" (pid {h['pid']}) since {h['at']}\n"
            )
        try:
            stack.cancel()
        except Exception as cancel_exc:
            raise auto.ConcurrentUpdateError(
                f"Lock recovery failed for '{stack_name}': {cancel_exc}"
            ) from exc
        return operation()


def _run_preview_on_stack(
    stack: auto.Stack, output_file: Path | None
) -> dict[str, Any]:
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
