"""The free half of the matching: everything decidable without a model.

Kleinanzeigen already applied location, radius, price band and shipping
server-side, so this is mostly about keyword precision -- the noise that
actually ruins a feed is accessories and wanted-ads, not distant sellers.
"""
from __future__ import annotations

import re

# "Suche Playstation" is a wanted-ad; anzeige:angebote misses some of them.
_WANTED = re.compile(r"^\s*(suche|gesucht|ankauf|kaufe)\b", re.I)


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

    # Kleinanzeigen already applied versand:ja / versand:nein, but a search can
    # be edited after its listings were stored, so re-check rather than trust it.
    mode = spec.get("shipping", "any")
    if mode in ("only", "ship") and not shipping_ok:
        return False, "pickup-only-listing"
    if mode == "pickup" and shipping_ok:
        return False, "shipped-listing"

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
