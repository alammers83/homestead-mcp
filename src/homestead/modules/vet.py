"""Vet visits, vaccines with due dates, and weight tracking (append-only records)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from homestead.config import HouseholdConfig
from homestead.dates import add_months, past_day
from homestead.modules.pack import find_pet
from homestead.store import JsonlStore

DUE_SOON_DAYS = 30
FAST_CHANGE_PCT = 5  # a 5% swing in a month is worth asking the vet about


def vaccine_status(due: date, today: date, soon_days: int = DUE_SOON_DAYS) -> str:
    if due < today:
        return "overdue"
    if due <= today + timedelta(days=soon_days):
        return "due soon"
    return "ok"


def weight_trend(
    entries: list[tuple[date, float]], today: date, target: tuple[float, float] | None
) -> dict[str, Any]:
    """Change over 30/90 days and flags, from (date, lb) pairs sorted oldest first."""
    if not entries:
        return {"latest_lb": None, "flags": []}
    latest_day, latest = entries[-1]

    def change_since(days: int) -> float | None:
        """Latest minus the oldest reading inside the window."""
        cutoff = today - timedelta(days=days)
        window = [w for d, w in entries[:-1] if d >= cutoff]
        return round(latest - window[0], 1) if window else None

    flags = []
    month = [w for d, w in entries if d >= latest_day - timedelta(days=31)]
    if len(month) > 1 and abs(latest - month[0]) / month[0] * 100 >= FAST_CHANGE_PCT:
        pct = (latest - month[0]) / month[0] * 100
        flags.append(f"{pct:+.1f}% in about a month; worth mentioning to the vet")
    if target and not target[0] <= latest <= target[1]:
        flags.append(f"outside the target range {target[0]:g}-{target[1]:g} lb")
    return {
        "latest_lb": latest,
        "weighed_on": latest_day.isoformat(),
        "change_30d_lb": change_since(30),
        "change_90d_lb": change_since(90),
        "target_lb": list(target) if target else None,
        "flags": flags,
    }


class VetTools:
    def __init__(self, cfg: HouseholdConfig, state_dir: Path, now) -> None:
        self.cfg = cfg
        self.now = now
        self.vaccines = JsonlStore(state_dir / "vaccines.jsonl")
        self.visits = JsonlStore(state_dir / "vet_visits.jsonl")
        self.weights = JsonlStore(state_dir / "weights.jsonl")

    def _today(self) -> date:
        return self.now().astimezone(self.cfg.tz).date()

    def _weights(self, pet_key: str) -> list[tuple[date, float]]:
        rows = [
            (date.fromisoformat(e["on"]), float(e["lb"]))
            for e in self.weights.entries()
            if e["pet"] == pet_key
        ]
        return sorted(rows, key=lambda r: r[0])

    def vaccines_due(self, within_days: int = DUE_SOON_DAYS) -> list[dict[str, Any]]:
        """Latest record per (pet, vaccine) with its due date and status."""
        today = self._today()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for e in self.vaccines.entries():
            k = (e["pet"], e["vaccine"].lower())
            if k not in latest or e["given_on"] >= latest[k]["given_on"]:
                latest[k] = e
        out = []
        for (pet_key, _), e in latest.items():
            due = date.fromisoformat(e["due_on"])
            out.append(
                {
                    "pet": self.cfg.pets[pet_key].name if pet_key in self.cfg.pets else pet_key,
                    "vaccine": e["vaccine"],
                    "given_on": e["given_on"],
                    "due_on": e["due_on"],
                    "status": vaccine_status(due, today, within_days),
                }
            )
        order = {"overdue": 0, "due soon": 1, "ok": 2}
        return sorted(out, key=lambda v: (order[v["status"]], v["due_on"]))

    # ------------------------------------------------------------------ tools

    def vet_status(self, pet: str = "", due_within_days: int = DUE_SOON_DAYS) -> dict[str, Any]:
        """Vaccines (with due dates), the last vet visit, and the vet's contact info.

        Args:
            pet: pet name or key; empty for everyone.
            due_within_days: count a vaccine as "due soon" within this many days.
        """
        names = None
        if pet:
            names = {find_pet(self.cfg, pet).name}
        vaccines = [
            v for v in self.vaccines_due(due_within_days) if names is None or v["pet"] in names
        ]
        last_visits: dict[str, dict[str, Any]] = {}
        for e in self.visits.entries():
            name = self.cfg.pets[e["pet"]].name if e["pet"] in self.cfg.pets else e["pet"]
            if (names is None or name in names) and (
                name not in last_visits or e["on"] >= last_visits[name]["on"]
            ):
                last_visits[name] = e
        vet = self.cfg.vet
        return {
            "vaccines": vaccines,
            "last_visits": last_visits,
            "vet": {"name": vet.name, "phone": vet.phone, "emergency": vet.emergency}
            if vet
            else None,
        }

    def record_vaccine(
        self,
        pet: str,
        vaccine: str,
        given_on: str = "today",
        valid_months: int = 12,
        note: str = "",
    ) -> dict[str, Any]:
        """Record a vaccine; the due date is given_on plus valid_months.

        Args:
            pet: pet name or key.
            vaccine: e.g. "rabies", "DHPP", "leptospirosis", "bordetella", "FVRCP".
            given_on: "today", "yesterday", or YYYY-MM-DD.
            valid_months: 12 for most annual shots; 36 for a 3-year rabies or DHPP.
        """
        p = find_pet(self.cfg, pet)
        if not vaccine.strip():
            raise ValueError("Vaccine name is empty")
        if not 1 <= valid_months <= 60:
            raise ValueError("valid_months must be 1-60")
        given = past_day(given_on, self._today())
        entry = {
            "pet": p.key,
            "vaccine": vaccine.strip()[:60],
            "given_on": given.isoformat(),
            "due_on": add_months(given, valid_months).isoformat(),
            "note": note[:280],
            "recorded_at": self.now().isoformat(),
        }
        return {"recorded": self.vaccines.append(entry)}

    def log_vet_visit(
        self,
        pet: str,
        reason: str,
        visit_date: str = "today",
        notes: str = "",
        weight_lb: float = 0,
    ) -> dict[str, Any]:
        """Record a vet visit. A weight from the visit is also added to the weight history.

        Args:
            pet: pet name or key.
            reason: e.g. "annual exam", "ear infection".
            visit_date: "today", "yesterday", or YYYY-MM-DD.
            notes: what the vet said, meds prescribed (max 1000 characters).
            weight_lb: weight measured at the visit; 0 if not weighed.
        """
        p = find_pet(self.cfg, pet)
        if not reason.strip():
            raise ValueError("Reason is empty")
        on = past_day(visit_date, self._today())
        entry = {
            "pet": p.key,
            "on": on.isoformat(),
            "reason": reason.strip()[:120],
            "notes": notes[:1000],
            "recorded_at": self.now().isoformat(),
        }
        self.visits.append(entry)
        if weight_lb:
            self.log_weight(p.key, weight_lb, on.isoformat())
        return {"recorded": entry, "weight_logged": bool(weight_lb)}

    def log_weight(self, pet: str, weight_lb: float, weighed_on: str = "today") -> dict[str, Any]:
        """Record a pet's weight.

        Args:
            pet: pet name or key.
            weight_lb: weight in pounds.
            weighed_on: "today", "yesterday", or YYYY-MM-DD.
        """
        p = find_pet(self.cfg, pet)
        if not 0.5 <= weight_lb <= 250:
            raise ValueError("weight_lb must be between 0.5 and 250")
        on = past_day(weighed_on, self._today())
        entry = {"pet": p.key, "on": on.isoformat(), "lb": round(float(weight_lb), 1)}
        return {"recorded": self.weights.append(entry)}

    def weight_history(self, pet: str) -> dict[str, Any]:
        """A pet's weight history with 30/90-day change and any flags.

        Args:
            pet: pet name or key.
        """
        p = find_pet(self.cfg, pet)
        entries = self._weights(p.key)
        return {
            "pet": p.name,
            **weight_trend(entries, self._today(), p.target_weight_lb),
            "history": [{"on": d.isoformat(), "lb": w} for d, w in entries[-24:]],
        }
