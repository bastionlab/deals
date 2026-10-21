"""Discord webhooks, one per channel. The dashboard hub writes /etc/discord.json:
{"deals": url, "wohnungen": url, "jobs": url, "money": url, "alerts": url}; a missing key = that channel is off.

Each channel only ever gets what was first seen after its hook was set, so linking a channel never floods it
with the backlog. `sent` remembers what went out, per channel and dedupe key."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx

from .db import connect

log = logging.getLogger("discord")

HOOKS = Path(os.environ.get("DISCORD_HOOKS", "/etc/discord.json"))
SPACING = 0.5               # webhooks allow 5 per 2 s; stay under it across all channels
_cache: tuple[float, dict] = (0.0, {})
_lock = asyncio.Lock()
_last = 0.0


def hooks() -> dict:
    global _cache
    try:
        m = HOOKS.stat().st_mtime
    except OSError:
        return {}
    if m != _cache[0]:
        try:
            _cache = (m, {k: v for k, v in json.loads(HOOKS.read_text()).items() if v})
        except (OSError, ValueError):
            _cache = (m, {})
    return _cache[1]


def hook(channel: str) -> str | None:
    return hooks().get(channel)


def init() -> None:
    with connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS discord_sent (channel TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL,
                                                     PRIMARY KEY (channel, key));
            CREATE TABLE IF NOT EXISTS discord_since (channel TEXT PRIMARY KEY, at REAL NOT NULL);""")


def since(channel: str) -> float:
    """When this channel was first seen configured; nothing older than that is ever posted."""
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO discord_since VALUES (?,?)", (channel, time.time()))
        return con.execute("SELECT at FROM discord_since WHERE channel=?", (channel,)).fetchone()[0]


def was_sent(channel: str, *keys: str) -> bool:
    keys = [k for k in keys if k]
    if not keys:
        return False
    with connect() as con:
        return con.execute(f"SELECT 1 FROM discord_sent WHERE channel=? AND key IN ({','.join('?' * len(keys))})",
                           (channel, *keys)).fetchone() is not None


def mark_sent(channel: str, *keys: str) -> None:
    with connect() as con:
        con.executemany("INSERT OR IGNORE INTO discord_sent VALUES (?,?,?)",
                        [(channel, k, time.time()) for k in keys if k])


async def post(client: httpx.AsyncClient, channel: str, payload: dict) -> bool:
    """One message to a channel's webhook. False when the channel is off or Discord refused it."""
    global _last
    url = hook(channel)
    if not url:
        return False
    body = {"allowed_mentions": {"parse": []}} | payload
    async with _lock:
        for _ in range(3):
            wait = SPACING - (time.monotonic() - _last)
            if wait > 0:
                await asyncio.sleep(wait)
            _last = time.monotonic()
            try:
                r = await client.post(url, json=body, timeout=15)
            except httpx.HTTPError as e:
                log.warning("discord %s: %s", channel, e)
                return False
            if r.status_code == 429:
                try:
                    retry = float(r.json().get("retry_after", 1))
                except ValueError:
                    retry = 1.0
                await asyncio.sleep(min(retry, 30))
                continue
            if r.status_code >= 300:
                log.warning("discord %s refused: %s %s", channel, r.status_code, r.text[:200])
                return False
            return True
    return False


def clip(s: str | None, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
