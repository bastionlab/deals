"""Job tables in the shared deals.db. Same connection rules as app.db."""
from __future__ import annotations

import json
import re
import sqlite3
import time

from ..db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_searches (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    enabled        INTEGER NOT NULL DEFAULT 1,
    spec           TEXT NOT NULL DEFAULT '{}',   -- json, see api.JobSearch
    last_polled_at REAL,
    last_error     TEXT,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT,
    location      TEXT,
    lat REAL, lon REAL,
    postal_code   TEXT,
    work_mode     TEXT,                  -- onsite | hybrid | remote; NULL = not stated
    employment    TEXT,                  -- json list
    posted_at     REAL,
    first_seen_at REAL NOT NULL,
    salary_min    REAL, salary_max REAL, -- EUR per year
    salary_text   TEXT,
    description   TEXT,
    apply_url     TEXT,                  -- what the portal links to (employer / ATS)
    apply_email   TEXT,
    agency        INTEGER,               -- 1 = Personalvermittlung / Zeitarbeit
    lang          TEXT,
    attrs         TEXT,                  -- json
    dedupe        TEXT,                  -- normalised company|title: the same job on several portals
    detail_at     REAL,                  -- full posting read
    apply         TEXT,                  -- json from apply.resolve: {channel, label, url, email, via}
    reading       TEXT,                  -- json: the model's facts (mode, seniority, agency, lang, ...)
    read_at       REAL,
    status        TEXT,                  -- active | gone
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_seen ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe);

