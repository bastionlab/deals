"""Build a search from a plain-language wish, then check it against reality.

A single-shot prompt writes plausible-looking filters that quietly do the wrong
thing: asked for "RTX 3060 oder 3090" it produced the terms rtx/geforce/nvidia,
which happily match an RTX 5070. The model has no way to know that without
seeing what the filters actually return.

So this runs the search it just wrote, feeds the real results back in next to
the original wish, and lets the model correct itself. Grounding the critique in
listings the user can see is the whole point -- the model is judging its own
output against evidence rather than guessing twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from . import ai, filters
from .config import BROWSER_HEADERS, HTTP_TIMEOUT
from .sources import kleinanzeigen as ka

log = logging.getLogger("compose")

MAX_ROUNDS = 3          # draft plus up to two corrections
PROBE_SPACING = 1.5     # seconds between probe fetches; this runs outside the poller's limiter
SAMPLE = 12             # how many listings the critic is shown

SPEC_PROPS = {
    "name": {"type": "string"},
    "keyword": {"type": "string"},
    "terms": {"type": "array", "items": {"type": "string"}},
    "exclude": {"type": "array", "items": {"type": "string"}},
    "minPrice": {"type": ["integer", "null"]},
    "maxPrice": {"type": ["integer", "null"]},
    "radius": {"type": "integer"},
    "shipping": {"type": "string", "enum": ["any", "ship", "pickup"]},
    "intent": {"type": "string"},
    "note": {"type": "string"},
}
DRAFT_SCHEMA = {"type": "object", "properties": SPEC_PROPS, "required": list(SPEC_PROPS)}
CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "problem": {"type": "string"},
        **SPEC_PROPS,
    },
    "required": ["ok", "problem", *SPEC_PROPS],
}

DRAFT_SYSTEM = """Du baust Suchfilter fuer Kleinanzeigen.de. Aus einer Beschreibung in Alltagssprache machst du ein Filter-Objekt. Antworte NUR mit JSON.

FELDER

name: kurzer Slug, 1-2 Woerter, klein, ohne Umlaute.

keyword: exakt das, was man in die Kleinanzeigen-Suchmaske tippt. 2-3 Woerter, nur Produktkategorie plus das wichtigste Merkmal.
VERBOTEN im keyword: Zustand (gebraucht, neu, defekt), Wertung (guenstig, gut, top), Preise, Orte, Versandwoerter.
Je kuerzer, desto mehr Treffer. Lieber zu allgemein als zu eng.

terms: eine Anzeige wird nur behalten, wenn Titel oder Beschreibung MINDESTENS EINEN dieser Schnipsel enthaelt.
Nennt der Nutzer konkrete Modelle ("3060 oder 3090"), dann gehoeren GENAU diese Modelle hier hinein,
in allen ueblichen Schreibweisen ("rtx 3060", "3060"), und NICHT die Oberkategorie ("rtx", "nvidia"):
die Oberkategorie wuerde jedes andere Modell durchlassen.
Niemals Absichten oder Taetigkeiten. Alles klein. 3-8 Eintraege.

exclude: eine Anzeige wird verworfen, sobald sie EINEN dieser Schnipsel enthaelt.
Zubehoer, Ersatzteile, defekte Ware, Tauschgesuche. Alles klein. 4-10 Eintraege.
Niemals Zustands- oder Wertungswoerter ausschliessen (nicht "neu", nicht "gebraucht").

minPrice / maxPrice: ganze Euro oder null. Nie 0 schreiben, dann null.
radius: km um Berlin, Standard 10.
shipping: "pickup" nur bei Abholung, "ship" wenn verschickt werden soll, sonst "any".

