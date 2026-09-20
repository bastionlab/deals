"""SQLite storage. One file, WAL, no ORM."""
import json
import sqlite3
import time
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    enabled        INTEGER NOT NULL DEFAULT 1,
    -- what the user typed; the AI query compiler will fill the spec from this later
    raw_query      TEXT NOT NULL,
    spec           TEXT NOT NULL DEFAULT '{}',   -- json: keywords, exclude, price, radius...
    max_seen_id    INTEGER NOT NULL DEFAULT 0,   -- highest Kleinanzeigen ad id seen
    last_polled_at REAL,
    last_error     TEXT,
    empty_streak   INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    price_cents   INTEGER,           -- NULL = no price / "VB" only
    price_type    TEXT,              -- fixed | negotiable | free | none
    postal_code   TEXT,
    district      TEXT,
    distance_km   REAL,
    shipping      INTEGER NOT NULL DEFAULT 0,
    posted_at     REAL,
    image_url     TEXT,
    first_seen_at REAL NOT NULL,
    raw           TEXT,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS matches (
    search_id   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    matched_at  REAL NOT NULL,
    notified_at REAL,
    score       REAL,                -- filled from phase 4 on
    verdict     TEXT,                -- user feedback: up | down
    PRIMARY KEY (search_id, source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_new ON matches(notified_at) WHERE notified_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_listings_seen ON listings(first_seen_at DESC);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


def add_search(name: str, raw_query: str, spec: dict) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO searches (name, raw_query, spec, created_at) VALUES (?,?,?,?)",
            (name, raw_query, json.dumps(spec, ensure_ascii=False), time.time()),
        )
        return cur.lastrowid


def update_search(search_id: int, name: str, spec: dict) -> bool:
    with connect() as con:
        n = con.execute(
            "UPDATE searches SET name=?, raw_query=?, spec=? WHERE id=?",
            (name, spec.get("keyword", ""), json.dumps(spec, ensure_ascii=False), search_id),
        ).rowcount
    return n > 0


def rematch(search_id: int, spec: dict) -> dict:
    """Re-apply a search's filters to everything already stored.

    Editing a search should change the feed straight away instead of waiting for
    each listing to be re-posted. Newly matching listings are inserted already
    marked as notified: an edit must never fire a burst of Telegram pushes.
    """
    from .filters import check

    added = removed = 0
    with connect() as con:
        listings = con.execute("SELECT * FROM listings").fetchall()
        current = {(r["source"], r["source_id"]) for r in con.execute(
            "SELECT source, source_id FROM matches WHERE search_id=?", (search_id,))}
        now = time.time()
        for li in listings:
            key = (li["source"], li["source_id"])
            keep, _ = check(dict(li), spec)
            if keep and key not in current:
                con.execute(
                    """INSERT INTO matches (search_id, source, source_id, matched_at, notified_at)
                       VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (search_id, *key, now, now))
                added += 1
            elif not keep and key in current:
                con.execute(
                    "DELETE FROM matches WHERE search_id=? AND source=? AND source_id=?",
                    (search_id, *key))
                removed += 1
    return {"added": added, "removed": removed}


def active_searches() -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT * FROM searches WHERE enabled=1 ORDER BY COALESCE(last_polled_at,0) ASC"
        ).fetchall()


def upsert_listing(li: dict) -> bool:
    """Insert a listing. Returns True if it was new to us."""
    with connect() as con:
        cur = con.execute(
            """INSERT INTO listings (source, source_id, url, title, description,
                   price_cents, price_type, postal_code, district, distance_km,
                   shipping, posted_at, image_url, first_seen_at, raw)
               VALUES (:source,:source_id,:url,:title,:description,:price_cents,
                   :price_type,:postal_code,:district,:distance_km,:shipping,
                   :posted_at,:image_url,:first_seen_at,:raw)
               ON CONFLICT(source, source_id) DO NOTHING""",
            {**li, "first_seen_at": time.time()},
        )
        return cur.rowcount > 0


def record_match(search_id: int, source: str, source_id: str) -> bool:
    """Link a listing to a search. Returns True if this pairing is new."""
    with connect() as con:
        cur = con.execute(
            """INSERT INTO matches (search_id, source, source_id, matched_at)
               VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
            (search_id, source, source_id, time.time()),
        )
        return cur.rowcount > 0


def pending_notifications(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT m.search_id, m.source, m.source_id, s.name AS search_name, l.*
               FROM matches m
               JOIN listings l ON l.source=m.source AND l.source_id=m.source_id
               JOIN searches s ON s.id=m.search_id
               WHERE m.notified_at IS NULL
               ORDER BY l.posted_at DESC NULLS LAST, l.first_seen_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def mark_notified(search_id: int, source: str, source_id: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE matches SET notified_at=? WHERE search_id=? AND source=? AND source_id=?",
            (time.time(), search_id, source, source_id),
        )


def update_search_state(search_id: int, *, max_seen_id: int | None = None,
                        error: str | None = None, empty: bool = False) -> int:
    """Returns the current empty-parse streak."""
    with connect() as con:
        row = con.execute("SELECT empty_streak FROM searches WHERE id=?", (search_id,)).fetchone()
        streak = (row["empty_streak"] if row else 0) + 1 if empty else 0
        if max_seen_id is not None:
            con.execute(
                "UPDATE searches SET max_seen_id=MAX(max_seen_id,?) WHERE id=?",
                (max_seen_id, search_id),
            )
        con.execute(
            "UPDATE searches SET last_polled_at=?, last_error=?, empty_streak=? WHERE id=?",
            (time.time(), error, streak, search_id),
        )
        return streak
