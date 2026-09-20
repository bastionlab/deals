# deals — Kleinanzeigen/eBay steal finder

Finds underpriced listings in Berlin (or shippable) and pushes them to Telegram
before anyone else notices. Runs in **CT 107 `deals`** (192.168.1.57).

## Layout

| path | what |
|---|---|
| `app/sources/kleinanzeigen.py` | URL builder + result-page parser |
| `app/filters.py` | the free half of matching (keywords, wanted-ads, distance) |
| `app/ai.py` | relevance judging with granite4.2:8b + the numeric guard |
| `app/poller.py` | the loop: global rate limiter, dedupe, parse-break alarm |
| `app/notify.py` | Telegram cards |
| `app/cli.py` | `deals` command |
| `/etc/deals/deals.env` | secrets, 0600, **not in git** |
| `/var/lib/deals/deals.db` | SQLite (WAL) |

Deploy after changes: `./deploy.sh` (syncs into CT 107, reinstalls the unit, restarts).

## Commands (inside CT 107)

```sh
deals add <name> "<search words>" --terms a,b --exclude x,y --min-price 25 --max-price 250 --radius 10
deals ls            # searches, hit counts, last poll, last error
deals recent        # what matched lately
deals poll --quiet  # one pass, no Telegram
deals chatid        # after messaging the bot once
deals testsend      # prove the Telegram path works
deals disable <name> / enable <name> / rm <name>
```

## How the scraping stays polite

One **global** rate limiter (`DEALS_FETCH_INTERVAL`, default 8 s, ±40 % jitter)
shared by every search, so request volume does not grow with the number of
searches. Searches are polled round-robin, oldest-first: with N searches each
refreshes every N × 8 s. HTTP 403/429 triggers a shared exponential backoff and
one Telegram alert.

## Selector policy

Kleinanzeigen serves Tailwind utility classes that change without warning.
The parser anchors **only** on `data-adid` / `data-href` on `<article>` and the
per-card `ld+json`; price, location, distance and time come from text-shaped
regexes. **Never select on a class name.**

A fetch that returns HTTP 200 with zero parsed ads is treated as a markup
change, not as "no results": after 3 in a row the bot alerts you. This is the
failure that otherwise goes unnoticed for weeks.

## Kleinanzeigen URL grammar (verified 2026-09-20)

```
/s-berlin/anzeige:angebote/preis:25:250/versand:ja/<slug>/k0l3331r10?sortingField=SORTING_DATE
/s-seite:2/berlin/<filters>/<slug>/k0l3331r10?sortingField=SORTING_DATE
```
`l3331` = Berlin, `r10` = 10 km. Location, radius, price band, offers-only and
shipping all filter server-side, so they cost no AI. Ad ids increase with
posting time, which is what makes "new since last poll" a single cheap request.

## The AI relevance filter

Each search can carry an `intent`: one or two sentences saying what actually
counts. Only listings that survived the free filters are shown to the model, and
only the ones new since the last poll, so most polls never call it at all.

**The model is not trusted with numbers.** In a production run it kept a 4 TB
drive for a "mindestens 6 TB" search with score 100, and kept a 512 GB SSD whose
own reason read *"1 TB minimum nicht erreicht"*. So any size or inch bound in the
intent (`mindestens 6 TB`, `maximal 27 Zoll`, `ab 1 TB`) is enforced in code
against the listing title, and that verdict overrides the model's.

The guard uses the **largest** matching number in the title and does nothing when
the title states no number — both choices fail open, because a missed bargain
costs more than one extra card in the feed. Judging as a whole fails open too: if
Ollama is down the listing goes through unjudged.

    deals judge --all -v        # re-run over everything, showing each verdict

## Status

- **Phase 0/1 — done.** Kleinanzeigen source, scheduler, dedupe, hard filters, Telegram.
- **Phase 3 — done.** Relevance judging (`app/ai.py`), intents, `/deals` shows what
  was filtered and why.
- Phase 2 — eBay Browse API (`EBAY_DE`). `ebay.de` HTML is 403, the API is not.
  Cache the OAuth app token: the token endpoint allows only 1000 calls/day.
- Phase 4 — price stats, backfill sweep on save, deal score (non-monotonic:
  >75 % off is a scam signal, not a steal).
- Phase 5 — `/deals` page in the dashboard, 👍/👎 feedback.
