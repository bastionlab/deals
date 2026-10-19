"""Where a search looks, and how far a flat is from it.

A search area is either a named place (Nominatim's bounding box, plus an
optional margin) or a pin with a radius. Both end up as plain numbers in the
spec, so matching never calls out to anything.
"""
from __future__ import annotations

import math
import re

import httpx

from ..config import BROWSER_HEADERS, HTTP_TIMEOUT

NOMINATIM = "https://nominatim.openstreetmap.org"
# Nominatim's policy asks for an identifying UA; this runs a handful of times per saved search
NOMINATIM_HEADERS = {"User-Agent": "pve-flats/0.1 (personal flat search, low volume)",
                     "Accept-Language": "de"}
KA_PLACES = "https://www.kleinanzeigen.de/s-ort-empfehlungen.json"

# Kleinanzeigen only offers these radii; anything else is rounded up to the next one
KA_RADII = (5, 10, 20, 30, 50, 100, 150, 200)


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def in_area(spec: dict, lat: float, lon: float) -> tuple[bool, float]:
    """(inside, km from the area's centre)."""
    km = km_between(spec["lat"], spec["lon"], lat, lon)
    if spec.get("mode") == "pin":
        return km <= float(spec.get("radius_km") or 0), km
    s, n, w, e = spec["bbox"]
    # a degree of latitude is ~111 km; a degree of longitude shrinks with cos(lat)
    margin = float(spec.get("radius_km") or 0)
    dlat = margin / 111.0
    dlon = margin / (111.0 * max(0.2, math.cos(math.radians(spec["lat"]))))
    return (s - dlat <= lat <= n + dlat and w - dlon <= lon <= e + dlon), km


def reach_km(spec: dict) -> float:
    """Radius of a circle around the centre that covers the whole area."""
    if spec.get("mode") == "pin":
        return float(spec.get("radius_km") or 0)
    s, n, w, e = spec["bbox"]
    corner = max(km_between(spec["lat"], spec["lon"], la, lo) for la in (s, n) for lo in (w, e))
    return corner + float(spec.get("radius_km") or 0)


def ka_radius(km: float) -> int:
    return next((r for r in KA_RADII if r >= km), KA_RADII[-1])


async def geocode(client: httpx.AsyncClient, query: str) -> dict | None:
    r = await client.get(f"{NOMINATIM}/search", headers=NOMINATIM_HEADERS, timeout=HTTP_TIMEOUT,
                         params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "de",
                                 "addressdetails": 1})
    r.raise_for_status()
    hits = r.json()
    if not hits:
        return None
    h = hits[0]
    s, n, w, e = (float(x) for x in h["boundingbox"])
    a = h.get("address") or {}
    return {"label": h.get("display_name", query).split(",")[0], "full": h.get("display_name"),
            "lat": float(h["lat"]), "lon": float(h["lon"]), "bbox": [s, n, w, e],
            "postcode": a.get("postcode"), "city": a.get("city") or a.get("town") or a.get("village")}


async def reverse(client: httpx.AsyncClient, lat: float, lon: float) -> dict | None:
    r = await client.get(f"{NOMINATIM}/reverse", headers=NOMINATIM_HEADERS, timeout=HTTP_TIMEOUT,
                         params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14,
                                 "addressdetails": 1})
    r.raise_for_status()
    h = r.json()
    if "error" in h:
        return None
    a = h.get("address") or {}
    label = ", ".join(x for x in (a.get("suburb") or a.get("city_district"),
                                  a.get("city") or a.get("town") or a.get("village")) if x)
    return {"label": label or h.get("display_name", "").split(",")[0], "full": h.get("display_name"),
            "postcode": a.get("postcode"), "city": a.get("city") or a.get("town") or a.get("village")}


async def ka_location(client: httpx.AsyncClient, *queries: str | None) -> tuple[str, str] | None:
    """Kleinanzeigen's own place id ("l3331") and slug for the first query it knows.

    Its suggestion endpoint answers {"_0": "Deutschland", "_3331": "Berlin", ...};
    the first real entry is the best match.
    """
    for q in queries:
        if not q:
            continue
        r = await client.get(KA_PLACES, params={"query": q}, headers=BROWSER_HEADERS,
                             timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            continue
        for key, name in r.json().items():
            if key != "_0" and re.fullmatch(r"_\d+", key):
                return f"l{key[1:]}", name
    return None
