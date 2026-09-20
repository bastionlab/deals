"""The free half of the matching: everything decidable without a model.

Kleinanzeigen already applied location, radius, price band and shipping
server-side, so this is mostly about keyword precision -- the noise that
actually ruins a feed is accessories and wanted-ads, not distant sellers.
"""
from __future__ import annotations

import re

from .config import NEARBY_KM

# "Suche Playstation" is a wanted-ad; anzeige:angebote misses some of them.
_WANTED = re.compile(r"^\s*(suche|gesucht|ankauf|kaufe)\b", re.I)


def is_shipped(listing: dict, nearby_km: float = NEARBY_KM) -> bool:
    """Does this listing actually need shipping?

    Kleinanzeigen's own flag says only that the seller is willing to post it,
    and most nearby sellers tick it too. Something 4 km away you simply collect,
    so it is not a Versand listing however the seller labelled it. An unknown
    distance counts as far: we cannot claim it is around the corner.
    """
    if not listing.get("shipping"):
        return False
    dist = listing.get("distance_km")
    return dist is None or float(dist) > nearby_km


def _haystack(listing: dict) -> str:
    return f"{listing.get('title') or ''} {listing.get('description') or ''}".lower()


def check(listing: dict, spec: dict) -> tuple[bool, str | None]:
    """Return (keep, reason_if_dropped)."""
    hay = _haystack(listing)

    if _WANTED.match(listing.get("title") or ""):
        return False, "wanted-ad"

    for bad in (spec.get("exclude") or []):
        if bad.lower() in hay:
            return False, f"excluded:{bad}"

    terms = [t.lower() for t in (spec.get("terms") or []) if t.strip()]
    if terms and not any(t in hay for t in terms):
        return False, "no-term-match"

    for need in (spec.get("require") or []):
        if need.lower() not in hay:
            return False, f"missing:{need}"

    radius = spec.get("radius_km")
    dist = listing.get("distance_km")
    shipping_ok = bool(listing.get("shipping"))

    # The mode is judged on the derived meaning, not on the raw flag: a seller
    # 4 km away who also posts is still something you collect.
    mode = spec.get("shipping", "any")
    shipped = is_shipped(listing)
    if mode in ("only", "ship") and not shipped:
        return False, "collectable-not-shipped"
    if mode == "pickup" and shipped:
        return False, "too-far-shipped-only"

    if radius is not None and dist is not None and dist > float(radius) and not shipping_ok:
        return False, "too-far-and-no-shipping"

    price = listing.get("price_cents")
    if price is not None:
        lo, hi = spec.get("min_price"), spec.get("max_price")
        if lo is not None and price < int(lo) * 100:
            return False, "below-min-price"
        if hi is not None and price > int(hi) * 100:
            return False, "above-max-price"
    elif spec.get("require_price"):
        return False, "no-price"

    return True, None