intent: ein bis zwei deutsche Saetze, was WIRKLICH als Treffer zaehlt. Hier gehoeren harte Bedingungen hin,
die Stichwoerter nicht ausdruecken koennen: Mindestgroessen ("mindestens 6 TB", "mindestens 27 Zoll"),
erlaubte Modelle ("nur RTX 3060 oder RTX 3090"), ausgeschlossene Bauformen (keine Gehaeuse, keine Komplett-PCs).
Zahlen immer als "mindestens N TB" oder "maximal N GB".
Gewuenschte Farben, Groessen oder Varianten des Nutzers gehoeren immer in den intent.
Schreibe NUR Bedingungen, die der Nutzer verlangt hat. Erfinde keine Produkteigenschaften
("mit Display", "Smartwatch"), die du nicht sicher weisst: ein falscher Satz hier sortiert jedes
echte Angebot aus. Eine genannte Modellnummer schliesst ihre Varianten ein ("3090" auch "3090 Ti"),
ausser der Nutzer schliesst sie aus.

note: ein kurzer deutscher Satz zu deinen Annahmen."""

CRITIQUE_SYSTEM = """Du pruefst einen Kleinanzeigen-Suchfilter gegen das, was er tatsaechlich liefert.

Du bekommst: den urspruenglichen WUNSCH des Nutzers, den aktuellen FILTER und die ECHTEN TREFFER,
die dieser Filter gerade auf Kleinanzeigen erzeugt hat.

Vergleiche die Treffer mit dem Wunsch und entscheide:
- ok=true, wenn die Treffer zum Wunsch passen und nichts Offensichtliches fehlt.
- ok=false, wenn etwas systematisch falsch laeuft. Typische Fehler:
  * Treffer, die der Nutzer nie wollte (falsches Modell, falsche Kategorie) -> terms zu allgemein,
    oder intent zu vage. Trage die konkret gewuenschten Modelle in terms ein.
  * gar keine Treffer -> keyword zu speziell oder Preisgrenze zu niedrig. Mach das keyword kuerzer.
  * fast alles wird aussortiert -> terms oder exclude zu streng.

