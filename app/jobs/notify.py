"""Strong new job matches -> Discord #jobs.

The model rates generously (most passing postings score 85-100), so only fit >= MIN_FIT goes out, first seen
after the channel was linked, not from a new search's first sweep, not thumbed down or already applied.
The same job on several portals (jobs.dedupe = company|title) goes out once."""
from __future__ import annotations

import logging

import httpx

from .. import discord
from . import db as jdb

log = logging.getLogger("jobs.discord")

CHANNEL = "jobs"
MIN_FIT = 90
MAX_PER_FLUSH = 8
SEED_GRACE = 3600
MODE = {"remote": "🏠 Remote", "hybrid": "🔀 Hybrid", "onsite": "🏢 Vor Ort"}


def _label(source: str) -> str:
    from .poller import SOURCES                 # late: poller imports this module
    return getattr(SOURCES.get(source), "LABEL", source)


def embed(j: dict) -> dict:
    facts = [f"**Match {j['fit']}**", j.get("company") or "", j.get("location") or "", MODE.get(j.get("work_mode") or "", "")]
    lines = [" · ".join(x for x in facts if x)]
    if j.get("salary_text"):
        lines.append(f"💶 {discord.clip(j['salary_text'], 100)}")
    if j.get("fazit"):
        lines.append(f"*{discord.clip(j['fazit'], 300)}*")
    pros = [p for p in (j.get("pros") or []) if isinstance(p, str)][:3]
    if pros:
        lines.append("\n".join(f"✓ {discord.clip(p, 120)}" for p in pros))
    gaps = [g for g in (j.get("gaps") or []) if isinstance(g, str)][:2]
    if gaps:
        lines.append("\n".join(f"✗ {discord.clip(g, 120)}" for g in gaps))
    apply = j.get("apply") or {}
    url = apply.get("url") or ""
    email = apply.get("email") or j.get("apply_email") or (url[7:] if url.startswith("mailto:") else "")
    if not url.startswith("http"):
        url = j["url"]                          # embeds only link http(s)
    if email:
        lines.append(f"✉️ Bewerbung an `{email}` · [Anzeige]({j['url']})")
    elif url != j["url"]:
        lines.append(f"[Bewerben]({url}) · [Anzeige]({j['url']})")
    return {"title": discord.clip(j["title"], 250), "url": url, "color": 0x9B59B6, "description": "\n".join(lines),
            "footer": {"text": f"{_label(j['source'])} · {j['search_name']}"}}


def pending() -> list[dict]:
    since = discord.since(CHANNEL)
    with jdb.connect() as con:
        rows = con.execute(
            """SELECT j.*, m.fit, m.fazit, m.pros, m.gaps, s.name AS search_name
               FROM job_matches m
               JOIN jobs j ON j.source=m.source AND j.source_id=m.source_id
               JOIN job_searches s ON s.id=m.search_id
               WHERE m.passed=1 AND m.fit >= ? AND COALESCE(m.hidden, 0)=0 AND COALESCE(m.verdict, '') != 'down'
                 AND m.applied_at IS NULL AND s.enabled=1 AND COALESCE(j.status, 'active') = 'active'
                 AND j.first_seen_at >= ? AND m.matched_at - s.created_at > ?
               ORDER BY m.fit DESC, j.first_seen_at""", (MIN_FIT, since, SEED_GRACE)).fetchall()
    return [jdb.decode(r) for r in rows]


async def flush(client: httpx.AsyncClient) -> int:
    if not discord.hook(CHANNEL):
        return 0
    n = 0
    for j in pending():
        keys = [f"{j['source']}:{j['source_id']}", f"d:{j['dedupe']}" if j.get("dedupe") else ""]
        if discord.was_sent(CHANNEL, *keys):
            discord.mark_sent(CHANNEL, *keys)
            continue
        if n >= MAX_PER_FLUSH:
            break
        if not await discord.post(client, CHANNEL, {"embeds": [embed(j)]}):
            break
        discord.mark_sent(CHANNEL, *keys)
        n += 1
    if n:
        log.info("posted %d jobs to discord", n)
    return n
