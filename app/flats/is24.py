"""ImmoScout24 rental flats, through the API its own mobile app uses.

The website answers 401 behind a captcha, the app API does not. It is
unofficial, so it gets its own slow limiter and as few calls as the job
needs: one result page per search per cycle, one exposé per new flat.

- search/list: radius around the search's centre, cold-rent cap, rooms and
  m² filtered server-side, newest first, 50 per page. The card price is the
  Kaltmiete, which is never more than the warm rent, so capping it at the
  warm budget never loses a flat that fits (same argument as Kleinanzeigen).
- expose/{id}: the portal's own Kosten block (Kaltmiete, Nebenkosten,
  Heizkosten, Gesamtmiete), WBS as a checkbox, description, contact person
  and imprint. A 404 means the flat was taken down.
"""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

from . import geo
from .ka import _num

SOURCE = "immoscout24"
API = "https://api.mobile.immobilienscout24.de"
HEADERS = {"User-Agent": "ImmoScout_27.3_26.0_._", "Accept": "application/json",
           "Content-Type": "application/json"}
SEARCH_BODY = {"supportedResultListTypes": [], "userData": {}}


def expose_url(source_id: str) -> str:
    """What the user opens: the website's exposé page."""
    return f"https://www.immobilienscout24.de/expose/{source_id}"


def api_expose(source_id: str) -> str:
    return f"{API}/expose/{source_id}"


def _range(lo, hi) -> str | None:
    if not lo and not hi:
        return None
    return f"{float(lo):.1f}-{'' if not hi else f'{float(hi):.1f}'}" if lo else f"-{float(hi):.1f}"


def search_url(spec: dict, page: int = 1) -> str | None:
    """None if the search has no centre to draw a radius around."""
    if spec.get("lat") is None:
        return None
    km = max(1.0, round(geo.reach_km(spec) + 0.05, 1))
    q = [f"searchType=radius", f"geocoordinates={spec['lat']:.5f};{spec['lon']:.5f};{km}",
         "realestatetype=apartmentrent"]
    if spec.get("max_warm"):
        q += [f"price=-{float(spec['max_warm']):.1f}", "pricetype=rentpermonth"]
    if (r := _range(spec.get("min_rooms"), spec.get("max_rooms"))):
        q.append(f"numberofrooms={r}")
    if (r := _range(spec.get("min_area"), spec.get("max_area"))):
        q.append(f"livingspace={r}")
    q += [f"pagenumber={page}", "sorting=-firstactivation"]
    return f"{API}/search/list?{'&'.join(q)}"


_AGO = re.compile(r"vor (?:über )?(\d+|einer|einem) (Minute|Stunde|Tag|Woche|Monat|Jahr)", re.I)
_UNIT = {"minute": 60, "stunde": 3600, "tag": 86400, "woche": 7 * 86400, "monat": 30 * 86400,
         "jahr": 365 * 86400}


def _posted(text: str | None, now: float) -> float | None:
    """'vor 20 Minuten' / 'vor einer Stunde' / 'vor 3 Tagen' -> epoch. Coarse, as the card is."""
    m = _AGO.search(text or "")
    if not m:
        return None
    n = 1 if not m.group(1).isdigit() else int(m.group(1))
    return now - n * _UNIT[m.group(2).lower()]


def parse_list(data: dict, now: float | None = None) -> tuple[list[dict], int | None]:
    """search/list JSON -> (card-level flats, number of pages)."""
    now = now or time.time()
    out = []
    for row in data.get("resultListItems") or []:
        if row.get("type") != "EXPOSE_RESULT":
            continue
        it = row.get("item") or {}
        if not it.get("id"):
            continue
        attrs = [a.get("value", "") for a in it.get("attributes") or []]
        pick = lambda unit: next((_num(a) for a in attrs if a.rstrip().endswith(unit)), None)  # noqa: E731
        addr = it.get("address") or {}
        line = re.sub(r"\s*\(unvollständige Adresse\)", "", addr.get("line") or "")
        plz = re.search(r"\b(\d{5})\b", line)
        district = line.split("Berlin, ", 1)[1].strip() if "Berlin, " in line else None
        pic = (it.get("titlePicture") or {}).get("full")
        out.append({
            "source": SOURCE, "source_id": str(it["id"]), "url": expose_url(str(it["id"])),
            "title": (it.get("title") or "").strip(),
            "image_url": pic,
            "posted_at": _posted(it.get("published"), now),
            "postal_code": plz.group(1) if plz else None,
            "district": district,
            "address": line or None,
            "lat": addr.get("lat"), "lon": addr.get("lon"),
            "area": pick("m²"), "rooms": pick("Zi."), "list_price": pick("€"),
            "commercial": 0 if it.get("isPrivate") else 1,
        })
    return out, data.get("numberOfPages")


