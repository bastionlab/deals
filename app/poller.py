"""The polling loop.

One global rate limiter, not one per search: how hard we hit Kleinanzeigen
must not depend on how many searches you happen to have saved. Searches are
served round-robin (oldest last_polled_at first), one request per keyword, so
with K keywords across all searches each URL refreshes every K * FETCH_MIN_INTERVAL
seconds.

After each search, one listing already in the feed is re-opened to see whether
it has sold (see `recheck`). Those requests share the same limiter, so they slow
the search refresh a little rather than adding a second stream of traffic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import httpx

from . import ai, db, filters, notify
from .config import (BROWSER_HEADERS, EMPTY_PARSE_ALERT_AFTER, FETCH_JITTER,
                     FETCH_MIN_INTERVAL, HTTP_TIMEOUT)
from .sources import kleinanzeigen as ka

log = logging.getLogger("poller")

HEADERS = BROWSER_HEADERS
MAX_NOTIFY_PER_FLUSH = 8
# An ad counts as new if it was posted within this window. Comparing ids against
# max_seen_id alone silently swallowed bumped ads and ads that only one of a
# search's keywords finds; record_match already guarantees each ad fires once.
FRESH_SECONDS = 24 * 3600
# Detail-page rechecks per search served. db.RECHECK_AFTER decides which are due;
# usually nothing is, and then this costs no request at all.
RECHECKS_PER_SEARCH = 1


class RateLimiter:
    """Global spacing with jitter, so the request pattern isn't metronomic."""

    def __init__(self, interval: float, jitter: float):
        self.interval = interval
        self.jitter = jitter
        self._next = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        if now < self._next:
            await asyncio.sleep(self._next - now)
        spacing = self.interval * (1 + random.uniform(-self.jitter, self.jitter))
        self._next = time.monotonic() + spacing


class Backoff:
    """Shared across searches: a block is a property of the source, not a query."""

    def __init__(self) -> None:
        self.level = 0
        self.alerted = False

    def penalty(self) -> float:
        return 0.0 if self.level == 0 else min(300.0, 15.0 * 2 ** (self.level - 1))

    def hit(self) -> None:
        self.level = min(self.level + 1, 5)

    def ok(self) -> None:
        self.level = 0
        self.alerted = False


def _is_fresh(ad: dict, max_seen_id: int, now: float) -> bool:
    if ad.get("posted_at"):
        return ad["posted_at"] >= now - FRESH_SECONDS
    return int(ad["source_id"]) > max_seen_id


