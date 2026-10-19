"""Kleinanzeigen search-results source.

Everything we need for matching is on the result card: id, url, title,
description, price, PLZ + district, distance, posting time, image and the
"Versand möglich" marker. Detail pages are fetched only by `ad_status`, to
find out whether an ad we already showed has since been sold.

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
# Some cards say "(ca. 10 km)" instead of "(10 km)". Missing that left the
# distance unknown, and unknown counts as far -- Berlin ads showed up as Versand.
_LOC = re.compile(r'<span>(\d{5})\s+([^<]{1,60}?)</span>'
                  r'(?:\s*<span[^>]*>\((?:ca\.?\s*)?(\d+(?:[.,]\d+)?)\s*km\)</span>)?')
_WHEN = re.compile(r'<span>(Heute|Gestern),\s*(\d{1,2}):(\d{2})</span>|<span>(\d{2})\.(\d{2})\.(\d{4})</span>')
_PRICE = re.compile(r'>\s*(?:(\d{1,3}(?:\.\d{3})*|\d+)\s*€\s*(VB)?|(VB)|(Zu verschenken))\s*<')

_UMLAUT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def slug(text: str) -> str:
    text = text.lower().strip()
    for k, v in _UMLAUT.items():
        text = text.replace(k, v)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text)).strip("-")


def keywords(spec: dict) -> list[str]:
    """A search may poll several keywords: "rtx 3060, rtx 3090".

    Kleinanzeigen's search box needs EVERY word to appear, so one broad
    keyword ("nvidia grafikkarte rtx") silently misses most ads -- sellers
    write "MSI RTX 3060 Ventus". Several short keywords, one URL each, and the
    local terms/exclude/AI filters do the narrowing.
    """
    kws = [k.strip() for k in (spec.get("keyword") or "").split(",") if k.strip()]
    return kws or [""]


def build_url(spec: dict, page: int = 1, keyword: str | None = None) -> str:
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
    kw = keyword if keyword is not None else keywords(spec)[0]
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


# --- is it still for sale? ---------------------------------------------------
# A detail page comes in two layouts (Kleinanzeigen A/B-tests them, the same
# URL alternates between fetches) and each states the status differently:
#   new: <h1 ...><span ...>Reserviert •</span>Title</h1>   (also Gelöscht, Verkauft)
#   old: showPausedVeil: true / showDeletedVeil: true in an inline script
# An ad that is gone for good no longer has a page and redirects elsewhere.
_STATUS_NEW = re.compile(r'<h1[^>]*>\s*<span[^>]*>\s*(Reserviert|Gelöscht|Verkauft)\s*•')
_VEIL = re.compile(r'show(Paused|Deleted)Veil:\s*true')
_PAGE_OK = re.compile(r'id="viewad-title"|itemprop="name" data-soldlabel')


def ad_status(source_id: str, status_code: int, location: str | None, page_html: str) -> str | None:
    """'active' | 'reserved' | 'gone', or None when the page cannot tell.

    None is the answer for anything unexpected (a block page, a third layout):
    a listing must never be hidden because we misread a page.
    """
    if status_code in (404, 410):
        return "gone"
    if status_code in (301, 302, 303, 307, 308):
        # a changed slug redirects to the same ad; anything else means it is gone
        return None if location and source_id in location else "gone"
    if status_code != 200:
        return None
    if m := _STATUS_NEW.search(page_html):
        return "reserved" if m.group(1) == "Reserviert" else "gone"
    if m := _VEIL.search(page_html):
        return "reserved" if m.group(1) == "Paused" else "gone"
    return "active" if _PAGE_OK.search(page_html) else None


# --- who is selling? ---------------------------------------------------------
# Phishing ads come from accounts made a day or two earlier. Both detail layouts
# state the account's age: old "Aktiv seit 12.03.2021", new userDetails.userSince
# (and the same "Aktiv seit <!-- -->12.03.2021" in its rendered HTML).
_SINCE = re.compile(r'Aktiv seit\s*(?:<!--\s*-->)?\s*(\d{2}\.\d{2}\.\d{4})'
                    r'|userSince&quot;:\[0,&quot;(\d{2}\.\d{2}\.\d{4})')


def seller_since(page_html: str) -> float | None:
    """Epoch of the seller's 'Aktiv seit' day, None when the page does not say."""
    m = _SINCE.search(page_html)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) or m.group(2), "%d.%m.%Y").timestamp()
    except ValueError:
        return None


def is_new_seller(since: float | None, seen_at: float | None, days: float) -> bool:
    """Account younger than `days` when we first saw the ad. Unknown age is not suspicious.
    SQL twin: db.NEW_SELLER_SQL -- change both together."""
    return since is not None and since > (seen_at or time.time()) - days * 86400
