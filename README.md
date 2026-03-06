# ol-concourse

MIT Open Learning Concourse CI/CD monorepo containing Concourse resource types and pipeline tooling.

## Packages

| Package | Description | PyPI |
|---------|-------------|------|
| [`pipeline_lib`](./pipeline_lib/) | Python DSL for building Concourse pipelines programmatically | [`ol-concourse-lib`](https://pypi.org/project/ol-concourse-lib/) |
| [`resources/packer`](./resources/packer/) | Concourse resource for running Packer builds | — |
| [`resources/pulumi`](./resources/pulumi/) | Concourse resource for running Pulumi deployments | — |
| [`resources/github-issues`](./resources/github-issues/) | Concourse resource for managing GitHub Issues | — |

## Docker Images

| Image | Source |
|-------|--------|
| `mitodl/concourse-packer-resource` | `resources/packer/Dockerfile` |
| `mitodl/concourse-pulumi-resource` | `resources/pulumi/Dockerfile` |
| `mitodl/concourse-pulumi-resource-provisioner` | `resources/pulumi/Dockerfile.mitol_provision` |
| `mitodl/concourse-github-issues-resource` | `resources/github-issues/Dockerfile` |

## Development

This monorepo uses [uv](https://docs.astral.sh/uv/) for dependency and workspace management.

```bash
# Install all workspace dependencies
uv sync

# Run pre-commit hooks
uv run pre-commit run --all-files

# Run tests for a specific package
uv run --package ol-concourse-github-issues pytest resources/github-issues/tests/
```

## License

BSD-3-Clause — see individual package READMEs for details.
