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
from .market import list_markets, load_market
from .pipeline import Pipeline
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


@app.command()
def run(
    market: str = typer.Option(..., help="Market code, e.g. ee"),
    band: str = typer.Option("1000-100000", help="Follower band, e.g. 2000-50000"),
    target: int = typer.Option(40, help="Stop after this many qualified creators"),
    budget: int = typer.Option(300, help="Hard credit budget for this run"),
    judge: str = typer.Option("none", help="Fit judge: none (free/claude arrive later)"),
    hashtags: Optional[str] = typer.Option(None, help="Comma-separated hashtags (default: market seeds)"),
    keywords: Optional[str] = typer.Option(None, help="Comma-separated keywords (default: market seeds)"),
    max_pages_per_seed: int = typer.Option(5, help="Following-list pages per snowball seed"),
    harvest_share: float = typer.Option(0.35, help="Share of the budget available to harvest"),
    no_link_pages: bool = typer.Option(False, help="Do not fetch bio-link pages for contacts"),
    brief: str = typer.Option("", help="Free-text brief stored with the run"),
    export: bool = typer.Option(True, help="Write XLSX and CSV when finished"),
    as_json: bool = typer.Option(False, "--json", help="Print the run summary as JSON"),
) -> None:
    """Harvest, filter, enrich and snowball one market."""
    store = _setup()
    mk = load_market(market)
    lo, hi = _parse_band(band)
    settings = config.RunSettings(band_min=lo, band_max=hi, target=target, budget=budget,
                                  max_pages_per_seed=max_pages_per_seed,
                                  extra={"harvest_share": harvest_share})
    params = {"band_min": lo, "band_max": hi, "target": target, "judge": judge,
              "hashtags": hashtags, "keywords": keywords, "harvest_share": harvest_share}
    run_id = store.create_run(mk.code, params, budget, brief=brief)
    meter = CreditMeter(budget=budget)
    client = ScrapeCreators(store, config.sc_key(), meter)
    pipe = Pipeline(store, client, mk, settings, run_id,
                    progress=None if as_json else _print_progress,
                    fetch_link_pages=not no_link_pages)
    result = pipe.run(
        hashtags=[h.strip().lstrip("#") for h in hashtags.split(",")] if hashtags else None,
        keywords=[k.strip() for k in keywords.split(",")] if keywords else None,
    )
    client.close()
    paths = export_run(store, run_id, config.exports_dir()) if export else []
    if as_json:
        typer.echo(json.dumps({"run": result, "exports": [str(p) for p in paths]}, default=str))
        return
    f = result["funnel"]
    typer.echo(f"\nRun {run_id} ({result['status']}): {f.get('qualified_total_for_run')} qualified from "
               f"{f.get('seen')} accounts reviewed; {result['credits_used']} credits, "
               f"{result['cache_hits']} cache hits.")
    for p in paths[:1]:
        typer.echo(f"Exported {p}")


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
