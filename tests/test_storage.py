"""Unit tests for the Storage layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from causal_graph_mcp.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path)
    yield s
    s.close()


def _make_node(
    node_id: str,
    kind: str = "function",
    module: str = "mod",
    file: str = "mod.py",
    **kwargs,
) -> dict:
    defaults = {
        "id": node_id,
        "kind": kind,
        "module": module,
        "file": file,
        "line_start": 1,
        "line_end": 10,
        "signature": f"def {node_id}()",
        "docstring": f"Docstring for {node_id}",
        "is_public": 1,
        "is_test": 0,
        "body_hash": f"hash_{node_id}",
    }
    defaults.update(kwargs)
    return defaults


def _make_edge(src: str, dst: str, kind: str = "calls", **kwargs) -> dict:
    defaults = {
        "src": src,
        "dst": dst,
        "kind": kind,
        "confidence": 1.0,
        "weight": 1,
        "scope": None,
        "detail": None,
    }
    defaults.update(kwargs)
    return defaults


class TestSchemaCreation:
    def test_tables_exist(self, storage: Storage) -> None:
        conn = sqlite3.connect(str(storage._db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "nodes" in tables
        assert "edges" in tables
        assert "file_hashes" in tables

    def test_fts_table_exists(self, storage: Storage) -> None:
        conn = sqlite3.connect(str(storage._db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "nodes_fts" in tables

    def test_indexes_exist(self, storage: Storage) -> None:
        conn = sqlite3.connect(str(storage._db_path))
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "idx_edges_src" in indexes
        assert "idx_edges_dst" in indexes
        assert "idx_edges_kind" in indexes


class TestNodeCRUD:
    def test_upsert_and_get(self, storage: Storage) -> None:
        nodes = [
            _make_node("mod.func_a"),
            _make_node("mod.func_b"),
            _make_node("mod.MyClass", kind="class"),
        ]
        storage.upsert_nodes(nodes)

        result = storage.get_node("mod.func_a")
        assert result is not None
        assert result["id"] == "mod.func_a"
        assert result["kind"] == "function"

        result = storage.get_node("mod.MyClass")
        assert result is not None
        assert result["kind"] == "class"

    def test_get_nonexistent(self, storage: Storage) -> None:
        assert storage.get_node("does.not.exist") is None

    def test_upsert_replaces(self, storage: Storage) -> None:
        storage.upsert_nodes([_make_node("mod.func_a", body_hash="v1")])
        storage.upsert_nodes([_make_node("mod.func_a", body_hash="v2")])

        result = storage.get_node("mod.func_a")
        assert result["body_hash"] == "v2"

        # Should still be one node, not two
        all_nodes = storage.get_all_nodes()
        assert len(all_nodes) == 1


class TestEdgeCRUD:
    def test_insert_and_query_edges(self, storage: Storage) -> None:
        storage.upsert_nodes([
            _make_node("mod.func_a"),
            _make_node("mod.func_b"),
            _make_node("mod.func_c"),
        ])
        storage.insert_edges([
            _make_edge("mod.func_a", "mod.func_b", "calls"),
            _make_edge("mod.func_a", "mod.func_c", "mutates"),
            _make_edge("mod.func_c", "mod.func_a", "asserts_on"),
        ])

        out_edges = storage.get_edges("mod.func_a", direction="out")
        assert len(out_edges) == 2
        assert {e["kind"] for e in out_edges} == {"calls", "mutates"}

        in_edges = storage.get_edges("mod.func_a", direction="in")
        assert len(in_edges) == 1
        assert in_edges[0]["kind"] == "asserts_on"

        both_edges = storage.get_edges("mod.func_a", direction="both")
        assert len(both_edges) == 3

    def test_all_edge_kinds(self, storage: Storage) -> None:
        storage.upsert_nodes([
            _make_node("a"),
            _make_node("b"),
        ])
        edge_kinds = [
            "calls", "imports", "mutates", "asserts_on",
            "side_effect", "inherits", "overrides",
        ]
        edges = [_make_edge("a", "b", kind=k) for k in edge_kinds]
        storage.insert_edges(edges)

        all_edges = storage.get_edges("a", direction="out")
        assert len(all_edges) == 7
        assert {e["kind"] for e in all_edges} == set(edge_kinds)


class TestFileScopedUpdate:
    def test_update_replaces_file_data(self, storage: Storage) -> None:
        # Insert initial data for auth.py
        old_nodes = [
            _make_node("auth.old_func", file="auth.py", module="auth"),
        ]
        old_edges = [
            _make_edge("auth.old_func", "auth.old_func", "calls"),
        ]
        storage.upsert_nodes(old_nodes)
        storage.insert_edges(old_edges)
        storage.set_file_hash("auth.py", "old_hash")

        # Update with new data
        new_nodes = [
            _make_node("auth.new_func", file="auth.py", module="auth"),
            _make_node("auth.another_func", file="auth.py", module="auth"),
        ]
        new_edges = [
            _make_edge("auth.new_func", "auth.another_func", "calls"),
        ]
        storage.update_file_scope("auth.py", new_nodes, new_edges, "new_hash")

        # Old data gone
        assert storage.get_node("auth.old_func") is None
        assert len(storage.get_edges("auth.old_func")) == 0

        # New data present
        assert storage.get_node("auth.new_func") is not None
        assert storage.get_node("auth.another_func") is not None
        assert len(storage.get_edges("auth.new_func", direction="out")) == 1

        # File hash updated
        assert storage.get_file_hash("auth.py") == "new_hash"

    def test_update_atomicity(self, storage: Storage) -> None:
        # Insert initial data
        storage.upsert_nodes([_make_node("auth.func", file="auth.py", module="auth")])
        storage.set_file_hash("auth.py", "original_hash")

        bad_nodes = [_make_node("auth.new", file="auth.py", module="auth")]
        # Use an edge referencing a column that doesn't exist to trigger a real error
        # after the delete has happened but during the edge insert.
        # Instead, we patch insert_edges to raise mid-transaction.
        original_insert_edges = storage.insert_edges

        def failing_insert(edges):
            raise sqlite3.OperationalError("simulated failure")

        # The update_file_scope does its own edge inserts inline, so we need to
        # make the connection raise during the edge INSERT within the transaction.
        # We'll wrap the connection with a proxy.
        original_conn = storage._conn

        class FailingConnection:
            """Proxy that fails on edge INSERT within update_file_scope."""

            def __init__(self, real_conn):
                self._real = real_conn
                self._fail_on_edge_insert = False
                self._insert_count = 0

            def execute(self, sql, params=None):
                if "INSERT INTO edges" in sql:
                    raise sqlite3.OperationalError("simulated failure")
                if params is None:
                    return self._real.execute(sql)
                return self._real.execute(sql, params)

            def commit(self):
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            def __getattr__(self, name):
                return getattr(self._real, name)

        storage._conn = FailingConnection(original_conn)

        bad_edges = [_make_edge("auth.new", "auth.new", "calls")]
        with pytest.raises(sqlite3.OperationalError, match="simulated failure"):
            storage.update_file_scope("auth.py", bad_nodes, bad_edges, "new_hash")

        # Restore original connection
        storage._conn = original_conn

        # Original data should still be intact due to rollback
        assert storage.get_node("auth.func") is not None
        assert storage.get_file_hash("auth.py") == "original_hash"


class TestFTS5Search:
    def test_basic_search(self, storage: Storage) -> None:
        storage.upsert_nodes([
            _make_node("auth.create_token", docstring="Creates a signed JWT for the given user"),
            _make_node("auth.verify_token", docstring="Verifies a JWT token and returns claims"),
            _make_node("views.login", docstring="Handles user login via form submission"),
        ])

        results = storage.search("JWT token")
        assert len(results) >= 1
        result_ids = [r["id"] for r in results]
        assert "auth.create_token" in result_ids or "auth.verify_token" in result_ids

    def test_search_with_kind_filter(self, storage: Storage) -> None:
        storage.upsert_nodes([
            _make_node("auth.create_token", kind="function"),
            _make_node("auth.Token", kind="class", docstring="Token class for JWT"),
        ])

        results = storage.search("token", kinds=["class"])
        assert all(r["kind"] == "class" for r in results)

    def test_search_limit(self, storage: Storage) -> None:
        nodes = [
            _make_node(f"mod.func_{i}", docstring=f"Function {i} does token processing")
            for i in range(20)
        ]
        storage.upsert_nodes(nodes)

        results = storage.search("token", limit=5)
        assert len(results) <= 5


class TestStats:
    def test_get_stats(self, storage: Storage) -> None:
        storage.upsert_nodes([
            _make_node("a"),
            _make_node("b"),
            _make_node("c"),
        ])
        storage.insert_edges([
            _make_edge("a", "b", "calls"),
            _make_edge("a", "c", "calls"),
            _make_edge("b", "c", "mutates"),
        ])

        stats = storage.get_stats()
        assert stats["total_nodes"] == 3
        assert stats["total_edges"] == 3
        assert stats["edge_breakdown"]["calls"] == 2
        assert stats["edge_breakdown"]["mutates"] == 1
