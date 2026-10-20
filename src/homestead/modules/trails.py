"""Rank configured trails for a window: dog-friendly, and shaded or watered when it's hot.

There's no good free trails API, so trails live in the household config with
your own read on shade and water access. Ranking is a pure function.
"""

from __future__ import annotations

from typing import Any

from homestead.config import Trail

_SHADE = {"high": 2, "medium": 1, "low": 0}
WARM_MARGIN_F = 8  # within this many degrees of the dog limit counts as "warm"


def rank_trails(
    trails: list[Trail],
    *,
    max_temp_f: float,
    dog_heat_limit_f: float,
    with_dog: bool,
    duration_hours: float,
    pace_mph: float = 2.0,
) -> dict[str, Any]:
    too_hot = with_dog and max_temp_f > dog_heat_limit_f
    warm = max_temp_f >= dog_heat_limit_f - WARM_MARGIN_F
    reach = duration_hours * pace_mph

    picks, skipped = [], []
    for t in trails:
        if with_dog and not t.dogs_allowed:
            skipped.append({"trail": t.name, "why": "no dogs"})
            continue
        if t.miles > reach:
            skipped.append({"trail": t.name, "why": f"{t.miles} mi is more than ~{reach:.1f} mi"})
            continue
        if warm and t.shade == "low":
            skipped.append({"trail": t.name, "why": "little shade on a warm day"})
            continue
        if too_hot and t.shade != "high":
            skipped.append({"trail": t.name, "why": "only fully shaded trails when it's this hot"})
            continue

        weight = 2.0 if warm else 0.5
        score = _SHADE[t.shade] * weight + (1.5 if warm else 0.3) * t.water
        score += 1 - abs(reach - t.miles) / max(reach, 1)  # prefer trails that fill the window
        why = [f"{t.shade} shade"]
        if t.water:
            why.append("water access")
        if t.notes:
            why.append(t.notes)
        picks.append(
            {
                "trail": t.name,
                "miles": t.miles,
                "surface": t.surface,
                "why": "; ".join(why),
                "score": round(score, 2),
            }
        )

    picks.sort(key=lambda p: -p["score"])
    warning = None
    if too_hot:
        warning = (
            f"Forecast high in the window is {max_temp_f:.0f}°F, over the dog limit "
            f"({dog_heat_limit_f:.0f}°F). Even shaded trails are risky; try an earlier start."
        )
    return {"suggestions": picks, "skipped": skipped, "warning": warning}
