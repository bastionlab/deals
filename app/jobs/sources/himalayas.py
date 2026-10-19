"""Himalayas: remote-only job board, free public JSON API (no key needed).

Verified 2026-10-19 from CT 107:
- GET https://himalayas.app/jobs/api/search?q=<kw>&country=Germany&sort=recent&page=N
  -> {"comments", "totalCount", "jobs": [...20]}; server-side keyword search.
  country=Germany keeps jobs open to Germany (incl. worldwide ones); sort=recent is newest first
  after a few promoted jobs on page 1. Search has page= only (no cursor; nextCursor is for the
  unfiltered /jobs/api browse feed). Per their docs: page until a page is empty, totalCount is a
  stale estimate, results can shift -> de-duplicate on guid.
- Fields: title, companyName, employmentType, minSalary/maxSalary/currency/salaryPeriod,
  locationRestrictions (country list, [] = anywhere), pubDate (epoch), description (HTML), guid.
- applicationLink is always the Himalayas page (+utm) -> apply_url stays None. The job pages answer 403
  to scripts, so there is no detail().
Terms (himalayas.app/api): link back to the Himalayas URL AND mention Himalayas as the source; do not
resubmit the jobs to other boards/aggregators. Keyless callers share one public rate allowance
(a free key gets 60/min, 10k/day) -> keep requests sparse.
"""
from __future__ import annotations

import time

from .common import Limiter, job, text, yearly

SOURCE = "himalayas"
LABEL = "Himalayas"
INTERVAL = 6.0

API = "https://himalayas.app/jobs/api/search"
COUNTRY = "Germany"      # the poller's user lives here; the remote leg needs jobs open to DE

_EMPLOYMENT = {"full time": ["vollzeit"], "part time": ["teilzeit"], "contractor": ["freelance"],
               "intern": ["praktikum"], "temporary": ["befristet"]}
_PERIOD = {"annual": "year", "yearly": "year", "monthly": "month", "weekly": "week",
           "daily": "day", "hourly": "hour"}


def _region(countries: list[str]) -> str:
    if not countries:
        return "Worldwide"
    if len(countries) <= 6:
        return ", ".join(countries)
    return ", ".join(countries[:5]) + f" +{len(countries) - 5}"


def _salary(j: dict):
    lo, hi, cur = j.get("minSalary"), j.get("maxSalary"), (j.get("currency") or "").upper()
    if not lo and not hi:
        return None, None, None
    period = _PERIOD.get((j.get("salaryPeriod") or "").lower())
    shown = "–".join(f"{v:,.0f}" for v in (lo, hi) if v)
    txt = f"{cur or '?'} {shown} {j.get('salaryPeriod') or ''}".strip()
    # job() wants EUR per year; other currencies stay text only
    if cur != "EUR" or not period:
        return None, None, txt
    return yearly(lo or hi, period), yearly(hi or lo, period), txt


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    if not q.get("remote"):
        return []
    cutoff = time.time() - (q.get("max_age_days") or 30) * 86400
    out: dict[str, dict] = {}
    for kw in q.get("keywords") or []:
        for page in range(1, max(1, q.get("max_pages") or 1) + 1):
            await limiter.wait()
            r = await client.get(API, params={"q": kw, "country": COUNTRY, "sort": "recent", "page": page},
                                 headers={"Accept": "application/json"})
            r.raise_for_status()
            rows = r.json().get("jobs") or []
            fresh = [j for j in rows if (j.get("pubDate") or 0) >= cutoff]
            for j in fresh:
                guid = j.get("guid") or ""
                if not guid or guid in out:
                    continue
                lo, hi, sal = _salary(j)
                region = _region(j.get("locationRestrictions") or [])
                out[guid] = job(
                    SOURCE, guid.split("/companies/", 1)[-1], guid, j.get("title") or "",
                    company=j.get("companyName") or None,
                    location=region,
                    work_mode="remote",
                    employment=_EMPLOYMENT.get((j.get("employmentType") or "").lower(), []),
                    posted_at=j.get("pubDate"),
                    salary_min=lo, salary_max=hi, salary_text=sal,
                    description=text(j.get("description")) or None,
                    attrs={k: v for k, v in {
                        "region": region,
                        "seniority": ", ".join(j.get("seniority") or []),
                        "category": ", ".join((j.get("parentCategories") or [])[:3]),
                    }.items() if v},
                )
            if not fresh or len(rows) == 0:
                break
    return list(out.values())


if __name__ == "__main__":
    import asyncio

    import httpx

    from .common import HEADERS, TIMEOUT

    def _line(j):
        ts = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
        return " | ".join(str(x) for x in (j["title"][:60], j["company"], j["location"][:50], j["work_mode"], ts, j["apply_url"], j["salary_text"], j["salary_min"]))

    async def _main():
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            q = {"keywords": ["python"], "place": None, "remote": True, "max_age_days": 7, "max_pages": 3}
            jobs = await search(c, q, Limiter(INTERVAL))
            print(f"--- remote=True: {len(jobs)} jobs")
            for j in jobs:
                print(_line(j))
            assert await search(c, dict(q, remote=False), Limiter(INTERVAL)) == []

    asyncio.run(_main())
