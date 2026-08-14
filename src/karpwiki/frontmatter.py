"""Required frontmatter for all wiki page types (01 §6).

The global tag minimum is 2; a workspace's SCHEMA.md may raise it or require additional
specific tags (`page_conventions`, 09 §6), so both are parameters here.
"""

import re
import uuid
from collections.abc import Sequence
from datetime import date

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import PageStatus, PageType

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

DEFAULT_REQUIRED_TAGS_MIN = 2


class FrontmatterError(ValueError):
    """Raised when a page's frontmatter is missing or invalid."""


class Frontmatter(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    date: date
    tags: list[str]
    page_type: PageType
    workspace_id: str = Field(min_length=1)
    status: PageStatus
    current_version: uuid.UUID


def split_frontmatter(document: str) -> tuple[dict, str]:
    """Split a markdown document into its YAML frontmatter block and body."""
    match = _FRONTMATTER_RE.match(document)
    if match is None:
        raise FrontmatterError("document has no `---` frontmatter block")
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FrontmatterError("frontmatter must be a YAML mapping")
    return parsed, match.group(2)


def validate_frontmatter(
    data: dict,
    *,
    required_tags_min: int = DEFAULT_REQUIRED_TAGS_MIN,
    additional_required_tags: Sequence[str] = (),
) -> Frontmatter:
    try:
        frontmatter = Frontmatter.model_validate(data)
    except ValidationError as exc:
        raise FrontmatterError(str(exc)) from exc

    if len(frontmatter.tags) < required_tags_min:
        raise FrontmatterError(
            f"at least {required_tags_min} tags are required, got {len(frontmatter.tags)}"
        )
    missing = [tag for tag in additional_required_tags if tag not in frontmatter.tags]
    if missing:
        raise FrontmatterError(f"missing workspace-required tag(s): {', '.join(missing)}")
    return frontmatter
