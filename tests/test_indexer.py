"""Integration tests for the indexer pipeline."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from causal_graph_mcp.indexer import (
    _derive_module_name,
    _discover_files,
    index_files,
    index_project,
)
from causal_graph_mcp.storage import Storage


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a small Python project for testing."""
    # auth.py
    (tmp_path / "auth.py").write_text(
        '''
def create_token(user_id: int) -> str:
    """Creates a signed token for the given user."""
    return f"token_{user_id}"

def verify_token(token: str) -> bool:
    return token.startswith("token_")
''',
        encoding="utf-8",
    )

    # views.py — calls auth functions
    (tmp_path / "views.py").write_text(
        '''
from auth import create_token, verify_token

def login_handler(user_id: int):
    token = create_token(user_id)
    return {"token": token}

def check_handler(token: str):
    valid = verify_token(token)
    return {"valid": valid}
''',
        encoding="utf-8",
    )

    # utils/__init__.py
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "__init__.py").write_text(
        'VERSION = "1.0.0"\n',
        encoding="utf-8",
    )

    # utils/helpers.py
    (tmp_path / "utils" / "helpers.py").write_text(
        '''
def format_output(data: dict) -> str:
    return str(data)
''',
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path)
    yield s
    s.close()


class TestFileDiscovery:
    def test_discover_files(self, project: Path) -> None:
        """Finds all .py files and skips excluded directories."""
        # Create excluded dirs
        (project / "__pycache__").mkdir()
        (project / "__pycache__" / "cached.py").write_text("# cached")
        (project / ".venv").mkdir()
        (project / ".venv" / "lib.py").write_text("# venv lib")
        (project / ".git").mkdir()
        (project / ".git" / "hook.py").write_text("# hook")

        files = _discover_files(str(project))
        basenames = {os.path.basename(f) for f in files}

        assert "auth.py" in basenames
        assert "views.py" in basenames
        assert "helpers.py" in basenames
        assert "__init__.py" in basenames
        assert "cached.py" not in basenames
        assert "lib.py" not in basenames
        assert "hook.py" not in basenames

    def test_gitignore_respected(self, project: Path) -> None:
        """Files matching .gitignore patterns are excluded."""
        # Create generated directory with files
        (project / "generated").mkdir()
        (project / "generated" / "auto.py").write_text("# auto-generated")

        # Create .gitignore
        (project / ".gitignore").write_text("generated\n")

        files = _discover_files(str(project))
        basenames = {os.path.basename(f) for f in files}

        assert "auto.py" not in basenames
        assert "auth.py" in basenames


class TestChangeDetection:
    def test_skips_unchanged(self, project: Path) -> None:
        """Re-indexing skips files that haven't changed."""
        storage = Storage(project)
        try:
            # First index
            result1 = index_project(str(project), storage)
            assert result1.files_parsed > 0
            assert result1.files_skipped == 0

            # Second index — nothing changed
            result2 = index_project(str(project), storage)
            assert result2.files_parsed == 0
            assert result2.files_skipped == result1.files_parsed
        finally:
            storage.close()

    def test_detects_changes(self, project: Path) -> None:
        """Re-indexing detects and re-parses changed files."""
        storage = Storage(project)
        try:
            # First index
            result1 = index_project(str(project), storage)
            total = result1.files_parsed

            # Modify one file
            (project / "auth.py").write_text(
                '''
def create_token(user_id: int) -> str:
    """Creates a signed token — MODIFIED."""
    return f"token_v2_{user_id}"
''',
                encoding="utf-8",
            )

            # Re-index
            result2 = index_project(str(project), storage)
            assert result2.files_parsed == 1
            assert result2.files_skipped == total - 1
        finally:
            storage.close()


class TestFullPipeline:
    def test_indexes_project(self, project: Path) -> None:
        """Full pipeline indexes nodes and edges correctly."""
        storage = Storage(project)
        try:
            result = index_project(str(project), storage)

            assert result.files_parsed == 4  # auth, views, __init__, helpers
            assert result.nodes_indexed > 0
            assert result.edges_indexed > 0
            assert result.duration_ms >= 0

            # Verify nodes are in storage
            stats = storage.get_stats()
            assert stats["total_nodes"] == result.nodes_indexed
            assert stats["total_edges"] == result.edges_indexed

            # Verify specific nodes exist
            assert storage.get_node("auth.create_token") is not None
            assert storage.get_node("views.login_handler") is not None
            assert storage.get_node("utils.helpers.format_output") is not None
        finally:
            storage.close()

    def test_index_files_incremental(self, project: Path) -> None:
        """index_files() indexes only specified files."""
        storage = Storage(project)
        try:
            auth_path = str(project / "auth.py")
            result = index_files([auth_path], str(project), storage)

            assert result.files_parsed == 1
            assert storage.get_node("auth.create_token") is not None
            # views.py was not indexed
            assert storage.get_node("views.login_handler") is None
        finally:
            storage.close()

    def test_empty_project(self, tmp_path: Path) -> None:
        """Indexing an empty directory produces zero counts."""
        storage = Storage(tmp_path)
        try:
            result = index_project(str(tmp_path), storage)
            assert result.files_parsed == 0
            assert result.nodes_indexed == 0
            assert result.edges_indexed == 0
        finally:
            storage.close()


class TestModuleNameDerivation:
    def test_simple_file(self) -> None:
        assert _derive_module_name("/project/main.py", "/project") == "main"

    def test_nested_file(self) -> None:
        assert _derive_module_name("/project/src/auth/utils.py", "/project") == "src.auth.utils"

    def test_init_file(self) -> None:
        assert _derive_module_name("/project/src/auth/__init__.py", "/project") == "src.auth"

    def test_top_level_init(self) -> None:
        assert _derive_module_name("/project/pkg/__init__.py", "/project") == "pkg"
