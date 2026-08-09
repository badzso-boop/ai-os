"""DesignBrief Data Model and Startup Brief Parser.

This module defines the `DesignBrief` dataclass and the `parse_startup_brief`
function, which converts raw text prompts or structured Markdown brief files
(in Hungarian or English) into a normalized, structured `DesignBrief`.

Architecture & Design Principles:
- Compiler First: Brief parsing is 100% deterministic (regex and heuristic text parsing, zero LLM tokens).
- Supports both file paths (`Path` or `str`) and inline text prompt strings.
- Performs robust section extraction across Hungarian and English headers, extracting:
  startup name, value prop, target audience, core flow steps, pages, brand/tone,
  sim entities, and non-requirements.
- Provides clean serialization (`to_dict` / `from_dict`) for IPC / storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union
import unicodedata


@dataclass
class DesignBrief:
    """Structured representation of a startup design brief."""

    name: str = ""
    value_prop: str = ""
    target_audience: str = ""
    core_flow: List[str] = field(default_factory=list)
    pages: List[str] = field(default_factory=list)
    brand_tone: str = ""
    sim_entities: List[str] = field(default_factory=list)
    non_requirements: List[str] = field(default_factory=list)
    raw_prompt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize design brief to dictionary format."""
        return {
            "name": self.name,
            "value_prop": self.value_prop,
            "target_audience": self.target_audience,
            "core_flow": list(self.core_flow),
            "pages": list(self.pages),
            "brand_tone": self.brand_tone,
            "sim_entities": list(self.sim_entities),
            "non_requirements": list(self.non_requirements),
            "raw_prompt": self.raw_prompt,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DesignBrief:
        """Deserialize design brief from dictionary format."""
        return cls(
            name=str(data.get("name", "")),
            value_prop=str(data.get("value_prop", "")),
            target_audience=str(data.get("target_audience", "")),
            core_flow=list(data.get("core_flow", [])),
            pages=list(data.get("pages", [])),
            brand_tone=str(data.get("brand_tone", "")),
            sim_entities=list(data.get("sim_entities", [])),
            non_requirements=list(data.get("non_requirements", [])),
            raw_prompt=str(data.get("raw_prompt", "")),
        )


def _normalize_header(text: str) -> str:
    """Normalize header string for section matching (lowercase, unaccented, stripped)."""
    if not text:
        return ""
    cleaned = re.sub(r"^#+\s*", "", text).strip()
    nfkd = unicodedata.normalize("NFD", cleaned)
    unaccented = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return unaccented.lower().strip()


def _parse_bullet_or_numbered_list(text: str) -> List[str]:
    """Extract list items from bullet points, numbered lists, or lines/commas."""
    if not text:
        return []
    items: List[str] = []
    lines = text.strip().splitlines()
    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
        m = re.match(r"^(?:\d+[\.\)]|[-*•])\s+(.+)$", line_str)
        if m:
            items.append(m.group(1).strip())
        elif line_str.startswith("#"):
            continue
        else:
            if "," in line_str and not line_str.startswith(("http", "https")):
                parts = [p.strip() for p in line_str.split(",") if p.strip()]
                if len(parts) > 1:
                    items.extend(parts)
                else:
                    items.append(line_str)
            else:
                items.append(line_str)
    return items


def _parse_comma_or_bullet_list(text: str) -> List[str]:
    """Parse pages or entities from comma-separated strings or bullet lists."""
    if not text:
        return []
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) == 1 and "," in lines[0]:
        return [p.strip() for p in lines[0].split(",") if p.strip()]
    items: List[str] = []
    for line in lines:
        m = re.match(r"^(?:\d+[\.\)]|[-*•])\s+(.+)$", line)
        val = m.group(1).strip() if m else line
        if "," in val:
            items.extend([p.strip() for p in val.split(",") if p.strip()])
        else:
            items.append(val)
    return items


