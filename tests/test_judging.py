"""Pre-judge, judge, pitch and the external-judge handoff, with a stub judge."""

import json

import httpx
from conftest import FIXTURE_NOW

from scout.checks import check_run
from scout.config import RunSettings
from scout.export import accepted_by_first_source, build_sheets
from scout.judge import DeferredJudge, FreeJudge, JudgeResult, batches, judge_payload, prejudge_payload
from scout.llm import LLMClient, Model, parse_json_object
from scout.market import load_market
from scout.pipeline import Pipeline
from scout.sources.scrapecreators import CreditMeter, ScrapeCreators


class StubJudge:
    """Says yes only to @digikamu and accepts it with a real quote from its bio."""

    name = "stub"

    def __init__(self, quote="Tech, News, Reviews"):
        self.quote = quote
        self.prejudged: list[str] = []

    def prejudge(self, market, items):
        self.prejudged += [it["handle"] for it in items]
        return {it["id"]: {"verdict": "yes" if it["handle"] == "digikamu" else
                           ("unsure" if it["handle"] == "taiturku" else "no"),
                           "reason": "stub", "model": "stub"} for it in items}

    def judge(self, market, items):
        out = {}
        for it in items:
            accept = it["handle"] == "digikamu"
            out[it["id"]] = JudgeResult.model_validate({
                "id": it["id"], "decision": "accept" if accept else "reject",
                "fit_score": 80 if accept else 10, "market_resolution": "FI",
                "content_language": "fi", "gaming_pc_relevance": 4 if accept else 0,
                "niche_category": "tech review" if accept else "other",
                "evidence_quote": self.quote if accept else "", "reasons": "stub"}).model_dump()
            out[it["id"]]["model"] = "stub"
        return out

    def pitch(self, market, items):
        return {it["id"]: {"language": it["content_language"], "subject": "Yhteistyö?",
                           "body": "Moi!", "dm": "Moi!", "model": "stub"} for it in items}


def fi_market():
    m = load_market("fi")
    m.retailer_seeds = [{"name": "Jimm's PC", "handle": "jimmspc"}]
    return m


def make(store, judge, target=1, judge_kind="free"):
    settings = RunSettings(band_min=1000, band_max=100_000, target=target, budget=50,
                           extra={"use_retailer_seeds": True, "pitches": True})
    run_id = store.create_run("FI", {"band_min": 1000, "band_max": 100_000, "target": target,
                                     "judge": judge_kind}, 50)
    client = ScrapeCreators(store, None, CreditMeter(50), offline=True)
    return Pipeline(store, client, fi_market(), settings, run_id, fetch_link_pages=False,
                    now=FIXTURE_NOW, judge=judge), run_id


def test_prejudge_no_is_stored_and_yes_is_enriched_and_accepted(cached_store):
    store = cached_store
    judge = StubJudge()
    pipe, run_id = make(store, judge)
    run = pipe.run(hashtags=[], keywords=[])

    digi = store.get_creator_by_handle("tiktok", "digikamu")
    assert store.get_screening("FI", "tiktok", digi["uid"])["status"] == "accepted"
    # Confident "no" is skipped before any paid call but kept with its reason.
    yle = store.get_creator_by_handle("tiktok", "yletamakinontotta")
    s = store.get_screening("FI", "tiktok", yle["uid"])
    assert s["status"] == "filtered" and s["reason"].startswith("pre-judge: no")
    assert yle.get("enriched_at") is None
    # Accepted creators become snowball seeds.
    seeds = {x["handle"]: x for x in store.seeds("FI", active_only=False)}
    assert seeds["digikamu"]["kind"] == "creator"
    f = run["funnel"]
    assert f["prejudge_yes"] == 1 and f["prejudge_no"] >= 1 and f["accepted"] == 1
    assert run["status"] == "target_met"
    assert accepted_by_first_source(store, run_id, "FI") == {"snowball": 1}

    row = build_sheets(store, run_id)["Shortlist"][0]
    assert row["tiktok_url"].endswith("@digikamu") and row["pitch_language"] == "fi"
    assert row["prejudge"].startswith("yes")
    assert check_run(store, run_id).passed


def test_unverifiable_evidence_downgrades_accept_to_maybe(cached_store):
    store = cached_store
    pipe, run_id = make(store, StubJudge(quote="I build gaming PCs every week"))
    pipe.run(hashtags=[], keywords=[])
    digi = store.get_creator_by_handle("tiktok", "digikamu")
    assert store.get_screening("FI", "tiktok", digi["uid"])["status"] == "maybe"
    d = store.get_decision("FI", "tiktok", digi["uid"])
    assert "evidence quote not found in bio or captions" in d["data"]["code_notes"]


def test_decision_follows_rubric_scores(cached_store):
    store = cached_store
    pipe, run_id = make(store, StubJudge())
    pipe.run(hashtags=[], keywords=[])
    digi = store.get_creator_by_handle("tiktok", "digikamu")
    base = {"id": digi["uid"], "fit_score": 60, "market_resolution": "FI",
            "evidence_quote": "Tech, News, Reviews", "reasons": "x"}
    status = pipe.apply_decision(digi["uid"], {**base, "decision": "maybe",
                                               "gaming_pc_relevance": 3}, "stub")
    assert status == "accepted"
    status = pipe.apply_decision(digi["uid"], {**base, "decision": "accept",
                                               "gaming_pc_relevance": 1, "young_gamer_appeal": 2},
                                 "stub")
    assert status == "maybe"


