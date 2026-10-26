"""Arbeitnow: German/EU job board with a free public JSON API.

Verified 2026-10-19 from the deals container:
- GET https://www.arbeitnow.com/api/job-board-api?page=N -> {"data": [...300 jobs], "links": {"next"}, "meta"}
  newest first by created_at (epoch); one page is ~2 MB and spans ~1.5 days.
- NO server-side search: search=, q=, remote=, location=, tags= are all ignored (same 300 jobs back).
  So we page the newest postings once and filter locally (keywords, remote flag, place name).
- Each job carries the full HTML description, remote bool, tags, job_types, location (a city), url.
- The posting page links "<url>/apply", which 302s to the employer's ATS (join.com, personio, ...);
  detail() reads that Location header without following it -> apply_url.
Terms (meta.terms): "free public API, please do not abuse. I would appreciate linking back to the site."
-> keep `url` (the Arbeitnow page) visible as the source link; jobs refresh hourly, so a 15 min cache.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from .common import Limiter, guess_mode, job, text

SOURCE = "arbeitnow"
LABEL = "Arbeitnow"
INTERVAL = 6.0

API = "https://www.arbeitnow.com/api/job-board-api"
CACHE_TTL = 15 * 60

# every leg and every saved search reads the same newest pages: fetch once, filter many times
_pages: dict[int, tuple[float, dict]] = {}

_EMPLOYMENT = [
    ("vollzeit", r"full[\s-]?time|fulltime|vollzeit|cdi"),
    ("teilzeit", r"part[\s-]?time|teilzeit"),
    ("werkstudent", r"working student|werkstudent|student"),
    ("praktikum", r"\bintern|praktikum|\bstage\b"),
    ("ausbildung", r"ausbildung|apprentice"),
    ("freelance", r"freelance|freiberuf"),
    ("minijob", r"minijob|mini-job"),
    ("befristet", r"fixed[\s-]?term|befristet|cdd"),
]


def _employment(types: list[str]) -> list[str]:
    t = " ".join(types).lower()
    # "unbefristet" must not count as befristet
    t = t.replace("unbefristet", "")
    return [k for k, rx in _EMPLOYMENT if re.search(rx, t)]


def _word(w: str) -> re.Pattern:
    # short words ("go", "ai", "c#") need boundaries; long ones match inside German compounds
    # ("entwickler" in "Softwareentwickler")
    if len(w) <= 3:
        return re.compile(r"(?<![\w])" + re.escape(w) + r"(?![\w])", re.I)
    return re.compile(re.escape(w), re.I)


def _matcher(keywords: list[str]):
    groups = [[_word(w) for w in k.split()] for k in keywords if k.strip()]
    return lambda hay: not groups or any(all(p.search(hay) for p in g) for g in groups)


async def _page(client, n: int, limiter: Limiter) -> dict:
    hit = _pages.get(n)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    await limiter.wait()
    r = await client.get(API, params={"page": n}, headers={"Accept": "application/json"})
    r.raise_for_status()
    d = r.json()
    _pages[n] = (time.time(), d)
    return d


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    match = _matcher(q.get("keywords") or [])
    cutoff = time.time() - (q.get("max_age_days") or 30) * 86400
    place = (q.get("place") or "").strip().lower()
    out: dict[str, dict] = {}
    for n in range(1, max(1, q.get("max_pages") or 1) + 1):
        d = await _page(client, n, limiter)
        rows = d.get("data") or []
        fresh = [j for j in rows if (j.get("created_at") or 0) >= cutoff]
        for j in fresh:
            if q.get("remote") and not j.get("remote"):
                continue
            loc = j.get("location") or ""
            # place filter by name only: the API has no coordinates
            if not q.get("remote") and place and place not in loc.lower():
                continue
            desc = text(j.get("description"))
            hay = " ".join([j.get("title") or "", " ".join(j.get("tags") or []), desc])
            if not match(hay):
                continue
            mode = "remote" if j.get("remote") else guess_mode(j.get("title"), loc)
            out[j["slug"]] = job(
                SOURCE, j["slug"], j["url"], j.get("title") or "",
                company=j.get("company_name") or None,
                location=loc or None,
                work_mode=mode,
                employment=_employment(j.get("job_types") or []),
                posted_at=j.get("created_at"),
                description=desc or None,
                attrs={k: v for k, v in {
                    "tags": ", ".join(j.get("tags") or [])[:200],
                    "job_types": ", ".join(j.get("job_types") or [])[:200],
                }.items() if v},
            )
        if not fresh or not (d.get("links") or {}).get("next"):
            break
    return list(out.values())


async def detail(client, job_: dict, limiter: Limiter) -> dict | None:
    """Resolve "<url>/apply" (a redirect to the employer's ATS) without following it."""
    await limiter.wait()
    r = await client.get(job_["url"].rstrip("/") + "/apply", follow_redirects=False)
    loc = r.headers.get("location") if r.is_redirect else None
    if not loc:
        return None
    host = urlsplit(loc).netloc.lower()
    # a redirect back onto Arbeitnow (login, own form) is not an employer link
    if not host or host.endswith("arbeitnow.com"):
        return None
    return {"apply_url": loc}


if __name__ == "__main__":
    import asyncio

    import httpx

    from .common import HEADERS, TIMEOUT

    def _line(j):
        ts = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
        return " | ".join(str(x) for x in (j["title"][:60], j["company"], j["location"], j["work_mode"], ts, j["apply_url"]))

    async def _main():
        lim = Limiter(INTERVAL)
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            for remote, place in ((True, None), (False, "Berlin")):
                q = {"keywords": ["python"], "place": place, "lat": None, "lon": None, "radius_km": 0,
                     "remote": remote, "max_age_days": 14, "max_pages": 3}
                jobs = await search(c, q, lim)
                for j in jobs[:4]:
                    j.update(await detail(c, j, lim) or {})
                print(f"--- remote={remote} place={place}: {len(jobs)} jobs")
                for j in jobs:
                    print(_line(j))

    asyncio.run(_main())
