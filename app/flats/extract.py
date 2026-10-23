"""Read a flat ad: what kind of offer it is, what it really costs, how to reach the landlord.

Division of labour, learned on the deals side (see app/ai.py):
- the model READS: offer type, rent figures written in prose ("864 € warm"),
  WBS, scam smell, how the landlord wants to be contacted;
- code DECIDES: the warm rent is assembled here from the portal's own fields
  first and the model's figures only where those are missing, and every
  budget/size/room comparison lives in match.py. The model never sees a budget.
- e-mail addresses and phone numbers come from regexes, not the model: a
  digit the model "corrects" would send a future application into the void.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from ..ai import MODEL, OLLAMA, TIMEOUT

log = logging.getLogger("flats.ai")

# Berlin Betriebskostenspiegel ballpark, €/m² per month. Only used when an ad
# gives no figure at all, and the result is always labelled "geschätzt".
EXTRA_PER_M2 = 2.2        # kalte Nebenkosten
HEATING_PER_M2 = 1.3

KINDS = ["wohnung", "wg_zimmer", "zwischenmiete", "tausch", "gesuch", "sonstiges"]
RISK = ["niedrig", "mittel", "hoch"]
TRI = ["ja", "nein", "unbekannt"]

SCHEMA = {
    "type": "object",
    "properties": {
        "art": {"type": "string", "enum": KINDS},
        "warmmiete": {"type": "number"},
        "kaltmiete": {"type": "number"},
        "nebenkosten": {"type": "number"},
        "heizkosten": {"type": "number"},
        "heizung_in_nebenkosten": {"type": "string", "enum": TRI},
        "wbs": {"type": "string", "enum": TRI},
        "betrug": {"type": "string", "enum": RISK},
        "kontakt": {"type": "string"},
        "kontakt_name": {"type": "string"},
        "notiz": {"type": "string"},
    },
    "required": ["art", "warmmiete", "kaltmiete", "nebenkosten", "heizkosten",
                 "heizung_in_nebenkosten", "wbs", "betrug", "kontakt", "kontakt_name", "notiz"],
}

SYSTEM = """Du liest ein Wohnungsinserat (deutsch oder franzoesisch) und gibst die Fakten als JSON zurueck.

art:
- "wohnung": eine ganze Wohnung zur regulaeren, unbefristeten Miete
- "wg_zimmer": nur ein Zimmer in einer WG oder Wohnung ("colocation", "chambre chez l'habitant")
- "zwischenmiete": befristet, Untermiete auf Zeit, moebliertes Apartment auf Zeit,
  "2-6 Monate", "Business Apartment", "Zwischenmiete", "bail mobilité", "location courte durée", "sous-location"
- "tausch": Wohnungstausch, die Wohnung gibt es nur gegen eine andere ("Tauschwohnung", "Swap")
- "gesuch": jemand SUCHT eine Wohnung, bietet keine an
- "sonstiges": Stellplatz, Gewerbe, Ferienwohnung, Werbung, kein konkretes Angebot

Zahlen in Euro pro Monat, nur wenn sie im Text oder in den Fakten stehen, sonst 0.
Nicht rechnen, nicht schaetzen, nur abschreiben.
- warmmiete: Gesamtmiete/Warmmiete inkl. Nebenkosten und Heizung, wenn so genannt ("864 € warm").
  "799 €/980 € warm" oder "799 kalt / 980 warm" heisst: kaltmiete 799, warmmiete 980.
  Franzoesisch: "charges comprises"/"CC" = warmmiete, "hors charges"/"HC" = kaltmiete, "provision sur charges" = nebenkosten.
  Der angezeigte Preis ist meist die Kaltmiete, nicht die Warmmiete.
- kaltmiete, nebenkosten, heizkosten: einzeln, wenn genannt
- heizung_in_nebenkosten: "ja" wenn die Nebenkosten die Heizung einschliessen

wbs: "ja" wenn ein WBS (Wohnberechtigungsschein) noetig ist, "nein" wenn ausdruecklich keiner, sonst "unbekannt".

betrug: Wie wahrscheinlich ist das ein Betrugsinserat?
- "hoch": Vermieter im Ausland, Schluessel per Post, Geld oder Kaution VOR Besichtigung,
  Kontakt nur per WhatsApp/E-Mail ins Ausland, Preis fuer die Lage unrealistisch niedrig,
  Airbnb-/Treuhand-Zahlung, Ausweiskopie vorab
