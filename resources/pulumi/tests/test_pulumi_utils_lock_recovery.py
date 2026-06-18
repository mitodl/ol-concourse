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
    "at 2020-01-01T00:00:00Z"
)

_DEVELOPER_LOCK_MSG = (
    "error: the stack is currently locked by 1 lock(s).\n"
    "s3://mitol-pulumi-state/.pulumi/locks/organization/ol-application-edxapp/"
    "mitx.Production/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.json: "
    "created by tmacey@tmacey-laptop.local (pid 12345) "
    "at 2020-01-01T00:00:00Z"
)

_MULTI_LOCK_ALL_CONCOURSE_MSG = (
    "error: the stack is currently locked by 2 lock(s).\n"
    "lock1.json: created by root@aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee (pid 1) "
    "at 2020-01-01T00:00:00Z\n"
    "lock2.json: created by root@bbbbbbbb-cccc-dddd-eeee-ffffffffffff (pid 2) "
    "at 2020-01-01T00:01:00Z"
)

_MIXED_LOCK_MSG = (
    "error: the stack is currently locked by 2 lock(s).\n"
    "lock1.json: created by root@aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee (pid 1) "
    "at 2020-01-01T00:00:00Z\n"
    "lock2.json: created by tmacey@my-workstation (pid 9999) "
    "at 2020-01-01T00:01:00Z"
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
        assert holders[0]["at"] == "2020-01-01T00:00:00Z"

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


# Timestamps clearly in the past (> 15 min) so age check always passes in tests
_OLD_TS = "2020-01-01T00:00:00Z"
# Timestamp a few seconds ago — always younger than the 15-min threshold
_FRESH_TS = "2099-12-31T23:59:59Z"


def _stale_holder(
    user: str = "root", host: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
) -> dict[str, str]:
    return {"user": user, "host": host, "pid": "1", "at": _OLD_TS}


def _fresh_holder(
    user: str = "root", host: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
) -> dict[str, str]:
    return {"user": user, "host": host, "pid": "1", "at": _FRESH_TS}


# ---------------------------------------------------------------------------
# _is_recoverable_lock
# ---------------------------------------------------------------------------


class TestIsRecoverableLock:
    # --- hostname / user checks (age bypassed with min_age_minutes=0) ---

    def test_concourse_root_uuid_hostname_passes_identity_check(self) -> None:
        assert _is_recoverable_lock([_stale_holder()], min_age_minutes=0) is True

    def test_developer_hostname_fails_identity_check(self) -> None:
        holders = [_stale_holder(host="tmacey-laptop.local")]
        assert _is_recoverable_lock(holders, min_age_minutes=0) is False

    def test_non_root_user_fails_identity_check(self) -> None:
        holders = [_stale_holder(user="ubuntu")]
        assert _is_recoverable_lock(holders, min_age_minutes=0) is False

    def test_all_concourse_multiple_locks_pass_identity_check(self) -> None:
        holders = [
            _stale_holder(host="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            _stale_holder(host="bbbbbbbb-cccc-dddd-eeee-ffffffffffff"),
        ]
        assert _is_recoverable_lock(holders, min_age_minutes=0) is True

    def test_mixed_locks_fail_identity_check(self) -> None:
        holders = [
            _stale_holder(host="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            _stale_holder(user="tmacey", host="my-workstation"),
        ]
        assert _is_recoverable_lock(holders, min_age_minutes=0) is False

    def test_empty_holders_not_recoverable(self) -> None:
        assert _is_recoverable_lock([]) is False

    # --- age threshold checks ---

    def test_stale_lock_is_recoverable(self) -> None:
        # _OLD_TS is years in the past, comfortably older than any threshold
        assert _is_recoverable_lock([_stale_holder()]) is True

    def test_fresh_lock_is_not_recoverable(self) -> None:
        # _FRESH_TS is far in the future — always "not yet old enough"
        assert _is_recoverable_lock([_fresh_holder()]) is False

    def test_custom_min_age_zero_bypasses_age_check(self) -> None:
        assert _is_recoverable_lock([_fresh_holder()], min_age_minutes=0) is True

    def test_unparseable_timestamp_blocks_recovery_with_age_check(self) -> None:
        holders = [
            {
                "user": "root",
                "host": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "pid": "1",
                "at": "not-a-timestamp",
            }
        ]
        assert _is_recoverable_lock(holders) is False

    def test_unparseable_timestamp_passes_when_age_check_disabled(self) -> None:
        holders = [
            {
                "user": "root",
                "host": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "pid": "1",
                "at": "not-a-timestamp",
            }
        ]
        assert _is_recoverable_lock(holders, min_age_minutes=0) is True


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
