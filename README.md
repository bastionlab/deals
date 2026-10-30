# deals — Deals, Flats and Jobs finders

The backend behind the Deals, Flats and Jobs add-ons of [Bastion](https://github.com/bastionlab/core), a
phone-first dashboard for a Proxmox home server. It runs in its own container: one poller watches the portals,
a local model (Ollama) reads the ads, plain code decides what you see, and a small API serves the feeds to the
dashboard. Good new finds can go to Discord.

> **⚠ Personal use only, at your own risk.** This code scrapes sites (Kleinanzeigen, ImmoScout24, job portals
> and others) whose terms may forbid automated access. Some sources are unofficial APIs that can change or
> break at any time. Keep the request rate low and decide for yourself whether to run it.

One rule runs through all three finders: **the model reads, code decides.** The model pulls facts out of prose.
Every number compared against your limits (price, size, rent, distance, salary) is checked in code. Anything
unknown passes, because a missed find costs more than one extra card.

## Deals

Finds underpriced Kleinanzeigen listings near you (or shippable) and pushes them to Discord before anyone else
notices. eBay is not implemented yet.

- **Polite polling.** One global rate limiter (`DEALS_FETCH_INTERVAL`, default 8 s, ±40 % jitter) is shared by
  every search, so request volume does not grow with the number of searches. Searches go round-robin,
  oldest first. HTTP 403/429 starts a shared exponential backoff and one alert.
- **Server-side filters first.** Location, radius, price band, offers-only and shipping are part of the
  Kleinanzeigen URL, so they cost nothing. Ad ids grow with posting time, so "new since last poll" is one
  cheap request.
- **Free filters.** Required, any-of and excluded words, wanted-ads ("Suche …") and distance
  (`app/filters.py`).
- **Model judging.** A search can carry an `intent`: one or two sentences saying what counts. Only new
  listings that passed the free filters are shown to the model, so most polls never call it.
- **Numbers in code.** Size and inch bounds in the intent ("mindestens 6 TB", "maximal 27 Zoll") are
  checked against the title in code and override the model. The model once kept a 4 TB drive for a
  "mindestens 6 TB" search with score 100.
- **Abholen vs Versand.** "Versand möglich" only says the seller is willing to post. A listing counts as
  shipped only when it is further than `DEALS_NEARBY_KM` (default 15). Unknown distance counts as far.
  The rule lives in `filters.is_shipped()` and its SQL twin `SHIPPED_SQL` in `api.py`: change both together.
- **Phishing guard.** A seller account younger than `DEALS_NEW_SELLER_DAYS` (default 3) when the ad first
  shows up is never pushed; it lands under "Aussortierte".
- **Sold ads drop out.** After each search one feed listing that is due gets re-opened (every 20 min on day
  one, 2 h in week one, then 12 h). Gone ads leave the feed and Discord; reserved ones keep a badge. A page
  that cannot be read never hides a listing.
- **Composing a search** (`app/compose.py`). Draft filters, run the search for real, show the model its own
  hits next to your wish, correct, repeat. The best round wins, not the last one.

**Notifications:** matches go to Discord `#deals`; markup breaks and blocks go to `#alerts`.

### Selector policy

Kleinanzeigen serves utility classes that change without warning. The parser anchors only on `data-adid` /
`data-href` on `<article>` and the per-card `ld+json`; price, place and time come from text regexes.
**Never select on a class name.** A 200 response with zero parsed ads is treated as a markup change: after 3
in a row you get an alert instead of weeks of silence.

## Flats (`app/flats/`)

Rental flats for the Flats add-on. Runs as a second task inside the same poller and shares the Kleinanzeigen
rate limiter and backoff.

**Sources** (each only sees searches in its country):

- Kleinanzeigen, category Mietwohnungen (Germany)
- inberlinwohnen.de, the shared portal of Berlin's state-owned housing companies (Berlin)
- ImmoScout24, through the API its mobile app uses, with its own slow limiter (Germany)
- Bien'ici, through the JSON its map page loads (France)

Immowelt, Leboncoin, SeLoger and similar sites are bot-walled and not used.

**Matching:**

- The model reads the ad: offer kind (flat, WG room, sublet, swap, wanted ad), rent figures written in
  prose, WBS, scam smell and how to apply. It never sees your budget.
- Code builds the warm rent: portal field, then the model's copied figure, then cold + extra costs +
  heating, then a per-m² estimate (labelled as estimated).
- Outcomes: `hard` (outside area, size, rooms, budget, swap, WBS: not shown), `soft` (wanted ads, WG rooms,
  sublets, scam suspicion, estimated rent over budget: under "Aussortiert") or `ok`.
- Area is a city outline or a pin with a radius, resolved once when you save the search (Nominatim).
- The same flat on two portals, reposted or matched twice folds into one card (title + rooms + m²).
- Gone flats drop out through rechecks of the ad or the housing company's own page.
- A first message to the landlord is written from your own profile. Code picks the salutation; anything the
  ad asks for that your profile lacks comes back as missing, never invented.

**Notifications:** new flats that pass a search with a known warm rent go to Discord `#wohnungen`, once per
flat. A new search's first sweep is not sent.

## Jobs (`app/jobs/`)

Job postings for the Jobs add-on, a third task inside the poller.

- **CV in, profile out.** You upload a PDF; the model turns it into a short profile of facts the CV states.
  It can also suggest portal keywords from your wish.
- **Sources:** Bundesagentur für Arbeit, StepStone, LinkedIn (logged-out search, no login), XING, Arbeitnow,
  Berlin Startup Jobs, Remotive, Himalayas, Jobicy, RemoteOK, We Work Remotely, Welcome to the Jungle,
  HelloWork and France Travail. Each has its own limiter. Country-bound portals only see searches in their
  country.
- **Matching, pass one (code):** too old, excluded word or company, wrong work mode, too far, or salary
  stated below your floor is never shown.
- **Matching, pass two (model):** the model reads the full posting against your CV and wish (work mode,
  level, agency, remote region, fit score). Everything here is soft, so a wrongly hidden job stays findable.
- **Where to apply:** the employer's own channel behind the portal posting (ATS or career page, e-mail, or a
  search through a self-hosted SearXNG), else the portal itself.

