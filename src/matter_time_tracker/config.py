"""Configuration module. Reads settings from .env and locates the matters registry."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Project root is two levels up from this file (src/matter_time_tracker/config.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Default email time-estimation thresholds (hours per email by word count).
# Each entry is (max_words_exclusive, hours).  The last entry acts as the
# catch-all for anything above the previous threshold.
# Override via the EMAIL_TIME_THRESHOLDS env var as a JSON array of arrays,
# e.g. '[[100, 0.1], [300, 0.2], [null, 0.3]]'
# ---------------------------------------------------------------------------
_DEFAULT_EMAIL_THRESHOLDS: list[tuple[int | None, float]] = [
    (100, 0.1),   # under 100 words
    (300, 0.2),   # 100–300 words
    (None, 0.3),  # over 300 words
]


def get_anthropic_api_key() -> str:
    """Return the Anthropic API key from the environment."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file."
        )
    return key


def get_matters_registry_path() -> Path:
    """Return the path to the matters registry JSON file.

    Defaults to matters_registry.json in the project root unless
    MATTERS_REGISTRY_PATH is set in the environment.
    """
    custom = os.getenv("MATTERS_REGISTRY_PATH")
    if custom:
        return Path(custom).resolve()
    return PROJECT_ROOT / "matters_registry.json"


def get_output_dir() -> Path:
    """Return the directory where exported reports are saved.

    Defaults to an 'output' folder in the project root unless
    OUTPUT_DIR is set in the environment.
    """
    custom = os.getenv("OUTPUT_DIR")
    path = Path(custom).resolve() if custom else PROJECT_ROOT / "output"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_email_time_thresholds() -> list[tuple[int | None, float]]:
    """Return the word-count → hours thresholds for estimating email time.

    Each tuple is ``(max_words_exclusive | None, hours)``.  Evaluate in order;
    the first entry whose word-count ceiling is *None* or exceeds the email's
    word count determines the estimated time.
    """
    raw = os.getenv("EMAIL_TIME_THRESHOLDS")
    if not raw:
        return list(_DEFAULT_EMAIL_THRESHOLDS)
    parsed = json.loads(raw)
    return [(int(w) if w is not None else None, float(h)) for w, h in parsed]


def estimate_email_time(word_count: int) -> float:
    """Return estimated hours for an email with *word_count* words."""
    for ceiling, hours in get_email_time_thresholds():
        if ceiling is None or word_count < ceiling:
            return hours
    # Fallback (should not happen with a well-formed config)
    return get_email_time_thresholds()[-1][1]
