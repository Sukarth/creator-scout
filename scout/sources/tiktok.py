"""Normalise TikTok responses from ScrapeCreators into plain account and video dicts.

The same author object appears in several shapes (hashtag results, keyword
results, following lists, profile). These helpers map each shape onto one set
of keys so the pipeline never touches raw payloads.

Known quirks:
- Hashtag results report ``follower_count`` and ``aweme_count`` as 0; those
  zeros mean "not provided" and are returned as ``None``.
- Keyword results omit the author ``region`` and ``signature``; the video's own
  ``region`` is still present and is kept as a weaker market hint.
- The profile endpoint does not return a region.
"""

from __future__ import annotations

from typing import Any

PLATFORM = "tiktok"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def account_from_author(author: dict, counts_reliable: bool = True) -> dict:
    """Map an inline author/user object (search results, following lists)."""
    followers = _int(author.get("follower_count"))
    videos = _int(author.get("aweme_count"))
    if not counts_reliable:
        followers = followers or None
        videos = videos or None
    return {
        "uid": str(author.get("uid") or author.get("id") or ""),
        "handle": _str(author.get("unique_id")),
        "sec_uid": _str(author.get("sec_uid")),
        "nickname": _str(author.get("nickname")),
        "region": (_str(author.get("region")) or "").upper() or None,
        "language": _str(author.get("language")),
        "followers": followers,
        "following": _int(author.get("following_count")) if counts_reliable else None,
        "videos": videos,
        "hearts": _int(author.get("total_favorited")) if counts_reliable else None,
        "bio": author.get("signature") if author.get("signature") is not None else None,
        "is_private": bool(author.get("secret")) if author.get("secret") is not None else None,
        "ins_id": _str(author.get("ins_id")),
        "youtube_channel_id": _str(author.get("youtube_channel_id")),
        "enterprise_reason": _str(author.get("enterprise_verify_reason")),
    }


def video_from_aweme(aweme: dict, source: str) -> dict:
    stats = aweme.get("statistics") or {}
    commerce = aweme.get("commerce_info") or {}
    is_ad = bool(aweme.get("is_paid_partnership")) or bool(aweme.get("is_ads")) \
        or commerce.get("branded_content_type") == 1
    author = aweme.get("author") or {}
    return {
        "video_id": str(aweme.get("aweme_id")),
        "uid": str(author.get("uid") or aweme.get("author_user_id") or ""),
        "caption": aweme.get("desc") or "",
        "create_time": _int(aweme.get("create_time")),
        "play_count": _int(stats.get("play_count")),
        "digg_count": _int(stats.get("digg_count")),
        "comment_count": _int(stats.get("comment_count")),
        "share_count": _int(stats.get("share_count")),
        "is_ad": is_ad,
        "is_pinned": bool(aweme.get("is_top")),
        "region": (_str(aweme.get("region")) or "").upper() or None,
        "caption_language": _str(aweme.get("desc_language")),
        "source": source,
    }


def parse_hashtag(body: dict, tag: str) -> tuple[list[tuple[dict, dict]], Any, bool]:
    """Return ``([(account, video), ...], next_cursor, has_more)``."""
    out = []
    for aweme in body.get("aweme_list") or []:
        author = aweme.get("author") or {}
        out.append((account_from_author(author, counts_reliable=False),
                    video_from_aweme(aweme, f"hashtag:{tag}")))
    return out, body.get("cursor"), bool(body.get("has_more"))


def parse_keyword(body: dict, query: str) -> tuple[list[tuple[dict, dict]], Any, bool]:
    out = []
    for item in body.get("search_item_list") or []:
        aweme = item.get("aweme_info") or {}
        author = aweme.get("author") or {}
        out.append((account_from_author(author), video_from_aweme(aweme, f"keyword:{query}")))
    return out, body.get("cursor"), bool(body.get("has_more"))


def parse_following(body: dict) -> tuple[list[dict], Any, bool, int | None]:
    """Return ``(accounts, next_min_time, has_more, total)``."""
    accounts = [account_from_author(u) for u in body.get("followings") or []]
    return accounts, body.get("min_time"), bool(body.get("has_more")), _int(body.get("total"))


def parse_users(body: dict) -> list[dict]:
    return [account_from_author(u.get("user_info") or {}) for u in body.get("user_list") or []]


def parse_profile(body: dict) -> dict:
    user = body.get("user") or {}
    stats = body.get("statsV2") or body.get("stats") or {}
    commerce = user.get("commerceUserInfo") or {}
    bio_link = (user.get("bioLink") or {}).get("link")
    return {
        "uid": str(user.get("id") or ""),
        "handle": _str(user.get("uniqueId")),
        "sec_uid": _str(user.get("secUid")),
        "nickname": _str(user.get("nickname")),
        "language": _str(user.get("language")),
        "followers": _int(stats.get("followerCount")),
        "following": _int(stats.get("followingCount")),
        "videos": _int(stats.get("videoCount")),
        "hearts": _int(stats.get("heartCount") or stats.get("heart")),
        "bio": user.get("signature") or "",
        "bio_link": _str(bio_link),
        "is_private": bool(user.get("privateAccount")),
        "is_organization": bool(user.get("isOrganization")),
        "is_commerce": bool(commerce.get("commerceUser")) or bool(user.get("ttSeller")),
        "following_visibility": user.get("followingVisibility"),
        "verified": bool(user.get("verified")),
    }


def parse_videos(body: dict) -> tuple[list[dict], Any, bool]:
    vids = [video_from_aweme(a, "profile_videos") for a in body.get("aweme_list") or []]
    return vids, body.get("max_cursor"), bool(body.get("has_more"))


def parse_region(body: dict) -> str | None:
    return (_str(body.get("region")) or "").upper() or None
