"""The flat hunter's loop. Runs as a task inside the deals poller process.

Per cycle:
1. inberlinwohnen: newest pages until one holds nothing new (one site-wide
   list, matched against every search locally). Every SWEEP_EVERY a full walk
   tells us which flats were taken down, since the portal has no per-flat
   "gone" state we could ask for.
2. Kleinanzeigen, per search: page 1 (the first poll of a new search walks
   SEED_PAGES back so the feed starts with what is online now). Cards that
   could fit any search get one detail fetch and one model reading, ever.
3. One due recheck of a KA flat in the feed (sold/deleted -> gone).

Every Kleinanzeigen request goes through the deals limiter and backoff.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..config import HTTP_TIMEOUT
from . import db as fdb
from . import extract, geo, ibw, ka, match

log = logging.getLogger("flats")

CYCLE = 150                 # s between cycle starts
SEED_PAGES = 3
MAX_DETAILS_PER_POLL = 40
IBW_INTERVAL = 6.0          # their own limiter: a different site
IBW_MAX_PAGES = 60
SWEEP_EVERY = 6 * 3600
BERLIN = (52.52, 13.405)
BERLIN_REACH_KM = 25


def touches_berlin(spec: dict) -> bool:
    if spec.get("lat") is None:
        return False
    return geo.km_between(spec["lat"], spec["lon"], *BERLIN) - geo.reach_km(spec) < BERLIN_REACH_KM


def match_everywhere(flat: dict, searches: list[dict]) -> int:
    """Re-evaluate one flat against every search. Returns how many it fits (ok or soft)."""
    n = 0
    for s in searches:
        res = match.check(flat, s["spec"])
        fdb.set_match(s["id"], flat["source"], flat["source_id"], res)
        n += res[0] != "hard"
    return n


def rematch(search: dict) -> dict:
    """Apply one search to everything stored -- after add or edit. Never fetches."""
    added = removed = 0
    with fdb.connect() as con:
        before = {(r[0], r[1]) for r in con.execute(
            "SELECT source, source_id FROM flat_matches WHERE search_id=?", (search["id"],))}
    for f in fdb.all_flats():
        if f["source"] == ka.SOURCE and not f.get("detail_at"):
            continue                          # card only: never read, so nothing to show yet
        res = match.check(f, search["spec"])
        fdb.set_match(search["id"], f["source"], f["source_id"], res)
        key = (f["source"], f["source_id"])
        if res[0] != "hard" and key not in before:
            added += 1
        elif res[0] == "hard" and key in before:
            removed += 1
    return {"added": added, "removed": removed}


# --- inberlinwohnen ----------------------------------------------------------

async def poll_ibw(client: httpx.AsyncClient, limiter, searches: list[dict], state: dict) -> None:
    known = fdb.known_ids(ibw.SOURCE)
    sweep = not known or time.time() - state.get("ibw_sweep", 0) > SWEEP_EVERY
    seen: set[str] = set()
    new: set[str] = set()          # pages overlap by an item, so count ids, not rows
    total = None
    for page in range(1, IBW_MAX_PAGES + 1):
        await limiter.wait()
        try:
            r = await client.get(ibw.page_url(page), timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            log.warning("ibw page %d: %s", page, exc)
            return
        if r.status_code != 200:
            log.warning("ibw page %d: http %s", page, r.status_code)
            return
        flats, total = ibw.parse(r.text)
        if not flats:
            if page == 1:
                log.error("ibw: parsed 0 flats on page 1 -- markup changed?")
                return
            break
        fresh = [f for f in flats if f["source_id"] not in known]
        for f in flats:
            seen.add(f["source_id"])
            fdb.upsert(f)
            if f["source_id"] not in known:
                match_everywhere(f, searches)
        new.update(f["source_id"] for f in fresh)
        if not sweep and not fresh:
            break                               # newest first: nothing new on this page, done
        if total and len(seen) >= total:
            break
    if sweep and total and len(seen) >= total * 0.9:
        # only trust a sweep that actually saw (nearly) everything the finder claims to hold
        gone = fdb.mark_gone_except(ibw.SOURCE, seen)
        state["ibw_sweep"] = time.time()
        log.info("ibw sweep: %d listed, %d new, %d gone", len(seen), len(new), gone)
    elif new:
        log.info("ibw: %d new", len(new))


# --- Kleinanzeigen -----------------------------------------------------------

async def _get(client, limiter, backoff, url, **kw) -> httpx.Response | None:
    await limiter.wait()
    if (pen := backoff.penalty()):
        await asyncio.sleep(pen)
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT, **kw)
    except httpx.HTTPError as exc:
        log.warning("fetch %s: %s", url, exc)
        backoff.hit()
        return None
    if r.status_code in (403, 429):
        backoff.hit()
        log.warning("KA http %s on %s", r.status_code, url)
        return None
    return r


async def read_flat(client, limiter, backoff, card: dict) -> dict | None:
    r = await _get(client, limiter, backoff, card["url"])
    if r is None or r.status_code != 200:
        return None
    backoff.ok()
    detail = ka.parse_detail(r.text)
    if not detail:
        log.warning("detail %s: unreadable page", card["source_id"])
        return None
    merged = {**card, **{k: v for k, v in detail.items() if v is not None}}
    reading = await extract.read_ad(client, merged)
    return extract.enrich(card, detail, reading, time.time())


async def poll_ka(client, limiter, backoff, search: dict, searches: list[dict]) -> None:
    spec = search["spec"]
    pages = 1 if search["seeded"] else SEED_PAGES
    details = cards_n = fits = 0
    for page in range(1, pages + 1):
        r = await _get(client, limiter, backoff, ka.build_url(spec, page))
        if r is None or r.status_code != 200:
            fdb.search_state(search["id"], error=f"http {r.status_code if r is not None else 'error'}")
            return
        backoff.ok()
        cards = ka.parse_list(r.text)
        if not cards:
            if page == 1:
                fdb.search_state(search["id"], error="parsed 0 ads")
                log.error("[%s] KA parsed 0 flats", search["name"])
                return
            break
        cards_n += len(cards)
        for card in cards:
            have = fdb.get(card["source"], card["source_id"])
            if have and have.get("detail_at"):
                fdb.upsert({k: v for k, v in card.items() if k != "card_description"})
                continue
            if not any(match.card_could_match(card, s["spec"]) for s in searches):
                if not have:
                    fdb.upsert({k: v for k, v in card.items() if k != "card_description"})
                continue
            if details >= MAX_DETAILS_PER_POLL:
                continue                        # next poll picks it up
            details += 1
            flat = await read_flat(client, limiter, backoff, card)
            if not flat:
                continue
            fdb.upsert(flat)
            fits += match_everywhere(flat, searches) > 0
    fdb.search_state(search["id"], error=None, seeded=True)
    log.info("[%s] KA %d cards, %d read, %d fit a search%s", search["name"], cards_n, details, fits,
             "" if search["seeded"] else " (seeded)")


async def recheck_ka(client, limiter, backoff) -> None:
    for row in fdb.due_rechecks(ka.SOURCE, 1):
        r = await _get(client, limiter, backoff, row["url"], follow_redirects=False)
        if r is None:
            return
        loc = r.headers.get("location")
        if r.is_redirect and loc and row["source_id"] in loc:
            r = await _get(client, limiter, backoff, str(r.next_request.url), follow_redirects=False)
            if r is None:
                return
            loc = r.headers.get("location")
        status = ka.ad_status(row["source_id"], r.status_code, loc, r.text)
        fdb.set_status(row["source"], row["source_id"], status)
        if status and status != (row["status"] or "active"):
            log.info("recheck %s: %s -> %s | %s", row["source_id"], row["status"] or "active", status,
                     row["title"][:60])


async def run(client: httpx.AsyncClient, ka_limiter, backoff) -> None:
    fdb.init()
    # same limiter class as the deals poller, separate instance: another site, another budget
    ibw_limiter = type(ka_limiter)(IBW_INTERVAL, 0.4)
    state: dict = {}
    log.info("flat hunter up")
    while True:
        started = time.monotonic()
        try:
            searches = fdb.searches(enabled_only=True)
            if searches:
                if any(touches_berlin(s["spec"]) for s in searches):
                    await poll_ibw(client, ibw_limiter, searches, state)
                for s in searches:
                    try:
                        await poll_ka(client, ka_limiter, backoff, s, searches)
                    except Exception:
                        log.exception("[%s] flat poll failed", s["name"])
                    await recheck_ka(client, ka_limiter, backoff)
        except Exception:
            log.exception("flat cycle failed")
        await asyncio.sleep(max(10.0, CYCLE - (time.monotonic() - started)))
