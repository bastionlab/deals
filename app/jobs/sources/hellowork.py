"""HelloWork (hellowork.com, ex-RegionsJob): big French general job board, server-rendered HTML.

Verified 2026-10-22 from CT 107 (home IP, desktop Chrome UA, no cookies) — every request 200, no
captcha / bot wall seen at >= 3 s gaps.

Search  GET /fr-fr/emploi/recherche.html?k=<what>&l=<where>&ray=<km>&st=date&d=w&t=Complet&p=2
- k = free text; l = place: "Lyon", "Lyon 69000", "69001" all work, but a bare name can be
  ambiguous ("Saint-Denis" -> La Réunion) and some name+code pairs give an empty page ("Paris 75001"),
  an INSEE code (75056, 69123) gives nothing -> we send the commune's first postal code ("75001").
  No l = all of France.
- ray only takes 0 | 5 | 10 | 20 | 50 (the slider's steps) -> the next step up, capped at 50.
- st=date = newest first (default st=relevance).
- d = h (24 h) | d (3 days) | w (1 week) | m (1 month) | all.
- t = Complet (full remote) | Partiel | Occasionnel | Pas_teletravail.
- c = CDI | CDD | Travail_temp | Stage | Alternance | Independant (not used: the poller filters).
- p=N, 30 cards per page; the header says "1 927 offres".
- Cards (<li data-id-storage-item-id="<id>">) hold title, company, "Paris - 75", contract
  ("CDI", "Alternance"), salary as shown ("900 - 1 300 € / mois"), extra tags ("2 ans"),
  a relative date ("il y a 3 jours") and an aria-label summing it up ("... en temps plein ...").
  Remote work shows only when stated, as a tag / in the aria-label ("Télétravail partiel").
Posting GET /fr-fr/emplois/<id>.html
- ld+json JobPosting (escaped as application/ld&#x2B;json): description (HTML), qualifications,
  datePosted (exact), employmentType, baseSalary, jobLocation {addressLocality, postalCode},
  jobLocationType "TELECOMMUTE" for remote, directApply. The tag list under the title carries
  "Télétravail complet / partiel / occasionnel".
- Applying is HelloWork's own form (directApply) or a link hidden behind ROT13-obfuscated
  data-atc attributes -> apply_url stays None, the posting page is the apply link.
"""
from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import datetime
from urllib.parse import urlencode

from .common import Limiter, clean, job, text, yearly

SOURCE = "hellowork"
LABEL = "HelloWork"
INTERVAL = 6.0
COUNTRY = "fr"              # the poller only asks this portal for searches located in France

BASE = "https://www.hellowork.com"
GEO_API = "https://geo.api.gouv.fr/communes"
RAYS = (0, 5, 10, 20, 50)
PER_PAGE = 30

_CONTRACT = {"cdi": [], "cdd": ["befristet"], "intérim": ["befristet"], "interim": ["befristet"],
             "stage": ["praktikum"], "alternance": ["ausbildung"], "indépendant": ["freelance"],
             "freelance": ["freelance"], "job étudiant": ["werkstudent"]}
_LD_TYPES = {"FULL_TIME": "vollzeit", "PART_TIME": "teilzeit", "INTERN": "praktikum",
             "TEMPORARY": "befristet", "CONTRACTOR": "freelance"}
_TELE = {"complet": "remote", "partiel": "hybrid", "occasionnel": "hybrid"}
_AGENCY_NAME = re.compile(r"(?i)recrutement|intérim|interim|\bcabinet\b|staffing|adecco|manpower|randstad|"
                          r"synergie|proman|\bcrit\b|start people|samsic emploi|partnaire|hays|expectra")
_AGENCY_TEXT = re.compile(r"(?i)\b(pour (le compte d['’]|l['’]un de )?(un|une|nos|notre) client|"
                          r"cabinet de recrutement|agence d['’]emploi|mission d['’]intérim|société de portage)")

_communes: dict = {}


class Blocked(RuntimeError):
    pass


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in s if not unicodedata.combining(ch))).strip()


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


def _age(days: int | None) -> str:
    if not days:
        return "all"
    for limit, code in ((1, "h"), (3, "d"), (7, "w"), (31, "m")):
        if days <= limit:
            return code
    return "all"


