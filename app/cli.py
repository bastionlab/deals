"""deals <command> -- manage searches and check the plumbing."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

import httpx

from . import db, notify
from .config import HTTP_TIMEOUT, TELEGRAM_TOKEN
from .sources import kleinanzeigen as ka


def _csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def cmd_init(args):
    db.init()
    print(f"schema ready at {db.DB_PATH}")


def cmd_add(args):
    db.init()
    spec = {
        "keyword": args.keyword,
        "terms": _csv(args.terms) or [args.keyword],
        "exclude": _csv(args.exclude),
        "require": _csv(args.require),
        "min_price": args.min_price,
        "max_price": args.max_price,
        "radius_km": args.radius,
        "location_id": args.location,
        "shipping": args.shipping,
        "offers_only": True,
    }
    sid = db.add_search(args.name, args.keyword, spec)
    print(f"#{sid} {args.name}")
    print(f"  {ka.build_url(spec)}")


def cmd_ls(args):
    db.init()
    with db.connect() as con:
        rows = con.execute("""
            SELECT s.*, (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) AS hits
            FROM searches s ORDER BY s.id""").fetchall()
    if not rows:
        print("no searches yet -- add one with: deals add ...")
        return
    for r in rows:
        last = datetime.fromtimestamp(r["last_polled_at"]).strftime("%H:%M:%S") if r["last_polled_at"] else "never"
        flag = "" if r["enabled"] else "  [disabled]"
        print(f"#{r['id']:<3} {r['name']:<24} hits={r['hits']:<5} last={last}{flag}")
        if r["last_error"]:
            print(f"     ! {r['last_error']}")


def _set_enabled(name: str, value: int):
    db.init()
    with db.connect() as con:
        n = con.execute("UPDATE searches SET enabled=? WHERE name=?", (value, name)).rowcount
    print(f"{'enabled' if value else 'disabled'} {name}" if n else f"no search named {name!r}")


def cmd_enable(args):
    _set_enabled(args.name, 1)


def cmd_disable(args):
    _set_enabled(args.name, 0)


def cmd_rm(args):
    db.init()
    with db.connect() as con:
        sid = con.execute("SELECT id FROM searches WHERE name=?", (args.name,)).fetchone()
        if not sid:
            print(f"no search named {args.name!r}")
            return
        con.execute("DELETE FROM matches WHERE search_id=?", (sid["id"],))
        con.execute("DELETE FROM searches WHERE id=?", (sid["id"],))
    print(f"removed {args.name}")


def cmd_chatid(args):
    """Find the chat id: message the bot once, then run this."""
    r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=HTTP_TIMEOUT)
    data = r.json()
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("first_name") or chat.get("title") or chat.get("type")
    if not seen:
        print("no messages yet -- send your bot any message, then run this again")
        sys.exit(1)
    for cid, who in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({who})")


def cmd_testsend(args):
    async def go():
        async with httpx.AsyncClient() as client:
            await notify.alert(client, "Testnachricht vom deals-poller — Verbindung steht.")
    if not notify.configured():
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set in the environment")
        sys.exit(1)
    asyncio.run(go())
    print("sent")


def cmd_poll(args):
    """One pass over every enabled search, then exit."""
    from .poller import Backoff, HEADERS, flush, poll_search
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async def go():
        db.init()
        backoff = Backoff()
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
            for row in db.active_searches():
                await poll_search(client, row, backoff)
                if not args.quiet:
                    await flush(client)
                await asyncio.sleep(2)
    asyncio.run(go())


def cmd_recent(args):
    db.init()
    with db.connect() as con:
        rows = con.execute("""
            SELECT l.*, s.name AS search_name FROM matches m
            JOIN listings l ON l.source=m.source AND l.source_id=m.source_id
            JOIN searches s ON s.id=m.search_id
            ORDER BY COALESCE(l.posted_at, l.first_seen_at) DESC LIMIT ?""",
            (args.limit,)).fetchall()
    for r in rows:
        price = "—" if r["price_cents"] is None else f"{r['price_cents']//100} €"
        when = datetime.fromtimestamp(r["posted_at"]).strftime("%d.%m %H:%M") if r["posted_at"] else "?"
        km = f"{r['distance_km']:.0f}km" if r["distance_km"] is not None else "?"
        ship = "📦" if r["shipping"] else "  "
        print(f"{when}  {price:>8}  {km:>5} {ship}  {r['title'][:58]:<58} [{r['search_name']}]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deals")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    a = sub.add_parser("add", help="add a search")
    a.add_argument("name")
    a.add_argument("keyword", help="what to type into the Kleinanzeigen search box")
    a.add_argument("--terms", help="comma list; a listing must contain one of these")
    a.add_argument("--exclude", help="comma list; drop listings containing any of these")
    a.add_argument("--require", help="comma list; all of these must appear")
    a.add_argument("--min-price", type=int)
    a.add_argument("--max-price", type=int)
    a.add_argument("--radius", type=int, default=10)
    a.add_argument("--location", default="l3331", help="l3331 = Berlin")
    a.add_argument("--shipping", choices=["any", "ship", "pickup"], default="any")
    a.set_defaults(func=cmd_add)

    sub.add_parser("ls").set_defaults(func=cmd_ls)
    for name, fn in (("enable", cmd_enable), ("disable", cmd_disable), ("rm", cmd_rm)):
        s = sub.add_parser(name)
        s.add_argument("name")
        s.set_defaults(func=fn)

    sub.add_parser("chatid").set_defaults(func=cmd_chatid)
    sub.add_parser("testsend").set_defaults(func=cmd_testsend)

    po = sub.add_parser("poll", help="one pass, then exit")
    po.add_argument("--quiet", action="store_true", help="do not send Telegram messages")
    po.set_defaults(func=cmd_poll)

    r = sub.add_parser("recent")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_recent)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
