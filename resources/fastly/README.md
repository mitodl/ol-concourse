# ol-concourse-fastly

Concourse resource type for tracking Fastly service VCL versions and performing
instant cache purges.

## Resource Type

```yaml
resource_types:
  - name: fastly
    type: registry-image
    source:
      repository: mitodl/concourse-fastly-resource
```

## Source Configuration

Identify the service by its opaque ID or by a domain it serves — one of the
two must be provided.

**By service ID:**

```yaml
resources:
  - name: my-fastly-service
    type: fastly
    source:
      api_token: ((fastly.api_token))
      service_id: ((fastly.service_id))
```

**By domain (resolved at runtime):**

```yaml
resources:
  - name: my-fastly-service
    type: fastly
    source:
      api_token: ((fastly.api_token))
      domain: www.example.com
```

| field | type | required | description |
|---|---|---|---|
| `api_token` | string | yes | Fastly API token. Needs `purge_select` scope for purge-only use; `global:read` scope to fetch VCL or resolve a service by domain. |
| `service_id` | string | no† | Alphanumeric Fastly service ID. Required unless `domain` is set. May be overridden per step. Takes precedence over `domain` when both are set. |
| `domain` | string | no† | Hostname served by the target service (e.g. `www.example.com`). The resource lists all services in the account at runtime and matches on domain name. Ignored when `service_id` is also set. |

† Exactly one of `service_id` or `domain` must be set for `check`/`get` and
all `put` modes except `url`.

## Behaviour

### `check`

Polls the Fastly API for the currently active VCL version number on the
service. Emits a new version whenever that number increases (i.e., a new VCL
version has been activated). The version key is `service_version`.

### `get`

Fetches metadata for the pinned service version and writes two files to the
destination directory:

| file | content |
|---|---|
| `service_version` | Integer string of the active VCL version number |
| `updated_at` | ISO-8601 timestamp of when that version was last updated |

#### Params

| param | type | default | description |
|---|---|---|---|
| `fetch_vcl` | `"generated"` \| `"custom"` \| `"both"` \| `false` | `false` | Download VCL content in addition to metadata files. |
| `vcl_dir` | string | `"vcl"` | Subdirectory within the destination directory where VCL files are written. |
| `service_id` | string | source value | Override the source-level service ID for this step. |

#### VCL output files

When `fetch_vcl` is set, additional files are written under `vcl/` (or
`vcl_dir`):

| `fetch_vcl` value | files written |
|---|---|
| `"generated"` | `vcl/generated.vcl` — the fully compiled VCL Fastly executes (includes all snippets and boilerplate) |
| `"custom"` | `vcl/{name}.vcl` for each custom-authored file; `vcl/main` containing the name of the file marked as main |
| `"both"` | all of the above |

The generated VCL is the right input for downstream linting or integration
tests. The custom VCL files are better suited for diffing or auditing
source-level changes between activations.

**Example — lint generated VCL after each activation:**

```yaml
- get: my-fastly-service
  params:
    fetch_vcl: generated

- task: lint-vcl
  config:
    platform: linux
    image_resource:
      type: registry-image
      source:
        repository: fastly/cli
    inputs:
      - name: my-fastly-service
    run:
      path: fastly
      args:
        - vcl
        - lint
        - --file
        - my-fastly-service/vcl/generated.vcl
```

### `put`

Performs an instant cache purge against the Fastly API.  Always returns a
static version (`service_version: "0"`) — purges do not advance the check
cursor.

#### Params

| param | type | default | description |
|---|---|---|---|
| `mode` | `"purge_all"` \| `"surrogate_key"` \| `"surrogate_keys"` \| `"url"` | `"purge_all"` | Which purge endpoint to call. |
| `service_id` | string | source value | Override the source-level service ID for this step. Required for all modes except `url`. |
| `surrogate_key` | string | — | Single surrogate key tag. Required when `mode="surrogate_key"`. |
| `surrogate_keys` | list of strings | — | Up to 256 surrogate key tags for a batch purge. Required when `mode="surrogate_keys"`. |
| `url` | string | — | Absolute URL of the cached object to purge. Required when `mode="url"`. |
| `soft` | bool | `false` | Issue a soft purge — marks objects stale rather than immediately inaccessible. Not supported with `mode="purge_all"` (use a common surrogate key such as `"all"` instead). |

#### Purge mode examples

**Purge everything (hard):**

```yaml
- put: my-fastly-service
  params:
    mode: purge_all
```

**Purge by surrogate key (soft):**

```yaml
- put: my-fastly-service
  params:
    mode: surrogate_key
    surrogate_key: html-pages
    soft: true
```

**Batch surrogate key purge:**

```yaml
- put: my-fastly-service
  params:
    mode: surrogate_keys
    surrogate_keys:
      - html-pages
      - api-responses
    soft: true
```

**Purge a single URL:**

```yaml
- put: my-fastly-service
  params:
    mode: url
    url: https://www.example.com/path/to/stale-page
```

## Full pipeline example

The following fragment triggers on new Fastly VCL activations, fetches the
compiled VCL for linting, and purges a surrogate-key scope on success.

```yaml
resource_types:
  - name: fastly
    type: registry-image
    source:
      repository: mitodl/concourse-fastly-resource

resources:
  - name: vcl-service
    type: fastly
    source:
      api_token: ((fastly.api_token))
      service_id: ((fastly.service_id))

jobs:
  - name: validate-and-purge
    plan:
      - get: vcl-service
        trigger: true
        params:
          fetch_vcl: generated

      - task: lint
        config:
          platform: linux
          image_resource:
            type: registry-image
            source:
              repository: alpine/curl
          inputs:
            - name: vcl-service
          run:
            path: sh
            args:
              - -c
              - echo "Linting VCL version $(cat vcl-service/service_version)"

      - put: vcl-service
        params:
          mode: surrogate_key
          surrogate_key: html-pages
          soft: true
```

## API token scopes

| operation | minimum scope |
|---|---|
| `check` / `get` (metadata only, `service_id` configured) | `global:read` |
| `check` / `get` (domain lookup) | `global:read` |
| `get` with `fetch_vcl` | `global:read` |
| `put` (any purge mode) | `purge_select` or `purge_all` |

Use a token with the narrowest scope that covers your pipeline's operations.

## Building

```bash
uv run --package ol-concourse-fastly pytest resources/fastly/tests/
```
