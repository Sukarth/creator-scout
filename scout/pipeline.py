"""Run orchestration: harvest, screen, pre-judge, enrich, judge, snowball, pitch.

Flow per run:
1. Harvest gaming-specific sources: local-language gaming hashtags, then global
   game hashtags through a proxy in the market, then keyword searches. General
   country tags are used only on request.
2. Repeat until the target is met, the budget is spent or seeds run dry:
   a. Pre-judge every new in-market, in-band candidate from free data (bio,
      nickname, harvested captions). Only a confident "no" is skipped.
   b. Enrich the "yes" and "unsure" candidates in priority order (paid).
   c. Judge enriched candidates for fit; accepted creators become seeds.
   d. Expand one following-list page of the most productive seeds.
3. Draft pitches for accepted creators.

With a deferred judge the run pauses at (a) or (c) and is resumed after an
external judge has written its answers.

Every account seen is stored with its market bucket and, when filtered, the
reason. Screening outcomes are per market, so accounts filtered for one market
stay available to others.
"""

from __future__ import annotations

import json
import time
import unicodedata
from typing import Any, Callable

from . import filters, metrics, signals
from .config import RunSettings
from .judge import DeferredJudge, judge_payload, pitch_payload, prejudge_payload
from .market import Market, supported_codes
from .sources import linkpages
from .sources import tiktok as tt
from .sources.scrapecreators import ApiError, BudgetExhausted, ScrapeCreators
from .store import Store
from .youtube_stage import YouTubeStage

PLATFORM = tt.PLATFORM
YOUTUBE = "youtube"

PENDING = "pending"
QUALIFIED = "needs_judgment"
ACCEPTED = "accepted"
MAYBE = "maybe"
REJECTED = "rejected"
FILTERED = "filtered"
OTHER_MARKET = "other_market"
BUSINESS = "business"
ERROR = "error"

ProgressFn = Callable[[dict], None]


class TimeLimit(BudgetExhausted):
    """The run's wall-clock deadline passed; stop fetching, keep what was found."""


class Paused(Exception):
    """The run is waiting for an external judge."""

    def __init__(self, status: str, count: int):
        super().__init__(status)
        self.status = status
        self.count = count


def keywords_meta_key(market: str) -> str:
    return f"keywords:{market}"


