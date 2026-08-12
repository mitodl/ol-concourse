"""Tests for resources/release/concourse.py."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from concourse import (
    ReleaseResource,
    ReleaseVersion,
    SEMVER_PATTERN,
    _authed_uri,
    _build_changelog_entry,
    _build_checklist,
    _clone,
    _commit_info_range,
    _compute_next_version,
    _get_in_flight_release_version,
    _is_release_machinery,
    _get_semver_tags,
    _parse_commit_log,
    _parse_semver_tuple,
    _parse_version_tuple,
    _run,
    _update_cumulative_changelog,
    CHANGELOG_HEADER,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_resource(**kwargs: Any) -> ReleaseResource:
    defaults: dict[str, Any] = {
        "uri": "https://github.com/mitodl/my-app.git",
        "branch": "main",
        "access_token": None,
        "repository": None,
    }
    defaults.update(kwargs)
    return ReleaseResource(**defaults)


def make_version(**kwargs) -> ReleaseVersion:
    defaults = {
        "version": "2026.4.14.1",
        "head_sha": "abc1234" * 5,
        "since": "2026.4.10.1",
        "commit_count": "3",
        "authors": "alice@example.com,bob@example.com",
    }
    defaults.update(kwargs)
    return ReleaseVersion(**defaults)


def make_commits(n: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "sha": f"{'a' * 7}{i}" * 5,
            "author": f"dev{i}@example.com",
            "message": f"Fix thing {i}",
            "pr_number": 100 + i,
            "pr_title": f"PR: Fix thing {i}",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# _parse_version_tuple
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("2026.4.14.1", (2026, 4, 14, 1)),
        ("2026.4.14.10", (2026, 4, 14, 10)),
        ("2026.1.5.1", (2026, 1, 5, 1)),
        ("not-a-version", (0, 0, 0, 0)),
        ("", (0, 0, 0, 0)),
    ],
)
def test_parse_version_tuple(tag, expected):
    assert _parse_version_tuple(tag) == expected


# ---------------------------------------------------------------------------
# _compute_next_version
# ---------------------------------------------------------------------------


def test_compute_next_version_no_tags():
    d = datetime.now(tz=UTC).date()
    today = f"{d.year}.{d.month}.{d.day}"
    assert _compute_next_version([]) == f"{today}.1"


def _fake_datetime(fixed: datetime):
    """Return a minimal datetime stand-in whose .now() returns *fixed*."""
    return type("DT", (), {"now": staticmethod(lambda tz=None: fixed)})()


def test_compute_next_version_increments_n(monkeypatch):
    monkeypatch.setattr(
        "concourse.datetime", _fake_datetime(datetime(2026, 4, 14, tzinfo=UTC))
    )
    tags = ["2026.4.14.1", "2026.4.14.2"]
    assert _compute_next_version(tags) == "2026.4.14.3"


def test_compute_next_version_new_day_resets_to_one(monkeypatch):
    monkeypatch.setattr(
        "concourse.datetime", _fake_datetime(datetime(2026, 4, 15, tzinfo=UTC))
    )
    tags = ["2026.4.14.1", "2026.4.14.2"]
    assert _compute_next_version(tags) == "2026.4.15.1"


def test_compute_next_version_ignores_other_date_tags(monkeypatch):
    monkeypatch.setattr(
        "concourse.datetime", _fake_datetime(datetime(2026, 4, 14, tzinfo=UTC))
    )
    tags = ["2026.4.13.5", "2026.4.14.3"]
    assert _compute_next_version(tags) == "2026.4.14.4"


def test_compute_next_version_handles_mixed_zero_padded_tags(monkeypatch):
    monkeypatch.setattr(
        "concourse.datetime", _fake_datetime(datetime(2026, 4, 14, tzinfo=UTC))
    )
    tags = ["2026.04.14.1", "2026.4.14.2", "2026.04.14.3", "2026.4.13.9"]
    assert _compute_next_version(tags) == "2026.4.14.4"


# ---------------------------------------------------------------------------
# _parse_semver_tuple / SEMVER_PATTERN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("1.2.3", (1, 2, 3)),
        ("v1.2.3", (1, 2, 3)),
        ("v10.20.300", (10, 20, 300)),
        ("0.0.1", (0, 0, 1)),
        ("not-semver", (0, 0, 0)),
        ("2026.4.14.1", (0, 0, 0)),  # date-format tag must not match
        ("", (0, 0, 0)),
    ],
)
def test_parse_semver_tuple(tag, expected):
    assert _parse_semver_tuple(tag) == expected


@pytest.mark.parametrize(
    "tag, should_match",
    [
        ("1.2.3", True),
        ("v1.2.3", True),
        ("v0.0.1", True),
        ("v10.20.300", True),
        ("2026.4.14.1", False),  # date-format must not collide
        ("v1.2.3.4", False),  # four components — not semver
        ("v1.2", False),  # only two components
        ("1.2.3-rc1", False),  # pre-release suffix
    ],
)
def test_semver_pattern(tag, should_match):
    assert bool(SEMVER_PATTERN.match(tag)) == should_match


@patch("concourse._run")
def test_get_semver_tags_sorted(mock_run, tmp_path):
    mock_run.return_value = "v2.0.0\nv1.9.0\nv1.10.0\n1.0.0\n2026.4.14.1\nbad-tag"
    result = _get_semver_tags(tmp_path, env={})
    # date-format and bad-tag excluded;
    # remainder sorted by (major, minor, patch)
    assert result == ["1.0.0", "v1.9.0", "v1.10.0", "v2.0.0"]
    assert "2026.4.14.1" not in result


# ---------------------------------------------------------------------------
# _parse_commit_log
# ---------------------------------------------------------------------------


def test_parse_commit_log_extracts_all_fields():
    output = "abc1234def5|dev@example.com|Dev Person|Fix bug"
    commits = _parse_commit_log(output)

    assert commits == [
        {
            "sha": "abc1234def5",
            "author": "dev@example.com",
            "author_name": "Dev Person",
            "message": "Fix bug",
            "pr_number": None,
            "pr_title": None,
        }
    ]


def test_parse_commit_log_message_with_pipe_stays_intact():
    """maxsplit=3 keeps a literal '|' in the commit subject from splitting it."""
    output = "abc1234def5|dev@example.com|Dev Person|fix: a | b thing"
    commits = _parse_commit_log(output)

    assert commits[0]["message"] == "fix: a | b thing"


def test_parse_commit_log_falls_back_to_email_when_name_empty():
    """An empty %an (e.g. a bot with no configured name) falls back to email."""
    output = "abc1234def5|bot@example.com||Automated bump"
    commits = _parse_commit_log(output)

    assert commits[0]["author_name"] == "bot@example.com"


def test_parse_commit_log_skips_malformed_lines():
    output = "\n".join(["not-enough-pipes", "abc1234def5|a@x.com|A|msg"])
    commits = _parse_commit_log(output)

    assert len(commits) == 1
    assert commits[0]["sha"] == "abc1234def5"


# ---------------------------------------------------------------------------
# _build_checklist
# ---------------------------------------------------------------------------


def test_build_checklist_with_prs():
    commits = [
        {
            "sha": "abc1234def5",
            "author": "dev@example.com",
            "author_name": "Dev Person",
            "message": "Fix bug",
            "pr_number": 42,
            "pr_title": "Fix the bug",
        }
    ]
    result = _build_checklist("2026.4.14.1", commits)
    assert "## Release 2026.4.14.1" in result
    assert "### Dev Person" in result
    assert "- [ ] **Fix the bug** (#42) by dev@example.com" in result
    assert "Closing this issue will trigger the production deployment" in result


def test_build_checklist_without_prs():
    commits = [
        {
            "sha": "abc1234def5",
            "author": "dev@example.com",
            "author_name": "Dev Person",
            "message": "Fix bug",
            "pr_number": None,
            "pr_title": None,
        }
    ]
    result = _build_checklist("2026.4.14.1", commits)
    assert "- [ ] `abc1234` Fix bug by dev@example.com" in result


def test_build_checklist_empty_commits():
    result = _build_checklist("2026.4.14.1", [])
    assert "## Release 2026.4.14.1" in result
    assert "Closing this issue will trigger the production deployment" in result


def test_build_checklist_groups_by_author_name():
    """Commits are grouped under a heading per author, in first-appearance order."""
    commits = [
        {
            "sha": "aaa1111aaaa",
            "author": "alice@example.com",
            "author_name": "Alice Author",
            "message": "First alice commit",
            "pr_number": None,
            "pr_title": None,
        },
        {
            "sha": "bbb2222bbbb",
            "author": "bob@example.com",
            "author_name": "Bob Builder",
            "message": "Bob's commit",
            "pr_number": None,
            "pr_title": None,
        },
        {
            "sha": "ccc3333cccc",
            "author": "alice@example.com",
            "author_name": "Alice Author",
            "message": "Second alice commit",
            "pr_number": None,
            "pr_title": None,
        },
    ]
    result = _build_checklist("2026.4.14.1", commits)
    lines = result.splitlines()

    alice_idx = lines.index("### Alice Author")
    bob_idx = lines.index("### Bob Builder")
    assert alice_idx < bob_idx, "First-appearance author (Alice) heads the list"

    # Both of Alice's commits are grouped together under her single heading,
    # not split across two "### Alice Author" sections.
    assert lines.count("### Alice Author") == 1
    assert "- [ ] `aaa1111` First alice commit by alice@example.com" in result
    assert "- [ ] `ccc3333` Second alice commit by alice@example.com" in result
    assert "- [ ] `bbb2222` Bob's commit by bob@example.com" in result


def test_build_checklist_falls_back_to_email_without_author_name():
    """A commit dict with no author_name groups under its raw email instead."""
    commits = [
        {
            "sha": "abc1234def5",
            "author": "bot@example.com",
            "message": "Automated bump",
            "pr_number": None,
            "pr_title": None,
        }
    ]
    result = _build_checklist("2026.4.14.1", commits)
    assert "### bot@example.com" in result


# ---------------------------------------------------------------------------
# _build_changelog_entry
# ---------------------------------------------------------------------------


def test_build_changelog_entry_format():
    commits: list[dict[str, Any]] = [
        {
            "sha": "abc1234def5",
            "author": "dev@example.com",
            "message": "Fix bug",
            "pr_number": 42,
            "pr_title": "Fix the bug",
        },
        {
            "sha": "def5678abc9",
            "author": "other@example.com",
            "message": "No PR commit",
            "pr_number": None,
            "pr_title": None,
        },
    ]
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    result = _build_changelog_entry("2026.4.14.1", commits)
    assert f"## [2026.4.14.1] - {today}" in result
    assert "### Changes" in result
    assert "- **Fix the bug** (#42) by dev@example.com" in result
    assert "- `def5678` No PR commit by other@example.com" in result


# ---------------------------------------------------------------------------
# _update_cumulative_changelog
# ---------------------------------------------------------------------------


def test_update_cumulative_changelog_creates_new_file(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    entry = "## [2026.4.14.1] - 2026-04-14\n\n### Changes\n\n- Fix thing\n"
    _update_cumulative_changelog(changelog, entry)
    content = changelog.read_text()
    assert CHANGELOG_HEADER in content
    assert entry in content


def test_update_cumulative_changelog_prepends_to_existing(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    old_entry = "## [2026.4.10.1] - 2026-04-10\n\n### Changes\n\n- Old fix\n"
    changelog.write_text(CHANGELOG_HEADER + "\n" + old_entry)

    new_entry = "## [2026.4.14.1] - 2026-04-14\n\n### Changes\n\n- New fix\n"
    _update_cumulative_changelog(changelog, new_entry)

    content = changelog.read_text()
    new_pos = content.index("2026.4.14.1")
    old_pos = content.index("2026.4.10.1")
    assert new_pos < old_pos, "New entry should appear before old entry"


def test_update_cumulative_changelog_header_only_file(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG_HEADER)
    entry = "## [2026.4.14.1] - 2026-04-14\n\n### Changes\n\n- Fix\n"
    _update_cumulative_changelog(changelog, entry)
    content = changelog.read_text()
    assert CHANGELOG_HEADER in content
    assert entry in content


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_download_version_writes_empty_since_when_no_prior_tag(
    mock_tmpdir, mock_run, tmp_path
):
    """Since file is written even when version.since is empty."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "abc" * 13 + "a"
    outputs = ["", "", f"{head_sha}|dev@example.com|Dev Person|Initial commit"]
    idx = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal idx
        out = outputs[idx % len(outputs)]
        idx += 1
        return out

    mock_run.side_effect = run_side_effect
    dest = tmp_path / "output"
    resource = make_resource()
    version = make_version(since="", head_sha=head_sha)
    resource.download_version(version, dest, MagicMock())
    assert (dest / "since").exists()
    assert (dest / "since").read_text() == ""


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_semver_fallback_used_when_no_date_tags(
    mock_tmpdir, mock_run, tmp_path, monkeypatch
):
    """With semver_tag_fallback=True and only semver tags, since = latest semver tag."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "newwork1" * 5

    outputs = [
        "",  # git clone
        "",  # git fetch --tags
        "v1.2.3\nv1.3.0\nbad-tag",  # git tag --list (no date-format tags)
        head_sha,  # git rev-parse origin/main
        "",  # git branch -r (no in-flight)
        # semver fallback branch: git tag --list again for _get_semver_tags
        "v1.2.3\nv1.3.0\nbad-tag",
        # _commit_info_range for v1.3.0..head_sha
        "dev@example.com\nalice@example.com",
    ]
    idx = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal idx
        out = outputs[idx]
        idx += 1
        return out

    mock_run.side_effect = run_side_effect
    resource = make_resource(semver_tag_fallback=True)

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    assert versions[0].since == "v1.3.0"
    assert versions[0].commit_count == "2"


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_semver_fallback_ignored_when_date_tags_exist(
    mock_tmpdir, mock_run, tmp_path, monkeypatch
):
    """Date-format tags take priority over semver tags even when fallback is on."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "newwork1" * 5
    tag_sha = "oldtag11" * 5

    outputs = [
        "",
        "",
        "v1.3.0\n2026.4.14.1",  # both semver and date-format present
        head_sha,
        "",  # git branch -r (no in-flight)
        tag_sha,  # rev-list -n1 2026.4.14.1
        "dev@example.com",
    ]
    idx = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal idx
        out = outputs[idx]
        idx += 1
        return out

    mock_run.side_effect = run_side_effect
    resource = make_resource(semver_tag_fallback=True)

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert versions[0].since == "2026.4.14.1"


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_semver_fallback_disabled(mock_tmpdir, mock_run, tmp_path):
    """With semver_tag_fallback=False, semver tags are ignored and since is empty."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "newwork1" * 5

    outputs = [
        "",
        "",
        "v1.2.3\nv1.3.0",  # only semver tags
        head_sha,
        "",  # git branch -r (no in-flight)
        "dev@example.com\nalice@example.com",  # _commit_info_all
    ]
    idx = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal idx
        out = outputs[idx]
        idx += 1
        return out

    mock_run.side_effect = run_side_effect
    resource = make_resource(semver_tag_fallback=False)

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert versions[0].since == ""


# ---------------------------------------------------------------------------
# semver_tag_fallback — _create_release (out / action=create)
# ---------------------------------------------------------------------------


@patch("concourse._run")
def test_create_release_semver_fallback_uses_latest_semver_as_since(mock_run, tmp_path):
    """_create_release uses latest semver tag as since_ref when no date-format tags."""
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "prebump1" * 5
    log_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "log" in cmd:
            log_calls.append(list(cmd))
            return ""
        if "tag" in cmd and "--list" in cmd:
            return "v2.0.0\nv2.1.0"  # only semver tags
        if "rev-parse" in cmd:
            return pre_bump_sha
        if "status" in cmd:
            return ""
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource(semver_tag_fallback=True)
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    # The git log call for changelog generation should use v2.1.0 as since_ref
    assert log_calls, "Expected at least one git log call"
    log_cmd_str = " ".join(log_calls[0])
    assert "v2.1.0" in log_cmd_str, (
        f"Expected v2.1.0 as since_ref in log call, got: {log_cmd_str}"
    )


@patch("concourse._run")
def test_create_release_semver_fallback_disabled_no_since(mock_run, tmp_path):
    """With semver_tag_fallback=False, since_ref is empty regardless of semver tags."""
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "prebump1" * 5
    log_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "log" in cmd:
            log_calls.append(list(cmd))
            return ""
        if "tag" in cmd and "--list" in cmd:
            return "v2.0.0\nv2.1.0"
        if "rev-parse" in cmd:
            return pre_bump_sha
        if "status" in cmd:
            return ""
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource(semver_tag_fallback=False)
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert log_calls, "Expected at least one git log call"
    log_cmd_str = " ".join(log_calls[0])
    # No since..until range — just the SHA
    assert "v2.1.0" not in log_cmd_str, (
        "semver tag must not appear in log call when fallback is disabled"
    )


def _check_git_stub(
    *,
    tags: list[str],
    head_sha: str,
    in_flight: list[str] | None = None,
    logs: dict[str, str] | None = None,
):
    """Return a ``_run`` replacement that dispatches on the git command.

    Dispatching on the command rather than on call position keeps these tests
    from breaking every time ``check`` gains or drops a git invocation, which
    is what made the previous positional-output fixtures unmaintainable.

    *logs* maps a ``git log`` rev-range (``"a..b"`` or a bare ref) to that
    call's output in ``%ae|%s`` format; ranges not listed produce no commits.
    """
    logs = logs or {}

    def run(cmd, **_kwargs):
        if cmd[1] == "tag" and cmd[2] == "--list":
            return "\n".join(tags)
        if cmd[1] == "rev-parse":
            return head_sha
        if cmd[1] == "ls-remote":
            return "\n".join(
                f"{'ab' * 20}\trefs/heads/releases/{v}" for v in (in_flight or [])
            )
        if cmd[1] == "log":
            return logs.get(cmd[-1], "")
        return ""

    return run


def _log(*commits: tuple[str, str]) -> str:
    """Render ``(email, subject)`` pairs as ``git log --format=%ae|%s`` output."""
    return "\n".join(f"{email}|{subject}" for email, subject in commits)


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_no_tags(mock_tmpdir, mock_run, tmp_path):
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "deadbeef" * 5

    call_index = 0
    # 0: clone, 1: fetch --tags, 2: tag --list (empty), 3: rev-parse,
    # 4: branch -r (no in-flight), 5: commit_info_all
    outputs = ["", "", "", head_sha, "", "dev@example.com"]

    def run_side_effect(cmd, **kwargs):
        nonlocal call_index
        out = outputs[call_index]
        call_index += 1
        return out

    mock_run.side_effect = run_side_effect
    resource = make_resource()

    with patch("concourse.datetime") as mock_dt:
        fixed_date = datetime(2026, 4, 14, tzinfo=UTC).date()
        mock_dt.now.return_value.date.return_value = fixed_date
        versions = resource.fetch_new_versions(None)
    assert versions[0].head_sha == head_sha
    assert versions[0].since == ""


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_head_equals_tag(mock_tmpdir, mock_run, tmp_path):
    """When HEAD is already tagged, return the existing version unchanged."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "tagged1234" * 4

    mock_run.side_effect = _check_git_stub(
        tags=["2026.4.10.1", "2026.4.14.1"],
        head_sha=head_sha,
        logs={
            # Nothing between the latest tag and HEAD -- HEAD *is* the tag.
            f"2026.4.14.1..{head_sha}": "",
            # Summarised against the release before it.
            f"2026.4.10.1..{head_sha}": _log(("dev@example.com", "a fix")),
        },
    )
    resource = make_resource()
    versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    assert versions[0].version == "2026.4.14.1"
    assert versions[0].head_sha == head_sha
    assert versions[0].since == "2026.4.10.1"
    assert versions[0].commit_count == "1"
    assert versions[0].in_flight == ""


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_new_commits(mock_tmpdir, mock_run, tmp_path, monkeypatch):
    """When HEAD is ahead of the latest tag, return the next version."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "newcommit1" * 4

    mock_run.side_effect = _check_git_stub(
        tags=["2026.4.14.1"],
        head_sha=head_sha,
        logs={
            f"2026.4.14.1..{head_sha}": _log(
                ("dev@example.com", "feat: a thing"),
                ("alice@example.com", "fix: another thing"),
            )
        },
    )
    resource = make_resource()

    with patch("concourse.datetime") as mock_dt:
        fixed_date = datetime(2026, 4, 14, tzinfo=UTC).date()
        mock_dt.now.return_value.date.return_value = fixed_date
        versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    assert versions[0].version == "2026.4.14.2"
    assert versions[0].since == "2026.4.14.1"
    assert versions[0].head_sha == head_sha
    assert versions[0].commit_count == "2"


# ---------------------------------------------------------------------------
# download_version (in)
# ---------------------------------------------------------------------------


@patch("concourse._enrich_with_github")
@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_download_version_writes_all_outputs(
    mock_tmpdir, mock_run, mock_enrich, tmp_path
):
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "abc" * 13 + "a"

    git_log_output = "\n".join(
        [
            f"{head_sha}|dev@example.com|Dev Person|Fix bug",
            f"{'b' * 40}|alice@example.com|Alice Author|Add feature",
        ]
    )
    outputs = ["", "", git_log_output]
    call_index = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal call_index
        out = outputs[call_index % len(outputs)]
        call_index += 1
        return out

    mock_run.side_effect = run_side_effect
    mock_enrich.side_effect = lambda commits, *a, **kw: commits

    dest = tmp_path / "output"
    resource = make_resource(access_token="tok", repository="mitodl/my-app")
    version = make_version(head_sha=head_sha)
    resource.download_version(version, dest, MagicMock())

    assert (dest / "version").read_text() == version.version
    assert (dest / "since").read_text() == version.since
    commits = json.loads((dest / "commits.json").read_text())
    assert len(commits) == 2
    assert (dest / "checklist.md").exists()
    assert (dest / "changelog_entry.md").exists()


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_download_version_no_since_uses_head_sha(mock_tmpdir, mock_run, tmp_path):
    """When version.since is empty, the full commit history up to head_sha is used."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "abc" * 13 + "a"
    outputs = [
        "",
        "",
        head_sha,
        f"{head_sha}|dev@example.com|Dev Person|Initial commit",
    ]
    call_index = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal call_index
        out = outputs[call_index % len(outputs)]
        call_index += 1
        return out

    mock_run.side_effect = run_side_effect

    dest = tmp_path / "output"
    resource = make_resource()
    version = make_version(since="", head_sha=head_sha)
    resource.download_version(version, dest, MagicMock())

    # The git log call should use head_sha directly (no since..head_sha range)
    log_calls = [c for c in mock_run.call_args_list if "log" in c.args[0]]
    assert log_calls, "Expected a git log call"
    log_cmd = log_calls[0].args[0]
    # range spec should be just the SHA, not "..SHA"
    assert head_sha in log_cmd
    assert ".." not in "".join(
        c for c in log_cmd if c not in ["--format=%H|%ae|%an|%s"]
    )


