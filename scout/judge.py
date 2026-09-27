"""LLM judgment: pre-judge triage, full fit judgment, pitches and keyword ideas.

Two judge implementations share one interface:
- ``FreeJudge`` calls the free LLM chain.
- ``DeferredJudge`` answers nothing; the pipeline pauses, the candidates are
  exported with ``scout candidates`` for an external judge (e.g. Claude in skill
  mode) and the answers are imported with ``scout decide``.

Payload builders are shared, so both judges see exactly the same evidence.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import metrics as metrics_mod
from . import signals
from .export import found_via
from .llm import LLMClient, LLMUnavailable, estimate_tokens
from .market import Market
from .store import Store

PLATFORM = "tiktok"
BRAND_PATH = Path(__file__).with_name("brand.yaml")


def load_brand(path: Path = BRAND_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trim(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---- response schemas -----------------------------------------------------

class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PrejudgeResult(_Lenient):
    id: str
    verdict: Literal["yes", "unsure", "no"]
    reason: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v):
        return str(v)

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v):
        return str(v).strip().lower()


class JudgeResult(_Lenient):
    id: str
    decision: Literal["accept", "maybe", "reject"]
    fit_score: int = Field(0, ge=0, le=100)
    is_business_account: bool = False
    is_organization: bool = False
    market_resolution: str = "unclear"
    content_language: str = ""
    niche_category: str = ""
    games: list[str] = []
    young_gamer_appeal: int = Field(0, ge=0, le=5)
    niche_tags: list[str] = []
    content_styles: list[str] = []
    trust_content_score: int = Field(0, ge=0, le=5)
    gaming_pc_relevance: int = Field(0, ge=0, le=5)
    sponsors_mentioned: list[str] = []
    competitor_conflict: bool = False
    brand_safety_flags: list[str] = []
    reasons: str = ""
    evidence_quote: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v):
        return str(v)

    @field_validator("decision", mode="before")
    @classmethod
    def _decision(cls, v):
        return str(v).strip().lower()

    @field_validator("fit_score", "trust_content_score", "gaming_pc_relevance",
                     "young_gamer_appeal", mode="before")
    @classmethod
    def _clamp(cls, v, info):
        top = 100 if info.field_name == "fit_score" else 5
        try:
            return max(0, min(top, int(float(v))))
        except (TypeError, ValueError):
            return 0

    @field_validator("niche_tags", "content_styles", "sponsors_mentioned", "brand_safety_flags",
                     "games", mode="before")
    @classmethod
    def _list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return [str(s) for s in v]


class PitchResult(_Lenient):
    id: str
    language: str = ""
    subject: str = ""
    body: str = ""
    dm: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v):
        return str(v)


# ---- payload builders -----------------------------------------------------

def prejudge_payload(store: Store, market: Market, uid: str, platform: str = PLATFORM) -> dict:
    """Free data only: what harvest, search and following lists already returned."""
    c = store.get_creator(platform, uid) or {}
    vids = store.videos_for(platform, uid, limit=3)
    captions = [_trim(v["caption"], 140) for v in vids if v.get("caption")]
    tags = sorted({t for v in vids for t in signals.hashtags_in(v.get("caption"))})[:12]
    item = {"id": uid, "platform": platform, "handle": c.get("handle"),
            "nickname": _trim(c.get("nickname"), 40), "bio": _trim(c.get("bio"), 160),
            ("video_titles" if platform == "youtube" else "captions"): captions[:3 if platform == "youtube" else 2],
            "hashtags": tags, "found_via": found_via(store, uid, limit=2, platform=platform)}
    if c.get("followers"):
        item["subscribers" if platform == "youtube" else "followers"] = c["followers"]
    return item


def judge_payload(store: Store, market: Market, uid: str, platform: str = PLATFORM) -> dict:
    c = store.get_creator(platform, uid) or {}
    m = store.get_metrics(platform, uid) or {}
    s = store.get_screening(market.code, platform, uid) or {}
    vids = store.videos_for(platform, uid, limit=12)
    item = {
        "id": uid, "platform": platform, "handle": c.get("handle"),
        "nickname": _trim(c.get("nickname"), 40),
        ("subscribers" if platform == "youtube" else "followers"): c.get("followers"),
        "app_language": c.get("language"), "country_field": c.get("region"),
        "market_bucket": s.get("bucket"),
        "market_evidence": (s.get("market_evidence") or [])[:5],
        "bio": _trim(c.get("bio"), 220), "bio_link": c.get("bio_link"),
        ("recent_video_titles" if platform == "youtube" else "recent_captions"):
            [_trim(v["caption"], 110) for v in vids if v.get("caption")][:10],
        "posts_per_week": m.get("posts_per_week"), "ad_posts_in_recent": m.get("ad_count"),
        "sponsors_detected": m.get("sponsors") or [],
        "competitors_detected": m.get("competitors") or [],
    }
    for key in ("views", "long", "shorts"):
        if m.get(key):
            v = m[key]
            item[f"{key}_views" if key != "views" else "views"] = \
                f"avg {v.get('avg_views')} over {v.get('window')} ({v.get('n')} videos)"
    return item


def pitch_payload(store: Store, market: Market, uid: str, platform: str = PLATFORM) -> dict:
    c = store.get_creator(platform, uid) or {}
    m = store.get_metrics(platform, uid) or {}
    d = (store.get_decision(market.code, platform, uid) or {}).get("data", {})
    vids = [v for v in store.videos_for(platform, uid, limit=6) if v.get("caption")]
    return {
        "id": uid, "platform": platform, "handle": c.get("handle"), "nickname": c.get("nickname"),
        "content_language": d.get("content_language") or c.get("language") or market.languages[0],
        "niche_tags": d.get("niche_tags"), "content_styles": d.get("content_styles"),
        "recent_videos": [_trim(v["caption"], 120) for v in vids[:4]],
        "suggested_deal": metrics_mod.suggest_deal(c.get("followers"), m.get("old_hardware")),
        "followers": c.get("followers"),
    }


def batches(items: list[dict], max_items: int, max_tokens: int) -> list[list[dict]]:
    """Split items into batches bounded by count and estimated prompt tokens."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for it in items:
        t = estimate_tokens(json.dumps(it, ensure_ascii=False))
        if cur and (len(cur) >= max_items or size + t > max_tokens):
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += t
    if cur:
        out.append(cur)
    return out


