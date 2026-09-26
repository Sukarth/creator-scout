"""Quality gates for a finished run. A run is reportable only when all gates pass."""

from __future__ import annotations

from dataclasses import dataclass, field

from .export import build_sheets
from .filters import SOFT_CAP_FACTOR
from .store import Store


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    run_id: int
    results: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)


def check_run(store: Store, run_id: int) -> Report:
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    market = run["market"]
    p = run["params"] or {}
    bands = {"tiktok": (p.get("band_min", 0), p.get("band_max", 10**12)),
             "youtube": (p.get("yt_band_min", 0), p.get("yt_band_max", 10**12))}
    report = Report(run_id)
    sheets = build_sheets(store, run_id)
    shortlist = sheets["Shortlist"]

    out_of_band = []
    for r in shortlist:
        for platform, uid in r["accounts"]:
            s = store.get_screening(market, platform, uid) or {}
            if s.get("run_id") != run_id or s.get("status") not in ("accepted", "needs_judgment"):
                continue  # linked account shown for context, not shortlisted itself
            f = (store.get_creator(platform, uid) or {}).get("followers")
            lo, hi = bands[platform]
            if f is None or not lo <= f <= hi * SOFT_CAP_FACTOR:
                out_of_band.append(f"{r['creator']} ({platform}: {f})")
    report.results.append(CheckResult(
        "followers inside band", not out_of_band,
        f"{len(shortlist)} rows checked" if not out_of_band else f"outside band: {out_of_band}"))

    unresolved, unresolved_pending = [], []
    for r in shortlist:
        if r["market"] == market:
            continue
        (unresolved if r["decision"] == "accept" else unresolved_pending).append(r["creator"])
    report.results.append(CheckResult(
        "market resolved for accepted rows", not unresolved,
        "ok" if not unresolved else f"unresolved: {unresolved}"))
    if unresolved_pending:
        report.warnings.append(f"{len(unresolved_pending)} unjudged rows with unconfirmed market: "
                               f"{unresolved_pending[:10]}")

    earlier = set()
    for platform in ("tiktok", "youtube"):
        earlier |= {(platform, u) for u in store.earlier_members(run_id, market, platform)}
    dups = [r["creator"] for r in shortlist if any(a in earlier for a in r["accounts"])]
    report.results.append(CheckResult(
        "no duplicates from earlier runs", not dups,
        f"{len(earlier)} accounts known from earlier runs" if not dups else f"duplicates: {dups}"))

    no_evidence = [r["creator"] for r in shortlist
                   if r["decision"] == "accept" and not (r.get("evidence_quote") or "").strip()]
    report.results.append(CheckResult(
        "accepted rows quote evidence", not no_evidence,
        "ok" if not no_evidence else f"missing evidence: {no_evidence}"))

    missing = []
    for r in shortlist:
        gaps = [name for name, ok in (
            ("country", bool(r["country"]) or r["market"] == market),
            ("followers", r["followers_tiktok"] is not None or r["subscribers_youtube"] is not None),
            ("views", bool(r["views_window_tiktok"] or r["views_window_youtube"]
                           or r["views_window_youtube_shorts"])),
            # Niche comes from the judge; unjudged (hard-filter-only) runs have none.
            ("niche", bool(r["niche"]) or r["decision"] != "accept"),
            ("contact", bool(r["contact"]))) if not ok]
        if gaps:
            missing.append(f"{r['creator']} ({', '.join(gaps)})")
    report.results.append(CheckResult(
        "required columns filled", not missing,
        f"{sum(1 for r in shortlist if r['contact'] != 'no email found')} of {len(shortlist)} "
        f"rows have an email" if not missing else f"gaps: {missing[:10]}"))

    accepted_rows = [r for r in shortlist if r["decision"] == "accept"]
    wrong_lang = [r["creator"] for r in accepted_rows if r.get("pitch_body")
                  and (r.get("pitch_language") or "")[:2].lower()
                  != (r.get("content_language") or "")[:2].lower()]
    report.results.append(CheckResult(
        "pitch language matches content language", not wrong_lang,
        f"{sum(1 for r in accepted_rows if r.get('pitch_body'))} pitches checked"
        if not wrong_lang else f"mismatch: {wrong_lang}"))

    f = run["funnel"] or {}
    if f:
        total = f.get("already_known", 0) + f.get("bucket_sure", 0) + f.get("bucket_unsure", 0) \
            + f.get("bucket_other", 0)
        report.results.append(CheckResult(
            "funnel totals add up", total == f.get("seen", 0),
            f"seen {f.get('seen')} = known {f.get('already_known')} + sure {f.get('bucket_sure')}"
            f" + unsure {f.get('bucket_unsure')} + other {f.get('bucket_other')}"))
    else:
        report.results.append(CheckResult("funnel totals add up", False, "run has no funnel"))

    unjudged = sum(1 for r in shortlist if r["decision"] == "not judged")
    if unjudged:
        report.warnings.append(f"{unjudged} shortlisted rows are not judged yet")
    if sheets["Existing partners"]:
        report.warnings.append(f"{len(sheets['Existing partners'])} existing partners found "
                               "(listed separately, not as new)")
    return report
