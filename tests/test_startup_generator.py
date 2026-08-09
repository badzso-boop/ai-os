"""Unit tests for generate_startup and write_scaffold in ai_os.core.startup.generator."""

from __future__ import annotations

from pathlib import Path
import pytest

from ai_os.core.startup.brief import DesignBrief
from ai_os.core.startup.generator import generate_startup, write_scaffold


def test_write_scaffold_creates_structure(tmp_path: Path) -> None:
    """Test that write_scaffold creates the expected file and directory structure."""
    out_dir = tmp_path / "scaffold_test"
    res_path = write_scaffold(out_dir, preset="startup")

    assert res_path == out_dir
    assert (out_dir / "index.html").exists()
    assert (out_dir / "styles" / "tokens.css").exists()
    assert (out_dir / "sim" / "seed.js").exists()

    html_content = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "<title>" in html_content
    assert '<link rel="stylesheet" href="styles/tokens.css">' in html_content
    assert '<script src="sim/seed.js">' in html_content


def test_generate_startup_injects_brief_data(tmp_path: Path) -> None:
    """Test that generate_startup populates index.html, tokens.css, and seed.js with brief data."""
    out_dir = tmp_path / "my_startup"
    brief = DesignBrief(
        title="FreshBox",
        value_proposition="Fresh organic vegetable boxes delivered weekly to your door.",
        target_audience="Busy families and eco-conscious foodies.",
        pages=["Landing", "Subscription", "Weekly Catalog", "Dashboard"],
        core_flow=["Select box size", "Customize contents", "Checkout", "Track delivery"],
        brand="Minimalist emerald theme with clean typography",
        sim_entities=["BoxPreset", "ProduceItem", "SubscriptionOrder", "CustomerProfile"],
    )

    res_path = generate_startup(out_dir, brief)

    assert res_path == out_dir

    # 1. Verify index.html injection
    html_content = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "FreshBox" in html_content
    assert "Fresh organic vegetable boxes delivered weekly to your door." in html_content
    assert "Subscription" in html_content
    assert "Customize contents" in html_content

    # 2. Verify styles/tokens.css injection
    css_content = (out_dir / "styles" / "tokens.css").read_text(encoding="utf-8")
    assert '--brand-title: "FreshBox";' in css_content
    assert "Minimalist emerald theme" in css_content
    # Check emerald theme primary color injection
    assert "--primary-color: #10b981;" in css_content

    # 3. Verify sim/seed.js injection
    js_content = (out_dir / "sim" / "seed.js").read_text(encoding="utf-8")
    assert "const simEntities = [" in js_content
    assert "BoxPreset" in js_content
    assert "ProduceItem" in js_content
    assert "SubscriptionOrder" in js_content
    assert "CustomerProfile" in js_content
    assert "FreshBox" in js_content


def test_generate_startup_minimal_brief(tmp_path: Path) -> None:
    """Test generate_startup with a minimal / default brief."""
    out_dir = tmp_path / "minimal_startup"
    brief = DesignBrief(title="AcmeCore")

    generate_startup(out_dir, brief)

    html_content = (out_dir / "index.html").read_text(encoding="utf-8")
    css_content = (out_dir / "styles" / "tokens.css").read_text(encoding="utf-8")
    js_content = (out_dir / "sim" / "seed.js").read_text(encoding="utf-8")

    assert "AcmeCore" in html_content
    assert '--brand-title: "AcmeCore";' in css_content
    assert "const simEntities = []" in js_content