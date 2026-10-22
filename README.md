# deals — Kleinanzeigen/eBay steal finder

Finds underpriced listings in Berlin (or shippable) and pushes them to Discord #deals
before anyone else notices. Runs in **CT 107 `deals`** (192.168.1.57).

## Layout

| path | what |
|---|---|
| `app/sources/kleinanzeigen.py` | URL builder + result-page parser |
| `app/filters.py` | the free half of matching (keywords, wanted-ads, distance) |
| `app/ai.py` | relevance judging with qwen3.5:9b + the numeric guard |
| `app/poller.py` | the loop: global rate limiter, dedupe, parse-break alarm |
| `app/notify.py` | Discord embeds (#deals) + #alerts |
| `app/cli.py` | `deals` command |
| `app/flats/` | flat hunter (see below): `ka.py`, `ibw.py` sources, `extract.py` model reading + rent, `match.py` rules, `poller.py` loop, `api.py` `/flats/*` |
| `/etc/deals/deals.env` | secrets, 0600, **not in git** |
| `/var/lib/deals/deals.db` | SQLite (WAL) |

Deploy after changes: `./deploy.sh` (syncs into CT 107, reinstalls the unit, restarts).

## Commands (inside CT 107)

```sh
deals add <name> "<search words>" --terms a,b --exclude x,y --min-price 25 --max-price 250 --radius 10
deals ls            # searches, hit counts, last poll, last error
deals recent        # what matched lately
deals poll --quiet  # one pass, no Discord
deals chatid        # after messaging the bot once
deals testsend      # prove the Discord #alerts path works
deals disable <name> / enable <name> / rm <name>
```

## How the scraping stays polite

One **global** rate limiter (`DEALS_FETCH_INTERVAL`, default 8 s, ±40 % jitter)
shared by every search, so request volume does not grow with the number of
searches. Searches are polled round-robin, oldest-first: with N searches each
refreshes every N × 8 s. HTTP 403/429 triggers a shared exponential backoff and
one Discord #alerts message.

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

## Abholen vs Versand

Kleinanzeigen's "Versand möglich" only says the seller is *willing* to post, and
plenty of sellers two streets away tick it. A listing therefore counts as
**Versand** only when it is further than `DEALS_NEARBY_KM` (default 15) — closer
than that you would simply collect it, whatever the seller ticked. An unknown
distance counts as far, since we cannot claim it is around the corner.

The raw flag stays in `listings.shipping` as Kleinanzeigen reported it; the
derived meaning lives in `filters.is_shipped()` and its SQL twin `SHIPPED_SQL`
in `api.py` — **change both together**.

A `pickup` search deliberately does *not* send `versand:nein`: that would drop
nearby sellers who also offer posting, and those are exactly the ones worth
collecting. It fetches the radius and applies the distance rule locally.

## Composing a search (the ✦ box)

`app/compose.py`. A single prompt writes filters that look right and match the
wrong things — asked for "RTX 3060 oder 3090" it produced the terms
rtx/geforce/nvidia, which happily match a 5070. The model cannot see that
without seeing results.

So the loop is: **draft → run the search for real → show the model its own hits
next to the original wish → correct → re-run**, up to `MAX_ROUNDS`. Streams
NDJSON, because it takes tens of seconds and watching it is most of the
reassurance.

Three things keep it honest:

- **Best round wins, not the last.** The critic can tighten a working filter
  into one that returns almost nothing (13 ads/2 hits → 3 ads/1 hit in testing).
  `_best()` picks the round with the most relevant hits, ties going to the
  tighter search, and the UI says when it reverted.
- **`tidy_keyword()`** strips conjunctions and caps length. "rtx 3060 oder 3090"
  as a search term returns zero ads, because the box matches literally.
- **Zero results force a correction in code**, since the critic will otherwise
  declare a filter broken and hand back the same one unchanged.

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

**Write the intent from facts, not guesses.** A search for a Fitbit Air was
composed with "Smartwatch mit Uhrgehäuse und Display"; the Fitbit Air has no
display, so the model threw out nearly every real device as "nur Armband". A
named model also covers its variants (`3090` matches `3090 Ti`) unless the
intent says otherwise; the prompt states that, because the model had been
rejecting Ti cards as "falsches Modell".

## Sold ads drop out of the feed

The search page simply stops listing a sold ad, so nothing used to tell us. After
each search the poller re-opens one feed listing that is due (`db.due_rechecks`:
every 20 min for the first day, 2 h for the first week, then 12 h) through the
same global rate limiter. `sources.kleinanzeigen.ad_status` reads the answer:

- redirect away from the ad, 404/410 → `gone`
- new layout: `<h1><span>Reserviert •</span>…` (or Gelöscht / Verkauft)
- old layout, served on the same URL by A/B test: `showPausedVeil: true` →
  reserved, `showDeletedVeil: true` → gone
- anything else unreadable → `None`, only `checked_at` is stamped. A listing is
  never hidden on a page we could not read.

`gone` is excluded in `_conditions()` (feed, tab counts) and from Discord;
`reserved` stays visible with a badge.

## Status

- **Phase 0/1 — done.** Kleinanzeigen source, scheduler, dedupe, hard filters, Telegram (since 2026-10-22 Discord).
- **Phase 3 — done.** Relevance judging (`app/ai.py`), intents, `/deals` shows what
  was filtered and why.
- Phase 2 — eBay Browse API (`EBAY_DE`). `ebay.de` HTML is 403, the API is not.
  Cache the OAuth app token: the token endpoint allows only 1000 calls/day.
- Phase 4 — price stats, backfill sweep on save, deal score (non-monotonic:
  >75 % off is a scam signal, not a steal).
- Phase 5 — `/deals` page in the dashboard, 👍/👎 feedback.

## Flat hunter (`app/flats/`)

Rental flats for the dashboard's **/wohnungen** page. Good new matches post to Discord #wohnungen.
Runs as a second task inside `deals-poller`, sharing its Kleinanzeigen rate limiter and backoff.

- **Sources:** Kleinanzeigen category 203 (per search: page 1, first poll walks 3 pages) and
  inberlinwohnen.de (one site-wide list of the state-owned companies; Livewire `wire:snapshot` JSON,
  full sweep every 6 h marks vanished flats gone). ImmoScout/Immowelt are bot-walled (401/403).
- **KA detail pages come in two layouts on the same URL** (cookie-bucketed): old server-rendered
  `addetailslist`, new Astro page whose `<astro-island props>` hold the ad incl. the seller's phone and
  imprint. `ka.parse_detail` handles both.
- **Model reads, code decides.** qwen3.5:9b gives offer kind (wohnung/wg_zimmer/zwischenmiete/tausch/
  gesuch/sonstiges), rent figures from prose, WBS, scam risk and how to apply — never a budget.
  Warm rent = portal field → model's copied figure → cold + NK + heating → per-m² estimate
  (labelled `estimated`). Warm is never below the card price. E-mails/phones come from regexes.
- **Match outcomes:** `hard` (outside area/size/rooms/budget/Tausch/WBS → not shown), `soft`
  (Gesuch, WG, Zwischenmiete, scam "hoch", only-estimated over budget → Aussortiert), `ok`.
  Unknown values pass.
- **Area:** city = Nominatim bbox (+ optional margin), pin = circle. Resolved once at save time, plus
  KA's own location id (`s-ort-empfehlungen.json`).