- "mittel": einzelne Auffaelligkeiten, z.B. hohe Abloese/Abstand fuer Moebel, Maklerprovision vom Untermieter
- "niedrig": normales Angebot
Bewerbung, Besichtigung oder Zahlung ueber eine echte Vermietungsplattform oder Agentur ist normal und KEIN
Betrugszeichen (Frankreich: Manda, Lodgis, Studapart, Bien'ici, SeLoger, Foncia, Nexity ...; Dossier ueber
DossierFacile). Betrug ist, wenn ein angeblicher Privatvermieter Geld vorab will oder an eine Fremdseite verweist.

kontakt: in hoechstens 12 Woertern, WIE man sich laut Inserat bewerben soll, z.B.
"Nachricht über Kleinanzeigen mit Selbstauskunft", "E-Mail an den Vermieter", "Anruf ab 18 Uhr".
Steht nichts dazu: "".
kontakt_name: Name des Ansprechpartners, wenn genannt, sonst "".
notiz: hoechstens 10 Woerter, was man vor einer Bewerbung wissen sollte (z.B. "befristet 1 Jahr, 1.900 € Abstand").
Antworte nur mit JSON."""


def _prompt(flat: dict) -> str:
    facts = "\n".join(f"- {k}: {v}" for k, v in (flat.get("attrs") or {}).items())
    desc = (flat.get("description") or "")[:3500]
    return (f"TITEL: {flat['title']}\n"
            f"ANGEZEIGTER PREIS: {flat.get('list_price') or '-'} €\n"
            f"FAKTEN DES PORTALS:\n{facts or '-'}\n\nBESCHREIBUNG:\n{desc or '-'}")


async def read_ad(client: httpx.AsyncClient, flat: dict) -> dict | None:
    """The model's reading of one ad, or None if it could not be had (fails open)."""
    body = {
        "model": MODEL, "stream": False, "think": False, "format": SCHEMA,
        "options": {"temperature": 0.1},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": _prompt(flat)}],
    }
    try:
        r = await client.post(f"{OLLAMA}/api/chat", json=body, timeout=TIMEOUT)
        r.raise_for_status()
        out = json.loads(r.json()["message"]["content"])
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
        log.warning("read_ad %s failed: %s", flat.get("source_id"), exc)
        return None
    if out.get("art") not in KINDS:
        out["art"] = "wohnung"
    if out.get("betrug") not in RISK:
        out["betrug"] = "niedrig"
    return out


# --- contact channels, by regex ---------------------------------------------
_AT = r"\s*(?:@|\(at\)|\[at\]|\{at\}|\(ät\)| at )\s*"
_DOT = r"\s*(?:\.|\(dot\)|\[dot\]|\(punkt\)|\[punkt\])\s*"
_EMAIL = re.compile(rf"([A-Za-z0-9._%+-]+){_AT}([A-Za-z0-9-]+(?:{_DOT}[A-Za-z0-9-]+)*){_DOT}([A-Za-z]{{2,10}})\b")
_PHONE = re.compile(r"(?<![\d€.,])(\+49|0049|0)[\s/-]{0,2}(\(?\d{2,5}\)?(?:[\s/-]{0,2}\d){5,11})(?![\d€])")
# French numbers are always 0 + 9 digits, written in pairs: "06 12 34 56 78", "+33 6 12 34 56 78"
_PHONE_FR = re.compile(r"(?<![\d€.,])(\+33|0033|0)\s?\(?0?\)?\s?([1-9](?:[\s.-]?\d{2}){4})(?![\d€])")


def contacts(text: str, cc: str = "de") -> list[dict]:
    """E-mail and phone channels written into an ad, de-obfuscated, de-duplicated."""
    out, seen = [], set()
    for m in _EMAIL.finditer(text or ""):
        domain = re.sub(_DOT, ".", m.group(2))
        addr = f"{m.group(1)}@{domain}.{m.group(3)}".lower()
        if addr not in seen:
            seen.add(addr)
            out.append({"type": "email", "value": addr})
    for m in (_PHONE_FR if cc == "fr" else _PHONE).finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(2))
        if not (len(digits) == 9 if cc == "fr" else 7 <= len(digits) <= 12):
            continue
        num = ("+33" + digits) if cc == "fr" else "+49" + digits.lstrip("0")
        if num not in seen:
            seen.add(num)
            out.append({"type": "phone", "value": num})
    return out


