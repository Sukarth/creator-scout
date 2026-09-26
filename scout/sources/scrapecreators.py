"""ScrapeCreators API client.

Every request is resolved against the local cache first. Live requests are
metered: the credit meter refuses a call once the run budget is spent, and the
``credits_charged`` / ``credits_remaining`` fields returned by the API are
recorded so usage can be reported exactly.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..store import Store

BASE_URL = "https://api.scrapecreators.com"


class BudgetExhausted(RuntimeError):
    """Raised when a live call would exceed the run's credit budget."""


class ApiError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


@dataclass
class CreditMeter:
    """Tracks credits spent against a hard budget for one run."""

    budget: int
    used: int = 0
    live_calls: int = 0
    cache_hits: int = 0
    remaining_on_account: int | None = None
    on_change: Callable[["CreditMeter"], None] | None = field(default=None, repr=False)

    @property
    def left(self) -> int:
        return max(self.budget - self.used, 0)

    def can_spend(self, credits: int = 1) -> bool:
        return self.used + credits <= self.budget

    def record_live(self, credits: int, remaining: int | None) -> None:
        self.used += credits
        self.live_calls += 1
        if remaining is not None:
            self.remaining_on_account = remaining
        if self.on_change:
            self.on_change(self)

    def record_hit(self) -> None:
        self.cache_hits += 1
        if self.on_change:
            self.on_change(self)


def cache_key(endpoint: str, params: dict) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    raw = endpoint + "?" + json.dumps(clean, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ScrapeCreators:
    """Thin cached client. Methods return the decoded JSON body."""

    def __init__(self, store: Store, api_key: str | None, meter: CreditMeter,
                 timeout: float = 60.0, max_retries: int = 2, offline: bool = False):
        self.store = store
        self.api_key = api_key
        self.meter = meter
        self.run_id: int | None = None  # recorded with each paid response
        self.offline = offline or not api_key
        self.max_retries = max_retries
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout,
                                  headers={"x-api-key": api_key or ""})

    def close(self) -> None:
        self._http.close()

    def get(self, endpoint: str, params: dict | None = None,
            max_age: float | None = 7 * 24 * 3600, expected_cost: int = 1) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = cache_key(endpoint, params)
        cached = self.store.cache_get(key, max_age)
        if cached is not None:
            self.meter.record_hit()
            return self._unwrap(cached["status_code"], cached["response"])
        if self.offline:
            raise ApiError(0, f"offline and not cached: {endpoint} {params}")
        if not self.meter.can_spend(expected_cost):
            raise BudgetExhausted(f"budget {self.meter.budget} reached ({self.meter.used} used)")

        status, body = self._request(endpoint, params)
        charged = _as_int(body.get("credits_charged")) if isinstance(body, dict) else None
        remaining = _as_int(body.get("credits_remaining")) if isinstance(body, dict) else None
        if charged is None:
            charged = expected_cost if status < 400 else 0
        self.meter.record_live(charged, remaining)
        if remaining is not None:
            self.store.meta_set("credits_remaining", remaining)
        total = int(self.store.meta_get("credits_used_total", "0")) + charged
        self.store.meta_set("credits_used_total", total)
        # Client errors such as "user not found" are cached too so the same
        # miss is never paid for twice. Server errors are not cached.
        if status < 500:
            self.store.cache_put(key, endpoint, params, status, body, charged, run_id=self.run_id)
        return self._unwrap(status, body)

    def _request(self, endpoint: str, params: dict) -> tuple[int, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.get(endpoint, params=params)
                try:
                    body = resp.json()
                except ValueError:
                    body = {"error": resp.text[:500]}
                if resp.status_code >= 500 and attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return resp.status_code, body
            except httpx.TransportError as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        raise ApiError(0, f"transport error: {last_exc}")

    @staticmethod
    def _unwrap(status: int, body: Any) -> dict:
        if status >= 400:
            msg = body.get("message") or body.get("error") if isinstance(body, dict) else str(body)
            raise ApiError(status, str(msg))
        return body if isinstance(body, dict) else {"data": body}

    # ---- TikTok endpoints -------------------------------------------------

    # ``region`` on search endpoints places the provider's proxy in that country.
    # It biases results toward local content but does not filter by country.

    def tiktok_hashtag(self, hashtag: str, cursor: int | str | None = None,
                       region: str | None = None, max_age: float | None = 24 * 3600) -> dict:
        return self.get("/v1/tiktok/search/hashtag",
                        {"hashtag": hashtag.lstrip("#"), "cursor": cursor, "region": region}, max_age)

    def tiktok_keyword(self, query: str, cursor: int | str | None = None,
                       date_posted: str | None = "last-6-months", sort_by: str = "relevance",
                       region: str | None = None, max_age: float | None = 24 * 3600) -> dict:
        return self.get("/v1/tiktok/search/keyword",
                        {"query": query, "date_posted": date_posted, "sort_by": sort_by,
                         "cursor": cursor, "region": region}, max_age)

    def tiktok_search_users(self, query: str, cursor: int | str | None = None,
                            max_age: float | None = 7 * 24 * 3600) -> dict:
        return self.get("/v1/tiktok/search/users", {"query": query, "cursor": cursor}, max_age)

    def tiktok_profile(self, handle: str, max_age: float | None = 7 * 24 * 3600) -> dict:
        return self.get("/v1/tiktok/profile", {"handle": handle.lstrip("@")}, max_age)

    def tiktok_region(self, handle: str, max_age: float | None = 30 * 24 * 3600) -> dict:
        return self.get("/v1/tiktok/profile/region", {"handle": handle.lstrip("@")}, max_age)

    def tiktok_videos(self, handle: str, sort_by: str = "latest",
                      max_cursor: int | str | None = None,
                      max_age: float | None = 7 * 24 * 3600) -> dict:
        return self.get("/v3/tiktok/profile/videos",
                        {"handle": handle.lstrip("@"), "sort_by": sort_by,
                         "max_cursor": max_cursor}, max_age)

    def tiktok_following(self, handle: str, min_time: int | str | None = None,
                         max_age: float | None = 7 * 24 * 3600) -> dict:
        return self.get("/v1/tiktok/user/following",
                        {"handle": handle.lstrip("@"), "min_time": min_time}, max_age)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
