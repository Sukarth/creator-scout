"""Full run on cached fixtures: a retailer seed's following list yields a qualified creator."""

from conftest import FIXTURE_NOW

from scout.checks import check_run
from scout.config import RunSettings
from scout.export import build_sheets, export_run
from scout.market import load_market
from scout.pipeline import Pipeline
from scout.sources.scrapecreators import CreditMeter, ScrapeCreators


def make_pipeline(store, market, settings, **kw):
    run_id = store.create_run(market.code, {"band_min": settings.band_min,
                                            "band_max": settings.band_max}, settings.budget)
    client = ScrapeCreators(store, api_key=None, meter=CreditMeter(settings.budget), offline=True)
    return Pipeline(store, client, market, settings, run_id, fetch_link_pages=False,
                    now=FIXTURE_NOW, **kw), run_id


def fi_market():
    m = load_market("fi")
    m.retailer_seeds = [{"name": "Jimm's PC", "handle": "jimmspc"},
                        {"name": "Nuvoo", "handle": "nuvoosuomi"}]
    return m


def test_snowball_from_retailer_finds_digikamu(cached_store, tmp_path):
    store = cached_store
    settings = RunSettings(band_min=1000, band_max=100_000, target=5, budget=50)
    pipe, run_id = make_pipeline(store, fi_market(), settings)
    run = pipe.run(hashtags=[], keywords=[])

    qualified = store.screenings("FI", "needs_judgment", run_id=run_id)
    handles = {store.get_creator("tiktok", s["uid"])["handle"] for s in qualified}
    assert handles == {"digikamu"}
    digi = store.get_creator_by_handle("tiktok", "digikamu")
    assert digi["emails"] == ["creatorbd06af@example.fi"]
    m = store.get_metrics("tiktok", digi["uid"])
    # 3 platform-flagged paid partnerships + 4 gifted posts marked "Mainos" in the caption.
    assert m["ad_count"] == 7
    assert {"realme.suomi", "samsung", "amarancreators"} <= set(m["sponsors"])

    # Out-of-band and out-of-market accounts are kept with their reasons.
    aguel = store.get_creator_by_handle("tiktok", "aguel_design")
    assert store.get_screening("FI", "tiktok", aguel["uid"])["reason"].startswith("below band")
    aus = store.get_creator_by_handle("tiktok", "melbournecomputerscrew")
    assert store.get_screening("FI", "tiktok", aus["uid"])["status"] == "other_market"
    # A German account found while scouting Finland is queued for Germany.
    de = store.get_creator_by_handle("tiktok", "pulsetechh")
    assert store.get_screening("DE", "tiktok", de["uid"])["status"] == "pending"

    seeds = {s["handle"]: s for s in store.seeds("FI", active_only=False)}
    assert seeds["nuvoosuomi"]["exhausted_reason"] == "following list hidden or empty"
    assert seeds["jimmspc"]["pages_fetched"] >= 1

    f = run["funnel"]
    assert f["seen"] == f["already_known"] + f["bucket_sure"] + f["bucket_unsure"] + f["bucket_other"]
    assert f["qualified_via_snowball"] == 1
    assert run["credits_used"] == 0  # everything came from the cache

    sheets = build_sheets(store, run_id)
    row = sheets["Shortlist"][0]
    assert row["handle"] == "digikamu" and row["found_via"] == "followed by @jimmspc"
    assert row["competitor_conflict"] == "no" and row["price_low_eur"] == 60
    paths = export_run(store, run_id, tmp_path)
    assert paths[0].suffix == ".xlsx" and paths[0].stat().st_size > 0
    assert check_run(store, run_id).passed


def test_second_run_returns_no_duplicates(cached_store):
    store = cached_store
    settings = RunSettings(band_min=1000, band_max=100_000, target=5, budget=50)
    make_pipeline(store, fi_market(), settings)[0].run(hashtags=[], keywords=[])
    # Reach the same accounts again through a fresh pass over the seed's list.
    store.update_seed("FI", "tiktok", "jimmspc", exhausted=0, pages_fetched=0, next_cursor=None)
    pipe, run_id = make_pipeline(store, fi_market(), settings)
    run = pipe.run(hashtags=[], keywords=[])
    assert store.screenings("FI", "needs_judgment", run_id=run_id) == []
    assert run["funnel"]["already_known"] >= 1
    assert check_run(store, run_id).passed


def test_hashtag_harvest_buckets(cached_store):
    store = cached_store
    ee = load_market("ee")
    ee.retailer_seeds = []
    settings = RunSettings(band_min=1000, band_max=100_000, target=5, budget=50)
    pipe, run_id = make_pipeline(store, ee, settings)
    pipe.harvest(hashtags=["eestitiktok"], keywords=[])
    f = pipe.funnel
    assert f["seen"] == 15  # 20 videos from 15 unique authors
    assert f["bucket_sure"] == 14  # every EE-registered author
    # Registered in PH but Estonian bio and captions: kept as unsure, not dropped.
    acc = store.get_creator_by_handle("tiktok", "eestiparimtiktokker")
    assert store.get_screening("EE", "tiktok", acc["uid"])["bucket"] == "unsure"


def test_budget_stops_cleanly(store):
    settings = RunSettings(band_min=1000, band_max=100_000, target=5, budget=0)
    run_id = store.create_run("FI", {}, 0)
    client = ScrapeCreators(store, api_key="dummy", meter=CreditMeter(0))
    pipe = Pipeline(store, client, fi_market(), settings, run_id, fetch_link_pages=False)
    run = pipe.run(hashtags=["pelikone"], keywords=[])
    assert run["status"] == "budget_exhausted" and run["credits_used"] == 0
