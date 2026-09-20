"""Read/write API for the dashboard.

Same trust model as dash-agent: LAN-only bind, a fixed source-IP allowlist and
a bearer token. Nothing here shells out -- the worst a caller can do is add or
delete a saved search.
"""
from __future__ import annotations

import json
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import db
from .sources import kleinanzeigen as ka

TOKEN = os.environ.get("DEALS_API_TOKEN", "")
ALLOWED = {ip for ip in os.environ.get(
    "DEALS_API_ALLOW", "192.168.1.54,192.168.1.50,127.0.0.1").split(",") if ip}

app = FastAPI(title="deals", docs_url=None, redoc_url=None)


async def guard(request: Request, authorization: str = Header(default="")) -> None:
    if request.client and request.client.host not in ALLOWED:
        raise HTTPException(403, "host not allowed")
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad token")


def _row(r) -> dict:
    d = dict(r)
    d["price_eur"] = None if d.get("price_cents") is None else d["price_cents"] / 100
    return d


@app.get("/status", dependencies=[Depends(guard)])
def status() -> dict:
    with db.connect() as con:
        searches = con.execute("SELECT * FROM searches ORDER BY id").fetchall()
        counts = con.execute(
            """SELECT COUNT(*) AS listings,
                      (SELECT COUNT(*) FROM matches) AS matches,
                      (SELECT COUNT(*) FROM matches WHERE notified_at IS NULL) AS queued
               FROM listings""").fetchone()
        day = con.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE first_seen_at > ?",
            (time.time() - 86400,)).fetchone()
    rows = [dict(s) for s in searches]
    last = [s["last_polled_at"] for s in rows if s["last_polled_at"]]
    return {
        "searches": len(rows),
        "enabled": sum(1 for s in rows if s["enabled"]),
        "last_poll": max(last) if last else None,
        "errors": [{"name": s["name"], "error": s["last_error"]} for s in rows if s["last_error"]],
        "listings": counts["listings"],
        "matches": counts["matches"],
        "queued": counts["queued"],
        "seen_24h": day["n"],
    }


@app.get("/searches", dependencies=[Depends(guard)])
def searches() -> list[dict]:
    with db.connect() as con:
        rows = con.execute("""
            SELECT s.*, (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) AS hits
            FROM searches s ORDER BY s.id""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["spec"] = json.loads(d.get("spec") or "{}")
        d["url"] = ka.build_url(d["spec"])
        out.append(d)
    return out


@app.get("/feed", dependencies=[Depends(guard)])
def feed(limit: int = 60, search: int | None = None, verdict: str | None = None,
         shipping: str | None = None) -> list[dict]:
    where, params = ["1=1"], []
    if search:
        where.append("m.search_id = ?")
        params.append(search)
    if shipping == "ship":
        where.append("l.shipping = 1")
    elif shipping == "pickup":
        where.append("l.shipping = 0")
    if verdict == "unrated":
        where.append("m.verdict IS NULL")
    elif verdict:
        where.append("m.verdict = ?")
        params.append(verdict)
    params.append(min(limit, 200))
    with db.connect() as con:
        rows = con.execute(f"""
            SELECT l.*, m.search_id, m.score, m.verdict, m.matched_at, s.name AS search_name
            FROM matches m
            JOIN listings l ON l.source=m.source AND l.source_id=m.source_id
            JOIN searches s ON s.id=m.search_id
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(l.posted_at, l.first_seen_at) DESC
            LIMIT ?""", params).fetchall()
    return [_row(r) for r in rows]


class NewSearch(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    keyword: str = Field(min_length=1, max_length=120)
    terms: list[str] = []
    exclude: list[str] = []
    min_price: int | None = None
    max_price: int | None = None
    radius_km: int = 10
    location_id: str = "l3331"
    shipping: str = "any"          # any | ship | pickup ("only" = legacy ship)


SHIPPING = {"any", "ship", "pickup"}


def _spec(body: NewSearch) -> dict:
    shipping = "ship" if body.shipping == "only" else body.shipping
    return {
        "keyword": body.keyword.strip(),
        "terms": [t for t in body.terms if t.strip()] or [body.keyword.strip()],
        "exclude": [t for t in body.exclude if t.strip()],
        "min_price": body.min_price,
        "max_price": body.max_price,
        "radius_km": max(0, min(body.radius_km, 200)),
        "location_id": body.location_id if body.location_id.startswith("l") else "l3331",
        "shipping": shipping if shipping in SHIPPING else "any",
        "offers_only": True,
    }


@app.post("/searches", dependencies=[Depends(guard)])
def add_search(body: NewSearch) -> dict:
    spec = _spec(body)
    try:
        sid = db.add_search(body.name.strip(), spec["keyword"], spec)
    except Exception as exc:
        raise HTTPException(400, f"could not add: {exc}") from exc
    return {"id": sid, "url": ka.build_url(spec)}


@app.post("/searches/{sid}/edit", dependencies=[Depends(guard)])
def edit_search(sid: int, body: NewSearch) -> dict:
    """Edit and re-apply. max_seen_id is deliberately left alone: widening a
    search must not make the next poll treat old ads as new and push them."""
    spec = _spec(body)
    try:
        ok = db.update_search(sid, body.name.strip(), spec)
    except Exception as exc:
        raise HTTPException(400, f"could not save: {exc}") from exc
    if not ok:
        raise HTTPException(404, "no such search")
    return {"id": sid, "url": ka.build_url(spec), **db.rematch(sid, spec)}


@app.post("/searches/{sid}/{action}", dependencies=[Depends(guard)])
def search_action(sid: int, action: str) -> dict:
    if action not in ("enable", "disable", "delete"):
        raise HTTPException(400, "unknown action")
    with db.connect() as con:
        if action == "delete":
            con.execute("DELETE FROM matches WHERE search_id=?", (sid,))
            n = con.execute("DELETE FROM searches WHERE id=?", (sid,)).rowcount
        else:
            n = con.execute("UPDATE searches SET enabled=? WHERE id=?",
                            (1 if action == "enable" else 0, sid)).rowcount
    if not n:
        raise HTTPException(404, "no such search")
    return {"ok": True}


class Feedback(BaseModel):
    search_id: int
    source: str
    source_id: str
    verdict: str | None = None       # up | down | null to clear


@app.post("/feedback", dependencies=[Depends(guard)])
def feedback(body: Feedback) -> dict:
    if body.verdict not in (None, "up", "down"):
        raise HTTPException(400, "verdict must be up, down or null")
    with db.connect() as con:
        n = con.execute(
            "UPDATE matches SET verdict=? WHERE search_id=? AND source=? AND source_id=?",
            (body.verdict, body.search_id, body.source, body.source_id)).rowcount
    if not n:
        raise HTTPException(404, "no such match")
    return {"ok": True}


@app.on_event("startup")
def _startup() -> None:
    db.init()
