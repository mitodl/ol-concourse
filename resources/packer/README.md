# concourse-packer-resource

A Concourse CI resource type for running [Packer](https://www.packer.io/) builds, built with [concoursetools](https://concoursetools.readthedocs.io/).

## Docker Images

| Image | Use case |
|-------|----------|
| `mitodl/concourse-packer-resource` | Standard Packer builds (Alpine-based, minimal) |

## Source Configuration

```yaml
resource_types:
- name: packer
  type: registry-image
  source:
    repository: mitodl/concourse-packer-resource
    tag: latest

resources:
- name: packer-build
  type: packer
  source: {}
```

## `put` Step Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `objective` | No | `validate` | `"validate"` or `"build"` |
| `template` | **Yes** | — | Path to the Packer template (`.pkr.hcl`) |
| `var_files` | No | — | List of var file paths |
| `vars` | No | — | Dict of Packer variables |
| `vars_from_files` | No | — | Map of var name → file path |
| `env_vars` | No | — | Environment variables to set |
| `env_vars_from_files` | No | — | Map of env var name → file path |
| `only` | No | — | List of sources to build exclusively |
| `excepts` | No | — | List of sources to skip |
| `force` | No | `false` | Pass `-force` to Packer |
| `debug` | No | `false` | Dump args to stderr |

## Example

```yaml
jobs:
- name: build-ami
  plan:
  - get: packer-templates
  - put: packer-build
    params:
      objective: build
      template: packer-templates/src/images/web.pkr.hcl
      vars:
        environment: production
      env_vars:
        AWS_DEFAULT_REGION: us-east-1
      env_vars_from_files:
        AWS_SESSION_TOKEN: packer-templates/.aws/session_token
```

## License

BSD-3-Clause — Copyright MIT Open Learning
