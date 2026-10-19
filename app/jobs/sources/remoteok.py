"""Remote OK: remote-only job board, free public JSON API.

Verified 2026-10-19 from CT 107:
- GET https://remoteok.com/api -> list; element 0 is {"last_updated", "legal"}, then ~100 newest jobs
  (spanning weeks). No paging. `?tags=python` (or `?tag=`) filters server-side by tag (100 python
  jobs); `?tags=a,b` narrows further. Tags are single words, so free-text keywords are matched
  locally as well: we read the plain feed plus one tags= request per keyword and filter both.
- Fields: id, epoch, position, company, location (often ""), tags[], description (HTML),
  salary_min/salary_max (USD, 0 = none), url and apply_url -> both the Remote OK page
  (apply goes through remoteok.com/l/<id>), so apply_url stays None.
- Text arrives double-encoded (UTF-8 bytes as \\u00e2\\u0080\\u0099 escapes); _fix() undoes that.
Terms (element 0 "legal"): link back to the Remote OK URL (followed, no nofollow) and mention Remote OK
as the source, or API access gets suspended; do not use their logo. They block heavy pollers
-> every URL is cached 30 min in-process and requests are spaced 10 s.
"""
from __future__ import annotations

import re
import time

from .common import Limiter, job, text

SOURCE = "remoteok"
LABEL = "Remote OK"
INTERVAL = 10.0

API = "https://remoteok.com/api"
CACHE_TTL = 30 * 60

_cache: dict[str, tuple[float, list]] = {}


def _fix(s: str | None) -> str:
    if not s:
        return ""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s        # already fine


def _word(w: str) -> re.Pattern:
    # short words need boundaries ("go", "ai"); long ones may sit inside compounds
    if len(w) <= 3:
        return re.compile(r"(?<![\w])" + re.escape(w) + r"(?![\w])", re.I)
    return re.compile(re.escape(w), re.I)


def _matcher(keywords: list[str]):
    groups = [[_word(w) for w in k.split()] for k in keywords if k.strip()]
    return lambda hay: not groups or any(all(p.search(hay) for p in g) for g in groups)


async def _get(client, url: str, limiter: Limiter) -> list:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    await limiter.wait()
    r = await client.get(url, headers={"Accept": "application/json"})
    r.raise_for_status()
    rows = [j for j in r.json() if isinstance(j, dict) and j.get("id")]   # drops the legal notice
    _cache[url] = (time.time(), rows)
    return rows


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    if not q.get("remote"):
        return []
    keywords = [k for k in q.get("keywords") or [] if k.strip()]
    match = _matcher(keywords)
    cutoff = time.time() - (q.get("max_age_days") or 30) * 86400
    rows = list(await _get(client, API, limiter))
    for kw in keywords:
        tags = ",".join(re.sub(r"[^\w+#.-]", "", w.lower()) for w in kw.split())
        if tags:
            rows += await _get(client, f"{API}?tags={tags}", limiter)
    out: dict[str, dict] = {}
    for j in rows:
        sid = str(j["id"])
        if sid in out or (j.get("epoch") or 0) < cutoff:
            continue
        title, desc = _fix(j.get("position")), text(_fix(j.get("description")))
        tags = j.get("tags") or []
        if not match(" ".join([title, " ".join(tags), desc])):
            continue
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        sal = f"USD {lo:,}–{hi:,} /year" if lo and hi and lo != hi else (f"USD {lo or hi:,} /year" if lo or hi else None)
        loc = _fix(j.get("location")).strip()
        out[sid] = job(
            SOURCE, sid, j.get("url") or f"https://remoteok.com/remote-jobs/{sid}", title,
            company=_fix(j.get("company")) or None,
            location=loc or None,
            work_mode="remote",
            employment=["freelance"] if re.search(r"(?i)\b(contract|freelance)\b", title) else [],
            posted_at=j.get("epoch"),
            salary_text=sal,      # USD; job() salary_min/max are EUR only
            description=desc or None,
            attrs={k: v for k, v in {"region": loc, "tags": ", ".join(tags)[:200]}.items() if v},
        )
    return list(out.values())


if __name__ == "__main__":
    import asyncio

    import httpx

    from .common import HEADERS, TIMEOUT

    def _line(j):
        ts = time.strftime("%Y-%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "-"
        return " | ".join(str(x) for x in (j["title"][:60], j["company"], j["location"], j["work_mode"], ts, j["apply_url"], j["salary_text"]))

    async def _main():
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            q = {"keywords": ["python"], "place": None, "remote": True, "max_age_days": 14, "max_pages": 3}
            jobs = await search(c, q, Limiter(INTERVAL))
            print(f"--- remote=True: {len(jobs)} jobs")
            for j in jobs:
                print(_line(j))
            assert await search(c, dict(q, remote=False), Limiter(INTERVAL)) == []

    asyncio.run(_main())
