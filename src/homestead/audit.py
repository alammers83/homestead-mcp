"""Append-only audit log of every tool call and approval decision (JSON Lines)."""

from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SENSITIVE = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|webhook)", re.I)
REDACTED = "[redacted]"


def redact(value: Any) -> Any:
    """Recursively redact values whose keys look like credentials."""
    if isinstance(value, dict):
        return {k: (REDACTED if _SENSITIVE.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def record(
        self,
        *,
        tool: str,
        tier: str,
        actor: str,
        outcome: str,
        params: dict[str, Any] | None = None,
        detail: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "ts": self._now().isoformat(),
            "tool": tool,
            "tier": tier,
            "actor": actor,
            "outcome": outcome,
            "params": redact(params or {}),
        }
        if detail:
            entry["detail"] = detail
        if request_id:
            entry["request_id"] = request_id
        line = json.dumps(entry, sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        # stderr, never stdout: stdout carries the MCP protocol in stdio mode.
        print(line, file=sys.stderr)
        return entry

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-n:] if line.strip()]
