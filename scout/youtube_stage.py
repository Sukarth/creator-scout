"""YouTube stages of a run: search harvest, screening, free enrichment, contact lookup.

Discovery costs one ScrapeCreators credit per search page. Channel statistics,
country and recent uploads come from the official YouTube Data API (free
quota), so screening and enrichment of YouTube channels cost no credits. Only
accepted channels get a ScrapeCreators channel lookup (1 credit) for email and
outbound links; a linked TikTok account is then resolved, linked to the channel
and, once in the market, used as a snowball seed.
"""

from __future__ import annotations

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

    def yt_search_page(self, query: str, token: str | None) -> tuple[int, str | None, bool]:
        """Fetch one search page; returns ``(new_in_market, next_token, fetched)``."""
        params = {"query": query, "region": self.market.code}
        if token:
            params["continuationToken"] = token
        body, _ = self._call(self.client.get, "/v1/youtube/search", params,
                             max_age=self.s.ttl_search)
        if body is None:
            return 0, None, False
        channels = yt.parse_search(body, query)
        new_ids = []
        for ch in channels:
            existing = self.store.get_creator(YT, ch["uid"])
            self.store.upsert_creator(YT, ch["uid"], run_id=self.run_id, handle=ch["handle"],
                                      nickname=ch["nickname"])
            self.store.upsert_videos(YT, [{
                "video_id": h["video_id"] or f"{ch['uid']}:{h['title'][:40]}", "uid": ch["uid"],
                "caption": h["title"], "play_count": h["views"],
                "create_time": yt._ts(h["published"]) if h.get("published") else None,
                "source": f"yt_search:{query}"} for h in ch["hits"]])
            self.store.add_edge(YT, ch["uid"], "yt_search", query, run_id=self.run_id)
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
        self.funnel["yt_search_pages"] = self.funnel.get("yt_search_pages", 0) + 1
        self.emit("harvest", f"YouTube '{query}' ({self.market.code}): {len(channels)} channels, "
                             f"{new_in_market} new in-market")
        return new_in_market, body.get("continuationToken"), True

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
            long_ids = self.youtube.uploads(uid, "long", 30)
            short_ids = self.youtube.uploads(uid, "shorts", 30)
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
        """Email and links for an accepted channel (1 credit); follow a TikTok link."""
        c = self.store.get_creator(YT, uid) or {}
        params = {"handle": c["handle"]} if c.get("handle") else {"channelId": uid}
        body, _ = self._call(self.client.get, "/v1/youtube/channel", params,
                             max_age=self.s.ttl_profile)
        if body is None:
            return
        info = yt.parse_sc_channel(body)
        emails = list(dict.fromkeys((c.get("emails") or []) + ([info["email"]] if info["email"] else [])
                                    + signals.extract_emails(info["description"])))
        links = dict(c.get("links") or {})
        links.update(info["links"])
        self.store.upsert_creator(YT, uid, emails=emails, links=links)
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
