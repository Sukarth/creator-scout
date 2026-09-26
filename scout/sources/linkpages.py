"""Fetch a creator's bio link (Linktree, Beacons, direct.me, personal site) for contacts.

Uses plain HTTP and costs no API credits. Failures are expected and silent: a
missing contact is recorded as such, never raised.
"""

from __future__ import annotations

import html
import re
from urllib.parse import unquote, urlparse

import httpx

from ..signals import EMAIL_RE, extract_links

MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
# Domains whose pages are login-walled or never carry creator contact details.
# Links to them are still recorded from the URL itself.
SKIP_DOMAINS = {"tiktok.com", "vm.tiktok.com", "instagram.com", "facebook.com", "youtube.com",
                "youtu.be", "twitch.tv", "x.com", "twitter.com", "discord.gg", "discord.com"}
IGNORED_EMAIL_DOMAINS = {"sentry.io", "example.com", "wixpress.com", "linktr.ee", "beacons.ai"}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def normalise_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def fetch_contacts(url: str | None, timeout: float = 10.0) -> dict:
    """Return ``{"emails": [...], "links": {...}, "ok": bool}`` for a bio link."""
    result: dict = {"emails": [], "links": {}, "ok": False}
    if not url:
        return result
    url = normalise_url(url)
    result["links"] = extract_links(url)
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    if host in SKIP_DOMAINS:
        result["ok"] = bool(result["links"])
        return result
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError:
        return result
    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
        return result
    body = html.unescape(resp.text[:500_000])
    emails: dict[str, None] = {}
    for m in MAILTO_RE.findall(body):
        emails.setdefault(unquote(m).lower(), None)
    for m in EMAIL_RE.findall(body):
        emails.setdefault(m.lower(), None)
    result["emails"] = [e for e in emails
                        if e.split("@")[-1] not in IGNORED_EMAIL_DOMAINS
                        and not e.endswith((".png", ".jpg", ".webp", ".svg", ".gif"))][:5]
    hrefs = " ".join(HREF_RE.findall(body))
    page_links = extract_links(hrefs)
    page_links.update(result["links"])
    result["links"] = page_links
    result["ok"] = True
    return result
