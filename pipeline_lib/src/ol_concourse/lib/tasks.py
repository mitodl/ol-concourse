"""Task factory functions for ol-concourse pipeline DSL."""

import shlex

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    Identifier,
    Input,
    Output,
    TaskConfig,
    TaskStep,
)

# Default image for pipeline_lib task steps.
# Bundles ol-concourse, bump-my-version, and git.
# Tag is kept as "latest" until a versioned release of the task image is published.
# Once the first image is built and pushed, pin this to a specific digest or tag
# (e.g. "2026.04.15") to ensure reproducible pipeline behavior.
TASK_IMAGE = AnonymousResource(
    type=REGISTRY_IMAGE,
    source={"repository": "mitodl/ol-concourse-dsl", "tag": "latest"},
)


def bump_version_task(
    version_file: str = "release/version",
    repository: str = "app-source",
    git_user: str = "CI",
    git_email: str = "odl-devops@mit.edu",
    image: AnonymousResource | None = None,
) -> TaskStep:
    """Generate a TaskStep that runs bump-my-version to update version strings in-place.

    Reads the target version from ``version_file``, then runs
    ``bump-my-version bump --new-version <version> --no-commit --allow-dirty`` inside
    ``repository``.  The modified files remain in the workspace for a subsequent
    ``put: release`` step to commit onto the release branch.

    :param version_file: Workspace-relative path to the file containing the
        version string, in ``input-name/relative/path`` form (default:
        ``release/version``).  The leading path component must be the name of a
        Concourse input resource in the build plan.
    :param repository: Name of the Concourse input/output resource directory
        containing the application source and its ``[tool.bumpversion]`` config in
        ``pyproject.toml`` (default: ``app-source``).
    :param git_user: Git committer name used when bump-my-version writes version files
        (default: ``CI``).
    :param git_email: Git committer email (default: ``odl-devops@mit.edu``).
    :param image: Container image for the task.  Defaults to
        ``ghcr.io/mitodl/ol-concourse-task:latest`` via :data:`TASK_IMAGE`.

    :raises ValueError: If ``version_file`` is not in ``input-name/path`` form
        (i.e. has no ``/``, or starts with ``/``, ``./``, or ``../``).

    :returns: A configured Concourse
        :class:`~ol_concourse.lib.models.pipeline.TaskStep`.
    """
    if "/" not in version_file or version_file.startswith(("/", "./", "../")):
        msg = (
            f"version_file must be workspace-relative in 'input-name/path' form "
            f"(e.g. 'release/version'), got: {version_file!r}"
        )
        raise ValueError(msg)

    version_file = version_file.strip()
    version_input = version_file.split("/")[0]
    # Normalize repository name via Identifier so the input/output names and the
    # shell script refer to exactly the same directory.
    repo_id = str(Identifier(repository))

    # De-duplicate: when version_file lives inside the repo input, don't emit
    # the same input name twice (Concourse treats duplicate input names as invalid).
    inputs = [Input(name=Identifier(version_input))]
    if version_input != repo_id:
        inputs.append(Input(name=Identifier(repo_id)))

    # Python script injected for the semver->calver one-time transition.
    # bump-my-version's `bump` and `replace` commands both call parse() on the
    # current version before touching files; when the parse regex is calver-only
    # and the current version is an old semver string, parse() returns None and
    # serialize/hooks crash.  This script reads the [[tool.bumpversion.files]]
    # entries directly and does plain string substitution, then updates the
    # current_version tracking field — no parse() call involved.
    _transition_script = r"""import tomllib, pathlib, re, sys

since = sys.argv[1]  # semver baseline passed in from outside
new_ver = sys.argv[2]

with open("pyproject.toml", "rb") as f:
    config = tomllib.load(f)

bv = config.get("tool", {}).get("bumpversion", {})
# The tracking field may have drifted from the version actually written in
# files (e.g. bumpversion PR merged at 0.92.0, release-script then pushed
# 0.93.0 before the Concourse pipeline ran).  Build an ordered list of
# candidates so we try the most-likely-correct value first.
tracked_ver = bv.get("current_version", since)
candidates = list(dict.fromkeys(c for c in (since, tracked_ver) if c))

unescape = lambda s: s.replace("{{", "{").replace("}}", "}")

for file_config in bv.get("files", []):
    filename = file_config.get("filename")
    if not filename:
        continue
    search_tmpl = file_config.get("search", "{current_version}")
    repl_tmpl   = file_config.get("replace", "{new_version}")
    repl = unescape(repl_tmpl).replace("{new_version}", new_ver)
    path = pathlib.Path(filename)
    if not path.exists():
        print("Skipping " + filename + " (not found)", file=sys.stderr)
        continue
    content = path.read_text()

    replaced = False
    for candidate in candidates:
        search = unescape(search_tmpl).replace("{current_version}", candidate)
        if search in content:
            path.write_text(content.replace(search, repl, 1))
            print("Updated " + filename + " (replaced " + candidate + ")")
            replaced = True
            break

    if not replaced:
        # Last-resort: regex scan for any semver-looking version in the
        # search pattern so we can handle arbitrary drift.
        pattern = re.escape(unescape(search_tmpl)).replace(
            re.escape("{current_version}"), r"([0-9]+\.[0-9]+\.[0-9]+)"
        )
        m = re.search(pattern, content)
        if m:
            actual = m.group(1)
            search = unescape(search_tmpl).replace("{current_version}", actual)
            path.write_text(content.replace(search, repl, 1))
            print("Updated " + filename + " (detected " + actual + " via regex)")
        else:
            print("Warning: version string not found in " + filename, file=sys.stderr)

# Update the current_version tracking field.  An ABSENT key has to be
# inserted, not skipped: bump-my-version cannot determine a current version
# without it and crashes even when --new-version is supplied (verified against
# 1.5.1), and that is the only path a calver-to-calver bump takes.
# Scope the edit to the [tool.bumpversion] table -- "current_version" also
# appears inside the [[tool.bumpversion.files]] search/replace templates.
pyproject = pathlib.Path("pyproject.toml")
content = pyproject.read_text()
table = re.search(r"^\[tool\.bumpversion\][^\n]*\n", content, re.MULTILINE)
if not table:
    print(
        "Warning: no [tool.bumpversion] table in pyproject.toml; "
        "current_version not tracked",
        file=sys.stderr,
    )
else:
    head, rest = content[: table.end()], content[table.end() :]
    next_table = re.search(r"^\[", rest, re.MULTILINE)
    body = rest[: next_table.start()] if next_table else rest
    tail = rest[next_table.start() :] if next_table else ""
    key = re.search(r"^[ \t]*current_version[ \t]*=[^\n]*$", body, re.MULTILINE)
    new_line = 'current_version = "' + new_ver + '"'
    if key:
        body = body[: key.start()] + new_line + body[key.end() :]
        print("Updated pyproject.toml current_version -> " + new_ver)
    else:
        body = new_line + "\n" + body
        print("Inserted pyproject.toml current_version = " + new_ver)
    pyproject.write_text(head + body + tail)
"""

    return TaskStep(
        task=Identifier("bump-version"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource=image or TASK_IMAGE,
            inputs=inputs,
            outputs=[
                Output(name=Identifier(repo_id)),
            ],
            run=Command(
                path="bash",
                args=[
                    "-ec",
                    rf"""VERSION=$(cat {shlex.quote(version_file)})
SINCE_SEMVER=""
if [ -f {shlex.quote(version_input + "/since")} ]; then
    SINCE=$(cat {shlex.quote(version_input + "/since")})
    SINCE_STRIPPED="${{SINCE#v}}"
    if echo "$SINCE_STRIPPED" | grep -qE '^[0-9]{{1,3}}\.[0-9]+\.[0-9]+$'; then
        SINCE_SEMVER="$SINCE_STRIPPED"
    fi
fi
git -C {shlex.quote(repo_id)} config user.email {shlex.quote(git_email)}
git -C {shlex.quote(repo_id)} config user.name {shlex.quote(git_user)}
cd {shlex.quote(repo_id)}
PYPROJECT_VER=""
if [ -f pyproject.toml ]; then
    PYPROJECT_VER=$(echo 'import tomllib as t
bv=t.load(open("pyproject.toml","rb")).get("tool",{{}}).get("bumpversion",{{}})
print(bv.get("current_version",""))
' | python3 2>/dev/null || true)
fi
if [ -z "$SINCE_SEMVER" ]; then
    PYPROJECT_STRIPPED="${{PYPROJECT_VER#v}}"
    if echo "$PYPROJECT_STRIPPED" | grep -qE '^[0-9]{{1,3}}\.[0-9]+\.[0-9]+$'; then
        SINCE_SEMVER="$PYPROJECT_STRIPPED"
    fi
fi
# bump-my-version cannot run at all without [tool.bumpversion].current_version,
# so a repo missing that key goes through the transition script even when there
# is no semver baseline: it finds the in-file version by regex and seeds the
# key, and every release after this one takes the bump-my-version path.
NEEDS_SEED=0
if [ -f pyproject.toml ] && [ -z "$PYPROJECT_VER" ]; then
    NEEDS_SEED=1
fi
if [ -n "$SINCE_SEMVER" ] || [ "$NEEDS_SEED" = 1 ]; then
    python3 -c {shlex.quote(_transition_script)} "$SINCE_SEMVER" "$VERSION"
else
    bump-my-version bump --new-version "$VERSION" --no-commit --allow-dirty --verbose
fi""",
                ],
            ),
        ),
    )


