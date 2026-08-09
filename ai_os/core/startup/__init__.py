"""Startup Generator Core Module.

This package provides data models, brief parsers, and scaffold generators for
the `ai-os startup` command pipeline (see docs/20_STARTUP_GENERATOR.md).
"""

from __future__ import annotations

from ai_os.core.startup.brief import DesignBrief, parse_startup_brief
from ai_os.core.startup.generator import generate_startup, write_scaffold

__all__ = [
    "DesignBrief",
    "parse_startup_brief",
    "generate_startup",
    "write_scaffold",
]