SECTION_PATTERNS = {
    "name_val": [
        "nev", "name", "nev + egymondatos value prop", "name & value proposition",
        "value prop", "value proposition", "summary", "osszefoglalo", "overview"
    ],
    "audience": [
        "celkozonseg", "target audience", "celcsoport", "audience", "users", "felhasznalok"
    ],
    "flow": [
        "a demo fo flow-ja", "a demo fo flow-ja (ezt szimulaljuk mukodokent)",
        "demo fo flow-ja", "fo flow", "core flow", "main flow", "flow", "user flow",
        "lepések", "lepesek", "steps", "demo flow"
    ],
    "pages": [
        "oldalak", "pages", "pages / ia", "ia", "screens", "views", "informacios architektura"
    ],
    "brand": [
        "marka / hangnem", "marka", "hangnem", "brand", "tone", "brand / tone",
        "brand & tone", "design", "stilus", "style", "márka"
    ],
    "entities": [
        "sim model", "sim_model", "sim entitasok", "entities", "mock entities",
        "szimulalt entitasok", "data model", "adatmodell", "mock model"
    ],
    "non_reqs": [
        "amit nem kell", "non-requirements", "exclusions", "out of scope",
        "out-of-scope", "nem kell", "scoping", "kizarasok"
    ]
}

ALL_KEYWORDS_REGEX = (
    r"Audience|Target Audience|Célközönség|Célcsoport|Pages|Oldalak|Flow|Core Flow|Main Flow|Fő flow|"
    r"Brand|Márka|Hangnem|Tone|Sim Model|Entities|Exclusions|Non-requirements|Amit NEM kell|Value Prop|Value Proposition"
)


def _match_section_category(header_norm: str) -> Optional[str]:
    """Match normalized header string to section category."""
    for category, keywords in SECTION_PATTERNS.items():
        for kw in keywords:
            if header_norm == kw or header_norm.startswith(kw):
                return category
    return None