problem: ein deutscher Satz, was schieflaeuft (oder warum alles in Ordnung ist).
Gib IMMER das vollstaendige Filter-Objekt zurueck, auch bei ok=true: unveraendert, wenn nichts zu
aendern ist. Antworte nur mit JSON."""

SELF_DEFEATING = {
    "neu", "neuwertig", "gebraucht", "used", "new", "gut", "guenstig", "günstig",
    "schnaeppchen", "schnäppchen", "top", "ovp", "originalverpackt", "zubehör",
    "zubehoer", "accessory", "teile", "inkl", "set",
}


# "rtx 3060 oder 3090" returns nothing: the search box matches the literal
# string, so conjunctions and long phrases are what produce an empty page.
_FILLER = {"oder", "und", "bzw", "bzw.", "sowie", "mit", "fuer", "für", "in", "ab", "bis"}


def tidy_keyword(kw: str, max_words: int = 3) -> str:
    words = [w for w in str(kw).strip().lower().split() if w not in _FILLER]
    return " ".join(words[:max_words])


def _clean(xs) -> list[str]:
    return [t for t in ({str(x).strip().lower() for x in xs if str(x).strip()}) if t]


def sanitise(raw: dict) -> dict:
    """The model puts words like neu/gebraucht in exclude, where they delete most
    results, and sometimes repeats a term in both lists, which cancels it out."""
    terms = _clean(raw.get("terms") or [])
    exclude = [x for x in _clean(raw.get("exclude") or [])
               if x not in SELF_DEFEATING and x not in terms]
    price = lambda v: int(v) if isinstance(v, (int, float)) and v > 0 else None
    name = "".join(c if c.isalnum() or c == "-" else "-"
                   for c in str(raw.get("name", "")).strip().lower()).strip("-")
    return {
        "name": name[:48],
        "keyword": tidy_keyword(raw.get("keyword", ""))[:120],
        "terms": sorted(terms)[:12],
        "exclude": sorted(exclude)[:15],
        "minPrice": price(raw.get("minPrice")),
        "maxPrice": price(raw.get("maxPrice")),
        "radius": max(0, min(int(raw.get("radius") or 10), 200)),
        "shipping": raw["shipping"] if raw.get("shipping") in ("any", "ship", "pickup") else "any",
        "intent": str(raw.get("intent", "")).strip()[:600],
        "note": str(raw.get("note", "")).strip()[:300],
    }


def to_spec(form: dict) -> dict:
    """Form shape -> the spec the poller and filters understand."""
    return {
        "keyword": form["keyword"],
        "terms": form["terms"] or [form["keyword"]],
        "exclude": form["exclude"],
        "min_price": form["minPrice"],
        "max_price": form["maxPrice"],
        "radius_km": form["radius"],
        "location_id": "l3331",
        "shipping": form["shipping"],
        "intent": form["intent"],
        "min_score": ai.DEFAULT_MIN_SCORE,
        "offers_only": True,
    }


async def _chat(client: httpx.AsyncClient, system: str, user: str, schema: dict) -> dict:
    r = await client.post(f"{ai.OLLAMA}/api/chat", timeout=ai.TIMEOUT, headers={"X-Job": "deals-message"}, json={
        "model": ai.MODEL, "stream": False, "think": False, "format": schema,
        "options": {"temperature": 0.2},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    })
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


async def probe(client: httpx.AsyncClient, form: dict) -> dict:
    """Run the search for real and report what it would have done with the page."""
    spec = to_spec(form)
    url = ka.build_url(spec, page=1)
    try:
        resp = await client.get(url, headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT,
                                follow_redirects=True)
        ads = ka.parse(resp.text) if resp.status_code == 200 else []
        status = resp.status_code
    except httpx.HTTPError as exc:
        log.warning("probe fetch failed: %s", exc)
        return {"url": url, "status": 0, "found": 0, "kept": [], "dropped": [], "judged": {}}

    kept, dropped = [], []
    for ad in ads:
        ok, why = filters.check(ad, spec)
        (kept if ok else dropped).append({**ad, "drop_reason": why})

    judged = await ai.judge(client, spec["intent"], kept[:SAMPLE]) if spec["intent"] else {}
    for ad in kept:
        v = judged.get(ad["source_id"])
        ad["ai_ok"] = ai.passes(v, ai.DEFAULT_MIN_SCORE) if v is not None else None
        ad["ai_reason"] = v["reason"] if v else None
    return {"url": url, "status": status, "found": len(ads), "kept": kept, "dropped": dropped}


def _evidence(wish: str, form: dict, result: dict) -> str:
    """What the critic sees: the wish, the filter, and the listings it produced."""
    def block(items: list[dict], key: str | None = None) -> str:
        if not items:
            return "  (keine)"
        out = []
        for a in items[:SAMPLE]:
            tail = f"   [{a.get(key)}]" if key and a.get(key) else ""
            out.append(f"  - {a['title'][:90]}{tail}")
        return "\n".join(out)

    would_notify = [a for a in result["kept"] if a.get("ai_ok") is not False]
    ai_dropped = [a for a in result["kept"] if a.get("ai_ok") is False]
    return "\n\n".join([
        f"WUNSCH DES NUTZERS:\n{wish}",
        f"AKTUELLER FILTER:\n{json.dumps(form, ensure_ascii=False)}",
        f"ECHTE TREFFER ({result['found']} Anzeigen auf Seite 1 dieser Suche)",
        f"WUERDEN GEMELDET ({len(would_notify)}):\n{block(would_notify)}",
        f"VON DER KI AUSSORTIERT ({len(ai_dropped)}):\n{block(ai_dropped, 'ai_reason')}",
        f"VON DEN STICHWOERTERN VERWORFEN ({len(result['dropped'])}):"
        f"\n{block(result['dropped'], 'drop_reason')}",
    ])


def _best(attempts: list[dict]) -> dict | None:
    """Most relevant hits wins; ties go to the tighter search.

    If nothing matched in any round the filter is over-constrained, so fall back
    to whichever round at least found listings to work with.
    """
    if not attempts:
        return None
    if any(a["kept"] for a in attempts):
        return max(attempts, key=lambda a: (a["kept"], -a["found"]))
    return max(attempts, key=lambda a: a["found"])


async def run(wish: str) -> AsyncIterator[dict]:
    """Yield progress events; the last one carries the finished form."""
    async with httpx.AsyncClient() as client:
        yield {"step": "draft"}
        try:
            form = sanitise(await _chat(client, DRAFT_SYSTEM, wish, DRAFT_SCHEMA))
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            yield {"step": "error", "message": f"Modell nicht erreichbar: {exc}"}
            return
        yield {"step": "drafted", "form": form}

        # Each round is a candidate, not an improvement: the critic can tighten a
        # working filter into one that returns almost nothing. Keep them all and
        # hand back the best, judged on what it actually produced.
        attempts: list[dict] = []
        result = None
        for rnd in range(MAX_ROUNDS):
            yield {"step": "probing", "round": rnd + 1, "keyword": form["keyword"]}
            result = await probe(client, form)
            would = [a for a in result["kept"] if a.get("ai_ok") is not False]
            attempts.append({"form": form, "result": result, "kept": len(would),
                             "found": result["found"], "round": rnd + 1})
            yield {"step": "probed", "round": rnd + 1, "found": result["found"],
                   "kept": len(would), "url": result["url"],
                   "samples": [a["title"] for a in would[:5]]}

            if result["status"] not in (200, 0) or rnd == MAX_ROUNDS - 1:
                break
            yield {"step": "checking", "round": rnd + 1}
            evidence = _evidence(wish, form, result)
            if result["found"] == 0:
                evidence += (
                    "\n\nZWINGEND: Diese Suche liefert NULL Anzeigen, das keyword ist zu speziell."
                    " Kuerze es auf hoechstens zwei allgemeine Woerter (nur Produktkategorie)."
                    " Die konkreten Modelle gehoeren in terms, nicht ins keyword."
                )
            try:
                crit = await _chat(client, CRITIQUE_SYSTEM, evidence, CRITIQUE_SCHEMA)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
                log.warning("critique failed: %s", exc)
                yield {"step": "verdict", "ok": None,
                       "problem": "Pruefung nicht moeglich, Entwurf bleibt unveraendert."}
                break
            if crit.get("ok") and result["found"]:
                yield {"step": "verdict", "ok": True, "problem": crit.get("problem", "")}
                break

            fixed = sanitise(crit)
            # the critic often agrees something is wrong and then hands back the
            # same filter, so force the one correction we can make ourselves
            if result["found"] == 0 and fixed["keyword"] == form["keyword"]:
                fixed["keyword"] = tidy_keyword(form["keyword"], max_words=2)
            changed = [k for k in ("keyword", "terms", "exclude", "minPrice", "maxPrice", "intent")
                       if form.get(k) != fixed.get(k)]
            if not changed:
                yield {"step": "verdict", "ok": None,
                       "problem": crit.get("problem") or "Keine Verbesserung gefunden."}
                break
            yield {"step": "verdict", "ok": False, "problem": crit.get("problem", ""),
                   "changed": changed}
            form = {**fixed, "note": form["note"]}
            await asyncio.sleep(PROBE_SPACING)

        best = _best(attempts)
        if best and best["round"] != len(attempts):
            yield {"step": "reverted", "round": best["round"],
                   "problem": f"Runde {best['round']} war besser ({best['kept']} statt "
                              f"{attempts[-1]['kept']} Treffern) — die wird genommen."}
        form = best["form"] if best else form
        preview = [] if not best else [
            {"title": a["title"],
             "price_eur": None if a["price_cents"] is None else a["price_cents"] / 100,
             "url": a["url"], "image_url": a["image_url"], "distance_km": a["distance_km"],
             "ai_reason": a.get("ai_reason")}
            for a in best["result"]["kept"] if a.get("ai_ok") is not False][:8]
        yield {"step": "done", "form": form, "preview": preview}
