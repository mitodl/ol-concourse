"""Pulumi Automation API operations for the Concourse Pulumi resource."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
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

# The per-resource change list rides on the Concourse *version*, which Concourse
# persists per-resource in its database and carries through every later step. A
# thousand-resource refactor would otherwise put a megabyte there. Cap what is
# carried; ``changes_total`` keeps the honest count so the rendered body can say
# how much it is not showing.
#
# This is only the default. The cap is a pipeline-level setting -- the resource's
# ``max_carried_changes`` source field or put param -- so raising or disabling it
# is a pipeline re-set, not a code change plus a package release plus a
# dependency bump.
DEFAULT_MAX_CARRIED_CHANGES = 200


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
    changes: list[dict[str, Any]] = field(default_factory=list)
    changes_total: int = 0
    error: str = ""

    def to_flat_dict(self) -> dict[str, str]:
        """Flatten to strings, for a Concourse version or metadata payload."""
        flat = {
            "version": str(self.version),
            "result": self.result,
            "resource_changes": json.dumps(self.resource_changes, sort_keys=True),
        }
        if self.duration_seconds is not None:
            flat["duration_seconds"] = str(self.duration_seconds)
        if self.changes:
            flat["changes"] = json.dumps(self.changes)
            flat["changes_total"] = str(self.changes_total)
        if self.error:
            flat["error"] = self.error
        return flat


def summarize_up_result(
    result: auto.UpResult,
    changes: list[ResourcePreEvent] | None = None,
    max_carried_changes: int = DEFAULT_MAX_CARRIED_CHANGES,
) -> StackUpdate:
    """Extract the resource summary from an UpResult.

    ``resource_changes`` is keyed by Pulumi's own op names (``create``, ``update``,
    ``delete``, ``replace``, ``same``).  Pulumi omits keys with a zero count rather
    than reporting them as 0, so absence means none of that op happened.

    *changes* is the ResourcePreEvent stream collected during the update.  The
    counts alone say a deploy touched five resources; only these say *which*
    five and *what* about them changed -- which is what a human deciding whether
    to promote actually needs.  ``UpResult`` does not carry them, so they have
    to be captured via ``on_event`` while ``up`` runs.

    *max_carried_changes* bounds how many of those events ride on the version.
    ``0`` disables the cap entirely.  ``changes_total`` always records the true
    count, so a capped list can say how much it is not showing rather than
    reading as complete.
    """
    summary = result.summary
    duration: int | None = None
    # UpdateSummary.start_time/end_time are datetimes, not the RFC 3339 strings
    # the wire format uses -- the automation API parses them before we see them.
    # end_time is Optional; an update still in progress has none.
    if summary.start_time and summary.end_time:
        duration = int((summary.end_time - summary.start_time).total_seconds())
    changed = [
        serialize_resource_event(evt)
        for evt in (changes or [])
        if evt.metadata and evt.metadata.op != OpType.SAME
    ]
    return StackUpdate(
        version=summary.version,
        result=summary.result,
        # Same OpMap-annotated-but-actually-strings field as the preview path.
        resource_changes=_op_counts(summary.resource_changes),
        duration_seconds=duration,
        changes=changed if max_carried_changes == 0 else changed[:max_carried_changes],
        changes_total=len(changed),
    )


def _collect_resource_events() -> tuple[list[ResourcePreEvent], Callable[..., None]]:
    """Return an event accumulator and the ``on_event`` callback that fills it.

    Mirrors what ``_run_preview_on_stack`` does for previews, so an update
    reports its per-resource detail the same way a preview already does.
    """
    events: list[ResourcePreEvent] = []

    def on_event(event: EngineEvent) -> None:
        if event.resource_pre_event and event.resource_pre_event.metadata:
            events.append(event.resource_pre_event)

    return events, on_event


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
    max_carried_changes: int = DEFAULT_MAX_CARRIED_CHANGES,
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
        payload = _run_preview_on_stack(stack, preview_file)
        return _preview_stack_update(payload, max_carried_changes)

    events, on_event = _collect_resource_events()
    result = _with_lock_recovery(
        lambda: stack.up(on_output=print, on_event=on_event), stack, stack_name
    )
    return summarize_up_result(result, events, max_carried_changes)


def _preview_stack_update(
    payload: dict[str, Any] | None = None,
    max_carried_changes: int = DEFAULT_MAX_CARRIED_CHANGES,
) -> StackUpdate:
    """Return the StackUpdate for a preview run, which applies nothing.

    A preview emits the same per-resource shape an update does, so it carries
    its changes on the version the same way -- that is what lets a promotion
    gate show "what applying this to the next environment would do" beside
    "what this deploy actually did".  ``version`` stays 0 because a preview
    creates no stack version.
    """
    payload = payload or {}
    changed: list[dict[str, Any]] = list(payload.get("changes") or [])
    return StackUpdate(
        version=0,
        result="preview",
        resource_changes={
            str(k): int(v) for k, v in (payload.get("change_summary") or {}).items()
        },
        changes=changed if max_carried_changes == 0 else changed[:max_carried_changes],
        changes_total=len(changed),
    )


def preview_failed(error: str) -> StackUpdate:
    """Return a StackUpdate standing in for a preview that could not run.

    Carried on the version so the reviewer is told the preview is missing.
    Silence would read as "nothing to see", which is the opposite of the truth.
    """
    return StackUpdate(
        version=0,
        result="preview-failed",
        resource_changes={},
        error=error,
    )


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
    max_carried_changes: int = DEFAULT_MAX_CARRIED_CHANGES,
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
        payload = _run_preview_on_stack(stack, preview_file)
        return _preview_stack_update(payload, max_carried_changes)

    events, on_event = _collect_resource_events()
    result = _with_lock_recovery(
        lambda: stack.up(on_output=print, on_event=on_event), stack, stack_name
    )
    return summarize_up_result(result, events, max_carried_changes)


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


def _op_name(op: Any) -> str:
    """Return an op's wire name whether the SDK handed us an enum or a string.

    ``SummaryEvent.resource_changes`` is annotated ``OpMap``
    (``Mapping[OpType, int]``) but its keys arrive as PLAIN STRINGS -- the
    automation API does not coerce them when deserializing the event log, unlike
    ``StepEventMetadata.op`` and ``PropertyDiff.diff_kind``, which really are
    parsed into enums. Assuming the annotation crashed a live gate preview with
    ``'str' object has no attribute 'value'``.
    """
    return op.value if hasattr(op, "value") else str(op)


def _op_counts(change_summary: Any) -> dict[str, int]:
    """Normalise a change summary to plain ``{op_name: count}``."""
    return {_op_name(k): int(v) for k, v in (change_summary or {}).items()}


# Longest value rendered for a single changed property. A Pulumi input can be a
# whole IAM policy document or a rendered config file; the point of showing
# values is to make a change scannable, which a 40KB blob is not -- and each
# property prints TWO of them, old and new. Sized to comfortably fit the cases
# that actually reward reading (versions, counts, ARNs, URLs, booleans) without
# letting one policy document swamp the list.
MAX_DIFF_VALUE_CHARS = 120

# What Pulumi substitutes for a value it has filtered. Matched so it renders as
# an explicit redaction rather than a confusing literal.
_SECRET_MARKERS = frozenset({"[secret]", "[unknown]"})

_MISSING = object()


def _parse_property_path(path: str) -> list[str | int]:
    """Split a Pulumi property path into segments.

    Pulumi's syntax is `root.nested["quoted key"][0]`, and a key is quoted
    precisely when it contains characters that would otherwise be structural --
    dots and slashes above all. Kubernetes labels are the everyday case:
    `labels["app.kubernetes.io/name"]`. Naively stripping brackets and splitting
    on dots shreds that into four meaningless fragments, so the value silently
    disappears from the diff -- which is worse than an error, because the gate
    still renders and just quietly says less than it should.

    Numeric bracket segments stay list indexes; quoted ones are single mapping
    keys, with `\"` and `\\` unescaped.
    """
    segments: list[str | int] = []
    buffer: list[str] = []
    index = 0
    length = len(path)

    def flush() -> None:
        if buffer:
            segments.append("".join(buffer))
            buffer.clear()

    while index < length:
        char = path[index]
        if char == ".":
            flush()
            index += 1
        elif char == "[":
            flush()
            index += 1
            if index < length and path[index] == '"':
                index += 1
                key: list[str] = []
                while index < length and path[index] != '"':
                    if path[index] == "\\" and index + 1 < length:
                        index += 1
                    key.append(path[index])
                    index += 1
                index += 1  # closing quote
                segments.append("".join(key))
            else:
                digits: list[str] = []
                while index < length and path[index] != "]":
                    digits.append(path[index])
                    index += 1
                raw = "".join(digits)
                segments.append(int(raw) if raw.isdigit() else raw)
            while index < length and path[index] != "]":
                index += 1
            index += 1  # closing bracket
        else:
            buffer.append(char)
            index += 1
    flush()
    return segments


def _resolve_property_path(root: Any, path: str) -> Any:
    """Resolve a Pulumi detailed-diff path against an input tree.

    Returns ``_MISSING`` rather than raising when the path does not exist, which
    is normal: an ``add`` has no old value and a ``delete`` has no new one.
    """
    current = root
    for segment in _parse_property_path(path):
        if isinstance(segment, int):
            if not isinstance(current, (list, tuple)) or segment >= len(current):
                return _MISSING
            current = current[segment]
        elif isinstance(current, dict):
            if segment not in current:
                return _MISSING
            current = current[segment]
        else:
            return _MISSING
    return current


def _render_value(value: Any) -> str | None:
    """Render one property value for display, redacted and truncated.

    ``None`` means "nothing to show" and is distinct from the JSON value null,
    which renders as ``"null"``.
    """
    if value is _MISSING:
        return None
    if isinstance(value, str) and value in _SECRET_MARKERS:
        return "«redacted by Pulumi»"
    try:
        rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > MAX_DIFF_VALUE_CHARS:
        rendered = rendered[:MAX_DIFF_VALUE_CHARS] + "…(truncated)"
    return rendered


def serialize_resource_event(event: ResourcePreEvent) -> dict[str, Any]:
    """Convert a ResourcePreEvent into a JSON-serialisable dict.

    Carries the OLD AND NEW VALUE of each changed property, not just its name.
    Knowing that `version` changed says almost nothing; knowing it went
    `1.2.3 -> 1.2.4` is the actual review.

    ★ VALUES COME FROM `inputs`, NEVER `outputs`. The automation API documents
    inputs as having secrets filtered out and large assets replaced by hashes;
    it makes no such promise about outputs. This body is published to a GitHub
    issue, so the difference matters. Anything still carrying Pulumi's filter
    marker is rendered as an explicit redaction.
    """
    meta = event.metadata
    old_inputs = getattr(meta.old, "inputs", None) or {}
    new_inputs = getattr(meta.new, "inputs", None) or {}

    detailed: dict[str, Any] = {}
    for path, diff in (meta.detailed_diff or {}).items():
        entry: dict[str, Any] = {
            "diff_kind": diff.diff_kind.value,
            "input_diff": diff.input_diff,
        }
        old = _render_value(_resolve_property_path(old_inputs, path))
        new = _render_value(_resolve_property_path(new_inputs, path))
        if old is not None:
            entry["old"] = old
        if new is not None:
            entry["new"] = new
        detailed[path] = entry

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
        "change_summary": _op_counts(result.change_summary),
        "changes": changes,
        "stdout": result.stdout,
    }

    if output_file is not None:
        output_file.write_text(json.dumps(payload, indent=2))

    return payload
