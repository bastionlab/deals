"""Welcome to the Jungle (welcometothejungle.com): French startup / tech board, searched via its Algolia index.

Verified 2026-10-22 from the deals container (home IP, no cookies):
- The search page is a Next.js app; its runtime config is GET /api/env ("window.env = {...}"):
  PUBLIC_ALGOLIA_APPLICATION_ID=CSEKHVMS53, PUBLIC_ALGOLIA_API_KEY_CLIENT=4bd8f6215d0cc52b26430765769e65a0
  (search-only key, restricted by Referer/Origin -> both set to the site). The jobs index is not in
  that config; it is wttj_jobs_production_<lang>, sorted replica ..._published_at_desc (newest first).
  The key is re-read from /api/env once if Algolia ever answers 403 (rotated key).
- POST https://<app>-dsn.algolia.net/1/indexes/<index>/query {query, page, hitsPerPage, filters,
  numericFilters, aroundLatLng, aroundRadius, restrictSearchableAttributes}.
  * Plain queries also hit summary/profile text ("developpeur" -> "Directeur de résidence ... tu
    développeras des partenariats"), so the query is restricted to the title (name). Only ["name"] is
    accepted there; other attributes 400 ("same priority").
  * aroundLatLng+aroundRadius (metres) filter on hit._geoloc (one point per office); a job with an
    office anywhere within the radius matches. offices.country_code:FR keeps the remote leg French.
  * remote facet: fulltime | partial | punctual | no | unknown. fulltime = remote only (rare,
    ~1 % of hits), partial = hybrid, punctual = "télétravail ponctuel" (counted as hybrid, like
    StepStone's "Homeoffice möglich"), no = onsite.
  * published_at_timestamp (epoch) for the age cut; nbPages tells the depth.
- A hit carries title, organization {name, slug}, offices [{city, district, state, country_code}],
  _geoloc, contract_type, salary_minimum/maximum + salary_period, language (fr|en|de), summary,
  profile, key_missions — but not the full ad text.
- Posting page /fr/companies/<org slug>/jobs/<job slug> holds an ld+json JobPosting (HTML
  description, jobLocation with postalCode, datePosted). Applying happens on WTTJ's own form (or
  behind its button for external ATS), no employer URL in the page -> apply_url stays None.
"""
from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from datetime import datetime

from .common import Limiter, clean, job, text, yearly

SOURCE = "wttj"
LABEL = "Welcome to the Jungle"
INTERVAL = 6.0
COUNTRY = "fr"              # the poller only asks this portal for searches located in France

BASE = "https://www.welcometothejungle.com"
APP_ID = "CSEKHVMS53"
API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
INDEX = "wttj_jobs_production_fr_published_at_desc"
PER_PAGE = 30
GEO_API = "https://geo.api.gouv.fr/communes"

_MODE = {"fulltime": "remote", "partial": "hybrid", "punctual": "hybrid", "no": "onsite"}
_EMPLOYMENT = {"full_time": ["vollzeit"], "part_time": ["teilzeit"], "internship": ["praktikum"],
               "apprenticeship": ["ausbildung"], "freelance": ["freelance"], "temporary": ["befristet"],
               "vie": ["befristet"], "graduate_program": ["vollzeit"]}
_PERIOD = {"yearly": "year", "monthly": "month", "weekly": "week", "daily": "day", "hourly": "hour"}
_PERIOD_FR = {"year": "an", "month": "mois", "week": "semaine", "day": "jour", "hour": "heure"}
_AGENCY = re.compile(r"(?i)\b(cabinet de recrutement|pour (le compte d'|l'un de )?(un|nos|notre) client|"
                     r"société de portage|intérim|agence de recrutement)\b")

_key = {"app": APP_ID, "key": API_KEY}
_communes: dict = {}


class Blocked(RuntimeError):
    pass


async def _commune(client, q: dict) -> dict | None:
    """The French commune at the search's centre (geo.api.gouv.fr, free, no key); None = not in France."""
    lat, lon, place = q.get("lat"), q.get("lon"), (q.get("place") or "").strip()
    ck = (round(lat, 4), round(lon, 4)) if lat is not None and lon is not None else place.lower()
    if not ck:
        return None
    if ck in _communes:
        return _communes[ck]
    fields = "nom,code,codeDepartement,codesPostaux,centre"
    if lat is not None and lon is not None:
        r = await client.get(GEO_API, params={"lat": lat, "lon": lon, "fields": fields})
    else:                                        # name only: exact name, biggest commune
        r = await client.get(GEO_API, params={"nom": place, "boost": "population", "limit": 5, "fields": fields})
    r.raise_for_status()
    rows = r.json()
    if lat is None or lon is None:
        rows = [c for c in rows if _fold(c["nom"]) == _fold(place)]
    _communes[ck] = c = rows[0] if rows else None
    return c


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in s if not unicodedata.combining(ch))).strip()


