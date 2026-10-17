"""Pack care: recurring care tasks per pet, a care log, and an approval-gated sitter message."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homestead.config import HouseholdConfig
from homestead.http import HttpClient

SITTER_WEBHOOK_ENV = "HOMESTEAD_SITTER_WEBHOOK"  # injected from Secret Manager in prod


class PackTools:
    def __init__(self, cfg: HouseholdConfig, state_dir: Path, http: HttpClient, now) -> None:
        self.cfg = cfg
        self.log_path = state_dir / "care_log.jsonl"
        self.outbox_path = state_dir / "outbox.jsonl"
        self.http = http
        self.now = now
        self._lock = threading.Lock()
        state_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers

    def _entries(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(x) for x in self.log_path.read_text("utf-8").splitlines() if x.strip()]

    def _last_done(self) -> dict[tuple[str, str], datetime]:
        last: dict[tuple[str, str], datetime] = {}
        for e in self._entries():
            at = datetime.fromisoformat(e["at"])
            key = (e["pet"], e["task"])
            if key not in last or at > last[key]:
                last[key] = at
        return last

    def _pet(self, pet: str):
        key = pet.strip().lower()
        for p in self.cfg.pets.values():
            if key in {p.key.lower(), p.name.lower()}:
                return p
        raise ValueError(
            f"Unknown pet {pet!r}. Options: {[p.name for p in self.cfg.pets.values()]}"
        )

    # ------------------------------------------------------------------ tools

    def list_pets(self) -> dict[str, Any]:
        """List the pets in the household and their recurring care tasks."""
        return {
            "pets": [
                {
                    "key": p.key,
                    "name": p.name,
                    "species": p.species,
                    "care": [
                        {"task": c.key, "label": c.label, "every_days": c.every_days}
                        for c in p.care
                    ],
                }
                for p in self.cfg.pets.values()
            ]
        }

    def care_status(self, pet: str = "") -> dict[str, Any]:
        """Show what's due or overdue. Leave pet empty for the whole household.

        Args:
            pet: pet name or key; empty for everyone.
        """
        pets = [self._pet(pet)] if pet else list(self.cfg.pets.values())
        now = self.now()
        last = self._last_done()
        items = []
        for p in pets:
            for c in p.care:
                done = last.get((p.key, c.key))
                due = (done + timedelta(days=c.every_days)) if done else None
                overdue_days = (now - due).days if due and now > due else 0
                items.append(
                    {
                        "pet": p.name,
                        "task": c.key,
                        "label": c.label,
                        "last_done": done.isoformat() if done else None,
                        "next_due": due.date().isoformat() if due else "never logged",
                        "status": "never logged"
                        if not done
                        else ("overdue" if now > due else "ok"),
                        "days_overdue": overdue_days,
                    }
                )
        items.sort(
            key=lambda i: (
                {"overdue": 0, "never logged": 1, "ok": 2}[i["status"]],
                -i["days_overdue"],
            )
        )
        return {"as_of": now.isoformat(), "items": items}

    def log_care(self, pet: str, task: str, note: str = "") -> dict[str, Any]:
        """Record that a care task was done just now (e.g. heartworm pill, litter, nail trim).

        Args:
            pet: pet name or key.
            task: task key from list_pets.
            note: optional short note.
        """
        p = self._pet(pet)
        if task not in {c.key for c in p.care}:
            raise ValueError(f"{p.name} has no task {task!r}. Options: {[c.key for c in p.care]}")
        entry = {"pet": p.key, "task": task, "at": self.now().isoformat(), "note": note[:280]}
        with self._lock, self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return {"logged": entry}

    def notify_sitter(self, message: str) -> dict[str, Any]:
        """Send a message to the pet sitter. Requires human approval before it is delivered.

        Args:
            message: the message text (max 1000 characters).
        """
        if not message.strip():
            raise ValueError("Message is empty")
        payload = {
            "to": self.cfg.sitter.name,
            "text": message.strip()[:1000],
            "sent_at": self.now().isoformat(),
        }
        if self.cfg.sitter.channel == "webhook":
            url = os.environ.get(SITTER_WEBHOOK_ENV)
            if not url:
                raise RuntimeError(f"{SITTER_WEBHOOK_ENV} is not set")
            status = self.http.post_json(url, payload)
            return {"delivered_via": "webhook", "http_status": status}
        with self._lock, self.outbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        return {"delivered_via": "outbox", "path": str(self.outbox_path)}
