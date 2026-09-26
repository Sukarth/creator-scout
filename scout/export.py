"""XLSX and CSV export of a run's results.

One row per creator: TikTok and YouTube accounts linked by bio links or
channel links are merged. The client's required columns (country,
followers/subscribers, average views with window and video count, niche and
games, contact) come first; risks and view trend follow. A second layout,
"Prenew format", mirrors the client's own collaboration sheet.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import metrics as metrics_mod
from .store import Store

PLATFORM = "tiktok"
YOUTUBE = "youtube"
OUTREACH_STATUSES = ["to contact", "contacted", "replied", "deal", "declined"]
COUNTRY_NAMES = {"EE": "Estonia", "FI": "Finland", "SE": "Sweden", "DE": "Germany", "PL": "Poland",
                 "LV": "Latvia", "LT": "Lithuania", "DK": "Denmark", "NO": "Norway", "NL": "Netherlands",
                 "FR": "France", "HU": "Hungary", "AT": "Austria", "BE": "Belgium", "CZ": "Czechia",
                 "US": "United States", "GB": "United Kingdom", "RU": "Russia", "UA": "Ukraine"}

SHORTLIST_COLUMNS = [
    "status", "creator", "platforms", "market", "country", "existing_partner",
    "followers_tiktok", "avg_views_tiktok", "views_window_tiktok", "views_range_tiktok",
    "subscribers_youtube", "avg_views_youtube", "views_window_youtube", "views_range_youtube",
    "avg_views_youtube_shorts", "views_window_youtube_shorts",
    "niche", "games", "contact", "risks", "trend",
    "decision", "fit_score", "young_gamer_appeal", "gaming_pc_relevance", "reasons",
    "evidence_quote", "tiktok_url", "youtube_url", "other_links", "market_confidence",
    "market_evidence", "sponsors_mentioned", "found_via", "prejudge",
    "suggested_deal", "price_low_eur", "price_high_eur",
    "pitch_language", "pitch_subject", "pitch_body", "dm_text", "first_seen",
]

PRENEW_COLUMNS = ["Creator key", "Market", "Country", "Creator / channel", "Platform",
                  "Niche / content", "YT subscribers", "YT views / video", "TikTok followers",
                  "TikTok views / video", "YT avg views (window)", "TikTok avg views (window)",
                  "Contact", "Existing partner", "Links"]


def profile_url(handle: str | None, platform: str = PLATFORM, uid: str | None = None) -> str:
    if platform == YOUTUBE:
        return f"https://www.youtube.com/@{handle}" if handle else (
            f"https://www.youtube.com/channel/{uid}" if uid else "")
    return f"https://www.tiktok.com/@{handle}" if handle else ""


def _date(ts: float | int | None) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") if ts else ""


SOURCE_LABELS = {
    "hashtag": "#{via}", "keyword": "TikTok search '{via}'", "following": "followed by @{via}",
    "retailer_search": "user search '{via}'", "yt_search": "YouTube search '{via}'",
    "tiktok_link": "linked from TikTok @{via}", "youtube_link": "linked from YouTube {via}",
}


def source_label(kind: str, via: str) -> str:
    return SOURCE_LABELS.get(kind, "{kind}:{via}").format(kind=kind, via=via)


def found_via(store: Store, uid: str, limit: int = 4, platform: str = PLATFORM) -> str:
    parts: list[str] = []
    for e in store.edges_to(platform, uid):
        label = source_label(e["kind"], e["via"])
        if label not in parts:
            parts.append(label)
    more = f" (+{len(parts) - limit} more)" if len(parts) > limit else ""
    return "; ".join(parts[:limit]) + more


def _decision(store: Store, market: str, platform: str, uid: str) -> dict:
    d = store.get_decision(market, platform, uid)
    if d is None:
        return {}
    data = dict(d["data"])
    data.setdefault("decision", d["decision"])
    data.setdefault("fit_score", d["fit_score"])
    return data


def _views(summary: dict | None) -> tuple:
    if not summary or summary.get("avg_views") is None:
        return None, "", "", None
    return (summary["avg_views"], f"{summary['window']}, {summary['n']} videos",
            summary.get("range") or "", summary.get("trend"))


def account_facts(store: Store, market: str, platform: str, uid: str) -> dict:
    """Everything known about one platform account, for merging into a creator row."""
    c = store.get_creator(platform, uid) or {}
    m = store.get_metrics(platform, uid) or {}
    s = store.get_screening(market, platform, uid) or {}
    return {"platform": platform, "uid": uid, "creator": c, "metrics": m, "screening": s,
            "decision": _decision(store, market, platform, uid),
            "pitch": store.get_pitch(market, platform, uid) or {},
            "prejudge": store.get_prejudgment(market, platform, uid) or {}}


def creator_row(store: Store, market: str, accounts: list[dict], partners=None) -> dict:
    """Merge the accounts of one creator into one row."""
    by_platform = {a["platform"]: a for a in accounts}
    tk, ytb = by_platform.get(PLATFORM), by_platform.get(YOUTUBE)
    judged = [a for a in accounts if a["decision"]]
    primary = max(judged or accounts, key=lambda a: (a["screening"].get("status") == "accepted",
                                                     a["decision"].get("fit_score") or 0))
    d, s = primary["decision"], primary["screening"]
    row: dict = {k: None for k in SHORTLIST_COLUMNS}
    row["status"] = ""
    row["creator"] = (primary["creator"].get("nickname") or primary["creator"].get("handle") or "")
    row["platforms"] = " + ".join(p for p, a in (("TikTok", tk), ("YouTube", ytb)) if a)
    row["accounts"] = [(a["platform"], a["uid"]) for a in accounts]
    row["market"] = d.get("market_resolution") or (market if s.get("bucket") == "sure" else f"{market}?")
    country = (ytb or {}).get("creator", {}).get("region") if ytb else None
    country = country or next((a["creator"].get("region") for a in accounts
                               if a["creator"].get("region_source") in ("lookup", "inline")
                               and a["creator"].get("region")), None)
    row["country"] = COUNTRY_NAMES.get(country or "", country or "")
    risks: list[str] = []
    views_trend: list[str] = []
    if tk:
        c, m = tk["creator"], tk["metrics"]
        row["followers_tiktok"] = c.get("followers")
        avg, win, rng, trend = _views(m.get("views"))
        row.update(avg_views_tiktok=avg, views_window_tiktok=win, views_range_tiktok=rng)
        if trend is not None:
            views_trend.append(f"TikTok {metrics_mod.trend_label(trend)}")
        row["tiktok_url"] = profile_url(c.get("handle"))
    if ytb:
        c, m = ytb["creator"], ytb["metrics"]
        row["subscribers_youtube"] = c.get("followers")
        avg, win, rng, trend = _views(m.get("long"))
        row.update(avg_views_youtube=avg, views_window_youtube=win, views_range_youtube=rng)
        savg, swin, _, strend = _views(m.get("shorts"))
        row.update(avg_views_youtube_shorts=savg, views_window_youtube_shorts=swin)
        for label, t in (("YouTube", trend), ("Shorts", strend)):
            if t is not None:
                views_trend.append(f"{label} {metrics_mod.trend_label(t)}")
        row["youtube_url"] = profile_url(c.get("handle"), YOUTUBE, ytb["uid"])
    row["trend"] = "; ".join(views_trend)

    niche = d.get("niche_category") or ", ".join(d.get("niche_tags") or [])
    row["niche"] = niche
    row["games"] = ", ".join(d.get("games") or [])
    emails = list(dict.fromkeys(e for a in accounts for e in (a["creator"].get("emails") or [])))
    row["contact"] = ", ".join(emails) or "no email found"
    links: dict = {}
    for a in accounts:
        links.update(a["creator"].get("links") or {})
    row["other_links"] = ", ".join(f"{k}: {v}" for k, v in links.items())

    competitors = sorted({x for a in accounts for x in (a["metrics"].get("competitors") or [])})
    if competitors or d.get("competitor_conflict"):
        risks.append("competitor sponsorship: " + (", ".join(competitors) or "flagged by judge"))
    risks += [f"brand safety: {f}" for f in d.get("brand_safety_flags") or []]
    days = [a["metrics"].get("days_since_last_post") for a in accounts
            if a["metrics"].get("days_since_last_post") is not None]
    if days and min(days) > 30:
        risks.append(f"inactive {int(min(days))} days")
    row["risks"] = "; ".join(risks)

    row["decision"] = d.get("decision") or ("not judged" if s.get("status") == "needs_judgment"
                                            else s.get("status"))
    row["fit_score"] = d.get("fit_score")
    row["young_gamer_appeal"] = d.get("young_gamer_appeal")
    row["gaming_pc_relevance"] = d.get("gaming_pc_relevance")
    row["reasons"] = d.get("reasons")
    row["evidence_quote"] = d.get("evidence_quote")
    row["market_confidence"] = {"sure": "high", "unsure": "medium"}.get(s.get("bucket"), "low")
    row["market_evidence"] = "; ".join(s.get("market_evidence") or [])
    row["sponsors_mentioned"] = ", ".join(sorted({x for a in accounts for x in
                                                  (a["metrics"].get("sponsors") or [])}
                                                 | set(d.get("sponsors_mentioned") or [])))
    row["found_via"] = "; ".join(found_via(store, a["uid"], 3, a["platform"]) for a in accounts)
    pj = primary["prejudge"]
    row["prejudge"] = f"{pj['verdict']}: {pj.get('reason') or ''}".strip(": ") if pj else ""
    followers = (tk or primary)["creator"].get("followers")
    m = primary["metrics"]
    row["suggested_deal"] = metrics_mod.suggest_deal(followers, m.get("old_hardware"))
    price = metrics_mod.price_estimate((m.get("views") or {}).get("avg_views") or m.get("median_views"))
    row["price_low_eur"], row["price_high_eur"] = (price if price else (None, None))
    pitch = primary["pitch"]
    row["pitch_language"] = pitch.get("language")
    row["pitch_subject"], row["pitch_body"], row["dm_text"] = (pitch.get("subject"), pitch.get("body"),
                                                               pitch.get("dm"))
    row["content_language"] = d.get("content_language")
    row["first_seen"] = _date(min((a["creator"].get("first_seen_at") or 0) for a in accounts) or None)
    partner = None
    if partners is not None:
        for a in accounts:
            partner = partners.match(a["creator"])
            if partner:
                break
    row["existing_partner"] = f"yes ({partner.key})" if partner else ""
    row["partner_key"] = partner.key if partner else None
    return row


def group_accounts(store: Store, market: str, screenings: list[dict]) -> list[list[dict]]:
    """Group screened accounts into creators via identity links."""
    groups: list[list[dict]] = []
    placed: set[tuple[str, str]] = set()
    for s in screenings:
        key = (s["platform"], s["uid"])
        if key in placed:
            continue
        members = [key] + [(p, u) for p, u, _ in store.linked_accounts(*key)]
        accounts = []
        for p, u in members:
            if (p, u) in placed:
                continue
            placed.add((p, u))
            if store.get_creator(p, u) is not None:
                accounts.append(account_facts(store, market, p, u))
        if accounts:
            groups.append(accounts)
    return groups


def build_sheets(store: Store, run_id: int) -> dict[str, list[dict]]:
    from .partners import PartnerIndex, load_partners

    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    market = run["market"]
    partners = PartnerIndex(load_partners())
    scr = store.screenings(market, run_id=run_id)
    judged_run = (run["params"] or {}).get("judge", "none") != "none"

    def rows(statuses: tuple[str, ...]) -> list[dict]:
        chosen = [s for s in scr if s["status"] in statuses]
        out = [creator_row(store, market, g, partners) for g in group_accounts(store, market, chosen)]
        out.sort(key=lambda r: (-(r["fit_score"] or 0), -((r["followers_tiktok"] or 0)
                                                          + (r["subscribers_youtube"] or 0))))
        return out

    if judged_run:
        shortlist, maybe = rows(("accepted",)), rows(("maybe", "needs_judgment"))
    else:
        shortlist, maybe = rows(("accepted", "needs_judgment")), rows(("maybe",))
    existing = [r for r in shortlist + maybe if r["partner_key"]]
    shortlist = [r for r in shortlist if not r["partner_key"]]
    maybe = [r for r in maybe if not r["partner_key"]]

    other = []
    for s in scr:
        if s["status"] != "other_market":
            continue
        c = store.get_creator(s["platform"], s["uid"]) or {}
        other.append({"platform": s["platform"], "handle": c.get("handle"),
                      "creator": c.get("nickname"),
                      "profile_url": profile_url(c.get("handle"), s["platform"], s["uid"]),
                      "region": c.get("region") or "unknown", "region_source": c.get("region_source"),
                      "language": c.get("language"), "followers": c.get("followers"),
                      "bio": c.get("bio"), "found_via": found_via(store, s["uid"], platform=s["platform"])})
    other.sort(key=lambda r: (r["region"], -(r["followers"] or 0)))

    everyone = []
    for s in scr:
        c = store.get_creator(s["platform"], s["uid"]) or {}
        everyone.append({"platform": s["platform"], "handle": c.get("handle"),
                         "creator": c.get("nickname"), "status": s["status"], "reason": s["reason"],
                         "bucket": s["bucket"], "region": c.get("region"),
                         "followers": c.get("followers"),
                         "market_evidence": "; ".join(s.get("market_evidence") or []),
                         "found_via": found_via(store, s["uid"], platform=s["platform"])})
    everyone.sort(key=lambda r: (r["status"], r["platform"], r["handle"] or ""))

    seeds = [{"seed": f"@{s['handle']}", "kind": s["kind"], "label": s["label"],
              "pages_fetched": s["pages_fetched"], "accounts_seen": s["accounts_seen"],
              "new_in_market": s["new_in_market"], "credits_spent": s["credits_spent"],
              "total_following": s["total_following"], "exhausted": bool(s["exhausted"]),
              "exhausted_reason": s["exhausted_reason"]}
             for s in store.seeds(market, active_only=False)]
    seeds += source_summary(store, run_id, market)

    funnel = run["funnel"] or {}
    log = [{"field": k, "value": v} for k, v in [
        ("run_id", run_id), ("market", market), ("brief", run["brief"]),
        ("params", json.dumps(run["params"])), ("status", run["status"]),
        ("budget", run["budget"]), ("credits_used", run["credits_used"]),
        ("api_calls", run["api_calls"]), ("cache_hits", run["cache_hits"]),
        ("llm_calls", run.get("llm_calls")), ("llm_tokens", run.get("llm_tokens")),
        ("accepted_by_first_source", json.dumps(accepted_by_first_source(store, run_id, market),
                                                ensure_ascii=False)),
        ("started", _iso(run["started_at"])), ("finished", _iso(run["finished_at"])),
        ("duration_s", round((run["finished_at"] or run["started_at"]) - run["started_at"], 1)),
    ]] + [{"field": f"funnel.{k}", "value": v} for k, v in funnel.items()]

    return {"Shortlist": shortlist, "Maybe": maybe, "Existing partners": existing,
            "Prenew format": [prenew_row(r, market) for r in shortlist + existing],
            "Other markets pool": other, "Seeds and sources": seeds, "All screened": everyone,
            "Run log": log}


def prenew_row(r: dict, market: str) -> dict:
    """A row in the layout of the client's collaboration sheet."""
    niche = r["niche"] or ""
    if r["games"]:
        niche = f"{niche}: {r['games']}" if niche else r["games"]
    yt_avg = r["avg_views_youtube"] or r["avg_views_youtube_shorts"]
    yt_win = r["views_window_youtube"] or r["views_window_youtube_shorts"]
    return {
        "Creator key": r["partner_key"] or r["creator"],
        "Market": market,
        "Country": r["country"],
        "Creator / channel": r["creator"],
        "Platform": r["platforms"],
        "Niche / content": niche,
        "YT subscribers": r["subscribers_youtube"],
        "YT views / video": r["views_range_youtube"] or (metrics_mod.fmt_count(yt_avg) + " avg"
                                                          if yt_avg else None),
        "TikTok followers": r["followers_tiktok"],
        "TikTok views / video": r["views_range_tiktok"] or None,
        "YT avg views (window)": f"{yt_avg} ({yt_win})" if yt_avg else None,
        "TikTok avg views (window)": (f"{r['avg_views_tiktok']} ({r['views_window_tiktok']})"
                                      if r["avg_views_tiktok"] else None),
        "Contact": r["contact"],
        "Existing partner": r["existing_partner"],
        "Links": ", ".join(x for x in (r["tiktok_url"], r["youtube_url"]) if x),
    }