# ---------------------------------------------------------------------------
# publish_new_version (out)
# ---------------------------------------------------------------------------


@patch("concourse._run")
def test_publish_new_version_invalid_action(mock_run, tmp_path):
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text("2026.4.14.1")

    resource = make_resource()
    with pytest.raises(ValueError, match="Invalid action"):
        resource.publish_new_version(
            tmp_path,
            MagicMock(),
            action="deploy",  # type: ignore[arg-type]
            repo_dir="app-source",
            version_file="release/version",
        )


@patch("concourse._run")
def test_publish_new_version_create(mock_run, tmp_path):
    """Create action: sets up branch, commits, pushes, tags."""
    version_str = "2026.4.14.1"

    # Set up workspace
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)

    app_dir = tmp_path / "app-source"
    app_dir.mkdir()

    pre_bump_sha = "pre1234" * 5 + "p"
    outputs = iter(
        [
            "",  # git config user.name
            "",  # git config user.email
            "",  # git fetch origin main --tags
            "",  # git ls-remote (no in-flight release to supersede)
            "",  # git status --porcelain (check dirty before reset — no dirty files)
            "",  # git checkout main
            "",  # git reset --hard origin/main
            pre_bump_sha,  # git rev-parse HEAD (pre-bump)
            "",  # git checkout -b releases/2026.4.14.1
            "",  # git status --porcelain (staging check — empty, no dirty files)
            "",  # git tag --list (for prior tags in _collect_commits_range)
            "",  # git log (no commits in range)
            "",  # git push origin releases/2026.4.14.1
            "",  # git tag 2026.4.14.1 <sha>
            "",  # git push origin refs/tags/2026.4.14.1
        ]
    )
    mock_run.side_effect = lambda cmd, **kw: next(outputs, "")

    resource = make_resource()
    returned_version, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert returned_version.version == version_str
    assert metadata["action"] == "create"

    # Verify tag was created pointing at pre_bump_sha
    tag_calls = [
        c
        for c in mock_run.call_args_list
        if "tag" in c.args[0] and pre_bump_sha in c.args[0]
    ]
    assert tag_calls, "Expected git tag call with pre_bump_sha"


