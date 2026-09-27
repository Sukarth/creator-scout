"""Build the read-only demo snapshot from the working database.

Copies only the chosen runs and the accounts they touched. The API cache, LLM
responses and client-provided partner data are left out; partner recall is
stored as counts only.

    python scripts/build_snapshot.py --runs 2 3 9
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scout import config  # noqa: E402
from scout.export import source_yield  # noqa: E402
from scout.partners import load_partners, recall_report  # noqa: E402
from scout.store import Store  # noqa: E402

RUN_TABLES = ["screenings", "decisions", "prejudgments", "run_members"]
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def _mask(text):
    return EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text) if isinstance(text, str) else text


def mask_emails(snap: Store) -> None:
    """The snapshot is published: keep only the first letter and the domain of emails."""
    c = snap.conn
    for table, key, cols in (("creators", ("platform", "uid"), ("bio", "emails", "links")),
                             ("videos", ("platform", "video_id"), ("caption",)),
                             ("decisions", ("market", "platform", "uid"), ("data",))):
        rows = c.execute(f"SELECT {', '.join(key + cols)} FROM {table}").fetchall()
        for row in rows:
            k, vals = row[:len(key)], row[len(key):]
            new = [_mask(v) for v in vals]
            if new != list(vals):
                sets = ", ".join(f"{col} = ?" for col in cols)
                where = " AND ".join(f"{col} = ?" for col in key)
                c.execute(f"UPDATE {table} SET {sets} WHERE {where}", (*new, *k))
    c.commit()


def build(run_ids: list[int], out: Path, titles: dict[int, str] | None = None,
          no_recall: set[int] | None = None) -> None:
    config.load_dotenv()
    src_path = config.db_path()
    src = Store(src_path)
    if out.exists():
        out.unlink()
    Store(out).close()  # create the schema
    db = sqlite3.connect(out)
    db.execute("ATTACH DATABASE ? AS src", (str(src_path),))
    ids = ",".join(str(int(i)) for i in run_ids)
    db.execute(f"INSERT INTO runs SELECT * FROM src.runs WHERE id IN ({ids})")
    for table in RUN_TABLES:
        db.execute(f"INSERT INTO {table} SELECT * FROM src.{table} WHERE run_id IN ({ids})")
    db.execute(f"INSERT INTO edges SELECT * FROM src.edges WHERE run_id IN ({ids})")
    # Every account a kept run screened or linked to.
    db.execute("CREATE TEMP TABLE keep AS SELECT platform, uid FROM screenings "
               "UNION SELECT platform, to_uid FROM edges")
    db.execute("INSERT INTO temp.keep SELECT platform_b, uid_b FROM src.identity_links l "
               "JOIN temp.keep k ON k.platform = l.platform_a AND k.uid = l.uid_a")
    db.execute("INSERT INTO temp.keep SELECT platform_a, uid_a FROM src.identity_links l "
               "JOIN temp.keep k ON k.platform = l.platform_b AND k.uid = l.uid_b")
    for table in ("creators", "videos", "metrics", "observations"):
        db.execute(f"INSERT OR IGNORE INTO {table} SELECT t.* FROM src.{table} t "
                   f"JOIN (SELECT DISTINCT platform, uid FROM temp.keep) k "
                   f"ON k.platform = t.platform AND k.uid = t.uid")
    db.execute("INSERT OR IGNORE INTO identity_links SELECT l.* FROM src.identity_links l "
               "WHERE EXISTS (SELECT 1 FROM temp.keep k WHERE k.platform = l.platform_a AND k.uid = l.uid_a)")
    markets = [r[0] for r in db.execute("SELECT DISTINCT market FROM runs")]
    # Seeds the runs used, except anything derived from the client's partner list.
    for m in markets:
        db.execute("INSERT INTO snowball_seeds SELECT * FROM src.snowball_seeds "
                   "WHERE market = ? AND kind NOT IN ('partner', 'retailer')", (m,))
    db.commit()

    snap = Store(out)
    partners = load_partners()
    for rid in run_ids:
        run = src.get_run(rid)
        snap.meta_set(f"yield:{rid}", json.dumps(source_yield(src, rid)))
        if partners and rid not in (no_recall or set()):
            rep = recall_report(src, run["market"], run_id=rid, partners=partners)
            snap.meta_set(f"recall:{rid}", json.dumps({
                "found": sum(1 for r in rep if r["found"]), "total": len(rep),
                "note": "Found includes partners outside the size band."}))
    snap.meta_set("demo_runs", json.dumps(run_ids))
    for rid, title in (titles or {}).items():
        snap.conn.execute("UPDATE runs SET brief = ? WHERE id = ?", (title, rid))
    mask_emails(snap)
    snap.conn.execute("DELETE FROM cache")
    snap.conn.execute("DELETE FROM llm_cache")
    snap.conn.commit()
    snap.conn.execute("VACUUM")
    snap.close()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB) with runs {run_ids}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, nargs="+", required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "demo" / "snapshot.db")
    ap.add_argument("--title", action="append", default=[], help="RUN_ID=display title")
    ap.add_argument("--no-recall", type=int, nargs="*", default=[],
                    help="Runs whose partner recall is not stored")
    a = ap.parse_args()
    build(a.runs, a.out, {int(t.split("=", 1)[0]): t.split("=", 1)[1] for t in a.title},
          set(a.no_recall))
