"""SQLite storage. One file, WAL, no ORM."""
import json
import sqlite3
import time
from pathlib import Path

from .config import DB_PATH, NEW_SELLER_DAYS

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
    status        TEXT,              -- active | reserved | gone; NULL = never rechecked
    checked_at    REAL,              -- last detail-page recheck
    gone_at       REAL,
    seller_since  REAL,              -- seller's "Aktiv seit" day; NULL = not read yet
    raw           TEXT,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS matches (
    search_id   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    matched_at  REAL NOT NULL,
    notified_at REAL,
    score       REAL,                -- AI relevance 0-100 (phase 4 adds a deal score)
    reason      TEXT,                -- the model's one-liner
    passed      INTEGER,             -- 1 ok / 0 filtered out / NULL not judged
    judged_at   REAL,
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


MIGRATIONS = {
    "matches": {
        "reason": "ALTER TABLE matches ADD COLUMN reason TEXT",
        "passed": "ALTER TABLE matches ADD COLUMN passed INTEGER",
        "judged_at": "ALTER TABLE matches ADD COLUMN judged_at REAL",
    },
    "listings": {
        "status": "ALTER TABLE listings ADD COLUMN status TEXT",
        "checked_at": "ALTER TABLE listings ADD COLUMN checked_at REAL",
        "gone_at": "ALTER TABLE listings ADD COLUMN gone_at REAL",
        "seller_since": "ALTER TABLE listings ADD COLUMN seller_since REAL",
    },
}


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    con.execute(ddl)
        prune_orphans(con)


def prune_orphans(con: sqlite3.Connection | None = None) -> int:
    """Drop matches whose search is gone.

    Deleting a search removes its matches, but a poll already in flight can
    insert more straight afterwards, and matches has no foreign key. The feed
    hides them behind its JOIN; the tab counts did not, which is how they
    surfaced. Cheap enough to run on every start.
    """
    owned = con is None
    con = con or connect()
    try:
        n = con.execute(
            "DELETE FROM matches WHERE search_id NOT IN (SELECT id FROM searches)").rowcount
        if owned:
            con.commit()
        return n
    finally:
        if owned:
            con.close()


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
    marked as notified: an edit must never fire a burst of Discord pushes.
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
                    (search_id, *key, now, now))   # judged_at stays NULL -> rejudge picks it up
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


def upsert_listing(li: dict) -> None:
    """Insert a listing, or refresh what can change on a re-seen one (price,
    shipping, and location fields an older parser missed)."""
    with connect() as con:
        con.execute(
            """INSERT INTO listings (source, source_id, url, title, description,
                   price_cents, price_type, postal_code, district, distance_km,
                   shipping, posted_at, image_url, first_seen_at, raw)
               VALUES (:source,:source_id,:url,:title,:description,:price_cents,
                   :price_type,:postal_code,:district,:distance_km,:shipping,
                   :posted_at,:image_url,:first_seen_at,:raw)
               ON CONFLICT(source, source_id) DO UPDATE SET
                   price_cents=excluded.price_cents, price_type=excluded.price_type,
                   shipping=excluded.shipping,
                   distance_km=COALESCE(excluded.distance_km, distance_km),
                   postal_code=COALESCE(excluded.postal_code, postal_code),
                   district=COALESCE(excluded.district, district)""",
            {**li, "first_seen_at": time.time()},
        )


def record_match(search_id: int, source: str, source_id: str) -> bool:
    """Link a listing to a search. Returns True if this pairing is new."""
    with connect() as con:
        cur = con.execute(
            """INSERT INTO matches (search_id, source, source_id, matched_at)
               VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
            (search_id, source, source_id, time.time()),
        )
        return cur.rowcount > 0


def set_verdict(search_id: int, source: str, source_id: str,
                verdict: dict | None, passed: bool) -> None:
    with connect() as con:
        con.execute(
            """UPDATE matches SET score=?, reason=?, passed=?, judged_at=?
               WHERE search_id=? AND source=? AND source_id=?""",
            (None if verdict is None else verdict["score"],
             None if verdict is None else verdict["reason"],
             1 if passed else 0,
             None if verdict is None else time.time(),
             search_id, source, source_id))


def unjudged(search_id: int, limit: int = 200) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT l.* FROM matches m
               JOIN listings l ON l.source=m.source AND l.source_id=m.source_id
               WHERE m.search_id=? AND m.judged_at IS NULL
               ORDER BY COALESCE(l.posted_at, l.first_seen_at) DESC LIMIT ?""",
            (search_id, limit)).fetchall()


