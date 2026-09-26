"""``scout`` command-line interface."""

from __future__ import annotations

import json
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
        band_min=p["band_min"], band_max=p["band_max"], target=p["target"], budget=run["budget"],
        max_pages_per_seed=p.get("max_pages_per_seed", 5),
        extra={k: p[k] for k in ("harvest_share", "use_general_tags", "use_retailer_seeds") if k in p})
    meter = CreditMeter(budget=run["budget"], used=run["credits_used"] or 0,
                        live_calls=run["api_calls"] or 0, cache_hits=run["cache_hits"] or 0)
    client = ScrapeCreators(store, None if offline else config.sc_key(), meter, offline=offline)
    judge, llm = _make_judge(store, p.get("judge", "none"))
    return Pipeline(store, client, load_market(run["market"]), settings, run["id"],
                    progress=progress, fetch_link_pages=not p.get("no_link_pages"),
                    judge=judge, llm=llm)


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
    band: str = typer.Option("1000-100000", help="Follower band, e.g. 2000-50000"),
    target: int = typer.Option(40, help="Stop after this many accepted (or qualified) creators"),
    budget: int = typer.Option(300, help="Hard credit budget for this run"),
    judge: str = typer.Option("free", help="Fit judge: free (LLM chain), claude (file handoff) or none"),
    hashtags: Optional[str] = typer.Option(None, help="Comma-separated hashtags (default: market plan)"),
    keywords: Optional[str] = typer.Option(None, help="Comma-separated keywords (default: market plan)"),
    general_tags: bool = typer.Option(False, help="Also harvest general country tags (lifestyle-heavy)"),
    retailer_seeds: bool = typer.Option(False, help="Also snowball from retailer and shop accounts"),
    max_pages_per_seed: int = typer.Option(5, help="Following-list pages per snowball seed"),
    harvest_share: float = typer.Option(0.35, help="Share of the budget available to harvest"),
    no_link_pages: bool = typer.Option(False, help="Do not fetch bio-link pages for contacts"),
    brief: str = typer.Option("", help="Free-text brief stored with the run"),
    export: bool = typer.Option(True, help="Write XLSX and CSV when finished"),
    as_json: bool = typer.Option(False, "--json", help="Print the run summary as JSON"),
) -> None:
    """Harvest, filter, judge and snowball one market."""
    store = _setup()
    mk = load_market(market)
    lo, hi = _parse_band(band)
    params = {"band_min": lo, "band_max": hi, "target": target, "judge": judge,
              "hashtags": hashtags, "keywords": keywords, "harvest_share": harvest_share,
              "use_general_tags": general_tags, "use_retailer_seeds": retailer_seeds,
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
           export: bool = typer.Option(True, help="Write XLSX and CSV when finished")) -> None:
    """Continue a paused or budget-limited run from its stored state."""
    store = _setup()
    run_id = run_id or store.latest_run_id()
    if budget is not None:
        store.update_run(run_id, budget=budget)
    run_row = store.get_run(run_id)
    pipe = _pipeline_for(store, run_row, _print_progress)
    result = pipe.run(resume=True)
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
        uids = [s["uid"] for s in store.screenings(code, "pending")
                if store.get_prejudgment(code, "tiktok", s["uid"]) is None]
        items = [prejudge_payload(store, mk, u) for u in uids]
    elif stage == "judge":
        items = [judge_payload(store, mk, s["uid"])
                 for s in store.screenings(code, "needs_judgment", run_id=run_id)]
    elif stage == "pitch":
        items = [pitch_payload(store, mk, s["uid"])
                 for s in store.screenings(code, "accepted", run_id=run_id)
                 if store.get_pitch(code, "tiktok", s["uid"]) is None]
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
        if stage == "prejudge":
            pipe.apply_prejudgment(uid, r["verdict"], r.get("reason", ""), model)
        elif stage == "judge":
            pipe.apply_decision(uid, r, model)
        else:
            store.set_pitch(pipe.market.code, "tiktok", uid, r["language"], r["subject"], r["body"],
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
