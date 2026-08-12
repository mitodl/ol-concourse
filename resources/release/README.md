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
      # ...or GitHub App auth instead of access_token (preferred — no token
      # expiry to track).  Use an https:// uri so that git pushes authenticate
      # with the minted installation token and private_key is not needed:
      # auth_method: app
      # app_id: ((github.release_bot_app_id))
      # app_installation_id: ((github.release_bot_app_installation_id))
      # private_ssh_key: ((github.release_bot_app_pem))
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
| `private_key` | No | — | SSH private key for git transport; not needed when `uri` is HTTPS |
| `auth_method` | No | `token` | `token` or `app` — see [Authentication](#authentication) |
| `access_token` | No | — | GitHub token; enables PR enrichment and authenticates HTTPS git operations |
| `app_id` | With `auth_method: app` | — | GitHub App ID |
| `app_installation_id` | With `auth_method: app` | — | ID of the App's installation on the target organization |
| `private_ssh_key` | With `auth_method: app` | — | The GitHub App's PEM private key (distinct from `private_key`) |
| `repository` | No | — | `owner/repo`; required for PR enrichment |
| `git_user_name` | No | `Concourse CI` | Git committer name for release commits |
| `git_user_email` | No | `concourse@example.com` | Git committer email |
| `changelog_style` | No | `null` | `"cumulative"` or `"per_release"`; omit to disable |
| `changelog_file` | No | `CHANGELOG.md` | Changelog filename (cumulative mode) |
| `changelog_dir` | No | `releases` | Directory for per-release files |
| `production_environment` | No | `Production` | GitHub Deployment environment consulted to decide whether a superseded release already shipped |

## `check` — Detect unreleased commits

Clones the repository and compares HEAD of `branch` to the latest `YYYY.MM.DD.N` tag.

- If HEAD is ahead of the latest tag, emits the **next version** (`YYYY.MM.DD.N`).
- If HEAD is already tagged, emits the existing version (no new commits).
- If no tags exist, emits `YYYY.MM.DD.1` for today's date.

The version object carries lightweight metadata for use by the Slack release bot's
`/release-notes` command without triggering the full pipeline:

```json
{
  "version": "2026.4.14.1",
  "head_sha": "<full SHA of HEAD at check time>",
  "since": "2026.4.10.2",
  "commit_count": "7",
  "authors": "alice@example.com,bob@example.com",
  "in_flight": ""
}
```

`head_sha` binds subsequent `in` and `out` steps to the exact commit evaluated
during `check`, preventing race conditions if new commits land between steps.

### Release-machinery commits are not releasable work

The version tag is planted on the **pre-bump** HEAD, and `action: finish` then
lands two more commits on the tracked branch — `Release <version>` and
`Merge releases/<version>`. So `<latest tag>..origin/<branch>` is never empty
once a release completes. Both are excluded from `commit_count`, `authors`,
`commits.json`, `checklist.md`, and the changelog, so a finished release does
not immediately propose its own bookkeeping as the next release's contents.

### In-flight releases are reported, not obeyed

`in_flight` names a release that was cut but never **finished** — a
`releases/YYYY.MM.DD.N` branch still present on the remote. It is also written
to the `in_flight` file by `in`, and set as `get`/`put` metadata.

It is **not** a deployment status. A release whose production deploy succeeded
and whose `action: finish` then failed is still in flight by this definition —
that case is precisely what this field exists to surface, and what decides
whether superseding it keeps its tag.

`check` **always advances** to the true next version regardless. It used to
pin to the in-flight version until the release finished or was abandoned,
which meant a single failed `action: finish` froze the resource indefinitely:
no new version and no new commits reported, with no signal that anything was
wrong. Superseding an in-flight release is `action: create`'s job.

Detection queries the remote with `git ls-remote` rather than reading local
`origin/releases/*` tracking refs, because an `out` step's workspace checkout
comes from the `git` resource and only tracks the configured branch.

> **Depth note**: The resource uses a shallow clone (`--depth=200`). For repositories
> where the previous release tag is more than 200 commits back, consider a full clone.

## `in` — Fetch release metadata

Clones the repository and generates release artefacts from `version.since..version.head_sha`.

### Output files

| File | Description |
|------|-------------|
| `version` | Plain version string, e.g. `2026.4.14.1` |
| `in_flight` | Version of a release cut but not yet finished, or empty (may already be in production — see below) |
| `commits.json` | Structured list of `{sha, author, author_name, pr_number, pr_title, message}` -- `author` is the commit email (matched against `auto_check_authors` by the `github-issues` resource), `author_name` is the git-configured display name |
| `checklist.md` | GitHub Issue body with a markdown task list grouped by author (`### <author_name>` headings, newest contributor first); use as `body_file` in `github-issues` resource |
| `changelog_entry.md` | Single [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) entry for this version |

## `out` — Create or finish a release

### `action: create`

Requires a checked-out git repository (from `get: app-source`) with version files
already updated by `bump_version_task`.

1. **Supersedes any older in-flight release** — deletes its `releases/` branch
   from the remote and reports it as `superseded` in the put metadata.
   Whether its *tag* is deleted depends on whether that release ever shipped:

   | In-flight release | Branch | Tag | `superseded_tag` |
   |---|---|---|---|
   | Never reached production | deleted | deleted | `deleted` |
   | Reached production, `finish` failed | deleted | **kept** | `kept` |

   The two are indistinguishable in git, so the resource asks the GitHub
   Deployments API for a successful deployment of that version to
   `production_environment` (default `Production`). A shipped release's tag is
   the only thing tying what production runs back to a commit, so it must
   outlive its branch — and it is then the correct `since` boundary for the new
   release, whose predecessor really is live. When the answer cannot be
   established (no credentials, no `repository`, or an API failure) the tag is
   **kept**: an unnecessary tag is recoverable, a deleted one is not.
2. Records the pre-bumpver HEAD SHA (this becomes the release tag, marking the code cut for RC).
3. Optionally cherry-picks `commit_hash` (hotfix) before the release commit.
4. Creates `releases/YYYY.MM.DD.N` branch.
5. Stages version-bump changes and optional changelog update in a single `"Release YYYY.MM.DD.N"` commit.
6. Pushes the branch.
7. Creates and pushes the `YYYY.MM.DD.N` tag on the pre-bumpver HEAD.

Only an **older** release is ever superseded. `create` binds the version
Concourse resolved when the build was scheduled, so a delayed or concurrent
build can carry a version older than the release now in flight; that is
refused rather than allowed to replace a newer cut with an older one.

If any superseded ref survives deletion (a protected ref, a transient remote
failure — both silenced by the best-effort deletion in `abandon`), `create`
**fails** rather than reporting a supersede. Continuing would leave a branch
for a later `check` to rediscover as in-flight, which is the failure this
resource exists to prevent.

Re-running `create` for the version that is *already* in flight does not delete
the refs it is re-creating — **provided that cut completed**. The branch is
pushed before the tag, so a failed tag push leaves `releases/X` present with no
`X` tag. That is a partially-created release, not a retrigger: the stale branch
carries the previous attempt's release commit, so the next push is rejected as
a non-fast-forward and every retry fails identically. That branch is deleted
before re-cutting. A cut with **both** branch and tag present is a true
retrigger and is left completely alone.

### `action: finish`

Merges `releases/YYYY.MM.DD.N` back into the configured `branch` (no fast-forward),
then deletes the release branch from the remote.
Run as the final step of the `deploy-production` job after production deployment is verified.

**Idempotent**: if the release branch is already gone the release has been
finished (or abandoned), so this returns the current tip of the tracked branch
instead of failing. Callers should therefore **not** wrap this put in a `try` —
an error raised here means a real, unfinished release.

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

## Authentication

| Method | Source fields |
|--------|--------------|
| Token | `access_token` |
| GitHub App | `auth_method: app`, `app_id`, `app_installation_id`, `private_ssh_key` |

Prefer GitHub App auth. The resource mints an installation access token per run,
so there is no PAT expiry to monitor — an expired fine-grained PAT fails a
release pipeline with an opaque `401 Bad credentials`. Grant the App
`contents: write` (release branches and tags) and `pull_requests: read` (PR
enrichment) on the target repositories.

Two distinct keys are involved, and the naming is easy to trip over:

- `private_key` — an **SSH** key, used only for git transport when `uri` is an
  `ssh://` / `git@` URL.
- `private_ssh_key` — the **GitHub App's** PEM private key, used to sign the JWT
  that mints installation tokens. Named for consistency with the
  `github-issues` resource.

The token (static or App-minted) is embedded into the git remote URL as
`https://x-access-token:TOKEN@host/...` for clone, push, and tag operations, so
with an `https://` *uri* App auth alone is sufficient and no SSH key is needed.
With a `git@` *uri*, git transport still requires `private_key`.

The `release`, `github-issues`, and `github-deployments` resources are all meant
to authenticate as the *same* GitHub App, so that a release workflow has one
registration to manage and one private key to rotate. The `ol_concourse.lib`
DSL wrappers (`release_resource`, `github_issues`, `github_deployment`) accept
`app_id`/`app_installation_id`/`private_ssh_key` as explicit parameters --
there is no library-wide default, so each pipeline passes its own credential
references for those three fields.

## Docker Image

```
mitodl/concourse-release-resource:latest
```

## License

BSD-3-Clause — Copyright MIT Open Learning