def _imprint(url: str) -> str | None:
    """AGENTS_CONTACT carries the imprint as an app link: is24://imprint?text=..."""
    if not url.startswith("is24://imprint"):
        return None
    return (parse_qs(urlparse(url).query).get("text") or [None])[0]


def parse_expose(data: dict) -> dict | None:
    """expose JSON -> the richer half of a flat, shaped like ka._facts so extract.enrich takes it."""
    sections = data.get("sections")
    if not isinstance(sections, list):
        return None
    kv: dict[str, str] = {}
    texts, phones, imprint, address = [], [], None, None
    agent = company = lat = lon = None
    premium_only = False
    for s in sections:
        t = s.get("type")
        if t == "ATTRIBUTE_LIST":
            for a in s.get("attributes") or []:
                label = (a.get("label") or "").rstrip(": ").strip()
                if a.get("type") == "TEXT":
                    kv[label] = a.get("text", "")
                elif a.get("type") == "CHECK":
                    kv[label] = "ja"
        elif t == "TOP_ATTRIBUTES":
            for a in s.get("attributes") or []:
                if a.get("label") == "Warmmiete":
                    kv.setdefault("Warmmiete", a.get("text", ""))
        elif t == "TEXT_AREA" and s.get("text"):
            texts.append(f"{s.get('title') or ''}:\n{s['text']}".strip(":\n"))
        elif t == "MAP":
            # a hidden street reads "Die vollständige Adresse ... erhältst du vom Anbieter."
            lines = [x for x in (s.get("addressLine1"), s.get("addressLine2")) if x and "Adresse" not in x]
            address = ", ".join(lines) or None
            loc = s.get("location") or {}
            lat, lon = loc.get("lat"), loc.get("lng")
        elif t == "AGENTS_INFO":
            agent, company = s.get("name") or agent, s.get("company") or company
        elif t == "CONTACT":
            phones += [p.get("text") for p in s.get("phoneNumbers") or [] if p.get("text")]
            premium_only = str(s.get("premiumProfileRequiredForContacting")).lower() == "true"
        elif t == "AGENTS_CONTACT":
            for ref in s.get("references") or []:
                imprint = _imprint(ref.get("url") or "") or imprint
    if not kv and not texts:
        return None
    heat_in = (kv.get("Heizkosten in Nebenkosten enthalten") or "").lower() == "ja"
    heating = _num(kv.get("Heizkosten"))
    return {
        "description": "\n\n".join(texts),
        "attrs": kv,
        "address": address,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "area": _num(kv.get("Wohnfläche ca.")),
        "rooms": _num(kv.get("Zimmer")),
        "cold": _num(kv.get("Kaltmiete (zzgl. Nebenkosten)")),
        "extra": _num(kv.get("Nebenkosten")),
        "heating": 0.0 if heat_in and heating is None else heating,
        "warm_attr": _num(kv.get("Gesamtmiete")) or _num(kv.get("Warmmiete")),
        "deposit": kv.get("Kaution oder Genossenschaftsanteile"),
        "available_from": kv.get("Bezugsfrei ab"),
        # a ticked box is a yes; no box says nothing, so the model decides then
        "wbs_attr": 1 if kv.get("Wohnberechtigungsschein erforderlich") == "ja" else None,
        "tausch_attr": None,
        "seller": company or agent,
        "contact_name": agent,
        "phone": phones[0] if phones else None,
        "imprint": imprint,
        "premium_only": premium_only,
    }