@patch("concourse._run")
def test_create_release_retrigger_with_matching_tag_is_idempotent(mock_run, tmp_path):
    """A retriggered create action for an already-tagged version is a no-op.

    Concourse re-triggers the build job whenever fetch_new_versions returns a
    changed ReleaseVersion -- including when only commit_count/authors shift
    because new commits landed on the tracked branch while a release was
    still in flight, even though the release `version` string itself is
    unchanged. Without this guard, retrying `create` for a version that
    already has a matching tag crashes on git tag's "already exists" error.
    """
    version_str = "2026.7.22.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "abc1234" * 5

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return pre_bump_sha
        if "tag" in cmd and "--list" in cmd:
            return version_str  # the tag already exists remotely
        if cmd[:3] == ["git", "rev-list", "-n1"]:
            return pre_bump_sha  # existing tag points at the same commit
        if "status" in cmd:
            return ""
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource()
    returned_version, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert returned_version.version == version_str
    assert metadata["action"] == "create"

    tag_create_calls = [
        c for c in mock_run.call_args_list if c.args[0][:3] == ["git", "tag", "-a"]
    ]
    assert not tag_create_calls, "Should not re-create an already-existing tag"
    push_tag_calls = [
        c for c in mock_run.call_args_list if "refs/tags/" in " ".join(c.args[0])
    ]
    assert not push_tag_calls, "Should not push a tag that already exists"
    branch_push_calls = [
        c
        for c in mock_run.call_args_list
        if c.args[0][:3] == ["git", "push", "origin"]
        and c.args[0][3].startswith("releases/")
    ]
    assert not branch_push_calls, "A retrigger should skip the branch push too"