def rent(flat: dict, reading: dict | None) -> dict:
    """Assemble cold/extra/heating/warm.

    Precedence: the portal's own fields, then figures the model copied out of
    the prose, then -- for the missing pieces only -- the per-m² ballpark.
    A warm figure below the cold one is a misread and is discarded.
    """
    r = reading or {}
    pos = lambda v: float(v) if isinstance(v, (int, float)) and v > 0 else None  # noqa: E731
    cold = flat.get("cold") or pos(r.get("kaltmiete"))
    extra = flat.get("extra") if flat.get("extra") is not None else pos(r.get("nebenkosten"))
    heating = flat.get("heating") if flat.get("heating") is not None else pos(r.get("heizkosten"))
    area = flat.get("area")

    warm, kind = flat.get("warm_attr"), "stated"
    if not warm:
        warm = pos(r.get("warmmiete"))
    if warm and cold and warm < cold:
        warm = None
    # the card price is cold or warm, never more than warm: a lower "Warmmiete"
    # field is a form slip (seen: field 800, ad text "Gesamtmiete: 980 €")
    if warm and flat.get("list_price") and warm < flat["list_price"]:
        warm = flat["list_price"]
    if not warm:
        # the card price stands in for Kaltmiete when nothing better exists
        base = cold or flat.get("list_price")
        if base and extra is not None and (heating is not None or r.get("heizung_in_nebenkosten") == "ja"):
            warm, kind = base + extra + (heating or 0), "sum"
        elif base and area:
            warm = base + (extra if extra is not None else EXTRA_PER_M2 * area) + \
                (heating if heating is not None else HEATING_PER_M2 * area)
            kind = "estimated"
        else:
            warm, kind = None, None
    return {"cold": cold, "extra": extra, "heating": heating,
            "warm": round(warm, 2) if warm else None, "warm_kind": kind}


KA_PORTAL = {"type": "kleinanzeigen", "label": "Kleinanzeigen-Nachricht", "how": "Nachricht über Kleinanzeigen"}


def enrich(flat: dict, detail: dict, reading: dict | None, now: float, portal: dict = KA_PORTAL,
           cc: str = "de") -> dict:
    """Merge card + detail page + model reading into the stored row.

    portal = the site's own message channel: {type, label, how (the default when the ad says nothing)}.
    """
    flat = {**flat, **{k: v for k, v in detail.items() if v is not None or k in ("cold", "extra", "heating")}}
    r = reading or {}
    money = rent(flat, reading)
    tausch = detail.get("tausch_attr")
    if r.get("art") == "tausch":
        tausch = 1
    elif tausch is None and reading:
        tausch = 0
    channels = []
    if detail.get("phone"):                      # the seller's own number from the ad page
        channels += contacts("0" + re.sub(r"^\+?49|^0", "", re.sub(r"[^\d+]", "", detail["phone"])))
    channels += [c for c in contacts("\n".join(x for x in (flat.get("title"), flat.get("description"),
                                                         detail.get("imprint")) if x), cc)
                 if c not in channels]
    channels.append({"type": portal["type"], "value": flat["url"], "label": portal["label"]})
    wbs = detail.get("wbs_attr") or {"ja": 1, "nein": 0}.get(r.get("wbs"))
    how = (r.get("kontakt") or "").strip()[:140] or portal["how"]
    if detail.get("premium_only"):
        how = "nur mit ImmoScout Plus: " + how
    return {
        **{k: flat.get(k) for k in ("source", "source_id", "url", "title", "image_url", "posted_at",
                                     "postal_code", "district", "area", "rooms", "list_price",
                                     "description", "lat", "lon", "address", "deposit",
                                     "available_from", "seller", "commercial", "attrs")},
        # only when the page said so: a None here would wipe a known age on upsert
        **({"seller_since": detail["seller_since"]} if detail.get("seller_since") else {}),
        **money,
        "wbs": wbs,
        "tausch": tausch,
        "kind": "wohnung" if r.get("art") == "tausch" else r.get("art"),
        "scam": r.get("betrug"),
        "ai_note": (r.get("notiz") or "").strip()[:140] or None,
        "contact": {
            "how": how,
            "name": (r.get("kontakt_name") or "").strip()[:80] or detail.get("contact_name") or flat.get("seller"),
            "channels": channels,
        },
        "detail_at": now,
        "judged_at": now if reading else None,
    }
