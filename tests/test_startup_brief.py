"""Unit tests for DesignBrief data model and parse_startup_brief parser."""

from __future__ import annotations

from pathlib import Path
import pytest

from ai_os.core.startup.brief import (
    SECTION_PATTERNS,
    DesignBrief,
    _extract_inline_field,
    _match_section_category,
    _normalize_header,
    _parse_bullet_or_numbered_list,
    _parse_comma_or_bullet_list,
    parse_startup_brief,
)

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


def test_header_normalization():
    """Test _normalize_header removes markdown symbols, accents, casing, and surrounding whitespace."""
    assert _normalize_header("## Célközönség") == "celkozonseg"
    assert _normalize_header("### Fő flow") == "fo flow"
    assert _normalize_header("# Márka / hangnem") == "marka / hangnem"
    assert _normalize_header("##  Szimulált Entitások  ") == "szimulalt entitasok"
    assert _normalize_header("### TARGET AUDIENCE") == "target audience"
    assert _normalize_header("# Árazás & Funkciók") == "arazas & funkciok"
    assert _normalize_header("") == ""
    assert _normalize_header("   ") == ""
    assert _normalize_header("### ") == ""


def test_regex_section_pattern_matching():
    """Test _match_section_category matching normalized headers to categories."""
    assert _match_section_category("nev") == "name_val"
    assert _match_section_category("name") == "name_val"
    assert _match_section_category("overview") == "name_val"

    assert _match_section_category("celkozonseg") == "audience"
    assert _match_section_category("target audience") == "audience"
    assert _match_section_category("users") == "audience"

    assert _match_section_category("core flow") == "flow"
    assert _match_section_category("fo flow") == "flow"
    assert _match_section_category("steps") == "flow"

    assert _match_section_category("oldalak") == "pages"
    assert _match_section_category("pages") == "pages"
    assert _match_section_category("screens") == "pages"

    assert _match_section_category("marka") == "brand"
    assert _match_section_category("brand") == "brand"
    assert _match_section_category("style") == "brand"

    assert _match_section_category("sim model") == "entities"
    assert _match_section_category("entities") == "entities"
    assert _match_section_category("mock entities") == "entities"

    assert _match_section_category("amit nem kell") == "non_reqs"
    assert _match_section_category("non-requirements") == "non_reqs"
    assert _match_section_category("out of scope") == "non_reqs"

    assert _match_section_category("unmatched section title") is None


def test_section_pattern_matching_in_markdown_parsing():
    """Test parsing markdown brief using alternative header aliases for all section categories."""
    markdown_content = """# AppName — Best app ever

## Overview
AppName — Best app ever

## Users
Freelancers and small agencies.

## Steps
1. Log in.
2. Create project.
3. Export report.

## Screens
Dashboard, Settings, Reports

## Style
Dark mode glassmorphism theme.

## Mock Entities
Project, Report, User

## Out of Scope
- Mobile app version
- Push notifications
"""
    brief = parse_startup_brief(markdown_content)

    assert brief.name == "AppName"
    assert brief.value_prop == "Best app ever"
    assert brief.target_audience == "Freelancers and small agencies."
    assert brief.core_flow == ["Log in.", "Create project.", "Export report."]
    assert brief.pages == ["Dashboard", "Settings", "Reports"]
    assert brief.brand_tone == "Dark mode glassmorphism theme."
    assert brief.sim_entities == ["Project", "Report", "User"]
    assert brief.non_requirements == ["Mobile app version", "Push notifications"]


def test_empty_input_fallback_defaults():
    """Test fallback defaults when parsing empty string or whitespace-only inputs."""
    for empty_input in ["", "   ", "\n\n\t  \n"]:
        brief = parse_startup_brief(empty_input)
        assert brief.name == "Untitled Startup"
        assert brief.value_prop == ""
        assert brief.target_audience == ""
        assert brief.core_flow == ["Interactive Product Demo"]
        assert brief.pages == ["Landing", "Demo"]
        assert brief.brand_tone == ""
        assert brief.sim_entities == []
        assert brief.non_requirements == []
        assert brief.raw_prompt == ""


def test_partial_input_fallback_defaults():
    """Test that missing fields fall back to default values while retaining parsed fields."""
    partial_md = """# CustomApp

## Value Prop
CustomApp - Simple accounting for freelancers.
"""
    brief = parse_startup_brief(partial_md)

    assert brief.name == "CustomApp"
    assert brief.value_prop == "Simple accounting for freelancers."
    assert brief.target_audience == ""
    # Defaults applied for missing required lists:
    assert brief.pages == ["Landing", "Demo"]
    assert brief.core_flow == ["Interactive Product Demo"]
    assert brief.brand_tone == ""
    assert brief.sim_entities == []
    assert brief.non_requirements == []


def test_design_brief_dataclass_defaults_and_dict_deserialization():
    """Test DesignBrief dataclass default initial values and empty dict deserialization."""
    default_brief = DesignBrief()
    assert default_brief.name == ""
    assert default_brief.value_prop == ""
    assert default_brief.target_audience == ""
    assert default_brief.core_flow == []
    assert default_brief.pages == []
    assert default_brief.brand_tone == ""
    assert default_brief.sim_entities == []
    assert default_brief.non_requirements == []
    assert default_brief.raw_prompt == ""

    from_empty_dict = DesignBrief.from_dict({})
    assert from_empty_dict == default_brief


def test_list_parsing_helper_functions():
    """Test bullet/numbered list and comma list helper parsing functions."""
    numbered_text = "1. First step\n2. Second step\n- Third step\n• Fourth step"
    assert _parse_bullet_or_numbered_list(numbered_text) == [
        "First step",
        "Second step",
        "Third step",
        "Fourth step",
    ]
    assert _parse_bullet_or_numbered_list("") == []

    comma_text = "Page 1, Page 2, Page 3"
    assert _parse_comma_or_bullet_list(comma_text) == ["Page 1", "Page 2", "Page 3"]
    assert _parse_comma_or_bullet_list("") == []


def test_inline_field_extraction():
    """Test _extract_inline_field with inline section patterns."""
    text = "Name: TestApp. Audience: Programmers. Pages: Home, Settings. Tone: Dark."
    assert _extract_inline_field(text, "Audience") == "Programmers"
    assert _extract_inline_field(text, "Pages") == "Home, Settings"
    assert _extract_inline_field(text, "Tone") == "Dark"
    assert _extract_inline_field(text, "NonExistentKey") is None