@patch("concourse._run")
def test_create_release_retrigger_since_ref_excludes_self(mock_run, tmp_path):
    """since_ref on a retrigger is the prior release tag, not `version` itself.

    prior_tags[-1] is `version` once it is already tagged (it's the latest
    tag), so naively using that as since_ref would make the commit/changelog
    range for this release measure zero commits against itself instead of
    the actual prior release.
    """
    version_str = "2026.7.22.1"
    prior_version = "2026.7.21.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "abc1234" * 5

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return pre_bump_sha
        if "tag" in cmd and "--list" in cmd:
            return f"{prior_version}\n{version_str}"
        if cmd[:3] == ["git", "rev-list", "-n1"]:
            return pre_bump_sha  # existing tag points at the same commit
        if "status" in cmd:
            return ""
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource()
    returned_version, _ = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert returned_version.since == prior_version


@patch("concourse._run")
def test_create_release_tag_conflict_with_different_sha_raises(mock_run, tmp_path):
    """A tag pointing at a different commit is a real conflict, not a retrigger."""
    version_str = "2026.7.22.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "abc1234" * 5
    other_sha = "def5678" * 5

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return pre_bump_sha
        if "tag" in cmd and "--list" in cmd:
            return version_str
        if cmd[:3] == ["git", "rev-list", "-n1"]:
            return other_sha  # existing tag points at a different commit
        if "status" in cmd:
            return ""
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource()
    with pytest.raises(RuntimeError, match="does not match the commit being released"):
        resource.publish_new_version(
            tmp_path,
            MagicMock(),
            action="create",
            repo_dir="app-source",
            version_file="release/version",
        )


@patch("concourse._run")
def test_publish_new_version_create_stashes_dirty_files(mock_run, tmp_path):
    """Dirty files from bump_version_task are stashed before git reset --hard.

    Stashed changes are popped after the reset so they survive and land in the
    release commit.
    """
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "prebump1" * 5
    stash_calls: list[list[str]] = []
    status_call_count = 0

    def fake_run(cmd, **kw):
        nonlocal status_call_count
        if "stash" in cmd:
            stash_calls.append(list(cmd))
            return ""
        if "status" in cmd and "--porcelain" in cmd:
            status_call_count += 1
            # First call (pre-reset check): return dirty marker to trigger stash.
            # Second call (staging check after stash pop): return dirty to trigger add.
            return "M pyproject.toml" if status_call_count <= 2 else ""
        if "rev-parse" in cmd:
            return pre_bump_sha
        if "tag" in cmd and "--list" in cmd:
            return ""
        if "log" in cmd:
            return ""
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource()
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    stash_push = [c for c in stash_calls if "push" in c]
    stash_pop = [c for c in stash_calls if "pop" in c]
    assert stash_push, "Expected git stash push when dirty files are present"
    assert stash_pop, "Expected git stash pop to restore version-bump changes"
    # stash push must precede stash pop
    push_idx = stash_calls.index(stash_push[0])
    pop_idx = stash_calls.index(stash_pop[0])
    assert push_idx < pop_idx, "git stash push must precede git stash pop"


@patch("concourse._run")
def test_publish_new_version_create_with_hotfix(mock_run, tmp_path):
    """Hotfix commit is cherry-picked before the release commit."""
    version_str = "2026.4.14.1"
    hotfix_sha = "hotfix12" * 5

    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "prebump1" * 5
    post_cherry_sha = "postchry" * 5
    call_order = []

    def track_run(cmd, **kw):
        call_order.append(cmd[1] if len(cmd) > 1 else cmd[0])
        if "rev-parse" in cmd and "HEAD" in cmd:
            # Return pre_bump_sha on first call (before cherry-pick), post after
            return post_cherry_sha if "cherry-pick" in call_order else pre_bump_sha
        if "status" in cmd:
            return ""
        if "tag" in cmd and "--list" in cmd:
            return ""
        if "log" in cmd:
            return ""
        return pre_bump_sha  # default fallback for any other rev-parse

    mock_run.side_effect = track_run

    resource = make_resource()
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
        commit_hash=hotfix_sha,
    )

    cherry_idx = next(i for i, c in enumerate(call_order) if c == "cherry-pick")
    # The first rev-parse HEAD (pre_bump_sha) comes before cherry-pick
    rev_parse_indices = [i for i, c in enumerate(call_order) if c == "rev-parse"]
    assert rev_parse_indices[0] < cherry_idx, (
        "pre_bump_sha rev-parse must precede cherry-pick"
    )


