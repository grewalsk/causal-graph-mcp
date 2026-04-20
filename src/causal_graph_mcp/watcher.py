"""File watcher for incremental re-indexing on .py file changes."""

from __future__ import annotations

import logging
import threading
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
    """Collects source file change events and debounces them."""

    def __init__(
        self,
        on_change: Callable[[list[str]], None],
        on_delete: Callable[[list[str]], None] | None = None,
        debounce_ms: int = 300,
    ) -> None:
        super().__init__()
        self._on_change = on_change
        self._on_delete = on_delete
        self._debounce_s = debounce_ms / 1000.0
        self._pending: set[str] = set()
        self._pending_deletes: set[str] = set()
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

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path, deleted=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path, deleted=True)
        dest = getattr(event, "dest_path", None)
        if dest:
            self._handle(dest)

    def _handle(self, path: str, deleted: bool = False) -> None:
        from causal_graph_mcp.language import get_source_extensions
        valid_exts = get_source_extensions() | {".py"}
        if not any(path.endswith(ext) for ext in valid_exts):
            return
        if self._should_ignore(path):
            return

        with self._lock:
            if deleted:
                self._pending_deletes.add(path)
                self._pending.discard(path)
            else:
                self._pending.add(path)
                self._pending_deletes.discard(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            files = list(self._pending)
            deletes = list(self._pending_deletes)
            self._pending.clear()
            self._pending_deletes.clear()
            self._timer = None

        if deletes and self._on_delete is not None:
            try:
                self._on_delete(deletes)
            except Exception:
                logger.exception("Error handling file deletions")
        if files:
            try:
                self._on_change(files)
            except Exception:
                logger.exception("Error during incremental re-index")


class FileWatcher:
    """Watches a project directory for source file changes and triggers re-indexing."""

    def __init__(
        self,
        project_root: str,
        on_change: Callable[[list[str]], None],
        on_delete: Callable[[list[str]], None] | None = None,
        debounce_ms: int = 300,
    ) -> None:
        self._project_root = project_root
        self._handler = _DebouncedHandler(on_change, on_delete, debounce_ms)
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
