"""Telegram push. Phase 1 has no score yet, so a card is title/price/where/when."""
from __future__ import annotations

import html
import logging
from datetime import datetime

import httpx

from .config import HTTP_TIMEOUT, TELEGRAM_CHAT_ID, TELEGRAM_TOKEN

log = logging.getLogger("notify")
API = "https://api.telegram.org/bot{token}/{method}"


def _api(method: str) -> str:
    return API.format(token=TELEGRAM_TOKEN, method=method)


def configured() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


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
        "📦 Versand" if row["shipping"] else "",
    ] if x)
    lines = [
        f"<b>{e(row['title'])}</b>",
        f"{_price(row)}   {where}",
    ]
    when = _when(row)
    if when:
        lines.append(f"<i>{when} · {e(row['search_name'])}</i>")
    return "\n".join(lines)


async def send(client: httpx.AsyncClient, row) -> bool:
    text = caption(row)
    markup = {"inline_keyboard": [[{"text": "Anzeige öffnen ↗", "url": row["url"]}]]}
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


async def alert(client: httpx.AsyncClient, text: str) -> None:
    """Operational alerts (parser broke, source unreachable) -- same chat, plain."""
    try:
        await client.post(_api("sendMessage"), json={
            "chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ {text}"}, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError:
        log.error("could not deliver alert: %s", text)
