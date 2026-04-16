# concourse-release-resource

A Concourse CI resource type for git release orchestration, built with [concoursetools](https://concoursetools.readthedocs.io/).

Handles the full release lifecycle:
- **check** — detects unreleased commits and computes the next `YYYY.MM.DD.N` version
- **in** — generates commit checklist, changelog entry, and structured commit data
- **out** — creates the release branch/tag and changelog file, or merges the release branch back to the target branch

Part of the [release management modernisation](https://github.com/mitodl/hq/issues/7185) epic.
Tracks [ol-concourse#13](https://github.com/mitodl/ol-concourse/issues/13).

## Source Configuration

```yaml
resource_types:
  - name: release
    type: registry-image
    source:
      repository: mitodl/concourse-release-resource
      tag: latest

resources:
  - name: app-release
    type: release
    check_every: never          # triggered via webhook from the release bot
    webhook_token: ((release.webhook_token))
    source:
      uri: git@github.com:mitodl/my-app.git
      branch: main              # default: main
      private_key: ((github.private_key))
      access_token: ((github.token))   # optional; enables PR enrichment
      repository: mitodl/my-app        # optional; required for PR enrichment
      git_user_name: Concourse CI
      git_user_email: concourse@mit.edu
      # Changelog options (omit to disable changelog management):
      changelog_style: cumulative      # "cumulative" or "per_release"
      changelog_file: CHANGELOG.md    # cumulative mode filename (default: CHANGELOG.md)
      changelog_dir: releases          # per_release mode directory (default: releases)
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `uri` | Yes | — | Git repository URI (SSH or HTTPS) |
| `branch` | No | `main` | Branch to track for new commits |
| `private_key` | No | — | SSH private key for git operations |
| `access_token` | No | — | GitHub token; enables PR number/title enrichment |
| `repository` | No | — | `owner/repo`; required when `access_token` is set |
| `git_user_name` | No | `Concourse CI` | Git committer name for release commits |
| `git_user_email` | No | `concourse@example.com` | Git committer email |
| `changelog_style` | No | `null` | `"cumulative"` or `"per_release"`; omit to disable |
| `changelog_file` | No | `CHANGELOG.md` | Changelog filename (cumulative mode) |
| `changelog_dir` | No | `releases` | Directory for per-release files |

## `check` — Detect unreleased commits

Clones the repository and compares HEAD of `branch` to the latest `YYYY.MM.DD.N` tag.

- If HEAD is ahead of the latest tag, emits the **next version** (`YYYY.MM.DD.N`).
- If HEAD is already tagged, emits the existing version (no new commits).
- If no tags exist, emits `YYYY.MM.DD.1` for today's date.

The version object carries lightweight metadata for use by the Slack release bot's
`/release-notes` command without triggering the full pipeline:

```json
{
  "version": "2026.04.14.1",
  "head_sha": "<full SHA of HEAD at check time>",
  "since": "2026.04.10.2",
  "commit_count": "7",
  "authors": "alice@example.com,bob@example.com"
}
```

`head_sha` binds subsequent `in` and `out` steps to the exact commit evaluated
during `check`, preventing race conditions if new commits land between steps.

> **Depth note**: The resource uses a shallow clone (`--depth=200`). For repositories
> where the previous release tag is more than 200 commits back, consider a full clone.

## `in` — Fetch release metadata

Clones the repository and generates release artefacts from `version.since..version.head_sha`.

### Output files

| File | Description |
|------|-------------|
| `version` | Plain version string, e.g. `2026.04.14.1` |
| `commits.json` | Structured list of `{sha, author, pr_number, pr_title, message}` |
| `checklist.md` | GitHub Issue body with markdown task list; use as `body_file` in `github-issues` resource |
| `changelog_entry.md` | Single [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) entry for this version |

## `out` — Create or finish a release

### `action: create`

Requires a checked-out git repository (from `get: app-source`) with version files
already updated by `bump_version_task`.

1. Records the pre-bumpver HEAD SHA (this becomes the release tag, marking the code cut for RC).
2. Optionally cherry-picks `commit_hash` (hotfix) before the release commit.
3. Creates `release/YYYY.MM.DD.N` branch.
4. Stages version-bump changes and optional changelog update in a single `"Release YYYY.MM.DD.N"` commit.
5. Pushes the branch.
6. Creates and pushes the `YYYY.MM.DD.N` tag on the pre-bumpver HEAD.

### `action: finish`

Merges `release/YYYY.MM.DD.N` back into the configured `branch` (no fast-forward).
Run as the final step of the `deploy-production` job after production deployment is verified.

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | Yes | `"create"` or `"finish"` |
| `repo_dir` | Yes | Name of the workspace directory containing the checked-out repo |
| `version_file` | Yes | Path to the `version` file (relative to workspace root), e.g. `release/version` |
| `commit_hash` | No | Commit SHA to cherry-pick (`create` only; hotfix support) |

### Example pipeline

```yaml
# create-release job (triggered via check webhook by the Slack release bot)
plan:
  - get: app-release          # in: writes version, checklist.md, changelog_entry.md
    trigger: true
  - get: app-source
  - task: bump-version        # bump_version_task() from pipeline_lib
  - put: app-release
    params:
      action: create
      repo_dir: app-source
      version_file: app-release/version

  - put: release-gate         # github-issues resource
    params:
      issue_title_template: "Release {version} — my-app"
      body_file: app-release/checklist.md

# deploy-production job (triggered by release-gate issue close)
plan:
  - get: release-gate
    trigger: true
  - [deploy steps]
  - put: app-release
    params:
      action: finish
      repo_dir: app-source
      version_file: app-release/version
```

## Changelog management

When `changelog_style` is set the `out action: create` step writes or updates a
changelog file and includes it in the `"Release YYYY.MM.DD.N"` commit.

### `changelog_style: cumulative`

Prepends a new [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) entry to
`changelog_file` (default `CHANGELOG.md`).  Creates the file with the standard
header if it does not yet exist.

### `changelog_style: per_release`

Writes a standalone `RELEASE_<version>.md` file to `changelog_dir` (default `releases/`).

## Docker Image

```
mitodl/concourse-release-resource:latest
```

## License

BSD-3-Clause — Copyright MIT Open Learning
