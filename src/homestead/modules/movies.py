"""Horror watchlist on TMDB: picks by subgenre and decade, where to stream, what you've seen.

Needs a free TMDB API Read Access Token in HOMESTEAD_TMDB_TOKEN (Secret Manager in
prod): https://www.themoviedb.org/settings/api. It's sent as a Bearer header, never
in the URL. Streaming availability is TMDB's, sourced from JustWatch.

This product uses the TMDB API but is not endorsed or certified by TMDB.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

from homestead.config import HouseholdConfig
from homestead.http import HttpClient
from homestead.store import JsonlStore

TMDB = "https://api.themoviedb.org/3"
TOKEN_ENV = "HOMESTEAD_TMDB_TOKEN"  # noqa: S105 (env var name, not a secret)
HORROR = 27
ATTRIBUTION = (
    "This product uses the TMDB API but is not endorsed or certified by TMDB. "
    "Streaming data from JustWatch."
)
MAX_PROVIDER_LOOKUPS = 12


def fold_watchlist(events: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Replay add / watched / remove events into the current list, keyed by TMDB id."""
    items: dict[int, dict[str, Any]] = {}
    for e in events:
        mid = int(e["tmdb_id"])
        if e["event"] == "add":
            items[mid] = {
                **items.get(mid, {}),
                **e["movie"],
                "added_at": e["at"],
                "note": e.get("note", ""),
                "watched": items.get(mid, {}).get("watched", False),
            }
        elif e["event"] == "watched":
            items[mid] = {
                **items.get(mid, e.get("movie", {})),
                "watched": True,
                "rating": e.get("rating") or None,
                "watched_note": e.get("note", ""),
                "watched_at": e["at"],
            }
        elif e["event"] == "remove":
            items.pop(mid, None)
    return items


def on_my_services(provider: str, services: tuple[str, ...]) -> bool:
    """Case-insensitive partial match, so "prime" finds "Amazon Prime Video"
    and "hbo" finds "HBO Max"."""
    name = provider.lower()
    return any(s.strip().lower() in name for s in services if s.strip())


def summarize(m: dict[str, Any]) -> dict[str, Any]:
    year = (m.get("release_date") or "")[:4]
    return {
        "tmdb_id": m["id"],
        "title": m.get("title", "?"),
        "year": int(year) if year.isdigit() else None,
        "rating": round(float(m.get("vote_average") or 0), 1),
        "votes": m.get("vote_count", 0),
        "overview": (m.get("overview") or "")[:300],
    }


def decade_range(decade: str) -> tuple[str, str]:
    """'1980s' or '80s' -> ('1980-01-01', '1989-12-31')."""
    text = decade.strip().lower().rstrip("s").lstrip("'")
    if len(text) == 2 and text.isdigit():
        text = ("19" if int(text) >= 20 else "20") + text
    if len(text) != 4 or not text.isdigit() or int(text) % 10:
        raise ValueError(f"Use a decade like '1980s' or '80s' (got {decade!r})")
    start = int(text)
    return f"{start}-01-01", f"{start + 9}-12-31"


def days_until_halloween(today: date) -> int:
    h = date(today.year, 10, 31)
    if today > h:
        h = date(today.year + 1, 10, 31)
    return (h - today).days


