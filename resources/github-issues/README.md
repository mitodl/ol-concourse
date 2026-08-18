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

## `out` — Create/update an issue

| Parameter | Required | Description |
|-----------|----------|-------------|
| `assignees` | No | List of GitHub usernames to assign |
| `labels` | No | List of label names to apply |
| `body_file` | No | Path to a file whose contents are used as the issue body, overriding `issue_body_template` |
| `body_files` | No | List of paths whose contents are concatenated **in the order given** to form the issue body, overriding `issue_body_template`. Use this when the body is assembled from more than one step's artifact -- a Concourse `put` emits no artifact of its own, so each fragment arrives via its own implicit `get`. Takes precedence over `body_file` if both are set. Unlike `body_file`, a **missing** entry does not fail the put: it is replaced by a visible warning naming the absent path, because a fragment's producing step may be allowed to fail without failing the build. |
| `title_template` | No | Overrides the source-level `issue_title_template` for this put. Use this to embed a value only known at build time, e.g. a release version loaded via `load_var` earlier in the same job -- put an unresolved `((.:my_var))` reference in the params value; Concourse resolves it to a plain string before this resource ever runs, so `title_template` arrives here already containing the concrete value. |

### Body size

GitHub rejects an issue body (or comment) longer than 65536 characters with a
`422 Validation Failed`, which fails the `put` *after* the deploy it reports on
has already run -- the gate issue never gets written and the build goes red with
nothing for a reviewer to act on. Bodies are therefore capped, and a body that
was cut says so in its own text; the full content stays in the build log.

With `body_files`, the cap is shared out between the fragments rather than
applied to the joined body, so an oversized first section cannot push a later
one out of the issue entirely. Each fragment gets an equal share and any share a
short fragment does not use goes to the ones that overflow.

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
mark -- only genuinely new or changed lines start unchecked.

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
