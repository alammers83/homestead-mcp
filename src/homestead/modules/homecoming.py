"""A new dog's homecoming: a gear checklist and a staged introduction plan.

The plan is common shelter and trainer guidance (decompression first, neutral-ground
meetings, slow and supervised cat introductions), dated from the arrival day. It's
a starting point, not a behaviorist; the stop signs say when to slow down.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from homestead.config import HouseholdConfig
from homestead.dates import resolve_day
from homestead.store import JsonlStore

GEAR = {
    "collar_tag": "Collar with an ID tag (your phone number)",
    "microchip": "Microchip registered to you",
    "leash": "A 6 ft leash of the new dog's own",
    "harness": "Well-fitted harness",
    "crate": "Crate sized for the new dog, with bedding, in a quiet spot",
    "gates": "Baby gates to split the house and give the cats escape routes",
    "bowls": "Separate food and water bowls, fed apart from the other dog",
    "food": "The same food the shelter or foster used, for the first week or two",
    "bed": "A bed of the new dog's own",
    "chews": "Separate chews and toys (pick up high-value items while the dogs adjust)",
    "cat_refuge": "Cat-only rooms or high perches the dog can't reach",
    "records": "Adoption and medical records",
    "vet_appt": "First vet visit booked within two weeks",
    "cleaner": "Enzyme cleaner for accidents",
}

STOP_SIGNS = [
    "stiff body, hard stare, raised hackles, or growling between the dogs",
    "fixating on a cat: locked eyes, ignoring treats, lunging at the gate",
    "a cat hiding and not eating, or using the floor instead of the litter box",
    "If you see these, go back a stage for a few days. Bring in a trainer if it keeps up.",
]


def schedule(
    new_dog: str, dogs: list[str], cats: list[str], arrival: date | None
) -> list[dict[str, Any]]:
    resident = " and ".join(dogs) or "the other dogs"
    kitties = " and ".join(cats) or "the cats"
    stages: list[tuple[int, int, str, list[str]]] = [
        (
            0,
            0,
            "Arrival day",
            [
                f"Walk {new_dog} and {resident} side by side on neutral ground (a street or park, "
                "not your yard) before going inside. Loose leashes; split up if either stiffens.",
                f"{kitties} stay behind a closed door. {new_dog} explores the house on leash.",
                "Keep it quiet: no visitors, no big outings.",
            ],
        ),
        (
            1,
            3,
            "Decompression",
            [
                "Short walks, a predictable routine, lots of crate or gated rest.",
                f"Feed {new_dog} and {resident} in separate rooms. No loose toys or chews together.",
                f"Scent swap: trade bedding between {new_dog} and {kitties}.",
            ],
        ),
        (
            4,
            7,
            "Getting acquainted",
            [
                f"Short, supervised time together inside for {new_dog} and {resident}; "
                "end on a calm note.",
                f"Let {kitties} see {new_dog} through a gate, {new_dog} on leash. Reward calm looks "
                "away from the cats.",
            ],
        ),
        (
            8,
            14,
            "Building routine",
            [
                f"{new_dog} and {resident} loose together while you're home and watching; "
                "separate them when you're out.",
                f"Same room as {kitties} with {new_dog} on leash and the cats free to leave.",
            ],
        ),
        (
            15,
            30,
            "More freedom",
            [
                f"Gradually drop the leash indoors around the cats while {new_dog} stays relaxed.",
                f"Still never leave {new_dog} alone with {kitties} until {new_dog} has been reliably calm for weeks.",
            ],
        ),
        (
            31,
            90,
            "Settling in",
            [
                "The 3-3-3 rule of thumb: about 3 days to decompress, 3 weeks to learn the routine, "
                "3 months to feel at home.",
            ],
        ),
    ]
    out = []
    for first, last, title, steps in stages:
        row: dict[str, Any] = {
            "days": f"day {first}" if first == last else f"days {first}-{last}",
            "stage": title,
            "steps": steps,
        }
        if arrival:
            row["from"] = (arrival + timedelta(days=first)).isoformat()
            row["to"] = (arrival + timedelta(days=last)).isoformat()
        out.append(row)
    return out


class HomecomingTools:
    def __init__(self, cfg: HouseholdConfig, state_dir: Path, now) -> None:
        self.cfg = cfg
        self.now = now
        self.checks = JsonlStore(state_dir / "homecoming.jsonl")

    def _done(self) -> dict[str, bool]:
        done: dict[str, bool] = {}
        for e in self.checks.entries():
            done[e["item"]] = bool(e["done"])
        return done

    def open_items(self) -> list[str]:
        done = self._done()
        return [label for key, label in GEAR.items() if not done.get(key)]

    def homecoming_plan(self, arrival_date: str = "") -> dict[str, Any]:
        """The new dog's gear checklist (with what's done) and the dated introduction plan.

        Args:
            arrival_date: YYYY-MM-DD to override the configured arrival date; empty to use it.
        """
        h = self.cfg.homecoming
        if h is None:
            raise ValueError("No homecoming section in the household config")
        today = self.now().astimezone(self.cfg.tz).date()
        arrival = resolve_day(arrival_date, today) if arrival_date else h.arrival
        pets = self.cfg.pets
        plan = schedule(
            pets[h.pet].name,
            [pets[d].name for d in h.resident_dogs],
            [pets[c].name for c in h.resident_cats],
            arrival,
        )
        done = self._done()
        current = None
        if arrival:
            day_n = (today - arrival).days
            current = next(
                (s["stage"] for s in plan if s["from"] <= today.isoformat() <= s["to"]),
                "before arrival" if day_n < 0 else "past the plan",
            )
        return {
            "pet": pets[h.pet].name,
            "arrival": arrival.isoformat() if arrival else "not set",
            "current_stage": current,
            "checklist": [
                {"item": key, "label": label, "done": done.get(key, False)}
                for key, label in GEAR.items()
            ],
            "plan": plan,
            "stop_signs": STOP_SIGNS,
        }

    def check_off_homecoming(self, item: str, done: bool = True) -> dict[str, Any]:
        """Mark a homecoming checklist item done (or not done).

        Args:
            item: item key from homecoming_plan, e.g. "crate", "gates".
            done: false to un-check it.
        """
        if self.cfg.homecoming is None:
            raise ValueError("No homecoming section in the household config")
        if item not in GEAR:
            raise ValueError(f"Unknown item {item!r}. Options: {list(GEAR)}")
        entry = {"item": item, "done": done, "at": self.now().isoformat()}
        self.checks.append(entry)
        remaining = self.open_items()
        return {"updated": entry, "remaining": len(remaining)}
