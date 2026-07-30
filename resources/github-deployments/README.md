# ol-concourse-github-deployments

A Concourse CI resource type for creating and tracking [GitHub Deployments](https://docs.github.com/en/rest/deployments/deployments) and Deployment Statuses, built with [concoursetools](https://concoursetools.readthedocs.io/).

## Source Configuration

```yaml
resource_types:
  - name: github-deployments
    type: registry-image
    source:
      repository: mitodl/concourse-github-deployments-resource
      tag: latest

resources:
  - name: github-deployment
    type: github-deployments
    check_every: never      # intended as a put-only resource
    source:
      repository: mitodl/my-app     # required: owner/repo
      environment: RC               # required: environment name (e.g. RC, Production)
      # Token auth (default):
      access_token: ((github.access_token))
      # ...or GitHub App auth (preferred — no token expiry to track):
      # auth_method: app
      # app_id: ((github.release_bot_app_id))
      # app_installation_id: ((github.release_bot_app_installation_id))
      # private_ssh_key: ((github.release_bot_app_pem))
```

| Source field | Required | Description |
|---|---|---|
| `repository` | Yes | GitHub repository in `owner/repo` form |
| `environment` | Yes | Deployment environment name (e.g. `RC`, `Production`, `Staging`) |
| `auth_method` | No | `token` (default) or `app` — see [Authentication](#authentication) |
| `access_token` | With `auth_method: token` | GitHub personal access token with `repo_deployments` scope |
| `app_id` | With `auth_method: app` | GitHub App ID |
| `app_installation_id` | With `auth_method: app` | ID of the App's installation on the target organization |
| `private_ssh_key` | With `auth_method: app` | The GitHub App's PEM private key |
| `gh_host` | No | GitHub API base URL (default: `https://api.github.com`; override for GitHub Enterprise) |

## `check` — Fetch versions

Returns the most recent deployment for the configured environment. When a previous version is known, returns all deployments with a higher deployment ID.

Intended for use with `check_every: never` — the resource is primarily driven by `put` steps.

## `in` — Download version

Fetches deployment metadata and writes `deployment.json` to the destination directory.

`deployment.json` fields:

| Field | Description |
|---|---|
| `deployment_id` | GitHub deployment ID (integer as string) |
| `sha` | Git SHA that was deployed |
| `ref` | Ref (branch, tag, or SHA) that was deployed |
| `environment` | Environment name |
| `description` | Deployment description |
| `created_at` | ISO 8601 timestamp of deployment creation |
| `state` | Latest deployment status state (or `pending` if no status yet) |
| `environment_url` | Environment URL from latest status (empty if not set) |
| `log_url` | Log URL from latest status (empty if not set) |

## `out` — Create / update a deployment

Two `action` values are supported:

### `action: start`

Creates a new GitHub Deployment for the given `ref` and immediately sets its status to `in_progress`.

| Parameter | Required | Description |
|---|---|---|
| `action` | Yes | Must be `"start"` |
| `ref` | Yes | Git ref, branch, or tag to deploy |
| `description` | No | Human-readable description shown in the GitHub UI |
| `environment_url` | No | URL of the deployed environment |
| `auto_merge` | No | Whether GitHub should auto-merge the default branch into `ref` (default: `false`) |
| `required_contexts` | No | Status check contexts that must pass; omit to use branch protection settings; pass `[]` to bypass all checks |
| `auto_inactive` | No | Mark older deployments in the same environment as `inactive` (default: `true`) |

### `action: finish`

Reads the deployment ID from a JSON file (typically `deployment.json` written by a prior `get` step) and creates a terminal Deployment Status.

| Parameter | Required | Description |
|---|---|---|
| `action` | Yes | Must be `"finish"` |
| `deployment_file` | Yes | Path to `deployment.json`, relative to the `put` step's input root |
| `state` | Yes | Final status: `success`, `failure`, `error`, or `inactive` |
| `description` | No | Human-readable description |
| `environment_url` | No | URL of the deployed environment |
| `auto_inactive` | No | Mark older deployments as `inactive` (default: `true`) |

### Example: two-step deploy flow

```yaml
jobs:
  - name: deploy-rc
    plan:
      - get: release
      - put: github-deployment
        params:
          action: start
          ref: release/2025.04.14.1
          description: "RC deployment started by Concourse"
      - get: github-deployment   # writes deployment.json to workspace
      - task: kubectl-rollout-status
          ...
      - put: github-deployment
        params:
          action: finish
          deployment_file: github-deployment/deployment.json
          state: success
          environment_url: https://rc.my-app.example.com
          description: "RC deployment complete"
      on_failure:
        put: github-deployment
        params:
          action: finish
          deployment_file: github-deployment/deployment.json
          state: failure
```

## Authentication

| Method | Source fields |
|--------|--------------|
| Token | `access_token` (needs the `repo_deployments` scope) |
| GitHub App | `auth_method: app`, `app_id`, `app_installation_id`, `private_ssh_key` |

Prefer GitHub App auth. The resource mints an installation access token per run,
so there is no PAT expiry to monitor — an expired fine-grained PAT fails a
release pipeline with an opaque `401 Bad credentials`. Grant the App
`deployments: write` on the target repositories.

The `release`, `github-issues`, and `github-deployments` resources are all meant
to authenticate as the *same* GitHub App, so that a release workflow has one
registration to manage and one private key to rotate. The `ol_concourse.lib`
DSL wrappers default to that shared App's credential references.

For GitHub Enterprise, set `gh_host` to your instance's API URL (e.g.
`https://github.example.com/api/v3`). Note that a GitHub App registration is
specific to one GitHub host.

## Docker Image

```
mitodl/concourse-github-deployments-resource:latest
```

## License

BSD-3-Clause — Copyright MIT Open Learning
