"""Unit tests for DesignBrief data model and parse_startup_brief parser."""

from __future__ import annotations

from pathlib import Path
import pytest

from ai_os.core.startup.brief import DesignBrief, parse_startup_brief

HUNGARIAN_BRIEF_FIXTURE = """# Startup brief

## Név + egymondatos value prop
FreshBox — heti dobozos, helyi termelői zöldség-előfizetés budapesti háztartásoknak.

## Célközönség
Egészségtudatos, elfoglalt 28–45 évesek, akik támogatnák a helyi termelőket.

## A demó fő flow-ja (EZT szimuláljuk működőként)
1. Kiválaszt egy doboz-méretet és gyakoriságot.
2. Megnézi a heti dobozt (mock termék-lista), cserél 1-2 tételt.
3. „Előfizet" (fake checkout, fake fizetés), lát egy megerősítést + egy dashboard-ot.

## Oldalak
Landing, Hogyan működik, Árazás, Termék-demó (interaktív), Dashboard (szimulált).

## Márka / hangnem
Friss, zöld, barátságos, minimál. Kézzel rajzolt zöldség-illusztrációk hangulat.

## Amit NEM kell
Valódi fizetés, valódi user-fiók, admin, e-mail — minden szimulált.
"""

ENGLISH_BRIEF_FIXTURE = """# Startup Brief: TaskFlow

## Name & Value Proposition
TaskFlow - Automated workflow management for remote software engineering teams.

## Target Audience
Engineering managers and team leads looking to streamline async code reviews.

## Core Flow
1. Connect repository.
2. Configure automated review rules.
3. View review status dashboard.

## Pages
Home, Features, Pricing, Live Demo

## Brand / Tone
Modern dark mode, developer-focused, clean aesthetics.

## Sim Model
Repository, Rule, ReviewResult, User

## Non-Requirements
- Real GitHub API integration
- Actual payment processing
"""

INLINE_PROMPT_FIXTURE = (
    "EcoTrack — Track your personal carbon footprint daily. "
    "Audience: Eco-conscious individuals. "
    "Pages: Home, Tracker, Insights. "
    "Flow: Log activity, View impact score, Get recommendations. "
    "Brand: Minimalist emerald theme."
)


def test_parse_hungarian_markdown_brief():
    """Test parsing a Hungarian structured markdown brief string."""
    brief = parse_startup_brief(HUNGARIAN_BRIEF_FIXTURE)

    assert brief.name == "FreshBox"
    assert brief.value_prop == "heti dobozos, helyi termelői zöldség-előfizetés budapesti háztartásoknak."
    assert "Egészségtudatos" in brief.target_audience
    assert len(brief.core_flow) == 3
    assert brief.core_flow[0] == "Kiválaszt egy doboz-méretet és gyakoriságot."
    assert len(brief.pages) == 5
    assert "Landing" in brief.pages
    assert "Termék-demó (interaktív)" in brief.pages
    assert "Friss, zöld" in brief.brand_tone
    assert len(brief.non_requirements) > 0
    assert brief.raw_prompt == HUNGARIAN_BRIEF_FIXTURE.strip()


def test_parse_english_markdown_brief():
    """Test parsing an English structured markdown brief string with sim model and list non-requirements."""
    brief = parse_startup_brief(ENGLISH_BRIEF_FIXTURE)

    assert brief.name == "TaskFlow"
    assert brief.value_prop == "Automated workflow management for remote software engineering teams."
    assert "Engineering managers" in brief.target_audience
    assert brief.core_flow == [
        "Connect repository.",
        "Configure automated review rules.",
        "View review status dashboard.",
    ]
    assert brief.pages == ["Home", "Features", "Pricing", "Live Demo"]
    assert brief.brand_tone == "Modern dark mode, developer-focused, clean aesthetics."
    assert brief.sim_entities == ["Repository", "Rule", "ReviewResult", "User"]
    assert brief.non_requirements == [
        "Real GitHub API integration",
        "Actual payment processing",
    ]


def test_parse_inline_text_prompt():
    """Test parsing a single-line unformatted prompt string with inline key-value phrases."""
    brief = parse_startup_brief(INLINE_PROMPT_FIXTURE)

    assert brief.name == "EcoTrack"
    assert "Track your personal carbon footprint daily" in brief.value_prop
    assert brief.target_audience == "Eco-conscious individuals"
    assert brief.pages == ["Home", "Tracker", "Insights"]
    assert brief.core_flow == ["Log activity", "View impact score", "Get recommendations"]
    assert brief.brand_tone == "Minimalist emerald theme"


def test_parse_from_file_path(tmp_path: Path):
    """Test parsing brief from a Path file object."""
    brief_file = tmp_path / "startup.md"
    brief_file.write_text(HUNGARIAN_BRIEF_FIXTURE, encoding="utf-8")

    brief = parse_startup_brief(brief_file)
    assert brief.name == "FreshBox"
    assert len(brief.pages) == 5

    # Also test passing str representation of Path
    brief_str = parse_startup_brief(str(brief_file))
    assert brief_str.name == "FreshBox"


def test_design_brief_serialization():
    """Test to_dict and from_dict serialization roundtrip on DesignBrief."""
    original = DesignBrief(
        name="TestApp",
        value_prop="Testing app serialization",
        target_audience="Developers",
        core_flow=["Step 1", "Step 2"],
        pages=["Landing", "Dashboard"],
        brand_tone="Sleek dark theme",
        sim_entities=["User", "Item"],
        non_requirements=["Real Auth"],
        raw_prompt="Raw test prompt text",
    )

    data_dict = original.to_dict()
    assert isinstance(data_dict, dict)
    assert data_dict["name"] == "TestApp"
    assert data_dict["core_flow"] == ["Step 1", "Step 2"]

    restored = DesignBrief.from_dict(data_dict)
    assert restored == original


def test_edge_cases_empty_and_minimal_inputs():
    """Test fallback behavior for empty or minimal prompt inputs."""
    empty_brief = parse_startup_brief("")
    assert empty_brief.name == "Untitled Startup"
    assert empty_brief.pages == ["Landing", "Demo"]
    assert empty_brief.core_flow == ["Interactive Product Demo"]
    assert empty_brief.raw_prompt == ""

    minimal_brief = parse_startup_brief("Just a raw unformatted text prompt about an AI tool.")
    assert minimal_brief.name == "Untitled Startup" or minimal_brief.name == "Just a raw unformatted text prompt about an AI tool."
    assert len(minimal_brief.pages) > 0
    assert len(minimal_brief.core_flow) > 0
    assert minimal_brief.raw_prompt == "Just a raw unformatted text prompt about an AI tool."