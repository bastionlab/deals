"""Kleinanzeigen rental flats (category 203, "Mietwohnungen").

The result card already carries m², rooms and the asking price, which is
enough to drop most ads for free. Only the survivors get a detail-page fetch:
that page has the portal's own key/value facts (Warmmiete, Nebenkosten,
Tauschangebot, ...), the full description, the seller and coordinates.

Same selector policy as app.sources.kleinanzeigen: anchor on ids and
data-attributes, never on Tailwind class names. The detail page is the older
server-rendered layout whose `addetailslist--*` classes have been stable for years.
"""
from __future__ import annotations

import html
import json
import re

from ..sources import kleinanzeigen as ka

SOURCE = "kleinanzeigen"
CATEGORY = "c203"


def build_url(spec: dict, page: int = 1) -> str:
    """Server-side filters: offers only, price cap, place + radius, newest first.

    The card price is whatever the landlord typed -- usually Kaltmiete,
    sometimes the warm total -- and either way it is at most the warm rent,
    so capping it at the warm budget never loses a flat that fits.
    """
    parts = [f"s-wohnung-mieten/{spec.get('ka_slug') or 'berlin'}"]
    if page > 1:
        parts.append(f"seite:{page}")
    parts.append("anzeige:angebote")
    if spec.get("max_warm"):
        parts.append(f"preis::{int(spec['max_warm'])}")
    parts.append(f"{CATEGORY}{spec.get('ka_location') or 'l3331'}r{int(spec.get('ka_radius') or 0)}")
    return f"{ka.BASE}/{'/'.join(parts)}?sortingField=SORTING_DATE"


_NUM = r"(\d+(?:[.,]\d+)?)"
# "39 m² · 1 Zi." is the card's own fact line; the description excerpt above it
# often mentions m² too, so the full pattern wins and a bare m² is the fallback
_AREA_ROOMS = re.compile(rf"{_NUM}\s*m²\s*·\s*{_NUM}\s*Zi\.")
_AREA = re.compile(rf"{_NUM}\s*m²")


def _num(s: str | None) -> float | None:
    """German number: "1.375" -> 1375, "41,18" -> 41.18."""
    if not s:
        return None
    s = s.strip().replace("€", "").replace("m²", "").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):
        s = s.replace(".", "")
    s = s.replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def parse_list(page_html: str) -> list[dict]:
    """Result page -> card-level flats. Reuses the deals card parser and adds m²/rooms."""
    cards = {c["source_id"]: c for c in ka.parse(page_html)}
    out = []
    for ad_id, _href, body in ka._ARTICLE.findall(page_html):
        c = cards.get(ad_id)
        if not c:
            continue
        text = ka._text(re.sub(r"<script.*?</script>", " ", body, flags=re.S))
        m = _AREA_ROOMS.search(text) or _AREA.search(text)
        out.append({
            "source": SOURCE, "source_id": ad_id, "url": c["url"], "title": c["title"],
            "card_description": c["description"],
            "image_url": c["image_url"], "posted_at": c["posted_at"],
            "postal_code": c["postal_code"], "district": c["district"],
            "area": _num(m.group(1)) if m else None,
            "rooms": _num(m.group(2)) if m and m.re is _AREA_ROOMS else None,
            "list_price": c["price_cents"] / 100 if c["price_cents"] else None,
        })
    return out


_KV = re.compile(r'<li class="addetailslist--detail">\s*(.*?)<span class="addetailslist--detail--value"\s*>\s*(.*?)</span>', re.S)
_TAGS = re.compile(r'<li class="checktag">\s*(.*?)\s*</li>', re.S)
_DESC = re.compile(r'<p id="viewad-description-text"[^>]*>(.*?)</p>', re.S)
_LAT = re.compile(r'<meta property="og:latitude" content="([\d.]+)"')
_LON = re.compile(r'<meta property="og:longitude" content="([\d.]+)"')
_STREET = re.compile(r'<span id="street-address"[^>]*>(.*?)</span>', re.S)
_LOCALITY = re.compile(r'<span id="viewad-locality"[^>]*>(.*?)</span>', re.S)
_SELLER = re.compile(r'class="[^"]*userprofile-vip"[^>]*>\s*(?:<a[^>]*>)?\s*([^<]+)', re.S)
_DATE = re.compile(r'id="viewad-extra-info".*?<span>(\d{2})\.(\d{2})\.(\d{4})</span>', re.S)


