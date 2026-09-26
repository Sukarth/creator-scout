"""``scout`` command-line interface."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Optional

import typer

from . import config
from .checks import check_run
from .export import export_run
from .judge import (DeferredJudge, FreeJudge, JudgeResult, PitchResult, PrejudgeResult,
                    judge_payload, pitch_payload, prejudge_payload)
from .llm import LLMClient
from .sources.youtube import YouTubeData
from .market import list_markets, load_market
from .pipeline import Pipeline, keywords_meta_key
from .sources.scrapecreators import CreditMeter, ScrapeCreators
from .store import Store

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Find small, niche creators per market and export an outreach sheet.")
cache_app = typer.Typer(help="Inspect or clear the API response cache.")
app.add_typer(cache_app, name="cache")


def _setup() -> Store:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    config.load_dotenv()
    return Store(config.db_path())


def _parse_band(band: str) -> tuple[int, int]:
    lo, _, hi = band.replace("k", "000").partition("-")
    return int(lo), int(hi)


def _print_progress(event: dict) -> None:
    typer.echo(f"[{event['credits_used']:>4}/{event['budget']} cr] {event['stage']:<8} {event['message']}")


@app.command()
def markets() -> None:
    """List configured markets."""
    _setup()
    for m in list_markets():
        typer.echo(f"{m.code.lower()}  {m.name:<10} languages={','.join(m.languages)}  "
                   f"hashtags={len(m.seed_hashtags)} keywords={len(m.seed_keywords)} "
                   f"retailer_seeds={len(m.retailer_seeds)}")


JUDGES = ("none", "free", "claude")


def _make_judge(store: Store, kind: str):
    """Return ``(judge, llm)`` for a judge kind."""
    if kind == "none":
        return None, None
    if kind == "claude":
        return DeferredJudge(), None
    if kind == "free":
        llm = LLMClient(store)
        if not llm.available():
            raise typer.BadParameter("judge 'free' needs GROQ_API_KEY or OPENCODE_API_KEY")
        return FreeJudge(llm), llm
    raise typer.BadParameter(f"judge must be one of {', '.join(JUDGES)}")


def _pipeline_for(store: Store, run: dict, progress=None, offline: bool = False) -> Pipeline:
    """Rebuild the pipeline of a stored run (for resume and decision import)."""
    p = run["params"]
    settings = config.RunSettings(
        band_min=p["band_min"], band_max=p["band_max"],
        yt_band_min=p.get("yt_band_min", 5_000), yt_band_max=p.get("yt_band_max", 250_000),
        target=p["target"], budget=run["budget"],
        max_pages_per_seed=p.get("max_pages_per_seed", 30),
        extra={k: p[k] for k in ("harvest_share", "use_general_tags", "use_retailer_seeds",
                                 "use_partner_seeds", "pitches", "platforms") if k in p})
    meter = CreditMeter(budget=run["budget"], used=run["credits_used"] or 0,
                        live_calls=run["api_calls"] or 0, cache_hits=run["cache_hits"] or 0)
    client = ScrapeCreators(store, None if offline else config.sc_key(), meter, offline=offline)
    client.run_id = run["id"]
    if p.get("use_partner_seeds"):
        settings.snowball_start_after = 0  # partner seeds are known-good: expand them at once
    judge, llm = _make_judge(store, p.get("judge", "none"))
    youtube = None
    if "youtube" in p.get("platforms", ["tiktok", "youtube"]):
        youtube = YouTubeData(store, offline=offline)
    return Pipeline(store, client, load_market(run["market"]), settings, run["id"],
                    progress=progress, fetch_link_pages=not p.get("no_link_pages"),
                    judge=judge, llm=llm, youtube=youtube)


def _band_params(preset: str, band: Optional[str], yt_band: Optional[str]) -> dict:
    if preset not in config.PRESETS:
        raise typer.BadParameter(f"preset must be one of {', '.join(config.PRESETS)}")
    (tlo, thi), (ylo, yhi) = config.PRESETS[preset]["tiktok"], config.PRESETS[preset]["youtube"]
    if band:
        tlo, thi = _parse_band(band)
    if yt_band:
        ylo, yhi = _parse_band(yt_band)
    return {"preset": preset, "band_min": tlo, "band_max": thi, "yt_band_min": ylo, "yt_band_max": yhi}


def _summary(store: Store, result: dict, paths: list[Path]) -> None:
    f = result["funnel"]
    judged = result["params"].get("judge", "none") != "none"
    noun = "accepted" if judged else "qualified"
    typer.echo(f"\nRun {result['id']} ({result['status']}): {f.get('target_count')} {noun} from "
               f"{f.get('seen')} accounts reviewed; {result['credits_used']} credits, "
               f"{result['cache_hits']} cache hits, {result.get('llm_calls') or 0} LLM calls.")
    if result["status"].startswith("awaiting"):
        stage = "prejudge" if result["status"] == "awaiting_prejudge" else "judge"
        typer.echo(f"Next: scout candidates --run {result['id']} --stage {stage} > {stage}.json, "
                   f"write decisions, then scout decide --run {result['id']} --stage {stage} "
                   f"--file decisions.json and scout resume --run {result['id']}")
    for p in paths[:1]:
        typer.echo(f"Exported {p}")


@app.command()
def run(
    market: str = typer.Option(..., help="Market code, e.g. ee"),
    preset: str = typer.Option("default", help="Size preset: default or hidden-gems"),
    band: Optional[str] = typer.Option(None, help="TikTok follower band, e.g. 4000-500000"),
    yt_band: Optional[str] = typer.Option(None, help="YouTube subscriber band, e.g. 50000-250000"),
    platforms: str = typer.Option("tiktok,youtube", help="Comma-separated platforms"),
    pitches: bool = typer.Option(False, help="Also draft outreach pitches for accepted creators"),
    target: int = typer.Option(40, help="Stop after this many accepted (or qualified) creators"),
    budget: int = typer.Option(300, help="Hard credit budget for this run"),
    judge: str = typer.Option("free", help="Fit judge: free (LLM chain), claude (file handoff) or none"),
    hashtags: Optional[str] = typer.Option(None, help="Comma-separated hashtags (default: market plan)"),
    keywords: Optional[str] = typer.Option(None, help="Comma-separated keywords (default: market plan)"),
    general_tags: bool = typer.Option(False, help="Also harvest general country tags (lifestyle-heavy)"),
    retailer_seeds: bool = typer.Option(False, help="Also snowball from retailer and shop accounts"),
    partner_seeds: bool = typer.Option(False, help="Also snowball from the client's existing partners"),
    max_pages_per_seed: int = typer.Option(30, help="Most following-list pages per snowball seed"),
    harvest_share: float = typer.Option(0.35, help="Share of the budget available to harvest"),
    no_link_pages: bool = typer.Option(False, help="Do not fetch bio-link pages for contacts"),
    brief: str = typer.Option("", help="Free-text brief stored with the run"),
    export: bool = typer.Option(True, help="Write XLSX and CSV when finished"),
    as_json: bool = typer.Option(False, "--json", help="Print the run summary as JSON"),
) -> None:
    """Harvest, filter, judge and snowball one market."""
    store = _setup()
    mk = load_market(market)
    params = {**_band_params(preset, band, yt_band), "target": target, "judge": judge,
              "platforms": [p.strip() for p in platforms.split(",") if p.strip()],
              "pitches": pitches,
              "hashtags": hashtags, "keywords": keywords, "harvest_share": harvest_share,
              "use_general_tags": general_tags, "use_retailer_seeds": retailer_seeds,
              "use_partner_seeds": partner_seeds,
              "max_pages_per_seed": max_pages_per_seed, "no_link_pages": no_link_pages}
    run_id = store.create_run(mk.code, params, budget, brief=brief)
    pipe = _pipeline_for(store, store.get_run(run_id), None if as_json else _print_progress)
    result = pipe.run(
        hashtags=[h.strip().lstrip("#") for h in hashtags.split(",")] if hashtags else None,
        keywords=[k.strip() for k in keywords.split(",")] if keywords else None,
    )
    pipe.client.close()
    paths = export_run(store, run_id, config.exports_dir()) if export else []
    if as_json:
        typer.echo(json.dumps({"run": result, "exports": [str(p) for p in paths]}, default=str))
        return
    _summary(store, result, paths)


@app.command()
def resume(run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)"),
           budget: Optional[int] = typer.Option(None, help="Raise the run's credit budget"),
           rescreen: bool = typer.Option(False, help="Re-apply hard filters to pending accounts first"),
           rejudge: bool = typer.Option(False, help="Send judged accounts back to the judge first"),
           preset: Optional[str] = typer.Option(None, help="Switch the run to a size preset"),
           platforms: Optional[str] = typer.Option(None, help="Switch the run's platforms"),
           partner_seeds: bool = typer.Option(False, help="Allow the client's partners as seeds"),
           export: bool = typer.Option(True, help="Write XLSX and CSV when finished")) -> None:
    """Continue a paused or budget-limited run from its stored state."""
    store = _setup()
    run_id = run_id or store.latest_run_id()
    if budget is not None:
        store.update_run(run_id, budget=budget)
    params = dict(store.get_run(run_id)["params"])
    if preset:
        params.update(_band_params(preset, None, None))
    if platforms:
        params["platforms"] = [p.strip() for p in platforms.split(",") if p.strip()]
    if partner_seeds:
        params["use_partner_seeds"] = True
    params.setdefault("max_pages_per_seed", 30)
    params["max_pages_per_seed"] = max(params["max_pages_per_seed"], 30)
    store.conn.execute("UPDATE runs SET params = ? WHERE id = ?", (json.dumps(params), run_id))
    store.conn.commit()
    run_row = store.get_run(run_id)
    pipe = _pipeline_for(store, run_row, _print_progress)
    result = pipe.run(resume=True, rescreen=rescreen, rejudge=rejudge)
    pipe.client.close()
    paths = export_run(store, run_id, config.exports_dir()) if export else []
    _summary(store, result, paths)


@app.command()
def keywords(market: str = typer.Option(..., help="Market code"),
             refresh: bool = typer.Option(False, help="Regenerate with the free LLM"),
             file: Optional[Path] = typer.Option(None, help="Import keyword ideas from a JSON file")) -> None:
    """Show the harvest plan: curated and generated hashtags and keywords."""
    store = _setup()
    mk = load_market(market)
    key = keywords_meta_key(mk.code)
    if file:
        data = json.loads(file.read_text(encoding="utf-8"))
        store.meta_set(key, json.dumps(data, ensure_ascii=False))
    elif refresh or not store.meta_get(key):
        judge, llm = _make_judge(store, "free")
        data = judge.keywords(mk)
        store.meta_set(key, json.dumps(data, ensure_ascii=False))
    pipe = Pipeline(store, ScrapeCreators(store, None, CreditMeter(0), offline=True), mk,
                    config.RunSettings(budget=0), run_id=0)
    for kind, term, region in pipe.harvest_plan():
        typer.echo(f"{kind:<8} {term}" + (f"   (proxy {region})" if region else ""))


@app.command()
def candidates(run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)"),
               stage: str = typer.Option("judge", help="prejudge, judge or pitch"),
               fmt: str = typer.Option("json", "--format", help="json or md")) -> None:
    """Print candidates awaiting an external judge (skill mode)."""
    store = _setup()
    run_id = run_id or store.latest_run_id()
    run_row = store.get_run(run_id)
    mk = load_market(run_row["market"])
    code = mk.code
    if stage == "prejudge":
        items = [prejudge_payload(store, mk, s["uid"], s["platform"])
                 for s in store.screenings(code, "pending")
                 if store.get_prejudgment(code, s["platform"], s["uid"]) is None]
    elif stage == "judge":
        items = [judge_payload(store, mk, s["uid"], s["platform"])
                 for s in store.screenings(code, "needs_judgment", run_id=run_id)]
    elif stage == "pitch":
        items = [pitch_payload(store, mk, s["uid"], s["platform"])
                 for s in store.screenings(code, "accepted", run_id=run_id)
                 if store.get_pitch(code, s["platform"], s["uid"]) is None]
    else:
        raise typer.BadParameter("stage must be prejudge, judge or pitch")
    if fmt == "md":
        for it in items:
            typer.echo(f"### @{it.get('handle')} (id {it['id']})")
            for k, v in it.items():
                if k not in ("id", "handle") and v not in (None, "", []):
                    typer.echo(f"- {k}: {v}")
            typer.echo("")
    else:
        typer.echo(json.dumps({"run": run_id, "stage": stage, "market": code, "accounts": items},
                              ensure_ascii=False, indent=1))


@app.command()
def decide(file: Path = typer.Option(..., help='JSON file: {"results": [...]} or a list'),
           run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)"),
           stage: str = typer.Option("judge", help="prejudge, judge or pitch"),
           model: str = typer.Option("claude", help="Label stored with each decision")) -> None:
    """Import decisions written by an external judge (skill mode)."""
    store = _setup()
    run_id = run_id or store.latest_run_id()
    pipe = _pipeline_for(store, store.get_run(run_id), offline=True)
    pipe.judge = pipe.judge or DeferredJudge()
    pipe.resume_state()
    raw = json.loads(file.read_text(encoding="utf-8"))
    rows = raw.get("results", raw) if isinstance(raw, dict) else raw
    schema = {"prejudge": PrejudgeResult, "judge": JudgeResult, "pitch": PitchResult}.get(stage)
    if schema is None:
        raise typer.BadParameter("stage must be prejudge, judge or pitch")
    ok, bad = 0, []
    for row in rows:
        try:
            r = schema.model_validate(row).model_dump()
        except Exception as exc:
            bad.append(f"{row.get('id') if isinstance(row, dict) else row}: {exc}".splitlines()[0])
            continue
        uid = r["id"]
        # YouTube channel ids start with "UC"; TikTok ids are numeric.
        platform = (row.get("platform") if isinstance(row, dict) else None) or \
            ("youtube" if str(uid).startswith("UC") else "tiktok")
        if stage == "prejudge":
            pipe.apply_prejudgment(uid, r["verdict"], r.get("reason", ""), model, platform)
        elif stage == "judge":
            pipe.apply_decision(uid, r, model, platform)
        else:
            store.set_pitch(pipe.market.code, platform, uid, r["language"], r["subject"], r["body"],
                            r["dm"], model, run_id)
            pipe.funnel["pitches"] += 1
        ok += 1
    store.update_run(run_id, funnel=pipe.funnel)
    typer.echo(f"imported {ok} {stage} results" + (f"; {len(bad)} invalid: {bad[:5]}" if bad else ""))
    raise typer.Exit(0 if not bad else 1)


@app.command()
def pitches(run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)")) -> None:
    """Draft pitches for accepted creators with the free LLM chain."""
    store = _setup()
    run_id = run_id or store.latest_run_id()
    run_row = store.get_run(run_id)
    pipe = _pipeline_for(store, run_row, _print_progress, offline=True)
    judge, llm = _make_judge(store, "free")
    pipe.judge, pipe.llm = judge, llm
    pipe.resume_state()
    n = pipe.write_pitches()
    store.update_run(run_id, funnel=pipe.funnel)
    typer.echo(f"drafted {n} pitches")


@app.command("refresh-metrics")
def refresh_metrics(run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)")) -> None:
    """Recompute view metrics from stored videos (no API calls)."""
    from . import metrics as metrics_mod
    store = _setup()
    run_id = run_id or store.latest_run_id()
    run_row = store.get_run(run_id)
    n = 0
    for s in store.screenings(run_row["market"], run_id=run_id):
        m = store.get_metrics(s["platform"], s["uid"])
        if not m or m.get("views"):
            continue
        vids = [v for v in store.videos_for(s["platform"], s["uid"], limit=60)
                if v.get("source") in ("profile_videos", "yt_long", "yt_shorts")]
        if not vids:
            continue
        m["views"] = metrics_mod.view_summary(vids)
        store.put_metrics(s["platform"], s["uid"], m)
        n += 1
    typer.echo(f"refreshed metrics for {n} accounts")


@app.command("yield")
def yield_(run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)")) -> None:
    """Accepted creators per 100 credits, by source type."""
    from .export import source_yield
    store = _setup()
    run_id = run_id or store.latest_run_id()
    typer.echo(f"{'source':<36}{'discovery':>10}{'enrich':>8}{'credits':>9}{'accepted':>10}{'per 100 cr':>12}")
    for g in source_yield(store, run_id):
        typer.echo(f"{g['source']:<36}{g['discovery_credits']:>10}{g['enrichment_credits']:>8}"
                   f"{g['credits']:>9}{g['accepted']:>10}{str(g['accepted_per_100_credits']):>12}")


@app.command()
def recall(market: str = typer.Option(..., help="Market code"),
           as_json: bool = typer.Option(False, "--json", help="Print JSON")) -> None:
    """Hold-out check: which of the client's existing partners did the tool find by itself?"""
    from .partners import load_partners, recall_report
    store = _setup()
    code = load_market(market).code
    partners = load_partners()
    if not partners:
        typer.echo("no partner list found (set SCOUT_PARTNERS_FILE)")
        raise typer.Exit(1)
    report = recall_report(store, code, partners=partners)
    if as_json:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        return
    found = sum(1 for r in report if r["found"])
    typer.echo(f"{code}: found {found} of {len(report)} existing partners by itself")
    for r in report:
        typer.echo(f"  {'FOUND' if r['found'] else 'MISSED':<6} {r['partner']:<22} "
                   f"({'/'.join(r['platforms']) or '?'}, {r['niche'] or '?'}): {r['stage']}")
        for a in r["accounts"]:
            via = ", ".join(f"{e['kind']}:{e['via']}" for e in a["found_via"])
            typer.echo(f"         {a['platform']:<8} @{a['handle']} {a['followers']} "
                       f"region={a['region']} status={a['status']} via {via}")


