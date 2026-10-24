"""Assemble the MCP server: every tool is registered through `ToolRegistry.add`, which
enforces its permission tier and writes the audit log. There is no other way in."""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from homestead import __version__
from homestead.approvals import ApprovalQueue
from homestead.audit import AuditLog
from homestead.auth import CALLBACK_PATH, GoogleOAuthProvider, OAuthConfig
from homestead.config import HouseholdConfig
from homestead.http import HttpClient, UpstreamError, UrllibClient
from homestead.modules.airquality import AirData
from homestead.modules.garden import GardenTools
from homestead.modules.homecoming import HomecomingTools
from homestead.modules.movies import MovieTools
from homestead.modules.outdoor import OutdoorData, OutdoorTools
from homestead.modules.pack import PackTools
from homestead.modules.sitter import SitterTools
from homestead.modules.supplies import SuppliesTools
from homestead.modules.tides import TideData
from homestead.modules.vet import VetTools
from homestead.policy import PolicyError, Tier, effective_tier

INSTRUCTIONS = """\
Homestead manages one household: outdoor plans, pets, the garden, and a horror watchlist.
Tools run at one of three permission tiers:
- READ tools have no side effects.
- WRITE tools change local records and are audited.
- APPROVAL tools never act directly. Calling one queues a request and returns its id;
  a human must approve it outside this conversation. Tell the user the request id and
  what will happen if they approve. You cannot approve requests yourself.
Go/no-go verdicts are planning aids. Report the reasons, not just the verdict.
"""


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    tier: Tier
    fn: Callable[..., Any]


