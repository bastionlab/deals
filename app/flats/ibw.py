"""inberlinwohnen.de -- the shared portal of Berlin's seven state-owned housing companies.

The flat finder is a Livewire page: every result item ships its full data as
JSON in a `wire:snapshot` attribute, so no HTML scraping is needed at all.
10 items per page, newest first, `?page=N`. Rent, Nebenkosten and Heizkosten
are separate numbers and WBS is an explicit field, so the model never sees
these: they are proper listings by construction.

Applying happens on the company's own site (degewo, Gewobag, ...), which the
deeplink points at -- that is the contact channel. The finder keeps listing
flats for days after the company took them down, so the deeplink is what
says whether a flat is still there (see deeplink_status).
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE = "https://www.inberlinwohnen.de"
SOURCE = "inberlinwohnen"
LIST_URL = f"{BASE}/wohnungsfinder/"

_SNAP = re.compile(r'wire:snapshot="([^"]+)"')


def page_url(page: int) -> str:
    return LIST_URL if page <= 1 else f"{LIST_URL}?page={page}"


def _f(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _unwrap(v):
    """Livewire serialises arrays as [payload, {"s": "arr"}]."""
    while isinstance(v, list) and len(v) == 2 and isinstance(v[1], dict) and "s" in v[1]:
        v = v[0]
    return v


def _details(item: dict) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(o):
        o = _unwrap(o)
        if isinstance(o, dict) and "label" in o and "value" in o:
            out[o["label"]] = re.sub(r"<[^>]+>", " ", html.unescape(str(o["value"]))).strip()
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for x in o.values():
                walk(x)
    walk(item.get("details"))
    return out


def title_wbs(title: str | None) -> int | None:
    """The WBS field is often "unbekannt" while the title says "mit WBS 160"."""
    t = title or ""
    if re.search(r"\b(ohne|kein\w*)\s+WBS\b", t, re.I):
        return 0
    return 1 if re.search(r"\bWBS\b", t, re.I) else None


def parse(page_html: str) -> tuple[list[dict], int | None]:
    """-> (flats on this page, total number of flats the finder holds)."""
    flats, total = [], None
    for raw in _SNAP.findall(page_html):
        try:
            snap = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        name = snap.get("memo", {}).get("name", "")
        data = snap.get("data") or {}
        if name == "apartment-finder.rentable-apartment-finder":
            ids = _unwrap(data.get("itemIds"))
            total = len(ids) if isinstance(ids, list) else None
        if name != "apartment-finder.item.apartment-item":
            continue
        rec = _unwrap(data.get("data"))
        item = _unwrap(data.get("item")) or {}
        if not isinstance(rec, dict) or rec.get("deleted") or rec.get("removed_at"):
            continue
        addr = _unwrap(rec.get("address")) or {}
        district = _unwrap(addr.get("district")) or {}
        main = _unwrap(district.get("main_district")) or {}
        company = _unwrap(rec.get("company")) or {}
        det = _details(item)

        cold, extra, heating = _f(rec.get("rent_net")), _f(rec.get("extra_costs")), _f(rec.get("heating_costs"))
        total_rent = _f((det.get("Gesamtmiete") or "").replace(".", "").replace(",", "."))
        if total_rent:
            warm, kind = total_rent, "stated"
        elif cold and extra is not None:
            warm, kind = cold + extra + (heating or 0), "sum"
        else:
            warm, kind = None, None
        wbs = det.get("WBS", "").lower()
        created = rec.get("created_at")
        posted = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
        street = " ".join(x for x in (addr.get("street_name"), addr.get("street_number")) if x)
        cname = (company.get("name") or "").strip()
        deeplink = rec.get("deeplink") or LIST_URL

        flats.append({
            "source": SOURCE,
            "source_id": str(rec["id"]),
            "url": deeplink,
            "title": (rec.get("title") or "").strip() or f"{rec.get('rooms')} Zimmer, {street}",
            "description": None,
            "image_url": None,       # their image host answers 403 to hotlinks
            "posted_at": posted,
            "lat": _f(addr.get("lat")), "lon": _f(addr.get("lon")),
            "postal_code": addr.get("zip_code"),
            "district": " – ".join(x for x in (district.get("name"), main.get("name")) if x) or None,
            "address": ", ".join(x for x in (street, addr.get("zip_code")) if x) or None,
            "area": _f(rec.get("area")), "rooms": _f(rec.get("rooms")),
            "list_price": cold,
            "cold": cold, "extra": extra, "heating": heating,
            "warm": warm, "warm_kind": kind,
            "available_from": det.get("Bezugsfertig ab"),
            "wbs": 0 if "nicht" in wbs else (1 if "erforderlich" in wbs else title_wbs(rec.get("title"))),
            "tausch": 0,
            "kind": "wohnung",
            "scam": "niedrig",
            "ai_note": None,
            "seller": cname or None,
            "commercial": 1,
            "contact": {
                "how": f"Bewerbung über das Portal von {cname}" if cname else "Bewerbung über den Link",
                "name": cname or None,
                "channels": [{"type": "portal", "value": deeplink, "label": cname or "Portal"}],
            },
            "attrs": det,
            "detail_at": None,
            "judged_at": None,
            "status": "active",      # listed right now; a re-listed flat comes back from "gone"
        })
    return flats, total


def deeplink_status(status_code: int, final_url: str) -> str | None:
    """Company page after redirects -> gone | active | None (could not tell, change nothing).

    Seen 2026-10-19 on flats the finder still listed: berlinovo answers 404,
    GESOBAU 404 on `detailseite/-1`, HOWOGE redirects to `/404` with a 200.
    degewo, Gewobag, STADT UND LAND and WBM showed no dead links at the time.
    """
    path = urlparse(final_url).path.rstrip("/")
    if status_code in (404, 410) or path.endswith(("/404", "/-1")):
        return "gone"
    if status_code == 200:
        return "active"
    return None
