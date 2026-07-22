"""Tests for resources/release/concourse.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from concourse import (
    ReleaseResource,
    ReleaseVersion,
    SEMVER_PATTERN,
    _build_changelog_entry,
    _build_checklist,
    _compute_next_version,
    _get_in_flight_release_version,
    _get_semver_tags,
    _parse_semver_tuple,
    _parse_version_tuple,
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
# _build_checklist
# ---------------------------------------------------------------------------


def test_build_checklist_with_prs():
    commits = [
        {
            "sha": "abc1234def5",
            "author": "dev@example.com",
            "message": "Fix bug",
            "pr_number": 42,
            "pr_title": "Fix the bug",
        }
    ]
    result = _build_checklist("2026.4.14.1", commits)
    assert "## Release 2026.4.14.1" in result
    assert "- [ ] **Fix the bug** (#42) by dev@example.com" in result
    assert "Closing this issue will trigger the production deployment" in result


def test_build_checklist_without_prs():
    commits = [
        {
            "sha": "abc1234def5",
            "author": "dev@example.com",
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
    assert "### Changes" in result


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
    outputs = ["", "", f"{head_sha}|dev@example.com|Initial commit"]
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


def _make_run_side_effects(
    tag_list: list[str],
    head_sha: str,
    tag_sha: str = "",
    in_flight_branch: str = "",
) -> list[str]:
    """Build a list of subprocess outputs for check's _run calls.

    Call order in _compute_versions:
      0  git clone
      1  git fetch --tags
      2  git tag --list
      3  git rev-parse origin/main  → head_sha
      4  git branch -r --list origin/releases/*  → in_flight_branch (empty = none)
      5  git rev-list -n1 <latest_tag>  (only when tags exist and no in-flight)
      6  git log --format=%ae  (commit range / all)
    """
    tags_output = "\n".join(tag_list)
    effects = [
        "",  # git clone
        "",  # git fetch --tags
        tags_output,  # git tag --list
        head_sha,  # git rev-parse origin/main
        in_flight_branch,  # git branch -r --list origin/releases/*
    ]
    if in_flight_branch:
        # In-flight path: rev-list -n1 in_flight_version + commit_info_range
        effects.append(tag_sha or head_sha)  # git rev-list -n1 in_flight_tag
        effects.append("dev@example.com")
    elif tag_list:
        effects.append(tag_sha or head_sha)  # git rev-list -n1 latest_tag
        if tag_sha != head_sha:
            effects.append("dev@example.com\nalice@example.com")
        else:
            effects.append("dev@example.com")
    else:
        effects.append("dev@example.com\nalice@example.com")  # commit_info_all
    return effects


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

    outputs = [
        "",  # 0: git clone
        "",  # 1: git fetch --tags
        "2026.4.10.1\n2026.4.14.1",  # 2: git tag --list
        head_sha,  # 3: git rev-parse origin/main
        "",  # 4: git branch -r (no in-flight)
        head_sha,  # 5: git rev-list -n1 2026.4.14.1 (same → tagged)
        "dev@example.com",  # 6: git log (commit_info_range)
    ]
    call_index = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal call_index
        out = outputs[call_index]
        call_index += 1
        return out

    mock_run.side_effect = run_side_effect
    resource = make_resource()
    versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    assert versions[0].version == "2026.4.14.1"
    assert versions[0].head_sha == head_sha


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_new_commits(mock_tmpdir, mock_run, tmp_path, monkeypatch):
    """When HEAD is ahead of the latest tag, return the next version."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "newcommit1" * 4
    tag_sha = "oldtagsha1" * 4

    outputs = [
        "",  # 0: git clone
        "",  # 1: git fetch --tags
        "2026.4.14.1",  # 2: git tag --list
        head_sha,  # 3: git rev-parse origin/main
        "",  # 4: git branch -r (no in-flight)
        tag_sha,  # 5: git rev-list -n1 2026.4.14.1
        "dev@example.com\nalice@example.com",  # 6: git log (commit_info_range)
    ]
    call_index = 0

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
            f"{head_sha}|dev@example.com|Fix bug",
            f"{'b' * 40}|alice@example.com|Add feature",
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
    outputs = ["", "", head_sha, f"{head_sha}|dev@example.com|Initial commit"]
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
    assert ".." not in "".join(c for c in log_cmd if c not in ["--format=%H|%ae|%s"])


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
        "  origin/releases/2026.4.10.1\n  origin/releases/2026.4.14.1\n"
    )
    result = _get_in_flight_release_version(tmp_path, env={})
    assert result == "2026.4.14.1"


@patch("concourse._run")
def test_get_in_flight_release_version_ignores_non_date_branches(mock_run, tmp_path):
    """Non date-format branch names under releases/ are ignored."""
    mock_run.return_value = (
        "  origin/releases/my-feature\n  origin/releases/2026.4.14.1\n"
    )
    result = _get_in_flight_release_version(tmp_path, env={})
    assert result == "2026.4.14.1"


# ---------------------------------------------------------------------------
# fetch_new_versions — in-flight blocking
# ---------------------------------------------------------------------------


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_blocks_while_release_in_flight(
    mock_tmpdir, mock_run, tmp_path, monkeypatch
):
    """Check returns the in-flight version even when new commits exist on main."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    in_flight_sha = "releasesha" * 4  # SHA the release tag points to
    new_head_sha = "newcommit1" * 4  # new commit pushed to main after the cut

    outputs = [
        "",  # 0: git clone
        "",  # 1: git fetch --tags
        "2026.4.10.1\n2026.4.14.1",  # 2: git tag --list (release tag exists)
        new_head_sha,  # 3: git rev-parse origin/main (new commit!)
        "  origin/releases/2026.4.14.1\n",  # 4: git branch -r → in-flight!
        in_flight_sha,  # 5: git rev-list -n1 2026.4.14.1
        "dev@example.com",  # 6: git log (commit_info_range)
    ]
    idx = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal idx
        out = outputs[idx]
        idx += 1
        return out

    mock_run.side_effect = run_side_effect
    resource = make_resource()

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    v = versions[0]
    # Must return the in-flight version, not a new one
    assert v.version == "2026.4.14.1"
    # head_sha is the tagged commit, not the new commit
    assert v.head_sha == in_flight_sha
    assert v.since == "2026.4.10.1"


@patch("concourse._run")
@patch("concourse.tempfile.TemporaryDirectory")
def test_fetch_new_versions_unblocked_after_branch_deleted(
    mock_tmpdir, mock_run, tmp_path, monkeypatch
):
    """After the release branch is deleted, check advances to the next version."""
    mock_tmpdir.return_value.__enter__.return_value = str(tmp_path)
    head_sha = "newcommit1" * 4
    tag_sha = "releasesha" * 4

    outputs = [
        "",  # 0: git clone
        "",  # 1: git fetch --tags
        "2026.4.14.1",  # 2: git tag --list
        head_sha,  # 3: git rev-parse origin/main
        "",  # 4: git branch -r → no in-flight branch
        tag_sha,  # 5: git rev-list -n1
        "dev@example.com\nalice@example.com",  # 6: git log
    ]
    idx = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal idx
        out = outputs[idx]
        idx += 1
        return out

    mock_run.side_effect = run_side_effect
    resource = make_resource()

    with patch("concourse.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = datetime(
            2026, 4, 14, tzinfo=UTC
        ).date()
        versions = resource.fetch_new_versions(None)

    assert len(versions) == 1
    # New commits → next version
    assert versions[0].version == "2026.4.14.2"
    assert versions[0].head_sha == head_sha


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