def source_summary(store: Store, run_id: int, market: str) -> list[dict]:
    """Per harvest source and snowball seed: accounts seen, in market, enriched, accepted."""
    rows = store.conn.execute(
        "SELECT e.kind, e.via, COUNT(DISTINCT e.to_uid) AS accounts,"
        " COUNT(DISTINCT CASE WHEN s.bucket IN ('sure', 'unsure') THEN e.to_uid END) AS in_market,"
        " COUNT(DISTINCT CASE WHEN s.status IN ('needs_judgment', 'accepted', 'maybe', 'rejected')"
        "   THEN e.to_uid END) AS enriched,"
        " COUNT(DISTINCT CASE WHEN s.status = 'accepted' THEN e.to_uid END) AS accepted"
        " FROM edges e LEFT JOIN screenings s ON s.uid = e.to_uid AND s.platform = e.platform"
        " AND s.market = ? WHERE e.run_id = ? AND e.kind IN ('hashtag', 'keyword', 'following',"
        " 'yt_search')"
        " GROUP BY e.kind, e.via ORDER BY accepted DESC, in_market DESC",
        (market, run_id),
    ).fetchall()
    return [{"seed": f"{r['kind']}: {r['via']}", "kind": r["kind"], "label": "",
             "accounts_seen": r["accounts"], "new_in_market": r["in_market"],
             "enriched": r["enriched"], "accepted": r["accepted"]} for r in rows]


