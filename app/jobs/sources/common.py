"""Shared pieces for every job source.

A source module exposes:
    SOURCE: str                      # short id, stored in jobs.source
    LABEL: str                       # what the UI shows
    INTERVAL: float                  # seconds between two requests to this host
    async def search(client, q: dict, limiter: Limiter) -> list[dict]
    async def detail(client, job: dict, limiter: Limiter) -> dict | None   # optional

q (built by the poller from a saved search):
    keywords: list[str]   1-4 short queries, each sent on its own ("python entwickler", "backend developer")
    place: str | None     city / region name, e.g. "Berlin"; None = no location filter
    lat, lon: float|None  the place's centre, when known
    radius_km: float      0 = the place itself
    remote: bool          ask the portal for remote-only postings (used for the remote leg of a search)
    max_age_days: int     newest postings only, when the portal can filter by date
    max_pages: int        how deep to page; sources stop early when a page holds nothing

Every job is a plain dict made with job(); unknown values stay None, never guessed.
"""
from __future__ import annotations

import asyncio
import html
import random
import re
import time

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
TIMEOUT = 25.0

WORK_MODES = ("onsite", "hybrid", "remote")
EMPLOYMENT = ("vollzeit", "teilzeit", "werkstudent", "praktikum", "ausbildung",
              "freelance", "minijob", "befristet")


class Limiter:
    """One per host: a fixed gap between requests, jittered +/- 30 %."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = time.monotonic() + self.interval * random.uniform(0.7, 1.3)


def job(source: str, source_id: str, url: str, title: str, **kw) -> dict:
    """The one shape every source returns. Unknown = None."""
    d = {
        "source": source, "source_id": str(source_id), "url": url, "title": clean(title),
        "company": None,
        "location": None,          # free text as the portal shows it ("Berlin, Hamburg")
        "lat": None, "lon": None, "postal_code": None,
        "work_mode": None,         # onsite | hybrid | remote; None = not stated
        "employment": [],          # subset of EMPLOYMENT
        "posted_at": None,         # epoch seconds
        "salary_min": None, "salary_max": None,   # EUR per YEAR (monthly * 12, hourly * 2080)
        "salary_text": None,       # as the ad says it
        "description": None,       # plain text (may be just a teaser until detail())
        "apply_url": None,         # the employer's / ATS's own application page, if known
        "apply_email": None,
        "agency": None,            # True = Personalvermittlung / Zeitarbeit / recruiter, when the portal says so
        "lang": None,              # de | en, when obvious
        "attrs": {},               # the portal's own extra facts, short strings
    }
    for k, v in kw.items():
        if k not in d:
            raise KeyError(f"unknown job field {k}")
        d[k] = v
    return d


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"(?i)<\s*(br|/p|/div|/li|/h\d|/tr)\s*/?>")
_LI = re.compile(r"(?i)<\s*li[^>]*>")


def text(fragment: str | None) -> str:
    """HTML -> readable plain text with line breaks kept."""
    if not fragment:
        return ""
    s = _BLOCK.sub("\n", fragment)
    s = _LI.sub("\n• ", s)
    s = html.unescape(_TAG.sub(" ", s))
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return "\n".join(line.strip() for line in s.splitlines()).strip()


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


_REMOTE = re.compile(r"(?i)\b(remote|home[\s-]?office|100\s?%\s?mobil|fully distributed|work from home|"
                     r"ortsunabhängig|mobiles arbeiten)\b")
_HYBRID = re.compile(r"(?i)\bhybrid\b|\d\s?(tage?|days?)\s+(im\s+)?(büro|office)")


def guess_mode(*texts: str | None) -> str | None:
    """A cheap hint from title/location text. Sources that know better set it themselves."""
    t = " ".join(x for x in texts if x)
    if _HYBRID.search(t):
        return "hybrid"
    if _REMOTE.search(t):
        return "remote"
    return None


def yearly(amount: float | None, unit: str | None) -> float | None:
    """Normalise a salary figure to EUR per year. unit: year|month|hour|day."""
    if amount is None:
        return None
    unit = (unit or "year").lower()
    f = {"year": 1, "month": 12, "week": 52, "day": 220, "hour": 2080}.get(unit, 1)
    return round(amount * f)