async def _refresh_key(client) -> None:
    r = await client.get(f"{BASE}/api/env")
    r.raise_for_status()
    m = re.search(r"window\.env\s*=\s*(\{.*\})", r.text, re.S)
    env = json.loads(m.group(1)) if m else {}
    if env.get("PUBLIC_ALGOLIA_API_KEY_CLIENT"):
        _key.update(app=env.get("PUBLIC_ALGOLIA_APPLICATION_ID") or _key["app"],
                    key=env["PUBLIC_ALGOLIA_API_KEY_CLIENT"])


async def _query(client, body: dict, limiter: Limiter) -> dict:
    for attempt in (1, 2):
        await limiter.wait()
        r = await client.post(
            f"https://{_key['app'].lower()}-dsn.algolia.net/1/indexes/{INDEX}/query",
            content=json.dumps(body),
            headers={"x-algolia-application-id": _key["app"], "x-algolia-api-key": _key["key"],
                     "Content-Type": "application/json", "Accept": "application/json",
                     "Referer": BASE + "/", "Origin": BASE})
        if r.status_code == 403 and attempt == 1:
            await limiter.wait()
            await _refresh_key(client)           # key rotated: read the current one off the site
            continue
        if r.status_code == 429:
            raise Blocked(f"WTTJ Algolia: {r.status_code}")
        r.raise_for_status()
        return r.json()
    return {}


def _km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


def _hit(h: dict, q: dict) -> dict:
    org = h.get("organization") or {}
    offices = h.get("offices") or []
    geos = h.get("_geoloc") or []
    # the office nearest the search centre is "the" location; the rest go to attrs
    # (_geoloc and offices are not always the same length / order: then only the French office counts)
    i = 0
    if q.get("lat") is not None and geos and len(geos) == len(offices):
        i = min(range(len(geos)), key=lambda k: _km(q["lat"], q["lon"], geos[k]["lat"], geos[k]["lng"]))
    elif offices:
        i = next((k for k, o in enumerate(offices) if o.get("country_code") == "FR"), 0)
    off = offices[i] if offices else {}
    geo = geos[i] if len(geos) == len(offices) and i < len(geos) else {}
    cities = list(dict.fromkeys(o.get("city") for o in offices if o.get("city")))
    loc = ", ".join(([off["city"]] if off.get("city") else []) + [c for c in cities if c != off.get("city")][:3])

    unit = _PERIOD.get(h.get("salary_period") or "")
    smin = smax = stext = None
    if unit and h.get("salary_currency") in (None, "EUR") and (h.get("salary_minimum") or h.get("salary_maximum")):
        lo, hi = h.get("salary_minimum"), h.get("salary_maximum")
        if unit == "year" and max(lo or 0, hi or 0) < 1000:      # some employers type k€ ("45")
            lo, hi = lo and lo * 1000, hi and hi * 1000
        smin, smax = yearly(lo, unit), yearly(hi, unit)
        span = "–".join(dict.fromkeys(f"{int(v):,}".replace(",", " ") for v in (lo, hi) if v))
        stext = f"{span} € / {_PERIOD_FR[unit]}"

    desc = "\n\n".join(x for x in [
        clean(h.get("summary")),
        "\n".join(f"• {clean(m)}" for m in h.get("key_missions") or []),
    ] if x)
    attrs = {k: v for k, v in {
        "remote": h.get("remote"),
        "contract": h.get("contract_type"),
        "experience": f"{h['experience_level_minimum']:g} ans+" if h.get("experience_level_minimum") else None,
        "education": h.get("education_level"),
        "sector": ", ".join(s.get("name") or "" for s in h.get("sectors") or [])[:120],
        "profession": ((h.get("new_profession") or {}).get("pivot_name")),
        "offices": ", ".join(cities)[:200] if len(cities) > 1 else None,
        "org_size": str(org["nb_employees"]) if org.get("nb_employees") else None,
    }.items() if v}
    return job(
        SOURCE, h.get("reference") or h["objectID"],
        f"{BASE}/fr/companies/{org.get('slug')}/jobs/{h.get('slug')}",
        h.get("name") or "",
        company=clean(org.get("name")) or None,
        location=loc or None,
        lat=geo.get("lat"), lon=geo.get("lng"),
        work_mode=_MODE.get(h.get("remote") or ""),
        employment=list(_EMPLOYMENT.get(h.get("contract_type") or "", [])),
        posted_at=h.get("published_at_timestamp") or _epoch(h.get("published_at")),
        salary_min=smin, salary_max=smax, salary_text=stext,
        description=desc or None,
        lang=h.get("language") if h.get("language") in ("fr", "en", "de") else None,
        attrs=attrs,
    )


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    base = {
        "hitsPerPage": PER_PAGE,
        "restrictSearchableAttributes": ["name"],
        "numericFilters": [f"published_at_timestamp>={int(time.time() - (q.get('max_age_days') or 30) * 86400)}"],
    }
    if q.get("remote"):
        base["filters"] = "remote:fulltime AND offices.country_code:FR"
    else:
        c = await _commune(client, q)
        if not c:
            return []                            # no place, or not in France
        lat, lon = q.get("lat"), q.get("lon")
        if lat is None or lon is None:
            lon, lat = c["centre"]["coordinates"]
            q = {**q, "lat": lat, "lon": lon}
        base["aroundLatLng"] = f"{lat},{lon}"
        base["aroundRadius"] = int(max(q.get("radius_km") or 0, 10) * 1000)   # 0 = the city itself
    out: dict[str, dict] = {}
    for kw in q.get("keywords") or [""]:
        for page in range(max(1, q.get("max_pages") or 1)):
            d = await _query(client, {**base, "query": kw, "page": page}, limiter)
            hits = d.get("hits") or []
            for h in hits:
                j = _hit(h, q)
                out.setdefault(j["source_id"], j)
            if not hits or page + 1 >= (d.get("nbPages") or 0):
                break
    return list(out.values())


