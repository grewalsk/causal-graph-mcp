"""SQLite storage layer for the causal dependency graph."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any


def _fts_safe_query(query: str) -> str:
    """Convert a user query into a safe FTS5 MATCH expression.

    Splits on non-word characters and wraps each surviving token in double
    quotes (with embedded " doubled). This treats FTS5 operators literally
    and prevents syntax errors on queries like "user.create" or "a OR".
    Returns "" when no usable tokens remain.
    """
    tokens = [t for t in re.split(r"\W+", query) if t]
    if not tokens:
        return ""
    return " ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens)


class Storage:
    """Persistent storage for nodes, edges, and file hashes using SQLite."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        db_dir = project_root / ".causal-graph"
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "index.db"
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FK enforcement disabled: edges legitimately reference external symbols
        # (e.g., builtins.str.startswith, third-party imports) not in the nodes table
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                id          TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                module      TEXT NOT NULL,
                file        TEXT NOT NULL,
                line_start  INTEGER NOT NULL,
                line_end    INTEGER NOT NULL,
                signature   TEXT,
                docstring   TEXT,
                is_public   INTEGER NOT NULL DEFAULT 1,
                is_test     INTEGER NOT NULL DEFAULT 0,
                body_hash   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                src         TEXT NOT NULL,
                dst         TEXT NOT NULL,
                kind        TEXT NOT NULL,
                confidence  REAL NOT NULL DEFAULT 1.0,
                weight      INTEGER NOT NULL DEFAULT 1,
                scope       TEXT,
                detail      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
            CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);

            CREATE TABLE IF NOT EXISTS file_hashes (
                file        TEXT PRIMARY KEY,
                sha256      TEXT NOT NULL,
                indexed_at  INTEGER NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                id,
                signature,
                docstring,
                content='nodes',
                content_rowid='rowid'
            );
        """)
        self._conn.commit()

    def upsert_nodes(self, nodes: list[dict[str, Any]]) -> None:
        """Insert or replace nodes in the database and update FTS index."""
        if not nodes:
            return
        # First, collect existing rowids for FTS cleanup
        node_ids = [n["id"] for n in nodes]
        placeholders = ",".join("?" for _ in node_ids)
        existing = self._conn.execute(
            f"SELECT rowid, id, signature, docstring FROM nodes WHERE id IN ({placeholders})",
            node_ids,
        ).fetchall()
        # Delete old FTS entries for existing nodes
        for row in existing:
            self._conn.execute(
                "INSERT INTO nodes_fts(nodes_fts, rowid, id, signature, docstring) "
                "VALUES('delete', ?, ?, ?, ?)",
                (row["rowid"], row["id"], row["signature"] or "", row["docstring"] or ""),
            )

        for node in nodes:
            self._conn.execute(
                "INSERT OR REPLACE INTO nodes "
                "(id, kind, module, file, line_start, line_end, signature, docstring, is_public, is_test, body_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node["id"],
                    node["kind"],
                    node["module"],
                    node["file"],
                    node["line_start"],
                    node["line_end"],
                    node.get("signature"),
                    node.get("docstring"),
                    node.get("is_public", 1),
                    node.get("is_test", 0),
                    node["body_hash"],
                ),
            )

        # Insert new FTS entries
        new_rows = self._conn.execute(
            f"SELECT rowid, id, signature, docstring FROM nodes WHERE id IN ({placeholders})",
            node_ids,
        ).fetchall()
        for row in new_rows:
            self._conn.execute(
                "INSERT INTO nodes_fts(rowid, id, signature, docstring) VALUES(?, ?, ?, ?)",
                (row["rowid"], row["id"], row["signature"] or "", row["docstring"] or ""),
            )
        self._conn.commit()

    def insert_edges(self, edges: list[dict[str, Any]]) -> None:
        """Insert edges into the database."""
        if not edges:
            return
        for edge in edges:
            self._conn.execute(
                "INSERT INTO edges (src, dst, kind, confidence, weight, scope, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    edge["src"],
                    edge["dst"],
                    edge["kind"],
                    edge.get("confidence", 1.0),
                    edge.get("weight", 1),
                    edge.get("scope"),
                    edge.get("detail"),
                ),
            )
        self._conn.commit()

    def delete_file(self, file_path: str) -> None:
        """Delete all nodes and their associated edges for a given file."""
        # Get node IDs for this file
        rows = self._conn.execute(
            "SELECT id FROM nodes WHERE file = ?", (file_path,)
        ).fetchall()
        node_ids = [row["id"] for row in rows]

        if not node_ids:
            self._conn.execute("DELETE FROM file_hashes WHERE file = ?", (file_path,))
            self._conn.commit()
            return

        placeholders = ",".join("?" for _ in node_ids)

        # Delete FTS entries
        fts_rows = self._conn.execute(
            f"SELECT rowid, id, signature, docstring FROM nodes WHERE id IN ({placeholders})",
            node_ids,
        ).fetchall()
        for row in fts_rows:
            self._conn.execute(
                "INSERT INTO nodes_fts(nodes_fts, rowid, id, signature, docstring) "
                "VALUES('delete', ?, ?, ?, ?)",
                (row["rowid"], row["id"], row["signature"] or "", row["docstring"] or ""),
            )

        # Delete edges referencing these nodes
        self._conn.execute(
            f"DELETE FROM edges WHERE src IN ({placeholders}) OR dst IN ({placeholders})",
            node_ids + node_ids,
        )
        # Delete nodes
        self._conn.execute(
            f"DELETE FROM nodes WHERE id IN ({placeholders})", node_ids
        )
        # Delete file hash
        self._conn.execute("DELETE FROM file_hashes WHERE file = ?", (file_path,))
        self._conn.commit()

    def update_file_scope(
        self,
        file_path: str,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        sha256: str,
    ) -> None:
        """Atomically delete all data for a file and reinsert with new data."""
        try:
            self._conn.execute("BEGIN")

            # Get existing node IDs for FTS cleanup
            rows = self._conn.execute(
                "SELECT id FROM nodes WHERE file = ?", (file_path,)
            ).fetchall()
            old_node_ids = [row["id"] for row in rows]

            if old_node_ids:
                placeholders = ",".join("?" for _ in old_node_ids)
                # Delete FTS entries
                fts_rows = self._conn.execute(
                    f"SELECT rowid, id, signature, docstring FROM nodes WHERE id IN ({placeholders})",
                    old_node_ids,
                ).fetchall()
                for row in fts_rows:
                    self._conn.execute(
                        "INSERT INTO nodes_fts(nodes_fts, rowid, id, signature, docstring) "
                        "VALUES('delete', ?, ?, ?, ?)",
                        (row["rowid"], row["id"], row["signature"] or "", row["docstring"] or ""),
                    )
                # Delete edges
                self._conn.execute(
                    f"DELETE FROM edges WHERE src IN ({placeholders}) OR dst IN ({placeholders})",
                    old_node_ids + old_node_ids,
                )
                # Delete nodes
                self._conn.execute(
                    f"DELETE FROM nodes WHERE id IN ({placeholders})", old_node_ids
                )

            # Insert new nodes
            for node in nodes:
                self._conn.execute(
                    "INSERT OR REPLACE INTO nodes "
                    "(id, kind, module, file, line_start, line_end, signature, docstring, is_public, is_test, body_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        node["id"],
                        node["kind"],
                        node["module"],
                        node["file"],
                        node["line_start"],
                        node["line_end"],
                        node.get("signature"),
                        node.get("docstring"),
                        node.get("is_public", 1),
                        node.get("is_test", 0),
                        node["body_hash"],
                    ),
                )

            # Insert FTS entries for new nodes
            new_node_ids = [n["id"] for n in nodes]
            if new_node_ids:
                placeholders = ",".join("?" for _ in new_node_ids)
                new_fts_rows = self._conn.execute(
                    f"SELECT rowid, id, signature, docstring FROM nodes WHERE id IN ({placeholders})",
                    new_node_ids,
                ).fetchall()
                for row in new_fts_rows:
                    self._conn.execute(
                        "INSERT INTO nodes_fts(rowid, id, signature, docstring) VALUES(?, ?, ?, ?)",
                        (row["rowid"], row["id"], row["signature"] or "", row["docstring"] or ""),
                    )

            # Insert new edges
            for edge in edges:
                self._conn.execute(
                    "INSERT INTO edges (src, dst, kind, confidence, weight, scope, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        edge["src"],
                        edge["dst"],
                        edge["kind"],
                        edge.get("confidence", 1.0),
                        edge.get("weight", 1),
                        edge.get("scope"),
                        edge.get("detail"),
                    ),
                )

            # Update file hash
            self._conn.execute(
                "INSERT OR REPLACE INTO file_hashes (file, sha256, indexed_at) VALUES (?, ?, ?)",
                (file_path, sha256, int(time.time())),
            )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Retrieve a single node by ID."""
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_edges(
        self, node_id: str, direction: str = "both"
    ) -> list[dict[str, Any]]:
        """Retrieve edges for a node. Direction: 'out', 'in', or 'both'."""
        if direction == "out":
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE src = ?", (node_id,)
            ).fetchall()
        elif direction == "in":
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE dst = ?", (node_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE src = ? OR dst = ?", (node_id, node_id)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_file_hash(self, file_path: str) -> str | None:
        """Get the stored SHA-256 hash for a file."""
        row = self._conn.execute(
            "SELECT sha256 FROM file_hashes WHERE file = ?", (file_path,)
        ).fetchone()
        return row["sha256"] if row else None

    def set_file_hash(self, file_path: str, sha256: str) -> None:
        """Set or update the SHA-256 hash for a file."""
        self._conn.execute(
            "INSERT OR REPLACE INTO file_hashes (file, sha256, indexed_at) VALUES (?, ?, ?)",
            (file_path, sha256, int(time.time())),
        )
        self._conn.commit()

    def search(
        self,
        query: str,
        kinds: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search nodes via FTS5 BM25 ranking."""
        if not query or not query.strip():
            return []
        fts_query = _fts_safe_query(query)
        if not fts_query:
            return []
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            rows = self._conn.execute(
                f"""
                SELECT n.*, rank
                FROM nodes_fts fts
                JOIN nodes n ON n.id = fts.id
                WHERE nodes_fts MATCH ?
                AND n.kind IN ({placeholders})
                ORDER BY rank
                LIMIT ?
                """,
                [fts_query] + kinds + [limit],
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT n.*, rank
                FROM nodes_fts fts
                JOIN nodes n ON n.id = fts.id
                WHERE nodes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_edges(self) -> list[dict[str, Any]]:
        """Return every edge in the database."""
        rows = self._conn.execute("SELECT * FROM edges").fetchall()
        return [dict(r) for r in rows]

    def get_all_nodes(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Get all nodes, optionally filtered by kind."""
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM nodes WHERE kind = ?", (kind,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM nodes").fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict[str, Any]:
        """Get summary statistics for the graph."""
        total_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        edge_rows = self._conn.execute(
            "SELECT kind, COUNT(*) as cnt FROM edges GROUP BY kind"
        ).fetchall()
        edge_breakdown = {row["kind"]: row["cnt"] for row in edge_rows}
        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "edge_breakdown": edge_breakdown,
        }

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
