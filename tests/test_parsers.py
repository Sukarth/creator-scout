from scout.sources import tiktok as tt


def test_hashtag_counts_are_unknown(fixture):
    pairs, cursor, has_more = tt.parse_hashtag(fixture("tiktok_hashtag_eestitiktok"), "eestitiktok")
    assert len(pairs) == 20 and has_more and cursor == 20
    acc, video = pairs[1]
    assert acc["handle"] == "hundijalavesi" and acc["region"] == "EE"
    assert acc["followers"] is None  # hashtag results report 0, meaning "not provided"
    assert video["source"] == "hashtag:eestitiktok" and video["play_count"] is not None


def test_keyword_has_followers_and_video_region(fixture):
    pairs, _, _ = tt.parse_keyword(fixture("tiktok_keyword_manguarvuti"), "mänguarvuti")
    assert len(pairs) == 30
    acc, video = pairs[0]
    assert acc["region"] is None and acc["followers"] == 64
    assert video["region"] == "BY"


def test_following_inline_fields(fixture):
    accounts, min_time, has_more, total = tt.parse_following(fixture("tiktok_following_jimmspc"))
    assert len(accounts) == 20 and has_more and total == 132 and min_time
    digi = next(a for a in accounts if a["handle"] == "digikamu")
    assert digi["region"] == "FI" and digi["followers"] == 32565
    assert "creatorbd06af@example.fi" in digi["bio"]


def test_hidden_following_list(fixture):
    accounts, _, has_more, total = tt.parse_following(fixture("tiktok_following_nuvoosuomi"))
    assert accounts == [] and not has_more and total == 0


def test_profile(fixture):
    p = tt.parse_profile(fixture("tiktok_profile_digikamu"))
    assert p["handle"] == "digikamu" and p["followers"] == 32571 and p["language"] == "fi"
    assert p["bio_link"] == "https://logi.gg/LGPFALL_FY27_DIGIKAMU"
    assert not p["is_private"] and not p["is_organization"]


def test_videos_flag_paid_partnerships(fixture):
    vids, max_cursor, has_more = tt.parse_videos(fixture("tiktok_videos_digikamu"))
    assert len(vids) == 10 and has_more and max_cursor
    assert sum(v["is_ad"] for v in vids) == 3


def test_region_lookup(fixture):
    assert tt.parse_region(fixture("tiktok_region_ossiteks")) == "FI"


def test_user_search(fixture):
    handles = [a["handle"] for a in tt.parse_users(fixture("tiktok_search_users_nuvoo"))]
    assert "nuvoosuomi" in handles
