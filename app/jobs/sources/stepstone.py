"""StepStone.de: server-rendered result list + posting pages, embedded JSON only.

Verified 2026-10-19 from CT 107 (home IP, desktop Chrome UA, no cookies) — no
DataDome / captcha seen in ~40 requests at >= 6 s gaps; every page answered 200.

Search  GET /jobs/<what>[/in-<where>]?radius=25&orderByField=date&ag=age_7&wfh=1&page=2
- <what>/<where> are lower-case slugs, spaces -> '-', umlauts kept (percent-encoded:
  /jobs/c%23-entwickler/in-m%C3%BCnchen works). A '.' in the slug 404s ("node.js"),
  so dots become '-'. No /in-… = nationwide (StepStone then reports radius 30, ignored).
- orderByField=date sorts newest first (categorization.orderBy == "date"); the
  `sort=2` people quote is ignored (orderBy stays "relevance").
- ag=age_1 | age_7 | age_30 ("Neuer als 24h" / 7 Tage / 30 Tage) — nothing finer.
- wfh=1 = "Nur Home-Office", wfh=2 = "Teilweise Home-Office" (shown in activeFilters).
  The newer workPolicy facet (wp=611350 "Remote") is offered as a link but NOT applied
  server-side (same totals, no active filter) — do not use it.
- page=N, 25 per page; unifiedPagination.pageCount says how many.
- The list is window.__PRELOADED_STATE__["app-unifiedResultlist"].searchResults.items.
  Each item has section main | regional | semantic | recommended | family. Multi-word
  queries match as a phrase: "python entwickler" in Berlin = 11 main + 250 "semantic"
  fill-ins (sales managers…) — only main + regional (outside the radius) are kept.
- item.workFromHome: 1 = Nur Home-Office, 2 = Teilweise Home-Office, 0/None = not stated.
- item.unifiedSalary is almost always salaryType "Predicted" (StepStone's own guess,
  "geschätzt für Vollzeit") — kept only as an attr, never as the job's salary.

Posting  GET /stellenangebote--x--<id>-inline.html (the slug is decorative, any works)
- ld+json JobPosting: description (HTML), datePosted, hiringOrganization, employmentType.
- window.__PRELOADED_STATE__.JobAdContent = { props: {...} }: metaData.workType
  ("Homeoffice möglich, Teilzeit, Vollzeit"), contractType, companyCard.sectors
  ("Personaldienstleistungen" = recruiter/agency), harmonisedJobId. offerLanguage is
  the site language ("de" on English ads too), so lang stays None.
- Apply target: the page holds no button URL; the button asks
  /application/process-bff/v1/apply-button/configuration?listingGlobalId=<guid>&locale=de
  (JSON, cheap). additionalInfo.applicationType: "ThirdParty" = employer's site/ATS;
  "ATSiSquirage" / "FirstParty" = StepStone's own form (apply_url stays None).
  The ThirdParty URL sits behind /application/process-bff/v2/redirect-to-relevant-apply-form
  (locale must be de_DE, "de"/"de-DE" -> 400), which for an anonymous visitor always
  302s to /job/<guid>/application/authentication — a login wall (6/6 tried), so it
  is not asked. Instead the ad's own description usually carries the link
  (greenhouse "#application", myworkdayjobs ".../apply", tracker "jump" links):
  an ATS host or a link whose URL / anchor / lead-in says bewerben/apply is taken,
  a non-ATS one followed once without redirects to see where it lands.
  buttonType "ThirdPartyBypass" hands out the external URL directly (not seen yet).
- /public-api/v1/listingdata/ (named in the page config) answers 403 — not usable.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit

from .common import Limiter, clean, guess_mode, job, text, yearly

SOURCE = "stepstone"
LABEL = "StepStone"
INTERVAL = 7.0
BASE = "https://www.stepstone.de"
KEEP_SECTIONS = ("main", "regional")
_RL_STATE = '__PRELOADED_STATE__["app-unifiedResultlist"] = '
_WFH = {1: "remote", 2: "hybrid"}
_EMPLOYMENT = (                              # workType / contractType wording -> EMPLOYMENT
    ("vollzeit", "vollzeit"), ("teilzeit", "teilzeit"), ("werkstudent", "werkstudent"),
    ("studentenjob", "werkstudent"), ("praktikum", "praktikum"), ("ausbildung", "ausbildung"),
    ("freie mitarbeit", "freelance"), ("freiberuf", "freelance"), ("minijob", "minijob"),
    ("befristet", "befristet"),
)
_AGENCY_SECTOR = re.compile(r"(?i)personaldienstleist|zeitarbeit|personalvermittl|arbeitnehmerüberlass")
_AGENCY_TEXT = re.compile(r"(?i)\b(unser(em)? (partner|kunden)unternehmen|im auftrag unsere[sr] (kunden|mandanten)|"
                          r"für unseren (kunden|mandanten)|personalberatung(en)?|personaldienstleister|"
                          r"personalvermittlung|arbeitnehmerüberlassung)\b")


class Blocked(RuntimeError):
    pass


def _slug(s: str) -> str:
    s = re.sub(r"[\s./]+", "-", s.strip().lower()).strip("-")
    return quote(s, safe="-")


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def _age(days: int | None) -> str | None:
    if not days:
        return None
    for limit in (1, 7, 30):
        if days <= limit:
            return f"age_{limit}"
    return None


def _check(r, what: str) -> str:
    if r.status_code in (403, 429) or "captcha-delivery" in r.text[:20000] or "datadome" in r.text[:5000].lower():
        raise Blocked(f"StepStone {what}: blocked ({r.status_code})")
    r.raise_for_status()
    return r.text


def _state(page: str, marker: str) -> dict | None:
    i = page.find(marker)
    if i < 0:
        return None
    return json.JSONDecoder().raw_decode(page, i + len(marker))[0]


def search_url(kw: str, q: dict, page: int) -> str:
    path = f"/jobs/{_slug(kw)}"
    params = []
    if q.get("place"):
        path += f"/in-{_slug(q['place'])}"
        params.append(f"radius={int(q.get('radius_km') or 0)}")
    params.append("orderByField=date")
    if ag := _age(q.get("max_age_days")):
        params.append(f"ag={ag}")
    if q.get("remote"):
        params.append("wfh=1")
    if page > 1:
        params.append(f"page={page}")
    return f"{BASE}{path}?{'&'.join(params)}"


def _item(it: dict) -> dict:
    url = (it.get("url") or "").split("?")[0] or f"{BASE}/stellenangebote--x--{it['id']}-inline.html"
    loc = clean(it.get("location")) or None
    plz = it.get("postCode") or (m.group(1) if loc and (m := re.match(r"(\d{5})\s", loc)) else None)
    attrs = {"guid": it.get("harmonisedId") or ""}
    if it.get("section") != "main":
        attrs["section"] = it.get("section")
    labels = [lb.get("label") for lb in it.get("labels") or [] if lb.get("label")]
    if any(lb.get("type") == "QUICK_APPLY" for lb in it.get("labels") or []):
        attrs["apply"] = "StepStone (Schnelle Bewerbung)"
    if labels:
        attrs["labels"] = ", ".join(labels)
    sal = it.get("unifiedSalary") or {}
    smin = smax = stext = None
    if sal.get("salaryAvailable") and sal.get("min"):
        unit, per = {"Annually": ("year", "Jahr"), "Monthly": ("month", "Monat"), "Hourly": ("hour", "Stunde"),
                     "Weekly": ("week", "Woche")}.get(sal.get("period"), ("year", "Jahr"))
        span = f"{sal['min']:,}–{sal.get('max') or sal['min']:,} € / {per}".replace(",", ".")
        if sal.get("salaryType") == "Predicted":
            attrs["salary_guess"] = f"{span} (StepStone-Schätzung)"
        else:                                    # employer-stated
            smin, smax, stext = yearly(sal["min"], unit), yearly(sal.get("max"), unit), sal.get("description") or span
    if not stext and it.get("salary"):
        stext = clean(it["salary"])
    mode = _WFH.get(it.get("workFromHome")) or guess_mode(it.get("title"), loc)
    return job(
        SOURCE, it["id"], url, it.get("title") or "",
        company=clean(it.get("companyName")) or None,
        location=loc, postal_code=plz, work_mode=mode,
        posted_at=_epoch(it.get("datePosted")),
        salary_min=smin, salary_max=smax, salary_text=stext,
        description=clean(it.get("textSnippet")) or None,
        attrs=attrs,
    )


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    out, seen = [], set()
    for kw in q.get("keywords") or []:
        for page in range(1, max(1, q.get("max_pages") or 1) + 1):
            await limiter.wait()
            r = await client.get(search_url(kw, q, page))
            st = _state(_check(r, "search"), _RL_STATE)
            if st is None:
                raise Blocked(f"StepStone search: no result state in page ({r.status_code}, {len(r.text)} B)")
            res = st.get("searchResults") or {}
            items = [it for it in res.get("items") or [] if it.get("section") in KEEP_SECTIONS]
            for it in items:
                if it.get("id") and it["id"] not in seen:
                    seen.add(it["id"])
                    out.append(_item(it))
            pages = (res.get("unifiedPagination") or {}).get("pageCount") or 1
            if not items or page >= pages:
                break
    return out


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


def _employment(*texts: str | None) -> list[str]:
    t = " ".join(x for x in texts if x).lower()
    return list(dict.fromkeys(v for k, v in _EMPLOYMENT if k in t))


async def _apply_kind(client, guid: str, limiter: Limiter) -> tuple[str | None, str | None]:
    """(applicationType, external url when StepStone gives it out)."""
    await limiter.wait()
    r = await client.get(f"{BASE}/application/process-bff/v1/apply-button/configuration",
                         params={"listingGlobalId": guid, "locale": "de"},
                         headers={"Accept": "application/json"})
    _check(r, "apply-button")
    info = r.json().get("additionalInfo") or {}
    for u in info.get("urls") or []:             # ThirdPartyBypass: the employer's URL as-is
        host = urlsplit(u.get("url") or "").netloc
        if host and "stepstone." not in host:
            return info.get("applicationType"), u["url"]
    return info.get("applicationType"), None


_ATS = re.compile(r"(?i)(^|\.)(join\.com|personio\.(de|com)|greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|"
                  r"smartrecruiters\.com|recruitee\.com|softgarden\.(io|de)|successfactors\.(com|eu)|"
                  r"myworkday(jobs|site)\.com|dvinci(-hr)?\.(com|de)|onlyfy\.jobs|rexx-systems\.com|"
                  r"bite\.de|umantis\.com|haufe\.com|concludis\.de|teamtailor\.com|taleo\.net|"
                  r"icims\.com|jobvite\.com|pinpointhq\.com|recruitingapp-\d+\.de|b-ite\.de|"
                  r"heyrecruit\.de|jobs\.ashbyhq\.com|kenjo\.io|factorial(hr)?\.\w+)$")
_APPLY_WORDS = re.compile(r"(?i)bewerb|apply|application|candidate")
_NOT_APPLY = re.compile(r"(?i)datenschutz|privacy|benefit|prozess|process|faq|glassdoor|kununu|linkedin|xing|"
                        r"instagram|facebook|youtube")


def _apply_link(desc_html: str) -> str | None:
    """Best guess at the 'apply here' link inside the ad text, or None."""
    best, best_score = None, 0
    for a in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', desc_html or "", re.S):
        url = html.unescape(a.group(1)).strip()
        if not url.startswith("http") or "stepstone." in urlsplit(url).netloc:
            continue
        anchor = clean(text(a.group(2))).replace("\u00ad", "")      # soft hyphens: "Daten\xadschutz"
        lead = clean(text(desc_html[max(0, a.start() - 120):a.start()])).replace("\u00ad", "")[-80:]
        if _NOT_APPLY.search(anchor) or _NOT_APPLY.search(urlsplit(url).path):
            continue
        score = (4 if _ATS.search(urlsplit(url).netloc) else 0) + \
                (2 if _APPLY_WORDS.search(url) or _APPLY_WORDS.search(anchor) else 0) + \
                (1 if _APPLY_WORDS.search(lead) else 0)
        if score > best_score:
            best, best_score = url, score
    return best


async def _land(client, url: str, limiter: Limiter) -> str:
    """Follow one redirect hop of a tracker link; keep the link if it does not redirect."""
    if _ATS.search(urlsplit(url).netloc):
        return url
    await limiter.wait()
    try:
        r = await client.get(url, follow_redirects=False)
    except Exception:
        return url
    loc = r.headers.get("location")
    return urljoin(url, loc) if r.is_redirect and loc else url


async def detail(client, j: dict, limiter: Limiter) -> dict | None:
    await limiter.wait()
    r = await client.get(f"{BASE}/stellenangebote--x--{j['source_id']}-inline.html")
    if r.status_code in (404, 410):
        return None
    page = _check(r, "posting")
    ld = _ld(page)
    m = re.search(r"JobAdContent = \{\s*props: ", page)
    props = json.JSONDecoder().raw_decode(page, m.end())[0] if m else {}
    if not ld and not props:
        raise Blocked(f"StepStone posting {j['source_id']}: no job data in page ({len(page)} B)")
    meta = ((props.get("listingHeader") or {}).get("listingData") or {}).get("metaData") or {}
    sections = props.get("textSections") or []
    desc = text(ld.get("description")) or "\n\n".join(
        f"{s.get('title') or ''}\n{text(s.get('content'))}".strip() for s in sections)
    sectors = [s.get("name") or "" for s in (props.get("companyCard") or {}).get("sectors") or []]
    attrs = dict(j.get("attrs") or {})
    agency = None
    if any(_AGENCY_SECTOR.search(s) for s in sectors) or _AGENCY_SECTOR.search(meta.get("contractType") or ""):
        agency = True
    elif _AGENCY_TEXT.search(desc[:1500]):
        agency = True
    if sectors:
        attrs["sector"] = ", ".join(sectors)
    if meta.get("contractType"):
        attrs["contract"] = meta["contractType"]
    mode = j.get("work_mode")
    wt = (meta.get("workType") or "").lower()
    if not mode and "homeoffice" in wt.replace("-", ""):
        mode = "hybrid"                          # "Homeoffice möglich" = the ad ticks the box, not remote-only
    email = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "\n".join(text(s.get("content")) for s in sections
                                                                if "kontakt" in (s.get("title") or "").lower()))
    out = {
        "description": desc or None,
        "employment": _employment(meta.get("workType"), meta.get("contractType"), ld.get("employmentType")),
        "work_mode": mode,
        "agency": agency,
        "posted_at": j.get("posted_at") or _epoch(ld.get("datePosted")),
        "apply_email": email.group(0).rstrip(".") if email else None,
    }
    if not j.get("company") and (org := (ld.get("hiringOrganization") or {}).get("name")):
        out["company"] = clean(org)
    guid = attrs.get("guid") or props.get("harmonisedJobId") or \
        ((props.get("listingHeader") or {}).get("listingData") or {}).get("externalId")
    if guid:
        kind, url = await _apply_kind(client, guid, limiter)
        if kind:
            attrs["apply"] = "extern" if kind.startswith("ThirdParty") else "StepStone"
            attrs["apply_type"] = kind
        if not url and kind and kind.startswith("ThirdParty"):
            raw = ld.get("description") or "".join(s.get("content") or "" for s in sections)
            if link := _apply_link(raw):
                url = await _land(client, link, limiter)
                if _NOT_APPLY.search(urlsplit(url).path) or urlsplit(url).path.lower().endswith(".pdf"):
                    url = None                   # the tracker led to a privacy PDF etc.
        out["apply_url"] = url
    out["attrs"] = attrs
    return out


if __name__ == "__main__":
    import asyncio
    import time

    import httpx

    from .common import HEADERS, TIMEOUT

    import sys

    async def main():
        lim = Limiter(INTERVAL)
        kw = sys.argv[1] if len(sys.argv) > 1 else "python entwickler"
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        q = {"keywords": [kw], "place": "Berlin", "lat": None, "lon": None,
             "radius_km": 25, "max_age_days": 30, "max_pages": 1,
             "remote": len(sys.argv) > 3}
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            print(search_url(q["keywords"][0], q, 1))
            found = await search(c, q, lim)
            print(f"{len(found)} jobs")
            for j in found:
                day = time.strftime("%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "?"
                print(f"  {j['source_id']} | {j['title'][:50]} | {j['company']} | {j['location'][:30]} | "
                      f"{j['work_mode']} | {day} | {j['attrs']}")
            for j in found[:n]:
                more = await detail(c, j, lim) or {}
                j = {**j, **{k: v for k, v in more.items() if v not in (None, "", [], {})}}
                print(f"* {j['title'][:50]} | {j['company']} | {j['location']} | {j['work_mode']} | "
                      f"{time.strftime('%Y-%m-%d', time.localtime(j['posted_at']))} | {j['apply_url']} | "
                      f"agency={j['agency']} emp={j['employment']} mail={j['apply_email']} "
                      f"attrs={j['attrs']}\n    {(j['description'] or '')[:160]!r}")

    asyncio.run(main())
