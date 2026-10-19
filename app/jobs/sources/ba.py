"""Bundesagentur für Arbeit Jobsuche — the biggest German job database (~1 M postings).

Verified 2026-10-19 from the home IP (CT 107), no login, static key header X-API-Key: jobboerse-jobsuche:
  search  GET rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs   (v4 search is 403 now)
          was=<keywords, words ANDed>  wo=<place>  umkreis=<km, any int 0..200, default 25; 0 = the place only>
          page=1..100 (101 -> 400)  size=<any, 100 is fine>  sort=veroeffdatum (newest first; default = relevance)
          veroeffentlichtseit=0|1|7|14|28 only — any other number is silently ignored (= all ages)
          angebotsart=1;4;34 (Arbeit; Ausbildung/duales Studium; Praktikum/Trainee) — 2 = Selbständigkeit, left out
          arbeitszeit=vz|tz|mj|snw (;-joined). `ho` is gone in v6 (returns 0 hits); home office is now
          homeoffice=prozentual_<N> (ads stating >= N % home office) and ";nv_true" adds "nach Vereinbarung"
          pav=false / zeitarbeit=false drop Personalvermittlung / Arbeitnehmerüberlassung; befristung=1 (befristet)|2|3
          empty wo= is a 400, so it is omitted when there is no place.
          Response: ergebnisliste[], maxErgebnisse. Items carry title, firma, stellenlokationen (plz/ort, breite/laenge),
          veroeffentlichungszeitraum.von (current publication; what veroeffentlichtseit filters on),
          datumErsteVeroeffentlichung, homeoffice*, salary fields, sometimes externeURL. No agency flag here.
  detail  GET .../pc/v4/jobdetails/<base64(refnr)>  (plain refnr -> 404, the v6 path -> 403)
          + stellenangebotsBeschreibung (plain text, sometimes markdown; emojis arrive as '?'),
          istPrivateArbeitsvermittlung / istArbeitnehmerUeberlassung (agency), allianzpartnerName/-Url.
          404 STELLENANGEBOT_NICHT_GEFUNDEN = taken down.
  contact GET .../pc/v3/jobs/<refnr>/bewerbung is captcha-gated (403 AAS_WRONG_PARAMETERS) — not used; the
          e-mail / apply link is fished out of the description instead.
Ads without externeURL are applied to through BA's own form/contact (captcha-gated), so they only get an
apply_url/apply_email when the description names one.
Quirks: salary fields lie — JAHRESGEHALT often holds a monthly figure (2200) or junk (2.3, span 1..20472), so
figures are sanity-checked before they become salary_min/max. externeURL mostly points at a partner board
(deutschland-stellenmarkt.de, ...) rather than the employer, but it is still where the ad wants applications.
"""
from __future__ import annotations

import base64
import datetime as dt
import math
import re
from zoneinfo import ZoneInfo

import httpx

from .common import HEADERS, TIMEOUT, Limiter, clean, guess_mode, job, text, yearly

SOURCE = "ba"
LABEL = "Arbeitsagentur"
INTERVAL = 3.0

API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
API_HEADERS = {**HEADERS, "Accept": "application/json", "X-API-Key": "jobboerse-jobsuche"}
PAGE_SIZE = 100
MAX_PAGE = 100
AGE_BUCKETS = (1, 7, 14, 28)
BERLIN = ZoneInfo("Europe/Berlin")

_ART = {"AUSBILDUNG": "ausbildung", "PRAKTIKUM_TRAINEE": "praktikum", "SELBSTAENDIGKEIT": "freelance"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
_URL = re.compile(r"(?:https?://|\bwww\.)[^\s<>\"')\]*]+", re.I)
# only links that look like an application / career page; a bare company homepage is not an apply_url
_APPLYISH = re.compile(r"(?i)karriere|career|jobs?\b|/jobs?/|bewerb|apply|stellen|recruit|personio|softgarden|"
                       r"greenhouse|lever\.co|workday|smartrecruiters|join\.com|onlyfy|rexx|d\.vinci|successfactors")
_WERKSTUDENT = re.compile(r"(?i)werkstudent")


def ba_url(refnr: str) -> str:
    return f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"


def _epoch(day: str | None) -> int | None:
    try:
        d = dt.date.fromisoformat((day or "")[:10])
    except ValueError:
        return None
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=BERLIN).timestamp())


