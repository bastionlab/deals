"""Push a deal to Discord #deals, operational alerts to #alerts (webhooks from the dashboard, /etc/discord.json).
Phase 1 has no score yet, so a card is title/price/where/when."""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from . import discord
from .filters import is_shipped

log = logging.getLogger("notify")


def configured() -> bool:
    return bool(discord.hook("deals"))


def _price(row) -> str:
    cents, kind = row["price_cents"], row["price_type"]
    if kind == "free":
        return "🎁 Zu verschenken"
    if cents is None:
        return "VB"
    euros = f"{cents // 100:,}".replace(",", ".")
    return f"{euros} €" + (" VB" if kind == "negotiable" else "")


def _when(row) -> str:
    ts = row["posted_at"]
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("heute %H:%M") if dt.date() == datetime.now().date() else dt.strftime("%d.%m. %H:%M")


# the first message to a seller; the dashboard's DealCard copies the same text
INQUIRY = "Hallo, ist es noch verfügbar?"


def _where(row) -> str:
    return " · ".join(x for x in [
        row["district"] or "",
        f"{row['distance_km']:.0f} km" if row["distance_km"] is not None else "",
        "📦 Versand" if is_shipped(dict(row)) else "🚲 Abholen",
    ] if x)


def embed(row) -> dict:
    when = _when(row)
    e = {"title": discord.clip(row["title"], 250), "url": row["url"], "color": 0x2ECC71,
         "description": f"**{_price(row)}**   {_where(row)}\n```\n{INQUIRY}\n```",
         "footer": {"text": " · ".join(x for x in (when, row["search_name"]) if x)}}
    if row["image_url"]:
        e["image"] = {"url": row["image_url"]}
    return e


async def send(client: httpx.AsyncClient, row) -> bool:
    return await discord.post(client, "deals", {"embeds": [embed(row)]})


async def alert(client: httpx.AsyncClient, text: str) -> bool:
    """Operational alerts (parser broke, source unreachable)."""
    return await discord.post(client, "alerts", {"content": f"⚠️ deals: {text}"})
