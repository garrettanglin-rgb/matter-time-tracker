"""Matters registry — loads and queries legal matters from the JSON file."""

import json
from dataclasses import dataclass
from pathlib import Path

from matter_time_tracker.config import get_matters_registry_path


@dataclass
class Matter:
    """A single legal matter entry."""

    matter_id: str
    matter_name: str
    client_name: str
    contact_emails: list[str]
    subject_keywords: list[str]

    def matches_subject(self, subject: str) -> bool:
        """Return True if any keyword appears in the given subject line."""
        lower = subject.lower()
        return any(kw.lower() in lower for kw in self.subject_keywords)

    def matches_email(self, email: str) -> bool:
        """Return True if the email matches any contact email for this matter."""
        lower = email.lower()
        return any(e.lower() == lower for e in self.contact_emails)


def load_matters(path: Path | None = None) -> list[Matter]:
    """Load all matters from the registry JSON file."""
    path = path or get_matters_registry_path()
    with open(path) as f:
        data = json.load(f)

    matters = []
    for entry in data["matters"]:
        matters.append(
            Matter(
                matter_id=entry["matter_id"],
                matter_name=entry["matter_name"],
                client_name=entry["client_name"],
                contact_emails=entry["contact_emails"],
                subject_keywords=entry["subject_keywords"],
            )
        )
    return matters


def find_matter_by_id(matter_id: str, matters: list[Matter] | None = None) -> Matter | None:
    """Look up a single matter by its identifier."""
    matters = matters or load_matters()
    for m in matters:
        if m.matter_id == matter_id:
            return m
    return None
