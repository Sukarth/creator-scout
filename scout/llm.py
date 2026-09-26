"""OpenAI-compatible chat client over free LLM tiers with rotation and fallback.

Providers are tried in order. Each Groq model has its own per-minute token
budget (8k on the free plan), so requests rotate across models and a local
sliding window keeps each model under its limit; a 429 puts the model on
cooldown for the time the server asks. Responses must be JSON objects: invalid
JSON is retried once on the same model with the parse error, then the next
model is tried. Successful responses are cached by prompt hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from .store import Store


class LLMUnavailable(RuntimeError):
    """No provider could answer the request."""


@dataclass
class Model:
    provider: str
    base_url: str
    key_env: str
    name: str
    tpm: int = 8000
    extra: dict = field(default_factory=dict)
    window: deque = field(default_factory=deque)  # (timestamp, tokens)
    cooldown_until: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.name}"

    def tokens_last_minute(self, now: float) -> int:
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        return sum(t for _, t in self.window)

    def has_room(self, tokens: int, now: float) -> bool:
        return now >= self.cooldown_until and self.tokens_last_minute(now) + tokens <= self.tpm * 0.95


GROQ = "https://api.groq.com/openai/v1"
OPENCODE = "https://opencode.ai/zen/v1"


def default_models() -> dict[str, Model]:
    return {
        "gpt-oss-120b": Model("groq", GROQ, "GROQ_API_KEY", "openai/gpt-oss-120b",
                              extra={"reasoning_effort": "low"}),
        "qwen3.8-27b": Model("groq", GROQ, "GROQ_API_KEY", "qwen/qwen3.8-27b",
                             extra={"reasoning_effort": "none"}),
        "gpt-oss-20b": Model("groq", GROQ, "GROQ_API_KEY", "openai/gpt-oss-20b",
                             extra={"reasoning_effort": "low"}),
        "space-bunny": Model("opencode", OPENCODE, "OPENCODE_API_KEY", "space-bunny-free",
                             tpm=10**9),
    }


# Model preference per task. Fast non-reasoning models first for bulk triage.
ROUTES = {
    "prejudge": ["qwen3.8-27b", "gpt-oss-20b", "gpt-oss-120b", "space-bunny"],
    "judge": ["gpt-oss-120b", "qwen3.8-27b", "gpt-oss-20b", "space-bunny"],
    "pitch": ["gpt-oss-120b", "qwen3.8-27b", "gpt-oss-20b", "space-bunny"],
    "keywords": ["gpt-oss-120b", "qwen3.8-27b", "space-bunny"],
}


def estimate_tokens(text: str) -> int:
    """Conservative token estimate; Estonian and Cyrillic text tokenise densely."""
    return int(len(text) / 2.8) + 20


def parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise json.JSONDecodeError("expected a JSON object", text, 0)
    return value


@dataclass
class LLMStats:
    calls: int = 0
    cache_hits: int = 0
    tokens: int = 0
    failures: int = 0
    by_model: dict = field(default_factory=dict)


class LLMClient:
    def __init__(self, store: Store | None = None, models: dict[str, Model] | None = None,
                 timeout: float = 90.0, max_wait: float = 75.0,
                 http: httpx.Client | None = None):
        self.store = store
        self.models = models or default_models()
        self.timeout = timeout
        self.max_wait = max_wait
        self.stats = LLMStats()
        self._http = http or httpx.Client(timeout=timeout)

    def available(self) -> bool:
        return any(os.environ.get(m.key_env) for m in self.models.values())

    def close(self) -> None:
        self._http.close()

    def chat_json(self, task: str, system: str, user: str, max_tokens: int = 1500,
                  temperature: float = 0.2, use_cache: bool = True) -> tuple[dict, str]:
        """Return ``(parsed_json, model_label)``."""
        key = hashlib.sha256(json.dumps([task, system, user, max_tokens, temperature],
                                        ensure_ascii=False).encode()).hexdigest()
        if use_cache and self.store:
            cached = self.store.llm_cache_get(key)
            if cached:
                self.stats.cache_hits += 1
                return cached["response"], cached["model"]

        need = estimate_tokens(system + user) + max_tokens
        route = [self.models[n] for n in ROUTES.get(task, ROUTES["judge"])
                 if n in self.models and os.environ.get(self.models[n].key_env)]
        if not route:
            raise LLMUnavailable("no LLM API key configured (GROQ_API_KEY or OPENCODE_API_KEY)")
        deadline = time.time() + self.max_wait
        errors: list[str] = []
        while True:
            now = time.time()
            ready = [m for m in route if m.has_room(need, now)]
            if not ready:
                if now >= deadline:
                    break
                time.sleep(2)
                continue
            for model in ready:
                result = self._try_model(model, system, user, max_tokens, temperature, errors)
                if result is not None:
                    if self.store:
                        self.store.llm_cache_put(key, task, model.label, result)
                    return result, model.label
            if time.time() >= deadline:
                break
        self.stats.failures += 1
        raise LLMUnavailable("; ".join(errors[-6:]) or "all models rate limited")

    def _try_model(self, model: Model, system: str, user: str, max_tokens: int,
                   temperature: float, errors: list[str]) -> dict | None:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(2):
            status, body, headers = self._post(model, messages, max_tokens, temperature)
            if status == 429:
                model.cooldown_until = time.time() + _retry_after(headers, default=20.0)
                errors.append(f"{model.label}: rate limited")
                return None
            if status != 200:
                errors.append(f"{model.label}: HTTP {status} {str(body)[:120]}")
                model.cooldown_until = time.time() + 10
                return None
            usage = body.get("usage") or {}
            used = int(usage.get("total_tokens") or estimate_tokens(system + user))
            model.window.append((time.time(), used))
            self.stats.calls += 1
            self.stats.tokens += used
            self.stats.by_model[model.label] = self.stats.by_model.get(model.label, 0) + 1
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            try:
                return parse_json_object(content)
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append(f"{model.label}: invalid JSON ({exc})")
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": content[:2000]},
                        {"role": "user", "content": f"That was not valid JSON ({exc}). "
                                                    "Reply with the corrected JSON object only."}]
        return None

    def _post(self, model: Model, messages: list[dict], max_tokens: int,
              temperature: float) -> tuple[int, Any, dict]:
        payload = {"model": model.name, "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "response_format": {"type": "json_object"},
                   **model.extra}
        try:
            resp = self._http.post(f"{model.base_url}/chat/completions", json=payload,
                                   headers={"Authorization": f"Bearer {os.environ[model.key_env]}"})
        except httpx.HTTPError as exc:
            return 0, str(exc), {}
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:300]
        return resp.status_code, body, dict(resp.headers)


def _retry_after(headers: dict, default: float) -> float:
    for key in ("retry-after", "x-ratelimit-reset-tokens"):
        value = headers.get(key)
        if not value:
            continue
        try:
            return min(float(value), 60.0)
        except ValueError:
            m = re.match(r"(?:(\d+)m)?([\d.]+)s", value)
            if m:
                return min(int(m.group(1) or 0) * 60 + float(m.group(2)), 60.0)
    return default
