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
    for table, key, cols in (("creators", ("platform", "uid"), ("bio", "bio_link", "emails", "links")),
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


TIE_WORDS = re.compile(r"(\bPR\b|\bads?\b|mainos|werbung|reklaam|sponsor|ladder|collab|campaign|"
                       r"caption|[#@])", re.I)
SEPARATORS = re.compile(r"(\s*(?:[;,]|\band\b|\.(?=\s|$))\s*)")


def drop_brand_ties(text: str, brand: re.Pattern) -> str:
    """Remove the parts of a sentence that tie the creator to the brand (an ad, a
    sponsorship, a campaign), keeping the rest. Plain mentions such as "fits the
    brand's audience" stay."""
    if not isinstance(text, str) or not brand.search(text):
        return text
    parts = SEPARATORS.split(text)  # segment, separator, segment, ...
    segments, seps = parts[0::2], [""] + parts[1::2]
    kept = [(sep, seg) for sep, seg in zip(seps, segments)
            if not (brand.search(seg) and TIE_WORDS.search(seg))]
    out = "".join(sep + seg for sep, seg in kept).strip()
    out = re.sub(r"^(?:[;,.]|and\b)\s*", "", out)
    out = re.sub(r"\s*(?:[;,]|\band)\s*$", "", out).strip()
    out = out[:1].upper() + out[1:]
    if out and out[-1] not in ".!?":
        out += "."
    return out


def scrub_brand(snap: Store, name: str) -> None:
    """The snapshot is published: leave out anything showing an existing deal between
    a creator and the brand, since that would hint at the client's private partner
    list. Evidence quotes naming the brand are replaced by another verbatim line."""
    brand = re.compile(re.escape(name), re.I)
    c = snap.conn
    for market, platform, uid, raw in c.execute(
            "SELECT market, platform, uid, data FROM decisions").fetchall():
        d = json.loads(raw)
        new = dict(d)
        for key in ("reasons", "summary"):
            new[key] = drop_brand_ties(d.get(key), brand)
        for key in ("sponsors_mentioned", "brand_safety_flags"):
            if isinstance(d.get(key), list):
                new[key] = [x for x in d[key] if not brand.search(str(x))]
        if isinstance(d.get("evidence_quote"), str) and brand.search(d["evidence_quote"]):
            bio = (c.execute("SELECT bio FROM creators WHERE platform = ? AND uid = ?",
                             (platform, uid)).fetchone() or [None])[0]
            captions = [r[0] for r in c.execute(
                "SELECT caption FROM videos WHERE platform = ? AND uid = ? AND caption != '' "
                "ORDER BY create_time DESC", (platform, uid))]
            candidates = [t for t in [bio, *captions] if t and not brand.search(t)]
            new["evidence_quote"] = candidates[0][:200] if candidates else ""
        if new != d:
            c.execute("UPDATE decisions SET data = ? WHERE market = ? AND platform = ? AND uid = ?",
                      (json.dumps(new, ensure_ascii=False), market, platform, uid))
    for market, platform, uid, reason in c.execute(
            "SELECT market, platform, uid, reason FROM screenings WHERE reason IS NOT NULL").fetchall():
        cleaned = drop_brand_ties(reason, brand)
        if cleaned != reason:
            c.execute("UPDATE screenings SET reason = ? WHERE market = ? AND platform = ? AND uid = ?",
                      (cleaned, market, platform, uid))
    for platform, uid, raw in c.execute("SELECT platform, uid, data FROM metrics").fetchall():
        m = json.loads(raw)
        new = {**m, **{k: [x for x in m[k] if not brand.search(str(x))]
                       for k in ("sponsors", "competitors") if isinstance(m.get(k), list)}}
        if new != m:
            c.execute("UPDATE metrics SET data = ? WHERE platform = ? AND uid = ?",
                      (json.dumps(new, ensure_ascii=False), platform, uid))
    c.execute("UPDATE videos SET caption = '' WHERE caption LIKE ?", (f"%{name}%",))
    _drop_brand_accounts(c, name)
    _neutral_brand_mentions(c, brand)
    c.commit()


def _drop_brand_accounts(c, name: str) -> None:
    """The brand's own accounts are not creators; leave them out of the published data."""
    like = f"%{name}%"
    accounts = c.execute("SELECT platform, uid FROM creators WHERE handle LIKE ? OR nickname LIKE ?",
                         (like, like)).fetchall()
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for platform, uid in accounts:
        for t in tables:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
            if {"platform", "uid"} <= cols:
                c.execute(f"DELETE FROM {t} WHERE platform = ? AND uid = ?", (platform, uid))
            if {"platform", "to_uid"} <= cols:
                c.execute(f"DELETE FROM {t} WHERE platform = ? AND (to_uid = ? OR from_uid = ?)",
                          (platform, uid, uid))
            if {"platform_a", "uid_a"} <= cols:
                c.execute(f"DELETE FROM {t} WHERE (platform_a = ? AND uid_a = ?) OR (platform_b = ? AND uid_b = ?)",
                          (platform, uid, platform, uid))
    c.execute("DELETE FROM snowball_seeds WHERE handle LIKE ?", (like,))
    c.execute("DELETE FROM edges WHERE via LIKE ?", (like,))


def neutral_brand(text, brand: re.Pattern):
    """Plain mentions of the client ("fits Prenew's audience") become "the brand"."""
    if not isinstance(text, str) or not brand.search(text):
        return text

    def repl(m):
        word = "the brand's" if m.group(1) else "the brand"
        start = m.start() == 0 or text[:m.start()].rstrip().endswith((".", "!", "?", ":"))
        return word[0].upper() + word[1:] if start else word
    return re.sub(brand.pattern + r"(['’]s)?", repl, text, flags=re.I)


def _neutral_brand_mentions(c, brand: re.Pattern) -> None:
    for market, platform, uid, raw in c.execute(
            "SELECT market, platform, uid, data FROM decisions").fetchall():
        d = json.loads(raw)
        new = {k: ([neutral_brand(x, brand) for x in v] if isinstance(v, list) else neutral_brand(v, brand))
               for k, v in d.items()}
        if new != d:
            c.execute("UPDATE decisions SET data = ? WHERE market = ? AND platform = ? AND uid = ?",
                      (json.dumps(new, ensure_ascii=False), market, platform, uid))
    for table in ("screenings", "prejudgments"):
        for market, platform, uid, reason in c.execute(
                f"SELECT market, platform, uid, reason FROM {table} WHERE reason IS NOT NULL").fetchall():
            cleaned = neutral_brand(reason, brand)
            if cleaned != reason:
                c.execute(f"UPDATE {table} SET reason = ? WHERE market = ? AND platform = ? AND uid = ?",
                          (cleaned, market, platform, uid))


def build(run_ids: list[int], out: Path, titles: dict[int, str] | None = None,
          no_recall: set[int] | None = None, brand: str | None = None) -> None:
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
    if brand:
        scrub_brand(snap, brand)
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
    ap.add_argument("--brand", default="prenew",
                    help="Client brand whose existing creator deals are left out of the snapshot")
    a = ap.parse_args()
    build(a.runs, a.out, {int(t.split("=", 1)[0]): t.split("=", 1)[1] for t in a.title},
          set(a.no_recall), a.brand)