def search_url(kw: str, q: dict, where: str | None, page: int) -> str:
    params = {"k": kw}
    if where:
        params["l"] = where
        r = q.get("radius_km") or 0
        params["ray"] = next((x for x in RAYS if x >= r), RAYS[-1])
    if q.get("remote"):
        params["t"] = "Complet"
    params["st"] = "date"
    params["d"] = _age(q.get("max_age_days"))
    if page > 1:
        params["p"] = page
    return f"{BASE}/fr-fr/emploi/recherche.html?{urlencode(params)}"


def _check(r, what: str) -> str:
    if r.status_code in (403, 429) or "captcha" in r.text[:5000].lower():
        raise Blocked(f"HelloWork {what}: blocked ({r.status_code})")
    r.raise_for_status()
    return r.text


_REL = re.compile(r"il y a (\d+) (minute|heure|jour|semaine|mois)", re.I)
_REL_S = {"minute": 60, "heure": 3600, "jour": 86400, "semaine": 7 * 86400, "mois": 30 * 86400}


def _ago(s: str | None) -> float | None:
    if not s:
        return None
    s = s.lower()
    if "aujourd" in s:
        return time.time()
    if "hier" in s:
        return time.time() - 86400
    m = _REL.search(s)
    return time.time() - int(m.group(1)) * _REL_S[m.group(2)] if m else None


def _salary(s: str | None) -> tuple[float | None, float | None]:
    """"900 - 1 300 € / mois" -> yearly (min, max)."""
    if not s or "€" not in s:
        return None, None
    nums = [float(n.replace(" ", "").replace(" ", "").replace("\xa0", "").replace(",", "."))
            for n in re.findall(r"\d[\d  \xa0]*(?:[.,]\d+)?", s.split("€")[0])]
    t = s.lower()
    unit = "month" if "mois" in t else "hour" if "heure" in t else "day" if "jour" in t else "year"
    if not nums:
        return None, None
    return yearly(nums[0], unit), yearly(nums[-1], unit)


def _employment(contract: str | None, label: str | None) -> list[str]:
    out = list(_CONTRACT.get((contract or "").strip().lower(), []))
    t = (label or "").lower()
    if "temps plein" in t:
        out.append("vollzeit")
    if "temps partiel" in t:
        out.append("teilzeit")
    return list(dict.fromkeys(out))


def _mode(s: str) -> str | None:
    m = re.search(r"(?i)t[ée]l[ée]travail (complet|partiel|occasionnel)", s)
    return _TELE[m.group(1).lower()] if m else None