# Generates a TaskStep to perform an instance refresh from a given set
# of filters and queires. The combination of filters and queries should
# be trusted to return one, and only one, autoscale group name.
def instance_refresh_task(
    filters: str,
    queries: str,
) -> TaskStep:
    """Generate a TaskStep that triggers an EC2 Auto Scaling instance refresh.

    :param filters: AWS CLI filter expression passed to
        ``describe-auto-scaling-groups --filters``.
    :param queries: JMESPath query expression that resolves to a single ASG name.
    :returns: A configured Concourse
        :class:`~ol_concourse.lib.models.pipeline.TaskStep`.
    """
    return TaskStep(
        task=Identifier("instance-refresh"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource={
                "type": REGISTRY_IMAGE,
                "source": {"repository": "amazon/aws-cli"},
            },
            params={},
            run=Command(
                path="bash",
                args=[
                    "-ec",
                    f"""ASG_NAME=$(aws autoscaling describe-auto-scaling-groups --color on --no-cli-auto-prompt --no-cli-pager --filters {filters} --query "{queries}" --output text);
                    aws autoscaling start-instance-refresh --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name "$ASG_NAME" --preferences MinHealthyPercentage=50,InstanceWarmup=120""",  # noqa: E501
                ],
            ),
        ),
    )


