"""YouTube: ScrapeCreators search/channel parsing and the official Data API for statistics.

Discovery uses ScrapeCreators search (1 credit per page, ``region`` biases the
results). Channel statistics, country and recent uploads come from the
official YouTube Data API v3, which is free within its daily quota (1 unit per
list call, 50 ids per call):

- ``channels.list``: subscribers, self-declared country, description, keywords.
- ``playlistItems.list`` on ``UULF<id>`` (long videos) and ``UUSH<id>``
  (Shorts): recent uploads of each kind, so the two are measured separately.
- ``videos.list``: views, likes, comments, duration per video.

API responses are cached in the same store as ScrapeCreators responses.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any

import httpx

from ..store import Store
from .scrapecreators import cache_key

PLATFORM = "youtube"
API = "https://www.googleapis.com/youtube/v3"


class YouTubeApiError(RuntimeError):
    pass


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def iso_duration_seconds(value: str | None) -> int | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


class YouTubeData:
    """Cached client for the official YouTube Data API v3."""

    def __init__(self, store: Store, api_key: str | None = None, timeout: float = 30.0,
                 http: httpx.Client | None = None, offline: bool = False):
        self.store = store
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
        self.offline = offline or not self.api_key
        self._http = http or httpx.Client(timeout=timeout)
        self.calls = 0
        self.cache_hits = 0

    def available(self) -> bool:
        return not self.offline

    def _get(self, path: str, params: dict, max_age: float) -> dict:
        key = cache_key("ytapi:" + path, params)
        cached = self.store.cache_get(key, max_age)
        if cached is not None:
            self.cache_hits += 1
            return cached["response"]
        if self.offline:
            raise YouTubeApiError(f"offline and not cached: {path} {params}")
        resp = self._http.get(f"{API}/{path}", params={**params, "key": self.api_key})
        body = resp.json()
        self.calls += 1
        if resp.status_code >= 400:
            if resp.status_code == 404:
                self.store.cache_put(key, "ytapi:" + path, params, 404, body, 0)
            raise YouTubeApiError(f"HTTP {resp.status_code}: "
                                  f"{(body.get('error') or {}).get('message', '')[:120]}")
        self.store.cache_put(key, "ytapi:" + path, params, 200, body, 0)
        return body

    def channels(self, ids: list[str], max_age: float = 3 * 86400) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 50):
            chunk = sorted(set(ids[i:i + 50]))
            body = self._get("channels", {"part": "snippet,statistics,contentDetails,brandingSettings",
                                          "id": ",".join(chunk)}, max_age)
            for it in body.get("items") or []:
                out[it["id"]] = parse_api_channel(it)
        return out

    def uploads(self, channel_id: str, kind: str, max_results: int = 30,
                max_age: float = 86400) -> tuple[list[str], int]:
        """Recent video ids of one kind, ``long`` (UULF) or ``shorts`` (UUSH), and the total count."""
        prefix = {"long": "UULF", "shorts": "UUSH"}[kind]
        try:
            body = self._get("playlistItems", {"part": "contentDetails",
                                               "playlistId": prefix + channel_id[2:],
                                               "maxResults": max_results}, max_age)
        except YouTubeApiError:
            return [], 0  # playlist missing: the channel has no videos of this kind
        ids = [it["contentDetails"]["videoId"] for it in body.get("items") or []]
        return ids, int((body.get("pageInfo") or {}).get("totalResults") or len(ids))

    def video_channels(self, video_ids: list[str], max_age: float = 7 * 86400) -> dict[str, dict]:
        """Map video ids (e.g. Shorts from search, which carry no channel) to their channel."""
        out: dict[str, dict] = {}
        for v in self.videos(video_ids, max_age=max_age):
            if v.get("uid"):
                out[v["video_id"]] = v
        return out

    def search_short_channels(self, query: str, region: str, language: str,
                              max_results: int = 50, max_age: float = 3 * 86400) -> list[dict]:
        """Official ``search.list`` for Shorts-length videos (100 quota units per call)."""
        body = self._get("search", {"part": "snippet", "q": query, "type": "video",
                                    "videoDuration": "short", "regionCode": region,
                                    "relevanceLanguage": language, "maxResults": max_results},
                         max_age)
        return [{"video_id": it["id"]["videoId"], "uid": it["snippet"]["channelId"],
                 "nickname": it["snippet"].get("channelTitle"),
                 "title": it["snippet"].get("title") or "",
                 "published": it["snippet"].get("publishedAt")}
                for it in body.get("items") or [] if (it.get("id") or {}).get("videoId")]

    def videos(self, ids: list[str], max_age: float = 86400) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            body = self._get("videos", {"part": "snippet,statistics,contentDetails",
                                        "id": ",".join(ids[i:i + 50])}, max_age)
            out += [parse_api_video(v) for v in body.get("items") or []]
        return out


def parse_api_channel(it: dict) -> dict:
    sn = it.get("snippet") or {}
    st = it.get("statistics") or {}
    branding = (it.get("brandingSettings") or {}).get("channel") or {}
    return {
        "uid": it["id"],
        "handle": (sn.get("customUrl") or "").lstrip("@") or None,
        "nickname": sn.get("title"),
        "country": (sn.get("country") or "").upper() or None,
        "language": sn.get("defaultLanguage"),
        "followers": None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
        "videos": _int(st.get("videoCount")),
        "hearts": _int(st.get("viewCount")),  # total channel views
        "bio": sn.get("description") or "",
        "keywords": branding.get("keywords") or "",
    }


def parse_api_video(v: dict) -> dict:
    sn = v.get("snippet") or {}
    st = v.get("statistics") or {}
    return {
        "video_id": v["id"],
        "uid": sn.get("channelId"),
        "caption": (sn.get("title") or "") + (" | " + " ".join("#" + t for t in sn.get("tags", [])[:8])
                                              if sn.get("tags") else ""),
        "create_time": _ts(sn.get("publishedAt")),
        "play_count": _int(st.get("viewCount")),
        "digg_count": _int(st.get("likeCount")),
        "comment_count": _int(st.get("commentCount")),
        "share_count": 0,
        "duration": iso_duration_seconds((v.get("contentDetails") or {}).get("duration")),
        "caption_language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
        "region": None,
        "is_ad": False,
    }


def parse_search(body: dict, query: str) -> list[dict]:
    """Channels referenced by a ScrapeCreators search page, with the hit that found them.

    Shorts results carry no channel, so channels come from videos and playlists.
    """
    seen: dict[str, dict] = {}
    for kind in ("videos", "playlists", "channels"):
        for item in body.get(kind) or []:
            ch = item.get("channel") or (item if kind == "channels" else {})
            cid = ch.get("id") or ch.get("channelId")
            if not cid or not str(cid).startswith("UC"):
                continue
            entry = seen.setdefault(cid, {"uid": cid, "handle": (ch.get("handle") or "").lstrip("@") or None,
                                          "nickname": ch.get("title") or ch.get("name"), "hits": []})
            entry["hits"].append({"kind": kind, "title": item.get("title") or "",
                                  "views": item.get("viewCountInt"),
                                  "video_id": item.get("id") if kind == "videos" else None,
                                  "published": item.get("publishedTime")})
    return list(seen.values())


def parse_shorts(body: dict) -> list[dict]:
    """Shorts from a ScrapeCreators search or hashtag page: video id, title, views (no channel)."""
    return [{"video_id": s["id"], "title": s.get("title") or "", "views": s.get("viewCountInt")}
            for s in body.get("shorts") or [] if s.get("id")]


def format_label(shorts_total: int, long_total: int) -> str:
    if not shorts_total and not long_total:
        return ""
    if shorts_total >= 2 * max(long_total, 1) or (shorts_total and not long_total):
        return "Shorts-first"
    if long_total >= 2 * max(shorts_total, 1) or (long_total and not shorts_total):
        return "long-form"
    return "mixed"


def parse_sc_channel(body: dict) -> dict:
    """ScrapeCreators channel page: email and outbound links (Instagram, TikTok, ...)."""
    links: dict[str, str] = {}
    for link in body.get("links") or []:
        url = link if isinstance(link, str) else (link.get("url") or link.get("link") or "")
        for name, pat in (("tiktok", r"tiktok\.com/@([A-Za-z0-9._]+)"),
                          ("instagram", r"instagram\.com/([A-Za-z0-9._]+)"),
                          ("twitch", r"twitch\.tv/([A-Za-z0-9_]+)"),
                          ("discord", r"(discord\.(?:gg|com/invite)/[A-Za-z0-9-]+)")):
            m = re.search(pat, url or "", re.I)
            if m:
                links.setdefault(name, m.group(1))
    for k, v in body.items():
        if isinstance(v, str) and v.startswith("http"):
            for name, pat in (("instagram", r"instagram\.com/([A-Za-z0-9._]+)"),
                              ("tiktok", r"tiktok\.com/@([A-Za-z0-9._]+)")):
                m = re.search(pat, v, re.I)
                if m:
                    links.setdefault(name, m.group(1))
    return {"email": body.get("email") or None, "links": links,
            "country_name": body.get("country"), "description": body.get("description") or ""}