def _card(oid: str, chunk: str, q: dict) -> dict | None:
    a = re.search(r'<a\b[^>]*data-cy="offerTitle"[^>]*>(.*?)</a>', chunk, re.S)
    if not a:
        return None
    tag = a.group(0)
    spans = re.findall(r"<span[^>]*>(.*?)</span>", a.group(1), re.S)
    title = clean(text(spans[0])) if spans else ""
    company = clean(text(spans[1])) if len(spans) > 1 else None
    label = html.unescape(m.group(1)) if (m := re.search(r'aria-label="([^"]*)"', tag)) else ""

    def cy(name):
        m = re.search(rf'data-cy="{name}"[^>]*>(.*?)</div>', chunk, re.S)
        return clean(text(m.group(1))) if m else None

    loc, contract = cy("localisationCard"), cy("contractCard")
    sal = next((clean(text(x)) for x in re.findall(r'<div class="[^"]*typo-s-bold[^"]*">(.*?)</div>', chunk, re.S)
                if "€" in x or "&#x20AC;" in x), None)
    tags = [clean(text(x)) for x in re.findall(r'data-cy="contractTag"[^>]*>(.*?)</div>', chunk, re.S)]
    when = re.search(r'<div class="typo-s text-grey-500[^"]*">(.*?)</div>', chunk, re.S)
    resp = re.search(r"product_response_type&quot;:&quot;(\w+)", chunk)
    smin, smax = _salary(sal)
    attrs = {k: v for k, v in {
        "contract": contract,
        "tags": ", ".join(tags)[:120],
        "apply": resp.group(1) if resp else None,
    }.items() if v}
    emp = _employment(contract, label)
    return job(
        SOURCE, oid, f"{BASE}/fr-fr/emplois/{oid}.html", title,
        company=company or None,
        location=loc,
        work_mode="remote" if q.get("remote") else _mode(" ".join(tags) + " " + label),
        employment=emp,
        posted_at=_ago(clean(text(when.group(1))) if when else None),
        salary_min=smin, salary_max=smax, salary_text=sal,
        agency=True if (contract or "").lower().startswith("int") or
        (company and _AGENCY_NAME.search(company)) else None,
        attrs=attrs,
    )


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    where = None
    if not q.get("remote"):
        c = await _commune(client, q)
        if not c:
            return []                            # no place, or not in France
        cps = c.get("codesPostaux") or []
        where = cps[0] if cps else c["nom"]     # a postal code is unambiguous ("Paris 75001" -> nothing)
    out: dict[str, dict] = {}
    for kw in q.get("keywords") or []:
        for page in range(1, max(1, q.get("max_pages") or 1) + 1):
            await limiter.wait()
            r = await client.get(search_url(kw, q, where, page),
                                 headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
            page_html = _check(r, "search")
            parts = re.split(r'<li data-id-storage-target="item"[^>]*?data-id-storage-item-id="(\d+)"', page_html)
            n = 0
            for oid, chunk in zip(parts[1::2], parts[2::2]):
                n += 1
                if oid not in out and (j := _card(oid, chunk, q)):
                    out[oid] = j
            if n < PER_PAGE:
                break
    return list(out.values())


def _ld(page: str) -> dict:
    for m in re.finditer(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for x in d if isinstance(d, list) else [d]:
            if isinstance(x, dict) and x.get("@type") == "JobPosting":
                return x
    return {}


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


async def detail(client, j: dict, limiter: Limiter) -> dict | None:
    await limiter.wait()
    r = await client.get(j["url"], headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
    if r.status_code in (404, 410):
        return None
    page = _check(r, "posting")
    ld = _ld(page)
    if not ld:
        return None
    desc = text(ld.get("description"))
    if isinstance(ld.get("qualifications"), str) and (extra := text(ld["qualifications"])):
        desc = f"{desc}\n\nProfil recherché\n{extra}"
    out = {"description": desc or None, "posted_at": _epoch(ld.get("datePosted")) or j.get("posted_at")}
    addr = ((ld.get("jobLocation") or {}) if isinstance(ld.get("jobLocation"), dict) else
            ((ld.get("jobLocation") or [{}])[0])).get("address") or {}
    if addr.get("postalCode") and not addr["postalCode"].endswith("000"):
        out["postal_code"] = addr["postalCode"]   # "75000" is just the city, not a real code
    types = ld.get("employmentType") or []
    types = [types] if isinstance(types, str) else types
    out["employment"] = list(dict.fromkeys((j.get("employment") or []) +
                                           [_LD_TYPES[t] for t in types if t in _LD_TYPES]))
    # "Télétravail complet / partiel / occasionnel" in the tag row, TELECOMMUTE in ld+json
    head = page[page.find('data-cy="jobTitle"'):][:8000]
    tele = re.search(r"T(?:é|&#xE9;)l(?:é|&#xE9;)travail (complet|partiel|occasionnel)", head)
    if tele:
        out["work_mode"] = _TELE[tele.group(1)]
    elif ld.get("jobLocationType") == "TELECOMMUTE":
        out["work_mode"] = "remote"
    if _AGENCY_TEXT.search(desc[:2000]):
        out["agency"] = True
    sal = ((ld.get("baseSalary") or {}).get("value") or {})
    if not j.get("salary_min") and sal.get("minValue"):
        unit = {"MONTH": "month", "HOUR": "hour", "DAY": "day", "WEEK": "week"}.get(sal.get("unitText"), "year")
        out["salary_min"], out["salary_max"] = yearly(sal["minValue"], unit), yearly(sal.get("maxValue"), unit)
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
                day = time.strftime("%m-%d %H:%M", time.localtime(j["posted_at"])) if j["posted_at"] else "?"
                print(f"  {j['title'][:50]} | {j['company']} | {j['location']} | {j['work_mode']} | {day} | "
                      f"{j['employment']} | {j['salary_text']} {j['salary_min']} | ag={j['agency']} | {j['attrs']}")
            for j in found[:2]:
                more = await detail(c, j, lim) or {}
                print(f"* {j['url']} {time.strftime('%Y-%m-%d', time.localtime(more.get('posted_at') or 0))} "
                      f"{more.get('postal_code')} {more.get('work_mode')} {more.get('employment')} "
                      f"agency={more.get('agency')}\n  {len(more.get('description') or '')} chars: "
                      f"{(more.get('description') or '')[:200]!r}")

    asyncio.run(main())
