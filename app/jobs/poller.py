"""Job hunter loop, a task inside deals-poller next to the deals and flats loops.

Every TICK:
1. Each search that is due (POLL_EVERY) asks every source, once per leg and
   keyword: a local leg (place + radius) when you want onsite/hybrid, a remote
   leg when you want remote. Every posting goes through match.check for every
   search; known postings only refresh their card.
2. Up to JUDGE_PER_TICK pairings that code let through are read: the full
   posting (source.detail), where to apply (apply.resolve), then the model
   against CV + wish (ai.read) and match.after_reading decides feed or aussortiert.
   Ollama down = the pairing stays unread and is retried next tick.

Each source has its own limiter: another host, another budget.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time

import httpx

from .. import discord
from . import ai, cv, match
from . import db as jdb
from . import notify as jnotify
from .sources.common import HEADERS, Limiter

log = logging.getLogger("jobs")

TICK = 120                   # s between loop passes; "search now" waits at most this long
POLL_EVERY = 2 * 3600        # per search; postings live for weeks, two hours is plenty fresh
JUDGE_PER_TICK = 40          # ~8 s each on qwen3.5:9b
# web searches for the employer's own posting: the search engines suspend after
# bursts of ~50 quick queries, and one job can take 2
FIND_PER_TICK = 12
MAX_PAGES = 2
SOURCE_NAMES = ("ba", "stepstone", "linkedin", "xing", "arbeitnow", "berlinstartupjobs",
                "remotive", "himalayas", "jobicy", "remoteok", "weworkremotely")


def load_sources() -> dict:
    out = {}
    for name in SOURCE_NAMES:
        try:
            mod = importlib.import_module(f".sources.{name}", __package__)
        except Exception as exc:                # a source being rewritten must not stop the rest
            log.warning("source %s not loaded: %s", name, exc)
            continue
        out[mod.SOURCE] = mod
    return out


SOURCES: dict = {}
LIMITERS: dict[str, Limiter] = {}
COOLDOWN = 1800              # a portal that blocked us (429/999/captcha) is left alone this long
_resting: dict[str, float] = {}


def _blocked(name: str, exc: Exception) -> None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (403, 429, 999) or type(exc).__name__ == "Blocked":
        _resting[name] = time.time() + COOLDOWN
        log.warning("%s blocked us (%s): pausing it %d min", name, status or type(exc).__name__, COOLDOWN // 60)


def resting(name: str) -> bool:
    return _resting.get(name, 0) > time.time()


def _limiter(mod) -> Limiter:
    if mod.SOURCE not in LIMITERS:
        LIMITERS[mod.SOURCE] = Limiter(getattr(mod, "INTERVAL", 6.0))
    return LIMITERS[mod.SOURCE]


# a student / trainee search must ASK for that, or every portal answers with full-time jobs
STUDENT_TERMS = {"werkstudent": "Werkstudent", "praktikum": "Praktikum", "ausbildung": "Ausbildung"}


def student_keywords(spec: dict) -> list[str]:
    kws = spec.get("keywords") or []
    want = set(spec.get("employment") or [])
    terms = [t for k, t in STUDENT_TERMS.items() if k in want]
    if not terms or not want <= set(STUDENT_TERMS) | {"teilzeit", "minijob"}:
        return kws
    out = []
    for t in terms:
        for kw in kws:
            q = kw if t.lower() in kw.lower() else f"{t} {kw}"
            out.append(" ".join(q.split()[:3]))
        out.append(t)            # "Werkstudent" alone: the reading sorts the field out
    return list(dict.fromkeys(out))[:8]


def legs(spec: dict) -> list[dict]:
    """The portal queries one search needs."""
    modes = set(spec.get("modes") or match.MODE_DE)
    base = {"keywords": student_keywords(spec), "max_age_days": spec.get("max_age_days") or 21,
            "no_agency": spec.get("no_agency", True),        # BA can drop recruiters server-side
            "max_pages": MAX_PAGES}
    out = []
    if modes & {"onsite", "hybrid"}:
        out.append({**base, "place": spec.get("place") or None, "lat": spec.get("lat"), "lon": spec.get("lon"),
                    "radius_km": spec.get("radius_km") or 0, "remote": False})
    if "remote" in modes:
        out.append({**base, "place": None, "lat": None, "lon": None, "radius_km": 0, "remote": True})
    return out


def settle(sid: int, job: dict, spec: dict) -> str:
    """Where one pairing stands, from code plus the stored reading -> hidden | out | in | queued.

    Only a pairing with no current fit is queued for the model; a changed filter is
    re-applied to the fit it already has, so editing a search costs no model time.
    """
    verdict, reason, km = match.check(job, spec)
    row = jdb.match_row(sid, job["source"], job["source_id"])
    judged = bool(row and row["judged_at"] and job.get("reading"))
    if verdict == "hard":
        # keep what would be expensive (or yours) to lose: the fit, your 👍/👎, "beworben"
        if judged or (row and (row["verdict"] or row["applied_at"])):
            jdb.put_match(sid, job, passed=False, reason=reason, km=km, hidden=True)
        elif row:
            jdb.drop_match(sid, job["source"], job["source_id"])
        return "hidden"
    if verdict == "soft":
        jdb.put_match(sid, job, passed=False, reason=reason, km=km)
        return "out"
    if not judged:
        jdb.put_match(sid, job, passed=None, reason=None, km=km)
        return "queued"
    fazit = row["fazit"] or job["reading"].get("fazit")
    passed, why = match.after_reading(job, {**job["reading"], "fit": row["fit"] or 0, "fazit": fazit}, spec, km)
    jdb.put_match(sid, job, passed=passed, reason=why or fazit, km=km)
    return "in" if passed else "out"


def match_all(job: dict, searches: list[dict]) -> None:
    for s in searches:
        settle(s["id"], job, s["spec"])


def rematch(search: dict) -> dict:
    """Apply one search to everything stored -- after add or edit. Never fetches, never asks the model."""
    out = {"hidden": 0, "out": 0, "in": 0, "queued": 0}
    since = time.time() - (search["spec"].get("max_age_days") or 21) * 86400
    for j in jdb.all_jobs(since, search["id"]):
        out[settle(search["id"], j, search["spec"])] += 1
    return {"kept": out["in"] + out["out"] + out["queued"], "shown": out["in"], "queued": out["queued"]}


def needs_poll(old: dict, new: dict) -> bool:
    """Does the edited search ask the portals anything the old one did not?

    Filters that only narrow (exclude words, level, salary, language, a smaller
    radius or age) are answered from what is stored.
    """
    all_src = set(SOURCES) or set(SOURCE_NAMES)
    if (set(new.get("sources") or all_src)) - set(old.get("sources") or all_src):
        return True
    if set(student_keywords(new)) - set(student_keywords(old)):
        return True
    old_legs = {q["remote"]: q for q in legs(old)}
    for q in legs(new):
        o = old_legs.get(q["remote"])
        if o is None or q["max_age_days"] > o["max_age_days"] or (o["no_agency"] and not q["no_agency"]):
            return True
        if not q["remote"] and ((q["lat"], q["lon"]) != (o["lat"], o["lon"]) or q["radius_km"] > o["radius_km"]):
            return True
    return False


async def poll_search(client: httpx.AsyncClient, search: dict, searches: list[dict]) -> None:
    spec = search["spec"]
    allowed = set(spec.get("sources") or SOURCES)
    errors, seen, new = [], 0, 0
    for q in legs(spec):
        for name, mod in SOURCES.items():
            if name not in allowed or resting(name):
                continue
            try:
                found = await mod.search(client, q, _limiter(mod))
            except Exception as exc:
                _blocked(name, exc)
                log.warning("[%s] %s %s: %s", search["name"], name, "remote" if q["remote"] else "local", exc)
                errors.append(f"{getattr(mod, 'LABEL', name)}: {str(exc)[:80]}")
                continue
            known = jdb.known_ids(name)
            for j in found:
                seen += 1
                is_new = j["source_id"] not in known
                jdb.upsert(j)
                if is_new:
                    new += 1
                    match_all(jdb.get(j["source"], j["source_id"]), searches)
                else:
                    settle(search["id"], jdb.get(j["source"], j["source_id"]), spec)
    jdb.search_state(search["id"], "; ".join(errors) or None)
    log.info("[%s] %d postings, %d new%s", search["name"], seen, new, f", {len(errors)} errors" if errors else "")


async def read_posting(client: httpx.AsyncClient, job: dict, budget: dict) -> dict:
    """Full posting + where to apply, once per job (where to apply again if the search was down)."""
    retry = (job.get("apply") or {}).get("retry")
    if job.get("detail_at") and not (retry and budget["find"] > 0):
        return job
    mod = SOURCES.get(job["source"])
    if not job.get("detail_at") and mod and hasattr(mod, "detail") and not resting(job["source"]):
        try:
            more = await mod.detail(client, job, _limiter(mod))
            if more:
                job = {**job, **{k: v for k, v in more.items() if v not in (None, "", [], {})}}
        except Exception as exc:
            _blocked(job["source"], exc)
            log.warning("detail %s/%s: %s", job["source"], job["source_id"], exc)
    try:
        apply = importlib.import_module(".apply", __package__)
        search = budget["find"] > 0
        job["apply"] = await apply.resolve(client, job, search=search)
        if search:
            budget["find"] -= 1
        elif (job["apply"] or {}).get("channel") == "portal":
            job["apply"]["retry"] = True      # no budget left this tick: look again later
    except Exception as exc:
        log.warning("apply %s/%s: %s", job["source"], job["source_id"], exc)
        job["apply"] = {"channel": "portal", "label": getattr(mod, "LABEL", job["source"]),
                        "url": job["url"], "email": job.get("apply_email"), "via": "posting"}
    job["detail_at"] = time.time()
    jdb.upsert(job)
    return job


async def judge(client: httpx.AsyncClient, limit: int = JUDGE_PER_TICK) -> int:
    profile = json.loads(jdb.setting("cv_profile", "{}") or "{}")
    h = jdb.setting("cv_hash")
    specs = {s["id"]: s["spec"] for s in jdb.searches()}
    done = 0
    budget = {"find": FIND_PER_TICK}
    for sid, job in jdb.unjudged(limit):
        spec = specs.get(sid)
        if spec is None:
            continue
        job = await read_posting(client, job, budget)
        reading = await ai.read(client, job, profile, spec.get("wish") or "")
        if reading is None:
            break                               # Ollama away: retry next tick, don't burn the queue
        job = {**job, "reading": reading, "read_at": time.time()}
        jdb.upsert(job)
        jdb.set_fit(sid, job["source"], job["source_id"], fit=reading["fit"], fazit=reading.get("fazit"),
                    pros=reading.get("passt"), gaps=reading.get("fehlt"), cv_hash=h)
        settle(sid, job, spec)
        done += 1
    if done:
        log.info("read %d postings", done)
    return done


async def run(client: httpx.AsyncClient | None = None) -> None:
    jdb.init()
    discord.init()
    SOURCES.update(load_sources())
    log.info("job hunter up: %s", ", ".join(SOURCES))
    # own client: portals want browser headers, not the KA client's cookies
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25.0) as c:
        while True:
            started = time.monotonic()
            try:
                searches = jdb.searches(enabled_only=True)
                now = time.time()
                for s in searches:
                    if now - (s["last_polled_at"] or 0) < POLL_EVERY:
                        continue
                    try:
                        await poll_search(c, s, searches)
                    except Exception as exc:
                        log.exception("[%s] job poll failed", s["name"])
                        jdb.search_state(s["id"], str(exc)[:200])
                    await judge(c)
                await judge(c)
            except Exception:
                log.exception("job cycle failed")
            try:
                await jnotify.flush(c)
            except Exception:
                log.exception("job discord push failed")
            await asyncio.sleep(max(10.0, TICK - (time.monotonic() - started)))
