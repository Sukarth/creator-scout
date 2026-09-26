"""Market definitions loaded from ``scout/markets/*.yaml``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import MARKETS_DIR

# Brands whose sponsorship of a creator conflicts with the client (direct
# refurbished competitor) or signals a competing new-PC retailer. Matched
# case-insensitively against sponsor handles and caption text.
COMPETITORS: dict[str, list[str]] = {
    "Nuvoo": ["nuvoo", "nuvoosuomi", "nuvoo.fi"],
    "Jimm's PC": ["jimmspc", "jimm's", "jimms"],
    "Gigantti": ["gigantti"],
    "Elgiganten": ["elgiganten"],
}

# Ad markers that are not language-specific.
GLOBAL_AD_MARKERS = ["#ad", "#sponsored", "#advert", "#partner", "sponsored", "paid partnership",
                     "#mainos", "#reklaam", "#werbung", "#anzeige"]


# Game titles and PC terms shared by all markets. Used only to order candidates.
GLOBAL_GAMING_TERMS = [
    "gaming", "gamer", "gameplay", "twitch", "stream", "streamer", "esports", "pc build", "pcbuild",
    "setup", "rtx", "gpu", "ryzen", "intel", "nvidia", "cs2", "csgo", "counter-strike", "fortnite",
    "minecraft", "valorant", "roblox", "gta", "league of legends", "apex", "rocket league",
    "warzone", "call of duty", "overwatch", "dota", "rust", "fifa", "eafc", "playstation",
    "xbox", "nintendo", "steam", "discord",
]


@dataclass
class Market:
    code: str
    name: str
    languages: list[str]
    currency: str = "EUR"
    # Market languages that are also widely spoken elsewhere; alone they are
    # not evidence that an account belongs to this market.
    shared_languages: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    tlds: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    seed_hashtags: list[str] = field(default_factory=list)
    global_hashtags: list[str] = field(default_factory=list)
    general_hashtags: list[str] = field(default_factory=list)
    seed_keywords: list[str] = field(default_factory=list)
    gaming_terms: list[str] = field(default_factory=list)
    retailer_seeds: list[dict] = field(default_factory=list)
    ad_markers: list[str] = field(default_factory=list)

    @property
    def all_ad_markers(self) -> list[str]:
        return GLOBAL_AD_MARKERS + self.ad_markers

    @property
    def all_gaming_terms(self) -> list[str]:
        return GLOBAL_GAMING_TERMS + self.gaming_terms


def load_market(code: str, directory: Path = MARKETS_DIR) -> Market:
    path = directory / f"{code.lower()}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no market config for '{code}' ({path})")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    signals = raw.get("market_signals") or {}
    return Market(
        code=raw["country_code"].upper(),
        name=raw.get("name", raw["country_code"]),
        languages=[l.lower() for l in raw.get("languages", [])],
        shared_languages=[l.lower() for l in raw.get("shared_languages", [])],
        currency=raw.get("currency", "EUR"),
        flags=signals.get("flags", []),
        tlds=signals.get("tlds", []),
        cities=signals.get("cities", []),
        words=signals.get("words", []),
        seed_hashtags=[h.lstrip("#") for h in raw.get("seed_hashtags", [])],
        global_hashtags=[h.lstrip("#") for h in raw.get("global_hashtags", [])],
        general_hashtags=[h.lstrip("#") for h in raw.get("general_hashtags", [])],
        seed_keywords=raw.get("seed_keywords", []),
        gaming_terms=raw.get("gaming_terms", []),
        retailer_seeds=raw.get("retailer_seeds", []),
        ad_markers=raw.get("ad_markers", []),
    )


def list_markets(directory: Path = MARKETS_DIR) -> list[Market]:
    return [load_market(p.stem, directory) for p in sorted(directory.glob("*.yaml"))]


def supported_codes(directory: Path = MARKETS_DIR) -> set[str]:
    return {p.stem.upper() for p in directory.glob("*.yaml")}
