"""YouTube parsing, view windows, partner matching and identity merging."""

from conftest import FIXTURE_NOW

from scout import metrics
from scout.export import group_accounts, creator_row
from scout.partners import Partner, PartnerIndex, norm
from scout.sources import youtube as yt


CHANNEL_A = "UC65Eq6kioOHsxDKah7disfQ"


def test_search_yields_channels_from_videos_and_playlists(fixture):
    channels = yt.parse_search(fixture("youtube_search_minecraft_eesti"), "minecraft eesti")
    by_id = {c["uid"]: c for c in channels}
    assert len(by_id) >= 8
    assert len(by_id[CHANNEL_A]["hits"]) >= 3  # videos and a playlist


def test_api_channel_country_and_subscribers(fixture):
    items = {i["id"]: yt.parse_api_channel(i) for i in fixture("youtube_api_channels")["items"]}
    ch = items[CHANNEL_A]
    assert ch["country"] == "EE" and ch["followers"] == 50500 and ch["handle"]


def test_long_and_shorts_are_measured_separately(fixture):
    long_v = [yt.parse_api_video(v) for v in fixture("youtube_api_videos_uulf_channel_a")["items"]]
    short_v = [yt.parse_api_video(v) for v in fixture("youtube_api_videos_uush_channel_a")["items"]]
    assert all(v["duration"] > 60 for v in long_v[:5])
    assert all(v["duration"] <= 60 for v in short_v)
    s = metrics.view_summary(long_v, now=FIXTURE_NOW)
    assert s["window"] in ("last 30 days", "last 90 days") and s["n"] >= 3 and s["avg_views"] > 0
    shorts = metrics.view_summary(short_v, now=FIXTURE_NOW)
    assert shorts["window"].startswith("last ") and "videos" in shorts["window"]  # old Shorts only


def test_view_summary_windows():
    day = 86400
    vids = [{"play_count": n, "create_time": FIXTURE_NOW - d * day} for n, d in
            [(1000, 1), (3000, 10), (2000, 20), (500, 60)]]
    s = metrics.view_summary(vids, now=FIXTURE_NOW)
    assert s["window"] == "last 30 days" and s["n"] == 3 and s["avg_views"] == 2000
    s = metrics.view_summary(vids[2:], now=FIXTURE_NOW)
    assert s["window"] == "last 90 days" and s["n"] == 2 or s["window"].startswith("last 2 videos")
    assert metrics.fmt_count(18_400) == "18K" and metrics.fmt_count(1_250) == "1.2K"


def test_sc_channel_links(fixture):
    info = yt.parse_sc_channel(fixture("youtube_channel_channel_a"))
    assert info["links"]["instagram"] and info["email"] is None


def test_partner_matching_by_normalised_name():
    p = Partner(key="Mari Mängib Palju", market="EE", country="Estonia",
                names={norm("Mari Mängib Palju")})
    idx = PartnerIndex([p])
    assert idx.match({"handle": "marimngibpalju", "nickname": "Mari"}) is p
    assert idx.match({"handle": "mari_mngib_palju_tv"}) is p
    assert idx.match({"handle": "marimn"}) is None  # too short to be a safe match


def test_linked_accounts_merge_into_one_row(store):
    store.upsert_creator("tiktok", "t1", handle="examplegamer", nickname="Example Gamer", followers=4800)
    store.upsert_creator("youtube", "UC1", handle="examplegamer", nickname="Example Gamer",
                         followers=50500, region="EE", region_source="channel")
    store.set_screening("EE", "youtube", "UC1", "accepted", bucket="sure", run_id=1)
    store.link_identities(("tiktok", "t1"), ("youtube", "UC1"), "test")
    groups = group_accounts(store, "EE", store.screenings("EE", run_id=1))
    assert len(groups) == 1 and len(groups[0]) == 2
    row = creator_row(store, "EE", groups[0])
    assert row["platforms"] == "TikTok + YouTube" and row["country"] == "Estonia"
    assert row["followers_tiktok"] == 4800 and row["subscribers_youtube"] == 50500
