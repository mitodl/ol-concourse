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
