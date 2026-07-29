# ol-concourse-pypi

Concourse resource type for PyPI package publishing and version tracking, built
with [ConcourseTools](https://github.com/gchq/ConcourseTools).

Replaces the unmaintained `cfplatformeng/concourse-pypi-resource`.

## Resource type

```yaml
resource_types:
  - name: pypi
    type: registry-image
    source:
      repository: mitodl/concourse-pypi-resource
```

## Source configuration

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `package_name` | ✅ | — | Name of the PyPI package |
| `password` | ✅ | — | PyPI API token (use `__token__` as username) |
| `username` | | `__token__` | Upload username |
| `repository_url` | | `https://upload.pypi.org/legacy/` | Upload endpoint (use TestPyPI for testing) |
| `index_url` | | `https://pypi.org` | Index for `check`/`get` operations |

## `check`: track new versions

Returns package versions from PyPI newer than the current pinned version,
ordered by PEP 440 precedence.

## `get`: download a version

Downloads distribution files for the pinned version to the destination directory.

Fetching a version's file metadata makes up to 5 attempts, backing off
exponentially (1s, 2s, 4s, 8s) between them, if PyPI's JSON index 404s. This
specifically covers the implicit `get` Concourse performs right after a `put`
succeeds: PyPI's index can lag a few seconds behind an upload actually
landing, and without the retry that race failed the whole build even though
the upload itself had genuinely already succeeded. Any non-404 error raises
immediately; a 404 only raises once all 5 attempts are exhausted.

**Parameters**

| Param | Default | Description |
|-------|---------|-------------|
| `download_sdist` | `true` | Download the source distribution |
| `download_wheel` | `false` | Download a wheel if available |

## `put`: publish a new version

Uploads distribution files to PyPI using `twine`.

**Parameters**

| Param | Required | Description |
|-------|----------|-------------|
| `glob` | ✅ | Glob pattern (relative to the Concourse working directory) matching the files to upload, e.g. `dist/my_pkg-*.tar.gz` |

### Example pipeline snippet

```yaml
resources:
  - name: my-package-pypi
    type: pypi
    source:
      package_name: my-package
      password: ((pypi.token))

jobs:
  - name: publish
    plan:
      - get: source-repo
        trigger: true
      - task: build
        # ... produces dist/ output
      - put: my-package-pypi
        params:
          glob: dist/my_package-*.tar.gz
```

## Authentication

Use a PyPI **API token** (not your account password).  Create one at
<https://pypi.org/manage/account/#api-tokens> and store it in Concourse Vault
as `((pypi.token))`.  The `username` defaults to `__token__`; do not change it
when using an API token.

> **OIDC / Trusted Publishers**: PyPI's OIDC trusted publisher mechanism only
> supports GitHub Actions, GitLab CI (hosted), Google Cloud Build, and
> ActiveState.  Custom OIDC providers (e.g. Keycloak) are not accepted.

## Development

```bash
# Generate requirements.txt for Docker builds
uv export --package ol-concourse-pypi --no-dev --no-emit-workspace \
  --no-hashes -o resources/pypi/requirements.txt

# Run tests
uv run --package ol-concourse-pypi pytest resources/pypi/tests/
```
