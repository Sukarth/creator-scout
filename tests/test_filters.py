from scout import filters
from scout.market import load_market

FI = load_market("fi")
EE = load_market("ee")


def bucket(market, **kw):
    kw.setdefault("region_source", "inline")
    kw.setdefault("language", None)
    kw.setdefault("bio", None)
    return filters.market_bucket(market, **kw)


def test_sure_when_region_matches():
    r = bucket(EE, region="EE")
    assert r.bucket == filters.SURE
    assert "registered region EE" in r.evidence[0]


def test_region_mismatch_with_finnish_language_is_unsure():
    # Inline region US, Finnish app language: the account must not be dropped.
    r = bucket(FI, region="US", language="fi")
    assert r.bucket == filters.UNSURE


def test_lookup_resolves_unsure_to_sure():
    r = bucket(FI, region="FI", region_source="lookup", language="fi")
    assert r.bucket == filters.SURE
    assert "(lookup)" in r.evidence[0]


def test_lookup_elsewhere_without_content_evidence_is_other():
    r = bucket(FI, region="US", region_source="lookup", language="fi")
    assert r.bucket == filters.OTHER


def test_lookup_elsewhere_with_strong_content_evidence_stays_unsure():
    r = bucket(EE, region="PH", region_source="lookup", bio="Eesti🇪🇪 Nr 1 Sisulooja Tallinn")
    assert r.bucket == filters.UNSURE


def test_keyword_author_with_video_region():
    r = bucket(FI, region=None, videos=[{"region": "FI", "caption": "", "caption_language": "fi"}])
    assert r.bucket == filters.UNSURE
    assert any("posted from FI" in e for e in r.evidence)


def test_shared_language_alone_is_not_evidence():
    # Russian-speaking account registered in Ukraine: not Estonian.
    r = bucket(EE, region="UA", language="ru",
               videos=[{"caption_language": "ru", "caption": "новый игровой пк", "region": "UA"}])
    assert r.bucket == filters.OTHER
    # Russian speaker with an Estonian city in the bio stays open.
    r = bucket(EE, region=None, language="ru", bio="Таллинн, стримы каждый день")
    assert r.bucket == filters.UNSURE
    # Registered in Estonia: sure, whatever the language.
    assert bucket(EE, region="EE", language="ru").bucket == filters.SURE


def test_unknown_region_account_is_queued_for_the_matching_market(store):
    from scout.config import RunSettings
    from scout.pipeline import Pipeline
    from scout.sources.scrapecreators import CreditMeter, ScrapeCreators
    store.upsert_creator("tiktok", "u1", handle="somefinn", bio="")
    store.upsert_videos("tiktok", [{"video_id": "v1", "uid": "u1", "caption": "pelataan fortnitea",
                                    "caption_language": "fi", "region": None}])
    pipe = Pipeline(store, ScrapeCreators(store, None, CreditMeter(0), offline=True), EE,
                    RunSettings(), run_id=1)
    assert pipe.screen("u1") == filters.OTHER
    queued = store.get_screening("FI", "tiktok", "u1")
    assert queued["status"] == "pending" and queued["bucket"] == "unsure"


def test_no_signals_is_other():
    r = bucket(EE, region="US", language="en", bio="LA vibes")
    assert r.bucket == filters.OTHER


def test_band():
    assert filters.band_reason(500, 1000, 100_000).startswith("below band")
    assert filters.band_reason(200_000, 1000, 100_000).startswith("above band")
    assert filters.band_reason(32_571, 1000, 100_000) is None
    assert filters.band_reason(None, 1000, 100_000) is None


def test_activity():
    assert filters.activity_reason(None, 60) == "no videos found"
    assert filters.activity_reason(90, 60).startswith("inactive")
    assert filters.activity_reason(3, 60) is None
