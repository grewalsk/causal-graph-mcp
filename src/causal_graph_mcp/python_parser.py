"""Python language parser — wraps the existing ast-based parser."""

from __future__ import annotations

from causal_graph_mcp.parser import ParseResult, parse_file


class PythonParser:
    """LanguageParser implementation for Python using ast + jedi."""

    @property
    def file_extensions(self) -> list[str]:
        return [".py"]

    @property
    def language_name(self) -> str:
        return "python"

    def parse(self, file_path: str, module_name: str) -> ParseResult:
        return parse_file(file_path, module_name)
