"""Jobicy: remote-only job board, free public JSON API (no key).

Verified 2026-10-19 from the deals container:
- GET https://jobicy.com/api/jobs?count=100&geo=germany&tag=<kw>[&cursor=]  (the docs' canonical
  endpoint; /api/v2/remote-jobs answers identically) -> {"jobs", "nextCursor", "hasMore", ...}
- `tag` is a server-side keyword search over the job content; geo=germany keeps jobs open to Germany
  (jobGeo "Europe", "EMEA", "Anywhere", ...). The feed only holds the last 7 days.
- Fields: id, url (Jobicy page), jobTitle, companyName, jobGeo, jobType[], jobLevel, jobDescription
  (HTML), pubDate (ISO), salaryMin/salaryMax/salaryCurrency/salaryPeriod when given.
- Direct ATS links are a paid (Bearer key) feature; the public `url` is the Jobicy page and its
  "Apply" button stays on Jobicy -> apply_url stays None.
Terms (friendlyNotice + jobi.cy/apidocs "Fair use"): credit Jobicy with a direct link to the source,
apply buttons must go to the job URL from the feed, keep the canonical Jobicy URL, cache responses;
"Polling a few times per day is normally sufficient and must not exceed once per hour"
-> every request URL is cached 1 h in-process.
"""
from __future__ import annotations

import time
from datetime import datetime

from .common import Limiter, job, text, yearly

SOURCE = "jobicy"
LABEL = "Jobicy"
INTERVAL = 8.0

API = "https://jobicy.com/api/jobs"
GEO = "germany"
CACHE_TTL = 3600

_cache: dict[tuple, tuple[float, dict]] = {}

_EMPLOYMENT = {"full-time": "vollzeit", "part-time": "teilzeit", "contract": "freelance",
               "freelance": "freelance", "internship": "praktikum", "temporary": "befristet"}
_PERIOD = {"yearly": "year", "annual": "year", "monthly": "month", "weekly": "week",
           "daily": "day", "hourly": "hour"}


def _salary(j: dict):
    lo, hi, cur = j.get("salaryMin"), j.get("salaryMax"), (j.get("salaryCurrency") or "").upper()
    if not lo and not hi:
        return None, None, None
    period = _PERIOD.get((j.get("salaryPeriod") or "").lower())
    shown = "–".join(f"{v:,.0f}" for v in dict.fromkeys(v for v in (lo, hi) if v))
    txt = f"{cur or '?'} {shown} {j.get('salaryPeriod') or ''}".strip()
    if cur != "EUR" or not period:      # job() wants EUR/year; the rest stays text
        return None, None, txt
    return yearly(lo or hi, period), yearly(hi or lo, period), txt


async def _get(client, params: dict, limiter: Limiter) -> dict:
    key = tuple(sorted(params.items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    await limiter.wait()
    r = await client.get(API, params=params, headers={"Accept": "application/json"})
    r.raise_for_status()
    d = r.json()
    _cache[key] = (time.time(), d)
    return d


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    if not q.get("remote"):
        return []
    cutoff = time.time() - (q.get("max_age_days") or 30) * 86400
    out: dict[int, dict] = {}
    for kw in q.get("keywords") or []:
        params = {"count": 100, "geo": GEO, "tag": kw}
        for _ in range(max(1, q.get("max_pages") or 1)):
            d = await _get(client, params, limiter)
            rows = d.get("jobs") or []
            fresh = 0
            for j in rows:
                try:
                    posted = datetime.fromisoformat(j["pubDate"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    posted = None
                if posted and posted < cutoff:
                    continue
                fresh += 1
                if j["id"] in out:
                    continue
                lo, hi, sal = _salary(j)
                geo = " ".join((j.get("jobGeo") or "").split())
                out[j["id"]] = job(
                    SOURCE, j["id"], j["url"], j.get("jobTitle") or "",
                    company=j.get("companyName") or None,
                    location=geo or None,
                    work_mode="remote",
                    employment=sorted({_EMPLOYMENT[t.lower()] for t in j.get("jobType") or []
                                       if t.lower() in _EMPLOYMENT}),
                    posted_at=posted,
                    salary_min=lo, salary_max=hi, salary_text=sal,
                    description=text(j.get("jobDescription")) or None,
                    attrs={k: v for k, v in {
                        "region": geo,
                        "level": j.get("jobLevel") or "",
                        "industry": ", ".join(j.get("jobIndustry") or []),
                    }.items() if v},
                )
            cur = d.get("nextCursor")
            if not fresh or not cur or not d.get("hasMore"):
                break
            params = dict(params, cursor=cur)
    return list(out.values())


if __name__ == "__main__":
    import asyncio

    import httpx

    from .common import HEADERS, TIMEOUT

    def _line(j):
        ts = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
        return " | ".join(str(x) for x in (j["title"][:60], j["company"], j["location"], j["work_mode"], ts, j["apply_url"], j["salary_text"], j["salary_min"], j["employment"]))

    async def _main():
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            q = {"keywords": ["python"], "place": None, "remote": True, "max_age_days": 14, "max_pages": 3}
            jobs = await search(c, q, Limiter(INTERVAL))
            print(f"--- remote=True: {len(jobs)} jobs")
            for j in jobs:
                print(_line(j))
            assert await search(c, dict(q, remote=False), Limiter(INTERVAL)) == []

    asyncio.run(_main())