def accepted_by_first_source(store: Store, run_id: int, market: str) -> dict[str, int]:
    """Attribute each accepted account to the source type that found it first in the run."""
    out: dict[str, int] = {}
    for s in store.screenings(market, "accepted", run_id=run_id):
        edges = [e for e in store.edges_to(s["platform"], s["uid"]) if e["run_id"] == run_id]
        if not edges:
            continue
        first = edges[0]
        label = {"hashtag": f"#{first['via']}", "keyword": f"TikTok search '{first['via']}'",
                 "following": "snowball", "yt_search": f"YouTube search '{first['via']}'",
                 "tiktok_link": "linked from TikTok", "youtube_link": "linked from YouTube"
                 }.get(first["kind"], first["kind"])
        out[label] = out.get(label, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _iso(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else ""


def _sheet_columns(name: str, rows: list[dict]) -> list[str]:
    if name in ("Shortlist", "Maybe", "Existing partners"):
        return SHORTLIST_COLUMNS
    if name == "Prenew format":
        return PRENEW_COLUMNS
    return _columns(rows)


def write_xlsx(sheets: dict[str, list[dict]], path: Path) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill("solid", fgColor="1F2937")
    for name, rows in sheets.items():
        ws = wb.create_sheet(name[:31])
        columns = _sheet_columns(name, rows)
        ws.append(columns)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
        for r in rows:
            ws.append([_cell(r.get(col)) for col in columns])
        ws.freeze_panes = "C2" if name in ("Shortlist", "Maybe", "Existing partners") else "A2"
        for i, col in enumerate(columns, start=1):
            width = min(max([len(str(col))] + [len(str(r.get(col) or ""))
                                                for r in rows[:200]]) + 2, 60)
            ws.column_dimensions[get_column_letter(i)].width = width
        if name in ("Shortlist", "Maybe") and rows:
            dv = DataValidation(type="list", formula1='"' + ",".join(OUTREACH_STATUSES) + '"',
                                allow_blank=True)
            ws.add_data_validation(dv)
            dv.add(f"A2:A{len(rows) + 1}")
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.alignment = Alignment(vertical="top", wrap_text=False)
            ws.auto_filter.ref = ws.dimensions
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_csvs(sheets: dict[str, list[dict]], directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, rows in sheets.items():
        columns = _sheet_columns(name, rows)
        path = directory / (name.lower().replace(" ", "_") + ".csv")
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                writer.writerow({k: _cell(r.get(k)) for k in columns})
        paths.append(path)
    return paths


def _columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols or ["(empty)"]


def _cell(value):
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def export_run(store: Store, run_id: int, out_dir: Path,
               formats: tuple[str, ...] = ("xlsx", "csv")) -> list[Path]:
    run = store.get_run(run_id)
    sheets = build_sheets(store, run_id)
    stamp = dt.datetime.fromtimestamp(run["started_at"]).strftime("%Y%m%d")
    base = f"{run['market'].lower()}_{stamp}_run{run_id}"
    paths: list[Path] = []
    if "xlsx" in formats:
        paths.append(write_xlsx(sheets, out_dir / f"{base}.xlsx"))
    if "csv" in formats:
        paths += write_csvs(sheets, out_dir / base)
    return paths
