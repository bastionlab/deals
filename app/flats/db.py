"""Flat tables in the shared deals.db. Same connection rules as app.db."""
from __future__ import annotations

import json
import sqlite3
import time

from ..db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS flat_searches (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    enabled        INTEGER NOT NULL DEFAULT 1,
    spec           TEXT NOT NULL DEFAULT '{}',   -- json, see api.FlatSearch
    seeded         INTEGER NOT NULL DEFAULT 0,   -- first poll walks a few pages back
    last_polled_at REAL,
    last_error     TEXT,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS flats (
    source        TEXT NOT NULL,         -- kleinanzeigen | inberlinwohnen
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    image_url     TEXT,
    posted_at     REAL,
    first_seen_at REAL NOT NULL,
    lat REAL, lon REAL,
    postal_code   TEXT,
    district      TEXT,
    address       TEXT,
    area          REAL,                  -- m²
    rooms         REAL,
    list_price    REAL,                  -- what the result card shows; on KA cold OR warm, the seller decides
    cold REAL, extra REAL, heating REAL,
    warm          REAL,
    warm_kind     TEXT,                  -- stated | sum | estimated; NULL = unknown
    deposit       TEXT,
    available_from TEXT,
    wbs           INTEGER,               -- 1 required / 0 not / NULL unknown
    tausch        INTEGER,
    kind          TEXT,                  -- wohnung | wg_zimmer | zwischenmiete | tausch | gesuch | sonstiges
    scam          TEXT,                  -- niedrig | mittel | hoch
    ai_note       TEXT,                  -- the model's one-liner, incl. why it smells
    seller        TEXT,
    commercial    INTEGER,
    contact       TEXT,                  -- json: {how, name, channels:[{type, value, label?}]}
    attrs         TEXT,                  -- json: the portal's own key/value facts
    detail_at     REAL,                  -- detail page read (KA); NULL = card only so far
    judged_at     REAL,                  -- model read it
    status        TEXT,                  -- active | reserved | gone
    checked_at    REAL,
    gone_at       REAL,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS flat_matches (
    search_id   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    matched_at  REAL NOT NULL,
    passed      INTEGER NOT NULL DEFAULT 1,  -- 0 = aussortiert (see reason), still viewable
    reason      TEXT,
    distance_km REAL,
    verdict     TEXT,                        -- up | down
    PRIMARY KEY (search_id, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_flats_seen ON flats(first_seen_at DESC);
"""

# card-level fields a re-seen listing may legitimately refresh
_CARD = ("url", "title", "image_url", "posted_at", "postal_code", "district", "area", "rooms",
         "list_price")
_ALL = ("description", "lat", "lon", "address", "cold", "extra", "heating", "warm", "warm_kind",
        "deposit", "available_from", "wbs", "tausch", "kind", "scam", "ai_note", "seller",
        "commercial", "contact", "attrs", "detail_at", "judged_at", "status")


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        con.execute("DELETE FROM flat_matches WHERE search_id NOT IN (SELECT id FROM flat_searches)")


def _enc(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def get(source: str, source_id: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM flats WHERE source=? AND source_id=?",
                        (source, source_id)).fetchone()
    return decode(r) if r else None


def decode(r: sqlite3.Row | dict) -> dict:
    d = dict(r)
    for k in ("contact", "attrs"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = None
    return d


def upsert(flat: dict) -> None:
    """Insert, or refresh card fields plus whatever richer fields this call carries.

    A card-only re-sighting must not wipe what a detail fetch already learned,
    so non-card columns are only written when the caller actually has them.
    """
    cols = ["source", "source_id", *_CARD, *[k for k in _ALL if k in flat]]
    vals = {k: _enc(flat.get(k)) for k in cols}
    vals["first_seen_at"] = time.time()
    upd = [f"{k}=COALESCE(excluded.{k}, {k})" for k in _CARD] + \
          [f"{k}=excluded.{k}" for k in cols if k in _ALL]
    with connect() as con:
        con.execute(
            f"""INSERT INTO flats ({', '.join(cols)}, first_seen_at)
                VALUES ({', '.join(':' + c for c in cols)}, :first_seen_at)
                ON CONFLICT(source, source_id) DO UPDATE SET {', '.join(upd)}""", vals)


def searches(enabled_only: bool = False) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM flat_searches" + (" WHERE enabled=1" if enabled_only else "") +
            " ORDER BY COALESCE(last_polled_at, 0) ASC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["spec"] = json.loads(d["spec"] or "{}")
        out.append(d)
    return out


def add_search(name: str, spec: dict) -> int:
    with connect() as con:
        return con.execute(
            "INSERT INTO flat_searches (name, spec, created_at) VALUES (?,?,?)",
            (name, json.dumps(spec, ensure_ascii=False), time.time())).lastrowid


def update_search(sid: int, name: str, spec: dict) -> bool:
    with connect() as con:
        return con.execute("UPDATE flat_searches SET name=?, spec=? WHERE id=?",
                           (name, json.dumps(spec, ensure_ascii=False), sid)).rowcount > 0


def search_state(sid: int, *, error: str | None = None, seeded: bool = False) -> None:
    with connect() as con:
        con.execute(
            "UPDATE flat_searches SET last_polled_at=?, last_error=?"
            + (", seeded=1" if seeded else "") + " WHERE id=?",
            (time.time(), error, sid))


def set_match(sid: int, source: str, source_id: str, result: tuple[str, str | None, float | None]) -> None:
    """result = (ok | soft | hard, reason, km). hard removes the pairing."""
    verdict, reason, km = result
    with connect() as con:
        if verdict == "hard":
            con.execute("DELETE FROM flat_matches WHERE search_id=? AND source=? AND source_id=?",
                        (sid, source, source_id))
            return
        con.execute(
            """INSERT INTO flat_matches (search_id, source, source_id, matched_at, passed, reason, distance_km)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT DO UPDATE SET passed=excluded.passed, reason=excluded.reason,
                                         distance_km=excluded.distance_km""",
            (sid, source, source_id, time.time(), 1 if verdict == "ok" else 0, reason, km))


def all_flats() -> list[dict]:
    with connect() as con:
        return [decode(r) for r in con.execute(
            "SELECT * FROM flats WHERE COALESCE(status, '') != 'gone'").fetchall()]


def known_ids(source: str) -> set[str]:
    with connect() as con:
        return {r[0] for r in con.execute("SELECT source_id FROM flats WHERE source=?", (source,))}


def mark_gone_except(source: str, alive: set[str]) -> int:
    """A full listing sweep is the only way to learn a flat on a list-only portal is gone."""
    now = time.time()
    with connect() as con:
        rows = [r[0] for r in con.execute(
            "SELECT source_id FROM flats WHERE source=? AND COALESCE(status,'') != 'gone'", (source,))]
        dead = [i for i in rows if i not in alive]
        con.executemany("UPDATE flats SET status='gone', gone_at=?, checked_at=? WHERE source=? AND source_id=?",
                        [(now, now, source, i) for i in dead])
        con.execute("UPDATE flats SET checked_at=? WHERE source=? AND COALESCE(status,'') != 'gone'",
                    (now, source))
    return len(dead)


RECHECK_AFTER = ((24 * 3600, 30 * 60), (7 * 86400, 3 * 3600), (float("inf"), 12 * 3600))


def due_rechecks(source: str, limit: int) -> list[dict]:
    """Detail-page rechecks for a portal where an ad can be opened by id."""
    now = time.time()
    with connect() as con:
        rows = con.execute(
            """SELECT DISTINCT f.source, f.source_id, f.url, f.title, f.status, f.checked_at,
                      COALESCE(f.posted_at, f.first_seen_at) AS born
               FROM flat_matches m JOIN flats f ON f.source=m.source AND f.source_id=m.source_id
               JOIN flat_searches s ON s.id=m.search_id AND s.enabled=1
               WHERE f.source=? AND m.passed=1 AND COALESCE(m.verdict,'') != 'down'
                 AND COALESCE(f.status,'') != 'gone'""", (source,)).fetchall()
    due = []
    for r in rows:
        every = next(after for upto, after in RECHECK_AFTER if now - (r["born"] or now) < upto)
        overdue = now - (r["checked_at"] or 0) - every
        if overdue >= 0:
            due.append((overdue, dict(r)))
    due.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in due[:limit]]


def set_status(source: str, source_id: str, status: str | None) -> None:
    now = time.time()
    with connect() as con:
        if status is None:
            con.execute("UPDATE flats SET checked_at=? WHERE source=? AND source_id=?",
                        (now, source, source_id))
        else:
            con.execute(
                """UPDATE flats SET status=?, checked_at=?,
                       gone_at=CASE WHEN ?='gone' THEN COALESCE(gone_at, ?) ELSE NULL END
                   WHERE source=? AND source_id=?""",
                (status, now, status, now, source, source_id))