# ---- prompts --------------------------------------------------------------

def prejudge_system(brand: dict, market: Market) -> str:
    return f"""You triage TikTok and YouTube accounts for influencer outreach by {brand['name']} ({brand['niche']}) in {market.name}.
You only see free data: nickname, bio or channel description, a few captions or video titles, hashtags and how the account was found.
The question: could a young audience that plays PC games plausibly watch this creator? Gaming (PC or console games, streaming, esports, game clips), tech, PC hardware, gaming gear and gaming news are the core; entertainment or comedy creators with a young, gaming-adjacent audience also count.
- "yes": a clear gaming, tech or young-gamer-audience signal.
- "unsure": too little information, or mixed content where gaming or a young gamer audience might appear. When in doubt, answer unsure.
- "no": confidently irrelevant. The content is clearly another niche for an older or non-gaming audience (food, beauty, fashion, parenting, news, politics, finance, music teaching) with no gaming signal, or the account is a shop, brand or organisation.
An empty bio or a single caption is "unsure", not "no". Text may be in {', '.join(market.languages)} or English.
Some local words for playing (e.g. Estonian "mängimine") also cover children's play, playgrounds, board games and sports; those are "no" unless video games appear.
Return JSON: {{"results": [{{"id": "<id>", "verdict": "yes|unsure|no", "reason": "<max 12 words>"}}]}} with exactly one entry per input id."""


