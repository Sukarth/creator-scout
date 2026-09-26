"""Creator Scout web app.

Two modes over the same core:
- Demo: browse saved real runs from a bundled read-only snapshot (no credits).
- Live: start a short run (access code required) and watch progress stream in.

Secrets stay on the server; the browser only ever sees results. Public demo
data shows creators' email addresses masked.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from scout import config
from scout.export import build_sheets, source_yield, write_xlsx
from scout.market import list_markets, load_market
from scout.pipeline import Pipeline
from scout.sources.scrapecreators import CreditMeter, ScrapeCreators
from scout.store import Store

HERE = Path(__file__).resolve().parent
SNAPSHOT = config.ROOT / "demo" / "snapshot.db"
LIVE_SECONDS = int(os.environ.get("SCOUT_LIVE_SECONDS", "170"))      # stop fetching
LIVE_HARD_SECONDS = int(os.environ.get("SCOUT_LIVE_HARD_SECONDS", "235"))  # stop judging
MAX_LIVE_BUDGET = int(os.environ.get("SCOUT_MAX_LIVE_BUDGET", "60"))
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

config.load_dotenv()
app = FastAPI(title="Creator Scout")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
_live_lock = threading.Lock()


def work_dir() -> Path:
    """Writable directory: the project data dir locally, /tmp on serverless hosts."""
    preferred = Path(os.environ.get("SCOUT_WORK_DIR", config.ROOT / "data"))
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return preferred
    except OSError:
        path = Path(tempfile.gettempdir()) / "creator-scout"
        path.mkdir(parents=True, exist_ok=True)
        return path


_stores: dict[str, Store] = {}


def store_for(source: str) -> Store:
    """``demo``: a private copy of the bundled snapshot. ``live``: the working database."""
    if source not in ("demo", "live"):
        raise HTTPException(404, "unknown source")
    if source not in _stores:
        if source == "demo":
            if not SNAPSHOT.is_file():
                raise HTTPException(503, "demo snapshot missing")
            target = work_dir() / "demo-copy.db"
            if not target.is_file() or target.stat().st_mtime < SNAPSHOT.stat().st_mtime:
                shutil.copyfile(SNAPSHOT, target)
            _stores[source] = Store(target)
        else:
            _stores[source] = Store(os.environ.get("SCOUT_DB", work_dir() / "live.db"))
    return _stores[source]


def mask(value):
    if isinstance(value, str):
        return EMAIL_RE.sub(lambda m: f"{m.group(1)}•••{m.group(2)}", value)
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    return value


def funnel(store: Store, run: dict) -> dict:
    f = run.get("funnel") or {}
    code, rid = run["market"], run["id"]
    count = lambda status: store.conn.execute(
        "SELECT COUNT(*) FROM screenings WHERE market = ? AND run_id = ? AND status = ?",
        (code, rid, status)).fetchone()[0]
    in_market = (f.get("bucket_sure") or 0) + (f.get("bucket_unsure") or 0)
    judged = store.conn.execute("SELECT COUNT(*) FROM decisions WHERE market = ? AND run_id = ?",
                                (code, rid)).fetchone()[0]
    return {"reviewed": f.get("seen") or 0, "in_market": in_market,
            "in_band": max(in_market - (f.get("filtered_band") or 0), 0),
            "prejudged_out": f.get("prejudge_no") or 0,
            "judged": judged, "accepted": count("accepted")}


def run_summary(store: Store, run: dict) -> dict:
    yield_raw = store.meta_get(f"yield:{run['id']}")
    return {"id": run["id"], "market": run["market"],
            "market_name": load_market(run["market"]).name, "brief": run["brief"],
            "status": run["status"], "credits": run["credits_used"],
            "llm_calls": run.get("llm_calls"), "params": run["params"],
            "funnel": funnel(store, run),
            "yield": json.loads(yield_raw) if yield_raw else source_yield(store, run["id"]),
            "recall": json.loads(store.meta_get(f"recall:{run['id']}") or "null")}


ROW_FIELDS = ["creator", "platforms", "market", "country", "followers_tiktok", "avg_views_tiktok",
              "views_window_tiktok", "views_range_tiktok", "subscribers_youtube", "youtube_format",
              "avg_views_youtube", "views_window_youtube", "avg_views_youtube_shorts",
              "views_window_youtube_shorts", "niche", "games", "contact", "risks", "trend",
              "decision", "fit_score", "young_gamer_appeal", "gaming_pc_relevance", "reasons",
              "evidence_quote", "found_via", "tiktok_url", "youtube_url", "market_evidence",
              "other_links"]


# ---- pages -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def api_config() -> dict:
    return {"live_enabled": bool(os.environ.get("SCOUT_ACCESS_CODE") and config.sc_key()),
            "max_budget": MAX_LIVE_BUDGET, "live_seconds": LIVE_HARD_SECONDS,
            "markets": [{"code": m.code, "name": m.name} for m in list_markets()],
            "presets": {k: v for k, v in config.PRESETS.items()}}


@app.get("/api/demo/runs")
def demo_runs() -> list[dict]:
    store = store_for("demo")
    ids = json.loads(store.meta_get("demo_runs") or "[]")
    runs = [store.get_run(i) for i in ids]
    return [run_summary(store, r) for r in runs if r]


@app.get("/api/{source}/runs/{run_id}")
def run_detail(source: str, run_id: int) -> dict:
    store = store_for(source)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run_summary(store, run)


@app.get("/api/{source}/runs/{run_id}/rows")
def run_rows(source: str, run_id: int) -> JSONResponse:
    store = store_for(source)
    if store.get_run(run_id) is None:
        raise HTTPException(404, "run not found")
    sheets = build_sheets(store, run_id)
    rows = [{**{k: r.get(k) for k in ROW_FIELDS}, "list": "shortlist"} for r in sheets["Shortlist"]]
    rows += [{**{k: r.get(k) for k in ROW_FIELDS}, "list": "maybe"} for r in sheets["Maybe"]]
    return JSONResponse(mask(rows) if source == "demo" else rows)


@app.get("/api/{source}/runs/{run_id}/export")
def run_export(source: str, run_id: int, layout: str = "full") -> Response:
    store = store_for(source)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    sheets = build_sheets(store, run_id)
    if source == "demo":
        sheets = {k: mask(v) for k, v in sheets.items()}
    if layout == "prenew":
        sheets = {"Prenew format": sheets["Prenew format"]}
    elif layout != "full":
        raise HTTPException(400, "layout must be full or prenew")
    buf = io.BytesIO()
    tmp = Path(tempfile.gettempdir()) / f"scout-export-{os.getpid()}-{run_id}.xlsx"
    write_xlsx(sheets, tmp)
    buf.write(tmp.read_bytes())
    tmp.unlink(missing_ok=True)
    name = f"creator-scout_{run['market'].lower()}_run{run_id}_{layout}.xlsx"
    return Response(buf.getvalue(), headers={"Content-Disposition": f'attachment; filename="{name}"'},
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---- live runs ---------------------------------------------------------------

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/live")
async def live_run(request: Request) -> StreamingResponse:
    body = await request.json()
    expected = os.environ.get("SCOUT_ACCESS_CODE")
    if not expected or not config.sc_key():
        raise HTTPException(503, "live runs are not enabled on this deployment")
    if not hmac.compare_digest(str(body.get("access_code", "")), expected):
        raise HTTPException(401, "wrong access code")
    try:
        market = load_market(str(body.get("market", "ee")))
    except FileNotFoundError:
        raise HTTPException(400, "unknown market")
    preset = body.get("preset", "hidden-gems")
    if preset not in config.PRESETS:
        raise HTTPException(400, "unknown preset")
    platforms = [p for p in body.get("platforms", ["tiktok", "youtube"]) if p in ("tiktok", "youtube")]
    budget = max(5, min(int(body.get("budget", 40)), MAX_LIVE_BUDGET))
    target = max(1, min(int(body.get("target", 10)), 50))
    if not _live_lock.acquire(blocking=False):
        raise HTTPException(409, "another live run is in progress; try again in a few minutes")

    (tlo, thi), (ylo, yhi) = config.PRESETS[preset]["tiktok"], config.PRESETS[preset]["youtube"]
    if body.get("band_min"):
        tlo = int(body["band_min"])
    if body.get("band_max"):
        thi = int(body["band_max"])
    store = store_for("live")
    params = {"preset": preset, "band_min": tlo, "band_max": thi, "yt_band_min": ylo,
              "yt_band_max": yhi, "target": target, "judge": "free", "platforms": platforms,
              "harvest_share": 0.5, "source": "web"}
    run_id = store.create_run(market.code, params, budget, brief="web live run")
    events: queue.Queue = queue.Queue()

    def work() -> None:
        from scout.judge import FreeJudge
        from scout.llm import LLMClient
        from scout.sources.youtube import YouTubeData
        try:
            settings = config.RunSettings(band_min=tlo, band_max=thi, yt_band_min=ylo, yt_band_max=yhi,
                                          target=target, budget=budget,
                                          extra={"harvest_share": 0.5, "platforms": platforms,
                                                 "explore_pages": 1, "max_pages_per_source": 3})
            settings.snowball_start_after = 1
            client = ScrapeCreators(store, config.sc_key(), CreditMeter(budget))
            client.run_id = run_id
            llm = LLMClient(store)
            pipe = Pipeline(store, client, market, settings, run_id, progress=events.put,
                            judge=FreeJudge(llm), llm=llm,
                            youtube=YouTubeData(store) if "youtube" in platforms else None)
            now = time.time()
            pipe.deadline, pipe.hard_deadline = now + LIVE_SECONDS, now + LIVE_HARD_SECONDS
            result = pipe.run()
            events.put({"stage": "finished", "run": run_summary(store, result)})
        except Exception as exc:  # report the type only; details go to the server log
            import traceback
            traceback.print_exc()
            events.put({"stage": "failed", "message": type(exc).__name__})
        finally:
            events.put(None)
            _live_lock.release()

    threading.Thread(target=work, daemon=True).start()

    def stream():
        yield _sse({"stage": "started", "run_id": run_id, "market": market.code, "budget": budget})
        last = time.time()
        while True:
            try:
                event = events.get(timeout=5)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                break
            if event.get("stage") not in ("finished", "failed"):
                event = {"stage": event.get("stage"), "message": event.get("message"),
                         "credits_used": event.get("credits_used"), "budget": event.get("budget"),
                         "funnel": {k: (event.get("funnel") or {}).get(k) for k in
                                    ("seen", "bucket_sure", "bucket_unsure", "filtered_band",
                                     "prejudge_no", "judged", "accepted")}}
            yield _sse(event)
            last = time.time()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
