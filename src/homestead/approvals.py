"""Approval queue for APPROVAL-tier tools.

The MCP server can only *create* requests. Approving, denying, and executing
happen exclusively through the `homestead` CLI run by a human, so the agent has
no code path that approves its own request.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TTL = timedelta(hours=24)


class ApprovalError(RuntimeError):
    pass


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    params: dict[str, Any]
    created_at: str
    expires_at: str
    status: str = "pending"  # pending | executed | denied | failed
    decided_at: str | None = None
    result: Any = None
    error: str | None = None
    history: list[str] = field(default_factory=list)


class ApprovalQueue:
    def __init__(
        self,
        directory: Path,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(UTC))
        self.ttl = ttl

    def _path(self, request_id: str) -> Path:
        if not request_id.isalnum():
            raise ApprovalError(f"Invalid request id {request_id!r}")
        return self.dir / f"{request_id}.json"

    def _save(self, req: ApprovalRequest) -> None:
        self._path(req.id).write_text(json.dumps(asdict(req), indent=2), encoding="utf-8")

    def create(self, tool: str, params: dict[str, Any]) -> ApprovalRequest:
        now = self._now()
        req = ApprovalRequest(
            id=secrets.token_hex(4),
            tool=tool,
            params=params,
            created_at=now.isoformat(),
            expires_at=(now + self.ttl).isoformat(),
        )
        self._save(req)
        return req

    def get(self, request_id: str) -> ApprovalRequest:
        path = self._path(request_id)
        if not path.exists():
            raise ApprovalError(f"No approval request {request_id!r}")
        return ApprovalRequest(**json.loads(path.read_text(encoding="utf-8")))

    def list(self, status: str | None = "pending") -> list[ApprovalRequest]:
        reqs = [
            ApprovalRequest(**json.loads(p.read_text("utf-8"))) for p in self.dir.glob("*.json")
        ]
        if status:
            reqs = [r for r in reqs if r.status == status and not self.is_expired(r)]
        return sorted(reqs, key=lambda r: r.created_at)

    def is_expired(self, req: ApprovalRequest) -> bool:
        return self._now() >= datetime.fromisoformat(req.expires_at)

    def _pending(self, request_id: str) -> ApprovalRequest:
        req = self.get(request_id)
        if req.status != "pending":
            raise ApprovalError(f"Request {request_id} is already {req.status}")
        if self.is_expired(req):
            raise ApprovalError(f"Request {request_id} expired at {req.expires_at}")
        return req

    def approve_and_execute(
        self, request_id: str, executor: Callable[[str, dict[str, Any]], Any]
    ) -> ApprovalRequest:
        req = self._pending(request_id)
        req.decided_at = self._now().isoformat()
        try:
            req.result = executor(req.tool, req.params)
            req.status = "executed"
        except Exception as exc:  # recorded, then surfaced to the CLI
            req.status = "failed"
            req.error = f"{type(exc).__name__}: {exc}"
        self._save(req)
        return req

    def deny(self, request_id: str, reason: str = "") -> ApprovalRequest:
        req = self._pending(request_id)
        req.status = "denied"
        req.decided_at = self._now().isoformat()
        req.error = reason or None
        self._save(req)
        return req
