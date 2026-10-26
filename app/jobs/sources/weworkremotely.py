"""We Work Remotely: remote-only job board, read via RSS.

Verified 2026-10-19 from the deals container:
- https://weworkremotely.com/remote-jobs.rss -> ~90 items (~0.8 MB) across all categories, mostly the
  last 4 weeks (a few stale ones from months/years ago). Category feeds exist
  (/categories/remote-back-end-programming-jobs.rss, 5 items) but add nothing the main feed lacks.
- No keyword search feed: /remote-jobs/search.rss?term= answers 406 -> filter locally.
- Item: title "Company: Role" (split on the first ": "), region ("Anywhere in the World", "USA Only"),
  country, state, skills, category, type ("Full-Time", "Contract"), pubDate, link, description (full
  HTML ending in "To apply: <the WWR page>").
- detail(): the posting page's "Apply now" button (id job-cta-alt) points to the employer's ATS
  -> apply_url. The feed alone never has it.
No published API terms; it is a public feed: keep `url` (the WWR page) as the source link, read the
main feed at most every 15 min (it is cached in-process), detail() only for jobs worth showing.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlsplit

from .common import Limiter, clean, job, text

SOURCE = "weworkremotely"
LABEL = "We Work Remotely"
INTERVAL = 10.0

FEED = "https://weworkremotely.com/remote-jobs.rss"
CACHE_TTL = 15 * 60

_cache: tuple[float, list[dict]] | None = None

_EMPLOYMENT = {"full-time": ["vollzeit"], "part-time": ["teilzeit"], "contract": ["freelance"],
               "internship": ["praktikum"]}


def _word(w: str) -> re.Pattern:
    # short words need boundaries ("go", "ai"); long ones may sit inside compounds
    if len(w) <= 3:
        return re.compile(r"(?<![\w])" + re.escape(w) + r"(?![\w])", re.I)
    return re.compile(re.escape(w), re.I)


def _matcher(keywords: list[str]):
    groups = [[_word(w) for w in k.split()] for k in keywords if k.strip()]
    return lambda hay: not groups or any(all(p.search(hay) for p in g) for g in groups)


async def _feed(client, limiter: Limiter) -> list[dict]:
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_TTL:
        return _cache[1]
    await limiter.wait()
    r = await client.get(FEED, headers={"Accept": "application/rss+xml, application/xml;q=0.9"})
    r.raise_for_status()
    rows = []
    for it in ET.fromstring(r.content).findall("./channel/item"):
        rows.append({t: (it.findtext(t) or "").strip() for t in
                     ("title", "region", "country", "state", "skills", "category", "type",
                      "description", "pubDate", "guid", "link")})
    _cache = (time.time(), rows)
    return rows


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    if not q.get("remote"):
        return []
    match = _matcher(q.get("keywords") or [])
    cutoff = time.time() - (q.get("max_age_days") or 30) * 86400
    out = []
    for it in await _feed(client, limiter):
        try:
            posted = parsedate_to_datetime(it["pubDate"]).timestamp()
        except (TypeError, ValueError):
            posted = None
        if posted and posted < cutoff:
            continue
        company, _, role = clean(it["title"]).partition(": ")
        if not role:
            company, role = "", company
        desc = re.sub(r"\s*To apply:\s*\S+\s*$", "", text(it["description"]))
        if not match(" ".join([role, it["skills"], it["category"], desc])):
            continue
        link = it["link"] or it["guid"]
        region = clean(it["region"])
        country = clean(it["country"])
        out.append(job(
            SOURCE, link.rstrip("/").rsplit("/", 1)[-1], link, role,
            company=company or None,
            location=region or country or None,
            work_mode="remote",
            employment=_EMPLOYMENT.get(it["type"].lower(), []),
            posted_at=posted,
            description=desc or None,
            attrs={k: v for k, v in {"region": region, "country": country[:200], "category": it["category"],
                                     "skills": it["skills"][:200]}.items() if v},
        ))
    return out


_CTA = re.compile(r'<a[^>]*id="job-cta-alt"[^>]*href="([^"]+)"|<a[^>]*href="([^"]+)"[^>]*id="job-cta-alt"')


async def detail(client, job_: dict, limiter: Limiter) -> dict | None:
    """The employer's apply link from the posting page's "Apply now" button."""
    await limiter.wait()
    r = await client.get(job_["url"])
    if r.status_code != 200:
        return None
    m = _CTA.search(r.text)
    if not m:
        return None
    href = unescape(m.group(1) or m.group(2)).strip()
    if href.lower().startswith("mailto:"):
        return {"apply_email": href[7:].split("?")[0] or None}
    host = urlsplit(href).netloc.lower()
    if not host or host.endswith("weworkremotely.com"):
        return None
    return {"apply_url": href}


if __name__ == "__main__":
    import asyncio

    import httpx

    from .common import HEADERS, TIMEOUT

    def _line(j):
        ts = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
        return " | ".join(str(x) for x in (j["title"][:60], j["company"], j["location"], j["work_mode"], ts, j["apply_url"], j["employment"]))

    async def _main():
        lim = Limiter(INTERVAL)
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            q = {"keywords": ["python"], "place": None, "remote": True, "max_age_days": 30, "max_pages": 3}
            jobs = await search(c, q, lim)
            for j in jobs[:4]:
                j.update(await detail(c, j, lim) or {})
            print(f"--- remote=True: {len(jobs)} jobs")
            for j in jobs:
                print(_line(j))
            assert await search(c, dict(q, remote=False), lim) == []

    asyncio.run(_main())