@patch("concourse._run")
def test_publish_new_version_finish(mock_run, tmp_path):
    """Finish action: merges release branch into the configured branch."""
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    mock_run.return_value = "mergesha1" * 5

    resource = make_resource()
    returned_version, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="finish",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert returned_version.version == version_str
    assert metadata["action"] == "finish"

    all_cmds = [c.args[0] for c in mock_run.call_args_list]

    merge_cmds = [c for c in all_cmds if "merge" in c]
    assert merge_cmds, "Expected a git merge call"
    merge_cmd = merge_cmds[0]
    assert f"origin/releases/{version_str}" in merge_cmd
    assert "--no-ff" in merge_cmd

    push_cmds = [c for c in all_cmds if "push" in c]
    assert push_cmds, "Expected a git push call"
    # Push should target the configured branch (main by default)
    assert any("main" in " ".join(c) for c in push_cmds), (
        "Push should target the configured branch"
    )
    # Release branch must be deleted so check stops seeing a release in flight
    delete_cmds = [c for c in push_cmds if "--delete" in c]
    assert any(f"releases/{version_str}" in " ".join(c) for c in delete_cmds), (
        "Finish must delete the release branch from the remote"
    )

    # The release branch fetch must use an explicit refspec so
    # refs/remotes/origin/releases/<version> exists locally regardless of the
    # checkout's pre-existing remote.origin.fetch config -- a plain branch-name
    # fetch silently lands in FETCH_HEAD with no ref to merge against when the
    # branch was never part of that config (concourse/concourse-release-resource
    # production incident: "not something we can merge" on origin/releases/...).
    fetch_cmds = [c for c in all_cmds if "fetch" in c]
    assert fetch_cmds, "Expected a git fetch call"
    fetch_cmd = fetch_cmds[0]
    assert (
        f"+refs/heads/releases/{version_str}:refs/remotes/origin/releases/{version_str}"
        in fetch_cmd
    ), "Release branch fetch must use an explicit refspec, not a plain branch name"


@patch("concourse._run")
def test_publish_new_version_finish_uses_configured_branch(mock_run, tmp_path):
    """Finish respects the source-level branch setting, not always 'main'."""
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text("2026.4.14.1")
    (tmp_path / "app-source").mkdir()

    mock_run.return_value = "mergesha1" * 5

    resource = make_resource(branch="develop")
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="finish",
        repo_dir="app-source",
        version_file="release/version",
    )

    all_cmds = [c.args[0] for c in mock_run.call_args_list]
    push_cmds = [c for c in all_cmds if "push" in c]
    assert any("develop" in " ".join(c) for c in push_cmds), (
        "Push should target the configured branch 'develop'"
    )


# ---------------------------------------------------------------------------
# Changelog integration in out (create)
# ---------------------------------------------------------------------------


@patch("concourse._run")
def test_create_writes_cumulative_changelog(mock_run, tmp_path):
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)

    app_dir = tmp_path / "app-source"
    app_dir.mkdir()
    changelog_path = app_dir / "CHANGELOG.md"

    def fake_run(cmd, **kw):
        if "status" in cmd:
            return ""
        if "tag" in cmd and "--list" in cmd:
            return ""
        if "log" in cmd:
            return ""
        if "rev-parse" in cmd:
            return "sha1234" * 7
        if "diff" in cmd and "--cached" in cmd:
            return ""  # nothing staged yet for changelog check
        return ""

    mock_run.side_effect = fake_run

    resource = make_resource(
        changelog_style="cumulative", changelog_file="CHANGELOG.md"
    )
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert changelog_path.exists(), "CHANGELOG.md should be created"
    content = changelog_path.read_text()
    assert f"## [{version_str}]" in content
    assert "Keep a Changelog" in content


@patch("concourse._run")
def test_create_writes_per_release_changelog(mock_run, tmp_path):
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)

    app_dir = tmp_path / "app-source"
    app_dir.mkdir()

    def fake_run(cmd, **kw):
        if "status" in cmd:
            return ""
        if "tag" in cmd and "--list" in cmd:
            return ""
        if "log" in cmd:
            return ""
        if "rev-parse" in cmd:
            return "sha1234" * 7
        return ""

    mock_run.side_effect = fake_run

    resource = make_resource(changelog_style="per_release", changelog_dir="releases")
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    release_file = app_dir / "releases" / f"RELEASE_{version_str}.md"
    assert release_file.exists(), f"Expected {release_file}"
    assert f"## [{version_str}]" in release_file.read_text()


# ---------------------------------------------------------------------------
# _get_in_flight_release_version
# ---------------------------------------------------------------------------


@patch("concourse._run")
def test_get_in_flight_release_version_none_when_no_branches(mock_run, tmp_path):
    """Returns None when git branch -r lists no releases/ branches."""
    mock_run.return_value = ""
    result = _get_in_flight_release_version(tmp_path, env={})
    assert result is None


@patch("concourse._run")
def test_get_in_flight_release_version_returns_latest(mock_run, tmp_path):
    """Returns the most recent date-format version when multiple branches exist."""
    mock_run.return_value = (
        "aaa\trefs/heads/releases/2026.4.10.1\nbbb\trefs/heads/releases/2026.4.14.1\n"
    )
    result = _get_in_flight_release_version(tmp_path, env={})
    assert result == "2026.4.14.1"


@patch("concourse._run")
def test_get_in_flight_release_version_ignores_non_date_branches(mock_run, tmp_path):
    """Non date-format branch names under releases/ are ignored."""
    mock_run.return_value = (
        "aaa\trefs/heads/releases/my-feature\nbbb\trefs/heads/releases/2026.4.14.1\n"
    )
    result = _get_in_flight_release_version(tmp_path, env={})
    assert result == "2026.4.14.1"


@patch("concourse._run")
def test_get_in_flight_release_version_asks_the_remote(mock_run, tmp_path):
    """Detection must query the remote, not local origin/* tracking refs.

    An ``out`` step's workspace checkout is produced by the ``git`` resource
    and only tracks the configured branch, so ``git branch -r`` would report
    no in-flight release there no matter how many exist on the remote.
    """
    mock_run.return_value = ""
    _get_in_flight_release_version(tmp_path, env={})
    cmd = mock_run.call_args.args[0]
    assert cmd[:2] == ["git", "ls-remote"]
    assert "refs/heads/releases/*" in cmd


