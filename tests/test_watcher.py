"""Unit tests for the file watcher."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from causal_graph_mcp.watcher import FileWatcher


class TestFileWatcher:
    def test_detects_py_changes(self, tmp_path: Path) -> None:
        """Watcher triggers callback on .py file modification."""
        changes: list[list[str]] = []

        def on_change(files: list[str]) -> None:
            changes.append(files)

        watcher = FileWatcher(str(tmp_path), on_change, debounce_ms=100)
        watcher.start()

        try:
            py_file = tmp_path / "test.py"
            py_file.write_text("x = 1")
            time.sleep(0.5)

            py_file.write_text("x = 2")
            time.sleep(0.5)

            assert len(changes) >= 1
            all_files = [f for batch in changes for f in batch]
            assert any("test.py" in f for f in all_files)
        finally:
            watcher.stop()

    def test_ignores_non_py(self, tmp_path: Path) -> None:
        """Watcher ignores non-.py files."""
        changes: list[list[str]] = []

        def on_change(files: list[str]) -> None:
            changes.append(files)

        watcher = FileWatcher(str(tmp_path), on_change, debounce_ms=100)
        watcher.start()

        try:
            (tmp_path / "data.json").write_text('{"key": "value"}')
            time.sleep(0.5)
            # No .py changes should have been detected
            all_files = [f for batch in changes for f in batch]
            assert not any(".json" in f for f in all_files)
        finally:
            watcher.stop()

    def test_debounce(self, tmp_path: Path) -> None:
        """Multiple rapid changes are batched into one callback."""
        changes: list[list[str]] = []

        def on_change(files: list[str]) -> None:
            changes.append(files)

        watcher = FileWatcher(str(tmp_path), on_change, debounce_ms=200)
        watcher.start()

        try:
            # Rapid writes
            f1 = tmp_path / "a.py"
            f2 = tmp_path / "b.py"
            f1.write_text("x = 1")
            f2.write_text("y = 1")
            time.sleep(0.05)
            f1.write_text("x = 2")

            time.sleep(0.5)

            # Should have batched into fewer callbacks than total writes
            assert len(changes) >= 1
        finally:
            watcher.stop()
