# ol-concourse-npm

Concourse resource type for NPM package publishing and version tracking, built
with [ConcourseTools](https://github.com/gchq/ConcourseTools).

## Resource type

```yaml
resource_types:
  - name: npm
    type: registry-image
    source:
      repository: mitodl/concourse-npm-resource
```

## Source configuration

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `package_name` | ✅ | — | Name of the npm package (with scope, e.g. `@myorg/pkg`) |
| `token` | ✅ | — | NPM automation token |
| `registry` | | `https://registry.npmjs.org` | npm-compatible registry URL |

## `check`: track new versions

Returns package versions from the registry newer than the current pinned
version, ordered by semver precedence.

## `get`: download a version

Downloads the package tarball for the pinned version to the destination
directory.

## `put`: publish a new version

Writes a temporary `.npmrc` with the auth token, runs `npm publish`, then
removes the `.npmrc` file.

**Parameters**

| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `package_dir` | ✅ | — | Path (relative to working directory) to the directory containing `package.json` |
| `tag` | | `latest` | npm distribution tag |
| `access` | | `public` | `public` or `restricted` |

### Example pipeline snippet

```yaml
resource_types:
  - name: npm
    type: registry-image
    source:
      repository: mitodl/concourse-npm-resource

resources:
  - name: my-npm-package
    type: npm
    source:
      package_name: my-npm-package
      token: ((npm.token))

jobs:
  - name: publish
    plan:
      - get: source-repo
        trigger: true
      - task: build
        # ... produces dist/ output with package.json
      - put: my-npm-package
        params:
          package_dir: source-repo/dist
          tag: latest
          access: public
```

## Authentication

Create an **Automation token** (type: `Automation`) at
<https://www.npmjs.com/settings/{username}/tokens> and store it in Concourse
Vault as `((npm.token))`.  Automation tokens bypass 2FA requirements, making
them suitable for CI pipelines.

> **Trusted Publishers / Provenance**: npm's OIDC-based trusted publishing
> (`npm publish --provenance`) is only available for GitHub Actions, GitLab CI
> (cloud), and CircleCI cloud runners.  Custom OIDC providers and Concourse are
> not supported.  Use automation tokens for Concourse pipelines.

## Development

```bash
# Generate requirements.txt for Docker builds
uv export --package ol-concourse-npm --no-dev --no-emit-workspace \
  --no-hashes -o resources/npm/requirements.txt

# Run tests
uv run --package ol-concourse-npm pytest resources/npm/tests/
```
