# concourse-github-issues-resource

A Concourse CI resource type for managing GitHub Issues, built with [concoursetools](https://concoursetools.readthedocs.io/).

## Source Configuration

```yaml
resource_types:
- name: github-issues
  type: registry-image
  source:
    repository: mitodl/concourse-github-issues-resource
    tag: latest

resources:
- name: my-github-issues
  type: github-issues
  source:
    repository: myorg/my-repo       # required
    access_token: ((github.token))  # optional for public repos (token auth)
    issue_state: closed             # "open" or "closed" (default: "closed")
    issue_prefix: "[bot]"           # optional: filter issues by title prefix
    labels: [pipeline-workflow]     # optional: filter by labels
    skip_if_labeled: [skip-ci]      # optional: skip issues that have any of these labels
    update_in_place: false          # optional: edit an existing open issue instead of
                                     # commenting on it (default: false)
```

## `check` — Fetch versions

Returns issues matching the configured `issue_state` and `issue_prefix`.
Issues that carry any label listed in `skip_if_labeled` are excluded from results.

## `in` — Download version

Downloads the issue metadata as `gh_issue.json` to the destination directory.
Marks the issue as consumed by prefixing the title with `[CONSUMED #<build_number>]`.

When the resource is configured with `skip_if_labeled`, the issue's labels are re-read first and the `get` **fails**, writing and renaming nothing, if it now carries one of them. `check` only filters on labels when it discovers a version, so this is what stops an issue that was abandoned (see `close_with_labels`) after `check` handed it to Concourse from triggering the job that consumes it.

## `out` — Create/update an issue

| Parameter | Required | Description |
|-----------|----------|-------------|
| `assignees` | No | List of GitHub usernames to assign |
| `labels` | No | List of label names to apply |
| `body_file` | No | Path to a file whose contents are used as the issue body, overriding `issue_body_template` |
| `body_files` | No | List of paths whose contents are concatenated **in the order given** to form the issue body, overriding `issue_body_template`. Use this when the body is assembled from more than one step's artifact -- a Concourse `put` emits no artifact of its own, so each fragment arrives via its own implicit `get`. Takes precedence over `body_file` if both are set. Unlike `body_file`, a **missing** entry does not fail the put: it is replaced by a visible warning naming the absent path, because a fragment's producing step may be allowed to fail without failing the build. |
| `close_if_file` | No | Path to a file whose presence means there is nothing to review (e.g. an empty Pulumi preview). The issue is created or updated as normal, then closed, so a gate that advances on a *closed* issue does not wait for a human. A missing file is the same as not setting this. |
| `skip_if_file` | No | Path to a file whose presence means there is nothing to post. No issue is searched for, created, updated, commented on or closed. Use this instead of `close_if_file` where a closed issue would itself approve something, e.g. a release issue whose closing ships the release. The put returns a fixed placeholder version (issue `0`, empty title, `open`), so Concourse records it once and never sees it as new again, and its implicit get never marks an issue `[CONSUMED]`. A `check` that is handed it back starts over from the latest matching issue. Never `get` the resource you put to as a gate; use a separate resource, as with any put to this resource. Takes precedence over `close_if_file`. A missing file is the same as not setting this. |
| `close_with_labels` | No | List of label names. Retires an issue instead of posting one: every issue whose title is exactly the rendered title (`title_template`, else `issue_title_template`) gets these labels. An **open** one also gets a comment linking the build and is closed; one already **closed** but not yet consumed by a gate (a consumed issue has been renamed `[CONSUMED ...]`, so it never matches) is only labelled. An open issue is labelled and closed in a single edit, so a gate watching for *closed* issues, with `skip_if_labeled` set to one of them, never sees it unlabelled, and an `update_in_place` put never sees it open with the label (and removes it as stale). The labels are checked again afterwards in case another writer replaced them. Whether an issue is open is read from the issues API, not the search result. Issues are found by search plus a read of the newest 100 issues, because the search index lags behind issue creation and an abandon can follow the QA put within moments. Never creates an issue; finding none is not an error. Use it to abandon a release whose gate issue would otherwise still ship it when closed. Returns the same fixed placeholder version as `skip_if_file` and reports the issue numbers as `retired_issues` metadata, so the implicit get has nothing to mark `[CONSUMED]`. `skip_if_file` wins when both are set. |
| `skip_if_branch_missing` | No | Name of a branch in the repository, e.g. `releases/2026.9.22.1`. When it does not exist, nothing is posted: no issue is searched for, created, updated, commented on or closed, and the put returns the same fixed placeholder version as `skip_if_file` with `skipped_by` metadata. Use it on a put that can run again after the thing it announces is over -- a release issue opened by a QA deploy that an infrastructure-only merge re-runs once the release has been abandoned or finished. Without it that run finds no *open* issue, opens a new one, and closing it ships a release that no longer exists. Existence is read from the branches API by exact name, not guessed from issues, so a release re-cut under an abandoned version number (abandoning deletes the tag, so the number is reused) still gets its issue. Does not apply to `close_with_labels`, which runs after an abandon has deleted the branch. Any lookup error other than the branch being absent fails the put. `skip_if_file` wins when both are set. |
| `skip_if_tag_missing` | No | Name of a tag in the repository, e.g. `2026.9.22.1`. The same as `skip_if_branch_missing`, but for a tag: when it does not exist, nothing is posted and the put returns the fixed placeholder version with `skipped_by` metadata. Prefer it on a release issue put. The release resource deletes the version tag when it abandons a release but keeps it when it finishes one, so a finished release that is waiting for approval again (Production rolled back past it) still gets its issue, which a branch check would withhold. A release re-cut under an abandoned version number recreates the tag, so it gets its issue too. Existence is read from the matching-refs API and only an exact `refs/tags/<tag>` counts. Does not apply to `close_with_labels`. Any lookup error fails the put. `skip_if_file` wins; with `skip_if_branch_missing` also set, either one missing skips. |
| `title_template` | No | Overrides the source-level `issue_title_template` for this put. Use this to embed a value only known at build time, e.g. a release version loaded via `load_var` earlier in the same job -- put an unresolved `((.:my_var))` reference in the params value; Concourse resolves it to a plain string before this resource ever runs, so `title_template` arrives here already containing the concrete value. |

The issue title and body are generated from configurable templates:
- `issue_title_template` — default: `[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed`
- `issue_body_template` — Markdown body with build details and a link to the build log

If no open issue matches the title, a new one is created. If a matching open
issue already exists, the default behavior is to **comment** on it -- for
most consumers of this resource, a fresh comment on an already-open issue
*is* the useful signal: it means this gate has been hit again (e.g. deploys
stacking up) before anyone closed the last one.

Set `update_in_place: true` to instead **edit the issue body in place**.
This is for cases where re-showing the same content on every hit is noise
rather than signal -- e.g. the release resource's checklist, where a
retrigger with no real app change to review (an unrelated upstream pipeline
commit) would otherwise post a second, freshly-unchecked checklist as a new
comment and read as the issue reopening. With `update_in_place`, any
checklist line (`- [ ] ...`) already checked in the current body stays
checked after the edit, matched by the line's content after the checkbox
mark -- only genuinely new or changed lines start unchecked. The put's
`labels` are also reconciled onto the issue, by adding and removing one label
at a time, so a label written after the put read the issue survives. A
matching issue that the issues API reports as closed keeps its labels: the
search index can still list an issue that `close_with_labels` has just
retired as open.

## Authentication

| Method | Source fields |
|--------|--------------|
| Token | `access_token` |
| GitHub App | `auth_method: app`, `app_id`, `app_installation_id`, `private_ssh_key` |

## Docker Image

```
mitodl/concourse-github-issues-resource:latest
```

## License

BSD-3-Clause — Copyright MIT Open Learning
