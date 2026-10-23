"""Where a search looks, and how far a flat is from it.

A search area is either a named place (Nominatim's bounding box, plus an
optional margin) or a pin with a radius. Both end up as plain numbers in the
spec, so matching never calls out to anything. The spec also keeps the
country ("de" / "fr"): it decides which portals a search asks.
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
# where we have portals; a bare "Paris" must not resolve to a street in Brandenburg
COUNTRIES = "de,fr"


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _rings(shape: dict) -> list[list[list[float]]]:
    """Every ring of a (Multi)Polygon, [lon, lat] points."""
    polys = shape["coordinates"] if shape["type"] == "MultiPolygon" else [shape["coordinates"]]
    return [ring for poly in polys for ring in poly]


def _inside(shape: dict, lat: float, lon: float) -> bool:
    """Even-odd ray cast over all rings: holes (an enclave) count as outside."""
    hit = False
    for ring in _rings(shape):
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def km_to_edge(shape: dict, lat: float, lon: float) -> float:
    """Shortest distance from a point to the outline, flat-earth (fine for a few km)."""
    kx = 111.32 * math.cos(math.radians(lat))
    best = float("inf")
    for ring in _rings(shape):
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            ax, ay = (x1 - lon) * kx, (y1 - lat) * 111.32
            bx, by = (x2 - lon) * kx, (y2 - lat) * 111.32
            dx, dy = bx - ax, by - ay
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def in_area(spec: dict, lat: float, lon: float) -> tuple[bool, float]:
    """(inside, km from the area's centre)."""
    km = km_between(spec["lat"], spec["lon"], lat, lon)
    if spec.get("mode") == "pin":
        return km <= float(spec.get("radius_km") or 0), km
    if spec.get("shape"):
        margin = float(spec.get("radius_km") or 0)
        s, n, w, e = spec["bbox"]
        dlat = margin / 111.0 + 0.01
        dlon = dlat / max(0.2, math.cos(math.radians(spec["lat"])))
        if not (s - dlat <= lat <= n + dlat and w - dlon <= lon <= e + dlon):
            return False, km                        # cheap reject before walking the outline
        if _inside(spec["shape"], lat, lon):
            return True, km
        return margin > 0 and km_to_edge(spec["shape"], lat, lon) <= margin, km
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


# outlines are simplified to ~50 m: plenty for "is this flat in Neukölln", and Berlin
# stays a few hundred points instead of 20k
SHAPE_TOLERANCE = 0.0005
# a place Nominatim only knows as a point (a Kiez, a square) still needs an area
POINT_RADIUS_KM = 2.0
_KEEP = {"boundary", "place"}          # areas people search in; not shops, streets or bus stops


def _hit(h: dict, query: str = "") -> dict:
    s, n, w, e = (float(x) for x in h["boundingbox"])
    a = h.get("address") or {}
    shape = h.get("geojson") if (h.get("geojson") or {}).get("type") in ("Polygon", "MultiPolygon") else None
    # "Neukölln, Berlin, Deutschland": enough to tell two Neuköllns apart, short enough for a chip
    parts = [x.strip() for x in (h.get("display_name") or query).split(",")]
    return {"label": parts[0], "full": h.get("display_name"),
            "hint": ", ".join(dict.fromkeys(x for x in (a.get("city") or a.get("town") or a.get("state"),
                                                        a.get("country")) if x and x != parts[0])),
            "ref": f"{(h.get('osm_type') or 'x')[:1].upper()}{h.get('osm_id')}",
            "kind": h.get("addresstype") or h.get("type"),
            "lat": float(h["lat"]), "lon": float(h["lon"]), "bbox": [s, n, w, e], "shape": shape,
            "postcode": a.get("postcode"), "city": a.get("city") or a.get("town") or a.get("village"),
            "country": a.get("country_code") or "de"}


async def places(client: httpx.AsyncClient, query: str, limit: int = 5) -> list[dict]:
    """Candidates for a place name, areas with an outline first."""
    r = await client.get(f"{NOMINATIM}/search", headers=NOMINATIM_HEADERS, timeout=HTTP_TIMEOUT,
                         params={"q": query, "format": "jsonv2", "limit": 10, "countrycodes": COUNTRIES,
                                 "addressdetails": 1, "polygon_geojson": 1,
                                 "polygon_threshold": SHAPE_TOLERANCE})
    r.raise_for_status()
    hits = [_hit(h, query) for h in ([h for h in r.json() if h.get("category") in _KEEP] or r.json())]
    # outlines first, Nominatim's own ranking (importance) within; then the boundary and
    # the place node of one town fold into the boundary
    hits.sort(key=lambda x: x["shape"] is None)
    out, seen = [], set()
    for x in hits:
        key = (x["label"], x["hint"])
        if x["shape"] or key not in seen:   # Bezirk and Ortsteil of one name both stay
            seen.add(key)
            out.append(x)
    return out[:limit]


async def lookup(client: httpx.AsyncClient, ref: str) -> dict | None:
    """Exactly the candidate the user picked ("R62422")."""
    r = await client.get(f"{NOMINATIM}/lookup", headers=NOMINATIM_HEADERS, timeout=HTTP_TIMEOUT,
                         params={"osm_ids": ref, "format": "jsonv2", "addressdetails": 1,
                                 "polygon_geojson": 1, "polygon_threshold": SHAPE_TOLERANCE})
    r.raise_for_status()
    hits = r.json()
    return _hit(hits[0]) if hits else None


async def geocode(client: httpx.AsyncClient, query: str) -> dict | None:
    hits = await places(client, query, 1)
    return hits[0] if hits else None


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
            "postcode": a.get("postcode"), "city": a.get("city") or a.get("town") or a.get("village"),
            "country": a.get("country_code") or "de"}


def country(spec: dict) -> str:
    """Searches saved before countries existed are all German."""
    return spec.get("country") or "de"


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
