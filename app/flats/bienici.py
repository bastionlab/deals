"""Bien'ici rental flats (France), through the JSON its own map page loads.

Leboncoin, SeLoger, PAP and Logic-Immo answer 403 behind DataDome/Cloudflare;
Bien'ici does not, and it carries most agency listings plus the private ones
posted there. One list request holds everything the feed needs (full
description, rent, charges, m², rooms, position), so there is no detail fetch:

- realEstateAds.json?filters=...: zoneIds of a place (from suggest.json),
  newest first, price = loyer charges comprises (= Warmmiete), rentWithoutCharges
  = loyer hors charges, charges = provision. Rent cap, rooms, m² filtered
  server-side. Positions are blurred to a ~50 m disk, fine for a radius.
- realEstateAd.json?id=...: 404 once the ad is taken down.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

import httpx

from ..config import HTTP_TIMEOUT

SOURCE = "bienici"
SITE = "https://www.bienici.com"
SUGGEST = "https://res.bienici.com/suggest.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Accept": "application/json", "Accept-Language": "fr-FR,fr;q=0.9"}
PER_PAGE = 50
PORTAL = {"type": "bienici", "label": "Bien'ici-Nachricht", "how": "Kontaktformular auf Bien'ici"}


def ad_url(source_id: str) -> str:
    return f"{SITE}/annonce/{source_id}"


def api_ad(source_id: str) -> str:
    return f"{SITE}/realEstateAd.json?id={source_id}"


async def zone_ids(client: httpx.AsyncClient, *queries: str | None) -> list[str]:
    """Bien'ici's own zone ids for the first place name it knows (city > arrondissement)."""
    for q in queries:
        if not q:
            continue
        r = await client.get(SUGGEST, params={"q": q}, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            continue
        for hit in r.json():
            if hit.get("zoneIds"):
                return [str(z) for z in hit["zoneIds"]]
    return []


def search_url(spec: dict, page: int = 1) -> str | None:
    if not spec.get("bienici_zones"):
        return None
    f = {"size": PER_PAGE, "from": (page - 1) * PER_PAGE, "filterType": "rent",
         "propertyType": ["flat", "house"], "onTheMarket": [True],
         "sortBy": "publicationDate", "sortOrder": "desc",
         "zoneIdsByTypes": {"zoneIds": spec["bienici_zones"]}}
    if spec.get("max_warm"):
        f["maxPrice"] = int(spec["max_warm"])
    if spec.get("min_rooms"):
        f["minRooms"] = int(spec["min_rooms"])
    if spec.get("max_rooms"):
        f["maxRooms"] = int(spec["max_rooms"] + 0.5)
    if spec.get("min_area"):
        f["minArea"] = int(spec["min_area"])
    if spec.get("max_area"):
        f["maxArea"] = int(spec["max_area"] + 0.5)
    return f"{SITE}/realEstateAds.json?filters={json.dumps(f, separators=(',', ':'))}"


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(html: str | None) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)).strip()


def parse(ad: dict, now: float | None = None) -> dict | None:
    """One ad -> a fully read flat (minus the model's reading), shaped for extract.enrich."""
    if not ad.get("id"):
        return None
    pos = (ad.get("blurInfo") or {}).get("position") or {}
    price = ad.get("price")
    cold = ad.get("rentWithoutCharges")
    charges = ad.get("charges")
    attrs = {k: v for k, v in (
        ("Loyer CC", f"{price} €" if price else None),
        ("Loyer HC", f"{cold} €" if cold else None),
        ("Charges", f"{charges} €" if charges is not None else None),
        ("Meublé", {True: "oui", False: "non"}.get(ad.get("isFurnished"))),
        ("Étage", ad.get("floor")),
        ("Ascenseur", {True: "oui", False: "non"}.get(ad.get("hasElevator"))),
        ("Balcon", "oui" if ad.get("hasBalcony") else None),
        ("Chambres", ad.get("bedroomsQuantity")),
        ("Frais d'agence", f"{ad['agencyRentalFee']:.0f} €" if ad.get("agencyRentalFee") else None),
        ("DPE", ad.get("energyClassification")),
        ("Encadrement des loyers", "oui" if ad.get("isSubjectToRentControl") else None),
    ) if v not in (None, "")}
    district = (ad.get("district") or {}).get("libelle") or ad.get("city")
    photos = ad.get("photos") or []
    pro = ad.get("accountType") not in (None, "particulier", "private")
    card = {
        "source": SOURCE, "source_id": str(ad["id"]), "url": ad_url(str(ad["id"])),
        "title": (ad.get("title") or f"{ad.get('roomsQuantity') or '?'} pièces {ad.get('city') or ''}").strip(),
        "image_url": photos[0].get("url") if photos else None,
        "posted_at": _epoch(ad.get("publicationDate")) or now,
        "postal_code": ad.get("postalCode"),
        "district": district,
        "address": ", ".join(x for x in (district, ad.get("city")) if x) or None,
        "lat": pos.get("lat"), "lon": pos.get("lon"),
        "area": ad.get("surfaceArea"), "rooms": ad.get("roomsQuantity"),
        "list_price": price,
        "commercial": 1 if pro else 0,
    }
    detail = {
        "description": _text(ad.get("description")),
        "attrs": attrs,
        "cold": cold,
        "extra": charges,
        "heating": None,
        # charges comprises: Bien'ici's price is what goes out every month
        "warm_attr": price,
        "deposit": f"{ad['safetyDeposit']:.0f} €" if ad.get("safetyDeposit") else None,
        "available_from": ad.get("availableDate"),
        "wbs_attr": None,
        "tausch_attr": 0,
        "seller": ad.get("accountDisplayName"),
        "contact_name": None,
        "phone": None,
        "imprint": None,
        "premium_only": False,
    }
    return {"card": card, "detail": detail}


def parse_list(data: dict) -> tuple[list[dict], int]:
    """-> ([{card, detail}], total)."""
    now = time.time()
    return [x for x in (parse(a, now) for a in data.get("realEstateAds") or []) if x], int(data.get("total") or 0)