def judge_system(brand: dict, market: Market) -> str:
    competitors = "; ".join(f"{c['name']} ({c['kind']}, TikTok: {', '.join('@' + h for h in c.get('handles', []))})"
                            for c in brand.get("competitors", []))
    return f"""You judge TikTok and YouTube creators for influencer partnerships with {brand['name']}.
About the brand: {brand['description']}
Target market: {market.name} ({market.code}); local languages: {', '.join(market.languages)}.
The core question: would a young audience that plays PC games watch this creator? Gaming and tech are the core; gaming gear, gaming news and entertainment creators with a young gamer audience also fit.
A good partner: {brand['good_partner']}
Reject: {brand['reject']}
Competitors and competing retailers: {competitors}. Sponsorship by one of them is a flag (competitor_conflict=true), never a reason to reject: it proves the creator takes deals.
Rules:
- Base every judgment only on the data given. evidence_quote must be copied verbatim from the bio, a caption or a video title. No evidence, no accept.
- Shops, retailers and brands: decision "reject", is_business_account=true.
- Organisations (police, schools, public bodies, media outlets): is_organization=true; at most "maybe".
- market_resolution: "{market.code}" if the creator is plausibly based in or speaks to {market.name}; another ISO country code if clearly elsewhere; "unclear" otherwise. Use market_bucket and market_evidence: "sure" means TikTok registration country is {market.code}.
- content_language: ISO 639-1 code of the language the creator mainly writes or speaks in captions.
- gaming_pc_relevance (0-5): 5 = PC hardware, builds or setups are the focus; 4 = mostly gameplay, streaming or game content; 3 = gaming is a regular part of the content; 2 = occasional gaming; 1 = a single mention; 0 = none.
- trust_content_score (0-5): how well the creator could make trust content (builds, benchmarks, setup tours, upgrade stories, honest reviews). It is a score for prioritising, not a requirement: a gamer whose audience plays games is a good partner even without hardware content today, because a PC upgrade story is a natural video for them.
- young_gamer_appeal (0-5): how likely a young PC-gaming audience watches this creator.
- niche_category: one of "gaming", "tech review", "gaming gear", "gaming news", "entertainment", "lifestyle", "other".
- games: the game titles the creator plays or covers (e.g. ["Minecraft", "Fortnite"]); empty if none.
- brand_safety_flags: short labels for real risks only (e.g. "gambling", "adult content", "hate speech", "mostly children on camera").
- "accept": a real creator (own content, not a repost or clip account) with gaming_pc_relevance >= 3, or young_gamer_appeal >= 4 for entertainment/tech creators. "maybe": borderline (relevance 2 or appeal 3), or the evidence is thin. "reject": no gaming or young-gamer angle, not a real creator, business account, or brand-unsafe.
- reasons: one or two sentences in English.
Return JSON: {{"results": [{{"id": "<id>", "decision": "accept|maybe|reject", "fit_score": 0-100, "is_business_account": bool, "is_organization": bool, "market_resolution": "...", "content_language": "..", "niche_category": "...", "games": [], "niche_tags": [], "content_styles": [], "trust_content_score": 0-5, "gaming_pc_relevance": 0-5, "young_gamer_appeal": 0-5, "sponsors_mentioned": [], "competitor_conflict": bool, "brand_safety_flags": [], "reasons": "...", "evidence_quote": "..."}}]}} with exactly one entry per input id."""


def pitch_system(brand: dict, market: Market) -> str:
    trust = "; ".join(brand.get("trust_points", []))
    return f"""You write first-contact outreach drafts from {brand['name']} to TikTok creators. A human reviews and sends them.
About the brand: {brand['description']} Trust points: {trust}.
For each creator write in their content_language (ISO code given; "et" Estonian, "ru" Russian, "fi" Finnish, "de" German, "en" English):
- subject: under 60 characters.
- body: under 120 words. Refer to one specific recent video from recent_videos. Say why their audience fits. Propose the suggested_deal in plain words; do not quote a price. Friendly and direct, no hype, no emojis. Sign as "[Name], {brand['name']}". End with one short sentence saying we found their public TikTok profile and they can reply "no" to never hear from us again.
- dm: under 300 characters, same language, for a TikTok or Instagram DM.
Return JSON: {{"results": [{{"id": "<id>", "language": "<ISO code used>", "subject": "...", "body": "...", "dm": "..."}}]}}"""


def keywords_system(brand: dict, market: Market) -> str:
    return f"""You help find small gaming and PC-hardware creators on TikTok in {market.name} for {brand['name']}.
Local languages: {', '.join(market.languages)}. Generate search terms that local gaming creators actually use.
- local_hashtags: 20 to 40 gaming-specific hashtags in the local languages (and local-English mixes like "eestigamer"). No general country or lifestyle tags such as a plain country name, "tiktok"+country or city names alone.
- tiktok_queries: 40 to 60 short TikTok search queries (1 to 4 words) mixing: popular game names with local-language phrasing (e.g. "minecraft suomi", "fortnite suomeksi", "minecraft eesti keeles", "gta rp eesti"); creator-style phrases in the local language (e.g. "pelaan", "striimi", "let's play suomi"); and tech-review phrases in the local language (PC builds, graphics cards, gaming gear reviews).
- youtube_queries: 20 to 40 YouTube search queries in the same style, favouring phrases local creators put in video titles (e.g. "minecraft suomeksi", "eesti keeles").
- shorts_hashtags: 10 to 20 hashtags local creators use on YouTube Shorts (e.g. "minecraftsuomi").
- global_hashtags: 5 to 15 game titles or hardware tags popular with players in {market.name}.
Hashtags are lowercase, without "#" and without spaces. Queries are lowercase, no "#".
Return JSON: {{"local_hashtags": [], "tiktok_queries": [], "youtube_queries": [], "shorts_hashtags": [], "global_hashtags": [], "notes": "one sentence"}}"""