def _ld(page: str) -> dict:
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for x in d if isinstance(d, list) else [d]:
            if isinstance(x, dict) and x.get("@type") == "JobPosting":
                return x
    return {}


async def detail(client, j: dict, limiter: Limiter) -> dict | None:
    await limiter.wait()
    r = await client.get(j["url"], headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
    if r.status_code in (404, 410):
        return None
    if r.status_code in (403, 429):
        raise Blocked(f"WTTJ posting: {r.status_code}")
    r.raise_for_status()
    ld = _ld(r.text)
    if not ld:
        return None
    desc = text(ld.get("description"))
    extra = text(ld.get("qualifications") if isinstance(ld.get("qualifications"), str) else None)
    if extra and extra[:80] not in desc:
        desc = f"{desc}\n\nProfil recherché\n{extra}"
    out = {"description": desc or None, "posted_at": j.get("posted_at") or _epoch(ld.get("datePosted"))}
    locs = ld.get("jobLocation") or []
    addr = ((locs[0] if isinstance(locs, list) and locs else locs) or {}).get("address") or {}
    if addr.get("postalCode"):
        out["postal_code"] = addr["postalCode"]
    if _AGENCY.search(desc[:1500]):
        out["agency"] = True
    return out


if __name__ == "__main__":
    import asyncio
    import sys

    import httpx

    from .common import HEADERS, TIMEOUT

    async def main():
        lim = Limiter(INTERVAL)
        kw = sys.argv[1] if len(sys.argv) > 1 else "développeur"
        remote = len(sys.argv) > 2 and sys.argv[2] == "remote"
        q = {"keywords": [kw], "place": "Paris", "lat": 48.8566, "lon": 2.3522, "radius_km": 20,
             "remote": remote, "max_age_days": 7, "max_pages": 1, "no_agency": True}
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            found = await search(c, q, lim)
            print(f"{len(found)} jobs (remote={remote})")
            for j in found:
                day = time.strftime("%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "?"
                print(f"  {j['title'][:55]} | {j['company']} | {(j['location'] or '')[:30]} | {j['work_mode']} | "
                      f"{day} | {j['employment']} | {j['salary_text']} | {j['lang']}")
            for j in found[:2]:
                more = await detail(c, j, lim) or {}
                print(f"* {j['url']}\n  {more.get('postal_code')} agency={more.get('agency')} "
                      f"{len(more.get('description') or '')} chars: {(more.get('description') or '')[:200]!r}")

    asyncio.run(main())