@app.command("partner-seeds")
def partner_seeds(market: str = typer.Option(..., help="Market code"),
                  budget: int = typer.Option(10, help="Credits for resolving handles"),
                  min_followers: int = typer.Option(1000, help="Smallest TikTok account accepted as a match"),
                  drop: str = typer.Option("", help="Comma-separated seed handles to remove first")) -> None:
    """Add the client's partners in a market as snowball seeds (after the recall test)."""
    from .partners import load_partners, norm
    from .sources import tiktok as tt
    store = _setup()
    code = load_market(market).code
    client = ScrapeCreators(store, config.sc_key(), CreditMeter(budget))
    for handle in [h.strip().lstrip("@").lower() for h in drop.split(",") if h.strip()]:
        store.conn.execute("DELETE FROM snowball_seeds WHERE market = ? AND handle = ? AND kind = 'partner'",
                           (code, handle))
        typer.echo(f"  removed seed @{handle}")
    store.conn.commit()
    added = 0
    for p in [x for x in load_partners() if x.market == code]:
        def pick(candidates: list[dict]) -> dict | None:
            """Closest to the follower count in the partner list, else the largest account.

            A match more than 3x off the listed count is a namesake, not the partner.
            """
            if p.tiktok_followers:
                off = lambda a: abs(math.log((a["followers"] or 1) / float(p.tiktok_followers)))
                best = min(candidates, key=off)
                return best if off(best) <= math.log(3) else None
            return max(candidates, key=lambda a: a["followers"] or 0)

        stored = [dict(r) for r in store.conn.execute(
            "SELECT uid, handle, nickname, followers FROM creators WHERE platform = 'tiktok'").fetchall()
            if p.matches(r["handle"], r["nickname"]) and (r["followers"] or 0) >= min_followers]
        known = pick(stored) if stored else None
        if known is None:
            body = client.tiktok_search_users(p.key)
            # Name matches include small namesakes: take the largest match and
            # require a real creator-sized account.
            hits = [a for a in tt.parse_users(body) if p.matches(a["handle"], a["nickname"])
                    and (a["followers"] or 0) >= min_followers]
            hit = pick(hits) if hits else None
            if hit is None:
                typer.echo(f"  no TikTok account with {min_followers}+ followers found for {p.key}")
                continue
            store.upsert_creator("tiktok", hit["uid"], handle=hit["handle"], nickname=hit["nickname"],
                                 followers=hit["followers"])
            known = hit
        if store.add_seed(code, "tiktok", known["handle"], "partner", uid=known["uid"]):
            added += 1
            typer.echo(f"  seed @{known['handle']} ({p.key})")
    typer.echo(f"added {added} partner seeds ({client.meter.used} credits); use them with "
               f"--partner-seeds on run or resume")


