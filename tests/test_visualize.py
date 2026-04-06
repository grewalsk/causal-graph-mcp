"""Tests for graph visualization (tree and mermaid formats)."""

from __future__ import annotations

from pathlib import Path

import pytest

from causal_graph_mcp.graph import get_subgraph
from causal_graph_mcp.indexer import index_project
from causal_graph_mcp.language import register_parser
from causal_graph_mcp.python_parser import PythonParser
from causal_graph_mcp.risk import compute_impact
from causal_graph_mcp.storage import Storage
from causal_graph_mcp.visualize import (
    render_impact_tree, render_mermaid, render_mermaid_impact, render_tree,
)


@pytest.fixture(autouse=True)
def _register(tmp_path):
    register_parser(PythonParser())


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "auth.py").write_text('''
import hashlib

def create_token(user_id: int) -> str:
    """Creates a signed token."""
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:16]

def verify_token(token: str) -> bool:
    return len(token) == 16

class Session:
    def save(self, user_id: int):
        self.token = create_token(user_id)
''')
    (tmp_path / "views.py").write_text('''
from auth import create_token

def login_handler(user_id: int):
    token = create_token(user_id)
    return {"token": token}

def dashboard():
    return {"ok": True}
''')
    (tmp_path / "test_auth.py").write_text('''
from auth import create_token, verify_token

def test_create_token():
    result = create_token(1)
    assert result is not None

def test_verify():
    assert verify_token("a" * 16) is True
''')
    return tmp_path


@pytest.fixture
def indexed(project: Path):
    storage = Storage(project)
    index_project(str(project), storage)
    yield storage, str(project)
    storage.close()


class TestTreeFormat:
    def test_call_graph_tree(self, indexed) -> None:
        storage, root = indexed
        subgraph = get_subgraph(storage, "auth.create_token", "both", 3, 0.0)
        output = render_tree(subgraph)

        print("\n=== TREE: call graph ===")
        print(output)
        print("========================\n")

        assert "auth.create_token (root)" in output
        assert "←" in output or "→" in output
        # Should show edge metadata
        assert "calls" in output or "asserts_on" in output

    def test_callers_only_tree(self, indexed) -> None:
        storage, root = indexed
        subgraph = get_subgraph(storage, "auth.create_token", "callers", 3, 0.0)
        output = render_tree(subgraph)

        print("\n=== TREE: callers only ===")
        print(output)
        print("==========================\n")

        assert "auth.create_token (root)" in output
        # Should have inbound arrows
        assert "←" in output

    def test_empty_graph_tree(self, indexed) -> None:
        storage, root = indexed
        subgraph = get_subgraph(storage, "views.dashboard", "callers", 3, 0.0)
        output = render_tree(subgraph)
        assert "dashboard" in output

    def test_impact_tree(self, indexed) -> None:
        storage, root = indexed
        impact = compute_impact(storage, "auth.create_token", 4)
        output = render_impact_tree(impact)

        print("\n=== TREE: impact analysis ===")
        print(output)
        print("==============================\n")

        assert "auth.create_token (changing)" in output
        assert "RISK" in output
        assert "Summary:" in output


class TestMermaidFormat:
    def test_call_graph_mermaid(self, indexed) -> None:
        storage, root = indexed
        subgraph = get_subgraph(storage, "auth.create_token", "both", 3, 0.0)
        output = render_mermaid(subgraph)

        print("\n=== MERMAID: call graph ===")
        print(output)
        print("============================\n")

        assert "```mermaid" in output
        assert "graph LR" in output
        assert "auth.create_token" in output
        assert "```" in output.split("```mermaid")[1]

    def test_impact_mermaid(self, indexed) -> None:
        storage, root = indexed
        impact = compute_impact(storage, "auth.create_token", 4)
        output = render_mermaid_impact(impact)

        print("\n=== MERMAID: impact analysis ===")
        print(output)
        print("=================================\n")

        assert "```mermaid" in output
        assert "auth.create_token" in output
        assert "classDef high" in output

    def test_empty_graph_mermaid(self, indexed) -> None:
        storage, root = indexed
        subgraph = get_subgraph(storage, "nonexistent.func", "both", 3, 0.0)
        output = render_mermaid(subgraph)
        assert "```mermaid" in output


class TestComparison:
    """Print both formats side by side for visual comparison."""

    def test_side_by_side(self, indexed) -> None:
        storage, root = indexed
        subgraph = get_subgraph(storage, "auth.create_token", "both", 3, 0.0)
        impact = compute_impact(storage, "auth.create_token", 4)

        tree_graph = render_tree(subgraph)
        mermaid_graph = render_mermaid(subgraph)
        tree_impact = render_impact_tree(impact)
        mermaid_impact = render_mermaid_impact(impact)

        print("\n" + "=" * 60)
        print("CALL GRAPH — TREE FORMAT")
        print("=" * 60)
        print(tree_graph)

        print("\n" + "=" * 60)
        print("CALL GRAPH — MERMAID FORMAT")
        print("=" * 60)
        print(mermaid_graph)

        print("\n" + "=" * 60)
        print("IMPACT ANALYSIS — TREE FORMAT")
        print("=" * 60)
        print(tree_impact)

        print("\n" + "=" * 60)
        print("IMPACT ANALYSIS — MERMAID FORMAT")
        print("=" * 60)
        print(mermaid_impact)
        print("=" * 60)