def _norm(text: str) -> str:
    """Lowercase letters and digits only, single-spaced.

    Quotes copied by an LLM often differ from the source in emoji variation
    selectors, quote marks or punctuation; comparing words avoids false misses.
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


class Pipeline(YouTubeStage):
    def __init__(self, store: Store, client: ScrapeCreators, market: Market,
                 settings: RunSettings, run_id: int, progress: ProgressFn | None = None,
                 fetch_link_pages: bool = True, now: float | None = None, judge=None,
                 llm=None, youtube=None):
        self.store = store
        self.youtube = youtube
        # Optional wall-clock limits (epoch seconds): no new fetches after
        # ``deadline``; judging of what was found stops at ``hard_deadline``.
        self.deadline: float | None = None
        self.hard_deadline: float | None = None
        self.harvest_spent = 0
        self._harvest = None
        self._plan_hashtags: list[str] | None = None
        self._plan_keywords: list[str] | None = None
        # Per-source-group yield, used to give productive sources more pages.
        self.group_stats: dict[str, dict] = {}
        self.group_of: dict[str, str] = {}
        self.current_group: str | None = None
        self.now = now
        self.client = client
        self.market = market
        self.s = settings
        self.run_id = run_id
        self.progress = progress or (lambda event: None)
        self.fetch_link_pages = fetch_link_pages
        self.judge = judge  # None: hard filters only
        self.llm = llm
        self.seen_this_run: set[str] = set()
        self.skipped_seeds: set[str] = set()
        self.skipped_uids: set[str] = set()
        self.judge_attempts: dict[str, int] = {}
        self.last_enrich_attempts = 0
        self.last_error_status: int | None = None
        self.supported = supported_codes()
        self.funnel: dict[str, int] = {
            "seen": 0, "new": 0, "already_known": 0,
            "bucket_sure": 0, "bucket_unsure": 0, "bucket_other": 0,
            "filtered_band": 0, "filtered_private": 0, "filtered_inactive": 0,
            "business": 0, "prejudged": 0, "prejudge_yes": 0, "prejudge_unsure": 0,
            "prejudge_no": 0, "region_lookups": 0, "profiles_fetched": 0, "videos_fetched": 0,
            "qualified": 0, "judged": 0, "accepted": 0, "maybe": 0, "rejected": 0,
            "judge_other_market": 0, "pitches": 0, "seeds_expanded": 0, "following_pages": 0,
            "hashtag_pages": 0, "keyword_pages": 0, "errors": 0,
        }
        self.status = "running"

    @property
    def judging(self) -> bool:
        return self.judge is not None

    # ---- helpers ------------------------------------------------------------

    def emit(self, stage: str, message: str, **extra: Any) -> None:
        self.progress({"stage": stage, "message": message, "credits_used": self.client.meter.used,
                       "budget": self.client.meter.budget, "funnel": dict(self.funnel), **extra})

    def count_status(self, *statuses: str) -> int:
        return len(self.store.screenings(self.market.code, statuses, run_id=self.run_id))

    def target_count(self) -> int:
        if self.judging:
            return self.count_status(ACCEPTED)
        return self.count_status(QUALIFIED, ACCEPTED, MAYBE)

    def target_met(self) -> bool:
        return self.target_count() >= self.s.target

    def _call(self, fn: Callable, *args: Any, **kwargs: Any) -> tuple[dict | None, int]:
        """Invoke a client method; return ``(body_or_None, credits_spent)``.

        ``BudgetExhausted`` propagates so the run can stop cleanly.
        """
        before = self.client.meter.used
        self.last_error_status = None
        if self.deadline and time.time() > self.deadline:
            raise TimeLimit("time limit reached")
        try:
            body = fn(*args, **kwargs)
        except ApiError as exc:
            self.funnel["errors"] += 1
            self.last_error_status = exc.status_code
            self.emit("error", str(exc))
            return None, self.client.meter.used - before
        return body, self.client.meter.used - before

    def _permanent_error(self) -> bool:
        """True when the last failure was a client error (e.g. account not found)."""
        return self.last_error_status is not None and 400 <= self.last_error_status < 500

    # ---- observation and screening ---------------------------------------

    def observe(self, acc: dict, kind: str, via: str, from_uid: str | None = None,
                video: dict | None = None) -> str | None:
        """Record an account seen in a result list and screen it.

        Returns the market bucket when the account is new to this run, else None.
        """
        uid = acc.get("uid")
        if not uid:
            return None
        existing = self.store.get_creator(PLATFORM, uid)
        fields = {k: acc.get(k) for k in ("handle", "sec_uid", "nickname", "language",
                                           "followers", "following", "videos", "hearts",
                                           "bio", "is_private")}
        if acc.get("region") and not (existing and existing.get("region_source") == "lookup"):
            fields["region"] = acc["region"]
            fields["region_source"] = "inline"
        links = signals.extract_links(acc.get("bio"), acc.get("ins_id"), acc.get("youtube_channel_id"))
        if links:
            merged = dict(existing["links"]) if existing else {}
            merged.update(links)
            fields["links"] = merged
        self.store.upsert_creator(PLATFORM, uid, run_id=self.run_id, **fields)
        self.store.add_observation(PLATFORM, uid, source=kind, followers=acc.get("followers"),
                                   following=acc.get("following"), videos=acc.get("videos"),
                                   hearts=acc.get("hearts"))
        if video:
            self.store.upsert_videos(PLATFORM, [video])
        self.store.add_edge(PLATFORM, uid, kind, via, from_uid=from_uid, run_id=self.run_id)

        if uid in self.seen_this_run:
            return None
        self.seen_this_run.add(uid)
        self.note_group(uid)
        self.funnel["seen"] += 1
        if existing is None:
            self.funnel["new"] += 1

        screening = self.store.get_screening(self.market.code, PLATFORM, uid)
        if screening and screening["status"] not in (PENDING, OTHER_MARKET):
            self.funnel["already_known"] += 1
            return None
        bucket = self.screen(uid, business_hint=acc.get("enterprise_reason"))
        self.funnel[f"bucket_{bucket}"] += 1
        # In-market TikTok accounts that link a YouTube channel: link and screen
        # the channel too (free API call).
        if bucket != filters.OTHER and (acc.get("youtube_channel_id") or "").startswith("UC") \
                and self.youtube is not None:
            self.link_youtube_channel(acc["youtube_channel_id"], (PLATFORM, uid),
                                      "TikTok profile links the YouTube channel")
        return bucket

    def screen(self, uid: str, business_hint: str | None = None) -> str:
        """Apply the hard filters that need no API call. Returns the bucket."""
        c = self.store.get_creator(PLATFORM, uid)
        code = self.market.code
        vids = self.store.videos_for(PLATFORM, uid, limit=20)
        res = filters.market_bucket(self.market, region=c.get("region"),
                                    region_source=c.get("region_source"),
                                    language=c.get("language"), bio=c.get("bio"), videos=vids)
        if res.bucket == filters.OTHER:
            region = c.get("region")
            self.store.set_screening(code, PLATFORM, uid, OTHER_MARKET,
                                     reason=f"other market ({region or 'unknown'})",
                                     bucket=res.bucket, evidence=res.evidence, run_id=self.run_id)
            self._prequeue_other_market(uid, region)
            return res.bucket
        reason = filters.band_reason(c.get("followers"), self.s.band_min, self.s.band_max)
        if reason:
            self.funnel["filtered_band"] += 1
            self.store.set_screening(code, PLATFORM, uid, FILTERED, reason=reason,
                                     bucket=res.bucket, evidence=res.evidence, run_id=self.run_id)
            return res.bucket
        if c.get("is_private"):
            self.funnel["filtered_private"] += 1
            self.store.set_screening(code, PLATFORM, uid, FILTERED, reason="private account",
                                     bucket=res.bucket, evidence=res.evidence, run_id=self.run_id)
            return res.bucket
        hints = signals.business_hints(c.get("handle"), c.get("nickname"), c.get("bio"))
        if business_hint:
            hints.insert(0, f"verified business ({business_hint})")
        if c.get("is_commerce") or c.get("is_organization"):
            hints.insert(0, "commerce/organization account")
        if hints and self._is_business(hints):
            self._mark_business(uid, c, "; ".join(hints), res)
            return res.bucket
        self.store.set_screening(code, PLATFORM, uid, PENDING, reason=None, bucket=res.bucket,
                                 evidence=res.evidence, run_id=self.run_id)
        return res.bucket

    @staticmethod
    def _is_business(hints: list[str]) -> bool:
        """Only strong hints mark a business outright; single weak words do not."""
        strong = [h for h in hints if h.startswith(("verified business", "commerce/", "handle looks"))]
        return bool(strong) or len(hints) >= 2

    def _mark_business(self, uid: str, c: dict, reason: str, res: filters.BucketResult) -> None:
        self.funnel["business"] += 1
        self.store.set_screening(self.market.code, PLATFORM, uid, BUSINESS,
                                 reason=f"business account: {reason}", bucket=res.bucket,
                                 evidence=res.evidence, run_id=self.run_id)
        if res.bucket != filters.OTHER and c.get("handle"):
            self.store.add_seed(self.market.code, PLATFORM, c["handle"], "business", uid=uid,
                                run_id=self.run_id)

    def _prequeue_other_market(self, uid: str, region: str | None) -> None:
        """Queue accounts that belong to another supported market for that market's runs.

        A known registration region queues the account as sure; without a region,
        any supported market whose language or local signals the account matches
        gets it as unsure.
        """
        if region:
            if region == self.market.code or region not in self.supported:
                return
            if self.store.get_screening(region, PLATFORM, uid) is None:
                self.store.set_screening(region, PLATFORM, uid, PENDING, bucket=filters.SURE,
                                         evidence=[f"registered region {region}",
                                                   f"found while scouting {self.market.code}"],
                                         run_id=None)
            return
        c = self.store.get_creator(PLATFORM, uid) or {}
        vids = self.store.videos_for(PLATFORM, uid, limit=10)
        for other in self._other_markets():
            if self.store.get_screening(other.code, PLATFORM, uid) is not None:
                continue
            res = filters.market_bucket(other, region=None, region_source=None,
                                        language=c.get("language"), bio=c.get("bio"), videos=vids)
            if res.bucket != filters.OTHER:
                self.store.set_screening(other.code, PLATFORM, uid, PENDING, bucket=filters.UNSURE,
                                         evidence=res.evidence + [f"found while scouting {self.market.code}"],
                                         run_id=None)

    def _other_markets(self) -> list[Market]:
        if not hasattr(self, "_other_market_cache"):
            from .market import load_market
            self._other_market_cache = [load_market(c) for c in sorted(self.supported)
                                        if c != self.market.code]
        return self._other_market_cache

    # ---- stage: harvest --------------------------------------------------

    def generated_keywords(self) -> dict:
        raw = self.store.meta_get(keywords_meta_key(self.market.code))
        return json.loads(raw) if raw else {}

    def ensure_keywords(self) -> None:
        """Generate local keyword ideas once per market when a free LLM judge is active."""
        if self.generated_keywords() or not hasattr(self.judge, "keywords"):
            return
        try:
            data = self.judge.keywords(self.market)
        except Exception as exc:  # generation is optional; curated seeds still work
            self.emit("keywords", f"keyword generation unavailable: {exc}")
            return
        self.store.meta_set(keywords_meta_key(self.market.code), json.dumps(data, ensure_ascii=False))
        self.emit("keywords", f"generated {len(data['local_hashtags'])} local hashtags, "
                              f"{len(data['keywords'])} keywords")

    def harvest_plan(self, hashtags: list[str] | None = None,
                     keywords: list[str] | None = None) -> list[tuple[str, str, str | None]]:
        """Ordered ``(kind, term, proxy_region)`` sources, gaming-specific first."""
        code = self.market.code
        plan: list[tuple[str, str, str | None]] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, term: str, region: str | None) -> None:
            term = term.strip().lstrip("#") if kind == "hashtag" else term.strip()
            if term and (kind, term.lower()) not in seen:
                seen.add((kind, term.lower()))
                plan.append((kind, term, region))

        if hashtags is not None or keywords is not None:
            global_tags = set(self.market.global_hashtags)
            global_tags |= set(self.generated_keywords().get("global_hashtags", []))
            for t in hashtags or []:
                add("hashtag", t, code if t.lstrip("#") in global_tags else None)
            for q in keywords or []:
                add("keyword", q, code)
            return plan
        gen = self.generated_keywords()
        platforms = self.s.extra.get("platforms", ["tiktok", "youtube"])
        tiktok_queries = self.market.seed_keywords + gen.get("tiktok_queries", []) + gen.get("keywords", [])
        yt_queries = self.market.youtube_queries + gen.get("youtube_queries", [])
        if "tiktok" in platforms:
            for t in self.market.seed_hashtags + gen.get("local_hashtags", []):
                add("hashtag", t, None)
            for q in tiktok_queries:
                add("tt_top", q, code)
                add("keyword", q, code)
                add("keyword_liked", q, code)
                add("tt_users", q, None)
            if self.s.extra.get("use_general_tags"):
                for t in self.market.general_hashtags:
                    add("hashtag", t, None)
            for t in self.market.global_hashtags + gen.get("global_hashtags", []):
                add("hashtag", t, code)
        if "youtube" in platforms:
            for q in yt_queries:
                add("yt_search", q, code)
                add("yt_channels", q, code)
                add("yt_shorts", q, code)
                add("yt_api_shorts", q, code)
            for t in self.market.shorts_hashtags + gen.get("shorts_hashtags", []):
                add("yt_shorts_tag", t, code)
        return plan

    def harvest_cap(self) -> int:
        return int(self.s.budget * self.s.extra.get("harvest_share", 0.35))

    @staticmethod
    def source_group(kind: str, region: str | None) -> str:
        return "global" if (kind == "hashtag" and region) else kind

    def note_group(self, uid: str) -> None:
        """Remember which source group first found ``uid`` in this run."""
        if self.current_group:
            self.group_of.setdefault(uid, self.current_group)

    def _group_credit(self, uid: str, field: str) -> None:
        group = self.group_of.get(uid)
        if group:
            self.group_stats.setdefault(group, self._new_group_stats())[field] += 1

    @staticmethod
    def _new_group_stats() -> dict:
        return {"pages": 0, "credits": 0, "new_in_market": 0, "yes": 0, "accepted": 0}

    def group_score(self, group: str) -> float:
        """Yield per credit: accepted creators weigh most, pre-judge "yes" and new in-market
        accounts give an early signal before anything is judged."""
        st = self.group_stats.get(group) or self._new_group_stats()
        value = 10 * st["accepted"] + 2 * st["yes"] + st["new_in_market"] + 1
        return value / (st["credits"] + 2)

    def harvest_iter(self, hashtags: list[str] | None = None, keywords: list[str] | None = None):
        """Yield once per harvest page fetched.

        Every source group (local hashtags, TikTok top / keyword / user search,
        YouTube search, channel search, Shorts search, global tags) first gets a
        couple of exploration pages; after that the next page goes to the group
        with the best yield per credit so far, so sources that work get more of
        the budget automatically.
        """
        groups: dict[str, list] = {}
        for kind, term, region in self.harvest_plan(hashtags, keywords):
            groups.setdefault(self.source_group(kind, region), []).append((kind, term, region))
        streams = {g: self._group_stream(g, sources) for g, sources in groups.items()}
        order = list(streams)
        explore = self.s.extra.get("explore_pages", 2)
        while streams:
            if self.harvest_spent >= self.harvest_cap():
                return
            fresh = [g for g in order if g in streams
                     and (self.group_stats.get(g) or {}).get("pages", 0) < explore]
            group = fresh[0] if fresh else max(streams, key=self.group_score)
            if next(streams[group], None) is None:
                del streams[group]
            else:
                yield True

    def _group_stream(self, group: str, sources: list):
        for kind, term, region in sources:
            for _ in self._harvest_source(kind, term, region):
                yield True

    def harvest_step(self) -> bool:
        """Fetch the next harvest page. Returns False once the plan or its cap is exhausted."""
        if self._harvest is None:
            self._harvest = self.harvest_iter(self._plan_hashtags, self._plan_keywords)
        return next(self._harvest, None) is not None

    def harvest(self, hashtags: list[str] | None = None, keywords: list[str] | None = None) -> None:
        """Run the whole harvest plan at once (used by tests and harvest-only runs)."""
        for _ in self.harvest_iter(hashtags, keywords):
            pass

    YT_MODES = ("yt_search", "yt_channels", "yt_shorts", "yt_shorts_tag", "yt_api_shorts")

    def _harvest_source(self, kind: str, term: str, region: str | None):
        """Page through one source while it keeps yielding new in-market accounts."""
        group = self.source_group(kind, region)
        stats = self.group_stats.setdefault(group, self._new_group_stats())
        cursor = None
        dry = 0
        # Deep pagination is allowed; the dry-page rule stops sources that stop yielding.
        max_pages = self.s.extra.get("max_pages_per_source", 10)
        if kind == "yt_api_shorts":
            max_pages = 1  # official search costs 100 quota units per call
            if self.funnel.get("yt_api_shorts_pages", 0) >= self.s.extra.get("max_api_searches", 25):
                return
        for page in range(max_pages):
            if self.harvest_spent >= self.harvest_cap():
                return
            before = self.client.meter.used
            self.current_group = group
            try:
                if kind in self.YT_MODES:
                    new_in_market, cursor, fetched = self.yt_search_page(term, cursor, kind)
                    items, has_more = None, bool(cursor)
                    if not fetched:
                        return
                else:
                    fetched_page = self._tiktok_search_page(kind, term, region, cursor)
                    if fetched_page is None:
                        return
                    new_in_market, items, cursor, has_more = fetched_page
            finally:
                spent = self.client.meter.used - before
                self.harvest_spent += spent
                stats["credits"] += spent
                self.current_group = None
            stats["pages"] += 1
            stats["new_in_market"] += new_in_market
            yield True
            dry = dry + 1 if new_in_market < self.s.min_new_in_market_per_page else 0
            # A short page means the source is nearly exhausted even when the API
            # still reports ``has_more``; the next page is usually empty.
            short_page = items is not None and items < self.s.extra.get("min_page_size", 10)
            if dry >= self.s.stop_after_dry_pages or not has_more or short_page:
                return

    def _tiktok_search_page(self, kind: str, term: str, region: str | None, cursor):
        """One TikTok hashtag / keyword / top / user search page.

        Returns ``(new_in_market, items_on_page, next_cursor, has_more)`` or None.
        """
        if kind == "hashtag":
            body, _ = self._call(self.client.tiktok_hashtag, term, cursor=cursor, region=region,
                                 max_age=self.s.ttl_search)
            parse = tt.parse_hashtag
        elif kind in ("keyword", "keyword_liked"):
            liked = kind == "keyword_liked"
            body, _ = self._call(self.client.tiktok_keyword, term, cursor=cursor,
                                 date_posted="all-time" if liked else self.s.keyword_date_posted,
                                 sort_by="most-liked" if liked else "relevance", region=region,
                                 max_age=self.s.ttl_search)
            parse = tt.parse_keyword
        elif kind == "tt_top":
            body, _ = self._call(self.client.get, "/v1/tiktok/search/top",
                                 {"query": term, "region": region, "cursor": cursor},
                                 max_age=self.s.ttl_search)
            parse = tt.parse_top
        elif kind == "tt_users":
            body, _ = self._call(self.client.tiktok_search_users, term, cursor=cursor,
                                 max_age=self.s.ttl_search)
            parse = None
        else:
            return None
        if body is None:
            return None
        if parse is None:
            accounts = tt.parse_users(body)
            pairs = [(a, None) for a in accounts]
            cursor, has_more = body.get("cursor"), bool(body.get("has_more"))
        else:
            pairs, cursor, has_more = parse(body, term)
        self.funnel[f"{kind}_pages"] = self.funnel.get(f"{kind}_pages", 0) + 1
        new_in_market = 0
        for acc, video in pairs:
            bucket = self.observe(acc, kind, term, video=video)
            if bucket in (filters.SURE, filters.UNSURE):
                new_in_market += 1
        proxy = f" via {region} proxy" if region else ""
        self.emit("harvest", f"{kind} '{term}'{proxy}: {len(pairs)} results, "
                             f"{new_in_market} new in-market accounts")
        return new_in_market, len(pairs), cursor, has_more

    # ---- stage: retailer seeds ------------------------------------------

    def resolve_retailer_seeds(self) -> None:
        for entry in self.market.retailer_seeds:
            label = entry.get("name")
            handle = entry.get("handle")
            if not handle and entry.get("query"):
                handle = self._resolve_handle(entry["query"])
            if handle:
                if self.store.add_seed(self.market.code, PLATFORM, handle, "retailer", label=label,
                                       run_id=self.run_id):
                    self.emit("seeds", f"retailer seed @{handle} ({label})")

    def _resolve_handle(self, query: str) -> str | None:
        known = self.store.meta_get(f"resolved_handle:{self.market.code}:{query}")
        if known is not None:
            return known or None
        body, _ = self._call(self.client.tiktok_search_users, query)
        handle = None
        if body:
            needle = "".join(ch for ch in query.lower().split()[0] if ch.isalnum())
            for acc in tt.parse_users(body):
                h = acc.get("handle") or ""
                if needle and needle in "".join(ch for ch in h.lower() if ch.isalnum()):
                    handle = h
                    self.observe(acc, "retailer_search", query)
                    break
        self.store.meta_set(f"resolved_handle:{self.market.code}:{query}", handle or "")
        return handle

    # ---- stage: pre-judge ------------------------------------------------

    def _pending(self, platform: str | None = None) -> list[dict]:
        return [s for s in self.store.screenings(self.market.code, PENDING, platform=platform)
                if s["uid"] not in self.skipped_uids]

    def prejudge_pending(self) -> int:
        """Triage pending candidates from free data. Returns the number triaged."""
        if not self.judging:
            return 0
        code = self.market.code
        todo = [(s["platform"], s["uid"]) for s in self._pending()
                if self.store.get_prejudgment(code, s["platform"], s["uid"]) is None]
        if not todo:
            return 0
        items = [prejudge_payload(self.store, self.market, uid, platform) for platform, uid in todo]
        results = self.judge.prejudge(self.market, items)
        if results is None:
            raise Paused("awaiting_prejudge", len(todo))
        for platform, uid in todo:
            r = results.get(uid)
            # A missing answer never excludes: the account continues as "unsure".
            verdict = r["verdict"] if r else "unsure"
            reason = (r or {}).get("reason") or ("" if r else "pre-judge gave no answer")
            self.apply_prejudgment(uid, verdict, reason, (r or {}).get("model"), platform)
        self.emit("prejudge", f"triaged {len(todo)} candidates: {self.funnel['prejudge_yes']} yes, "
                              f"{self.funnel['prejudge_unsure']} unsure, "
                              f"{self.funnel['prejudge_no']} no so far")
        return len(todo)

    def apply_prejudgment(self, uid: str, verdict: str, reason: str, model: str | None,
                          platform: str = PLATFORM) -> None:
        code = self.market.code
        self.store.set_prejudgment(code, platform, uid, verdict, reason, model, self.run_id)
        self.funnel["prejudged"] += 1
        self.funnel[f"prejudge_{verdict}"] += 1
        if verdict == "yes":
            self._group_credit(uid, "yes")
        if verdict == "no":
            self.store.set_screening(code, platform, uid, FILTERED,
                                     reason=f"pre-judge: no ({reason})", run_id=self.run_id)

    # ---- stage: enrichment -----------------------------------------------

    def priority(self, s: dict) -> tuple:
        """Lower sorts first.

        Order: confirmed market, pre-judge yes, more gaming terms, then a size
        hint (followers known in band, else the harvested video's view count,
        since near-zero views usually mean an account below the band).
        """
        uid, platform = s["uid"], s["platform"]
        c = self.store.get_creator(platform, uid) or {}
        pj = self.store.get_prejudgment(self.market.code, platform, uid)
        verdict_rank = {"yes": 0, "unsure": 1}.get(pj["verdict"], 2) if pj else 1
        vids = self.store.videos_for(platform, uid, limit=3)
        texts = [c.get("nickname"), c.get("bio")] + [v["caption"] for v in vids] + \
                [e["via"] for e in self.store.edges_to(platform, uid) if e["kind"] == "hashtag"]
        hits = len(signals.gaming_hits(texts, self.market))
        if c.get("followers") is not None:
            size_rank = 0
        else:
            views = max((v.get("play_count") or 0 for v in vids), default=0)
            size_rank = 1 if views >= self.s.extra.get("min_views_hint", 1000) else 2
        return (s["bucket"] != filters.SURE, verdict_rank, -min(hits, 5), size_rank)

    def enrich_pending(self, limit: int | None = None) -> int:
        """Enrich up to ``limit`` candidates in priority order. Returns the number qualified."""
        code = self.market.code
        pending = self._pending()
        if self.judging:
            pending = [s for s in pending
                       if (self.store.get_prejudgment(code, s["platform"], s["uid"]) or {})
                       .get("verdict") in ("yes", "unsure")]
        pending.sort(key=self.priority)
        qualified = attempted = 0
        for s in pending:
            if self.target_met() or (limit is not None and attempted >= limit):
                break
            attempted += 1
            enrich = self.yt_enrich_one if s["platform"] == YOUTUBE else self.enrich_one
            if enrich(s["uid"]):
                qualified += 1
        self.last_enrich_attempts = attempted
        return qualified

    def enrich_one(self, uid: str) -> bool:
        code = self.market.code
        c = self.store.get_creator(PLATFORM, uid)
        handle = c.get("handle")
        if not handle:
            self.skipped_uids.add(uid)
            return False
        screening = self.store.get_screening(code, PLATFORM, uid) or {}

        # Unsure accounts: confirm the registration region (1 credit) before paying
        # for the profile and videos; most unsure accounts fail here.
        if screening.get("bucket") == filters.UNSURE and c.get("region_source") != "lookup":
            if not self._lookup_region(uid, handle) or not self._still_pending(uid):
                return False
            c = self.store.get_creator(PLATFORM, uid)

        if not c.get("enriched_at"):
            body, _ = self._call(self.client.tiktok_profile, handle, max_age=self.s.ttl_profile)
            if body is None:
                self._fail(uid, "profile unavailable")
                return False
            self.funnel["profiles_fetched"] += 1
            p = tt.parse_profile(body)
            emails = signals.extract_emails(p["bio"])
            links = dict(c.get("links") or {})
            links.update(signals.extract_links(p["bio"]))
            self.store.upsert_creator(
                PLATFORM, uid, handle=p["handle"], sec_uid=p["sec_uid"], nickname=p["nickname"],
                language=p["language"], followers=p["followers"], following=p["following"],
                videos=p["videos"], hearts=p["hearts"], bio=p["bio"], bio_link=p["bio_link"],
                is_private=p["is_private"], is_organization=p["is_organization"],
                is_commerce=p["is_commerce"], emails=emails, links=links, enriched_at=time.time())
            self.store.add_observation(PLATFORM, uid, "profile", followers=p["followers"],
                                       following=p["following"], videos=p["videos"],
                                       hearts=p["hearts"])
            if not self._still_pending(uid):
                return False
            bucket = self.store.get_screening(code, PLATFORM, uid)["bucket"]
            if bucket == filters.UNSURE and c.get("region_source") != "lookup":
                if not self._lookup_region(uid, handle) or not self._still_pending(uid):
                    return False

        # Recent videos: activity, metrics, ad markers.
        body, _ = self._call(self.client.tiktok_videos, handle, max_age=self.s.ttl_profile)
        if body is None:
            self._fail(uid, "videos unavailable")
            return False
        self.funnel["videos_fetched"] += 1
        vids, _, _ = tt.parse_videos(body)
        for v in vids:
            v["uid"] = uid
            if not v["is_ad"]:
                v["is_ad"] = signals.is_ad_caption(v["caption"], self.market)
        self.store.upsert_videos(PLATFORM, vids)
        c = self.store.get_creator(PLATFORM, uid)
        m = metrics.compute(vids, c.get("followers"), now=self.now)
        captions = [v["caption"] for v in vids]
        sponsors = sorted({h for v in vids for h in signals.sponsor_mentions(v["caption"], self.market)})
        m["sponsors"] = sponsors
        m["competitors"] = signals.competitor_hits(captions + sponsors + [c.get("bio") or ""])
        m["old_hardware"] = metrics.has_old_hardware(captions + [c.get("bio") or ""])
        self.store.put_metrics(PLATFORM, uid, m)

        reason = filters.activity_reason(m["days_since_last_post"], self.s.max_days_since_post)
        if reason:
            self.funnel["filtered_inactive"] += 1
            self.store.set_screening(code, PLATFORM, uid, FILTERED, reason=reason, run_id=self.run_id)
            return False
        # Re-bucket now that captions are known (adds caption evidence).
        if not self._still_pending(uid):
            return False

        self._collect_link_contacts(uid)
        self.store.set_screening(code, PLATFORM, uid, QUALIFIED, reason=None, run_id=self.run_id)
        self.store.add_run_member(self.run_id, code, PLATFORM, uid, QUALIFIED)
        self.funnel["qualified"] += 1
        if not self.judging:
            self.store.add_seed(code, PLATFORM, handle, "creator", uid=uid, run_id=self.run_id)
        self.emit("enrich", f"enriched @{handle} ({c.get('followers')} followers)", handle=handle)
        return True

    def _fail(self, uid: str, reason: str) -> None:
        """Record a failed lookup. Transient failures leave the account pending for retry."""
        if self._permanent_error():
            self.store.set_screening(self.market.code, PLATFORM, uid, ERROR, reason=reason,
                                     run_id=self.run_id)
        else:
            self.skipped_uids.add(uid)

    def _still_pending(self, uid: str) -> bool:
        """Re-run the hard filters on current data; True if the account is still a candidate."""
        self.screen(uid)
        return self.store.get_screening(self.market.code, PLATFORM, uid)["status"] == PENDING

    def _lookup_region(self, uid: str, handle: str) -> bool:
        body, _ = self._call(self.client.tiktok_region, handle)
        if body is None:
            self._fail(uid, "region lookup failed")
            return False
        self.funnel["region_lookups"] += 1
        region = tt.parse_region(body)
        if region:
            self.store.upsert_creator(PLATFORM, uid, region=region, region_source="lookup")
        return True

    def _collect_link_contacts(self, uid: str) -> None:
        c = self.store.get_creator(PLATFORM, uid)
        if not self.fetch_link_pages or not c.get("bio_link"):
            return
        found = linkpages.fetch_contacts(c["bio_link"])
        if not found["ok"]:
            return
        emails = list(dict.fromkeys((c.get("emails") or []) + found["emails"]))
        links = dict(found["links"])
        links.update(c.get("links") or {})
        self.store.upsert_creator(PLATFORM, uid, emails=emails, links=links)

    # ---- stage: judge ----------------------------------------------------

    def judge_qualified(self) -> int:
        """Judge enriched candidates of this run. Returns the number judged."""
        if not self.judging:
            return 0
        code = self.market.code
        todo = [(s["platform"], s["uid"])
                for s in self.store.screenings(code, QUALIFIED, run_id=self.run_id)
                if self.judge_attempts.get(s["uid"], 0) < 2]
        if not todo:
            return 0
        # Judge and store in chunks: progress is saved as it goes, so a long
        # re-judge under free-tier rate limits can be watched and interrupted.
        chunk = self.s.extra.get("judge_chunk", 12)
        judged = 0
        for start in range(0, len(todo), chunk):
            if self.hard_deadline and time.time() > self.hard_deadline:
                self.emit("judge", f"time limit: {len(todo) - start} left unjudged")
                break
            part = todo[start:start + chunk]
            items = [judge_payload(self.store, self.market, uid, platform) for platform, uid in part]
            results = self.judge.judge(self.market, items)
            if results is None:
                raise Paused("awaiting_judgment", len(todo))
            for platform, uid in part:
                r = results.get(uid)
                if r is None:
                    self.judge_attempts[uid] = self.judge_attempts.get(uid, 0) + 1
                    continue
                self.apply_decision(uid, r, r.get("model"), platform)
                judged += 1
            self.emit("judge", f"judged {judged} of {len(todo)}: {self.funnel['accepted']} accepted, "
                               f"{self.funnel['maybe']} maybe, {self.funnel['rejected']} rejected so far")
        return judged

    def apply_decision(self, uid: str, r: dict, model: str | None, platform: str = PLATFORM) -> str:
        """Store a judgment after code-side checks. Returns the resulting status."""
        code = self.market.code
        c = self.store.get_creator(platform, uid) or {}
        m = self.store.get_metrics(platform, uid) or {}
        s = self.store.get_screening(code, platform, uid) or {}
        r = dict(r)
        notes: list[str] = []
        decision = r.get("decision", "maybe")

        if m.get("competitors"):
            r["competitor_conflict"] = True
            r["sponsors_mentioned"] = sorted(set(r.get("sponsors_mentioned") or [])
                                             | set(m.get("sponsors") or []))
        def downgrade(to: str, note: str) -> None:
            nonlocal decision
            decision = to
            notes.append(note)

        # Keep the decision consistent with the rubric's own score thresholds.
        relevance = r.get("gaming_pc_relevance") or 0
        appeal = r.get("young_gamer_appeal") or 0
        meets_rule = relevance >= 3 or appeal >= 4
        # Only upgrade when the judge's own fit score agrees; a low score with high
        # relevance usually means "gaming, but not a real creator" (e.g. clip accounts).
        if decision == "maybe" and meets_rule and (r.get("fit_score") or 0) >= 50 \
                and not r.get("brand_safety_flags") \
                and not r.get("is_business_account") and not r.get("is_organization") \
                and self._evidence_found(r.get("evidence_quote"), uid, platform):
            decision = "accept"
            notes.append(f"upgraded: relevance {relevance}, appeal {appeal} meet the accept rule")
        elif decision == "accept" and not meets_rule:
            downgrade("maybe", f"relevance {relevance}, appeal {appeal} below the accept rule")
        if r.get("is_business_account") and decision != "reject":
            downgrade("reject", "business account")
        if r.get("is_organization") and decision == "accept":
            downgrade("maybe", "organisation account")
        if decision == "accept" and not self._evidence_found(r.get("evidence_quote"), uid, platform):
            downgrade("maybe", "evidence quote not found in bio or captions")
        resolution = (r.get("market_resolution") or "unclear").upper()
        if s.get("bucket") == filters.UNSURE and resolution not in (code, "UNCLEAR"):
            status = OTHER_MARKET
            self.funnel["judge_other_market"] += 1
            reason = f"judge: based in {resolution}"
        else:
            if s.get("bucket") == filters.UNSURE and resolution == "UNCLEAR" and decision == "accept":
                downgrade("maybe", "market unclear")
            status = {"accept": ACCEPTED, "maybe": MAYBE, "reject": REJECTED}[decision]
            reason = None if status == ACCEPTED else (r.get("reasons") or "")[:200]
        r["decision"] = decision
        if notes:
            r["code_notes"] = notes
        self.store.set_decision(code, platform, uid, decision, r.get("fit_score"), r, model,
                                self.run_id)
        self.store.set_screening(code, platform, uid, status, reason=reason, run_id=self.run_id)
        self.store.add_run_member(self.run_id, code, platform, uid, status)
        self.funnel["judged"] += 1
        if status in (ACCEPTED, MAYBE, REJECTED):
            self.funnel[status] += 1
        if status == ACCEPTED:
            self._group_credit(uid, "accepted")
            try:
                self.on_accept(platform, uid, c, r)
            except BudgetExhausted:
                # Linking another platform is optional; judging must not stop on it.
                self.emit("budget", f"skipped cross-platform link for {uid}: budget spent")
        return status

    def on_accept(self, platform: str, uid: str, c: dict, r: dict) -> None:
        """Accepted creators feed the snowball and get their other platforms linked."""
        code = self.market.code
        label = f"@{c.get('handle')}" if platform == PLATFORM else f"YouTube {c.get('nickname')}"
        self.emit("judge", f"accepted {label} (fit {r.get('fit_score')})", handle=c.get("handle"))
        if platform == PLATFORM and c.get("handle"):
            self.store.add_seed(code, PLATFORM, c["handle"], "creator", uid=uid, run_id=self.run_id)
            yt_link = (c.get("links") or {}).get("youtube") or ""
            if yt_link.startswith("UC") and self.youtube is not None:
                self.link_youtube_channel(yt_link, (PLATFORM, uid), "TikTok bio links the channel")
        elif platform == YOUTUBE and not isinstance(self.judge, DeferredJudge):
            self.yt_after_accept(uid)
            # A linked TikTok account of an accepted channel is a snowball seed.
            for p, other, _ in self.store.linked_accounts(YOUTUBE, uid):
                oc = self.store.get_creator(p, other) or {}
                if p == PLATFORM and oc.get("handle"):
                    self.store.add_seed(code, PLATFORM, oc["handle"], "linked", uid=other,
                                        run_id=self.run_id)

    def _evidence_found(self, quote: str | None, uid: str, platform: str = PLATFORM) -> bool:
        """The quote must appear (whitespace- and case-insensitively) in the bio or a caption."""
        q = _norm(quote or "")
        if len(q) < 4:
            return False
        c = self.store.get_creator(platform, uid) or {}
        texts = [c.get("bio") or "", c.get("nickname") or ""] + \
                [v["caption"] or "" for v in self.store.videos_for(platform, uid, limit=40)]
        blob = " ".join(_norm(t) for t in texts)
        probe = q[:40]
        return probe in blob or all(part in blob for part in q.split("…") if part.strip())

    # ---- stage: pitches --------------------------------------------------

    def write_pitches(self) -> int:
        if not self.judging:
            return 0
        code = self.market.code
        todo = {s["uid"]: s["platform"] for s in self.store.screenings(code, ACCEPTED, run_id=self.run_id)
                if self.store.get_pitch(code, s["platform"], s["uid"]) is None}
        if not todo:
            return 0
        results = self.judge.pitch(self.market, [pitch_payload(self.store, self.market, u, p)
                                                 for u, p in todo.items()])
        if not results:
            return 0
        for uid, p in results.items():
            if uid in todo:
                self.store.set_pitch(code, todo[uid], uid, p.get("language"), p.get("subject"),
                                     p.get("body"), p.get("dm"), p.get("model"), self.run_id)
                self.funnel["pitches"] += 1
        self.emit("pitch", f"drafted {self.funnel['pitches']} pitches")
        return len(results)

    # ---- stage: snowball -------------------------------------------------

    def seed_usable(self, seed: dict) -> bool:
        """Creator seeds must be accepted (or qualified when not judging); shops only on request."""
        if seed["kind"] in ("retailer", "business"):
            return bool(self.s.extra.get("use_retailer_seeds"))
        if seed["kind"] == "partner":
            return bool(self.s.extra.get("use_partner_seeds"))
        if seed["kind"] == "linked":
            return True  # added only for accepted channels on another platform
        if not seed.get("uid"):
            return False
        s = self.store.get_screening(self.market.code, PLATFORM, seed["uid"]) or {}
        if self.judging:
            return s.get("status") == ACCEPTED
        return s.get("status") in (QUALIFIED, ACCEPTED)

    def seed_score(self, seed: dict) -> float:
        prior = {"creator": 2.0, "linked": 2.0, "partner": 2.0, "retailer": 1.5,
                 "business": 1.0}.get(seed["kind"], 1.0)
        if seed["pages_fetched"] == 0:
            return prior * 1.5  # try new seeds early
        return (seed["new_in_market"] + 1) / (seed["credits_spent"] + 1) * prior

    def usable_seeds(self) -> list[dict]:
        return sorted((s for s in self.store.seeds(self.market.code)
                       if s["handle"] not in self.skipped_seeds and self.seed_usable(s)),
                      key=self.seed_score, reverse=True)

    def snowball_step(self) -> bool:
        """Expand one following page of the most productive usable seed."""
        for seed in self.usable_seeds():
            if self.expand_seed(seed):
                return True
        return False

    def snowball_round(self) -> int:
        """Fetch one following page from each usable seed, best first. Returns pages fetched."""
        pages = 0
        for seed in self.usable_seeds():
            if self.target_met():
                break
            if self.expand_seed(seed):
                pages += 1
        return pages

    def expand_seed(self, seed: dict) -> bool:
        code, handle = self.market.code, seed["handle"]
        if seed["pages_fetched"] >= self.s.max_pages_per_seed:
            self.store.update_seed(code, PLATFORM, handle, exhausted=1, exhausted_reason="page cap")
            return False
        cursor = seed["next_cursor"]
        body, spent = self._call(self.client.tiktok_following, handle,
                                 min_time=int(cursor) if cursor else None,
                                 max_age=self.s.ttl_profile)
        if body is None:
            if self._permanent_error():
                self.store.update_seed(code, PLATFORM, handle, exhausted=1,
                                       exhausted_reason="following unavailable",
                                       credits_spent=seed["credits_spent"] + spent)
            else:
                # Transient failure: skip for this run, retry on a later one.
                self.skipped_seeds.add(handle)
            return False
        accounts, next_cursor, has_more, total = tt.parse_following(body)
        self.funnel["following_pages"] += 1
        if seed["pages_fetched"] == 0:
            self.funnel["seeds_expanded"] += 1
        seed_uid = seed.get("uid")
        if seed_uid:
            self.store.upsert_creator(PLATFORM, seed_uid, following_visible=bool(accounts))
        new_in_market = 0
        self.current_group = "snowball (partner seeds)" if seed["kind"] == "partner" else "snowball"
        for acc in accounts:
            bucket = self.observe(acc, "following", handle, from_uid=seed_uid)
            if bucket in (filters.SURE, filters.UNSURE):
                new_in_market += 1
        self.current_group = None
        fields: dict[str, Any] = {
            "pages_fetched": seed["pages_fetched"] + 1,
            "next_cursor": str(next_cursor) if next_cursor else None,
            "accounts_seen": seed["accounts_seen"] + len(accounts),
            "new_in_market": seed["new_in_market"] + new_in_market,
            "credits_spent": seed["credits_spent"] + spent,
            "total_following": total,
        }
        if not accounts:
            fields.update(exhausted=1, exhausted_reason="following list hidden or empty")
        elif not has_more:
            fields.update(exhausted=1, exhausted_reason="end of list")
        elif new_in_market == 0 and seed["pages_fetched"] >= 1 and seed["new_in_market"] == 0:
            fields.update(exhausted=1, exhausted_reason="two pages without in-market accounts")
        self.store.update_seed(code, PLATFORM, handle, **fields)
        self.emit("snowball", f"@{handle} following page {fields['pages_fetched']}: "
                              f"{len(accounts)} accounts, {new_in_market} new in-market")
        return True

    # ---- top level -------------------------------------------------------

    def process_pending(self) -> None:
        """Pre-judge, enrich and judge in small chunks so the target check stays current."""
        chunk = None if isinstance(self.judge, DeferredJudge) else self.s.extra.get("enrich_chunk", 12)
        while not self.target_met():
            self.prejudge_pending()
            self.enrich_pending(limit=chunk if self.judging else None)
            self.judge_qualified()
            if not self.last_enrich_attempts or not self.judging:
                return

    def main_loop(self) -> None:
        """Process candidates, then fetch more: harvest pages first, snowball once it can start.

        Hashtags and searches only find starting points in small markets; the
        following lists of accepted creators find the rest. As soon as
        ``snowball_start_after`` creators are accepted, the loop alternates
        ``snowball_pages_per_harvest_page`` snowball pages with one harvest page.
        """
        harvest_open = True
        while True:
            self.process_pending()
            if self.target_met():
                return
            progressed = False
            if self.count_status(ACCEPTED) >= self.s.snowball_start_after or not harvest_open:
                for _ in range(self.s.snowball_pages_per_harvest_page):
                    if self.snowball_step():
                        progressed = True
                        self.process_pending()
                        if self.target_met():
                            return
            if harvest_open:
                if self.harvest_step():
                    progressed = True
                else:
                    harvest_open = False
                    progressed = progressed or self.snowball_step()
            if not progressed:
                self.process_pending()
                return

    def rescreen(self) -> int:
        """Re-apply the hard filters to this run's pending accounts (after a config change)."""
        moved = 0
        for s in self.store.screenings(self.market.code, PENDING, run_id=self.run_id):
            (self.yt_screen if s["platform"] == YOUTUBE else self.screen)(s["uid"])
            if self.store.get_screening(self.market.code, s["platform"], s["uid"])["status"] != PENDING:
                moved += 1
        self.funnel["rescreened_out"] = self.funnel.get("rescreened_out", 0) + moved
        self.emit("rescreen", f"{moved} pending accounts no longer pass the hard filters")
        return moved

    def reopen_judgments(self) -> int:
        """Send this run's judged accounts back to the judge (after a rubric change)."""
        code = self.market.code
        reopened = 0
        for status in (ACCEPTED, MAYBE, REJECTED):
            for s in self.store.screenings(code, status, run_id=self.run_id):
                for table in ("decisions", "pitches"):  # a pitch follows the decision it was written for
                    self.store.conn.execute(
                        f"DELETE FROM {table} WHERE market = ? AND platform = ? AND uid = ?",
                        (code, s["platform"], s["uid"]))
                self.store.set_screening(code, s["platform"], s["uid"], QUALIFIED,
                                         run_id=self.run_id)
                self.funnel[status] = max(self.funnel.get(status, 0) - 1, 0)
                self.funnel["judged"] = max(self.funnel.get("judged", 0) - 1, 0)
                reopened += 1
        self.store.conn.commit()
        self.emit("judge", f"reopened {reopened} judgments")
        return reopened

    def resume_state(self) -> None:
        """Restore counters of a paused run so a resumed run reports one funnel."""
        run = self.store.get_run(self.run_id) or {}
        for k, v in (run.get("funnel") or {}).items():
            if isinstance(v, int) and k != "target_count":
                self.funnel[k] = v
        rows = self.store.conn.execute(
            "SELECT DISTINCT to_uid FROM edges WHERE run_id = ?", (self.run_id,)).fetchall()
        self.seen_this_run = {r["to_uid"] for r in rows}

    def run(self, hashtags: list[str] | None = None, keywords: list[str] | None = None,
            resume: bool = False, rescreen: bool = False, rejudge: bool = False) -> dict:
        started = time.time()
        try:
            if resume:
                self.resume_state()
                if rescreen:
                    self.rescreen()
                if rejudge:
                    self.reopen_judgments()
            else:
                if self.judging:
                    self.ensure_keywords()
                if self.s.extra.get("use_retailer_seeds"):
                    self.resolve_retailer_seeds()
            self._plan_hashtags, self._plan_keywords = hashtags, keywords
            self.main_loop()
            if self.s.extra.get("pitches"):
                self.write_pitches()
            self.status = "target_met" if self.target_met() else "done"
        except BudgetExhausted as exc:
            self.status = "time_limit" if isinstance(exc, TimeLimit) else "budget_exhausted"
            self.emit("budget", f"{'time limit' if isinstance(exc, TimeLimit) else 'credit budget'} "
                                "reached; judging what was enriched")
            try:
                self.judge_qualified()
                if self.s.extra.get("pitches"):
                    self.write_pitches()
            except (Paused, BudgetExhausted) as p:
                if isinstance(p, Paused):
                    self.status = p.status
        except Paused as p:
            self.status = p.status
            self.emit("paused", f"waiting for external judge: {p.count} candidates ({p.status})")
        self._finish(started)
        return self.store.get_run(self.run_id)

    def _finish(self, started: float) -> None:
        self.funnel["target_count"] = self.target_count()
        meter = self.client.meter
        fields: dict[str, Any] = dict(status=self.status, funnel=self.funnel,
                                      credits_used=meter.used, api_calls=meter.live_calls,
                                      cache_hits=meter.cache_hits, finished_at=time.time())
        if self.llm is not None:
            run = self.store.get_run(self.run_id) or {}
            fields["llm_calls"] = (run.get("llm_calls") or 0) + self.llm.stats.calls
            fields["llm_tokens"] = (run.get("llm_tokens") or 0) + self.llm.stats.tokens
        self.store.update_run(self.run_id, **fields)
        self.emit("done", f"run {self.run_id} {self.status}: {self.target_count()} "
                          f"{'accepted' if self.judging else 'qualified'}, {meter.used} credits "
                          f"in {time.time() - started:.0f}s")
