"""Remotive: remote-only job board, free public JSON API.

Verified 2026-10-19 from the deals container:
- GET https://remotive.com/api/remote-jobs -> {"00-warning", "0-legal-notice", "job-count", "jobs": [...]}
- The free feed is tiny (12 jobs today) and `search=` / `limit=` are effectively ignored: search=python
  and search=shopify both returned the same 12 jobs. So: one GET of the whole feed, filter locally.
- No apply link in the API; `url` is the Remotive posting page -> apply_url stays None.
- `salary` is free text ("$170k - $200k", "$50-$75 /hour", "OTE $25k - $35k"), mostly USD.
Terms (0-legal-notice): link back to the Remotive URL AND name Remotive as the source; do not push the
jobs to third-party boards (Jooble, Google Jobs, LinkedIn ...); jobs are delayed 24 h; "you only need to
GET ... a couple of times a day (we advise max. 4 times a day)", excessive requests get blocked.
-> the feed is cached 6 h in-process (= max 4 GETs/day while the poller runs); a restart refetches.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .common import Limiter, job, text, yearly

SOURCE = "remotive"
LABEL = "Remotive"
INTERVAL = 60.0          # never the real guard; CACHE_TTL is

API = "https://remotive.com/api/remote-jobs"
CACHE_TTL = 6 * 3600

_cache: tuple[float, list[dict]] | None = None

_EMPLOYMENT = {"full_time": ["vollzeit"], "part_time": ["teilzeit"], "freelance": ["freelance"],
               "contract": ["freelance"], "internship": ["praktikum"]}


def _word(w: str) -> re.Pattern:
    # short words need boundaries ("go", "ai"); long ones may sit inside compounds
    if len(w) <= 3:
        return re.compile(r"(?<![\w])" + re.escape(w) + r"(?![\w])", re.I)
    return re.compile(re.escape(w), re.I)


def _matcher(keywords: list[str]):
    groups = [[_word(w) for w in k.split()] for k in keywords if k.strip()]
    return lambda hay: not groups or any(all(p.search(hay) for p in g) for g in groups)


_NUM = re.compile(r"(\d+(?:[.,]\d+)?)\s*(k)?\b", re.I)


def _salary(s: str) -> tuple[float | None, float | None]:
    """EUR figures only, and only when the unit is clear; anything else stays text."""
    if not s or not re.search(r"€|\beur\b", s, re.I) or re.search(r"[$£]|usd|gbp|chf", s, re.I):
        return None, None
    if re.search(r"per task|ote|equity", s, re.I):
        return None, None
    vals = []
    for n, k in _NUM.findall(s):
        n = n.replace(",", ".") if k else n.replace(",", "").replace(".", "")
        try:
            vals.append(float(n) * (1000 if k else 1))
        except ValueError:
            return None, None
    if not vals or len(vals) > 2:
        return None, None
    unit = ("hour" if re.search(r"hour|/h\b|stunde", s, re.I) else
            "month" if re.search(r"month|monat", s, re.I) else
            "year" if re.search(r"year|annual|jahr|p\.a\.", s, re.I) or min(vals) >= 10000 else None)
    if unit is None:
        return None, None
    return yearly(min(vals), unit), yearly(max(vals), unit)


async def _feed(client, limiter: Limiter) -> list[dict]:
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_TTL:
        return _cache[1]
    await limiter.wait()
    r = await client.get(API, headers={"Accept": "application/json"})
    r.raise_for_status()
    _cache = (time.time(), r.json().get("jobs") or [])
    return _cache[1]


def _epoch(s: str | None) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    if not q.get("remote"):
        return []
    match = _matcher(q.get("keywords") or [])
    cutoff = time.time() - (q.get("max_age_days") or 30) * 86400
    out = []
    for j in await _feed(client, limiter):
        posted = _epoch(j.get("publication_date"))
        if posted and posted < cutoff:
            continue
        desc = text(j.get("description"))
        hay = " ".join([j.get("title") or "", j.get("category") or "", " ".join(j.get("tags") or []), desc])
        if not match(hay):
            continue
        sal = (j.get("salary") or "").strip()
        lo, hi = _salary(sal)
        region = (j.get("candidate_required_location") or "").strip()
        out.append(job(
            SOURCE, j["id"], j["url"], j.get("title") or "",
            company=j.get("company_name") or None,
            location=region or None,
            work_mode="remote",
            employment=_EMPLOYMENT.get(j.get("job_type") or "", []),
            posted_at=posted,
            salary_min=lo, salary_max=hi, salary_text=sal or None,
            description=desc or None,
            attrs={k: v for k, v in {"region": region[:200], "category": j.get("category") or "",
                                     "tags": ", ".join(j.get("tags") or [])[:200]}.items() if v},
        ))
    return out


if __name__ == "__main__":
    import asyncio

    import httpx

    from .common import HEADERS, TIMEOUT

    def _line(j):
        ts = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
        return " | ".join(str(x) for x in (j["title"][:60], j["company"], j["location"], j["work_mode"], ts, j["apply_url"], j["salary_text"], j["salary_min"]))

    async def _main():
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            q = {"keywords": ["python"], "place": None, "remote": True, "max_age_days": 30, "max_pages": 3}
            jobs = await search(c, q, Limiter(INTERVAL))
            print(f"--- remote=True: {len(jobs)} jobs")
            for j in jobs:
                print(_line(j))
            assert await search(c, dict(q, remote=False), Limiter(INTERVAL)) == []
        for s in ("€60k - €80k", "60.000 - 75.000 EUR per year", "€25/hour", "$170k - $200k", "€4.500 / month"):
            print(s, "->", _salary(s))

    asyncio.run(_main())
