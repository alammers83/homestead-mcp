"""Sitter handoff packet and household reminders, assembled from the other pet modules.

`sitter_packet` is READ (a preview). `send_sitter_packet` is APPROVAL tier: the packet
is rebuilt when a human approves, so it reflects the records at that moment.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from homestead.config import HouseholdConfig
from homestead.dates import resolve_day
from homestead.modules.homecoming import HomecomingTools
from homestead.modules.pack import PackTools
from homestead.modules.supplies import SuppliesTools
from homestead.modules.vet import VetTools

MAX_TRIP_DAYS = 60


def render_packet(packet: dict[str, Any]) -> str:
    """Plain text for the sitter (the outbox or a webhook gets this)."""
    lines = [f"Pet care, {packet['dates']}", ""]
    for p in packet["pets"]:
        lines.append(f"{p['name']} ({p['species']})")
        if p["feeding"]:
            lines.append(f"  Feeding: {p['feeding']}")
        if p["notes"]:
            lines.append(f"  Notes: {p['notes']}")
    if packet["care_schedule"]:
        lines += ["", "Care due while I'm away:"]
        lines += [f"  {c['due']}: {c['pet']}, {c['task']}" for c in packet["care_schedule"]]
    if packet["supplies"]:
        lines += ["", "Supplies:"]
        lines += [f"  {s}" for s in packet["supplies"]]
    if packet["vet"]:
        v = packet["vet"]
        lines += ["", f"Vet: {v['name']} {v['phone']}".rstrip()]
        if v["emergency"]:
            lines.append(f"Emergency: {v['emergency']}")
    if packet["house_notes"]:
        lines += ["", f"House: {packet['house_notes']}"]
    return "\n".join(lines)


class SitterTools:
    def __init__(
        self,
        cfg: HouseholdConfig,
        pack: PackTools,
        vet: VetTools,
        supplies: SuppliesTools,
        homecoming: HomecomingTools,
        now,
    ) -> None:
        self.cfg = cfg
        self.pack = pack
        self.vet = vet
        self.supplies = supplies
        self.homecoming = homecoming
        self.now = now

    def _today(self) -> date:
        return self.now().astimezone(self.cfg.tz).date()

    def _range(self, start_date: str, end_date: str) -> tuple[date, date]:
        start = resolve_day(start_date, self._today())
        end = resolve_day(end_date, self._today())
        if end < start:
            raise ValueError("end_date is before start_date")
        if (end - start).days > MAX_TRIP_DAYS:
            raise ValueError(f"Keep it to {MAX_TRIP_DAYS} days or less")
        return start, end

    def _packet(self, start: date, end: date) -> dict[str, Any]:
        days = (end - start).days + 1
        supply_lines = []
        if self.cfg.supplies:
            for s in self.supplies.items():
                if "days_left" in s and s["days_left"] < days:
                    supply_lines.append(
                        f"{s['label']}: may run out (about {s['days_left']:.0f} days left)"
                    )
                elif s["status"] == "never counted":
                    supply_lines.append(f"{s['label']}: not tracked; check the supply")
        vet = self.cfg.vet
        return {
            "dates": f"{start:%a %b %-d} to {end:%a %b %-d} ({days} days)",
            "pets": [
                {"name": p.name, "species": p.species, "feeding": p.feeding, "notes": p.notes}
                for p in self.cfg.pets.values()
            ],
            "care_schedule": self.pack.due_between(start, end),
            "supplies": supply_lines,
            "vet": {"name": vet.name, "phone": vet.phone, "emergency": vet.emergency}
            if vet
            else None,
            "house_notes": self.cfg.sitter.house_notes,
        }

    # ------------------------------------------------------------------ tools

    def sitter_packet(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Preview the sitter handoff: pets, feeding, care due during the trip, supplies, vet.

        Args:
            start_date: first day away, "today", "tomorrow", or YYYY-MM-DD.
            end_date: last day away, YYYY-MM-DD.
        """
        start, end = self._range(start_date, end_date)
        packet = self._packet(start, end)
        return {**packet, "text": render_packet(packet)}

    def send_sitter_packet(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Send the sitter handoff packet for a trip. Requires human approval before delivery.

        Args:
            start_date: first day away, "today", "tomorrow", or YYYY-MM-DD.
            end_date: last day away, YYYY-MM-DD.
        """
        start, end = self._range(start_date, end_date)
        return self.pack.deliver(render_packet(self._packet(start, end)))

    def reminders(self, days_ahead: int = 14) -> dict[str, Any]:
        """Everything that needs attention soon: care, vaccines, supplies, homecoming prep.

        Args:
            days_ahead: how far ahead to look, 1-60.
        """
        if not 1 <= days_ahead <= 60:
            raise ValueError("days_ahead must be 1-60")
        today = self._today()
        horizon = today + timedelta(days=days_ahead)
        care = [
            i
            for i in self.pack.care_status()["items"]
            if i["status"] in {"overdue", "never logged"}
            or (i["next_due"][:1].isdigit() and i["next_due"] <= horizon.isoformat())
        ]
        out: dict[str, Any] = {
            "as_of": today.isoformat(),
            "care": care,
            "vaccines": [v for v in self.vet.vaccines_due(days_ahead) if v["status"] != "ok"],
        }
        if self.cfg.supplies:
            out["supplies"] = [
                s
                for s in self.supplies.items()
                if s["status"] in {"out", "low"}
                or ("days_left" in s and s["days_left"] <= days_ahead)
            ]
        h = self.cfg.homecoming
        if h and (h.arrival is None or h.arrival >= today):
            out["homecoming_prep"] = self.homecoming.open_items()
        return out
