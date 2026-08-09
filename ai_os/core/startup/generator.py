"""Startup Generator Pipeline and Scaffold Builder.

This module implements `write_scaffold` and `generate_startup` for the `ai-os startup`
pipeline. It deterministically creates project directory structures, generates HTML/CSS/JS
scaffold templates, and injects brief details (title, value proposition, design brand tokens,
and simulation entities) into generated files.

Architecture & Design Principles:
- Compiler First: Generation is 100% deterministic template parsing and string manipulation.
- Zero external network or LLM dependencies.
- Generates web assets (index.html, styles/tokens.css, sim/seed.js) ready for local previewing.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
import re

from ai_os.core.startup.brief import DesignBrief


def _get_default_html_scaffold(preset: str = "startup") -> str:
    """Return default HTML template string for scaffold."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{TITLE}}</title>
    <link rel="stylesheet" href="styles/tokens.css">
</head>
<body>
    <header class="hero">
        <h1 id="app-title">{{TITLE}}</h1>
        <p id="value-prop" class="subtitle">{{VALUE_PROPOSITION}}</p>
    </header>
    <main class="content">
        <section id="pages-section">
            <h2>Pages</h2>
            <ul id="pages-list">
{{PAGES_ITEMS}}
            </ul>
        </section>
        <section id="core-flow-section">
            <h2>Core Flow</h2>
            <ol id="core-flow-list">
{{CORE_FLOW_ITEMS}}
            </ol>
        </section>
    </main>
    <script src="sim/seed.js"></script>
</body>
</html>
"""


def _get_default_css_scaffold(preset: str = "startup") -> str:
    """Return default CSS design tokens template string."""
    return """:root {
  --brand-title: "{{BRAND_TITLE}}";
  --brand-tone: "{{BRAND_TONE}}";
  --primary-color: #2563eb;
  --accent-color: #38bdf8;
  --bg-color: #0f172a;
  --text-color: #f8fafc;
  --font-family: system-ui, -apple-system, sans-serif;
}

body {
  background-color: var(--bg-color);
  color: var(--text-color);
  font-family: var(--font-family);
  margin: 0;
  padding: 2rem;
}

.hero {
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  padding-bottom: 1.5rem;
  margin-bottom: 2rem;
}

.subtitle {
  color: var(--accent-color);
  font-size: 1.2rem;
}
"""


def _get_default_js_scaffold(preset: str = "startup") -> str:
    """Return default simulation seed JS template string."""
    return """// AI-OS Simulation Seed Data
const simEntities = {{SIM_ENTITIES_JSON}};