# ---- judges ---------------------------------------------------------------

class DeferredJudge:
    """Answers nothing; the pipeline pauses for an external judge."""

    name = "deferred"

    def prejudge(self, market: Market, items: list[dict]):
        return None

    def judge(self, market: Market, items: list[dict]):
        return None

    def pitch(self, market: Market, items: list[dict]):
        return None


class HybridJudge(DeferredJudge):
    """Free LLM pre-judge and keyword ideas; the fit judgment is left to an external
    judge (Claude), asked once for all enriched candidates when the run's budget is
    spent. One handoff instead of one per batch keeps interactive runs short."""

    name = "hybrid"
    judge_at_end = True

    def __init__(self, free: "FreeJudge"):
        self.free = free

    def prejudge(self, market: Market, items: list[dict]):
        return self.free.prejudge(market, items)

    def keywords(self, market: Market) -> dict:
        return self.free.keywords(market)


class FreeJudge:
    name = "free"
    PREJUDGE_BATCH = 30
    JUDGE_BATCH = 6

    def __init__(self, llm: LLMClient, brand: dict | None = None, parallel: int = 6):
        self.llm = llm
        self.brand = brand or load_brand()
        self.parallel = parallel

    def _run(self, task: str, system: str, items: list[dict], schema, max_items: int,
             max_prompt_tokens: int, out_tokens_per_item: int, temperature: float) -> dict[str, dict]:
        results: dict[str, dict] = {}

        def ask(batch: list[dict]):
            user = json.dumps({"accounts": batch}, ensure_ascii=False)
            try:
                return self.llm.chat_json(task, system, user,
                                          max_tokens=300 + out_tokens_per_item * len(batch),
                                          temperature=temperature)
            except LLMUnavailable:
                return None

        # Batches run in parallel; the LLM client spreads them over every free
        # model that has capacity (each Groq model has its own rate limit).
        todo = batches(items, max_items, max_prompt_tokens)
        with ThreadPoolExecutor(max_workers=min(self.parallel, len(todo) or 1)) as pool:
            answers = list(pool.map(ask, todo))
        for answer in answers:
            if answer is None:
                continue
            data, model = answer
            for raw in data.get("results") or []:
                try:
                    parsed = schema.model_validate(raw)
                except Exception:
                    continue
                entry = parsed.model_dump()
                entry["model"] = model
                results[parsed.id] = entry
        return results

    def prejudge(self, market: Market, items: list[dict]) -> dict[str, dict]:
        return self._run("prejudge", prejudge_system(self.brand, market), items, PrejudgeResult,
                         self.PREJUDGE_BATCH, 4200, 30, 0.1)

    def judge(self, market: Market, items: list[dict]) -> dict[str, dict]:
        return self._run("judge", judge_system(self.brand, market), items, JudgeResult,
                         self.JUDGE_BATCH, 3400, 320, 0.2)

    def pitch(self, market: Market, items: list[dict]) -> dict[str, dict]:
        return self._run("pitch", pitch_system(self.brand, market), items, PitchResult,
                         3, 2500, 450, 0.6)

    def keywords(self, market: Market) -> dict:
        user = json.dumps({"market": market.name, "curated_local_hashtags": market.seed_hashtags,
                           "curated_keywords": market.seed_keywords,
                           "curated_global_hashtags": market.global_hashtags}, ensure_ascii=False)
        data, model = self.llm.chat_json("keywords", keywords_system(self.brand, market), user,
                                         max_tokens=3000, temperature=0.4)
        tags = lambda xs: list(dict.fromkeys(str(x).strip().lstrip("#").lower().replace(" ", "")
                                             for x in xs or [] if str(x).strip()))
        queries = lambda xs: list(dict.fromkeys(str(x).strip().lstrip("#").lower()
                                                for x in xs or [] if str(x).strip()))
        return {"local_hashtags": tags(data.get("local_hashtags")),
                "tiktok_queries": queries(data.get("tiktok_queries") or data.get("keywords")),
                "youtube_queries": queries(data.get("youtube_queries")),
                "shorts_hashtags": tags(data.get("shorts_hashtags")),
                "global_hashtags": tags(data.get("global_hashtags")),
                "notes": data.get("notes", ""), "model": model}