async def poll_search(client: httpx.AsyncClient, row, backoff: Backoff,
                      keyword: str | None = None) -> int:
    """Fetch page 1 of one search keyword. Returns the number of listings queued for push."""
    spec = json.loads(row["spec"] or "{}")
    url = ka.build_url(spec, page=1, keyword=keyword)
    seeding = row["max_seen_id"] == 0
    now = time.time()

    try:
        resp = await client.get(url, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        db.update_search_state(row["id"], error=f"fetch: {exc}")
        backoff.hit()
        log.warning("[%s] fetch failed: %s", row["name"], exc)
        return 0

    if resp.status_code != 200:
        db.update_search_state(row["id"], error=f"http {resp.status_code}")
        backoff.hit()
        log.warning("[%s] http %s", row["name"], resp.status_code)
        if resp.status_code in (403, 429) and not backoff.alerted:
            backoff.alerted = True
            await notify.alert(client, f"Kleinanzeigen antwortet mit HTTP {resp.status_code} "
                                       f"— Polling pausiert kurz.")
        return 0

    backoff.ok()
    ads = ka.parse(resp.text)

    if not ads:
        # 200 OK with zero ads is the signature of a markup change, and it is
        # exactly the failure that would otherwise go unnoticed for weeks.
        streak = db.update_search_state(row["id"], error="parsed 0 ads", empty=True)
        log.error("[%s] parsed 0 ads (streak %d)", row["name"], streak)
        if streak == EMPTY_PARSE_ALERT_AFTER:
            await notify.alert(client, f"Parser liefert 0 Treffer für „{row['name']}\" "
                                       f"({streak}x in Folge) — Markup hat sich vermutlich geändert.")
        return 0

    max_id = row["max_seen_id"]
    candidates: list[tuple[dict, bool]] = []       # (listing, is_new_since_last_poll)
    for ad in ads:
        ad_id = int(ad["source_id"])
        max_id = max(max_id, ad_id)
        db.upsert_listing(ad)                      # keep everything: phase 4 prices itself off this
        keep, reason = filters.check(ad, spec)
        if not keep:
            log.debug("[%s] drop %s (%s)", row["name"], ad_id, reason)
            continue
        if not db.record_match(row["id"], ad["source"], ad["source_id"]):
            continue                               # already known for this search
        candidates.append((ad, not seeding and _is_fresh(ad, row["max_seen_id"], now)))

    # The model only ever sees what survived the free filters, and only the
    # handful that are new -- usually zero, so most polls never call it at all.
    intent = (spec.get("intent") or "").strip()
    min_score = int(spec.get("min_score") or ai.DEFAULT_MIN_SCORE)
    verdicts = await ai.judge(client, intent, [a for a, _ in candidates]) if candidates else {}

    queued = rejected = 0
    for ad, is_new in candidates:
        v = verdicts.get(ad["source_id"])
        ok = ai.passes(v, min_score)
        if intent:
            db.set_verdict(row["id"], ad["source"], ad["source_id"], v, ok)
        if not ok:
            db.mark_notified(row["id"], ad["source"], ad["source_id"])   # filtered, stay quiet
            rejected += 1
        elif is_new:
            queued += 1
        else:
            db.mark_notified(row["id"], ad["source"], ad["source_id"])   # backfill, stay quiet

    db.update_search_state(row["id"], max_seen_id=max_id)
    log.info("[%s|%s] %d ads, %d new to push, %d filtered by AI%s", row["name"], keyword or "",
             len(ads), queued, rejected, " (seeded)" if seeding else "")
    return queued


async def recheck(client: httpx.AsyncClient, limiter: RateLimiter, backoff: Backoff,
                  limit: int = RECHECKS_PER_SEARCH) -> None:
    """Re-open listings the feed still shows and record whether they sold.

    Plenty of good offers are gone within hours yet stayed in the feed forever,
    because the search page simply stops listing them and nothing told us.
    """
    for row in db.due_rechecks(limit):
        await limiter.wait()
        if (pen := backoff.penalty()):
            await asyncio.sleep(pen)
        url = row["url"]
        try:
            resp = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=False)
            loc = resp.headers.get("location")
            if resp.is_redirect and loc and row["source_id"] in loc:   # slug changed, same ad
                resp = await client.get(resp.next_request.url, timeout=HTTP_TIMEOUT,
                                        follow_redirects=False)
                loc = resp.headers.get("location")
        except httpx.HTTPError as exc:
            log.warning("recheck %s failed: %s", row["source_id"], exc)
            return
        if resp.status_code in (403, 429):
            backoff.hit()
            return
        status = ka.ad_status(row["source_id"], resp.status_code, loc, resp.text)
        prev = db.set_status(row["source"], row["source_id"], status)
        if status is None:
            log.warning("recheck %s: unreadable page (http %s)", row["source_id"], resp.status_code)
        elif status != (prev or "active"):
            log.info("recheck %s: %s -> %s | %s", row["source_id"], prev or "active", status,
                     row["title"][:60])


async def flush(client: httpx.AsyncClient) -> None:
    if not notify.configured():
        return
    for row in db.pending_notifications(MAX_NOTIFY_PER_FLUSH):
        if await notify.send(client, row):
            db.mark_notified(row["search_id"], row["source"], row["source_id"])
            await asyncio.sleep(0.4)
        else:
            break          # Telegram is unhappy; leave the rest queued for next pass


async def run() -> None:
    db.init()
    limiter = RateLimiter(FETCH_MIN_INTERVAL, FETCH_JITTER)
    backoff = Backoff()
    log.info("poller up (interval %.1fs)", FETCH_MIN_INTERVAL)
    if not notify.configured():
        log.warning("Telegram not configured (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID empty) -- "
                    "nothing will be pushed. Message the bot, run `deals chatid`, put the id in /etc/deals/deals.env.")

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        while True:
            searches = db.active_searches()
            if not searches:
                await asyncio.sleep(30)
                continue
            for row in searches:
                for kw in ka.keywords(json.loads(row["spec"] or "{}")):
                    await limiter.wait()
                    if (pen := backoff.penalty()):
                        log.info("backing off %.0fs", pen)
                        await asyncio.sleep(pen)
                    try:
                        await poll_search(client, row, backoff, kw)
                    except Exception:
                        log.exception("[%s] unexpected error", row["name"])
                    await flush(client)
                try:
                    await recheck(client, limiter, backoff)
                except Exception:
                    log.exception("recheck failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram's carry the bot token
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
