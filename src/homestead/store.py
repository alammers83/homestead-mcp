"""Append-only JSON Lines records in the state directory (local disk or the GCS mount)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class JsonlStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text("utf-8").splitlines() if x.strip()]