CREATE TABLE IF NOT EXISTS job_matches (
    search_id   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    matched_at  REAL NOT NULL,
    passed      INTEGER,                     -- 1 feed, 0 aussortiert, NULL = not judged yet
    reason      TEXT,                        -- why aussortiert (code) or the model's one-liner
    fit         INTEGER,                     -- 0-100 against CV + wish
    pros        TEXT,                        -- json list
    gaps        TEXT,                        -- json list
    judged_at   REAL,
    cv_hash     TEXT,                        -- which CV the fit was judged against
    distance_km REAL,
    verdict     TEXT,                        -- up | down
    applied_at  REAL,
    hidden      INTEGER,                     -- 1 = code said hard; kept only so the fit survives an edit
    fazit       TEXT,                        -- the model's one-liner, kept apart from reason
    PRIMARY KEY (search_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS job_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CARD = ("url", "title", "company", "location", "lat", "lon", "postal_code", "work_mode",
         "employment", "posted_at", "salary_min", "salary_max", "salary_text", "agency", "lang",
         "attrs", "dedupe")
# a re-sighted card must not wipe what a detail read learned
_RICH = ("description", "apply_url", "apply_email", "detail_at", "apply", "reading", "read_at", "status")
_JSON = ("employment", "attrs", "apply", "reading")


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        have = {r[1] for r in con.execute("PRAGMA table_info(job_matches)")}
        for col in ("hidden INTEGER", "fazit TEXT"):
            if col.split()[0] not in have:
                con.execute(f"ALTER TABLE job_matches ADD COLUMN {col}")
        if "fazit" not in have:             # until now a passed pairing kept the model's line in reason
            con.execute("UPDATE job_matches SET fazit=reason WHERE passed=1")
        con.execute("DELETE FROM job_matches WHERE search_id NOT IN (SELECT id FROM job_searches)")


def _enc(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def decode(r: sqlite3.Row | dict) -> dict:
    d = dict(r)
    for k in (*_JSON, "pros", "gaps"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = None
    return d


# --- the same job on several portals ----------------------------------------
_GENDER = re.compile(r"\(\s*(m|w|d|f|x|div|i|gn|all genders?|alle geschlechter)(\s*[/|,]\s*\w+)*\s*\)"
                     r"|\b(m|w|f)\s*/\s*(w|m|f)\s*/\s*(d|x|div)\b|\*\s*in\b|:in\b|/-?in\b", re.I)
_LEGAL = re.compile(r"\b(gmbh|ag|se|kg|kgaa|ug|mbh|e\.?\s?v|inc|ltd|llc|co|corp|gbr|ohg|holding|group|"
                    r"deutschland|germany)\b\.?", re.I)


def norm_title(t: str) -> str:
    t = _GENDER.sub(" ", t or "")
    return " ".join(re.findall(r"[a-zäöüß0-9+#]+", t.lower()))


def norm_company(c: str | None) -> str:
    c = _LEGAL.sub(" ", c or "")
    return " ".join(re.findall(r"[a-zäöüß0-9]+", c.lower().replace("&", " ")))


def dedupe_key(job: dict) -> str | None:
    company = norm_company(job.get("company"))
    return f"{company}|{norm_title(job['title'])}" if company else None


def upsert(job: dict) -> None:
    job = {**job, "dedupe": dedupe_key(job)}
    cols = ["source", "source_id", *_CARD, *[k for k in _RICH if k in job]]
    vals = {k: _enc(job.get(k)) for k in cols}
    vals["first_seen_at"] = time.time()
    upd = [f"{k}=COALESCE(excluded.{k}, {k})" for k in _CARD] + \
          [f"{k}=excluded.{k}" for k in cols if k in _RICH]
    with connect() as con:
        con.execute(
            f"""INSERT INTO jobs ({', '.join(cols)}, first_seen_at)
                VALUES ({', '.join(':' + c for c in cols)}, :first_seen_at)
                ON CONFLICT(source, source_id) DO UPDATE SET {', '.join(upd)}""", vals)


def get(source: str, source_id: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE source=? AND source_id=?", (source, source_id)).fetchone()
    return decode(r) if r else None


def known_ids(source: str) -> set[str]:
    with connect() as con:
        return {r[0] for r in con.execute("SELECT source_id FROM jobs WHERE source=?", (source,))}


def all_jobs(since: float, sid: int | None = None) -> list[dict]:
    """Live jobs young enough, plus (with sid) everything already paired with that search."""
    with connect() as con:
        return [decode(r) for r in con.execute(
            """SELECT * FROM jobs WHERE COALESCE(status,'') != 'gone' AND (COALESCE(posted_at, first_seen_at) >= ?
                 OR EXISTS (SELECT 1 FROM job_matches m WHERE m.search_id=? AND m.source=jobs.source
                            AND m.source_id=jobs.source_id))""",
            (since, sid))]


# --- searches ----------------------------------------------------------------

def searches(enabled_only: bool = False) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM job_searches" + (" WHERE enabled=1" if enabled_only else "") +
            " ORDER BY COALESCE(last_polled_at, 0) ASC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["spec"] = json.loads(d["spec"] or "{}")
        out.append(d)
    return out


def search(sid: int) -> dict | None:
    return next((s for s in searches() if s["id"] == sid), None)


def add_search(name: str, spec: dict) -> int:
    with connect() as con:
        return con.execute("INSERT INTO job_searches (name, spec, created_at) VALUES (?,?,?)",
                           (name, json.dumps(spec, ensure_ascii=False), time.time())).lastrowid


def update_search(sid: int, name: str, spec: dict, repoll: bool) -> bool:
    with connect() as con:
        return con.execute("UPDATE job_searches SET name=?, spec=?" + (", last_polled_at=NULL" if repoll else "")
                           + " WHERE id=?", (name, json.dumps(spec, ensure_ascii=False), sid)).rowcount > 0


def search_state(sid: int, error: str | None) -> None:
    with connect() as con:
        con.execute("UPDATE job_searches SET last_polled_at=?, last_error=? WHERE id=?",
                    (time.time(), error, sid))


# --- matches -----------------------------------------------------------------

def match_row(sid: int, source: str, source_id: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM job_matches WHERE search_id=? AND source=? AND source_id=?",
                        (sid, source, source_id)).fetchone()
    return decode(r) if r else None


def put_match(sid: int, job: dict, *, passed: bool | None, reason: str | None, km: float | None,
              hidden: bool = False) -> None:
    """Where a pairing stands now (poller.settle decides). Fit, verdict and applied_at are left alone."""
    with connect() as con:
        con.execute(
            """INSERT INTO job_matches (search_id, source, source_id, matched_at, passed, reason, distance_km, hidden)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT DO UPDATE SET passed=excluded.passed, reason=excluded.reason,
                   distance_km=excluded.distance_km, hidden=excluded.hidden""",
            (sid, job["source"], job["source_id"], time.time(), None if passed is None else int(passed),
             reason, km, int(hidden)))


def drop_match(sid: int, source: str, source_id: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM job_matches WHERE search_id=? AND source=? AND source_id=?",
                    (sid, source, source_id))


def set_fit(sid: int, source: str, source_id: str, *, fit: int | None, fazit: str | None,
            pros: list | None, gaps: list | None, cv_hash: str) -> None:
    """The model's score; whether it passes is settle's call."""
    with connect() as con:
        con.execute(
            """UPDATE job_matches SET fit=?, fazit=?, pros=?, gaps=?, judged_at=?, cv_hash=?
               WHERE search_id=? AND source=? AND source_id=?""",
            (fit, fazit, _enc(pros or []), _enc(gaps or []), time.time(), cv_hash, sid, source, source_id))


def unjudged(limit: int) -> list[tuple[int, dict]]:
    """Pairings code let through that the model has not scored yet, newest jobs first."""
    with connect() as con:
        rows = con.execute(
            """SELECT m.search_id, j.* FROM job_matches m
               JOIN jobs j ON j.source=m.source AND j.source_id=m.source_id
               JOIN job_searches s ON s.id=m.search_id AND s.enabled=1
               WHERE m.passed IS NULL
               ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC LIMIT ?""", (limit,)).fetchall()
    return [(r["search_id"], decode(r)) for r in rows]


def reset_fit(sid: int | None = None) -> int:
    """Every fit is stale (CV or wish changed). Follow with poller.rematch: it re-queues what code lets through."""
    with connect() as con:
        return con.execute(
            "UPDATE job_matches SET judged_at=NULL WHERE judged_at IS NOT NULL"
            + (" AND search_id=?" if sid else ""), (sid,) if sid else ()).rowcount


# --- settings (CV) -----------------------------------------------------------

def setting(key: str, default: str = "") -> str:
    with connect() as con:
        r = con.execute("SELECT value FROM job_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO job_settings (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


MAX_AGE_DEFAULT = 21 * 86400