# Generates a TaskStep that can be used to block a job from completing until
# the most recent instance refresh is completed. If no instance refresh is
# found, the task finishes immediately. The combination filters + queries is
# expected to return one and only one autoscale group name.
def block_for_instance_refresh_task(
    filters: str,
    queries: str,
    check_freq: int = 10,
) -> TaskStep:
    """Generate a TaskStep that blocks until the latest EC2 instance refresh completes.

    Polls ``describe-instance-refreshes`` every ``check_freq`` seconds until the
    refresh leaves ``InProgress``, ``Pending``, or ``Canceling`` state.  If no
    refresh is found the task exits immediately.

    :param filters: AWS CLI filter expression passed to
        ``describe-auto-scaling-groups --filters``.
    :param queries: JMESPath query that resolves to a single ASG name.
    :param check_freq: Polling interval in seconds (default: ``10``).
    :returns: A configured Concourse
        :class:`~ol_concourse.lib.models.pipeline.TaskStep`.
    """
    return TaskStep(
        task=Identifier("block-for-instance-refresh"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource={
                "type": REGISTRY_IMAGE,
                "source": {"repository": "amazon/aws-cli"},
            },
            params={},
            run=Command(
                path="bash",
                args=[
                    "-evc",
                    f""" ASG_NAME=$(aws autoscaling describe-auto-scaling-groups --color on --no-cli-auto-prompt --no-cli-pager --filters {filters} --query "{queries}" --output text);
                    status="InProgress"
                    while [ "$status" = "InProgress" ] || [ "$status" == "Pending" ] || [ "$status" == "Canceling" ]
                    do
                        sleep {check_freq}
                        status=$(aws autoscaling describe-instance-refreshes --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name "$ASG_NAME" --query "sort_by(InstanceRefreshes, &StartTime)[].{{Status: Status}}" --output text | tail -n 1)
                        aws autoscaling describe-instance-refreshes --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name $ASG_NAME --query "sort_by(InstanceRefreshes, &StartTime)[].{{InstanceRefreshId: InstanceRefreshId, StartTime: StartTime, Status: Status}}" --output text | tail -n 1
                    done""",  # noqa: E501
                ],
            ),
        ),
    )
