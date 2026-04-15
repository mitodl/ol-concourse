"""Concourse resource for registering GitHub Deployments and Deployment Statuses.

Usage example::

    resource_types:
      - name: github-deployments
        type: registry-image
        source:
          repository: mitodl/concourse-github-deployments-resource
          tag: latest

    resources:
      - name: github-deployment
        type: github-deployments
        check_every: never
        source:
          repository: mitodl/my-app
          environment: RC
          access_token: ((github.access_token))

    jobs:
      - name: deploy-rc
        plan:
          - put: github-deployment
            params:
              action: start
              ref: release/2025.04.14.1
              description: "RC deployment started"
          - task: kubectl-rollout-status
            ...
          - put: github-deployment
            params:
              action: finish
              deployment_file: github-deployment/deployment.json
              state: success
              environment_url: https://rc.my-app.example.com
"""

import json
from pathlib import Path
from typing import Literal

from concoursetools import BuildMetadata, ConcourseResource
from concoursetools.version import SortableVersionMixin, Version
from github import Auth, Consts
from github import Github
from github.GithubObject import NotSet

ISO_8601_FORMAT = "%Y-%m-%dT%H:%M:%S"

VALID_START_STATES = frozenset({"in_progress", "queued", "pending"})
VALID_FINISH_STATES = frozenset({"success", "failure", "error", "inactive"})


class GithubDeploymentVersion(Version, SortableVersionMixin):
    """A Concourse version representing a single GitHub Deployment.

    Version identity is stable: a deployment's ID never changes, and we do not
    include the mutable deployment status state so that a status transition
    (``in_progress`` → ``success``) does not produce a phantom second version
    for the same deployment.
    """

    def __init__(
        self,
        deployment_id: str,
        environment: str,
        sha: str,
        created_at: str,
    ):
        """Initialize version with deployment_id, environment, sha, and created_at."""
        self.deployment_id = deployment_id
        self.environment = environment
        self.sha = sha
        self.created_at = created_at

    def __lt__(self, other: "GithubDeploymentVersion") -> bool:
        """Return True if this version has a lower deployment_id than other."""
        return int(self.deployment_id) < int(other.deployment_id)