def test_rejected_creators_are_not_seeds(cached_store):
    store = cached_store
    pipe, run_id = make(store, StubJudge(), target=5)
    pipe.run(hashtags=[], keywords=[])
    taiturku = store.get_creator_by_handle("tiktok", "taiturku")
    seed_handles = {s["handle"] for s in store.seeds("FI", active_only=False) if pipe.seed_usable(s)}
    assert "taiturku" not in seed_handles
    assert store.get_screening("FI", "tiktok", taiturku["uid"])["status"] != "accepted"


def test_deferred_judge_pauses_and_resumes(cached_store):
    store = cached_store
    pipe, run_id = make(store, DeferredJudge(), judge_kind="claude")
    run = pipe.run(hashtags=[], keywords=[])
    assert run["status"] == "awaiting_prejudge"

    # External judge answers the pre-judge; the run resumes and pauses for judgment.
    stub = StubJudge()
    pending = [prejudge_payload(store, pipe.market, s["uid"]) for s in store.screenings("FI", "pending")]
    pipe2, _ = make(store, DeferredJudge(), judge_kind="claude")
    pipe2.run_id = run_id
    for uid, r in stub.prejudge(pipe.market, pending).items():
        pipe2.apply_prejudgment(uid, r["verdict"], r["reason"], "claude")
    run = pipe2.run(resume=True)
    assert run["status"] == "awaiting_judgment"

    items = [judge_payload(store, pipe.market, s["uid"])
             for s in store.screenings("FI", "needs_judgment", run_id=run_id)]
    for uid, r in stub.judge(pipe.market, items).items():
        pipe2.apply_decision(uid, r, "claude")
    store.update_run(run_id, funnel=pipe2.funnel)  # as `scout decide` does
    pipe3, _ = make(store, DeferredJudge(), judge_kind="claude")
    pipe3.run_id = run_id
    run = pipe3.run(resume=True)
    assert run["status"] == "target_met"
    assert run["funnel"]["accepted"] == 1


def test_harvest_plan_is_gaming_first_with_proxy_for_global_tags():
    store_less = Pipeline.__new__(Pipeline)
    store_less.market = load_market("ee")
    store_less.s = RunSettings()
    store_less.generated_keywords = lambda: {"local_hashtags": ["eestics2"], "keywords": [],
                                              "global_hashtags": ["cs2"]}
    plan = Pipeline.harvest_plan(store_less)
    terms = [t for _, t, _ in plan]
    assert terms[0] == "mängimine" and "eestics2" in terms
    assert "eestitiktok" not in terms  # general tags only on request
    assert ("hashtag", "cs2", "EE") in plan and ("hashtag", "геймер", "EE") in plan
    assert terms.index("eestics2") < terms.index("cs2")
    store_less.s = RunSettings(extra={"use_general_tags": True})
    assert "eestitiktok" in [t for _, t, _ in Pipeline.harvest_plan(store_less)]


def test_harvest_interleaves_source_groups():
    p = Pipeline.__new__(Pipeline)
    p.market = load_market("ee")
    p.s = RunSettings(budget=100)
    p.harvest_spent = 0
    p.generated_keywords = lambda: {}
    order = []

    def fake_source(kind, term, region):
        order.append(kind)
        yield True

    p._harvest_source = fake_source
    list(Pipeline.harvest_iter(p))
    # The first three pages come from three different source groups.
    assert order[:3] == ["hashtag", "yt_search", "keyword"]


def test_batches_respect_count_and_tokens():
    items = [{"id": str(i), "bio": "x" * 280} for i in range(70)]
    out = batches(items, max_items=30, max_tokens=10_000)
    assert [len(b) for b in out] == [30, 30, 10]
    out = batches(items, max_items=30, max_tokens=1000)
    assert all(len(b) <= 9 for b in out)


def test_parse_json_object_tolerates_fences():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here: {"a": [1, 2]} done') == {"a": [1, 2]}


def test_llm_rotates_to_next_model_on_rate_limit(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        if body["model"] == "first":
            return httpx.Response(429, headers={"retry-after": "30"}, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"results": []}'}}],
                                         "usage": {"total_tokens": 50}})

    models = {"a": Model("groq", "https://x", "GROQ_API_KEY", "first"),
              "b": Model("groq", "https://x", "GROQ_API_KEY", "second")}
    llm = LLMClient(models=models, http=httpx.Client(transport=httpx.MockTransport(handler)))
    import scout.llm as llm_mod
    monkeypatch.setitem(llm_mod.ROUTES, "judge", ["a", "b"])
    data, label = llm.chat_json("judge", "system", "user", max_tokens=100)
    assert data == {"results": []} and label == "groq:second" and calls == ["first", "second"]
    assert models["a"].cooldown_until > 0


def test_free_judge_maps_results_by_id(monkeypatch):
    class FakeLLM:
        def chat_json(self, task, system, user, max_tokens, temperature):
            ids = [a["id"] for a in json.loads(user)["accounts"]]
            return {"results": [{"id": i, "verdict": "YES", "reason": "games"} for i in ids]
                    + [{"id": "bogus", "verdict": "maybe?"}]}, "fake:model"

    judge = FreeJudge(FakeLLM(), brand={"name": "B", "niche": "gaming"})
    out = judge.prejudge(load_market("ee"), [{"id": "1", "handle": "a"}, {"id": "2", "handle": "b"}])
    assert set(out) == {"1", "2"} and out["1"]["verdict"] == "yes" and out["1"]["model"] == "fake:model"