def _age_bucket(days: int | None) -> int | None:
    # BA ignores any value outside its buckets, so round up to the next one it understands
    if not days or days <= 0:
        return None
    return next((b for b in AGE_BUCKETS if days <= b), None)


def _salary(e: dict) -> tuple[float | None, float | None, str | None]:
    kind = e.get("verguetungsangabe")
    if kind not in ("JAHRESGEHALT", "MONATSGEHALT", "STUNDENLOHN"):
        return None, None, None
    if e.get("artDerVerguetung") == "FESTGEHALT":
        lo = hi = e.get("festgehalt")
    else:
        lo, hi = e.get("gehaltsspanneVon"), e.get("gehaltsspanneBis")

    def norm(v):
        if not v:
            return None
        v = float(v)
        if kind == "STUNDENLOHN":
            return yearly(v, "hour") if 8 <= v <= 300 else None
        # "JAHRESGEHALT" is the form's default: employers type monthly pay into it all the time
        if 1000 <= v < 15000:
            return yearly(v, "month")
        return yearly(v, "year") if 15000 <= v <= 500000 else None

    lo, hi = norm(lo), norm(hi)
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    unit = {"JAHRESGEHALT": "Jahr", "MONATSGEHALT": "Monat", "STUNDENLOHN": "Stunde"}[kind]
    raw = [x for x in (e.get("festgehalt"), e.get("gehaltsspanneVon"), e.get("gehaltsspanneBis")) if x]
    de = lambda x: f"{x:,.2f}".rstrip("0").rstrip(".").translate(str.maketrans(",.", ".,"))
    txt = (" – ".join(de(x) for x in raw) + f" € / {unit}") if raw else None
    return lo, hi, txt


def _mode(e: dict, title: str) -> str | None:
    if e.get("homeofficemoeglich"):
        if e.get("homeofficetyp") == "ANGABE_IN_PROZENT" and (e.get("homeofficeprozent") or 0) >= 100:
            return "remote"
        return "hybrid"     # "nach Vereinbarung" or a share below 100 %
    hint = guess_mode(title)
    if hint:
        return hint
    # explicit "no home office" from the employer; missing field stays unknown
    return "onsite" if e.get("homeofficemoeglich") is False else None


def _employment(e: dict, title: str) -> list[str]:
    out = []
    if e.get("arbeitszeitVollzeit"):
        out.append("vollzeit")
    if any(e.get(k) for k in ("arbeitszeitTeilzeitVormittag", "arbeitszeitTeilzeitNachmittag",
                              "arbeitszeitTeilzeitAbend", "arbeitszeitTeilzeitFlexibel")):
        out.append("teilzeit")
    if _WERKSTUDENT.search(title):
        out.append("werkstudent")
    art = _ART.get(e.get("stellenangebotsart"))
    if art:
        out.append(art)
    if e.get("istGeringfuegigeBeschaeftigung"):
        out.append("minijob")
    if e.get("vertragsdauer") == "BEFRISTET":
        out.append("befristet")
    return out


def _pick_place(locs: list[dict], lat, lon) -> dict:
    # multi-site ads list every site; take the one nearest the search centre
    with_geo = [l for l in locs if l.get("breite") is not None]
    if not with_geo:
        return locs[0] if locs else {}
    if lat is None or lon is None:
        return with_geo[0]
    return min(with_geo, key=lambda l: math.hypot(l["breite"] - lat, (l["laenge"] - lon) * math.cos(math.radians(lat))))