class ConcourseGithubDeploymentsResource(ConcourseResource):
    """Concourse resource for creating and tracking GitHub Deployment records.

    Intended to be used as a ``put``-only resource (``check_every: never``).
    Supports a two-step deployment flow:

    - ``action: start`` — creates a new GitHub Deployment and immediately sets
      its status to ``in_progress``.
    - ``action: finish`` — reads a prior deployment's ID from a JSON file and
      creates a final status (``success``, ``failure``, or ``error``).
    """

    def __init__(
        self,
        /,
        repository: str,
        environment: str,
        access_token: str,
        gh_host: str = Consts.DEFAULT_BASE_URL,
    ):
        """Initialize the resource with repository credentials and environment."""
        super().__init__(GithubDeploymentVersion)
        auth = Auth.Token(access_token)
        self.gh = Github(base_url=gh_host, auth=auth)
        self.repo = self.gh.get_repo(repository)
        self.environment = environment

    def _to_version(self, deployment) -> GithubDeploymentVersion:
        return GithubDeploymentVersion(
            deployment_id=str(deployment.id),
            environment=deployment.environment,
            sha=deployment.sha,
            created_at=deployment.created_at.strftime(ISO_8601_FORMAT),
        )

    def _latest_status(self, deployment):
        """Return the most recent DeploymentStatus for the given deployment, or None.

        The GitHub API returns statuses with the most recent first, so we
        simply take the first element rather than fetching and sorting all of them.
        """
        try:
            return deployment.get_statuses()[0]
        except IndexError:
            return None

    def fetch_new_versions(
        self, previous_version: GithubDeploymentVersion | None = None
    ) -> set[GithubDeploymentVersion]:
        """Return GitHub Deployments for the configured environment.

        When no previous version is known, returns only the most recent
        deployment.  When a previous version is provided, returns all
        deployments whose ID is strictly greater than the previous deployment's
        ID (stopping early once we pass it, since the API returns
        newest-first).

        :param previous_version: The last version seen by Concourse, or None.
        :returns: Set of new :class:`GithubDeploymentVersion` instances.
        """
        deployments = self.repo.get_deployments(environment=self.environment)

        if previous_version is None:
            for dep in deployments:
                return {self._to_version(dep)}
            return set()

        versions: set[GithubDeploymentVersion] = set()
        for dep in deployments:
            if int(dep.id) <= int(previous_version.deployment_id):
                break
            versions.add(self._to_version(dep))
        return versions

    def download_version(
        self,
        version: GithubDeploymentVersion,
        destination_dir: str,
        build_metadata: BuildMetadata,
    ) -> tuple[GithubDeploymentVersion, dict[str, str]]:
        """Fetch deployment metadata and write it to ``deployment.json``.

        The file contains all fields needed by a subsequent ``action: finish``
        put step, including ``deployment_id``, ``sha``, ``ref``,
        ``environment``, and the latest status state.

        :param version: The deployment version to fetch.
        :param destination_dir: Directory where ``deployment.json`` is written.
        :param build_metadata: Concourse build metadata (unused, required by protocol).
        :returns: The fetched version and an empty metadata dict.
        """
        dep = self.repo.get_deployment(int(version.deployment_id))
        latest_status = self._latest_status(dep)
        data = {
            "deployment_id": str(dep.id),
            "sha": dep.sha,
            "ref": dep.ref,
            "environment": dep.environment,
            "description": dep.description or "",
            "created_at": dep.created_at.strftime(ISO_8601_FORMAT),
            "state": latest_status.state if latest_status else "pending",
            "environment_url": (
                latest_status.environment_url
                if latest_status and latest_status.environment_url
                else ""
            ),
            "log_url": (
                latest_status.log_url
                if latest_status and latest_status.log_url
                else ""
            ),
        }
        Path(destination_dir).joinpath("deployment.json").write_text(
            json.dumps(data, indent=2)
        )
        return version, {}

    def publish_new_version(  # noqa: PLR0913
        self,
        sources_dir: str,
        build_metadata: BuildMetadata,
        action: Literal["start", "finish"] = NotSet,
        ref: str | None = None,
        deployment_file: str | None = None,
        state: str | None = None,
        environment_url: str | None = None,
        description: str | None = None,
        auto_merge: bool = False,
        required_contexts: list[str] | None = None,
        auto_inactive: bool = True,
    ) -> tuple[GithubDeploymentVersion, dict[str, str]]:
        """Create or update a GitHub Deployment and Deployment Status.

        Two ``action`` values are supported:

        **start**
            Creates a new GitHub Deployment for ``ref`` and immediately sets its
            status to ``in_progress``.  Requires ``ref``.

        **finish**
            Reads the deployment ID from ``deployment_file`` (a path relative to
            ``sources_dir``, typically pointing to the ``deployment.json`` written
            by a prior ``get`` step) and creates a terminal Deployment Status.
            Requires ``deployment_file`` and ``state`` (one of ``success``,
            ``failure``, ``error``, ``inactive``).

        :param sources_dir: Root directory of the put step's task inputs.
        :param build_metadata: Concourse build metadata.
        :param action: ``"start"`` or ``"finish"`` (required).
        :param ref: Git ref, branch, or tag to deploy (required for ``start``).
        :param deployment_file: Path relative to ``sources_dir`` to a JSON file
            containing at least ``"deployment_id"`` (required for ``finish``).
        :param state: Terminal deployment status state (required for ``finish``).
            Must be one of ``success``, ``failure``, ``error``, ``inactive``.
        :param environment_url: URL of the deployed environment.
        :param description: Human-readable description shown in the GitHub UI.
        :param auto_merge: Whether GitHub should auto-merge the default branch
            into ``ref`` before creating the deployment (default: ``False``).
        :param required_contexts: Status check contexts that must pass before
            the deployment is allowed.  Omit to inherit the branch protection
            settings; pass ``[]`` to bypass all checks.
        :param auto_inactive: Mark all older deployments in the same environment
            as ``inactive`` when this status is set (default: ``True``).
        :returns: A :class:`GithubDeploymentVersion` representing the deployment
            and an empty metadata dict.
        :raises ValueError: If required parameters are missing or invalid.
        """
        if action not in ("start", "finish"):
            msg = f"action must be 'start' or 'finish', got {action!r}"
            raise ValueError(msg)

        if action == "start":
            return self._start(
                ref=ref,
                description=description,
                auto_merge=auto_merge,
                required_contexts=required_contexts,
                environment_url=environment_url,
                auto_inactive=auto_inactive,
            )

        return self._finish(
            sources_dir=sources_dir,
            deployment_file=deployment_file,
            state=state,
            environment_url=environment_url,
            description=description,
            auto_inactive=auto_inactive,
        )

    def _start(  # noqa: PLR0913
        self,
        ref: str | None,
        description: str | None,
        auto_merge: bool,
        required_contexts: list[str] | None,
        environment_url: str | None,
        auto_inactive: bool,
    ) -> tuple[GithubDeploymentVersion, dict[str, str]]:
        """Create a new deployment and set its status to ``in_progress``."""
        if not ref:
            msg = "ref is required for action=start"
            raise ValueError(msg)

        dep = self.repo.create_deployment(
            ref=ref,
            environment=self.environment,
            description=description if description is not None else NotSet,
            auto_merge=auto_merge,
            required_contexts=(
                required_contexts if required_contexts is not None else NotSet
            ),
            task="deploy",
        )
        dep.create_status(
            state="in_progress",
            description=description if description is not None else NotSet,
            environment=self.environment,
            environment_url=environment_url if environment_url is not None else NotSet,
            auto_inactive=auto_inactive,
        )
        return self._to_version(dep), {}

    def _finish(  # noqa: PLR0913
        self,
        sources_dir: str,
        deployment_file: str | None,
        state: str | None,
        environment_url: str | None,
        description: str | None,
        auto_inactive: bool,
    ) -> tuple[GithubDeploymentVersion, dict[str, str]]:
        """Read a deployment ID from a file and create a terminal status."""
        if not deployment_file:
            msg = "deployment_file is required for action=finish"
            raise ValueError(msg)
        if not state:
            msg = "state is required for action=finish"
            raise ValueError(msg)
        if state not in VALID_FINISH_STATES:
            msg = (
                f"state must be one of {sorted(VALID_FINISH_STATES)} for action=finish,"
                f" got {state!r}"
            )
            raise ValueError(msg)

        dep_path = Path(sources_dir) / deployment_file
        if not dep_path.exists():
            msg = f"deployment_file not found: {dep_path}"
            raise FileNotFoundError(msg)

        dep_data = json.loads(dep_path.read_text())
        if "deployment_id" not in dep_data:
            msg = f"deployment_file {dep_path} does not contain 'deployment_id'"
            raise ValueError(msg)

        dep = self.repo.get_deployment(int(dep_data["deployment_id"]))
        dep.create_status(
            state=state,
            description=description if description is not None else NotSet,
            environment=self.environment,
            environment_url=environment_url if environment_url is not None else NotSet,
            auto_inactive=auto_inactive,
        )
        return self._to_version(dep), {}
