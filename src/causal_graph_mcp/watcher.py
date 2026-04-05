"""File watcher for incremental re-indexing on .py file changes."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# Directories to ignore
_IGNORE_DIRS = frozenset({
    "__pycache__", ".venv", "venv", "env", "node_modules",
    ".git", ".causal-graph", ".tox", ".mypy_cache", ".pytest_cache",
})


class _DebouncedHandler(FileSystemEventHandler):
    """Collects .py file change events and debounces them."""

    def __init__(self, callback: Callable[[list[str]], None], debounce_ms: int = 300) -> None:
        super().__init__()
        self._callback = callback
        self._debounce_s = debounce_ms / 1000.0
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _should_ignore(self, path: str) -> bool:
        parts = Path(path).parts
        return any(part in _IGNORE_DIRS for part in parts)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path)

    def _handle(self, path: str) -> None:
        from causal_graph_mcp.language import get_source_extensions
        valid_exts = get_source_extensions() | {".py"}
        if not any(path.endswith(ext) for ext in valid_exts):
            return
        if self._should_ignore(path):
            return

        with self._lock:
            self._pending.add(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            files = list(self._pending)
            self._pending.clear()
            self._timer = None

        try:
            self._callback(files)
        except Exception:
            logger.exception("Error during incremental re-index")


class FileWatcher:
    """Watches a project directory for .py file changes and triggers re-indexing."""

    def __init__(
        self,
        project_root: str,
        on_change: Callable[[list[str]], None],
        debounce_ms: int = 300,
    ) -> None:
        self._project_root = project_root
        self._handler = _DebouncedHandler(on_change, debounce_ms)
        self._observer = Observer()
        self._observer.daemon = True

    def start(self) -> None:
        """Start watching for file changes in a background daemon thread."""
        self._observer.schedule(self._handler, self._project_root, recursive=True)
        self._observer.start()
        logger.info("File watcher started for %s", self._project_root)

    def stop(self) -> None:
        """Stop the file watcher."""
        self._observer.stop()
        self._observer.join(timeout=2)
        logger.info("File watcher stopped")