# ---------------------------------------------------------------------------
# fetch_new_versions — in-flight releases are reported, not obeyed
# ---------------------------------------------------------------------------


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_advances_past_an_in_flight_release(
    mock_tmpdir, mock_run, tmp_path
):
    """An unfinished release must never freeze check.

    Pinning check to the in-flight version meant one failed `action=finish`
    stopped the resource from ever reporting a new version or new commits
    again -- silently, for as long as the abandoned `releases/X` branch sat on
    the remote. Check now advances and reports the in-flight release instead,
    leaving it to `action=create` to supersede it.
    """
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    new_head_sha = "newcommit1" * 4  # commit pushed to main after the cut

    mock_run.side_effect = _check_git_stub(
        tags=["2026.4.10.1", "2026.4.14.1"],
        head_sha=new_head_sha,
        in_flight=["2026.4.14.1"],
        logs={
            f"2026.4.14.1..{new_head_sha}": _log(
                ("dev@example.com", "feat: landed while the release was stuck")
            )
        },
    )
    resource = make_resource()

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    v = versions[0]
    assert v.version == "2026.4.14.2"
    assert v.head_sha == new_head_sha
    assert v.since == "2026.4.14.1"
    assert v.commit_count == "1"
    assert v.in_flight == "2026.4.14.1"


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_clears_in_flight_after_branch_deleted(
    mock_tmpdir, mock_run, tmp_path
):
    """Once the release branch is gone, nothing is in flight."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "newcommit1" * 4

    mock_run.side_effect = _check_git_stub(
        tags=["2026.4.14.1"],
        head_sha=head_sha,
        logs={
            f"2026.4.14.1..{head_sha}": _log(
                ("dev@example.com", "feat: a thing"),
                ("alice@example.com", "fix: another"),
            )
        },
    )
    resource = make_resource()

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    assert versions[0].version == "2026.4.14.2"
    assert versions[0].head_sha == head_sha
    assert versions[0].in_flight == ""


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_ignores_its_own_release_commits(
    mock_tmpdir, mock_run, tmp_path
):
    """A finished release must not read as two commits waiting to be released.

    `finish` lands "Release X" and "Merge releases/X" on the tracked branch,
    on top of a tag planted on the pre-bump HEAD -- so `<tag>..origin/main` is
    never empty once a release completes.
    """
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "mergecommit" * 4

    mock_run.side_effect = _check_git_stub(
        tags=["2026.4.10.1", "2026.4.14.1"],
        head_sha=head_sha,
        logs={
            f"2026.4.14.1..{head_sha}": _log(
                ("ci@example.com", "Merge releases/2026.4.14.1"),
                ("ci@example.com", "Release 2026.4.14.1"),
            ),
            f"2026.4.10.1..{head_sha}": _log(("dev@example.com", "feat: real work")),
        },
    )
    resource = make_resource()

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    # Still the released version -- not 2026.4.14.2 containing its own bookkeeping.
    assert versions[0].version == "2026.4.14.1"
    assert versions[0].commit_count == "1"


# ---------------------------------------------------------------------------
# publish_new_version — action=abandon
# ---------------------------------------------------------------------------


@patch("concourse._run")
def test_publish_new_version_abandon(mock_run, tmp_path):
    """Abandon deletes the release branch and tag from the remote."""
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    main_sha = "mainshaa1" * 5
    calls: list[list[str]] = []

    def track_run(cmd, **kw):
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            return main_sha
        return ""

    mock_run.side_effect = track_run

    resource = make_resource()
    returned_version, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="abandon",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert returned_version.version == version_str
    assert metadata["action"] == "abandon"
    assert returned_version.head_sha == main_sha

    push_cmds = [c for c in calls if "push" in c and "--delete" in c]
    deleted_refs = {" ".join(c) for c in push_cmds}
    assert any(f"releases/{version_str}" in r for r in deleted_refs), (
        "Abandon must delete the releases/ branch"
    )
    assert any(f"refs/tags/{version_str}" in r for r in deleted_refs), (
        "Abandon must delete the version tag"
    )


@patch("concourse._run")
def test_publish_new_version_abandon_idempotent(mock_run, tmp_path):
    """Abandon is idempotent — CalledProcessError from missing refs is silenced."""
    import subprocess

    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    main_sha = "mainshaa1" * 5

    def track_run(cmd, **kw):
        if "push" in cmd and "--delete" in cmd:
            raise subprocess.CalledProcessError(1, cmd, "", "remote ref not found")
        if "rev-parse" in cmd:
            return main_sha
        return ""

    mock_run.side_effect = track_run

    resource = make_resource()
    # Must not raise even though all push --delete calls fail
    returned_version, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="abandon",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert returned_version.version == version_str
    assert metadata["action"] == "abandon"


@patch("concourse._run")
def test_publish_new_version_invalid_action_includes_abandon(mock_run, tmp_path):
    """Error message for invalid action mentions all three valid actions."""
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text("2026.4.14.1")

    resource = make_resource()
    with pytest.raises(ValueError, match="abandon"):
        resource.publish_new_version(
            tmp_path,
            MagicMock(),
            action="deploy",  # type: ignore[arg-type]
            repo_dir="app-source",
            version_file="release/version",
        )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_github_token_defaults_to_static_access_token():
    resource = make_resource(access_token="static-tok")
    assert resource.github_token == "static-tok"


def test_github_token_is_none_when_unauthenticated():
    assert make_resource().github_token is None


@patch("concourse.GithubIntegration")
@patch("concourse.Auth")
def test_github_token_mints_installation_token(mock_auth, mock_integration):
    mock_integration.return_value.get_access_token.return_value.token = "ghs_minted"

    resource = make_resource(
        auth_method="app",
        access_token=None,
        app_id="810341",
        app_installation_id="46690837",
        private_ssh_key="-----BEGIN RSA PRIVATE KEY-----",
    )

    assert resource.github_token == "ghs_minted"
    mock_auth.AppAuth.assert_called_once_with(
        "810341", "-----BEGIN RSA PRIVATE KEY-----"
    )
    # Concourse source values arrive as strings; PyGithub asserts an int here.
    mock_integration.return_value.get_access_token.assert_called_once_with(46690837)


@patch("concourse.GithubIntegration")
@patch("concourse.Auth")
def test_installation_token_is_minted_once_and_cached(mock_auth, mock_integration):
    """Repeated use within one invocation must not burn extra API calls."""
    mock_integration.return_value.get_access_token.return_value.token = "ghs_minted"

    resource = make_resource(
        auth_method="app",
        app_id="810341",
        app_installation_id="46690837",
        private_ssh_key="key",
    )
    tokens = {resource.github_token for _ in range(3)}

    assert tokens == {"ghs_minted"}
    assert mock_integration.return_value.get_access_token.call_count == 1


@patch("concourse.GithubIntegration")
@patch("concourse.Auth")
def test_app_auth_ignores_stale_access_token(mock_auth, mock_integration):
    """An access_token left in source must not shadow app auth."""
    mock_integration.return_value.get_access_token.return_value.token = "ghs_minted"

    resource = make_resource(
        auth_method="app",
        access_token="expired-pat",
        app_id="810341",
        app_installation_id="46690837",
        private_ssh_key="key",
    )

    assert resource.github_token == "ghs_minted"


# ---------------------------------------------------------------------------
# _authed_uri
# ---------------------------------------------------------------------------


def test_authed_uri_embeds_token_for_https():
    assert (
        _authed_uri("https://github.com/mitodl/my-app.git", "tok")
        == "https://x-access-token:tok@github.com/mitodl/my-app.git"
    )


def test_authed_uri_replaces_existing_credentials():
    assert (
        _authed_uri("https://x-access-token:old@github.com/mitodl/my-app.git", "new")
        == "https://x-access-token:new@github.com/mitodl/my-app.git"
    )


def test_authed_uri_preserves_non_default_port():
    assert (
        _authed_uri("https://github.example.com:8443/mitodl/my-app.git", "tok")
        == "https://x-access-token:tok@github.example.com:8443/mitodl/my-app.git"
    )


@pytest.mark.parametrize(
    ("uri", "token"),
    [
        ("git@github.com:mitodl/my-app.git", "tok"),  # SSH remote
        ("ssh://git@github.com/mitodl/my-app.git", "tok"),  # SSH remote
        ("https://github.com/mitodl/my-app.git", None),  # no token
        ("https://github.com/mitodl/my-app.git", ""),  # no token
    ],
)
def test_authed_uri_returns_uri_unchanged(uri, token):
    assert _authed_uri(uri, token) == uri


# ---------------------------------------------------------------------------
# Token plumbing into git operations
# ---------------------------------------------------------------------------


@patch("concourse._run")
def test_clone_embeds_token_and_redacts_it(mock_run):
    _clone(
        "https://github.com/mitodl/my-app.git",
        Path("/tmp/repo"),  # noqa: S108
        env={},
        access_token="tok",
    )

    clone_cmd = mock_run.call_args_list[0]
    authed = "https://x-access-token:tok@github.com/mitodl/my-app.git"
    assert authed in clone_cmd.args[0]
    assert clone_cmd.kwargs["redact"] == "tok"


@patch("concourse.subprocess.run")
def test_run_redacts_the_token_from_the_raised_cmd(mock_subprocess_run):
    """CalledProcessError stringifies cmd, so an authed URL would leak there."""
    mock_subprocess_run.return_value = MagicMock(
        returncode=128, stdout="", stderr="fatal: repository not found"
    )
    secret = "ghs_installationtoken"
    authed = f"https://x-access-token:{secret}@github.com/mitodl/my-app.git"

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _run(["git", "clone", authed, "/tmp/repo"], redact=secret)  # noqa: S108

    assert secret not in str(excinfo.value)
    assert "x-access-token:***@github.com" in excinfo.value.cmd[2]


@patch("concourse.subprocess.run")
def test_run_leaves_cmd_alone_without_redact(mock_subprocess_run):
    mock_subprocess_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _run(["git", "status"])

    assert excinfo.value.cmd == ["git", "status"]


@patch("concourse._run")
def test_clone_leaves_ssh_uri_alone(mock_run):
    _clone(
        "git@github.com:mitodl/my-app.git",
        Path("/tmp/repo"),  # noqa: S108
        env={},
        access_token="tok",
    )

    assert "git@github.com:mitodl/my-app.git" in mock_run.call_args_list[0].args[0]


@patch("concourse.GithubIntegration")
@patch("concourse.Auth")
@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_check_clones_with_installation_token(
    mock_tmpdir, mock_run, mock_auth, mock_integration, tmp_path
):
    """App auth must reach the clone, so a private repo is readable without SSH."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    mock_integration.return_value.get_access_token.return_value.token = "ghs_minted"
    mock_run.return_value = ""

    resource = make_resource(
        auth_method="app",
        app_id="810341",
        app_installation_id="46690837",
        private_ssh_key="key",
    )
    resource.fetch_new_versions(None)

    clone_cmd = next(
        call for call in mock_run.call_args_list if "clone" in call.args[0]
    )
    assert (
        "https://x-access-token:ghs_minted@github.com/mitodl/my-app.git"
        in clone_cmd.args[0]
    )


