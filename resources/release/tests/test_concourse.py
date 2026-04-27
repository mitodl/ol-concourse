"""Tests for resources/release/concourse.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from concourse import (
    ReleaseResource,
    ReleaseVersion,
    _build_changelog_entry,
    _build_checklist,
    _compute_next_version,
    _parse_version_tuple,
    _update_cumulative_changelog,
    CHANGELOG_HEADER,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_resource(**kwargs) -> ReleaseResource:
    defaults = {
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


def make_commits(n: int = 2) -> list[dict]:
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
    commits = [
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


# ---------------------------------------------------------------------------
# fetch_new_versions (check)
# ---------------------------------------------------------------------------


def _make_run_side_effects(
    tag_list: list[str], head_sha: str, tag_sha: str = ""
) -> list:
    """Build a list of subprocess outputs for check's _run calls."""
    # clone=no output; fetch tags=no output; tag --list; rev-parse HEAD; rev-list -n1
    tags_output = "\n".join(tag_list)
    effects = [
        "",  # git clone
        "",  # git fetch --tags
        tags_output,  # git tag --list
        head_sha,  # git rev-parse origin/main
    ]
    if tag_list:
        effects.append(tag_sha or head_sha)  # git rev-list -n1 latest_tag
        if tag_sha != head_sha:
            effects.append(  # git log --format=%ae (for commit_info_range)
                "dev@example.com\nalice@example.com"
            )
        else:
            # git log --format=%ae (for commit_info_range with prev_tag)
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
    outputs = ["", "", "", head_sha, "dev@example.com"]

    def run_side_effect(cmd, **kwargs):
        nonlocal call_index
        out = outputs[call_index % len(outputs)]
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
        "",
        "",
        "2026.4.10.1\n2026.4.14.1",
        head_sha,
        head_sha,
        "dev@example.com",
    ]
    call_index = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal call_index
        out = outputs[call_index % len(outputs)]
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
        "",
        "",
        "2026.4.14.1",
        head_sha,
        tag_sha,
        "dev@example.com\nalice@example.com",
    ]
    call_index = 0

    def run_side_effect(cmd, **kwargs):
        nonlocal call_index
        out = outputs[call_index % len(outputs)]
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
    # _clone calls _run twice (git clone + git fetch --tags), then _collect_commits
    # calls git log once using version.head_sha directly (no rev-parse needed).
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
            action="deploy",
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
            "",  # git checkout main  (new: ensure correct branch)
            "",  # git reset --hard origin/main  (new: sync with remote)
            pre_bump_sha,  # git rev-parse HEAD (pre-bump)
            "",  # git checkout -b release/2026.4.14.1
            "",  # git status --porcelain (empty — no dirty files)
            "",  # git tag --list (for prior tags in _collect_commits_range)
            "",  # git log (no commits in range)
            "",  # git push origin release/2026.4.14.1
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
    assert f"origin/release/{version_str}" in merge_cmd
    assert "--no-ff" in merge_cmd

    push_cmds = [c for c in all_cmds if "push" in c]
    assert push_cmds, "Expected a git push call"
    # Push should target the configured branch (main by default)
    assert any("main" in " ".join(c) for c in push_cmds), (
        "Push should target the configured branch"
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
