"""Does a flat fit a search? Pure code, no model, no I/O.

Three outcomes:
- hard: outside what you asked for (area, size, rooms, budget, Tausch, WBS), or no price
        at all ("Preis auf Anfrage") -> not in the feed at all
- soft: inside, but not worth looking at by default (a Gesuch, a WG room, a short let,
        a scam smell, or only an ESTIMATED warm rent over budget) -> "Aussortiert", one click away
- ok

Anything unknown passes (except the price, see above): an ad that does not state its size must not vanish
because of it. That is the same fail-open rule the deals judge follows.
"""
from __future__ import annotations

from . import geo

TEMPORARY = {"wg_zimmer": "nur ein WG-Zimmer", "zwischenmiete": "Zwischenmiete / befristet"}
NOT_AN_OFFER = {"gesuch": "Gesuch, kein Angebot", "sonstiges": "kein Wohnungsangebot"}


def _eur(v: float) -> str:
    return f"{v:,.0f} €".replace(",", ".")


def check(flat: dict, spec: dict) -> tuple[str, str | None, float | None]:
    """-> (ok | soft | hard, reason, km from the area's centre)."""
    km = None
    if flat.get("lat") is not None and flat.get("lon") is not None and spec.get("lat") is not None:
        inside, km = geo.in_area(spec, flat["lat"], flat["lon"])
        km = round(km, 1)
        if not inside:
            return "hard", "außerhalb des Suchgebiets", km

    area, rooms = flat.get("area"), flat.get("rooms")
    if area and spec.get("min_area") and area < spec["min_area"]:
        return "hard", f"{area:g} m² < {spec['min_area']} m²", km
    if area and spec.get("max_area") and area > spec["max_area"]:
        return "hard", f"{area:g} m² > {spec['max_area']} m²", km
    if rooms and spec.get("min_rooms") and rooms < spec["min_rooms"]:
        return "hard", f"{rooms:g} Zi. < {spec['min_rooms']:g}", km
    if rooms and spec.get("max_rooms") and rooms > spec["max_rooms"]:
        return "hard", f"{rooms:g} Zi. > {spec['max_rooms']:g}", km

    tausch = spec.get("tausch", "exclude")
    if tausch == "exclude" and flat.get("tausch") == 1:
        return "hard", "Tauschwohnung", km
    if tausch == "only" and flat.get("tausch") != 1:
        return "hard", "kein Tausch", km

    wbs = spec.get("wbs", "any")
    if wbs == "without" and flat.get("wbs") == 1:
        return "hard", "WBS erforderlich", km
    if wbs == "only" and flat.get("wbs") != 1:
        return "hard", "kein WBS-Angebot", km

    if not any(flat.get(k) for k in ("warm", "cold", "list_price")):
        return "hard", "kein Preis angegeben", km

    budget = spec.get("max_warm")
    warm, how = flat.get("warm"), flat.get("warm_kind")
    if budget:
        if warm is not None and how in ("stated", "sum") and warm > budget:
            return "hard", f"{_eur(warm)} warm > {_eur(budget)}", km
        if warm is None and flat.get("list_price") and flat["list_price"] > budget:
            return "hard", f"{_eur(flat['list_price'])} > {_eur(budget)}", km
    if spec.get("min_warm") and warm is not None and how in ("stated", "sum") and warm < spec["min_warm"]:
        return "hard", f"{_eur(warm)} warm < {_eur(spec['min_warm'])}", km

    kind = flat.get("kind")
    if kind in NOT_AN_OFFER:
        return "soft", NOT_AN_OFFER[kind], km
    if kind in TEMPORARY and not spec.get("allow_temporary"):
        return "soft", TEMPORARY[kind], km
    if flat.get("scam") == "hoch":
        return "soft", "Betrugsverdacht" + (f": {flat['ai_note']}" if flat.get("ai_note") else ""), km
    if budget and warm is not None and how == "estimated" and warm > budget:
        return "soft", f"geschätzt {_eur(warm)} warm > {_eur(budget)}", km
    return "ok", None, km


def card_could_match(card: dict, spec: dict) -> bool:
    """Cheap pre-check on a result card before spending a detail fetch and a model call.

    Only rules the card can answer with certainty: the card price is never more
    than the warm rent, and m²/rooms on the card are the portal's own fields.
    """
    p = card.get("list_price")
    if spec.get("max_warm") and p and p > spec["max_warm"]:
        return False
    area, rooms = card.get("area"), card.get("rooms")
    if area and spec.get("min_area") and area < spec["min_area"]:
        return False
    if area and spec.get("max_area") and area > spec["max_area"]:
        return False
    if rooms and spec.get("min_rooms") and rooms < spec["min_rooms"]:
        return False
    if rooms and spec.get("max_rooms") and rooms > spec["max_rooms"]:
        return False
    return True
