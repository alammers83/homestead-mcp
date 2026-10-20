"""Food and medication inventory: estimated stock from the last count and daily use.

You count what's on hand (`record_stock`); the estimate draws down by `daily_use`
each day. Reordering is APPROVAL tier: an approved request goes on the shopping
list in the state directory. Nothing here buys anything.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from homestead.config import HouseholdConfig, Supply
from homestead.store import JsonlStore


def estimate(supply: Supply, counted: float, counted_at: datetime, now: datetime) -> dict[str, Any]:
    days = max(0.0, (now - counted_at).total_seconds() / 86400)
    left = max(0.0, counted - supply.daily_use * days)
    days_left = left / supply.daily_use
    if left <= 0:
        status = "out"
    elif days_left <= supply.low_days:
        status = "low"
    else:
        status = "ok"
    return {
        "estimated_left": round(left, 1),
        "days_left": round(days_left, 1),
        "status": status,
    }


class SuppliesTools:
    def __init__(self, cfg: HouseholdConfig, state_dir: Path, now) -> None:
        self.cfg = cfg
        self.now = now
        self.counts = JsonlStore(state_dir / "stock.jsonl")
        self.shopping = JsonlStore(state_dir / "shopping_list.jsonl")

    def _supply(self, item: str) -> Supply:
        key = item.strip().lower()
        for s in self.cfg.supplies.values():
            if key in {s.key.lower(), s.label.lower()}:
                return s
        raise ValueError(f"Unknown item {item!r}. Options: {sorted(self.cfg.supplies)}")

    def items(self) -> list[dict[str, Any]]:
        last: dict[str, dict[str, Any]] = {}
        for e in self.counts.entries():
            if e["item"] not in last or e["at"] >= last[e["item"]]["at"]:
                last[e["item"]] = e
        now = self.now()
        out = []
        for s in self.cfg.supplies.values():
            row: dict[str, Any] = {
                "item": s.key,
                "label": s.label,
                "unit": s.unit,
                "daily_use": s.daily_use,
                "for": [self.cfg.pets[p].name for p in s.pets],
            }
            if s.key in last:
                e = last[s.key]
                row |= {"last_count": e["quantity"], "counted_at": e["at"]}
                row |= estimate(s, e["quantity"], datetime.fromisoformat(e["at"]), now)
            else:
                row["status"] = "never counted"
            out.append(row)
        order = {"out": 0, "low": 1, "never counted": 2, "ok": 3}
        return sorted(out, key=lambda r: order[r["status"]])

    # ------------------------------------------------------------------ tools

    def supplies_status(self) -> dict[str, Any]:
        """Estimated food and medication on hand, days left, and what's running low."""
        if not self.cfg.supplies:
            raise ValueError("No supplies in the household config")
        return {"as_of": self.now().isoformat(), "items": self.items()}

    def record_stock(self, item: str, quantity: float, note: str = "") -> dict[str, Any]:
        """Record how much of an item is on hand right now (after a count or a delivery).

        Args:
            item: item key or label from supplies_status.
            quantity: amount on hand, in the item's unit.
            note: optional short note.
        """
        s = self._supply(item)
        if quantity < 0:
            raise ValueError("quantity can't be negative")
        entry = {"item": s.key, "quantity": float(quantity), "at": self.now().isoformat()}
        if note:
            entry["note"] = note[:280]
        return {"recorded": self.counts.append(entry), "unit": s.unit}

    def request_reorder(self, item: str, quantity: float, note: str = "") -> dict[str, Any]:
        """Ask to reorder an item. Requires human approval; then it goes on the shopping list.

        Args:
            item: item key or label from supplies_status.
            quantity: how much to buy, in the item's unit or as packages (say which in note).
            note: e.g. "the 30 lb bag", "same as last time".
        """
        s = self._supply(item)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        entry = {
            "item": s.key,
            "label": s.label,
            "quantity": float(quantity),
            "unit": s.unit,
            "where": s.reorder_note,
            "note": note[:280],
            "added_at": self.now().isoformat(),
        }
        return {
            "added_to_shopping_list": self.shopping.append(entry),
            "path": str(self.shopping.path),
        }
