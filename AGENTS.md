# ol-concourse Agent Instructions

MIT Open Learning Concourse CI/CD monorepo. Contains two distinct things:
1. **`pipeline_lib`** — a Python DSL (`ol-concourse-lib` on PyPI) for composing Concourse pipelines programmatically with Pydantic models.
2. **`resources/`** — custom Concourse resource type implementations (`packer`, `pulumi`, `github-issues`, `pypi`, `npm`), each shipped as a Docker image.

## Commands

```bash
# Install all workspace dependencies
uv sync

# Run tests for a specific package
uv run --package ol-concourse pytest pipeline_lib/tests/
uv run --package ol-concourse-github-issues pytest resources/github-issues/tests/
uv run --package ol-concourse-pypi pytest resources/pypi/tests/
uv run --package ol-concourse-npm pytest resources/npm/tests/
uv run --package ol-concourse-pulumi pytest resources/pulumi/tests/

# Run a single test
uv run --package ol-concourse-pypi pytest resources/pypi/tests/test_foo.py::test_bar

# Lint and format
uv run ruff check --fix .
uv run ruff format .
uv run mypy .

# All pre-commit hooks (includes yamlfmt, yamllint, ruff, mypy, actionlint, shellcheck)
uv run pre-commit run --all-files
```

## Architecture

### pipeline_lib

`pipeline_lib/src/ol_concourse/lib/models/pipeline.py` is **auto-generated** from the [Concourse JSON schema](https://github.com/nicholasdille/concourse-jsonschema) — do not edit it manually (see pipeline_lib/README.md for the regeneration command).

The composable unit is `PipelineFragment` (`models/fragment.py`), which holds `resource_types`, `resources`, and `jobs`. Pipelines are assembled by combining fragments:

```python
fragment = PipelineFragment.combine_fragments(fragment_a, fragment_b)
pipeline = fragment.to_pipeline()
yaml.dump(json.loads(pipeline.model_dump_json()))  # → Concourse YAML
```

High-level builder functions live in:
- `lib/resources.py` — `git_repo`, `registry_image`, `github_issues`, `schedule`, `s3_object`, …
- `lib/resource_types.py` — `packer_build`, `pulumi_provisioner_resource`, `github_issues_resource`, …
- `lib/jobs/infrastructure.py` — `packer_jobs`, `pulumi_jobs_chain`, `pulumi_job`
- `lib/notifications.py` — `notification` (Slack)

`PipelineFragment` deduplicates `resource_types` and `resources` by name on assignment, so the same resource type can be included in multiple fragments and merged safely.

### Custom resource types

Each resource under `resources/` follows the [concoursetools](https://concoursetools.readthedocs.io/) pattern:

- Subclass `ConcourseResource[VersionType]` and implement `fetch_new_versions`, `download_version`, `publish_new_version` (check/get/put).
- Version types subclass `TypedVersion` (dataclass-style, as in `pulumi`) or `Version` + `SortableVersionMixin` (as in `github-issues`).
- Source-level params set on `__init__` are merged/overridden by step-level params passed to `download_version`/`publish_new_version` — use the `_resolve_params` pattern from `resources/pulumi/concourse.py`.
- Each resource's `destination_dir` is its own output directory; `destination_dir.parent` is the job working directory containing all fetched inputs.

## Key Conventions

- **Python 3.13**, Pydantic v2, ruff line length 88, pep257 docstrings, double-quoted strings.
- `Identifier` (from `models/pipeline.py`) enforces `^[a-z][\w\d\-_.]*$` — all resource/job names must match.
- Resource source configs map directly to `__init__` parameters of the `ConcourseResource` subclass.
- Tests in `**/tests/` may use `S101` (assert) and `S105` (hardcoded passwords) without ruff warnings — those rules are ignored there.
- Each resource package builds with `hatchling`; the wheel includes only the specific source files listed in `[tool.hatch.build.targets.wheel]`.
- YAML files are auto-formatted by `yamlfmt` (2-space mapping/sequence, 80-char width) — let pre-commit handle formatting rather than editing by hand.
