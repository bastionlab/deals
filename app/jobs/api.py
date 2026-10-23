"""/jobs/* endpoints, mounted into app.api (same guard, same token)."""
from __future__ import annotations

import base64
import binascii
import json
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..flats import geo
from . import ai, cv
from . import db as jdb
from .poller import load_sources, needs_poll, rematch

MODES = ("onsite", "hybrid", "remote")
LEVELS = ("junior", "mid", "senior", "lead")
EMPLOYMENT = ("vollzeit", "teilzeit", "werkstudent", "praktikum", "ausbildung", "freelance", "minijob")


# module level: with postponed annotations FastAPI cannot see classes local to build()
class JobSearch(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    wish: str = Field(default="", max_length=1500)          # free text: what you want, what you don't
    keywords: list[str] = Field(default_factory=list)       # what the portals are asked; empty = from the wish
    modes: list[str] = Field(default_factory=lambda: list(MODES))
    place: str = Field(default="", max_length=120)
    radius_km: float = 25
    employment: list[str] = Field(default_factory=list)     # empty = any
    permanent_only: bool = False
    levels: list[str] = Field(default_factory=list)         # empty = any
    min_salary: float | None = None                         # EUR / year; unstated salaries pass
    langs: list[str] = Field(default_factory=list)          # ad language; empty = any
    no_agency: bool = True
    exclude: list[str] = Field(default_factory=list)        # words in the title
    exclude_companies: list[str] = Field(default_factory=list)
    max_age_days: int = 14
    min_fit: int = 60
    sources: list[str] = Field(default_factory=list)        # empty = all


class Upload(BaseModel):
    filename: str = Field(max_length=200)
    data: str                                               # base64 PDF
    search_id: int | None = None                            # that search's own CV; None = the shared one


class Profile(BaseModel):
    profile: dict
    search_id: int | None = None


class Wish(BaseModel):
    wish: str = Field(default="", max_length=1500)
    search_id: int | None = None                            # whose CV to suggest from


class Feedback(BaseModel):
    search_id: int
    source: str
    source_id: str
    verdict: str | None = None


class Applied(BaseModel):
    search_id: int
    source: str
    source_id: str
    applied: bool = True


def _list(v: list[str], allowed: tuple | None = None, n: int = 20) -> list[str]:
    out = []
    for x in v or []:
        x = " ".join(str(x).split())[:60]
        if x and (allowed is None or x in allowed) and x not in out:
            out.append(x)
    return out[:n]


def build(guard) -> APIRouter:
    r = APIRouter(prefix="/jobs", dependencies=[Depends(guard)])
    sources = load_sources()
    labels = {k: getattr(m, "LABEL", k) for k, m in sources.items()}

    @r.get("/status")
    def status() -> dict:
        with jdb.connect() as con:
            rows = [dict(x) for x in con.execute("SELECT * FROM job_searches ORDER BY id")]
            per = {x["source"]: x["n"] for x in con.execute(
                "SELECT source, COUNT(*) AS n FROM jobs WHERE first_seen_at > ? GROUP BY source",
                (time.time() - 30 * 86400,))}
            c = con.execute(
                """SELECT (SELECT COUNT(*) FROM jobs WHERE first_seen_at > ?) AS day,
                          (SELECT COUNT(*) FROM job_matches m JOIN job_searches s ON s.id=m.search_id
                             WHERE m.passed IS NULL AND s.enabled=1) AS pending""",
                (time.time() - 86400,)).fetchone()
        last = [s["last_polled_at"] for s in rows if s["last_polled_at"]]
        return {"searches": len(rows), "enabled": sum(1 for s in rows if s["enabled"]),
                "last_poll": max(last) if last else None,
                "errors": [{"name": s["name"], "error": s["last_error"]} for s in rows if s["last_error"]],
                "per_source": per, "seen_24h": c["day"], "pending": c["pending"],
                "sources": [{"id": k, "label": v} for k, v in labels.items()],
                "cv": bool(jdb.setting("cv_text")) or any(jdb.cv_owner(s["id"]) for s in rows)}

    # --- CV ---------------------------------------------------------------------

    def _rejudge_all(sid: int | None = None) -> int:
        """Every fit was against the old CV: back into the queue, whatever code still lets through.

        The shared CV only touches searches without their own; a search's CV only that search.
        """
        hit = [s for s in jdb.searches() if (s["id"] == sid if sid else jdb.cv_owner(s["id"]) is None)]
        for s in hit:
            jdb.reset_fit(s["id"])
        return sum(rematch(s)["queued"] for s in hit)

    def _profile(sid: int | None) -> dict:
        return json.loads(jdb.cv_setting("cv_profile", sid, "{}") or "{}")

    @r.get("/cv")
    def get_cv(search: int | None = None) -> dict:
        own = jdb.cv_owner(search)
        text = jdb.cv_setting("cv_text", search)
        base = {"own": own is not None if search else None, "shared": bool(jdb.setting("cv_text"))}
        if not text:
            return {"present": False, **base}
        return {"present": True, **base, "filename": jdb.cv_setting("cv_filename", search),
                "uploaded_at": float(jdb.cv_setting("cv_uploaded_at", search, "0") or 0), "chars": len(text),
                "profile": _profile(search)}

    @r.post("/cv")
    async def upload_cv(b: Upload) -> dict:
        try:
            data = base64.b64decode(b.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, "kein gültiges base64") from exc
        if len(data) > cv.MAX_PDF:
            raise HTTPException(413, "PDF größer als 8 MB")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, "keine PDF-Datei")
        try:
            text = cv.pdf_text(data)
        except Exception as exc:
            raise HTTPException(400, f"PDF nicht lesbar: {exc}") from exc
        if len(text) < 200:
            raise HTTPException(400, "Im PDF steht kaum Text – ist es ein Scan? Bitte ein PDF mit Text hochladen.")
        async with httpx.AsyncClient() as c:
            try:
                prof = await cv.profile(c, text)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise HTTPException(502, f"KI nicht erreichbar: {exc}") from exc
        sid = b.search_id if b.search_id and jdb.search(b.search_id) else None
        k = lambda key: jdb.cv_key(key, sid)  # noqa: E731
        jdb.set_setting(k("cv_text"), text)
        jdb.set_setting(k("cv_filename"), b.filename)
        jdb.set_setting(k("cv_uploaded_at"), str(time.time()))
        jdb.set_setting(k("cv_profile"), json.dumps(prof, ensure_ascii=False))
        jdb.set_setting(k("cv_hash"), cv.cv_hash(text))
        return {"profile": prof, "chars": len(text), "rejudge": _rejudge_all(sid)}

    @r.post("/cv/profile")
    def set_profile(b: Profile) -> dict:
        """Hand edits to the extracted profile (the model missed a skill, wrong level ...)."""
        sid = jdb.cv_owner(b.search_id)          # edits whichever CV that search reads against
        if not jdb.setting(jdb.cv_key("cv_text", sid)):
            raise HTTPException(400, "Erst einen Lebenslauf hochladen")
        p = b.profile
        clean = {k: str(p.get(k) or "")[:300] for k in ("name", "headline", "location", "education", "summary")}
        clean["summary"] = str(p.get("summary") or "")[:1200]
        for k in ("roles", "skills", "languages"):
            clean[k] = _list(p.get(k) or [], n=30)
        clean["level"] = p.get("level") if p.get("level") in LEVELS else "mid"
        try:
            clean["years"] = max(0, min(60, int(p.get("years") or 0)))
        except (TypeError, ValueError):
            clean["years"] = 0
        jdb.set_setting(jdb.cv_key("cv_profile", sid), json.dumps(clean, ensure_ascii=False))
        jdb.set_setting(jdb.cv_key("cv_hash", sid),
                        cv.cv_hash(jdb.setting(jdb.cv_key("cv_text", sid)) + json.dumps(clean, sort_keys=True)))
        return {"profile": clean, "rejudge": _rejudge_all(sid)}

    @r.post("/cv/delete")
    def delete_cv(search: int | None = None) -> dict:
        """With ?search=: drop that search's own CV (it falls back to the shared one)."""
        jdb.drop_cv(search)
        if search:
            _rejudge_all(search)
        return {"ok": True}

    @r.post("/suggest")
    async def suggest(b: Wish) -> dict:
        profile = _profile(b.search_id)
        if not b.wish.strip() and not profile:
            raise HTTPException(400, "Erst einen Wunsch eingeben oder einen Lebenslauf hochladen")
        async with httpx.AsyncClient() as c:
            try:
                return await ai.suggest(c, b.wish.strip(), profile)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise HTTPException(502, f"KI nicht erreichbar: {exc}") from exc

    # --- searches ---------------------------------------------------------------

    @r.get("/searches")
    def searches() -> list[dict]:
        return sorted(jdb.searches(), key=lambda s: s["id"])

    async def _spec(b: JobSearch, sid: int | None = None) -> dict:
        modes = _list(b.modes, MODES) or list(MODES)
        keywords = ai.tidy_keywords(_list(b.keywords, n=6))
        if not keywords:
            profile = _profile(sid)
            if not b.wish.strip() and not profile:
                raise HTTPException(400, "Suchbegriffe, Wunsch oder Lebenslauf fehlt")
            async with httpx.AsyncClient() as c:
                try:
                    keywords = (await ai.suggest(c, b.wish.strip(), profile))["keywords"]
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    raise HTTPException(502, f"KI nicht erreichbar: {exc}") from exc
            if not keywords:
                raise HTTPException(400, "Konnte keine Suchbegriffe ableiten – bitte selbst eintragen")
        spec = {"wish": b.wish.strip(), "keywords": keywords, "modes": modes,
                "place": "", "lat": None, "lon": None,
                "radius_km": max(0.0, min(float(b.radius_km or 0), 200.0))}
        if b.place.strip() and set(modes) & {"onsite", "hybrid"}:
            async with httpx.AsyncClient() as c:
                try:
                    hit = await geo.geocode(c, b.place.strip())
                except httpx.HTTPError as exc:
                    raise HTTPException(502, f"geocoder: {exc}") from exc
            if not hit:
                raise HTTPException(400, f"„{b.place}“ nicht gefunden")
            spec.update(place=hit["label"], lat=hit["lat"], lon=hit["lon"], country=hit.get("country") or "de")
        spec.update(
            employment=_list(b.employment, EMPLOYMENT), permanent_only=bool(b.permanent_only),
            levels=_list(b.levels, LEVELS),
            min_salary=b.min_salary if b.min_salary and b.min_salary > 0 else None,
            langs=_list(b.langs, ("de", "en", "fr")), no_agency=bool(b.no_agency),
            exclude=_list(b.exclude), exclude_companies=_list(b.exclude_companies),
            max_age_days=max(1, min(int(b.max_age_days or 14), 60)),
            min_fit=max(0, min(int(b.min_fit or 0), 100)),
            sources=_list(b.sources, tuple(sources)),
        )
        return spec

    @r.post("/searches")
    async def add(b: JobSearch) -> dict:
        spec = await _spec(b)
        try:
            sid = jdb.add_search(b.name.strip(), spec)
        except Exception as exc:
            raise HTTPException(400, f"could not add: {exc}") from exc
        return {"id": sid, "spec": spec, **rematch({"id": sid, "spec": spec})}

    @r.post("/searches/{sid}/edit")
    async def edit(sid: int, b: JobSearch) -> dict:
        old = jdb.search(sid)
        if not old:
            raise HTTPException(404, "no such search")
        spec = await _spec(b, sid)
        # only what the portals are asked needs a new poll; every filter is answered from stored postings
        repoll = needs_poll(old["spec"], spec)
        try:
            jdb.update_search(sid, b.name.strip(), spec, repoll)
        except Exception as exc:
            raise HTTPException(400, f"could not save: {exc}") from exc
        # the fit is scored against the wish; everything else re-applies to the fit we have
        if old["spec"].get("wish") != spec.get("wish"):
            jdb.reset_fit(sid)
        out = rematch({"id": sid, "spec": spec})
        return {"id": sid, "spec": spec, "repoll": repoll, "rejudge": out["queued"], **out}

    @r.post("/searches/{sid}/{action}")
    def action(sid: int, action: str) -> dict:
        if action not in ("enable", "disable", "delete", "run"):
            raise HTTPException(400, "unknown action")
        with jdb.connect() as con:
            if action == "delete":
                n = con.execute("DELETE FROM job_searches WHERE id=?", (sid,)).rowcount
                con.execute("DELETE FROM job_matches WHERE search_id=?", (sid,))
                con.executemany("DELETE FROM job_settings WHERE key=?", [(jdb.cv_key(k, sid),) for k in jdb.CV_KEYS])
            elif action == "run":
                n = con.execute("UPDATE job_searches SET last_polled_at=NULL WHERE id=?", (sid,)).rowcount
            else:
                n = con.execute("UPDATE job_searches SET enabled=? WHERE id=?",
                                (1 if action == "enable" else 0, sid)).rowcount
        if not n:
            raise HTTPException(404, "no such search")
        return {"ok": True}

    # --- feed -------------------------------------------------------------------

    _JOIN = """FROM job_matches m
               JOIN jobs j ON j.source=m.source AND j.source_id=m.source_id
               JOIN job_searches s ON s.id=m.search_id"""

    def _where(search: int | None, show: str) -> tuple[list[str], list]:
        # the one definition of the feed; /feed and /counts both build from it
        where, params = ["COALESCE(j.status,'') != 'gone'", "s.enabled = 1", "COALESCE(m.hidden, 0) = 0"], []
        if show == "passed":
            where.append("m.passed = 1 AND COALESCE(m.verdict,'') != 'down' AND m.applied_at IS NULL")
        elif show == "rejected":
            where.append("(m.passed = 0 OR m.verdict = 'down')")
        elif show == "applied":
            where[1] = where[2] = "1=1"         # applied stays applied, whatever the search says now
            where.append("(m.applied_at IS NOT NULL OR m.verdict = 'up')")
        elif show == "pending":
            where.append("m.passed IS NULL")
        if search:
            where.append("m.search_id = ?")
            params.append(search)
        return where, params

    def _row(row) -> dict:
        d = jdb.decode(row)
        d.pop("description", None)              # the list never shows it; keeps the payload small
        d["source_label"] = labels.get(d["source"], d["source"])
        rd = d.pop("reading", None) or {}
        d["mode"] = d.get("work_mode") or (rd.get("arbeitsmodell") if rd.get("arbeitsmodell") in MODES else None)
        d["level"] = rd.get("level") if rd.get("level") in LEVELS else None
        d["remote_region"] = rd.get("remote_raum") or None
        how = (rd.get("bewerbung") or "").strip()
        d["how"] = how if len(how) >= 10 else None    # the model sometimes answers just "url"
        d["salary_said"] = d.get("salary_text") or rd.get("gehalt") or None
        d["german"] = rd.get("deutsch") if rd.get("deutsch") not in (None, "unbekannt") else None
        return d

    @r.get("/feed")
    def feed(search: int | None = None, show: str = "passed", limit: int = 150) -> list[dict]:
        where, params = _where(search, show)
        order = "m.fit DESC, " if show in ("passed", "applied") else ""
        with jdb.connect() as con:
            rows = con.execute(f"""
                SELECT j.*, m.search_id, m.passed, m.reason, m.fit, m.pros, m.gaps, m.distance_km,
                       m.verdict, m.applied_at, m.matched_at, s.name AS search_name
                {_JOIN} WHERE {' AND '.join(where)}
                ORDER BY {order}COALESCE(j.posted_at, j.first_seen_at) DESC LIMIT ?""",
                [*params, min(limit, 400)]).fetchall()
        # one row per real job: the same posting on three portals is one card with three links
        groups: dict[str, dict] = {}
        for row in rows:
            d = _row(row)
            key = f"{d['search_id']}|{d.get('dedupe') or d['source'] + ':' + d['source_id']}"
            g = groups.get(key)
            if g is None:
                d["also"] = []
                groups[key] = d
                continue
            g["also"].append({"source": d["source"], "label": d["source_label"], "url": d["url"]})
            # the employer's own channel beats a portal's
            if (d.get("apply") or {}).get("channel", "portal") != "portal" and \
                    (g.get("apply") or {}).get("channel", "portal") == "portal":
                g["apply"] = d["apply"]
        return list(groups.values())

    @r.get("/counts")
    def counts(show: str = "passed") -> dict:
        out = {}
        with jdb.connect() as con:
            for kind in ("passed", "rejected", "pending", "applied"):
                where, params = _where(None, kind)
                out[kind] = {str(x["sid"]): x["n"] for x in con.execute(
                    f"""SELECT m.search_id AS sid, COUNT(DISTINCT COALESCE(j.dedupe, j.source || j.source_id)) AS n
                        {_JOIN} WHERE {' AND '.join(where)} GROUP BY m.search_id""", params)}
        return {"per_search": out.get(show, {}), "total": sum(out.get(show, {}).values()),
                **{k: sum(v.values()) for k, v in out.items()}}

    @r.post("/feedback")
    def feedback(b: Feedback) -> dict:
        if b.verdict not in (None, "up", "down"):
            raise HTTPException(400, "verdict must be up, down or null")
        with jdb.connect() as con:
            n = con.execute("UPDATE job_matches SET verdict=? WHERE search_id=? AND source=? AND source_id=?",
                            (b.verdict, b.search_id, b.source, b.source_id)).rowcount
        if not n:
            raise HTTPException(404, "no such match")
        return {"ok": True}

    @r.post("/applied")
    def applied(b: Applied) -> dict:
        with jdb.connect() as con:
            n = con.execute("UPDATE job_matches SET applied_at=? WHERE search_id=? AND source=? AND source_id=?",
                            (time.time() if b.applied else None, b.search_id, b.source, b.source_id)).rowcount
        if not n:
            raise HTTPException(404, "no such match")
        return {"ok": True}

    @r.get("/job/{source}/{source_id}")
    def one(source: str, source_id: str) -> dict:
        j = jdb.get(source, source_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j

    return r
