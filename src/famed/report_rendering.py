#!/usr/bin/env python3
"""Small shared helpers for deterministic Markdown report rendering."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from famed.person_sync import parse_front_matter


def render_yaml_front_matter(front_matter: Mapping[str, Any]) -> str:
    """Render compact YAML front matter using the repo's existing settings."""
    return yaml.safe_dump(dict(front_matter), sort_keys=False, allow_unicode=False).strip()


def markdown_table_cell(value: Any) -> str:
    """Normalize a value for Markdown table output."""
    if value is None:
        return ""
    return " ".join(str(value).split()).replace("|", r"\|").replace("\n", " ")


def split_front_matter(markdown_content: str) -> tuple[dict[str, Any], str]:
    """Return parsed front matter and the remaining Markdown body."""
    if not markdown_content.startswith("---\n"):
        return {}, markdown_content

    end_marker_index = markdown_content.find("\n---\n", 4)
    if end_marker_index == -1:
        return {}, markdown_content

    front_matter = parse_front_matter(markdown_content)
    body = markdown_content[end_marker_index + len("\n---\n") :]
    return front_matter, body


def strip_leading_report_chrome(body: str) -> str:
    """Drop the leading report heading/backlink chrome from a derived report body."""
    lines = body.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]

    while lines and not lines[0].strip():
        lines = lines[1:]

    if lines and lines[0].startswith("[[") and ("<-" in lines[0] or "←" in lines[0]):
        lines = lines[1:]

    while lines and not lines[0].strip():
        lines = lines[1:]

    return "\n".join(lines).rstrip()