class ToolRegistry:
    def __init__(
        self, server: MCPServer, cfg: HouseholdConfig, audit: AuditLog, approvals: ApprovalQueue
    ) -> None:
        self.server = server
        self.cfg = cfg
        self.audit = audit
        self.approvals = approvals
        self.tools: dict[str, RegisteredTool] = {}

    def add(self, fn: Callable[..., Any], tier: Tier, name: str | None = None) -> None:
        name = name or fn.__name__
        tier = effective_tier(name, tier, self.cfg.policy.get(name))
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
            if tier is Tier.APPROVAL:
                req = self.approvals.create(name, params)
                self.audit.record(
                    tool=name,
                    tier=tier.name,
                    actor="agent",
                    outcome="queued",
                    params=params,
                    request_id=req.id,
                )
                return {
                    "status": "pending_approval",
                    "request_id": req.id,
                    "expires_at": req.expires_at,
                    "message": (
                        f"Queued. Nothing has happened yet. A human must run "
                        f"`homestead approve {req.id}` to carry this out."
                    ),
                }
            try:
                result = fn(**params)
            except Exception as exc:
                self.audit.record(
                    tool=name,
                    tier=tier.name,
                    actor="agent",
                    outcome="error",
                    params=params,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                if isinstance(exc, ValueError | UpstreamError):
                    # Anticipated failures: the model sees the message and can correct course.
                    raise ToolError(str(exc)) from exc
                raise  # anything else is a crash; the SDK keeps its details server-side
            self.audit.record(tool=name, tier=tier.name, actor="agent", outcome="ok", params=params)
            return result

        annotations = ToolAnnotations(
            title=name.replace("_", " ").capitalize(),
            readOnlyHint=tier is Tier.READ,
            destructiveHint=False,
            openWorldHint=tier is Tier.APPROVAL,
        )
        self.server.tool(name=name, annotations=annotations)(guarded)
        self.tools[name] = RegisteredTool(name, tier, fn)

    def check_policy_targets(self) -> None:
        unknown = set(self.cfg.policy) - set(self.tools)
        if unknown:
            raise PolicyError(f"Policy references unknown tools: {sorted(unknown)}")

    def execute_approved(self, tool: str, params: dict[str, Any]) -> Any:
        """Run the underlying function for an approved request (called by the human CLI only)."""
        if tool not in self.tools:
            raise PolicyError(f"Tool {tool!r} is not registered")
        return self.tools[tool].fn(**params)


@dataclass
class App:
    server: MCPServer
    registry: ToolRegistry
    audit: AuditLog
    approvals: ApprovalQueue


def state_dir_from_env() -> Path:
    return Path(os.environ.get("HOMESTEAD_STATE_DIR", ".state"))


def build_app(
    cfg: HouseholdConfig,
    state_dir: Path,
    http: HttpClient | None = None,
    now: Callable[[], datetime] | None = None,
    oauth: OAuthConfig | None = None,
) -> App:
    now = now or (lambda: datetime.now(UTC))
    # NOAA asks API users to identify themselves; set this to your repo URL or email.
    http = http or UrllibClient(
        user_agent=os.environ.get(
            "HOMESTEAD_USER_AGENT",
            "homestead-mcp/0.1 (https://github.com/alammers83/homestead-mcp)",
        )
    )
    audit = AuditLog(state_dir / "audit.jsonl", now=now)
    approvals = ApprovalQueue(state_dir / "approvals", now=now)

    provider = GoogleOAuthProvider(oauth, state_dir, http, audit, now) if oauth else None
    server = MCPServer(
        name="homestead",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=oauth.auth_settings() if oauth else None,
    )
    if provider:
        # Google sends the browser back here; it's public by design (see auth.py).
        server.custom_route(CALLBACK_PATH, methods=["GET"])(provider.google_callback)
    registry = ToolRegistry(server, cfg, audit, approvals)

    outdoor = OutdoorTools(cfg, OutdoorData(http), now, AirData(http), TideData(http))
    registry.add(outdoor.list_activities, Tier.READ)
    registry.add(outdoor.check_conditions, Tier.READ)
    registry.add(outdoor.river_level, Tier.READ)
    registry.add(outdoor.air_quality, Tier.READ)
    registry.add(outdoor.tides, Tier.READ)
    registry.add(outdoor.daylight, Tier.READ)
    registry.add(outdoor.suggest_trails, Tier.READ)

    pack = PackTools(cfg, state_dir, http, now)
    registry.add(pack.list_pets, Tier.READ)
    registry.add(pack.care_status, Tier.READ)
    registry.add(pack.log_care, Tier.WRITE)
    registry.add(pack.notify_sitter, Tier.APPROVAL)

    vet = VetTools(cfg, state_dir, now)
    registry.add(vet.vet_status, Tier.READ)
    registry.add(vet.weight_history, Tier.READ)
    registry.add(vet.record_vaccine, Tier.WRITE)
    registry.add(vet.log_vet_visit, Tier.WRITE)
    registry.add(vet.log_weight, Tier.WRITE)

    supplies = SuppliesTools(cfg, state_dir, now)
    registry.add(supplies.supplies_status, Tier.READ)
    registry.add(supplies.record_stock, Tier.WRITE)
    registry.add(supplies.request_reorder, Tier.APPROVAL)

    homecoming = HomecomingTools(cfg, state_dir, now)
    registry.add(homecoming.homecoming_plan, Tier.READ)
    registry.add(homecoming.check_off_homecoming, Tier.WRITE)

    sitter = SitterTools(cfg, pack, vet, supplies, homecoming, now)
    registry.add(sitter.sitter_packet, Tier.READ)
    registry.add(sitter.reminders, Tier.READ)
    registry.add(sitter.send_sitter_packet, Tier.APPROVAL)

    garden = GardenTools(cfg, state_dir, http, now)
    registry.add(garden.planting_calendar, Tier.READ)
    registry.add(garden.water_today, Tier.READ)
    registry.add(garden.log_watering, Tier.WRITE)

    movies = MovieTools(cfg, state_dir, http, now)
    registry.add(movies.search_movies, Tier.READ)
    registry.add(movies.horror_picks, Tier.READ)
    registry.add(movies.watchlist, Tier.READ)
    registry.add(movies.add_to_watchlist, Tier.WRITE)
    registry.add(movies.mark_watched, Tier.WRITE)
    registry.add(movies.remove_from_watchlist, Tier.WRITE)

    def list_pending_approvals() -> dict[str, Any]:
        """List requests waiting for a human decision. Read-only; you cannot approve them."""
        return {
            "pending": [
                {"id": r.id, "tool": r.tool, "params": r.params, "expires_at": r.expires_at}
                for r in approvals.list("pending")
            ]
        }

    def recent_activity(limit: int = 20) -> dict[str, Any]:
        """Show the most recent audit-log entries (what tools ran, when, and the outcome).

        Args:
            limit: number of entries, 1-100.
        """
        return {"entries": audit.tail(max(1, min(limit, 100)))}

    registry.add(list_pending_approvals, Tier.READ)
    registry.add(recent_activity, Tier.READ)

    registry.check_policy_targets()
    return App(server, registry, audit, approvals)