**Notifications:** strong new matches (fit ≥ 90) go to Discord `#jobs`, once per job across portals.

## Layout

```
app/poller.py        the loop: deals, plus the flats and jobs tasks
app/api.py           the API for the dashboard (/flats/*, /jobs/* routers)
app/sources/         Kleinanzeigen URL builder + parser
app/filters.py       free deal matching
app/ai.py            deal judging + the numeric guard
app/compose.py       the search composer
app/discord.py       webhooks, one per channel
app/cli.py           the `deals` command
app/flats/           flat sources, reading, matching, dedupe, messages
app/jobs/            CV, job sources, matching, apply links
systemd/             deals-poller, deals-api, the CLI wrapper
```

## Install

**From Bastion.** Install the **Hunters backend** add-on on the Add-ons page, then Deals, Flats or Jobs.
It needs the Local AI add-on (Ollama) first. The install creates a Debian container (2 GB RAM, 12 GB disk),
records it as `cts.deals` in `/etc/dash/config.json`, clones this repo on the host, runs `bootstrap.sh`, then
`deploy.sh`. A container the config already knows is left alone.

**By hand**, on the Proxmox host, with Bastion's config in place:

```sh
DASH_CT=<container id> ./bootstrap.sh   # once: Python, venv, folders, a fresh API token
./deploy.sh                             # every change: sync code, refresh env, restart
```

`bootstrap.sh` installs Python into the container, creates `/opt/deals`, `/var/lib/deals` and
`/etc/deals/deals.env`, and writes a random API token to `token` next to the script (0600, gitignored).
`deploy.sh` reads the container and the addresses it needs from `/etc/dash/config.json`, copies `app/`,
installs `requirements.txt`, writes the env, installs both units and restarts them.

## Config

Everything lives in `/etc/deals/deals.env` inside the container (0600, not in git). `deploy.sh` keeps the
first four in step with the dashboard; the rest are yours to set.

- `DEALS_API_TOKEN`: bearer token the dashboard sends (from the `token` file)
- `DEALS_API_ALLOW`: source IPs allowed to call the API (dashboard, host, localhost)
- `DEALS_OLLAMA_URL`: Ollama on the Local AI container
- `DEALS_SEARX_URL`: SearXNG, used to find where to apply
- `DEALS_DB`: SQLite file (default `/var/lib/deals/deals.db`, WAL)
- `DEALS_FETCH_INTERVAL`: seconds between Kleinanzeigen requests (default 8)
- `DEALS_NEARBY_KM`: below this a listing is pickup, not shipping (default 15)
- `DEALS_NEW_SELLER_DAYS` / `DEALS_FLAT_NEW_SELLER_DAYS`: seller age that counts as a scam signal
  (default 3 / 30)

Discord webhooks come from `/etc/discord.json` in the container, which Bastion's Discord settings write:
`{"deals": url, "wohnungen": url, "jobs": url, "alerts": url}`. A missing key turns that channel off. A
channel only gets what was first seen after its hook was set, so linking one never floods it.

The API listens on port 8090. It checks the source IP and the bearer token on every route and never shells
out: the worst a caller can do is change saved searches.

## CLI

Inside the container, `deals` works on deal searches:

```sh
deals add <name> "<search words>" --terms a,b --exclude x,y --min-price 25 --max-price 250 --radius 10
deals ls            # searches, hit counts, last poll, last error
deals recent        # what matched lately
deals poll --quiet  # one pass, nothing to Discord
deals judge --all -v  # re-run the model over everything, showing each verdict
deals testsend      # prove the Discord #alerts path works
deals disable <name> / enable <name> / rm <name>
```

`add` also takes `--require`, `--location` (a Kleinanzeigen location id, default Berlin), `--shipping
any|ship|pickup`, `--intent` and `--min-score`. Flats and jobs are managed from the dashboard.

Logs: `journalctl -u deals-poller -u deals-api`.

## Development

Python 3 with FastAPI, httpx and pypdf (`requirements.txt`). There is no test suite yet. To try a change
without touching the live feed, point `DEALS_DB` at a scratch file and run `python -m app.cli init`, then `python -m app.cli poll --quiet`
or `uvicorn app.api:app` from the repo root. Ship with `./deploy.sh`.

Write intents and prompts from facts, not guesses: a named model also covers its variants (`3090` matches
`3090 Ti`) unless the intent says otherwise, and a wrong detail in the intent (a "display" on a device that
has none) makes the model throw out every real match.

## License

[AGPL-3.0](LICENSE), like the Bastion core. Contributions: pull requests here; for the Deals, Flats and Jobs
add-ons themselves see the [marketplace](https://github.com/bastionlab/addons).
