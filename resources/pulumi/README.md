# concourse-pulumi-resource

A Concourse CI resource type for managing [Pulumi](https://www.pulumi.com/) stacks using the Pulumi Automation API, built with [concoursetools](https://concoursetools.readthedocs.io/).

## Docker Images

| Image | Use case |
|-------|----------|
| `mitodl/concourse-pulumi-resource` | Standard deployments (official Pulumi Python image) |
| `mitodl/concourse-pulumi-resource-provisioner` | MIT ODL deployments (includes AWS CLI, kubectl, auto plugin install) |

## Source Configuration

```yaml
resource_types:
- name: pulumi
  type: registry-image
  source:
    repository: mitodl/concourse-pulumi-resource
    tag: latest

resources:
- name: my-stack
  type: pulumi
  source:
    stack_name: applications.myapp.production
    project_name: ol-infrastructure-myapp
    source_dir: src/ol_infrastructure/applications/myapp
    env_pulumi:
      AWS_REGION: us-east-1
```

## `check` — Stack version

Returns a static version `{"id": "0"}`. Pulumi stacks are managed via `put`, not polled.

## `get` Step Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `skip_implicit_get` | No | `false` | Skip fetching stack outputs |
| `stack_name` | No | from source | Override stack name |
| `output_key` | No | — | Fetch only a single output key |
| `env_pulumi` | No | — | Additional Pulumi env vars |
| `env_os` | No | — | OS environment variables |

Outputs are written to `{source_dir}/{stack_name}_outputs.json`.

## `put` Step Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `action` | **Yes** | — | `"create"`, `"update"`, or `"destroy"` |
| `stack_name` | No | from source | Override stack name |
| `project_name` | No | from source | Override project name |
| `source_dir` | No | from source | Path to Pulumi project directory |
| `stack_config` | No | `{}` | Dict of Pulumi config key-value pairs |
| `preview` | No | `false` | Preview changes only, no apply |
| `refresh_stack` | No | `true` | Refresh state before update/destroy |
| `env_pulumi` | No | — | Pulumi-specific environment variables |
| `env_os` | No | — | OS environment variables |
| `env_vars_from_files` | No | — | Map of env var name → file path |

## Example

```yaml
jobs:
- name: deploy
  plan:
  - get: pulumi-code
  - put: my-stack
    params:
      action: update
      source_dir: pulumi-code/src/ol_infrastructure/applications/myapp
      env_pulumi:
        AWS_REGION: us-east-1
      env_vars_from_files:
        PULUMI_CONFIG_PASSPHRASE: pulumi-code/.pulumi/passphrase
```

## License

BSD-3-Clause — Copyright MIT Open Learning
