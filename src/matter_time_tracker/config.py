"""Configuration module. Reads settings from .env and locates the matters registry."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root is two levels up from this file (src/matter_time_tracker/config.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")


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