def _attrs(e: dict) -> dict:
    a = {}
    if e.get("hauptberuf"):
        a["beruf"] = e["hauptberuf"]
    if e.get("stellenangebotsart") and e["stellenangebotsart"] != "ARBEIT":
        a["art"] = e["stellenangebotsart"].lower()
    first = e.get("datumErsteVeroeffentlichung")
    if first and first != (e.get("veroeffentlichungszeitraum") or {}).get("von"):
        a["erstmals"] = first      # re-published ad: posted_at is the refresh, this is the original
    if (e.get("eintrittszeitraum") or {}).get("von"):
        a["eintritt"] = e["eintrittszeitraum"]["von"]
    if e.get("befristungInMonaten"):
        a["befristung"] = f"{e['befristungInMonaten']} Monate"
    if e.get("homeofficeprozent"):
        a["homeoffice"] = f"{e['homeofficeprozent']} %"
    elif e.get("homeofficetyp") == "NACH_VEREINBARUNG":
        a["homeoffice"] = "nach Vereinbarung"
    if e.get("arbeitszeitSchichtNachtWochenende"):
        a["schicht"] = "Schicht/Nacht/Wochenende"
    if e.get("chiffrenummer"):
        a["chiffre"] = e["chiffrenummer"]
    if e.get("entfernung") is not None:
        a["entfernung_km"] = str(e["entfernung"])
    return a


def _to_job(e: dict, lat=None, lon=None) -> dict | None:
    ref = e.get("referenznummer")
    title = e.get("stellenangebotsTitel") or e.get("hauptberuf")
    if not ref or not title:
        return None
    locs = e.get("stellenlokationen") or []
    place = _pick_place(locs, lat, lon)
    addr = place.get("adresse") or {}
    towns = list(dict.fromkeys(l.get("adresse", {}).get("ort") for l in locs if l.get("adresse", {}).get("ort")))
    lo, hi, stxt = _salary(e)
    return job(
        SOURCE, ref, ba_url(ref), title,
        company=clean(e.get("firma")) or None,
        location=", ".join(towns) or None,
        lat=place.get("breite"), lon=place.get("laenge"), postal_code=addr.get("plz"),
        work_mode=_mode(e, title),
        employment=_employment(e, title),
        posted_at=_epoch((e.get("veroeffentlichungszeitraum") or {}).get("von") or e.get("datumErsteVeroeffentlichung")),
        salary_min=lo, salary_max=hi, salary_text=stxt,
        apply_url=e.get("externeURL") or None,
        attrs=_attrs(e),
    )


async def _get(client: httpx.AsyncClient, limiter: Limiter, path: str, params=None) -> httpx.Response:
    await limiter.wait()
    return await client.get(API + path, params=params, headers=API_HEADERS, timeout=TIMEOUT)


async def search(client: httpx.AsyncClient, q: dict, limiter: Limiter) -> list[dict]:
    base = {"size": PAGE_SIZE, "sort": "veroeffdatum", "angebotsart": "1;4;34"}
    if q.get("place"):
        base["wo"] = q["place"]
        base["umkreis"] = max(0, min(200, int(round(q.get("radius_km") or 0))))
    if q.get("remote"):
        base["homeoffice"] = "prozentual_100"   # 100 % only; "nach Vereinbarung" is hybrid at best
    if q.get("no_agency"):
        # not part of the shared q contract; lets a caller drop recruiters server-side (search items carry no flag)
        base["pav"] = "false"
        base["zeitarbeit"] = "false"
    age = _age_bucket(q.get("max_age_days"))
    if age:
        base["veroeffentlichtseit"] = age
    cutoff = (dt.datetime.now(BERLIN) - dt.timedelta(days=q["max_age_days"])).timestamp() \
        if q.get("max_age_days") else None

    seen, out = set(), []
    for kw in (q.get("keywords") or [None]):
        params = dict(base)
        if kw and kw.strip():
            params["was"] = kw.strip()
        for page in range(1, min(q.get("max_pages") or 1, MAX_PAGE) + 1):
            r = await _get(client, limiter, "/pc/v6/jobs", {**params, "page": page})
            r.raise_for_status()
            data = r.json()
            items = data.get("ergebnisliste") or []
            for e in items:
                j = _to_job(e, q.get("lat"), q.get("lon"))
                if not j or j["source_id"] in seen:
                    continue
                # bucket rounding widens the window; trim back to what was asked
                if cutoff and j["posted_at"] and j["posted_at"] < cutoff - 86400:
                    continue
                seen.add(j["source_id"])
                out.append(j)
            if len(items) < PAGE_SIZE or page * PAGE_SIZE >= (data.get("maxErgebnisse") or 0):
                break
    return out


