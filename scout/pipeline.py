"""Run orchestration: harvest, screen, enrich, snowball.

Flow per run:
1. Harvest seed hashtags and keywords (capped at a share of the budget).
2. Resolve retailer and competitor seed accounts.
3. Repeat until the target is met, the budget is spent or seeds run dry:
   enrich pending in-market candidates, then expand one page of the most
   productive seeds' following lists.

Every account seen is stored with its market bucket and, when filtered, the
reason. Screening outcomes are per market, so accounts filtered for one market
stay available to others.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import filters, metrics, signals
from .config import RunSettings
from .market import Market, supported_codes
from .sources import linkpages
from .sources import tiktok as tt
from .sources.scrapecreators import ApiError, BudgetExhausted, ScrapeCreators
from .store import FINAL_STATUSES, Store

PLATFORM = tt.PLATFORM

PENDING = "pending"
QUALIFIED = "needs_judgment"
FILTERED = "filtered"
OTHER_MARKET = "other_market"
BUSINESS = "business"
ERROR = "error"

# Statuses that count toward a run's target.
QUALIFIED_STATUSES = (QUALIFIED, "accepted", "maybe")

ProgressFn = Callable[[dict], None]


class Pipeline:
    def __init__(self, store: Store, client: ScrapeCreators, market: Market,
                 settings: RunSettings, run_id: int, progress: ProgressFn | None = None,
                 fetch_link_pages: bool = True, now: float | None = None):
        self.store = store
        self.now = now
        self.client = client
        self.market = market
        self.s = settings
        self.run_id = run_id
        self.progress = progress or (lambda event: None)
        self.fetch_link_pages = fetch_link_pages
        self.seen_this_run: set[str] = set()
        self.skipped_seeds: set[str] = set()
        self.skipped_uids: set[str] = set()
        self.last_error_status: int | None = None
        self.supported = supported_codes()
        self.funnel: dict[str, int] = {
            "seen": 0, "new": 0, "already_known": 0,
            "bucket_sure": 0, "bucket_unsure": 0, "bucket_other": 0,
            "filtered_band": 0, "filtered_private": 0, "filtered_inactive": 0,
            "filtered_other": 0, "business": 0, "region_lookups": 0,
            "profiles_fetched": 0, "videos_fetched": 0, "qualified": 0,
            "qualified_via_snowball": 0, "seeds_expanded": 0, "following_pages": 0,
            "hashtag_pages": 0, "keyword_pages": 0, "errors": 0,
        }
        self.status = "running"

    # ---- helpers ------------------------------------------------------------

    def emit(self, stage: str, message: str, **extra: Any) -> None:
        self.progress({"stage": stage, "message": message, "credits_used": self.client.meter.used,
                       "budget": self.client.meter.budget, "funnel": dict(self.funnel), **extra})

    def qualified_count(self) -> int:
        return len(self.store.screenings(self.market.code, QUALIFIED_STATUSES, run_id=self.run_id))

    def target_met(self) -> bool:
        return self.qualified_count() >= self.s.target

    def _call(self, fn: Callable, *args: Any, **kwargs: Any) -> tuple[dict | None, int]:
        """Invoke a client method; return ``(body_or_None, credits_spent)``.

        ``BudgetExhausted`` propagates so the run can stop cleanly.
        """
        before = self.client.meter.used
        self.last_error_status = None
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
        self.funnel["seen"] += 1
        if existing is None:
            self.funnel["new"] += 1

        screening = self.store.get_screening(self.market.code, PLATFORM, uid)
        if screening and screening["status"] not in (PENDING, OTHER_MARKET):
            self.funnel["already_known"] += 1
            return None
        bucket = self.screen(uid, business_hint=acc.get("enterprise_reason"))
        self.funnel[f"bucket_{bucket}"] += 1
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
        if hints and self._is_business(hints, c):
            self._mark_business(uid, c, "; ".join(hints), res)
            return res.bucket
        self.store.set_screening(code, PLATFORM, uid, PENDING, reason=None, bucket=res.bucket,
                                 evidence=res.evidence, run_id=self.run_id)
        return res.bucket

    @staticmethod
    def _is_business(hints: list[str], creator: dict) -> bool:
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
        """Queue accounts registered in another supported market for that market's runs."""
        if not region or region == self.market.code or region not in self.supported:
            return
        if self.store.get_screening(region, PLATFORM, uid) is None:
            self.store.set_screening(region, PLATFORM, uid, PENDING, bucket=filters.SURE,
                                     evidence=[f"registered region {region}",
                                               f"found while scouting {self.market.code}"],
                                     run_id=None)

    # ---- stage: harvest --------------------------------------------------

    def harvest(self, hashtags: list[str] | None = None, keywords: list[str] | None = None) -> None:
        cap = int(self.s.budget * self.s.extra.get("harvest_share", 0.35))
        hashtags = self.market.seed_hashtags if hashtags is None else hashtags
        keywords = self.market.seed_keywords if keywords is None else keywords
        for tag in hashtags:
            if self.client.meter.used >= cap:
                break
            self._harvest_source("hashtag", tag, cap)
        for query in keywords:
            if self.client.meter.used >= cap:
                break
            self._harvest_source("keyword", query, cap)

    def _harvest_source(self, kind: str, term: str, cap: int) -> None:
        cursor = None
        dry = 0
        max_pages = self.s.max_pages_per_hashtag if kind == "hashtag" else self.s.max_pages_per_keyword
        for page in range(max_pages):
            if self.client.meter.used >= cap:
                return
            if kind == "hashtag":
                body, _ = self._call(self.client.tiktok_hashtag, term, cursor=cursor,
                                     max_age=self.s.ttl_search)
            else:
                body, _ = self._call(self.client.tiktok_keyword, term, cursor=cursor,
                                     date_posted=self.s.keyword_date_posted,
                                     max_age=self.s.ttl_search)
            if body is None:
                return
            parse = tt.parse_hashtag if kind == "hashtag" else tt.parse_keyword
            pairs, cursor, has_more = parse(body, term)
            self.funnel[f"{kind}_pages"] += 1
            new_in_market = 0
            for acc, video in pairs:
                bucket = self.observe(acc, kind, term, video=video)
                if bucket in (filters.SURE, filters.UNSURE):
                    new_in_market += 1
            self.emit("harvest", f"{kind} '{term}' page {page + 1}: {len(pairs)} videos, "
                                 f"{new_in_market} new in-market accounts")
            dry = dry + 1 if new_in_market < self.s.min_new_in_market_per_page else 0
            if dry >= self.s.stop_after_dry_pages or not has_more or not pairs:
                return

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

    # ---- stage: enrichment -----------------------------------------------

    def enrich_pending(self) -> int:
        """Enrich pending candidates until the target is met. Returns number qualified."""
        pending = [s for s in self.store.screenings(self.market.code, PENDING)
                   if s["uid"] not in self.skipped_uids]
        # Sure before unsure; accounts with known in-band followers first.
        pending.sort(key=lambda s: (s["bucket"] != filters.SURE,
                                    self.store.get_creator(PLATFORM, s["uid"]).get("followers") is None))
        qualified = 0
        for s in pending:
            if self.target_met():
                break
            if self.enrich_one(s["uid"]):
                qualified += 1
        return qualified

    def enrich_one(self, uid: str) -> bool:
        code = self.market.code
        c = self.store.get_creator(PLATFORM, uid)
        handle = c.get("handle")
        if not handle:
            return False
        screening = self.store.get_screening(code, PLATFORM, uid) or {}

        # Unsure accounts with known followers: confirm region before paying for more.
        if screening.get("bucket") == filters.UNSURE and c.get("followers") is not None \
                and c.get("region_source") != "lookup":
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
        if any(e["kind"] == "following" for e in self.store.edges_to(PLATFORM, uid)) and \
                not any(e["kind"] in ("hashtag", "keyword") for e in self.store.edges_to(PLATFORM, uid)):
            self.funnel["qualified_via_snowball"] += 1
        self.store.add_seed(code, PLATFORM, handle, "creator", uid=uid, run_id=self.run_id)
        self.emit("enrich", f"qualified @{handle} ({c.get('followers')} followers)",
                  handle=handle)
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

    # ---- stage: snowball -------------------------------------------------

    def seed_score(self, seed: dict) -> float:
        prior = {"retailer": 3.0, "business": 2.0, "creator": 1.5}.get(seed["kind"], 1.0)
        if seed["pages_fetched"] == 0:
            return prior
        return (seed["new_in_market"] + 1) / (seed["credits_spent"] + 1) * prior

    def snowball_round(self) -> int:
        """Fetch one following page from each active seed, best first. Returns pages fetched."""
        seeds = sorted((s for s in self.store.seeds(self.market.code)
                        if s["handle"] not in self.skipped_seeds),
                       key=self.seed_score, reverse=True)
        pages = 0
        for seed in seeds:
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
        for acc in accounts:
            bucket = self.observe(acc, "following", handle, from_uid=seed_uid)
            if bucket in (filters.SURE, filters.UNSURE):
                new_in_market += 1
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

    def run(self, hashtags: list[str] | None = None, keywords: list[str] | None = None,
            skip_harvest: bool = False) -> dict:
        started = time.time()
        try:
            if not skip_harvest:
                self.emit("harvest", "harvesting hashtags and keywords")
                self.harvest(hashtags, keywords)
            self.resolve_retailer_seeds()
            for round_no in range(self.s.max_snowball_rounds):
                self.emit("enrich", f"round {round_no + 1}: enriching pending candidates")
                self.enrich_pending()
                if self.target_met():
                    break
                if self.snowball_round() == 0:
                    self.enrich_pending()
                    break
            else:
                self.enrich_pending()
            self.status = "target_met" if self.target_met() else "done"
        except BudgetExhausted:
            self.status = "budget_exhausted"
            self.emit("budget", "credit budget reached; stopping")
        self.funnel["qualified_total_for_run"] = self.qualified_count()
        meter = self.client.meter
        self.store.update_run(self.run_id, status=self.status, funnel=self.funnel,
                              credits_used=meter.used, api_calls=meter.live_calls,
                              cache_hits=meter.cache_hits, finished_at=time.time())
        self.emit("done", f"run {self.run_id} {self.status}: {self.qualified_count()} qualified, "
                          f"{meter.used} credits in {time.time() - started:.0f}s")
        return self.store.get_run(self.run_id)