class MovieTools:
    def __init__(self, cfg: HouseholdConfig, state_dir: Path, http: HttpClient, now) -> None:
        self.cfg = cfg
        self.http = http
        self.now = now
        self.events = JsonlStore(state_dir / "watchlist.jsonl")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = os.environ.get(TOKEN_ENV)
        if not token:
            raise ValueError(f"TMDB isn't set up: {TOKEN_ENV} is not set")
        return self.http.get_json(
            f"{TMDB}{path}", params, headers={"Authorization": f"Bearer {token}"}
        )

    def _providers(self, tmdb_id: int) -> dict[str, Any]:
        region = (self._get(f"/movie/{tmdb_id}/watch/providers").get("results") or {}).get(
            self.cfg.movies.region
        ) or {}
        names = {
            kind: sorted({p["provider_name"] for p in region.get(kind) or []})
            for kind in ("flatrate", "free", "ads", "rent")
        }
        streaming = names["flatrate"] + names["free"] + names["ads"]
        return {
            "stream": names["flatrate"] + names["free"] + names["ads"],
            "rent": names["rent"],
            "on_your_services": [
                n for n in streaming if on_my_services(n, self.cfg.movies.services)
            ],
        }

    def _keyword_id(self, subgenre: str) -> int:
        results = self._get("/search/keyword", {"query": subgenre}).get("results") or []
        exact = [r for r in results if r["name"].lower() == subgenre.strip().lower()]
        if not (exact or results):
            raise ValueError(
                f"TMDB has no keyword like {subgenre!r}; try 'slasher' or 'folk horror'"
            )
        return int((exact or results)[0]["id"])

    def _today(self) -> date:
        return self.now().astimezone(self.cfg.tz).date()

    # ------------------------------------------------------------------ tools

    def search_movies(self, query: str) -> dict[str, Any]:
        """Search TMDB for a movie by title, to get its tmdb_id.

        Args:
            query: title, optionally with a year ("The Thing 1982").
        """
        if not query.strip():
            raise ValueError("Query is empty")
        params: dict[str, Any] = {"query": query.strip(), "include_adult": "false"}
        words = query.split()
        if len(words) > 1 and words[-1].isdigit() and len(words[-1]) == 4:
            params = {"query": " ".join(words[:-1]), "year": words[-1], "include_adult": "false"}
        results = self._get("/search/movie", params).get("results") or []
        return {"results": [summarize(m) for m in results[:8]], "attribution": ATTRIBUTION}

    def horror_picks(
        self,
        subgenre: str = "",
        decade: str = "",
        max_runtime: int = 0,
        streaming_only: bool = False,
        sort: str = "top_rated",
        limit: int = 8,
    ) -> dict[str, Any]:
        """Horror movie suggestions you haven't added or seen, with where to stream them.

        Args:
            subgenre: a TMDB keyword such as "slasher", "folk horror", "haunted house", "zombie".
            decade: e.g. "1980s" or "70s".
            max_runtime: in minutes; 0 for any length.
            streaming_only: only movies on the streaming services in your config.
            sort: "top_rated" or "popular".
            limit: how many picks, 1-12.
        """
        if sort not in {"top_rated", "popular"}:
            raise ValueError("sort must be 'top_rated' or 'popular'")
        if not 1 <= limit <= 12:
            raise ValueError("limit must be 1-12")
        if streaming_only and not self.cfg.movies.services:
            raise ValueError("Add your streaming services under movies.services in the config")
        params: dict[str, Any] = {
            "with_genres": HORROR,
            "include_adult": "false",
            "sort_by": "vote_average.desc" if sort == "top_rated" else "popularity.desc",
            "vote_count.gte": self.cfg.movies.min_votes,
        }
        if decade:
            params["primary_release_date.gte"], params["primary_release_date.lte"] = decade_range(
                decade
            )
        if max_runtime:
            params["with_runtime.lte"] = max_runtime
        if subgenre:
            params["with_keywords"] = self._keyword_id(subgenre)

        seen = set(fold_watchlist(self.events.entries()))
        candidates = []
        for page in (1, 2):
            payload = self._get("/discover/movie", {**params, "page": page})
            candidates += [m for m in payload.get("results") or [] if m["id"] not in seen]
            if len(candidates) >= limit * 2 or page >= int(payload.get("total_pages") or 1):
                break

        picks = []
        for m in candidates[:MAX_PROVIDER_LOOKUPS]:
            where = self._providers(m["id"])
            if streaming_only and not where["on_your_services"]:
                continue
            picks.append({**summarize(m), "where": where})
            if len(picks) >= limit:
                break
        return {
            "picks": picks,
            "filters": {
                k: v
                for k, v in {
                    "subgenre": subgenre,
                    "decade": decade,
                    "max_runtime": max_runtime,
                    "streaming_only": streaming_only,
                }.items()
                if v
            },
            "days_until_halloween": days_until_halloween(self._today()),
            "attribution": ATTRIBUTION,
        }

    def watchlist(self, include_watched: bool = False) -> dict[str, Any]:
        """Your horror watchlist, with where each unwatched movie is streaming.

        Args:
            include_watched: also list what you've already seen, with your ratings.
        """
        items = fold_watchlist(self.events.entries())
        to_watch = [{"tmdb_id": k, **v} for k, v in items.items() if not v.get("watched")]
        to_watch.sort(key=lambda i: i.get("added_at", ""))
        for item in to_watch[:MAX_PROVIDER_LOOKUPS]:
            item["where"] = self._providers(item["tmdb_id"])
        out: dict[str, Any] = {"to_watch": to_watch, "attribution": ATTRIBUTION}
        if include_watched:
            out["watched"] = sorted(
                ({"tmdb_id": k, **v} for k, v in items.items() if v.get("watched")),
                key=lambda i: i.get("watched_at", ""),
                reverse=True,
            )
        out["days_until_halloween"] = days_until_halloween(self._today())
        return out

    def add_to_watchlist(self, tmdb_id: int, note: str = "") -> dict[str, Any]:
        """Add a movie to the watchlist (get the tmdb_id from search_movies or horror_picks).

        Args:
            tmdb_id: TMDB movie id.
            note: optional, e.g. "for the Halloween party".
        """
        m = summarize(self._get(f"/movie/{int(tmdb_id)}"))
        movie = {"title": m["title"], "year": m["year"]}
        self.events.append(
            {
                "event": "add",
                "tmdb_id": m["tmdb_id"],
                "movie": movie,
                "note": note[:280],
                "at": self.now().isoformat(),
            }
        )
        return {"added": {"tmdb_id": m["tmdb_id"], **movie}}

    def mark_watched(self, tmdb_id: int, rating: int = 0, note: str = "") -> dict[str, Any]:
        """Mark a movie as watched, with an optional 1-5 rating. It needn't be on the list.

        Args:
            tmdb_id: TMDB movie id.
            rating: 1-5, or 0 for no rating.
            note: optional, e.g. "great practical effects".
        """
        if not 0 <= rating <= 5:
            raise ValueError("rating must be 0-5")
        items = fold_watchlist(self.events.entries())
        movie = items.get(int(tmdb_id))
        if movie is None:
            m = summarize(self._get(f"/movie/{int(tmdb_id)}"))
            movie = {"title": m["title"], "year": m["year"]}
        entry = {
            "event": "watched",
            "tmdb_id": int(tmdb_id),
            "movie": {"title": movie.get("title"), "year": movie.get("year")},
            "rating": rating,
            "note": note[:280],
            "at": self.now().isoformat(),
        }
        self.events.append(entry)
        return {"watched": entry["movie"], "rating": rating or None}

    def remove_from_watchlist(self, tmdb_id: int) -> dict[str, Any]:
        """Take a movie off the watchlist.

        Args:
            tmdb_id: TMDB movie id.
        """
        items = fold_watchlist(self.events.entries())
        if int(tmdb_id) not in items:
            raise ValueError(f"{tmdb_id} isn't on the watchlist")
        self.events.append(
            {"event": "remove", "tmdb_id": int(tmdb_id), "at": self.now().isoformat()}
        )
        return {"removed": items[int(tmdb_id)].get("title")}
