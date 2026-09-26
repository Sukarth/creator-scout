"""Quality gates for a finished run. A run is reportable only when all gates pass."""

from __future__ import annotations

from dataclasses import dataclass, field

from .export import build_sheets
from .store import Store

PLATFORM = "tiktok"


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
    params = run["params"] or {}
    band_min, band_max = params.get("band_min", 0), params.get("band_max", 10**12)
    report = Report(run_id)
    sheets = build_sheets(store, run_id)
    shortlist = sheets["Shortlist"]
    by_handle = {r["handle"]: r for r in shortlist}
    screenings = {s["uid"]: s for s in store.screenings(market, run_id=run_id)}
    shortlisted_uids = [uid for uid, s in screenings.items()
                        if s["status"] in ("accepted", "needs_judgment")]

    out_of_band = [r["handle"] for r in shortlist
                   if r["followers"] is None or not band_min <= r["followers"] <= band_max]
    report.results.append(CheckResult(
        "followers inside band", not out_of_band,
        f"{len(shortlist)} rows checked" if not out_of_band else f"outside band: {out_of_band}"))

    unresolved_accepted, unresolved_pending = [], []
    for uid in shortlisted_uids:
        s = screenings[uid]
        handle = (store.get_creator(PLATFORM, uid) or {}).get("handle")
        row = by_handle.get(handle, {})
        if s["bucket"] == "sure" or row.get("market") == market:
            continue
        (unresolved_accepted if s["status"] == "accepted" else unresolved_pending).append(handle)
    report.results.append(CheckResult(
        "market resolved for accepted rows", not unresolved_accepted,
        "ok" if not unresolved_accepted else f"unresolved: {unresolved_accepted}"))
    if unresolved_pending:
        report.warnings.append(f"{len(unresolved_pending)} unjudged rows with unconfirmed market: "
                               f"{unresolved_pending[:10]}")

    earlier = store.earlier_members(run_id, market, PLATFORM)
    dups = [uid for uid in shortlisted_uids if uid in earlier]
    report.results.append(CheckResult(
        "no duplicates from earlier runs", not dups,
        f"{len(earlier)} creators known from earlier runs" if not dups else f"duplicates: {dups}"))

    no_evidence = [r["handle"] for r in shortlist
                   if r["decision"] == "accept" and not (r.get("evidence_quote") or "").strip()]
    report.results.append(CheckResult(
        "accepted rows quote evidence", not no_evidence,
        "ok" if not no_evidence else f"missing evidence: {no_evidence}"))

    no_contact_field = [r["handle"] for r in shortlist if not (r["emails"] or r["other_links"])]
    report.results.append(CheckResult(
        "contact path or explicit 'no contact found'", not no_contact_field,
        f"{sum(1 for r in shortlist if r['emails'] != 'no contact found')} of {len(shortlist)} "
        f"rows have an email"))

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
    return report
