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
        "views": view_summary(vids, now),
        "n_videos": len(vids),
        "median_views": int(statistics.median(views)),
        "er_views": round(er_views, 4) if er_views is not None else None,
        "er_followers": round(er_followers, 4) if er_followers is not None else None,
        "posts_per_week": ppw,
        "last_post_at": last,
        "days_since_last_post": round((now - last) / 86400, 1) if last else None,
        "ad_count": sum(1 for v in vids if v.get("is_ad")),
    }


def fmt_count(n: float | None) -> str:
    """Compact count in the style of a marketing sheet: 950, 1.2K, 18K, 1.1M."""
    if n is None:
        return ""
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{round(n / 1000)}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(int(round(n, -1) if n >= 100 else n))


def _percentile(values: list[int], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def view_summary(videos: list[dict], now: float | None = None, min_videos: int = 3) -> dict:
    """Average views over the last 30 days, or 90 when fewer than ``min_videos`` posts.

    When even 90 days holds too few posts, the most recent posts are used and the
    window says so. ``range`` spans the 20th to 80th percentile, the way a
    marketing sheet quotes "10K-30K". ``trend`` compares the newer half of the
    window with the older half (1.0 = flat).
    """
    now = now or time.time()
    vids = sorted((v for v in videos if v.get("play_count") is not None and v.get("create_time")),
                  key=lambda v: v["create_time"], reverse=True)
    if not vids:
        return {"avg_views": None, "window": "no videos", "n": 0, "range": "", "trend": None}
    window_label, chosen = None, []
    for days in (30, 90):
        chosen = [v for v in vids if now - v["create_time"] <= days * 86400]
        if len(chosen) >= min_videos:
            window_label = f"last {days} days"
            break
    if window_label is None:
        chosen = vids[:max(min_videos, len(chosen))]
        age = int((now - chosen[-1]["create_time"]) / 86400)
        window_label = f"last {len(chosen)} videos ({age} days)"
    views = [v["play_count"] for v in chosen]
    half = len(chosen) // 2
    trend = None
    if half >= 1:
        newer = statistics.mean(views[:half])
        older = statistics.mean(views[half:])
        trend = round(newer / older, 2) if older else None
    lo, hi = _percentile(views, 0.2), _percentile(views, 0.8)
    return {
        "avg_views": int(statistics.mean(views)),
        "median_views": int(statistics.median(views)),
        "window": window_label,
        "n": len(chosen),
        "range": f"{fmt_count(lo)}-{fmt_count(hi)}" if hi > lo else fmt_count(hi),
        "trend": trend,
    }


def trend_label(trend: float | None) -> str:
    if trend is None:
        return ""
    if trend >= 1.3:
        return f"rising (x{trend})"
    if trend <= 0.7:
        return f"falling (x{trend})"
    return f"stable (x{trend})"


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
