"""Performance metrics from recent videos, price estimates and deal suggestions."""

from __future__ import annotations

import re
import statistics
import time

# Flat-fee pricing per 1,000 expected views, with a floor for nano creators.
CPM_LOW, CPM_HIGH = 3.0, 7.0
FLOOR_LOW, FLOOR_HIGH = 50, 100

OLD_HARDWARE_RE = re.compile(
    r"\b(gtx\s?(?:9\d0|10[5-8]0|16[56]0)|rx\s?(?:4[78]0|5[5-9]0)|i[357][-\s]?[2-7]\d{3}|"
    r"ryzen\s?[35]\s?(?:1|2)\d{3})\b", re.I)


def compute(videos: list[dict], followers: int | None, now: float | None = None) -> dict:
    """Summarise recent videos. Pinned videos count for totals but not recency."""
    now = now or time.time()
    vids = [v for v in videos if v.get("play_count") is not None]
    if not vids:
        return {"n_videos": 0, "median_views": None, "er_views": None, "er_followers": None,
                "posts_per_week": None, "last_post_at": None, "days_since_last_post": None,
                "ad_count": 0}
    views = [v["play_count"] for v in vids]
    engagements = [(v.get("digg_count") or 0) + (v.get("comment_count") or 0)
                   + (v.get("share_count") or 0) for v in vids]
    er_views = statistics.median(e / p for e, p in zip(engagements, views) if p) \
        if any(views) else None
    er_followers = statistics.median(engagements) / followers if followers else None

    recent = [v for v in vids if not v.get("is_pinned") and v.get("create_time")]
    times = sorted((v["create_time"] for v in recent), reverse=True)
    last = times[0] if times else None
    ppw = None
    if len(times) >= 2 and times[0] > times[-1]:
        weeks = (times[0] - times[-1]) / (7 * 86400)
        ppw = round((len(times) - 1) / weeks, 2) if weeks > 0 else None
    return {
        "n_videos": len(vids),
        "median_views": int(statistics.median(views)),
        "er_views": round(er_views, 4) if er_views is not None else None,
        "er_followers": round(er_followers, 4) if er_followers is not None else None,
        "posts_per_week": ppw,
        "last_post_at": last,
        "days_since_last_post": round((now - last) / 86400, 1) if last else None,
        "ad_count": sum(1 for v in vids if v.get("is_ad")),
    }


def price_estimate(median_views: int | None) -> tuple[int, int] | None:
    """Estimated flat fee (EUR) for one sponsored video."""
    if median_views is None:
        return None
    low = max(median_views * CPM_LOW / 1000, FLOOR_LOW)
    high = max(median_views * CPM_HIGH / 1000, FLOOR_HIGH)
    return int(round(low, -1)), int(round(high, -1))


def has_old_hardware(texts: list[str]) -> str | None:
    for t in texts:
        m = OLD_HARDWARE_RE.search(t or "")
        if m:
            return m.group(0)
    return None


def suggest_deal(followers: int | None, old_hardware: str | None = None) -> str:
    if old_hardware:
        return f"Upgrade for content (shows {old_hardware}) + affiliate code 5-8%"
    if followers is not None and followers < 10_000:
        return "Gifting (loaner or discounted PC) + affiliate code 5-8%"
    return "Paid integration + affiliate code 5-8%"
