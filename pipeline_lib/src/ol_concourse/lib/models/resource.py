"""Pydantic models for Concourse resource source configurations."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Git(BaseModel):
    """Source configuration for Concourse's built-in ``git`` resource type.

    ``version_type`` is what the resource versions on, and it changes which
    other fields are consulted. Under the default ``commits`` the resource
    walks ``branch`` and honours ``paths``/``ignore_paths``. Under ``tags`` it
    fetches ``refs/tags/*`` straight from ``uri``: ``branch`` and ``paths`` are
    both ignored, so a matching tag anywhere in the repo produces a version
    whether or not those paths changed. Setting ``tag_regex`` alone does not
    switch it -- the resource's ``check`` dispatches on ``version_type``.

    The three values are not interchangeable beyond that, and the differences
    only surface at run time. ``out`` exits 1 for both ``tags`` and
    ``branches``, so a resource that anything ``put``s to has to stay on
    ``commits``; switching a bidirectional resource over breaks the put step,
    not the set-pipeline. A ``branches`` ``get`` also writes only a
    ``branches.json`` listing into the destination, never a checkout, so steps
    downstream of it have no working tree to read.
    """

    uri: str
    branch: str = "main"
    version_type: Literal["commits", "tags", "branches"] | None = None
    paths: list[Path] | None = None
    private_key: str | None = None
    ignore_paths: list[Path] | None = None
    fetch_tags: bool = False
    tag_regex: str | None = None
    version_depth: int | None = None
    model_config = ConfigDict(extra="allow")
