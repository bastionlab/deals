"""Kleinanzeigen search-results source.

Everything we need is on the result card, so we never fetch detail pages:
id, url, title, description, price, PLZ + district, distance, posting time,
image and the "Versand möglich" marker.

Selector policy: Kleinanzeigen serves Tailwind utility classes, which churn.
We anchor only on things that carry meaning -- the `data-adid`/`data-href`
attributes on <article> and the per-card ld+json -- and read the rest with
text-shaped regexes. Never select on a class name.
"""
from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta

BASE = "https://www.kleinanzeigen.de"
SOURCE = "kleinanzeigen"

_ARTICLE = re.compile(r'<article[^>]*\sdata-adid="(\d+)"[^>]*\sdata-href="([^"]+)"(.*?)</article>', re.S)
_LDJSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_H3 = re.compile(r'<h3[^>]*>.*?<a[^>]*>(.*?)</a>', re.S)
_LOC = re.compile(r'<span>(\d{5})\s+([^<]{1,60}?)</span>(?:\s*<span[^>]*>\((\d+(?:[.,]\d+)?)\s*km\)</span>)?')
_WHEN = re.compile(r'<span>(Heute|Gestern),\s*(\d{1,2}):(\d{2})</span>|<span>(\d{2})\.(\d{2})\.(\d{4})</span>')
_PRICE = re.compile(r'>\s*(?:(\d{1,3}(?:\.\d{3})*|\d+)\s*€\s*(VB)?|(VB)|(Zu verschenken))\s*<')

_UMLAUT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def slug(text: str) -> str:
    text = text.lower().strip()
    for k, v in _UMLAUT.items():
        text = text.replace(k, v)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text)).strip("-")


def build_url(spec: dict, page: int = 1) -> str:
    """Compose a search URL. All of these filters run server-side, so the
    location/radius/price/shipping half of the job costs us no AI at all."""
    parts: list[str] = []
    parts.append(f"s-seite:{page}" if page > 1 else "s-berlin")
    if page > 1:
        parts.append("berlin")
    if spec.get("offers_only", True):
        parts.append("anzeige:angebote")
    lo, hi = spec.get("min_price"), spec.get("max_price")
    if lo is not None or hi is not None:
        parts.append(f"preis:{lo if lo is not None else ''}:{hi if hi is not None else ''}")
    # "only" is the legacy spelling of "ship". versand:ja narrows server-side and
    # the distance rule is applied locally afterwards. Pickup deliberately does
    # NOT send versand:nein: that would drop nearby sellers who happen to offer
    # posting as well, and those are exactly the ones you would go and collect.
    shipping = spec.get("shipping", "any")
    if shipping in ("only", "ship"):
        parts.append("versand:ja")
    kw = spec.get("keyword") or ""
    if kw:
        parts.append(slug(kw))
    loc = spec.get("location_id", "l3331")          # l3331 = Berlin
    radius = int(spec.get("radius_km", 10))
    parts.append(f"k0{loc}r{radius}")
    return f"{BASE}/{'/'.join(parts)}?sortingField=SORTING_DATE"


def _text(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def _parse_when(body: str, now: float) -> float | None:
    m = _WHEN.search(body)
    if not m:
        return None
    if m.group(1):
        day = datetime.fromtimestamp(now).replace(
            hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
        if m.group(1) == "Gestern":
            day -= timedelta(days=1)
        # "Heute, 23:50" seen just after midnight means yesterday
        if day.timestamp() > now + 300:
            day -= timedelta(days=1)
        return day.timestamp()
    return datetime(int(m.group(6)), int(m.group(5)), int(m.group(4))).timestamp()


def _parse_price(body: str) -> tuple[int | None, str]:
    for m in _PRICE.finditer(body):
        if m.group(4):
            return 0, "free"
        if m.group(3):
            return None, "negotiable"
        if m.group(1):
            euros = int(m.group(1).replace(".", ""))
            return euros * 100, "negotiable" if m.group(2) else "fixed"
    return None, "none"


def parse(page_html: str, now: float | None = None) -> list[dict]:
    now = now or time.time()
    out: list[dict] = []
    for ad_id, href, body in _ARTICLE.findall(page_html):
        title = description = image = None
        for block in _LDJSON.findall(body):
            try:
                d = json.loads(block)
            except json.JSONDecodeError:
                continue
            if d.get("@type") == "ImageObject":
                title = d.get("title")
                description = d.get("description")
                image = d.get("contentUrl")
                break
        if not title:                                   # ld+json gone -> fall back to the heading
            m = _H3.search(body)
            title = _text(m.group(1)) if m else None
        if not title:
            continue                                    # nothing usable; skip rather than store junk

        price_cents, price_type = _parse_price(body)
        loc = _LOC.search(body)
        km = loc.group(3).replace(",", ".") if loc and loc.group(3) else None
        out.append({
            "source": SOURCE,
            "source_id": ad_id,
            "url": BASE + href,
            "title": title.strip(),
            "description": (description or "").strip() or None,
            "price_cents": price_cents,
            "price_type": price_type,
            "postal_code": loc.group(1) if loc else None,
            "district": loc.group(2).strip() if loc else None,
            "distance_km": float(km) if km else None,
            "shipping": 1 if "Versand möglich" in body else 0,
            "posted_at": _parse_when(body, now),
            "image_url": image,
            "raw": None,
        })
    return out