# ---------------------------------------------------------------------------
# Release-machinery commit filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "Release 2026.4.14.1",
        "Release 2026.12.1.3",
        "Merge releases/2026.4.14.1",
    ],
)
def test_is_release_machinery_matches_this_resources_own_commits(subject):
    assert _is_release_machinery(subject)


@pytest.mark.parametrize(
    "subject",
    [
        "Release the kraken",
        "feat: release notes page",
        "Merge releases/my-feature",
        "Merge pull request #12 from mitodl/fix",
        "Release 2026.4.14.1 was broken, reverting",
    ],
)
def test_is_release_machinery_leaves_real_commits_alone(subject):
    assert not _is_release_machinery(subject)


def test_parse_commit_log_drops_release_machinery_commits():
    """A release's checklist must not list the previous release's bookkeeping."""
    output = (
        "aaa111|ci@example.com|Concourse CI|Merge releases/2026.4.14.1\n"
        "bbb222|ci@example.com|Concourse CI|Release 2026.4.14.1\n"
        "ccc333|dev@example.com|Dev|feat: something real\n"
    )
    commits = _parse_commit_log(output)
    assert [c["message"] for c in commits] == ["feat: something real"]


@patch("concourse._run")
def test_commit_info_range_excludes_release_machinery(mock_run, tmp_path):
    """Machinery commits must not inflate commit_count or the author list."""
    mock_run.return_value = (
        "ci@example.com|Merge releases/2026.4.14.1\n"
        "ci@example.com|Release 2026.4.14.1\n"
        "dev@example.com|feat: something real\n"
    )
    count, authors = _commit_info_range(tmp_path, "2026.4.14.1", "headsha", env={})
    assert count == 1
    assert authors == "dev@example.com"


@patch("concourse._run")
def test_commit_info_range_keeps_subjects_containing_a_pipe(mock_run, tmp_path):
    """%s is last and split with maxsplit=1, so a piped subject stays intact."""
    mock_run.return_value = "dev@example.com|feat: support a|b syntax\n"
    count, authors = _commit_info_range(tmp_path, "", "headsha", env={})
    assert count == 1
    assert authors == "dev@example.com"


# ---------------------------------------------------------------------------
# publish_new_version — create supersedes an in-flight release
# ---------------------------------------------------------------------------


class _FakeRemote:
    """A stateful `_run` stub that models ref deletion actually taking effect.

    `_assert_refs_deleted` re-queries the remote after a delete, so a stub that
    always reports the same refs would make every supersede look like a failed
    deletion.
    """

    def __init__(self, *, branches=(), tags=(), head="prebump1" * 5):
        self.branches = set(branches)
        self.tags = set(tags)
        self.head = head
        self.commands: list[str] = []

    def __call__(self, cmd, **_kwargs):
        self.commands.append(" ".join(cmd))
        if cmd[:2] == ["git", "ls-remote"]:
            return self._ls_remote(cmd[-1])
        if cmd[:4] == ["git", "push", "origin", "--delete"]:
            ref = cmd[4]
            if ref.startswith("refs/tags/"):
                self.tags.discard(ref[len("refs/tags/") :])
            else:
                self.branches.discard(ref)
            return ""
        if cmd[:2] == ["git", "rev-parse"]:
            return self.head
        if "tag" in cmd and "--list" in cmd:
            return "\n".join(sorted(self.tags))
        if cmd[:3] == ["git", "rev-list", "-n1"]:
            return "othersha" * 5
        return ""

    def _ls_remote(self, pattern: str) -> str:
        if pattern.startswith("refs/tags/"):
            name = pattern[len("refs/tags/") :]
            return f"aaa\trefs/tags/{name}\n" if name in self.tags else ""
        prefix = "refs/heads/"
        if pattern.endswith("*"):
            stem = pattern[len(prefix) : -1]
            return "".join(
                f"aaa\t{prefix}{b}\n"
                for b in sorted(self.branches)
                if b.startswith(stem)
            )
        name = pattern[len(prefix) :]
        return f"aaa\t{prefix}{name}\n" if name in self.branches else ""


@patch("concourse.ReleaseResource._reached_production", return_value=False)
@patch("concourse._run")
def test_create_supersedes_a_release_that_never_shipped(
    mock_run, _mock_shipped, tmp_path
):
    """A cut that never reached production is discarded branch and tag.

    Its tag would otherwise sit between the real releases and corrupt the
    `since` boundary of every later release.
    """
    version_str = "2026.4.14.2"
    stale = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    remote = _FakeRemote(branches={f"releases/{stale}"}, tags={stale})
    mock_run.side_effect = remote
    resource = make_resource()
    returned_version, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    all_cmds = remote.commands
    assert any("--delete" in c and f"releases/{stale}" in c for c in all_cmds), (
        "Must delete the superseded release branch"
    )
    assert any("--delete" in c and f"refs/tags/{stale}" in c for c in all_cmds), (
        "An unshipped release's tag must go with its branch"
    )
    assert metadata["superseded"] == stale
    assert metadata["superseded_tag"] == "deleted"
    assert returned_version.in_flight == version_str


@patch("concourse.ReleaseResource._reached_production", return_value=True)
@patch("concourse._run")
def test_create_keeps_the_tag_of_a_superseded_release_that_shipped(
    mock_run, _mock_shipped, tmp_path
):
    """A release whose finish failed after shipping keeps its tag.

    ol-analytics-api sat exactly here: 2026.8.3.1 deployed to production
    repeatedly while its releases/ branch never merged. That tag is the only
    thing tying what production runs back to a commit, so superseding must
    take the branch and leave the tag -- which also makes it the correct
    `since` boundary for the new release, whose predecessor really is live.
    """
    version_str = "2026.4.14.2"
    stale = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    remote = _FakeRemote(branches={f"releases/{stale}"}, tags={stale})
    mock_run.side_effect = remote
    resource = make_resource()
    _, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    all_cmds = remote.commands
    assert any("--delete" in c and f"releases/{stale}" in c for c in all_cmds), (
        "Must still delete the superseded release branch"
    )
    assert not any("--delete" in c and f"refs/tags/{stale}" in c for c in all_cmds), (
        "Must not delete the tag for code that reached production"
    )
    assert not any(c.startswith("git tag -d") for c in all_cmds), (
        "Must not delete the shipped tag locally either -- it is the since boundary"
    )
    assert metadata["superseded_tag"] == "kept"


@patch("concourse._run")
def test_create_does_not_supersede_itself(mock_run, tmp_path):
    """A retriggered create for the in-flight version must not delete its own refs."""
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    pre_bump_sha = "prebump1" * 5

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "ls-remote"]:
            return f"abc\trefs/heads/releases/{version_str}\n"
        if cmd[:2] == ["git", "rev-parse"]:
            return pre_bump_sha
        if "tag" in cmd and "--list" in cmd:
            return version_str
        if cmd[:3] == ["git", "rev-list", "-n1"]:
            return pre_bump_sha
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource()
    _, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    all_cmds = [" ".join(c.args[0]) for c in mock_run.call_args_list]
    assert not any("--delete" in c for c in all_cmds), (
        "A retrigger must not delete the release it is re-creating"
    )
    assert "superseded" not in metadata


