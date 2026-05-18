"""OrderLog — append-only JSONL persistence for OrderRecord.

Per design 7.C.5 / 8.15 / tasks.md task 23. Satisfies clause 2.17.

Each successful checkout writes one JSON-line record to
``settings.order_log_path``. Cross-platform locking via portalocker
ensures concurrent writers don't interleave.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterator

try:
    import portalocker
    _HAS_PORTALOCKER = True
except ImportError:  # pragma: no cover — fallback when not installed
    portalocker = None  # type: ignore[assignment]
    _HAS_PORTALOCKER = False

from .state import OrderRecord


class OrderLog:
    """Append-only JSONL store for OrderRecord."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: OrderRecord) -> None:
        """Append one record as JSON-line, creating parent dirs if needed.

        Cross-platform file lock via portalocker (when available) prevents
        partial-line interleaving between concurrent writers; an in-process
        threading.Lock guards same-process writers.
        """
        line = record.model_dump_json() + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                if _HAS_PORTALOCKER:
                    portalocker.lock(f, portalocker.LOCK_EX)
                    try:
                        f.write(line)
                        f.flush()
                    finally:
                        portalocker.unlock(f)
                else:
                    f.write(line)
                    f.flush()

    def read_all(self) -> Iterator[OrderRecord]:
        """Yield every persisted OrderRecord in append order.

        Skips malformed lines silently (used mostly in tests/admin).
        """
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    yield OrderRecord.model_validate(payload)
                except (json.JSONDecodeError, ValueError):
                    continue

    def clear(self) -> None:
        """Truncate the log (test/admin use)."""
        with self._lock:
            if self._path.exists():
                self._path.unlink()