def _clean(s: str) -> str:
    return re.sub(r"[ \t\xa0]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip(" ,\n")


# --- the new detail layout ---------------------------------------------------
# Kleinanzeigen A/B-tests two detail layouts on the same URL (consecutive
# fetches alternate). The new one is an Astro page: every <astro-island> carries
# the whole ad as serialised props, where each value is [type, value]
# (0 = plain/object, 1 = array). Same facts as the old layout, plus the
# seller's phone number and the imprint of commercial landlords.
_ISLAND = re.compile(r'<astro-island[^>]*?\sprops="([^"]*)"')


def _astro(v):
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        t, x = v
        if t == 0:
            return {k: _astro(y) for k, y in x.items()} if isinstance(x, dict) else x
        if t == 1:
            return [_astro(y) for y in x]
        return x
    if isinstance(v, dict):
        return {k: _astro(y) for k, y in v.items()}
    return v


def _astro_ad(page_html: str) -> dict | None:
    for raw in _ISLAND.findall(page_html):
        if "localizedAttributes" not in raw:
            continue
        try:
            props = {k: _astro(v) for k, v in json.loads(html.unescape(raw)).items()}
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
        data = props.get("data")
        if isinstance(data, dict) and data.get("id"):
            return data
    return None


def _detail_from_astro(ad: dict) -> dict:
    kv = {a.get("localizedName", ""): a.get("localizedValue", "")
          for a in ad.get("localizedAttributes") or [] if isinstance(a, dict)}
    tags = [t for t in ad.get("tags") or [] if isinstance(t, str)]
    if tags:
        kv["Ausstattung"] = ", ".join(tags)
    loc = ad.get("locationSearchParameters") or {}
    user = ad.get("userDetails") or {}
    desc = _clean(re.sub(r"<br\s*/?>", "\n", ad.get("description") or ""))
    imprint = _clean(re.sub(r"<br\s*/?>", "\n", ad.get("imprint") or "")) if ad.get("imprint") else None
    return _facts(kv, desc, loc.get("latitude"), loc.get("longitude"),
                  ", ".join(x for x in (loc.get("streetAndHouseNumber"), loc.get("location")) if x),
                  user.get("contactName"),
                  1 if user.get("commercial") is True else (0 if user.get("commercial") is False else None),
                  phone=user.get("phoneNumber") or None, imprint=imprint)


def _facts(kv: dict, desc: str, lat, lon, address: str, seller, commercial, **extra) -> dict:
    tausch = kv.get("Tauschangebot")
    return {
        "description": re.sub(r"\n{3,}", "\n\n", desc or ""),
        "attrs": kv,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "address": address or None,
        "area": _num(kv.get("Wohnfläche")),
        "rooms": _num(kv.get("Zimmer")),
        "cold": _num(kv.get("Kaltmiete")),
        "extra": _num(kv.get("Nebenkosten")),
        "heating": _num(kv.get("Heizkosten")),
        "warm_attr": _num(kv.get("Warmmiete")),
        "deposit": kv.get("Kaution / Genoss.-Anteile") or kv.get("Kaution"),
        "available_from": kv.get("Verfügbar ab"),
        # "Kein Tausch" is explicit; a missing field says nothing, so the model decides then
        "tausch_attr": None if tausch is None else (0 if "kein" in tausch.lower() else 1),
        "seller": (seller or "").strip() or None,
        "commercial": commercial,
        "phone": extra.get("phone"),
        "imprint": extra.get("imprint"),
    }


def parse_detail(page_html: str) -> dict | None:
    """Detail page (either layout) -> the richer half of a flat. None if it is not an ad page."""
    out = _parse_detail(page_html)
    if out is not None:
        out["seller_since"] = ka.seller_since(page_html)
    return out


def _parse_detail(page_html: str) -> dict | None:
    if ad := _astro_ad(page_html):
        return _detail_from_astro(ad)
    d = _DESC.search(page_html)
    kv = {_clean(k): _clean(v) for k, v in _KV.findall(page_html)}
    if not d and not kv:
        return None
    desc = _clean(re.sub(r"<br\s*/?>", "\n", d.group(1))) if d else ""
    if _TAGS.search(page_html):
        kv["Ausstattung"] = ", ".join(_clean(t) for t in _TAGS.findall(page_html))
    lat, lon = _LAT.search(page_html), _LON.search(page_html)
    street, loc = _STREET.search(page_html), _LOCALITY.search(page_html)
    seller = _SELLER.search(page_html)
    address = ", ".join(x for x in (_clean(street.group(1)) if street else "",
                                    _clean(loc.group(1)) if loc else "") if x)
    commercial = 1 if "Gewerblicher Nutzer" in page_html else (0 if "Privater Nutzer" in page_html else None)
    return _facts(kv, desc, lat.group(1) if lat else None, lon.group(1) if lon else None,
                  address, _clean(seller.group(1)) if seller else None, commercial)


def ad_status(source_id: str, status_code: int, location: str | None, page_html: str) -> str | None:
    return ka.ad_status(source_id, status_code, location, page_html)
