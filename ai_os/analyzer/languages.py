"""Language detection and per-language Tree-sitter grammar/query wiring."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tree_sitter_css as _css
import tree_sitter_html as _html
import tree_sitter_java as _java
import tree_sitter_javascript as _js
import tree_sitter_python as _py
import tree_sitter_sql as _sql
import tree_sitter_typescript as _ts

QUERIES_DIR = Path(__file__).parent / "queries"


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    extensions: tuple[str, ...]
    language_factory: Callable[[], object]
    query_file: str | None
    comment_template: str

    def load_query_source(self) -> str | None:
        if self.query_file is None:
            return None
        return (QUERIES_DIR / self.query_file).read_text(encoding="utf-8")


LANGUAGES: dict[str, LanguageProfile] = {
    "python": LanguageProfile("python", (".py",), _py.language, "python.scm", "# {}"),
    "java": LanguageProfile("java", (".java",), _java.language, "java.scm", "// {}"),
    "javascript": LanguageProfile(
        "javascript", (".js", ".jsx", ".mjs", ".cjs"), _js.language, "javascript.scm", "// {}"
    ),
    "typescript": LanguageProfile(
        "typescript", (".ts", ".tsx"), _ts.language_typescript, "typescript.scm", "// {}"
    ),
    # HTML/CSS have no function/class symbols to extract via query — no query_file.
    "html": LanguageProfile("html", (".html", ".htm"), _html.language, None, "<!-- {} -->"),
    "css": LanguageProfile("css", (".css",), _css.language, None, "/* {} */"),
    "sql": LanguageProfile("sql", (".sql",), _sql.language, "sql.scm", "-- {}"),
}

_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ext: profile.name for profile in LANGUAGES.values() for ext in profile.extensions
}


def detect_language(path: Path) -> str | None:
    """Map a file path to a registered language name by extension, or None if unsupported."""
    return _EXTENSION_TO_LANGUAGE.get(path.suffix.lower())
