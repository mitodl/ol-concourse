"""Constants shared across the ol-concourse pipeline DSL."""

REGISTRY_IMAGE = "registry-image"

# Default GitHub repository used for the GitHub Issues resource in release pipelines.
GH_ISSUES_DEFAULT_REPOSITORY = "ol-platform-eng/concourse-workflow"

# Credential references for the single shared GitHub App used by every resource
# in the release workflow (release, github-issues, github-deployments).
#
# Deliberately one App rather than one per resource type or per token family:
# an App installation mints short-lived tokens on demand, so there is exactly
# one registration to monitor and one private key to rotate, instead of a set
# of fine-grained PATs that expire silently and take a release pipeline down
# with a 401 when they do.
#
# Point every ``auth_method="app"`` resource at these, and grant the App the
# union of the permissions those resources need (contents:write for release
# branches/tags, issues:write for the release gate, deployments:write for
# deployment records).
GITHUB_APP_ID = "((github_app.app_id))"
GITHUB_APP_INSTALLATION_ID = "((github_app.installation_id))"
GITHUB_APP_PRIVATE_KEY = "((github_app.private_key))"
