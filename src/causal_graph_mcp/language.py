"""Language detection and parser registry for multi-language support."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from causal_graph_mcp.parser import ParseResult


class LanguageParser(Protocol):
    """Protocol for language-specific parsers."""

    def parse(self, file_path: str, module_name: str) -> ParseResult:
        """Parse a source file and extract nodes and edges."""
        ...

    @property
    def file_extensions(self) -> list[str]:
        """File extensions this parser handles (e.g. ['.py'])."""
        ...

    @property
    def language_name(self) -> str:
        """Language identifier (e.g. 'python', 'javascript')."""
        ...


# Extension → language name mapping
_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

# Registered parsers by language name
_PARSERS: dict[str, LanguageParser] = {}


def register_parser(parser: LanguageParser) -> None:
    """Register a language parser."""
    _PARSERS[parser.language_name] = parser


def get_parser(file_path: str) -> LanguageParser | None:
    """Get the appropriate parser for a file based on its extension."""
    ext = Path(file_path).suffix.lower()
    lang = _EXTENSION_MAP.get(ext)
    if lang is None:
        return None
    return _PARSERS.get(lang)


def detect_language(file_path: str) -> str | None:
    """Detect the language of a file from its extension."""
    ext = Path(file_path).suffix.lower()
    return _EXTENSION_MAP.get(ext)


def supported_extensions() -> set[str]:
    """Return all file extensions that have registered parsers."""
    return {ext for ext, lang in _EXTENSION_MAP.items() if lang in _PARSERS}


def get_source_extensions() -> set[str]:
    """Return all known source file extensions (even without parsers)."""
    return set(_EXTENSION_MAP.keys())