@app.command()
def reset(market: str = typer.Option(..., help="Market code"),
          yes: bool = typer.Option(False, "--yes", help="Confirm")) -> None:
    """Forget a market's screenings, seeds and decisions. Keeps creators and the API cache."""
    if not yes:
        typer.echo("refusing without --yes")
        raise typer.Exit(1)
    store = _setup()
    typer.echo(json.dumps(store.reset_market(load_market(market).code)))


@app.command()
def export(run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)"),
           fmt: str = typer.Option("xlsx,csv", "--format", help="xlsx, csv or both")) -> None:
    """Export a run to XLSX and/or CSV."""
    store = _setup()
    run_id = run_id or store.latest_run_id()
    for p in export_run(store, run_id, config.exports_dir(), tuple(fmt.split(","))):
        typer.echo(str(p))


@app.command()
def check(run_id: Optional[int] = typer.Option(None, "--run", help="Run id (default: latest)")) -> None:
    """Run the quality gates for a run. Exits non-zero on failure."""
    store = _setup()
    run_id = run_id or store.latest_run_id()
    report = check_run(store, run_id)
    for r in report.results:
        typer.echo(f"{'PASS' if r.passed else 'FAIL'}  {r.name}: {r.detail}")
    for w in report.warnings:
        typer.echo(f"WARN  {w}")
    typer.echo(f"\nrun {run_id}: {'all checks passed' if report.passed else 'checks failed'}")
    raise typer.Exit(0 if report.passed else 1)