if (typeof window !== "undefined") {
    window.SIM_SEED = {
        title: "{{TITLE}}",
        entities: simEntities,
        initialized: true
    };
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = { simEntities };
}
"""


def write_scaffold(out_dir: Path | str, preset: str = "startup") -> Path:
    """Generate base scaffold directory structure and template files.

    Args:
        out_dir: Destination directory path.
        preset: Layout preset name (default: 'startup').

    Returns:
        Path object pointing to out_dir.
    """
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "styles").mkdir(parents=True, exist_ok=True)
    (dest / "sim").mkdir(parents=True, exist_ok=True)

    html_content = _get_default_html_scaffold(preset)
    css_content = _get_default_css_scaffold(preset)
    js_content = _get_default_js_scaffold(preset)

    (dest / "index.html").write_text(html_content, encoding="utf-8")
    (dest / "styles" / "tokens.css").write_text(css_content, encoding="utf-8")
    (dest / "sim" / "seed.js").write_text(js_content, encoding="utf-8")

    return dest


def _inject_html_brief(html_content: str, brief: DesignBrief) -> str:
    """Inject brief fields (title, value proposition, pages, flow) into index.html content."""
    title_str = brief.title or brief.name or "Untitled Startup"
    val_prop_str = brief.value_proposition or brief.value_prop or "Innovative solution"

    pages_html = "\n".join(
        f'                <li><a href="#{p.lower().replace(" ", "-")}">{html.escape(p)}</a></li>'
        for p in brief.pages
    ) if brief.pages else '                <li><a href="#home">Home</a></li>'

    flow_html = "\n".join(
        f'                <li>{html.escape(step)}</li>'
        for step in brief.core_flow
    ) if brief.core_flow else '                <li>Interactive Demo</li>'

    res = html_content
    if "{{TITLE}}" in res:
        res = res.replace("{{TITLE}}", html.escape(title_str))
    else:
        res = re.sub(r"<title>.*?</title>", f"<title>{html.escape(title_str)}</title>", res)
        res = re.sub(r'<h1 id="app-title">.*?</h1>', f'<h1 id="app-title">{html.escape(title_str)}</h1>', res)

    if "{{VALUE_PROPOSITION}}" in res:
        res = res.replace("{{VALUE_PROPOSITION}}", html.escape(val_prop_str))
    else:
        res = re.sub(r'<p id="value-prop".*?>.*?</p>', f'<p id="value-prop" class="subtitle">{html.escape(val_prop_str)}</p>', res)

    if "{{PAGES_ITEMS}}" in res:
        res = res.replace("{{PAGES_ITEMS}}", pages_html)

    if "{{CORE_FLOW_ITEMS}}" in res:
        res = res.replace("{{CORE_FLOW_ITEMS}}", flow_html)

    return res


def _inject_css_tokens(css_content: str, brief: DesignBrief) -> str:
    """Inject brand tokens (brand title, tone, primary/accent colors) into tokens.css."""
    title_str = brief.title or brief.name or "Untitled Startup"
    brand_tone = brief.brand or brief.brand_tone or "Modern and clean"

    res = css_content
    if "{{BRAND_TITLE}}" in res:
        res = res.replace("{{BRAND_TITLE}}", title_str)
    else:
        res = re.sub(r'--brand-title:\s*".*?";', f'--brand-title: "{title_str}";', res)

    if "{{BRAND_TONE}}" in res:
        res = res.replace("{{BRAND_TONE}}", brand_tone)
    else:
        res = re.sub(r'--brand-tone:\s*".*?";', f'--brand-tone: "{brand_tone}";', res)

    brand_lower = brand_tone.lower()
    if "emerald" in brand_lower or "green" in brand_lower or "zöld" in brand_lower:
        res = re.sub(r'--primary-color:\s*#[0-9a-fA-F]+;', '--primary-color: #10b981;', res)
        res = re.sub(r'--accent-color:\s*#[0-9a-fA-F]+;', '--accent-color: #6ee7b7;', res)
    elif "purple" in brand_lower or "violet" in brand_lower or "lila" in brand_lower:
        res = re.sub(r'--primary-color:\s*#[0-9a-fA-F]+;', '--primary-color: #8b5cf6;', res)
        res = re.sub(r'--accent-color:\s*#[0-9a-fA-F]+;', '--accent-color: #c084fc;', res)

    if "light" in brand_lower or "világos" in brand_lower:
        res = re.sub(r'--bg-color:\s*#[0-9a-fA-F]+;', '--bg-color: #f8fafc;', res)
        res = re.sub(r'--text-color:\s*#[0-9a-fA-F]+;', '--text-color: #0f172a;', res)

    return res


def _populate_sim_seed(js_content: str, brief: DesignBrief) -> str:
    """Populate sim/seed.js with sim_entities list from DesignBrief."""
    title_str = brief.title or brief.name or "Untitled Startup"
    entities = brief.sim_entities if brief.sim_entities else []
    entities_json = json.dumps(entities, indent=2)

    res = js_content
    if "{{SIM_ENTITIES_JSON}}" in res:
        res = res.replace("{{SIM_ENTITIES_JSON}}", entities_json)
    else:
        res = re.sub(r"const simEntities = \[.*?\];", f"const simEntities = {entities_json};", res, flags=re.DOTALL)

    if "{{TITLE}}" in res:
        res = res.replace("{{TITLE}}", title_str)

    return res


def generate_startup(out_dir: Path | str, brief: DesignBrief, preset: str = "startup") -> Path:
    """Generate a fully populated startup project scaffold from a DesignBrief.

    Args:
        out_dir: Directory path where output files will be saved.
        brief: DesignBrief object with startup metadata.
        preset: Layout preset name (default: 'startup').

    Returns:
        Path object pointing to out_dir.
    """
    dest = Path(out_dir)
    write_scaffold(dest, preset=preset)

    html_path = dest / "index.html"
    css_path = dest / "styles" / "tokens.css"
    js_path = dest / "sim" / "seed.js"

    # Inject fields into index.html
    html_content = html_path.read_text(encoding="utf-8")
    updated_html = _inject_html_brief(html_content, brief)
    html_path.write_text(updated_html, encoding="utf-8")

    # Inject brand tokens into styles/tokens.css
    css_content = css_path.read_text(encoding="utf-8")
    updated_css = _inject_css_tokens(css_content, brief)
    css_path.write_text(updated_css, encoding="utf-8")

    # Populate sim/seed.js with sim_entities
    js_content = js_path.read_text(encoding="utf-8")
    updated_js = _populate_sim_seed(js_content, brief)
    js_path.write_text(updated_js, encoding="utf-8")

    return dest