"""YouTube stages of a run: search harvest, screening, free enrichment, contact lookup.

Discovery costs one ScrapeCreators credit per search page. Channel statistics,
country and recent uploads come from the official YouTube Data API (free
quota), so screening and enrichment of YouTube channels cost no credits.
Contacts come only from the channel description (free). A TikTok handle in the
description of an accepted channel is resolved, linked to the channel and used
as a snowball seed.
"""

from __future__ import annotations

import re
import time

from . import filters, metrics, signals
from .sources import tiktok as tt
from .sources import youtube as yt

YT = yt.PLATFORM
TT = tt.PLATFORM


class YouTubeStage:
    """Mixin for :class:`scout.pipeline.Pipeline`; relies on its store, client and helpers."""

    youtube: yt.YouTubeData | None

    # ---- harvest ----------------------------------------------------------

    def yt_search_page(self, query: str, token: str | None,
                       mode: str = "yt_search") -> tuple[int, str | None, bool]:
        """Fetch one discovery page; returns ``(new_in_market, next_token, fetched)``.

        Modes: ``yt_search`` (mixed results), ``yt_channels`` (channel results),
        ``yt_shorts`` (Shorts search), ``yt_shorts_tag`` (Shorts by hashtag) and
        ``yt_api_shorts`` (official Shorts-length search, free quota). Shorts
        results carry no channel, so their channels come from the free API.
        """
        body: dict | None = {}
        if mode == "yt_api_shorts":
            if not self.youtube or not self.youtube.available() or token:
                return 0, None, False
            try:
                hits = self.youtube.search_short_channels(query, self.market.code,
                                                          self.market.languages[0])
            except yt.YouTubeApiError as exc:
                self.emit("error", f"YouTube API: {exc}")
                return 0, None, False
            channels = self._channels_from_hits(hits)
        else:
            if mode == "yt_shorts_tag":
                endpoint = "/v1/youtube/search/hashtag"
                params = {"hashtag": query.lstrip("#").replace(" ", ""), "type": "shorts"}
            else:
                endpoint = "/v1/youtube/search"
                params = {"query": query, "region": self.market.code}
                if mode in ("yt_channels", "yt_shorts"):
                    params["type"] = {"yt_channels": "channels", "yt_shorts": "shorts"}[mode]
            if token:
                params["continuationToken"] = token
            body, _ = self._call(self.client.get, endpoint, params, max_age=self.s.ttl_search)
            if body is None:
                return 0, None, False
            channels = yt.parse_search(body, query)
            shorts = yt.parse_shorts(body)
            if shorts and self.youtube and self.youtube.available():
                try:
                    owners = self.youtube.video_channels([s["video_id"] for s in shorts])
                except yt.YouTubeApiError:
                    owners = {}
                channels += self._channels_from_hits(
                    [{"video_id": s["video_id"], "uid": owners[s["video_id"]]["uid"],
                      "nickname": None, "title": s["title"], "views": s["views"],
                      "published": None} for s in shorts if s["video_id"] in owners])
        new_ids = []
        for ch in channels:
            existing = self.store.get_creator(YT, ch["uid"])
            self.store.upsert_creator(YT, ch["uid"], run_id=self.run_id, handle=ch["handle"],
                                      nickname=ch["nickname"])
            self.store.upsert_videos(YT, [{
                "video_id": h["video_id"] or f"{ch['uid']}:{h['title'][:40]}", "uid": ch["uid"],
                "caption": h["title"], "play_count": h["views"],
                "create_time": yt._ts(h["published"]) if h.get("published") else None,
                "source": f"{mode}:{query}"} for h in ch["hits"]])
            self.store.add_edge(YT, ch["uid"], mode, query, run_id=self.run_id)
            self.note_group(ch["uid"])
            if ch["uid"] not in self.seen_this_run:
                new_ids.append(ch["uid"])
                if existing is None:
                    self.funnel["new"] += 1
        self._yt_fetch_channels(new_ids)
        new_in_market = 0
        for uid in new_ids:
            self.seen_this_run.add(uid)
            self.funnel["seen"] += 1
            self.funnel["yt_channels_seen"] = self.funnel.get("yt_channels_seen", 0) + 1
            screening = self.store.get_screening(self.market.code, YT, uid)
            if screening and screening["status"] not in ("pending", "other_market"):
                self.funnel["already_known"] += 1
                continue
            bucket = self.yt_screen(uid)
            self.funnel[f"bucket_{bucket}"] += 1
            if bucket != filters.OTHER:
                new_in_market += 1
        self.funnel[f"{mode}_pages"] = self.funnel.get(f"{mode}_pages", 0) + 1
        self.emit("harvest", f"{mode} '{query}' ({self.market.code}): {len(channels)} channels, "
                             f"{new_in_market} new in-market")
        return new_in_market, (body or {}).get("continuationToken"), True

    def _channels_from_hits(self, hits: list[dict]) -> list[dict]:
        """Group video hits (with a known channel id) into parse_search-style channel entries."""
        by: dict[str, dict] = {}
        for h in hits:
            entry = by.setdefault(h["uid"], {"uid": h["uid"], "handle": None,
                                             "nickname": h.get("nickname"), "hits": []})
            entry["hits"].append({"kind": "shorts", "title": h["title"], "views": h.get("views"),
                                  "video_id": h["video_id"], "published": h.get("published")})
        return list(by.values())

    def _yt_fetch_channels(self, ids: list[str]) -> None:
        if not ids or not self.youtube or not self.youtube.available():
            return
        try:
            found = self.youtube.channels(ids)
        except yt.YouTubeApiError as exc:
            self.emit("error", f"YouTube API: {exc}")
            return
        for uid, ch in found.items():
            self.store.upsert_creator(
                YT, uid, handle=ch["handle"], nickname=ch["nickname"], region=ch["country"],
                region_source="channel" if ch["country"] else None, language=ch["language"],
                followers=ch["followers"], videos=ch["videos"], hearts=ch["hearts"],
                bio=(ch["bio"] + ("\n" + ch["keywords"] if ch["keywords"] else "")).strip(),
                emails=signals.extract_emails(ch["bio"]) or None,
                links=signals.extract_links(ch["bio"]) or None)

    # ---- screening --------------------------------------------------------

    def yt_screen(self, uid: str) -> str:
        """Hard filters for a channel: self-declared country, text signals, subscriber band."""
        c = self.store.get_creator(YT, uid) or {}
        code = self.market.code
        vids = self.store.videos_for(YT, uid, limit=20)
        # The channel country is self-declared: a match is strong, a different
        # country is treated like a registration elsewhere, empty is unknown.
        res = filters.market_bucket(self.market, region=c.get("region"),
                                    region_source="lookup" if c.get("region") else None,
                                    language=c.get("language"), bio=c.get("bio"), videos=vids)
        if res.bucket == filters.OTHER:
            self.store.set_screening(code, YT, uid, "other_market",
                                     reason=f"other market ({c.get('region') or 'unknown'})",
                                     bucket=res.bucket, evidence=res.evidence, run_id=self.run_id)
            return res.bucket
        lo, hi = self.s.band(YT)
        reason = filters.band_reason(c.get("followers"), lo, hi)
        if reason:
            self.funnel["filtered_band"] += 1
            self.store.set_screening(code, YT, uid, "filtered", reason=reason, bucket=res.bucket,
                                     evidence=res.evidence, run_id=self.run_id)
            return res.bucket
        self.store.set_screening(code, YT, uid, "pending", bucket=res.bucket,
                                 evidence=res.evidence, run_id=self.run_id)
        return res.bucket

    # ---- enrichment -------------------------------------------------------

    def yt_enrich_one(self, uid: str) -> bool:
        """Recent long videos and Shorts from the free API; metrics per format."""
        code = self.market.code
        if not self.youtube or not self.youtube.available():
            self.skipped_uids.add(uid)
            return False
        c = self.store.get_creator(YT, uid) or {}
        try:
            long_ids, long_total = self.youtube.uploads(uid, "long", 30)
            short_ids, short_total = self.youtube.uploads(uid, "shorts", 30)
            long_v = self.youtube.videos(long_ids) if long_ids else []
            short_v = self.youtube.videos(short_ids) if short_ids else []
        except yt.YouTubeApiError as exc:
            self.emit("error", f"YouTube API: {exc}")
            self.skipped_uids.add(uid)
            return False
        self.funnel["yt_enriched"] = self.funnel.get("yt_enriched", 0) + 1
        for v in long_v + short_v:
            v["uid"] = uid
            v["source"] = "yt_shorts" if v in short_v else "yt_long"
            v["is_ad"] = signals.is_ad_caption(v["caption"], self.market)
        self.store.upsert_videos(YT, long_v + short_v)
        now = self.now or time.time()
        m = metrics.compute(long_v + short_v, c.get("followers"), now=now)
        m["long"] = metrics.view_summary(long_v, now) if long_v else None
        m["shorts"] = metrics.view_summary(short_v, now) if short_v else None
        m["long_total"], m["shorts_total"] = long_total, short_total
        m["format"] = yt.format_label(short_total, long_total)
        # Headline views follow the format the channel posts most recently.
        latest_long = max((v["create_time"] or 0 for v in long_v), default=0)
        latest_short = max((v["create_time"] or 0 for v in short_v), default=0)
        m["views"] = m["long"] if latest_long >= latest_short and m["long"] else (m["shorts"] or m["long"])
        captions = [v["caption"] for v in long_v + short_v]
        m["sponsors"] = sorted({h for v in long_v + short_v
                                for h in signals.sponsor_mentions(v["caption"], self.market)})
        m["competitors"] = signals.competitor_hits(captions + [c.get("bio") or ""])
        m["old_hardware"] = metrics.has_old_hardware(captions + [c.get("bio") or ""])
        self.store.put_metrics(YT, uid, m)
        reason = filters.activity_reason(m["days_since_last_post"], self.s.max_days_since_post)
        if reason:
            self.funnel["filtered_inactive"] += 1
            self.store.set_screening(code, YT, uid, "filtered", reason=reason, run_id=self.run_id)
            return False
        if self.yt_screen(uid) == filters.OTHER or \
                self.store.get_screening(code, YT, uid)["status"] != "pending":
            return False
        self.store.set_screening(code, YT, uid, "needs_judgment", run_id=self.run_id)
        self.store.add_run_member(self.run_id, code, YT, uid, "needs_judgment")
        self.funnel["qualified"] += 1
        self.emit("enrich", f"enriched YouTube {c.get('nickname')} ({c.get('followers')} subscribers)")
        return True

    # ---- contacts and cross-platform links --------------------------------

    def yt_after_accept(self, uid: str) -> None:
        """Follow a TikTok link from the (free) channel description to link and snowball it.

        Contact details come only from free sources (the channel description);
        no credits are spent on contact enrichment.
        """
        c = self.store.get_creator(YT, uid) or {}
        links = dict(c.get("links") or {})
        m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", c.get("bio") or "", re.I)
        if m:
            links["tiktok"] = m.group(1)
            self.store.upsert_creator(YT, uid, links=links)
        tiktok_handle = links.get("tiktok")
        if tiktok_handle:
            self.link_tiktok_handle(tiktok_handle, (YT, uid), f"YouTube channel links @{tiktok_handle}")

    def link_tiktok_handle(self, handle: str, other: tuple[str, str], evidence: str) -> None:
        """Resolve a TikTok handle (1 credit if not cached), link it and screen it."""
        known = self.store.get_creator_by_handle(TT, handle)
        if known:
            self.store.link_identities((TT, known["uid"]), other, evidence)
            return
        body, _ = self._call(self.client.tiktok_profile, handle, max_age=self.s.ttl_profile)
        if body is None:
            return
        p = tt.parse_profile(body)
        if not p["uid"]:
            return
        acc = {k: p.get(k) for k in ("uid", "handle", "sec_uid", "nickname", "language", "followers",
                                     "following", "videos", "hearts", "bio", "is_private")}
        self.observe(acc, "youtube_link", other[1])
        self.store.link_identities((TT, p["uid"]), other, evidence)

    def link_youtube_channel(self, channel_id: str, other: tuple[str, str], evidence: str) -> None:
        """Link a YouTube channel id seen on a TikTok account; fetch it from the free API."""
        if not channel_id.startswith("UC"):
            return
        if self.store.get_creator(YT, channel_id) is None:
            self._yt_fetch_channels([channel_id])
            if self.store.get_creator(YT, channel_id) is None:
                return
            self.store.add_edge(YT, channel_id, "tiktok_link",
                                (self.store.get_creator(*other) or {}).get("handle") or other[1],
                                run_id=self.run_id)
            if channel_id not in self.seen_this_run:
                self.seen_this_run.add(channel_id)
                self.funnel["seen"] += 1
                bucket = self.yt_screen(channel_id)
                self.funnel[f"bucket_{bucket}"] += 1
        self.store.link_identities((YT, channel_id), other, evidence)
