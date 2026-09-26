"""Cheap text signals from bios and captions: market hints, contacts, ads, sponsors."""

from __future__ import annotations

import re
import unicodedata

from .market import COMPETITORS, Market

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MENTION_RE = re.compile(r"@([A-Za-z0-9._]{2,30})")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+(?:/[^\s]*)?")

LINK_PATTERNS = {
    "instagram": re.compile(r"(?:instagram\.com/|\big[:\s]+@?|\binsta[:\s]+@?)([A-Za-z0-9._]{2,30})", re.I),
    "youtube": re.compile(r"youtube\.com/(?:@|c/|channel/|user/)?([A-Za-z0-9._\-]{2,60})|\byt[:\s]+@?([A-Za-z0-9._\-]{2,40})", re.I),
    "twitch": re.compile(r"twitch\.tv/([A-Za-z0-9_]{2,30})|\btwitch[:\s]+@?([A-Za-z0-9_]{2,30})", re.I),
    "discord": re.compile(r"(discord\.gg/[A-Za-z0-9\-]+|discord\.com/invite/[A-Za-z0-9\-]+)", re.I),
}

# Handle or bio fragments that indicate a shop, brand or retailer account.
BUSINESS_WORDS = ["shop", "store", "kauppa", "pood", "official", "virallinen", "ametlik",
                  "gmbh", "oy", "oü", "ltd", "osta", "order now", "tellimine",
                  "webshop", "verkkokauppa", "e-pood", "online store"]
TLD_HANDLE_RE = re.compile(r"\.(fi|ee|de|com|se|no|dk|pl|lv|lt|eu)$", re.I)


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text or "").lower()


def extract_emails(*texts: str | None) -> list[str]:
    seen: dict[str, None] = {}
    for text in texts:
        for m in EMAIL_RE.findall(text or ""):
            seen.setdefault(m.strip(".").lower(), None)
    return list(seen)


def extract_links(text: str | None, ins_id: str | None = None,
                  youtube_channel_id: str | None = None) -> dict[str, str]:
    links: dict[str, str] = {}
    for name, pattern in LINK_PATTERNS.items():
        m = pattern.search(text or "")
        if m:
            links[name] = next(g for g in m.groups() if g)
    if ins_id:
        links["instagram"] = ins_id
    if youtube_channel_id:
        links.setdefault("youtube", youtube_channel_id)
    return links


def market_signals(text: str | None, market: Market) -> list[str]:
    """Return human-readable evidence that ``text`` relates to ``market``."""
    t = _norm(text or "")
    if not t:
        return []
    found: list[str] = []
    for flag in market.flags:
        if flag in (text or ""):
            found.append(f"flag {flag}")
    for tld in market.tlds:
        if re.search(re.escape(tld.lower()) + r"(?:\b|/|$)", t):
            found.append(f"domain {tld}")
    for city in market.cities:
        if re.search(r"\b" + re.escape(_norm(city)) + r"\b", t):
            found.append(f"city {city}")
    for word in market.words:
        if re.search(r"\b" + re.escape(_norm(word)), t):
            found.append(f"word '{word}'")
    return found


def is_ad_caption(caption: str | None, market: Market) -> bool:
    t = _norm(caption or "")
    return any(_norm(m) in t for m in market.all_ad_markers)


def sponsor_mentions(caption: str | None, market: Market) -> list[str]:
    """Handles mentioned in a caption that carries an ad marker."""
    if not is_ad_caption(caption, market):
        return []
    return [m.lower().rstrip(".") for m in MENTION_RE.findall(caption or "")]


def competitor_hits(texts: list[str]) -> list[str]:
    """Competitor brand names mentioned anywhere in ``texts``."""
    blob = _norm(" ".join(t for t in texts if t))
    hits = []
    for brand, needles in COMPETITORS.items():
        if any(n in blob for n in needles):
            hits.append(brand)
    return hits


def business_hints(handle: str | None, nickname: str | None, bio: str | None) -> list[str]:
    """Signals that an account is a shop, retailer or brand rather than a creator."""
    hints = []
    h = _norm(handle or "")
    if TLD_HANDLE_RE.search(h):
        hints.append(f"handle looks like a domain ({handle})")
    blob = f"{h} {_norm(nickname or '')} {_norm(bio or '')}"
    for word in BUSINESS_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", blob):
            hints.append(f"'{word}'")
    return hints