@app.command()
def credits() -> None:
    """Credits remaining on the account (as of the last live call) and usage per run."""
    store = _setup()
    typer.echo(f"credits remaining on account: {store.meta_get('credits_remaining', 'unknown')}")
    typer.echo(f"credits used by this installation: {store.meta_get('credits_used_total', '0')}")
    rows = store.conn.execute(
        "SELECT id, market, status, credits_used, budget, api_calls, cache_hits FROM runs"
        " ORDER BY id DESC LIMIT 15").fetchall()
    for r in rows:
        typer.echo(f"  run {r['id']:>3} {r['market']} {r['status'] or '':<17} "
                   f"{r['credits_used']}/{r['budget']} credits, {r['api_calls']} live calls, "
                   f"{r['cache_hits']} cache hits")


@app.command()
def api(endpoint: str = typer.Argument(..., help="e.g. /v1/tiktok/profile"),
        params: list[str] = typer.Argument(None, help="key=value pairs"),
        budget: int = typer.Option(1, help="Credits this call may spend"),
        save: Optional[Path] = typer.Option(None, help="Write the response to this JSON file")) -> None:
    """Call one endpoint through the cache and credit meter (for debugging)."""
    store = _setup()
    meter = CreditMeter(budget=budget)
    client = ScrapeCreators(store, config.sc_key(), meter)
    body = client.get(endpoint, dict(p.split("=", 1) for p in params or []))
    text = json.dumps(body, ensure_ascii=False, indent=1)
    if save:
        save.write_text(text, encoding="utf-8")
        typer.echo(f"saved {save} ({meter.used} credits, {meter.cache_hits} cache hits)")
    else:
        typer.echo(text[:5000])


@cache_app.command("stats")
def cache_stats() -> None:
    store = _setup()
    for endpoint, s in store.cache_stats().items():
        typer.echo(f"{endpoint:<32} {s['entries']:>5} entries  {s['credits']:>5} credits")


@cache_app.command("clear")
def cache_clear(yes: bool = typer.Option(False, "--yes", help="Confirm")) -> None:
    if not yes:
        typer.echo("refusing without --yes (cached responses cost credits to refetch)")
        raise typer.Exit(1)
    store = _setup()
    typer.echo(f"removed {store.cache_clear()} cache entries")


if __name__ == "__main__":
    app()
