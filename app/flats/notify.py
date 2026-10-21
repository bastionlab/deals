"""New flats that fit a search -> Discord #wohnungen.

Only what passed a search (in area, in budget, no scam/temporary/Gesuch, see match.check) with a known warm
rent, first seen after the channel was linked, and not part of a new search's first sweep. The same flat on
two portals or matched by two searches goes out once (title + rooms + m², as on the dashboard)."""
from __future__ import annotations

import logging

import httpx

from .. import discord
from . import db as fdb
from .dedupe import AREA_SLACK, group, pick, title_key

log = logging.getLogger("flats.discord")

CHANNEL = "wohnungen"
MAX_PER_FLUSH = 8
SEED_GRACE = 3600           # a new search's first hour is its backfill, not news
SOURCES = {"inberlinwohnen": "inberlinwohnen", "immoscout24": "ImmoScout24", "kleinanzeigen": "Kleinanzeigen"}
HOW = {"stated": "warm", "sum": "warm", "estimated": "warm (geschätzt)"}


def _fold_keys(f: dict, near: bool) -> list[str]:
    t = title_key(f.get("title"))
    if not t:
        return []
    b = int((f.get("area") or 0) // AREA_SLACK)
    return [f"t:{t}|{f.get('rooms')}|{x}" for x in ((b - 1, b, b + 1) if near else (b,))]


def _eur(v: float) -> str:
    return f"{v:,.0f} €".replace(",", ".")


def embed(f: dict, search: str) -> dict:
    facts = [f"**{_eur(f['warm'])} {HOW.get(f.get('warm_kind'), 'warm')}**",
             f"{f['rooms']:g} Zi." if f.get("rooms") else "", f"{f['area']:g} m²" if f.get("area") else "",
             f.get("district") or "", f"{f['distance_km']:.1f} km" if f.get("distance_km") is not None else ""]
    lines = [" · ".join(x for x in facts if x)]
    extra = [f"ab {f['available_from']}" if f.get("available_from") else "", "WBS nötig" if f.get("wbs") == 1 else ""]
    if any(extra):
        lines.append(" · ".join(x for x in extra if x))
    if f.get("ai_note"):
        lines.append(f"*{discord.clip(f['ai_note'], 200)}*")
    msg = fdb.message(f["source"], f["source_id"])
    if msg and msg.get("text"):
        lines.append("💬 Nachricht:\n```\n" + discord.clip(msg["text"].replace("```", "'''"), 1800) + "\n```")
    e = {"title": discord.clip(f["title"], 250), "url": f["url"], "color": 0x3498DB,
         "description": "\n".join(lines),
         "footer": {"text": f"{SOURCES.get(f['source'], f['source'])} · {search}"}}
    if f.get("image_url"):
        e["image"] = {"url": f["image_url"]}
    return e


def pending() -> list[dict]:
    since = discord.since(CHANNEL)
    with fdb.connect() as con:
        rows = con.execute(
            """SELECT f.*, m.distance_km, m.search_id, s.name AS search_name
               FROM flat_matches m
               JOIN flats f ON f.source=m.source AND f.source_id=m.source_id
               JOIN flat_searches s ON s.id=m.search_id
               WHERE m.passed=1 AND COALESCE(m.verdict, '') != 'down' AND s.enabled=1
                 AND COALESCE(f.status, 'active') = 'active' AND f.warm IS NOT NULL
                 AND f.first_seen_at >= ? AND m.matched_at - s.created_at > ?
               ORDER BY f.first_seen_at""", (since, SEED_GRACE)).fetchall()
    return [fdb.decode(r) for r in rows]


async def flush(client: httpx.AsyncClient) -> int:
    if not discord.hook(CHANNEL):
        return 0
    n = 0
    for g in group(pending()):
        ids = [f"{r['source']}:{r['source_id']}" for r in g]
        best = pick(g)
        if discord.was_sent(CHANNEL, *ids, *_fold_keys(best, near=True)):
            discord.mark_sent(CHANNEL, *ids)   # a new copy of a flat that already went out
            continue
        if n >= MAX_PER_FLUSH:
            break
        if not await discord.post(client, CHANNEL, {"embeds": [embed(best, best["search_name"])]}):
            break
        discord.mark_sent(CHANNEL, *ids, *_fold_keys(best, near=False))
        n += 1
    if n:
        log.info("posted %d flats to discord", n)
    return n
