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
    auto_check_authors:             # optional: auto-check bot-authored checklist lines
      - concourse@example.com
```

## `check` — Fetch versions

Returns issues matching the configured `issue_state` and `issue_prefix`.
Issues that carry any label listed in `skip_if_labeled` are excluded from results.

If `auto_check_authors` is set, every open matching issue returned by *this*
`check` call has its body scanned for release-resource-style checklist lines
(`- [ ] ... by <author>`), and any unchecked line whose author is in the list
is flipped to `- [x]` — mirroring the deprecated release-script bot
self-checking boxes for commits it authored itself (e.g. automated version
bumps) since there's no human available to check those off manually.
Already-checked lines and lines from other authors are left untouched; the
issue is only edited when a line actually changes.

Note this only covers issues `check` actually returns as new versions, not
every open matching issue that has ever existed: the first-ever check only
inspects the single most recent matching issue (to seed Concourse's state
cheaply), and subsequent checks are bounded to issues created/closed since
the previous version. An older open issue that predates `auto_check_authors`
being enabled won't be retroactively scanned until it produces a new
version itself.

## `in` — Download version

Downloads the issue metadata as `gh_issue.json` to the destination directory.
Marks the issue as consumed by prefixing the title with `[CONSUMED #<build_number>]`.

## `out` — Create/update an issue

| Parameter | Required | Description |
|-----------|----------|-------------|
| `assignees` | No | List of GitHub usernames to assign |
| `labels` | No | List of label names to apply |
| `body_file` | No | Path to a file whose contents are used as the issue body, overriding `issue_body_template` |

The issue title and body are generated from configurable templates:
- `issue_title_template` — default: `[bot] Pipeline {BUILD_PIPELINE_NAME} task {BUILD_JOB_NAME} completed`
- `issue_body_template` — Markdown body with build details and a link to the build log

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
