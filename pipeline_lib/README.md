# ol-concourse-lib

A Python DSL for programmatically building [Concourse CI/CD](https://concourse-ci.org/) pipelines.

Instead of writing YAML files by hand, use typed Python classes to construct type-safe pipeline definitions that serialize to valid Concourse pipeline YAML.

## Installation

```bash
pip install ol-concourse-lib
# or with Pulumi job builder support:
pip install "ol-concourse-lib[pulumi]"
```

## Quick Start

```python
from ol_concourse.lib.models.pipeline import Identifier
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.lib.resource_types import packer_build
from ol_concourse.lib.jobs.infrastructure import packer_jobs
import yaml, json

# Build a pipeline fragment for packer AMI builds
code = git_repo(
    name=Identifier("packer-templates"),
    uri="https://github.com/myorg/packer-templates",
    branch="main",
)

fragment = packer_jobs(
    dependencies=[],
    image_code=code,
    node_types=["web", "worker"],
)

pipeline = fragment.to_pipeline()
print(yaml.dump(json.loads(pipeline.model_dump_json())))
```

## API

### Models (`ol_concourse.lib.models`)

- `Pipeline` — root pipeline definition
- `Job`, `Resource`, `ResourceType` — core pipeline components
- `GetStep`, `PutStep`, `TaskStep`, `InParallelStep` — step types
- `TaskConfig`, `Command`, `Input`, `Output` — task configuration
- `PipelineFragment` — composable pipeline unit

### Builder Functions

| Module | Functions |
|--------|-----------|
| `ol_concourse.lib.resources` | `git_repo`, `registry_image`, `github_issues`, `schedule`, `s3_object`, … |
| `ol_concourse.lib.resource_types` | `packer_build`, `packer_validate`, `pulumi_provisioner_resource`, `github_issues_resource`, … |
| `ol_concourse.lib.jobs.infrastructure` | `packer_jobs`, `pulumi_jobs_chain`, `pulumi_job` |
| `ol_concourse.lib.containers` | `container_build_task` |
| `ol_concourse.lib.notifications` | `notification` (Slack) |
| `ol_concourse.lib.tasks` | `instance_refresh_task`, `block_for_instance_refresh_task` |

## Regenerating Pipeline Models

`models/pipeline.py` is generated from the [Concourse pipeline JSON schema](https://github.com/nicholasdille/concourse-jsonschema):

```bash
pip install datamodel-code-generator
datamodel-codegen \
    --url https://raw.githubusercontent.com/nicholasdille/concourse-jsonschema/main/concourse.json \
    --output src/ol_concourse/lib/models/pipeline.py \
    --output-model-type pydantic_v2.BaseModel
```

## License

BSD-3-Clause — Copyright MIT Open Learning