# ---------------------------------------------------------------------------
# publish_new_version — finish is idempotent
# ---------------------------------------------------------------------------


@patch("concourse._run")
def test_finish_is_a_noop_when_the_release_branch_is_gone(mock_run, tmp_path):
    """Re-running finish must succeed rather than fail on a missing branch.

    The production job legitimately re-runs without a new release. That used
    to fail every time, which is why the pipeline wrapped this put in a
    `try` -- and that `try` then silently swallowed the genuine finish
    failure that froze the resource.
    """
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    head = "mainhead" * 5

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "ls-remote"]:
            return ""  # branch already deleted by a prior successful finish
        if cmd[:2] == ["git", "rev-parse"]:
            return head
        return ""

    mock_run.side_effect = fake_run
    resource = make_resource()
    returned_version, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="finish",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert returned_version.head_sha == head
    assert metadata["action"] == "finish"
    all_cmds = [" ".join(c.args[0]) for c in mock_run.call_args_list]
    assert not any("merge" in c for c in all_cmds), (
        "Nothing to merge when the release branch is already gone"
    )
    assert not any("--delete" in c for c in all_cmds)


# ---------------------------------------------------------------------------
# _reached_production — did an in-flight release actually ship?
# ---------------------------------------------------------------------------


def _deployments(*states: str):
    """Build a fake get_deployments() result with the given status states."""
    deployment = MagicMock()
    deployment.get_statuses.return_value = [MagicMock(state=state) for state in states]
    return [deployment]


def test_reached_production_is_true_without_credentials():
    """Unknowable means "keep the tag" -- never destroy the only prod marker."""
    resource = make_resource(access_token=None, repository=None)
    assert resource._reached_production("2026.4.14.1") is True


@patch("concourse.Github")
def test_reached_production_true_on_a_successful_deployment(mock_github):
    mock_github.return_value.get_repo.return_value.get_deployments.return_value = (
        _deployments("in_progress", "success")
    )
    resource = make_resource(access_token="tok", repository="mitodl/my-app")
    assert resource._reached_production("2026.4.14.1") is True


@patch("concourse.Github")
def test_reached_production_false_when_no_deployment_succeeded(mock_github):
    """A cut that only ever failed to deploy is safe to discard entirely."""
    mock_github.return_value.get_repo.return_value.get_deployments.return_value = (
        _deployments("failure", "error")
    )
    resource = make_resource(access_token="tok", repository="mitodl/my-app")
    assert resource._reached_production("2026.4.14.1") is False


@patch("concourse.Github")
def test_reached_production_false_when_never_deployed(mock_github):
    mock_github.return_value.get_repo.return_value.get_deployments.return_value = []
    resource = make_resource(access_token="tok", repository="mitodl/my-app")
    assert resource._reached_production("2026.4.14.1") is False


@patch("concourse.Github")
def test_reached_production_queries_the_configured_environment(mock_github):
    """The environment name must match the pipeline's GitHub Deployment."""
    get_deployments = mock_github.return_value.get_repo.return_value.get_deployments
    get_deployments.return_value = []
    resource = make_resource(
        access_token="tok",
        repository="mitodl/my-app",
        production_environment="prod",
    )
    resource._reached_production("2026.4.14.1")
    get_deployments.assert_called_once_with(ref="2026.4.14.1", environment="prod")


@patch("concourse.Github", side_effect=RuntimeError("GitHub is down"))
def test_reached_production_is_true_when_the_api_fails(_mock_github):
    """An API failure must not be read as "never shipped" and delete the tag."""
    resource = make_resource(access_token="tok", repository="mitodl/my-app")
    assert resource._reached_production("2026.4.14.1") is True


# ---------------------------------------------------------------------------
# create — partial cuts, stale-ref survival, and version ordering
# ---------------------------------------------------------------------------


class _UndeletableSet(set):  # type: ignore[type-arg]
    """A set whose discard() is a no-op, standing in for a protected ref."""

    def discard(self, value):
        return


@patch("concourse._run")
def test_create_clears_a_branch_left_by_a_failed_tag_push(mock_run, tmp_path):
    """A cut that pushed its branch but not its tag must not wedge retries.

    `_create_release` pushes the branch before the tag, so a failed tag push
    leaves `releases/X` present with no X tag. That is not a completed release
    -- the genuine retrigger path is guarded by `version in prior_tags` -- but
    the stale branch still holds the previous attempt's release commit, so this
    run's push would be rejected as a non-fast-forward and every retry after it
    would fail identically.
    """
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    # Branch present, tag absent -- the partial-cut state.
    remote = _FakeRemote(branches={f"releases/{version_str}"}, tags=set())
    mock_run.side_effect = remote

    resource = make_resource()
    _, metadata = resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert any(
        "--delete" in c and f"releases/{version_str}" in c for c in remote.commands
    ), "Must clear the partially-created release branch before re-cutting"
    # Not a supersede -- it is the same version being re-cut.
    assert "superseded" not in metadata
    # The branch is pushed again as part of the fresh cut.
    assert any(c == f"git push origin releases/{version_str}" for c in remote.commands)


@patch("concourse._run")
def test_create_leaves_a_complete_cut_alone(mock_run, tmp_path):
    """Branch *and* tag present is a real retrigger -- delete nothing."""
    version_str = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    remote = _FakeRemote(
        branches={f"releases/{version_str}"}, tags={version_str}, head="othersha" * 5
    )
    mock_run.side_effect = remote

    resource = make_resource()
    resource.publish_new_version(
        tmp_path,
        MagicMock(),
        action="create",
        repo_dir="app-source",
        version_file="release/version",
    )

    assert not any("--delete" in c for c in remote.commands), (
        "A completed cut being retriggered must not have its refs deleted"
    )


@patch("concourse.ReleaseResource._reached_production", return_value=False)
@patch("concourse._run")
def test_create_fails_loudly_when_a_superseded_ref_survives(
    mock_run, _mock_shipped, tmp_path
):
    """A suppressed deletion failure must not be reported as a supersede.

    `_abandon_release` silences CalledProcessError to stay idempotent, which
    equally hides a protected ref or a transient remote failure. Continuing
    would leave a branch that a later check rediscovers as in-flight -- the
    exact failure this resource exists to prevent.
    """
    version_str = "2026.4.14.2"
    stale = "2026.4.14.1"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    remote = _FakeRemote(branches={f"releases/{stale}"}, tags={stale})
    # Deletion silently does nothing, as a protected ref would.
    remote.branches = _UndeletableSet(remote.branches)
    mock_run.side_effect = remote

    resource = make_resource()
    with pytest.raises(RuntimeError, match="Failed to delete"):
        resource.publish_new_version(
            tmp_path,
            MagicMock(),
            action="create",
            repo_dir="app-source",
            version_file="release/version",
        )


@patch("concourse._run")
def test_create_refuses_to_supersede_a_newer_release(mock_run, tmp_path):
    """An older build must not delete a newer release's refs.

    `create` binds the version Concourse resolved when the build was
    scheduled, so a delayed or concurrent build can carry a version older than
    the release now in flight.
    """
    version_str = "2026.4.14.1"
    newer = "2026.4.14.2"
    version_file = tmp_path / "release" / "version"
    version_file.parent.mkdir()
    version_file.write_text(version_str)
    (tmp_path / "app-source").mkdir()

    remote = _FakeRemote(branches={f"releases/{newer}"}, tags={newer})
    mock_run.side_effect = remote

    resource = make_resource()
    with pytest.raises(RuntimeError, match="Refusing to supersede"):
        resource.publish_new_version(
            tmp_path,
            MagicMock(),
            action="create",
            repo_dir="app-source",
            version_file="release/version",
        )

    assert not any("--delete" in c for c in remote.commands), (
        "Must not delete the newer release's refs before refusing"
    )
