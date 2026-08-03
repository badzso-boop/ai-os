"""Signature-only "skeleton stub" generation (doc 04 / doc 08): strips a symbol's body
and keeps only what a caller needs to know its interface — the compressed representation
handed to AI agents instead of full source.
"""
from __future__ import annotations

from ai_os.analyzer.languages import LanguageProfile
from ai_os.analyzer.tree_sitter_engine import Symbol

_BODY_STUBS = {
    "python": "\n    ...\n",
    "java": " {\n    // ...\n}\n",
    "javascript": " {\n    // ...\n}\n",
    "typescript": " {\n    // ...\n}\n",
}

_BANNER = "SKELETON STUB FOR: {fqn} — DO NOT EDIT. INTERFACE REFERENCE ONLY."


def extract_skeleton(symbol: Symbol, source: bytes, profile: LanguageProfile) -> str:
    """Returns a compressed, signature-only representation of `symbol`.

    HTML/CSS have no query-extractable symbols so this is never called for them; SQL
    symbols (create_table/create_view/create_function) have no body to strip, so the
    full statement text passes through unchanged.
    """
    banner = profile.comment_template.format(_BANNER.format(fqn=symbol.fqn))

    if symbol.body_start_byte is None:
        full_text = source[symbol.start_byte : symbol.end_byte].decode("utf-8", errors="replace")
        return f"{banner}\n{full_text}"

    header = source[symbol.start_byte : symbol.body_start_byte].decode(
        "utf-8", errors="replace"
    ).rstrip()
    body_stub = _BODY_STUBS.get(profile.name, " { ... }\n")
    return f"{banner}\n{header}{body_stub}"
