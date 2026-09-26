"""SQLite persistence: creators, screenings, edges, videos, runs and the API cache.

Every account ever seen is kept. Market-specific outcomes (bucket, filter
reason, status) live in ``screenings`` keyed by market, so an account filtered
out for one market remains available to others.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    handle TEXT,
    sec_uid TEXT,
    nickname TEXT,
    region TEXT,
    region_source TEXT,
    language TEXT,
    followers INTEGER,
    following INTEGER,
    videos INTEGER,
    hearts INTEGER,
    bio TEXT,
    bio_link TEXT,
    emails TEXT,
    links TEXT,
    is_private INTEGER,
    is_organization INTEGER,
    is_commerce INTEGER,
    following_visible INTEGER,
    enriched_at REAL,
    first_seen_run INTEGER,
    first_seen_at REAL,
    last_updated REAL,
    PRIMARY KEY (platform, uid)
);
CREATE INDEX IF NOT EXISTS idx_creators_handle ON creators(platform, handle);

CREATE TABLE IF NOT EXISTS screenings (
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    bucket TEXT,
    market_evidence TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    run_id INTEGER,
    updated_at REAL,
    PRIMARY KEY (market, platform, uid)
);

CREATE TABLE IF NOT EXISTS observations (
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    observed_at REAL NOT NULL,
    followers INTEGER,
    following INTEGER,
    videos INTEGER,
    hearts INTEGER,
    source TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    platform TEXT NOT NULL,
    to_uid TEXT NOT NULL,
    kind TEXT NOT NULL,
    via TEXT NOT NULL,
    from_uid TEXT,
    run_id INTEGER NOT NULL DEFAULT 0,
    created_at REAL,
    PRIMARY KEY (platform, to_uid, kind, via, run_id)
);

CREATE TABLE IF NOT EXISTS videos (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    caption TEXT,
    create_time INTEGER,
    play_count INTEGER,
    digg_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    is_ad INTEGER,
    region TEXT,
    caption_language TEXT,
    source TEXT,
    PRIMARY KEY (platform, video_id)
);
CREATE INDEX IF NOT EXISTS idx_videos_uid ON videos(platform, uid);

CREATE TABLE IF NOT EXISTS metrics (
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    data TEXT NOT NULL,
    computed_at REAL,
    PRIMARY KEY (platform, uid)
);

CREATE TABLE IF NOT EXISTS snowball_seeds (
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    uid TEXT,
    kind TEXT NOT NULL,
    label TEXT,
    pages_fetched INTEGER DEFAULT 0,
    next_cursor TEXT,
    exhausted INTEGER DEFAULT 0,
    exhausted_reason TEXT,
    accounts_seen INTEGER DEFAULT 0,
    new_in_market INTEGER DEFAULT 0,
    credits_spent INTEGER DEFAULT 0,
    total_following INTEGER,
    added_run INTEGER,
    PRIMARY KEY (market, platform, handle)
);

CREATE TABLE IF NOT EXISTS decisions (
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    run_id INTEGER,
    decision TEXT,
    fit_score INTEGER,
    data TEXT,
    model TEXT,
    created_at REAL,
    PRIMARY KEY (market, platform, uid)
);

CREATE TABLE IF NOT EXISTS prejudgments (
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    run_id INTEGER,
    verdict TEXT NOT NULL,
    reason TEXT,
    model TEXT,
    created_at REAL,
    PRIMARY KEY (market, platform, uid)
);

CREATE TABLE IF NOT EXISTS pitches (
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    run_id INTEGER,
    language TEXT,
    subject TEXT,
    body TEXT,
    dm TEXT,
    model TEXT,
    created_at REAL,
    PRIMARY KEY (market, platform, uid)
);

CREATE TABLE IF NOT EXISTS identity_links (
    platform_a TEXT NOT NULL,
    uid_a TEXT NOT NULL,
    platform_b TEXT NOT NULL,
    uid_b TEXT NOT NULL,
    evidence TEXT,
    PRIMARY KEY (platform_a, uid_a, platform_b, uid_b)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    task TEXT,
    model TEXT,
    response TEXT NOT NULL,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief TEXT,
    market TEXT,
    params TEXT,
    budget INTEGER,
    credits_used INTEGER DEFAULT 0,
    api_calls INTEGER DEFAULT 0,
    cache_hits INTEGER DEFAULT 0,
    funnel TEXT,
    status TEXT,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS run_members (
    run_id INTEGER NOT NULL,
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    uid TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, platform, uid)
);

CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    params TEXT NOT NULL,
    status_code INTEGER,
    response TEXT NOT NULL,
    credits_charged INTEGER,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# (table, column, declaration) for columns added to the schema over time.
ADDED_COLUMNS = [
    ("videos", "region", "TEXT"),
    ("videos", "caption_language", "TEXT"),
    ("runs", "llm_calls", "INTEGER DEFAULT 0"),
    ("runs", "llm_tokens", "INTEGER DEFAULT 0"),
    ("cache", "run_id", "INTEGER"),
]

# Columns a caller may set on ``creators`` via ``upsert_creator``.
CREATOR_FIELDS = (
    "handle", "sec_uid", "nickname", "region", "region_source", "language",
    "followers", "following", "videos", "hearts", "bio", "bio_link", "emails",
    "links", "is_private", "is_organization", "is_commerce", "following_visible",
    "enriched_at",
)
JSON_FIELDS = {"emails", "links"}

# Statuses that make an account "already known" for a market on later runs.
FINAL_STATUSES = ("accepted", "rejected", "contacted", "maybe")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # Worker threads (parallel LLM calls) use the cache; callers serialise access.
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        self.conn.execute("DROP TABLE IF EXISTS seeds")
        # Edges are kept per run so each run's sources can be attributed.
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'edges'").fetchone()
        if sql and "via, run_id)" not in sql["sql"]:
            self.conn.executescript("""
                ALTER TABLE edges RENAME TO edges_old;
                CREATE TABLE edges (
                    platform TEXT NOT NULL, to_uid TEXT NOT NULL, kind TEXT NOT NULL,
                    via TEXT NOT NULL, from_uid TEXT, run_id INTEGER NOT NULL DEFAULT 0,
                    created_at REAL, PRIMARY KEY (platform, to_uid, kind, via, run_id));
                INSERT INTO edges SELECT platform, to_uid, kind, via, from_uid,
                    COALESCE(run_id, 0), created_at FROM edges_old;
                DROP TABLE edges_old;
            """)
        for table, column, decl in ADDED_COLUMNS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.conn.close()

    # ---- cache -----------------------------------------------------------

    def cache_get(self, key: str, max_age: float | None) -> dict | None:
        row = self.conn.execute("SELECT * FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if max_age is not None and time.time() - row["fetched_at"] > max_age:
            return None
        return {
            "status_code": row["status_code"],
            "response": json.loads(row["response"]),
            "fetched_at": row["fetched_at"],
        }

    def cache_put(self, key: str, endpoint: str, params: dict, status_code: int,
                  response: Any, credits_charged: int, run_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cache (key, endpoint, params, status_code, response,"
            " credits_charged, fetched_at, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, endpoint, json.dumps(params, sort_keys=True), status_code,
             json.dumps(response, ensure_ascii=False), credits_charged, time.time(), run_id),
        )
        self.conn.commit()

    def cache_stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT endpoint, COUNT(*) AS n, SUM(credits_charged) AS credits"
            " FROM cache GROUP BY endpoint ORDER BY n DESC"
        ).fetchall()
        return {r["endpoint"]: {"entries": r["n"], "credits": r["credits"] or 0} for r in rows}

    def cache_clear(self) -> int:
        n = self.conn.execute("DELETE FROM cache").rowcount
        self.conn.commit()
        return n

    # ---- meta ------------------------------------------------------------

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    # ---- runs ------------------------------------------------------------

    def create_run(self, market: str, params: dict, budget: int, brief: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (brief, market, params, budget, status, started_at)"
            " VALUES (?, ?, ?, ?, 'running', ?)",
            (brief, market, json.dumps(params), budget, time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "funnel" in fields and not isinstance(fields["funnel"], str):
            fields["funnel"] = json.dumps(fields["funnel"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
        self.conn.commit()

    def get_run(self, run_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        run["params"] = json.loads(run["params"] or "{}")
        run["funnel"] = json.loads(run["funnel"] or "{}")
        return run

    def latest_run_id(self, market: str | None = None) -> int | None:
        if market:
            row = self.conn.execute(
                "SELECT id FROM runs WHERE market = ? ORDER BY id DESC LIMIT 1", (market,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return row["id"] if row else None

    # ---- creators --------------------------------------------------------

    def get_creator(self, platform: str, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM creators WHERE platform = ? AND uid = ?", (platform, uid)
        ).fetchone()
        return _creator_row(row)

    def get_creator_by_handle(self, platform: str, handle: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM creators WHERE platform = ? AND lower(handle) = lower(?)",
            (platform, handle),
        ).fetchone()
        return _creator_row(row)

    def upsert_creator(self, platform: str, uid: str, run_id: int | None = None,
                       **fields: Any) -> bool:
        """Insert or merge a creator. ``None`` values never overwrite stored data.

        Returns True when the creator was not seen before.
        """
        now = time.time()
        data = {k: v for k, v in fields.items() if k in CREATOR_FIELDS and v is not None}
        for k in JSON_FIELDS & data.keys():
            data[k] = json.dumps(data[k], ensure_ascii=False)
        for k in ("is_private", "is_organization", "is_commerce", "following_visible"):
            if k in data:
                data[k] = int(bool(data[k]))
        exists = self.conn.execute(
            "SELECT 1 FROM creators WHERE platform = ? AND uid = ?", (platform, uid)
        ).fetchone()
        if exists:
            if data:
                cols = ", ".join(f"{k} = ?" for k in data)
                self.conn.execute(
                    f"UPDATE creators SET {cols}, last_updated = ? WHERE platform = ? AND uid = ?",
                    (*data.values(), now, platform, uid),
                )
        else:
            data.update(platform=platform, uid=uid, first_seen_run=run_id,
                        first_seen_at=now, last_updated=now)
            cols = ", ".join(data)
            marks = ", ".join("?" for _ in data)
            self.conn.execute(f"INSERT INTO creators ({cols}) VALUES ({marks})", tuple(data.values()))
        self.conn.commit()
        return not exists

    def add_observation(self, platform: str, uid: str, source: str, **counts: Any) -> None:
        self.conn.execute(
            "INSERT INTO observations (platform, uid, observed_at, followers, following, videos,"
            " hearts, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, uid, time.time(), counts.get("followers"), counts.get("following"),
             counts.get("videos"), counts.get("hearts"), source),
        )
        self.conn.commit()

    # ---- edges -----------------------------------------------------------

    def add_edge(self, platform: str, to_uid: str, kind: str, via: str,
                 from_uid: str | None = None, run_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO edges (platform, to_uid, kind, via, from_uid, run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (platform, to_uid, kind, via, from_uid, run_id or 0, time.time()),
        )
        self.conn.commit()

    def edges_to(self, platform: str, uid: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE platform = ? AND to_uid = ? ORDER BY created_at",
            (platform, uid),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- videos ----------------------------------------------------------

    def upsert_videos(self, platform: str, videos: Iterable[dict]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO videos (platform, video_id, uid, caption, create_time,"
            " play_count, digg_count, comment_count, share_count, is_ad, region, caption_language,"
            " source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (platform, v["video_id"], v["uid"], v.get("caption"), v.get("create_time"),
                 v.get("play_count"), v.get("digg_count"), v.get("comment_count"),
                 v.get("share_count"), int(bool(v.get("is_ad"))), v.get("region"),
                 v.get("caption_language"), v.get("source"))
                for v in videos
            ],
        )
        self.conn.commit()

    def videos_for(self, platform: str, uid: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM videos WHERE platform = ? AND uid = ?"
            " ORDER BY create_time DESC LIMIT ?",
            (platform, uid, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- metrics ---------------------------------------------------------

    def put_metrics(self, platform: str, uid: str, data: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO metrics (platform, uid, data, computed_at) VALUES (?, ?, ?, ?)",
            (platform, uid, json.dumps(data), time.time()),
        )
        self.conn.commit()

    def get_metrics(self, platform: str, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT data FROM metrics WHERE platform = ? AND uid = ?", (platform, uid)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    # ---- screenings ------------------------------------------------------

    def get_screening(self, market: str, platform: str, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM screenings WHERE market = ? AND platform = ? AND uid = ?",
            (market, platform, uid),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["market_evidence"] = json.loads(out["market_evidence"] or "[]")
        return out

    def set_screening(self, market: str, platform: str, uid: str, status: str,
                      reason: str | None = None, bucket: str | None = None,
                      evidence: list[str] | None = None, run_id: int | None = None) -> None:
        current = self.get_screening(market, platform, uid)
        if current:
            bucket = bucket if bucket is not None else current["bucket"]
            evidence = evidence if evidence is not None else current["market_evidence"]
            run_id = run_id if run_id is not None else current["run_id"]
        self.conn.execute(
            "INSERT OR REPLACE INTO screenings (market, platform, uid, bucket, market_evidence,"
            " status, reason, run_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (market, platform, uid, bucket, json.dumps(evidence or [], ensure_ascii=False),
             status, reason, run_id, time.time()),
        )
        self.conn.commit()

    def screenings(self, market: str, status: str | tuple[str, ...] | None = None,
                   run_id: int | None = None, platform: str | None = None) -> list[dict]:
        sql = "SELECT * FROM screenings WHERE market = ?"
        args: list[Any] = [market]
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        if status:
            statuses = (status,) if isinstance(status, str) else status
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            args += list(statuses)
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["market_evidence"] = json.loads(d["market_evidence"] or "[]")
            out.append(d)
        return out

    def add_run_member(self, run_id: int, market: str, platform: str, uid: str, status: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO run_members (run_id, market, platform, uid, status)"
            " VALUES (?, ?, ?, ?, ?)", (run_id, market, platform, uid, status))
        self.conn.commit()

    def earlier_members(self, run_id: int, market: str, platform: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT uid FROM run_members WHERE market = ? AND platform = ? AND run_id < ?",
            (market, platform, run_id)).fetchall()
        return {r["uid"] for r in rows}

    # ---- judgments -------------------------------------------------------

    def set_prejudgment(self, market: str, platform: str, uid: str, verdict: str,
                        reason: str | None, model: str | None, run_id: int | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO prejudgments (market, platform, uid, run_id, verdict, reason,"
            " model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (market, platform, uid, run_id, verdict, reason, model, time.time()))
        self.conn.commit()

    def get_prejudgment(self, market: str, platform: str, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM prejudgments WHERE market = ? AND platform = ? AND uid = ?",
            (market, platform, uid)).fetchone()
        return dict(row) if row else None

    def set_decision(self, market: str, platform: str, uid: str, decision: str,
                     fit_score: int | None, data: dict, model: str | None,
                     run_id: int | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO decisions (market, platform, uid, run_id, decision, fit_score,"
            " data, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (market, platform, uid, run_id, decision, fit_score,
             json.dumps(data, ensure_ascii=False), model, time.time()))
        self.conn.commit()

    def get_decision(self, market: str, platform: str, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE market = ? AND platform = ? AND uid = ?",
            (market, platform, uid)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["data"] = json.loads(out["data"] or "{}")
        return out

    def set_pitch(self, market: str, platform: str, uid: str, language: str | None,
                  subject: str | None, body: str | None, dm: str | None, model: str | None,
                  run_id: int | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO pitches (market, platform, uid, run_id, language, subject, body,"
            " dm, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (market, platform, uid, run_id, language, subject, body, dm, model, time.time()))
        self.conn.commit()

    def get_pitch(self, market: str, platform: str, uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pitches WHERE market = ? AND platform = ? AND uid = ?",
            (market, platform, uid)).fetchone()
        return dict(row) if row else None

    def llm_cache_get(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT model, response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return {"model": row["model"], "response": json.loads(row["response"])} if row else None

    def llm_cache_put(self, key: str, task: str, model: str, response: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache (key, task, model, response, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (key, task, model, json.dumps(response, ensure_ascii=False), time.time()))
        self.conn.commit()

    def link_identities(self, a: tuple[str, str], b: tuple[str, str], evidence: str) -> None:
        """Record that two platform accounts belong to the same creator."""
        (pa, ua), (pb, ub) = sorted([a, b])
        if (pa, ua) == (pb, ub):
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO identity_links (platform_a, uid_a, platform_b, uid_b, evidence)"
            " VALUES (?, ?, ?, ?, ?)", (pa, ua, pb, ub, evidence))
        self.conn.commit()

    def linked_accounts(self, platform: str, uid: str) -> list[tuple[str, str, str]]:
        """All accounts connected to ``(platform, uid)``, as ``(platform, uid, evidence)``."""
        seen = {(platform, uid)}
        frontier = [(platform, uid)]
        out: list[tuple[str, str, str]] = []
        while frontier:
            p, u = frontier.pop()
            rows = self.conn.execute(
                "SELECT platform_b AS p, uid_b AS u, evidence FROM identity_links"
                " WHERE platform_a = ? AND uid_a = ?"
                " UNION SELECT platform_a, uid_a, evidence FROM identity_links"
                " WHERE platform_b = ? AND uid_b = ?", (p, u, p, u)).fetchall()
            for r in rows:
                if (r["p"], r["u"]) not in seen:
                    seen.add((r["p"], r["u"]))
                    frontier.append((r["p"], r["u"]))
                    out.append((r["p"], r["u"], r["evidence"]))
        return out

    def reset_market(self, market: str) -> dict:
        """Forget a market's screening state. Creators, videos and the API cache are kept."""
        counts = {}
        for table in ("screenings", "snowball_seeds", "run_members", "decisions",
                      "prejudgments", "pitches"):
            counts[table] = self.conn.execute(f"DELETE FROM {table} WHERE market = ?",
                                              (market,)).rowcount
        self.conn.commit()
        return counts

    # ---- seeds -----------------------------------------------------------

    def add_seed(self, market: str, platform: str, handle: str, kind: str,
                 uid: str | None = None, label: str | None = None,
                 run_id: int | None = None) -> bool:
        """Register a snowball seed. Returns True when it was not known before."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO snowball_seeds (market, platform, handle, uid, kind, label,"
            " added_run) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (market, platform, handle.lower(), uid, kind, label, run_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def update_seed(self, market: str, platform: str, handle: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE snowball_seeds SET {cols} WHERE market = ? AND platform = ? AND handle = ?",
            (*fields.values(), market, platform, handle.lower()),
        )
        self.conn.commit()

    def seeds(self, market: str, platform: str = "tiktok", active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM snowball_seeds WHERE market = ? AND platform = ?"
        if active_only:
            sql += " AND exhausted = 0"
        return [dict(r) for r in self.conn.execute(sql, (market, platform)).fetchall()]


def _creator_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["emails"] = json.loads(out["emails"]) if out.get("emails") else []
    out["links"] = json.loads(out["links"]) if out.get("links") else {}
    return out