def _apply_link(desc: str) -> str | None:
    for u in _URL.findall(desc):
        u = u.rstrip(".,;:\\")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if "arbeitsagentur.de" not in u and _APPLYISH.search(u):
            return u
    return None


async def detail(client: httpx.AsyncClient, job_: dict, limiter: Limiter) -> dict | None:
    ref = job_["source_id"]
    key = base64.b64encode(ref.encode()).decode()
    r = await _get(client, limiter, f"/pc/v4/jobdetails/{key}")
    if r.status_code == 404:
        return None     # taken down
    r.raise_for_status()
    e = r.json()
    fresh = _to_job(e, job_.get("lat"), job_.get("lon")) or {}
    out = dict(job_)
    # detail repeats the search fields; take its values where it has them, keep ours otherwise
    for k, v in fresh.items():
        if k in ("source", "source_id", "url", "attrs"):
            continue
        if v not in (None, [], ""):
            out[k] = v
    out["attrs"] = {**job_.get("attrs", {}), **fresh.get("attrs", {})}

    raw = e.get("stellenangebotsBeschreibung") or ""
    desc = text(raw) if "<" in raw and re.search(r"</?\w+[^>]*>", raw) else raw.strip()
    out["description"] = desc or out.get("description")
    if not out.get("apply_url"):
        out["apply_url"] = _apply_link(desc)
    emails = [m for m in _EMAIL.findall(desc) if "arbeitsagentur.de" not in m.lower()]
    out["apply_email"] = emails[0] if emails else out.get("apply_email")

    pav, anu = e.get("istPrivateArbeitsvermittlung"), e.get("istArbeitnehmerUeberlassung")
    if pav or anu:
        out["agency"] = True
        out["attrs"]["vermittlung"] = "Zeitarbeit" if anu else "Personalvermittlung"
    elif pav is False and anu is False:
        out["agency"] = False
    if e.get("allianzpartnerName") and e.get("allianzpartnerName") != "arbeitsagentur.de":
        out["attrs"]["partner"] = e["allianzpartnerName"]
    # allianzpartnerUrl is the employer's / board's homepage, not the ad — useful context, not an apply_url
    site = e.get("allianzpartnerUrl")
    if site and "arbeitsagentur.de" not in site:
        out["attrs"]["website"] = site
    if out.get("work_mode") is None:
        out["work_mode"] = guess_mode(out["title"], desc[:3000])
    return out


if __name__ == "__main__":
    import asyncio
    import os
    import sys
    import time

    async def _main():
        q = {"keywords": sys.argv[1:] or ["python entwickler"], "place": "Berlin", "lat": 52.52, "lon": 13.405,
             "radius_km": 25, "remote": False, "max_age_days": 30, "max_pages": 1}
        lim = Limiter(INTERVAL)
        async with httpx.AsyncClient(follow_redirects=True) as c:
            jobs = await search(c, q, lim)
            print(f"{len(jobs)} jobs")
            for j in jobs:
                day = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
                print(f"  {j['title'][:60]} | {j['company']} | {j['location']} | {j['work_mode']} | {day} | {j['apply_url']}")
            n = int(os.environ.get("BA_DETAILS", "5"))
            hits = 0
            for j in jobs[:n]:
                d = await detail(c, j, lim)
                if not d:
                    print("  gone:", j["source_id"])
                    continue
                hits += bool(d["apply_url"] or d["apply_email"])
                print(f"\n# {d['title']} | {d['company']} | agency={d['agency']} | {d['work_mode']} | {d['employment']}")
                print(f"  url={d['url']}\n  apply={d['apply_url']} email={d['apply_email']}")
                print(f"  salary={d['salary_min']}-{d['salary_max']} ({d['salary_text']}) plz={d['postal_code']} "
                      f"geo={d['lat']},{d['lon']} attrs={d['attrs']}")
                print("  " + (d["description"] or "")[:200].replace("\n", " / "))
            print(f"\n{hits}/{min(n, len(jobs))} details with apply_url or email")

    asyncio.run(_main())
