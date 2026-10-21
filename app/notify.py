"""Push a deal: to Discord #deals when its webhook is set (then Telegram stays quiet), else Telegram.
Phase 1 has no score yet, so a card is title/price/where/when."""
from __future__ import annotations

import html
import logging
from datetime import datetime

import httpx

from . import discord
from .config import HTTP_TIMEOUT, TELEGRAM_CHAT_ID, TELEGRAM_TOKEN
from .filters import is_shipped

log = logging.getLogger("notify")
API = "https://api.telegram.org/bot{token}/{method}"


def _api(method: str) -> str:
    return API.format(token=TELEGRAM_TOKEN, method=method)


def telegram() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def configured() -> bool:
    return bool(discord.hook("deals")) or telegram()


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


def caption(row) -> str:
    e = html.escape
    where = " · ".join(x for x in [
        e(row["district"] or ""),
        f"{row['distance_km']:.0f} km" if row["distance_km"] is not None else "",
        # the derived meaning, not KA's flag: a seller 4 km away who also posts is Abholen
        "📦 Versand" if is_shipped(dict(row)) else "🚲 Abholen",
    ] if x)
    lines = [
        f"<b>{e(row['title'])}</b>",
        f"{_price(row)}   {where}",
    ]
    when = _when(row)
    if when:
        lines.append(f"<i>{when} · {e(row['search_name'])}</i>")
    return "\n".join(lines)


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
    if discord.hook("deals"):
        return await discord.post(client, "deals", {"embeds": [embed(row)]})
    text = caption(row)
    markup = {"inline_keyboard": [[
        {"text": "💬 Nachricht kopieren", "copy_text": {"text": INQUIRY}},
        {"text": "Anzeige öffnen ↗", "url": row["url"]},
    ]]}
    base = {"chat_id": TELEGRAM_CHAT_ID, "parse_mode": "HTML", "reply_markup": markup}
    try:
        if row["image_url"]:
            r = await client.post(_api("sendPhoto"), json={
                **base, "photo": row["image_url"], "caption": text}, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return True
            log.warning("sendPhoto failed (%s), falling back to text", r.text[:200])
        r = await client.post(_api("sendMessage"), json={
            **base, "text": f"{text}\n{row['url']}",
            "link_preview_options": {"is_disabled": True}}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log.error("telegram error: %s", r.text[:300])
        return r.status_code == 200
    except httpx.HTTPError as exc:
        log.error("telegram unreachable: %s", exc)
        return False


async def alert(client: httpx.AsyncClient, text: str) -> bool:
    """Operational alerts (parser broke, source unreachable): Discord #alerts, else the Telegram chat, plain."""
    if discord.hook("alerts"):
        return await discord.post(client, "alerts", {"content": f"⚠️ deals: {text}"})
    if not telegram():
        return False
    try:
        r = await client.post(_api("sendMessage"), json={
            "chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ {text}"}, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError:
        log.error("could not deliver alert: %s", text)
        return False
    if r.status_code != 200:
        log.error("telegram refused alert: %s", r.text[:300])
    return r.status_code == 200
