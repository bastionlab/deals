"""Berlin Startup Jobs: WordPress job board for Berlin startups, read via RSS.

Verified 2026-10-19 from CT 107:
- https://berlinstartupjobs.com/?s=<kw>&feed=rss2[&paged=N] is a working keyword search (WP search,
  all words must match, ordered by relevance not date; orderby= is ignored). 12 items a page,
  a page past the end answers 404. Plain /feed/ and category feeds (/engineering/feed/,
  /skill-areas/python/feed/) also work but carry no keyword filter.
- Item: title "Role // Company" (split on the last " // "; the "Post your job" page has none and is
  dropped), link, pubDate, category (board area first, then skills), description = teaser only.
- Every job is in Berlin; the feed says nothing about onsite/hybrid -> guess_mode(title).
- detail(): the posting page carries a JobPosting JSON-LD (full description, employmentType,
  jobLocation, jobLocationType) and an "Apply now" button that points to the employer's ATS
  (join.com, personio, ...) or is a mailto: -> apply_url / apply_email.
No published API terms; it is a public feed: keep `url` as the source link, poll gently.
"""
from __future__ import annotations

import json
import math
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlsplit

from .common import Limiter, clean, guess_mode, job, text

SOURCE = "berlinstartupjobs"
LABEL = "Berlin Startup Jobs"
INTERVAL = 8.0

BASE = "https://berlinstartupjobs.com/"
BERLIN = (52.52, 13.405)
_POST_ID = re.compile(r"[?&]p=(\d+)")


def _near_berlin(q: dict) -> bool:
    place = (q.get("place") or "").strip().lower()
    if not place:
        return True
    if "berlin" in place:
        return True
    lat, lon = q.get("lat"), q.get("lon")
    if lat is None or lon is None:
        return False
    # equirectangular is plenty at this scale; 15 km = Berlin's own radius around its centre
    dx = math.radians(lon - BERLIN[1]) * math.cos(math.radians((lat + BERLIN[0]) / 2))
    dy = math.radians(lat - BERLIN[0])
    return 6371 * math.hypot(dx, dy) <= (q.get("radius_km") or 0) + 15


def _split(title: str) -> tuple[str, str | None]:
    if " // " in title:
        role, company = title.rsplit(" // ", 1)
        return role.strip(), company.strip() or None
    return title, None


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    if not q.get("remote") and not _near_berlin(q):
        return []
    cutoff = time.time() - (q.get("max_age_days") or 30) * 86400
    out: dict[str, dict] = {}
    for kw in q.get("keywords") or []:
        for page in range(1, max(1, q.get("max_pages") or 1) + 1):
            params = {"s": kw, "feed": "rss2"}
            if page > 1:
                params["paged"] = page
            await limiter.wait()
            r = await client.get(BASE, params=params,
                                 headers={"Accept": "application/rss+xml, application/xml;q=0.9"})
            if r.status_code == 404:        # past the last page
                break
            r.raise_for_status()
            items = ET.fromstring(r.content).findall("./channel/item")
            fresh = 0
            for it in items:
                title = clean(it.findtext("title"))
                role, company = _split(title)
                if not company:             # "Post your job" and other non-job pages
                    continue
                try:
                    posted = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
                except (TypeError, ValueError):
                    posted = None
                if posted and posted < cutoff:
                    continue
                fresh += 1
                link = (it.findtext("link") or "").strip()
                m = _POST_ID.search(it.findtext("guid") or "")
                sid = m.group(1) if m else link
                if sid in out:
                    continue
                mode = guess_mode(role)
                # the remote leg only wants what the ad itself calls remote
                if q.get("remote") and mode != "remote":
                    continue
                cats = [clean(c.text) for c in it.findall("category") if c.text]
                desc = text(it.findtext("description"))
                desc = re.sub(r"\s*Read more »\s*$", "", desc)
                out[sid] = job(
                    SOURCE, sid, link, role,
                    company=company,
                    location="Berlin",
                    work_mode=mode,
                    posted_at=posted,
                    description=desc or None,
                    attrs={k: v for k, v in {"area": cats[0] if cats else "",
                                             "tags": ", ".join(cats[1:])[:200]}.items() if v},
                )
            if len(items) < 12 or not fresh:
                break
    return list(out.values())


_LD = re.compile(r"(?s)<script[^>]*application/ld\+json[^>]*>(.*?)</script>")
_APPLY = re.compile(r'(?s)<a[^>]*href="([^"]+)"[^>]*>\s*Apply now')
_EMPLOYMENT = {"FULL_TIME": "vollzeit", "PART_TIME": "teilzeit", "INTERN": "praktikum",
               "CONTRACTOR": "freelance", "TEMPORARY": "befristet"}


async def detail(client, job_: dict, limiter: Limiter) -> dict | None:
    """Full text, employment, remote flag and the employer's apply link from the posting page."""
    await limiter.wait()
    r = await client.get(job_["url"])
    if r.status_code != 200:
        return None
    page = r.text
    upd: dict = {}
    for raw in _LD.findall(page):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("@type") != "JobPosting":
            continue
        if d.get("description"):
            upd["description"] = text(d["description"].replace("\n", "<br>"))
        et = d.get("employmentType")
        et = et if isinstance(et, list) else [et] if et else []
        emp = sorted({_EMPLOYMENT[e] for e in et if e in _EMPLOYMENT})
        if emp:
            upd["employment"] = emp
        addr = ((d.get("jobLocation") or {}).get("address") or {}) if isinstance(d.get("jobLocation"), dict) else {}
        if addr.get("addressLocality"):
            upd["location"] = addr["addressLocality"]
        if d.get("jobLocationType") == "TELECOMMUTE":
            upd["work_mode"] = "remote"
        elif not job_.get("work_mode"):
            mode = guess_mode(job_.get("title"), upd.get("description"))
            if mode:
                upd["work_mode"] = mode
        if d.get("validThrough"):
            upd["attrs"] = dict(job_.get("attrs") or {}, valid_through=d["validThrough"])
        break
    m = _APPLY.search(page)
    if m:
        href = unescape(m.group(1)).strip()
        if href.lower().startswith("mailto:"):
            upd["apply_email"] = href[7:].split("?")[0] or None
        elif not urlsplit(href).netloc.lower().endswith("berlinstartupjobs.com"):
            upd["apply_url"] = href
    return upd or None


if __name__ == "__main__":
    import asyncio

    import httpx

    from .common import HEADERS, TIMEOUT

    def _line(j):
        ts = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
        return " | ".join(str(x) for x in (j["title"][:60], j["company"], j["location"], j["work_mode"], ts,
                                           j["apply_url"] or (j["apply_email"] and "mailto:" + j["apply_email"]), j["employment"]))

    async def _main():
        lim = Limiter(INTERVAL)
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            for remote, place in ((True, None), (False, "Berlin")):
                q = {"keywords": ["python"], "place": place, "lat": None, "lon": None, "radius_km": 0,
                     "remote": remote, "max_age_days": 180, "max_pages": 2}
                jobs = await search(c, q, lim)
                for j in jobs[:5]:
                    j.update(await detail(c, j, lim) or {})
                print(f"--- remote={remote} place={place}: {len(jobs)} jobs")
                for j in jobs:
                    print(_line(j))
            print("hamburg:", await search(c, dict(q, place="Hamburg"), lim))
            print("potsdam 30km:", _near_berlin({"place": "Potsdam", "lat": 52.39, "lon": 13.06, "radius_km": 30}))

    asyncio.run(_main())