def all_matched(search_id: int, limit: int = 500) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            """SELECT l.* FROM matches m
               JOIN listings l ON l.source=m.source AND l.source_id=m.source_id
               WHERE m.search_id=?
               ORDER BY COALESCE(l.posted_at, l.first_seen_at) DESC LIMIT ?""",
            (search_id, limit)).fetchall()


# Python twin: sources.kleinanzeigen.is_new_seller -- change both together.
NEW_SELLER_SQL = f"(l.seller_since IS NOT NULL AND l.seller_since > l.first_seen_at - {NEW_SELLER_DAYS} * 86400)"


def pending_notifications(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            f"""SELECT m.search_id, m.source, m.source_id, s.name AS search_name, l.*
               FROM matches m
               JOIN listings l ON l.source=m.source AND l.source_id=m.source_id
               JOIN searches s ON s.id=m.search_id
               WHERE m.notified_at IS NULL AND COALESCE(m.passed, 1) = 1
                 AND COALESCE(l.status, '') != 'gone' AND NOT {NEW_SELLER_SQL}
                 AND m.matched_at > strftime('%s','now') - 86400   -- no stale flood after an outage
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


# How often a listing in the feed is re-opened to see whether it sold, by age.
# Fresh ads are the ones that go within hours, and the ones worth driving to.
RECHECK_AFTER = ((24 * 3600, 20 * 60), (7 * 86400, 2 * 3600), (float("inf"), 12 * 3600))


def due_rechecks(limit: int, now: float | None = None) -> list[sqlite3.Row]:
    """Listings someone could still see in the feed, most overdue first.

    Only what the feed shows: AI-rejected and thumbed-down matches are never
    looked at again, and neither is anything already gone.
    """
    now = now or time.time()
    with connect() as con:
        rows = con.execute(
            """SELECT DISTINCT l.source, l.source_id, l.url, l.title, l.status, l.checked_at,
                      COALESCE(l.posted_at, l.first_seen_at) AS born
               FROM matches m
               JOIN listings l ON l.source=m.source AND l.source_id=m.source_id
               JOIN searches s ON s.id=m.search_id AND s.enabled=1
               WHERE COALESCE(m.passed, 1) = 1 AND COALESCE(m.verdict, '') != 'down'
                 AND COALESCE(l.status, '') != 'gone'""").fetchall()
    due = []
    for r in rows:
        age = now - (r["born"] or now)
        every = next(after for upto, after in RECHECK_AFTER if age < upto)
        overdue = now - (r["checked_at"] or 0) - every
        if overdue >= 0:
            due.append((overdue, r))
    due.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in due[:limit]]


def set_status(source: str, source_id: str, status: str | None) -> str | None:
    """Record a recheck. Returns the previous status. None = page unreadable,
    which only stamps checked_at so the listing is not retried in a tight loop."""
    now = time.time()
    with connect() as con:
        row = con.execute("SELECT status FROM listings WHERE source=? AND source_id=?",
                          (source, source_id)).fetchone()
        prev = row["status"] if row else None
        if status is None:
            con.execute("UPDATE listings SET checked_at=? WHERE source=? AND source_id=?",
                        (now, source, source_id))
        else:
            con.execute(
                """UPDATE listings SET status=?, checked_at=?,
                       gone_at=CASE WHEN ?='gone' THEN COALESCE(gone_at, ?) ELSE NULL END
                   WHERE source=? AND source_id=?""",
                (status, now, status, now, source, source_id))
    return prev


def set_seller_since(source: str, source_id: str, since: float | None) -> None:
    if since is None:
        return
    with connect() as con:
        con.execute("UPDATE listings SET seller_since=? WHERE source=? AND source_id=?",
                    (since, source, source_id))


def get_listing(source: str, source_id: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM listings WHERE source=? AND source_id=?",
                           (source, source_id)).fetchone()
