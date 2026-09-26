"""Runtime configuration: paths, environment secrets and pipeline defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
MARKETS_DIR = PACKAGE_DIR / "markets"


def load_dotenv(path: Path | None = None) -> None:
    """Populate ``os.environ`` from a ``.env`` file without overriding existing values."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def data_dir() -> Path:
    path = Path(os.environ.get("SCOUT_DATA_DIR", ROOT / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return Path(os.environ.get("SCOUT_DB", data_dir() / "scout.db"))


def exports_dir() -> Path:
    path = Path(os.environ.get("SCOUT_EXPORTS_DIR", ROOT / "exports"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def sc_key() -> str | None:
    return os.environ.get("SC_KEY") or None


@dataclass
class RunSettings:
    """Tunable limits for one pipeline run."""

    band_min: int = 1_000
    band_max: int = 100_000
    target: int = 40
    budget: int = 300
    # Harvest
    max_pages_per_hashtag: int = 5
    max_pages_per_keyword: int = 3
    stop_after_dry_pages: int = 2
    min_new_in_market_per_page: int = 2
    keyword_date_posted: str = "last-6-months"
    # Enrichment
    videos_for_metrics: int = 20
    max_days_since_post: int = 60
    # Snowball
    max_pages_per_seed: int = 5
    max_snowball_rounds: int = 3
    # Cache TTLs in seconds
    ttl_profile: int = 7 * 24 * 3600
    ttl_search: int = 24 * 3600
    extra: dict = field(default_factory=dict)
