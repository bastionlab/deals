"""/flats/* endpoints, mounted into app.api (same guard, same token)."""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db as fdb
from ..sources.kleinanzeigen import slug
from . import bienici, dedupe, geo, ka, message
from .poller import CYCLE, rematch


# module level: with postponed annotations FastAPI cannot see classes local to build()
class FlatSearch(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    mode: str = "city"                       # city | pin
    place: str = Field(default="", max_length=120)
    ref: str | None = Field(default=None, max_length=24)   # city: the candidate picked ("R62422")
    lat: float | None = None                 # pin only
    lon: float | None = None
    radius_km: float = 0                     # pin: the radius; city: extra margin around it
    min_area: float | None = None
    max_area: float | None = None
    min_rooms: float | None = None
    max_rooms: float | None = None
    min_warm: float | None = None
    max_warm: float | None = None
    tausch: str = "exclude"                  # exclude | include | only
    wbs: str = "any"                         # any | without | only
    allow_temporary: bool = False            # WG rooms and short lets stay in the feed


class Profile(BaseModel):
    text: str = Field(default="", max_length=4000)
    search_id: int | None = None             # a search's own text; None = the shared one


class Draft(BaseModel):
    source: str
    source_id: str
    search_id: int | None = None             # whose "Über uns" writes it
    fresh: bool = False                      # write a new one even if a current draft exists


class Feedback(BaseModel):
    search_id: int
    source: str
    source_id: str
    verdict: str | None = None


def build(guard) -> APIRouter:
    r = APIRouter(prefix="/flats", dependencies=[Depends(guard)])

    @r.get("/status")
    def status() -> dict:
        with fdb.connect() as con:
            rows = [dict(x) for x in con.execute("SELECT * FROM flat_searches ORDER BY id")]
            per = {x["source"]: x["n"] for x in con.execute(
                "SELECT source, COUNT(*) AS n FROM flats WHERE COALESCE(status,'') != 'gone' GROUP BY source")}
            c = con.execute(
                """SELECT (SELECT COUNT(*) FROM flats WHERE first_seen_at > ?) AS day,
                          (SELECT COUNT(*) FROM flats WHERE judged_at IS NOT NULL) AS read,
                          (SELECT COUNT(*) FROM flats WHERE status = 'gone') AS gone""",
                (time.time() - 86400,)).fetchone()
        last = [s["last_polled_at"] for s in rows if s["last_polled_at"]]
        return {"searches": len(rows), "enabled": sum(1 for s in rows if s["enabled"]),
                "last_poll": max(last) if last else None, "cycle": CYCLE,
                "errors": [{"name": s["name"], "error": s["last_error"]} for s in rows if s["last_error"]],
                "per_source": per, "seen_24h": c["day"], "read_by_ai": c["read"], "gone": c["gone"]}

    @r.get("/searches")
    def searches() -> list[dict]:
        out = []
        for s in fdb.searches():
            s["url"] = ka.build_url(s["spec"]) if s["spec"].get("ka_location") else None
            out.append(s)
        out.sort(key=lambda s: s["id"])
        return out

    # 👍 flats live in their own list (/liked), under any search they were liked in
    _LIKED = """EXISTS (SELECT 1 FROM flat_matches u WHERE u.source=f.source AND u.source_id=f.source_id
                           AND u.verdict='up')"""

    _OUT = "(m.passed = 0 OR COALESCE(f.status,'') = 'reserved' OR COALESCE(m.verdict,'') = 'down')"
    _JOIN = """FROM flat_matches m
               JOIN flats f ON f.source=m.source AND f.source_id=m.source_id
               JOIN flat_searches s ON s.id=m.search_id"""

    def _cards(search: int | None, show: str) -> list[dict]:
        # the one definition of the feed; /feed and /counts both build from it.
        # Copies of one flat (dedupe.py) are judged as one: 👍 or 👎 on any copy, or
        # reserved anywhere, moves them all; aussortiert only if every copy is.
        # a flat posted over a week ago is long promised to someone; the 👍 list keeps those
        where, params = ["COALESCE(f.status, '') != 'gone'",
                         "COALESCE(f.posted_at, f.first_seen_at) >= ?"], [time.time() - fdb.MAX_AGE]
        if search:
            where.append("m.search_id = ?")
            params.append(search)
        with fdb.connect() as con:
            rows = [dict(x) for x in con.execute(f"""
                SELECT f.*, m.search_id, m.passed, m.reason, m.distance_km, m.verdict, m.matched_at,
                       s.name AS search_name, {_LIKED} AS liked, {_OUT} AS out
                {_JOIN} WHERE {' AND '.join(where)}
                ORDER BY COALESCE(f.posted_at, f.first_seen_at) DESC""", params)]
        out = []
        for g in dedupe.group(rows):
            if any(r["liked"] for r in g):
                continue
            rejected = all(r["out"] for r in g) or \
                any(r["verdict"] == "down" or r["status"] == "reserved" for r in g)
            if (show == "passed" and rejected) or (show == "rejected" and not rejected):
                continue
            card = dedupe.pick(g, (lambda r: r["verdict"] == "down") if rejected else (lambda r: not r["out"]))
            for k in ("liked", "out", "description"):  # the list never shows the text; keeps the payload small
                card.pop(k, None)
            out.append(card)
        out.sort(key=lambda c: c.get("posted_at") or c["first_seen_at"], reverse=True)
        return out

    @r.get("/feed")
    def feed(search: int | None = None, show: str = "passed", limit: int = 100) -> list[dict]:
        return [fdb.decode(c) for c in _cards(search, show)[:min(limit, 300)]]

    @r.get("/liked")
    def liked() -> list[dict]:
        """Every 👍 flat once, gone ones included: the user wants to see THAT it went."""
        with fdb.connect() as con:
            rows = con.execute(f"""
                SELECT f.*, m.search_id, m.passed, m.reason, m.distance_km, m.verdict, m.matched_at,
                       s.name AS search_name
                {_JOIN} WHERE m.verdict = 'up'
                GROUP BY f.source, f.source_id
                ORDER BY COALESCE(f.status, '') = 'gone', COALESCE(f.posted_at, f.first_seen_at) DESC""").fetchall()
        out = []
        for g in dedupe.group([dict(x) for x in rows]):
            d = fdb.decode(dedupe.pick(g, lambda r: r["status"] != "gone"))
            d.pop("description", None)
            out.append(d)
        return out

    @r.get("/counts")
    def counts(show: str = "passed") -> dict:
        # a flat two searches both found counts once in the total, once in each tab
        per = {str(s["id"]): n for s in fdb.searches() if (n := len(_cards(s["id"], show)))}
        rejected = len(_cards(None, "rejected"))
        with fdb.connect() as con:          # the 👍 list, folded the same way /liked folds it
            liked = len(dedupe.group([dict(x) for x in con.execute(f"""
                SELECT f.*, m.verdict {_JOIN} WHERE m.verdict = 'up' GROUP BY f.source, f.source_id""")]))
        return {"per_search": per, "total": len(_cards(None, show)), "sorted_out": 0 if show == "all" else rejected,
                "passed": len(_cards(None, "passed")), "rejected": rejected, "liked": liked}

    @r.get("/geocode")
    async def geocode(q: str | None = None, lat: float | None = None, lon: float | None = None,
                      ref: str | None = None) -> dict:
        """Resolve a place name (or a pin) for the search form's preview.

        A name answers the best hit plus `options` (up to 5) to choose from; ref = exactly that one.
        """
        async with httpx.AsyncClient() as c:
            try:
                if lat is not None and lon is not None:
                    hit = await geo.reverse(c, lat, lon)
                    if hit:
                        hit.update(lat=lat, lon=lon)
                elif ref:
                    hit = await geo.lookup(c, ref)
                elif q and q.strip():
                    opts = await geo.places(c, q.strip())
                    hit = {**opts[0], "options": opts} if opts else None
                else:
                    raise HTTPException(400, "q or lat/lon required")
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"geocoder: {exc}") from exc
        if not hit:
            raise HTTPException(404, "Ort nicht gefunden")
        return hit

    async def _spec(b: FlatSearch) -> dict:
        """Resolve the area once, at save time, so polling and matching never geocode."""
        mode = "pin" if b.mode == "pin" else "city"
        async with httpx.AsyncClient() as c:
            try:
                if mode == "pin":
                    if b.lat is None or b.lon is None:
                        raise HTTPException(400, "Pin fehlt")
                    rev = await geo.reverse(c, b.lat, b.lon) or {}
                    area = {"lat": b.lat, "lon": b.lon, "bbox": None,
                            "place": b.place.strip() or rev.get("label") or f"{b.lat:.4f}, {b.lon:.4f}"}
                    hints = (rev.get("postcode"), rev.get("city"))
                    cc = rev.get("country") or "de"
                else:
                    if not b.place.strip():
                        raise HTTPException(400, "Ort fehlt")
                    hit = (await geo.lookup(c, b.ref) if b.ref else None) or await geo.geocode(c, b.place.strip())
                    if not hit:
                        raise HTTPException(400, f"„{b.place}“ nicht gefunden")
                    area = {"lat": hit["lat"], "lon": hit["lon"], "bbox": hit["bbox"], "place": hit["label"],
                            "shape": hit["shape"], "ref": hit["ref"], "full": hit["full"]}
                    if not hit["shape"]:
                        # only a point is known (a Kiez, a square): a circle round it, or nothing would match
                        mode = "pin"
                        area["bbox"] = None
                        b.radius_km = max(float(b.radius_km or 0), geo.POINT_RADIUS_KM)
                    hints = (hit["label"], hit.get("city"), hit.get("postcode"))
                    cc = hit.get("country") or "de"
                # each portal only knows its own country
                kaloc = await geo.ka_location(c, *hints) if cc == "de" else None
                zones = await bienici.zone_ids(c, *hints) if cc == "fr" else []
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"geocoder: {exc}") from exc
        radius = max(0.0, min(float(b.radius_km or 0), 100.0))
        spec = {"mode": mode, **area, "radius_km": radius, "country": cc}
        if zones:
            spec["bienici_zones"] = zones
        # KA searches a circle round its own place; make it big enough to hold the whole
        # area, then match.check trims to the exact shape with real coordinates
        if kaloc:
            ka_place = kaloc[1].split(" - ")[0]
            # a plain city search where KA knows that very city: its own boundary (r0)
            # fits best. Anything else: a circle large enough for the whole area.
            same_city = mode == "city" and radius == 0 and ka_place.lower() == area["place"].lower()
            spec.update(ka_location=kaloc[0], ka_slug=slug(ka_place),
                        ka_radius=0 if same_city else geo.ka_radius(geo.reach_km(spec) + 2))
        pos = lambda v: v if v and v > 0 else None  # noqa: E731
        spec.update(
            min_area=pos(b.min_area), max_area=pos(b.max_area),
            min_rooms=pos(b.min_rooms), max_rooms=pos(b.max_rooms),
            min_warm=pos(b.min_warm), max_warm=pos(b.max_warm),
            tausch=b.tausch if b.tausch in ("exclude", "include", "only") else "exclude",
            wbs=b.wbs if b.wbs in ("any", "without", "only") else "any",
            allow_temporary=bool(b.allow_temporary),
        )
        return spec

    @r.post("/searches")
    async def add(b: FlatSearch) -> dict:
        spec = await _spec(b)
        try:
            sid = fdb.add_search(b.name.strip(), spec)
        except Exception as exc:
            raise HTTPException(400, f"could not add: {exc}") from exc
        return {"id": sid, "spec": spec, **rematch({"id": sid, "spec": spec})}

    @r.post("/searches/{sid}/edit")
    async def edit(sid: int, b: FlatSearch) -> dict:
        spec = await _spec(b)
        try:
            ok = fdb.update_search(sid, b.name.strip(), spec)
        except Exception as exc:
            raise HTTPException(400, f"could not save: {exc}") from exc
        if not ok:
            raise HTTPException(404, "no such search")
        return {"id": sid, "spec": spec, **rematch({"id": sid, "spec": spec})}

    @r.post("/searches/{sid}/{action}")
    def action(sid: int, action: str) -> dict:
        if action not in ("enable", "disable", "delete"):
            raise HTTPException(400, "unknown action")
        with fdb.connect() as con:
            if action == "delete":
                n = con.execute("DELETE FROM flat_searches WHERE id=?", (sid,)).rowcount
                con.execute("DELETE FROM flat_matches WHERE search_id=?", (sid,))
                con.execute("DELETE FROM flat_settings WHERE key=?", (f"profile@{sid}",))
            else:
                n = con.execute("UPDATE flat_searches SET enabled=? WHERE id=?",
                                (1 if action == "enable" else 0, sid)).rowcount
        if not n:
            raise HTTPException(404, "no such search")
        return {"ok": True}

    @r.post("/feedback")
    def feedback(b: Feedback) -> dict:
        if b.verdict not in (None, "up", "down"):
            raise HTTPException(400, "verdict must be up, down or null")
        with fdb.connect() as con:
            n = con.execute(
                "UPDATE flat_matches SET verdict=? WHERE search_id=? AND source=? AND source_id=?",
                (b.verdict, b.search_id, b.source, b.source_id)).rowcount
        if not n:
            raise HTTPException(404, "no such match")
        return {"ok": True}

    def _profile_key(sid: int | None) -> str:
        return f"profile@{sid}" if sid else "profile"

    def profile_for(sid: int | None) -> str:
        """A search's own text if it has one, else the shared one."""
        return (sid and fdb.setting(_profile_key(sid)).strip()) or fdb.setting("profile")

    @r.get("/profile")
    def get_profile(search: int | None = None) -> dict:
        own = fdb.setting(_profile_key(search)) if search else ""
        return {"text": own or fdb.setting("profile"), "own": bool(own.strip()) if search else None,
                "shared": fdb.setting("profile")}

    @r.post("/profile")
    def set_profile(b: Profile) -> dict:
        # an empty text on a search drops its own profile: it falls back to the shared one
        fdb.set_setting(_profile_key(b.search_id), b.text.strip())
        return {"ok": True}

    @r.post("/message")
    async def draft(b: Draft) -> dict:
        """The cached draft if it was written from the current profile, else a new one."""
        profile = profile_for(b.search_id)
        if not profile.strip():
            raise HTTPException(400, "Erst ein Profil anlegen: wer ihr seid, Name, Telefon …")
        f = fdb.get(b.source, b.source_id)
        if not f:
            raise HTTPException(404, "no such flat")
        ph = message.profile_hash(profile)
        old = fdb.message(b.source, b.source_id)
        if old and old["profile_hash"] == ph and not b.fresh:
            return {"text": old["text"], "missing": old["missing"], "cached": True}
        async with httpx.AsyncClient() as c:
            try:
                out = await message.write(c, f, profile)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise HTTPException(502, f"KI nicht erreichbar: {exc}") from exc
        fdb.save_message(b.source, b.source_id, ph, out["text"], out["missing"])
        return {"text": out["text"], "missing": out["missing"], "cached": False}

    @r.get("/flat/{source}/{source_id}")
    def one(source: str, source_id: str) -> dict:
        f = fdb.get(source, source_id)
        if not f:
            raise HTTPException(404, "no such flat")
        return f

    return r
