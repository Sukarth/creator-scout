from conftest import FIXTURE_NOW

from scout import metrics
from scout.sources import tiktok as tt


def test_metrics_from_real_videos(fixture):
    vids, _, _ = tt.parse_videos(fixture("tiktok_videos_digikamu"))
    m = metrics.compute(vids, followers=32571, now=FIXTURE_NOW)
    assert m["n_videos"] == 10
    assert m["median_views"] == 20531  # median of the ten play counts
    assert 0 < m["er_views"] < 0.2
    assert m["ad_count"] == 3
    assert m["days_since_last_post"] < 2
    assert m["posts_per_week"] > 1


def test_metrics_without_videos():
    m = metrics.compute([], followers=1000)
    assert m["median_views"] is None and m["days_since_last_post"] is None


def test_price_estimate_has_nano_floor():
    assert metrics.price_estimate(1000) == (50, 100)
    assert metrics.price_estimate(20531) == (60, 140)
    assert metrics.price_estimate(None) is None


def test_deal_suggestion():
    assert metrics.suggest_deal(5000).startswith("Gifting")
    assert metrics.suggest_deal(30000).startswith("Paid integration")
    old = metrics.has_old_hardware(["my setup: GTX 1060 and i5-4460"])
    assert old and metrics.suggest_deal(30000, old).startswith("Upgrade for content")
