"""Pydantic models for Concourse resource source configurations."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Git(BaseModel):
    """Source configuration for Concourse's built-in ``git`` resource type."""

    uri: str
    branch: str = "main"
    paths: list[Path] | None = None
    private_key: str | None = None
    ignore_paths: list[Path] | None = None
    fetch_tags: bool = False
    tag_regex: str | None = None
    version_depth: int | None = None
    model_config = ConfigDict(extra="allow")
