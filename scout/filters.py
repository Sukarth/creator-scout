"""Hard, deterministic filters: market bucket, follower band, privacy, activity.

Market buckets:
- ``sure``: the account's registration region equals the market.
- ``unsure``: region missing or different, but language, caption language,
  video region or bio/caption text points to the market. Resolved by the
  dedicated region lookup and, failing that, by the fit judge.
- ``other``: no link to the market. Kept in the pool under its own region.
"""

from __future__ import annotations

from dataclasses import dataclass

from .market import Market
from .signals import market_signals

SURE, UNSURE, OTHER = "sure", "unsure", "other"


@dataclass
class BucketResult:
    bucket: str
    evidence: list[str]


def market_bucket(market: Market, *, region: str | None, region_source: str | None,
                  language: str | None, bio: str | None,
                  videos: list[dict] | None = None) -> BucketResult:
    videos = videos or []
    code = market.code
    region = (region or "").upper() or None

    # Languages widely spoken outside the market (Russian for Estonia) are noted
    # but never count as evidence on their own.
    shared = {l.lower() for l in market.shared_languages}
    own = [l for l in market.languages if l not in shared]
    evidence: list[str] = []
    notes: list[str] = []
    if language and language.lower() in market.languages:
        (notes if language.lower() in shared else evidence).append(f"app language {language}")
    cap_langs = [(v.get("caption_language") or "").lower() for v in videos]
    own_caps = [l for l in cap_langs if l in own]
    shared_caps = [l for l in cap_langs if l in shared]
    if own_caps:
        evidence.append(f"{len(own_caps)} caption(s) in {own_caps[0]}")
    if shared_caps:
        notes.append(f"{len(shared_caps)} caption(s) in {shared_caps[0]}")
    vid_regions = [v.get("region") for v in videos if (v.get("region") or "").upper() == code]
    if vid_regions:
        evidence.append(f"{len(vid_regions)} video(s) posted from {code}")
    evidence += [f"bio {s}" for s in market_signals(bio, market)]
    for v in videos[:10]:
        for s in market_signals(v.get("caption"), market):
            tag = f"caption {s}"
            if tag not in evidence:
                evidence.append(tag)

    if region == code:
        label = "registered region" + (" (lookup)" if region_source == "lookup" else "")
        return BucketResult(SURE, [f"{label} {code}"] + evidence + notes)
    if region and region_source == "lookup":
        # The dedicated lookup is authoritative for registration country. Keep
        # the account open only when content strongly points to this market.
        strong = [e for e in evidence if not e.startswith("app language")]
        if len(strong) >= 2:
            return BucketResult(UNSURE, [f"registered region {region} (lookup)"] + strong)
        return BucketResult(OTHER, [f"registered region {region} (lookup)"])
    if evidence:
        prefix = [f"inline region {region}"] if region else ["region unknown"]
        return BucketResult(UNSURE, prefix + evidence)
    return BucketResult(OTHER, [f"region {region}" if region else "region unknown, no market signals"])


# Creators up to this multiple of the upper limit are kept and flagged
# "above typical range" rather than dropped.
SOFT_CAP_FACTOR = 1.5


def band_reason(followers: int | None, band_min: int, band_max: int,
                soft_factor: float = SOFT_CAP_FACTOR) -> str | None:
    """Return a filter reason when ``followers`` is outside the (soft) band, else None."""
    if followers is None:
        return None
    if followers < band_min:
        return f"below band ({followers} < {band_min})"
    if followers > band_max * soft_factor:
        return f"above band ({followers} > {int(band_max * soft_factor)})"
    return None


def above_typical(followers: int | None, band_max: int) -> bool:
    return followers is not None and followers > band_max


def activity_reason(days_since_last_post: float | None, max_days: int) -> str | None:
    if days_since_last_post is None:
        return "no videos found"
    if days_since_last_post > max_days:
        return f"inactive ({int(days_since_last_post)} days since last post)"
    return None
