import json
from pathlib import Path

import pytest

from scout.sources.scrapecreators import cache_key
from scout.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

# Fixture file -> (endpoint, params) exactly as the client issues the call.
CACHED_CALLS = {
    "tiktok_hashtag_eestitiktok": ("/v1/tiktok/search/hashtag", {"hashtag": "eestitiktok"}),
    "tiktok_keyword_manguarvuti": ("/v1/tiktok/search/keyword",
                                   {"query": "mänguarvuti", "date_posted": "last-6-months",
                                    "sort_by": "relevance"}),
    "tiktok_profile_digikamu": ("/v1/tiktok/profile", {"handle": "digikamu"}),
    "tiktok_region_ossiteks": ("/v1/tiktok/profile/region", {"handle": "ossiteks"}),
    "tiktok_videos_digikamu": ("/v3/tiktok/profile/videos", {"handle": "digikamu", "sort_by": "latest"}),
    "tiktok_following_jimmspc": ("/v1/tiktok/user/following", {"handle": "jimmspc"}),
    "tiktok_following_nuvoosuomi": ("/v1/tiktok/user/following", {"handle": "nuvoosuomi"}),
    "tiktok_search_users_nuvoo": ("/v1/tiktok/search/users", {"query": "nuvoo"}),
}

# Fixed clock just after the newest fixture video, so activity filters are stable.
FIXTURE_NOW = 1790500000


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def fixture():
    return load_fixture


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def cached_store(store):
    """A store whose API cache holds every saved fixture."""
    for name, (endpoint, params) in CACHED_CALLS.items():
        store.cache_put(cache_key(endpoint, params), endpoint, params, 200,
                        load_fixture(name), credits_charged=1)
    return store