def _extract_inline_field(content: str, keywords_regex: str) -> Optional[str]:
    """Extract an inline field value up to next keyword delimiter, newline, or string end."""
    pattern = rf"(?:{keywords_regex}):\s*(.*?)(?=\s*(?:\.\s+)?(?:{ALL_KEYWORDS_REGEX}):|\n|$)"
    m = re.search(pattern, content, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        return val.rstrip(".") if val.endswith(".") and not val.endswith("...") else val
    return None


def parse_startup_brief(source: Union[str, Path]) -> DesignBrief:
    """Parse a startup design brief from a file path or raw text string.

    Args:
        source: File path (Path or str) or raw string containing brief content or prompt.

    Returns:
        Structured DesignBrief object.
    """
    content: str = ""

    is_file = False
    if isinstance(source, Path):
        try:
            if source.is_file():
                is_file = True
                content = source.read_text(encoding="utf-8")
        except (OSError, ValueError):
            is_file = False
    elif isinstance(source, str):
        try:
            p = Path(source)
            if len(source) < 1024 and p.is_file():
                is_file = True
                content = p.read_text(encoding="utf-8")
        except (OSError, ValueError):
            is_file = False

    if not is_file:
        content = str(source)

    raw_prompt = content.strip()
    if not raw_prompt:
        return DesignBrief(
            name="Untitled Startup",
            pages=["Landing", "Demo"],
            core_flow=["Interactive Product Demo"],
            raw_prompt=""
        )

    lines = content.splitlines()
    sections: List[tuple[str, List[str]]] = []
    current_header = ""
    current_lines: List[str] = []

    doc_title = ""

    for line in lines:
        line_str = line.rstrip()
        if line_str.startswith("#"):
            if not doc_title and line_str.startswith("# "):
                doc_title = line_str.lstrip("# ").strip()
            if current_lines or current_header:
                sections.append((current_header, current_lines))
            current_header = line_str
            current_lines = []
        else:
            current_lines.append(line_str)

    if current_lines or current_header:
        sections.append((current_header, current_lines))

    parsed_fields: Dict[str, Any] = {
        "name": "",
        "value_prop": "",
        "target_audience": "",
        "core_flow": [],
        "pages": [],
        "brand_tone": "",
        "sim_entities": [],
        "non_requirements": [],
    }

    for header, sec_lines in sections:
        header_norm = _normalize_header(header)
        sec_text = "\n".join(sec_lines).strip()
        if not sec_text and not header_norm:
            continue

        cat = _match_section_category(header_norm)
        if cat == "name_val":
            first_line = sec_lines[0].strip() if sec_lines else ""
            if "—" in first_line:
                parts = first_line.split("—", 1)
                parsed_fields["name"] = parts[0].strip()
                parsed_fields["value_prop"] = parts[1].strip()
            elif " - " in first_line:
                parts = first_line.split(" - ", 1)
                parsed_fields["name"] = parts[0].strip()
                parsed_fields["value_prop"] = parts[1].strip()
            elif ":" in first_line and not first_line.startswith("http"):
                parts = first_line.split(":", 1)
                parsed_fields["name"] = parts[0].strip()
                parsed_fields["value_prop"] = parts[1].strip()
            else:
                parsed_fields["name"] = first_line
                if len(sec_lines) > 1:
                    parsed_fields["value_prop"] = "\n".join(sec_lines[1:]).strip()
        elif cat == "audience":
            parsed_fields["target_audience"] = sec_text
        elif cat == "flow":
            parsed_fields["core_flow"] = _parse_bullet_or_numbered_list(sec_text)
        elif cat == "pages":
            parsed_fields["pages"] = _parse_comma_or_bullet_list(sec_text)
        elif cat == "brand":
            parsed_fields["brand_tone"] = sec_text
        elif cat == "entities":
            parsed_fields["sim_entities"] = _parse_comma_or_bullet_list(sec_text)
        elif cat == "non_reqs":
            parsed_fields["non_requirements"] = _parse_bullet_or_numbered_list(sec_text)

    if not parsed_fields["name"]:
        m_name = re.search(r"(?:Név|Name):\s*([^\n—\-]+)", content, re.IGNORECASE)
        if m_name:
            parsed_fields["name"] = m_name.group(1).strip()

        if not parsed_fields["name"]:
            m_sep = re.search(r"^([A-Z][A-Za-z0-9_\s]{1,30})\s*[—\-]\s*(.+)$", content, re.MULTILINE)
            if m_sep:
                parsed_fields["name"] = m_sep.group(1).strip()
                if not parsed_fields["value_prop"]:
                    parsed_fields["value_prop"] = m_sep.group(2).strip()

        if not parsed_fields["name"] and doc_title:
            cleaned_title = re.sub(r"^(?:Startup Brief|Brief)[:\s]*", "", doc_title, flags=re.IGNORECASE).strip()
            if cleaned_title:
                parsed_fields["name"] = cleaned_title

    if not parsed_fields["value_prop"]:
        val_inline = _extract_inline_field(content, "Value Prop|Value Proposition|Egymondatos value prop")
        if val_inline:
            parsed_fields["value_prop"] = val_inline

    if not parsed_fields["target_audience"]:
        aud_inline = _extract_inline_field(content, "Célközönség|Target Audience|Audience|Célcsoport")
        if aud_inline:
            parsed_fields["target_audience"] = aud_inline

    if not parsed_fields["pages"]:
        pg_inline = _extract_inline_field(content, "Oldalak|Pages")
        if pg_inline:
            parsed_fields["pages"] = [p.strip() for p in pg_inline.split(",") if p.strip()]

    if not parsed_fields["core_flow"]:
        fl_inline = _extract_inline_field(content, "Core Flow|Flow|Fő flow|Main Flow")
        if fl_inline:
            parsed_fields["core_flow"] = _parse_bullet_or_numbered_list(fl_inline)

    if not parsed_fields["brand_tone"]:
        br_inline = _extract_inline_field(content, "Márka|Brand|Hangnem|Tone")
        if br_inline:
            parsed_fields["brand_tone"] = br_inline

    if not parsed_fields["name"]:
        parsed_fields["name"] = "Untitled Startup"

    if not parsed_fields["pages"]:
        parsed_fields["pages"] = ["Landing", "Demo"]

    if not parsed_fields["core_flow"]:
        parsed_fields["core_flow"] = ["Interactive Product Demo"]

    return DesignBrief(
        name=parsed_fields["name"],
        value_prop=parsed_fields["value_prop"],
        target_audience=parsed_fields["target_audience"],
        core_flow=parsed_fields["core_flow"],
        pages=parsed_fields["pages"],
        brand_tone=parsed_fields["brand_tone"],
        sim_entities=parsed_fields["sim_entities"],
        non_requirements=parsed_fields["non_requirements"],
        raw_prompt=raw_prompt,
    )