"""Permission tiers for MCP tools.

Every tool declares a tier in code. The household config may *tighten* a tool's
tier (e.g. require approval for logging care) but can never loosen it. That
rule is enforced here and covered by tests, so a config change in a pull
request can't quietly hand the agent more autonomy than the code allows.
"""

from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    READ = 0  # no side effects; runs freely
    WRITE = 1  # local, reversible side effects; runs autonomously, always audited
    APPROVAL = 2  # reaches outside the system; queued until a human approves

    @classmethod
    def parse(cls, value: str) -> Tier:
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            valid = ", ".join(t.name.lower() for t in cls)
            raise PolicyError(f"Unknown tier {value!r}. Valid tiers: {valid}") from exc


class PolicyError(ValueError):
    """Raised when a policy would grant more autonomy than the code allows."""


def effective_tier(tool: str, declared: Tier, override: Tier | None) -> Tier:
    """Return the tier a tool actually runs at, given an optional config override."""
    if override is None:
        return declared
    if override < declared:
        raise PolicyError(
            f"Policy for {tool!r} tries to loosen {declared.name} to {override.name}. "
            "Overrides may only tighten a tool's tier."
        )
    return override
