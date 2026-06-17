"""Tests for Pulumi lock recovery helpers in pulumi_utils."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pulumi import automation as auto

import pulumi_utils
from pulumi_utils import (
    _is_recoverable_lock,
    _parse_lock_holders,
    _with_lock_recovery,
)

# ---------------------------------------------------------------------------
# Realistic lock error message from the Pulumi S3 backend
# ---------------------------------------------------------------------------

_CONCOURSE_LOCK_MSG = (
    "error: the stack is currently locked by 1 lock(s). Either wait for the other "
    "process(es) to end or delete the lock file with `pulumi cancel`.\n"
    "s3://mitol-pulumi-state/.pulumi/locks/organization/ol-application-edxapp/"
    "mitx.Production/22d8693a-7bae-4563-bc98-27045e703b9f.json: "
    "created by root@6d33034d-39d7-41de-80a3-b03e008d0ea2 (pid 285) "
    "at 2026-06-17T18:46:42Z"
)

_DEVELOPER_LOCK_MSG = (
    "error: the stack is currently locked by 1 lock(s).\n"
    "s3://mitol-pulumi-state/.pulumi/locks/organization/ol-application-edxapp/"
    "mitx.Production/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.json: "
    "created by tmacey@tmacey-laptop.local (pid 12345) "
    "at 2026-06-17T17:00:00Z"
)

_MULTI_LOCK_ALL_CONCOURSE_MSG = (
    "error: the stack is currently locked by 2 lock(s).\n"
    "lock1.json: created by root@aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee (pid 1) "
    "at 2026-06-17T10:00:00Z\n"
    "lock2.json: created by root@bbbbbbbb-cccc-dddd-eeee-ffffffffffff (pid 2) "
    "at 2026-06-17T10:01:00Z"
)

_MIXED_LOCK_MSG = (
    "error: the stack is currently locked by 2 lock(s).\n"
    "lock1.json: created by root@aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee (pid 1) "
    "at 2026-06-17T10:00:00Z\n"
    "lock2.json: created by tmacey@my-workstation (pid 9999) "
    "at 2026-06-17T10:01:00Z"
)


def _concurrent_error(msg: str) -> auto.ConcurrentUpdateError:
    return auto.ConcurrentUpdateError(msg)


# ---------------------------------------------------------------------------
# _parse_lock_holders
# ---------------------------------------------------------------------------


class TestParseLockHolders:
    def test_parses_concourse_lock(self) -> None:
        holders = _parse_lock_holders(_concurrent_error(_CONCOURSE_LOCK_MSG))
        assert len(holders) == 1
        assert holders[0]["user"] == "root"
        assert holders[0]["host"] == "6d33034d-39d7-41de-80a3-b03e008d0ea2"
        assert holders[0]["pid"] == "285"
        assert holders[0]["at"] == "2026-06-17T18:46:42Z"

    def test_parses_developer_lock(self) -> None:
        holders = _parse_lock_holders(_concurrent_error(_DEVELOPER_LOCK_MSG))
        assert len(holders) == 1
        assert holders[0]["user"] == "tmacey"
        assert holders[0]["host"] == "tmacey-laptop.local"

    def test_parses_multiple_holders(self) -> None:
        holders = _parse_lock_holders(_concurrent_error(_MULTI_LOCK_ALL_CONCOURSE_MSG))
        assert len(holders) == 2
        assert holders[0]["pid"] == "1"
        assert holders[1]["pid"] == "2"

    def test_empty_for_unrecognised_message(self) -> None:
        holders = _parse_lock_holders(_concurrent_error("some other error"))
        assert holders == []


# ---------------------------------------------------------------------------
# _is_recoverable_lock
# ---------------------------------------------------------------------------


class TestIsRecoverableLock:
    def test_concourse_uuid_hostname_root_user_is_recoverable(self) -> None:
        holders = _parse_lock_holders(_concurrent_error(_CONCOURSE_LOCK_MSG))
        assert _is_recoverable_lock(holders) is True

    def test_developer_hostname_is_not_recoverable(self) -> None:
        holders = _parse_lock_holders(_concurrent_error(_DEVELOPER_LOCK_MSG))
        assert _is_recoverable_lock(holders) is False

    def test_all_concourse_multiple_locks_are_recoverable(self) -> None:
        holders = _parse_lock_holders(_concurrent_error(_MULTI_LOCK_ALL_CONCOURSE_MSG))
        assert _is_recoverable_lock(holders) is True

    def test_mixed_locks_are_not_recoverable(self) -> None:
        holders = _parse_lock_holders(_concurrent_error(_MIXED_LOCK_MSG))
        assert _is_recoverable_lock(holders) is False

    def test_empty_holders_are_not_recoverable(self) -> None:
        assert _is_recoverable_lock([]) is False

    def test_non_root_user_uuid_hostname_is_not_recoverable(self) -> None:
        holders = [
            {
                "user": "ubuntu",
                "host": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "pid": "1",
                "at": "t",
            }
        ]
        assert _is_recoverable_lock(holders) is False

    def test_root_user_non_uuid_hostname_is_not_recoverable(self) -> None:
        holders = [
            {"user": "root", "host": "my-server.example.com", "pid": "1", "at": "t"}
        ]
        assert _is_recoverable_lock(holders) is False


# ---------------------------------------------------------------------------
# _with_lock_recovery
# ---------------------------------------------------------------------------


class TestWithLockRecovery:
    def test_success_path_returns_result(self) -> None:
        stack = MagicMock()
        result = _with_lock_recovery(lambda: 42, stack, "my-stack")
        assert result == 42
        stack.cancel.assert_not_called()

    def test_cancels_and_retries_on_concourse_lock(self) -> None:
        stack = MagicMock()
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] == 1:
                raise auto.ConcurrentUpdateError(_CONCOURSE_LOCK_MSG)
            return "ok"

        result = _with_lock_recovery(flaky, stack, "my-stack")

        assert result == "ok"
        stack.cancel.assert_called_once()
        assert call_count[0] == 2

    def test_does_not_cancel_on_developer_lock(self) -> None:
        stack = MagicMock()

        def always_fails():
            raise auto.ConcurrentUpdateError(_DEVELOPER_LOCK_MSG)

        with pytest.raises(auto.ConcurrentUpdateError):
            _with_lock_recovery(always_fails, stack, "my-stack")

        stack.cancel.assert_not_called()

    def test_propagates_second_failure_after_cancel(self) -> None:
        stack = MagicMock()

        def always_fails():
            raise auto.ConcurrentUpdateError(_CONCOURSE_LOCK_MSG)

        with pytest.raises(auto.ConcurrentUpdateError):
            _with_lock_recovery(always_fails, stack, "my-stack")

        stack.cancel.assert_called_once()

    def test_raises_when_cancel_itself_fails(self) -> None:
        stack = MagicMock()
        stack.cancel.side_effect = RuntimeError("network error")

        def fails_with_concourse_lock():
            raise auto.ConcurrentUpdateError(_CONCOURSE_LOCK_MSG)

        with pytest.raises(auto.ConcurrentUpdateError, match="Lock recovery failed"):
            _with_lock_recovery(fails_with_concourse_lock, stack, "my-stack")

    def test_does_not_cancel_on_empty_holder_message(self) -> None:
        stack = MagicMock()

        def always_fails():
            raise auto.ConcurrentUpdateError("some unrecognised lock message")

        with pytest.raises(auto.ConcurrentUpdateError):
            _with_lock_recovery(always_fails, stack, "my-stack")

        stack.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# update_stack / destroy_stack integration with lock recovery
# ---------------------------------------------------------------------------


class TestUpdateStackLockRecovery:
    def test_update_stack_recovers_from_concourse_lock_on_up(self) -> None:
        mock_stack = MagicMock()
        up_result = MagicMock()
        up_result.summary.version = 7
        call_count = [0]

        def flaky_up(**_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise auto.ConcurrentUpdateError(_CONCOURSE_LOCK_MSG)
            return up_result

        mock_stack.up.side_effect = flaky_up

        with patch("pulumi_utils.auto") as mock_auto:
            mock_auto.select_stack.return_value = mock_stack
            mock_auto.StackNotFoundError = auto.StackNotFoundError
            mock_auto.ConcurrentUpdateError = auto.ConcurrentUpdateError
            mock_auto.ConfigValue = auto.ConfigValue
            mock_auto.LocalWorkspaceOptions = auto.LocalWorkspaceOptions

            version = pulumi_utils.update_stack(
                stack_name="my-stack",
                project_name="my-project",
                source_dir="/nonexistent/src",
                stack_config={},
                env_pulumi={},
                refresh_stack=False,
            )

        assert version == 7
        mock_stack.cancel.assert_called_once()

    def test_update_stack_does_not_cancel_developer_lock(self) -> None:
        mock_stack = MagicMock()
        mock_stack.up.side_effect = auto.ConcurrentUpdateError(_DEVELOPER_LOCK_MSG)

        with patch("pulumi_utils.auto") as mock_auto:
            mock_auto.select_stack.return_value = mock_stack
            mock_auto.StackNotFoundError = auto.StackNotFoundError
            mock_auto.ConcurrentUpdateError = auto.ConcurrentUpdateError
            mock_auto.ConfigValue = auto.ConfigValue
            mock_auto.LocalWorkspaceOptions = auto.LocalWorkspaceOptions

            with pytest.raises(auto.ConcurrentUpdateError):
                pulumi_utils.update_stack(
                    stack_name="my-stack",
                    project_name="my-project",
                    source_dir="/nonexistent/src",
                    stack_config={},
                    env_pulumi={},
                    refresh_stack=False,
                )

        mock_stack.cancel.assert_not_called()
