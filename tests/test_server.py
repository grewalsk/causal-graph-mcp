"""Tests for MCP server tool handler logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from causal_graph_mcp import server as server_module
from causal_graph_mcp.indexer import index_project
from causal_graph_mcp.server import _truncate, create_server
from causal_graph_mcp.storage import Storage


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a multi-file project for tool testing."""
    (tmp_path / "auth.py").write_text(
        '''
def create_token(user_id: int) -> str:
    """Creates a signed token for the given user."""
    return f"token_{user_id}"

def verify_token(token: str) -> bool:
    """Verifies a token is valid."""
    return token.startswith("token_")

class Session:
    def save(self):
        self.token = create_token(1)
''',
        encoding="utf-8",
    )

    (tmp_path / "views.py").write_text(
        '''
from auth import create_token

def login_handler(user_id: int):
    """Handle login."""
    token = create_token(user_id)
    return {"token": token}
''',
        encoding="utf-8",
    )

    (tmp_path / "test_auth.py").write_text(
        '''
from auth import create_token

def test_create_token():
    result = create_token(1)
    assert result is not None
''',
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def indexed_storage(project: Path) -> tuple[Storage, str]:
    """Index the project and return storage + root."""
    storage = Storage(project)
    index_project(str(project), storage)
    yield storage, str(project)
    storage.close()


@pytest.fixture(autouse=True)
def _patch_storage(indexed_storage, monkeypatch):
    """Patch server globals to use our test storage."""
    storage, root = indexed_storage
    monkeypatch.setattr(server_module, "_server_storage", storage)
    monkeypatch.setattr(server_module, "_server_project_root", root)


class TestToolHandlers:
    def test_index_project_tool(self, indexed_storage) -> None:
        storage, root = indexed_storage
        srv = create_server()
        # Call the tool via the server's tool list
        # We test the handler logic directly by calling _get_storage path
        from causal_graph_mcp.indexer import index_project as idx
        result = idx(root, storage)
        # Second index should skip all files
        assert result.files_skipped > 0 or result.files_parsed == 0

    def test_get_call_graph_tool(self, indexed_storage) -> None:
        storage, root = indexed_storage
        from causal_graph_mcp.graph import get_subgraph
        result = get_subgraph(storage, "auth.create_token", "callers", 3, 0.0)
        node_ids = {n["id"] for n in result.nodes}
        assert "auth.create_token" in node_ids

    def test_impact_analysis_tool(self, indexed_storage) -> None:
        storage, root = indexed_storage
        from causal_graph_mcp.risk import compute_impact
        result = compute_impact(storage, "auth.create_token", 4)
        assert result.changed_symbol == "auth.create_token"
        assert isinstance(result.summary, dict)
        assert "high_risk" in result.summary

    def test_semantic_search_tool(self, indexed_storage) -> None:
        storage, root = indexed_storage
        results = storage.search("token")
        assert len(results) > 0
        # Should find auth.create_token or auth.verify_token
        ids = {r["id"] for r in results}
        assert any("token" in id.lower() for id in ids)

    def test_get_symbol_tool(self, indexed_storage) -> None:
        storage, root = indexed_storage
        node = storage.get_node("auth.create_token")
        assert node is not None
        assert node["kind"] == "function"
        assert "create_token" in (node.get("signature") or "")
        # Verify source can be read
        file_path = node["file"]
        lines = Path(file_path).read_text().splitlines()
        start = node["line_start"] - 1
        end = node["line_end"]
        source = "\n".join(lines[start:end])
        assert "create_token" in source

    def test_project_map_tool(self, indexed_storage) -> None:
        storage, root = indexed_storage
        all_nodes = storage.get_all_nodes()
        assert len(all_nodes) > 0
        stats = storage.get_stats()
        assert stats["total_nodes"] > 0
        assert stats["total_edges"] > 0

    def test_find_mutations_tool(self, indexed_storage) -> None:
        storage, root = indexed_storage
        # Session.save does self.token = create_token(1), so it mutates Session.token
        in_edges = storage.get_edges("auth.Session.token", direction="in")
        mutators = [e for e in in_edges if e["kind"] == "mutates"]
        assert len(mutators) > 0
        assert any(e["src"] == "auth.Session.save" for e in mutators)


class TestTruncation:
    def test_small_response_unchanged(self) -> None:
        data = {"key": "value"}
        result = _truncate(data)
        assert result == {"key": "value"}
        assert "truncated" not in result

    def test_large_response_truncated(self) -> None:
        data = {"items": [{"data": "x" * 1000} for _ in range(100)]}
        result = _truncate(data)
        assert result.get("truncated") is True
        assert len(result["items"]) < 100
