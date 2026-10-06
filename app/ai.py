"""Relevance judging with the local model in CT 105.

Keyword filters cannot express "at least 6 TB", which is how 2 TB drives ended
up in a search for 6 TB ones. This asks qwen3.5:9b to judge each candidate
against a plain-language statement of what the search is actually after.

The small warm model rather than qwen3.6: only one model fits in 12 GB of VRAM
and warm-model keeps this one resident, so judging never evicts the chat model.

`think: false` is load-bearing -- with thinking on the same call costs roughly
25x the tokens and 10x the wall clock.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger("ai")

OLLAMA = "http://192.168.1.55:11434"
MODEL = "qwen3.5:9b"
BATCH = 6
TIMEOUT = 180.0
DEFAULT_MIN_SCORE = 50

# No id field on purpose. Asking the model to echo an identifier was the single biggest source of lost
# verdicts: granite returned "1", qwen3.5 returned "[1]", the listing's title, or even ",". A verdict that
# cannot be matched back fails open (the listing is notified unjudged), so the verdicts come back as a plain
# list in the order the listings were given and are zipped by position -- and a batch whose length does not
# match is re-judged one listing at a time, where position cannot be wrong.
SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "score": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["ok", "score", "reason"],
    }}},
    "required": ["verdicts"],
}

SYSTEM = """Du pruefst Kleinanzeigen-Inserate gegen einen Suchwunsch.

Antworte mit genau einem Verdict pro Inserat, in derselben Reihenfolge wie die Inserate,
als Liste "verdicts". Ueberspringe kein Inserat und fuege keines hinzu.

Fuer JEDES Inserat:
- ok: true nur wenn das Inserat den Suchwunsch WIRKLICH erfuellt
- score: 0-100 wie gut es passt; wenn ok=false immer 0
- reason: max 8 Woerter, deutsch, warum

Ort, Entfernung, Versand/Abholung und Preis sind schon geprueft und spielen hier
KEINE Rolle. Bewerte nur, ob das Produkt selbst dem Suchwunsch entspricht.

Sei streng. Typische Gruende fuer ok=false:
- Zahlenangaben verfehlt (zu wenig TB/GB, falsche Groesse, falsches Modell)
- es ist nur Zubehoer, ein Gehaeuse, eine Halterung oder ein Leergeraet
- es ist ein anderes Produkt aus derselben Kategorie
- Zustand passt nicht zum Wunsch
Wenn eine geforderte Zahl im Inserat nicht auftaucht, nimm NICHT an, dass sie erfuellt ist.
Ein genanntes Modell schliesst seine Varianten mit ein: "RTX 3090" passt auch auf "RTX 3090 Ti",
"OC", "Super" usw. Falsches Modell heisst eine andere Nummer, nicht ein Zusatz zur selben Nummer.
Varianten nur ablehnen, wenn der Suchwunsch sie ausdruecklich ausschliesst.
"OVP" heisst Originalverpackung: die Ware ist MIT Karton dabei, nicht nur der Karton.
Fehlt eine Zustandsangabe, ist das kein Grund fuer ok=false. Defekt ist nur, was das Inserat
ausdruecklich als defekt oder nicht funktionierend beschreibt.
Mehrere Stueck, Bundles oder Sets sind in Ordnung, solange das Gesuchte dabei ist.
Im Zweifel ok=true: ein verpasstes Schnaeppchen ist schlimmer als ein Treffer zu viel.
Antworte nur mit JSON."""


# --- deterministic numeric guard -------------------------------------------
# The model is fluent but cannot be trusted with numbers: in one production run
# it kept a 4 TB drive for a "mindestens 6 TB" search with score 100, and kept a
# 512 GB SSD whose own reason read "1 TB minimum nicht erreicht". Any size or
# inch requirement in the intent is therefore enforced in code, against the
# listing title, before the model's opinion is considered.

_UNIT = {"tb": ("size", 1000.0), "gb": ("size", 1.0), "mb": ("size", 0.001),
         "zoll": ("inch", 1.0), '"': ("inch", 1.0)}
_LIMIT_RE = re.compile(r"(mindestens|min\.?|ab|maximal|max\.?|hoechstens|höchstens)\s*"
                       r"(\d+(?:[.,]\d+)?)\s*(tb|gb|mb|zoll|\")", re.I)
_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(tb|gb|mb|zoll|\")", re.I)
_AT_MOST = {"maximal", "max", "max.", "hoechstens", "höchstens"}


def _norm(num: float, unit: str) -> tuple[str, float] | None:
    kind_scale = _UNIT.get(unit.strip().lower())
    return (kind_scale[0], num * kind_scale[1]) if kind_scale else None


def numeric_reject(intent: str, title: str) -> str | None:
    """Return a reason if the title's own numbers violate the intent.

    Uses the LARGEST matching value in the title, which fails open: a bundle
    listing ("RTX 3080 10GB + 32GB RAM") is kept for the model to judge rather
    than silently dropped on the wrong number.
    """
    lim = _LIMIT_RE.search(intent or "")
    if not lim:
        return None
    want = _norm(float(lim.group(2).replace(",", ".")), lim.group(3))
    if not want:
        return None
    kind, bound = want
    at_most = lim.group(1).lower().rstrip(".") in _AT_MOST

    found = [v for m in _VALUE_RE.finditer(title or "")
             if (v := _norm(float(m.group(1).replace(",", ".")), m.group(2))) and v[0] == kind]
    if not found:
        return None                      # nothing stated -> let the model decide
    best = max(x[1] for x in found)
    unit = lim.group(3)
    if at_most and best > bound:
        return f"{lim.group(2)} {unit} überschritten"
    if not at_most and best < bound:
        shown = f"{best / 1000:g} TB" if kind == "size" and best >= 1000 else (
            f"{best:g} GB" if kind == "size" else f"{best:g} Zoll")
        return f"{shown} < {lim.group(2)} {unit}"
    return None


def _prompt(intent: str, batch: list[dict]) -> str:
    lines = []
    for n, li in enumerate(batch, 1):
        desc = (li.get("description") or "")[:200]
        lines.append(f"[{n}] {li['title']} | {desc}")
    return f"SUCHWUNSCH: {intent}\n\nINSERATE:\n" + "\n".join(lines)


async def _one_batch(client: httpx.AsyncClient, intent: str, batch: list[dict]) -> dict[str, dict]:
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0.1},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _prompt(intent, batch)},
        ],
    }
    r = await client.post(f"{OLLAMA}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    verdicts = json.loads(r.json()["message"]["content"]).get("verdicts") or []
    if len(verdicts) != len(batch):
        if len(batch) == 1:
            log.warning("no usable verdict for %s", batch[0]["source_id"])
            return {}
        log.info("%d verdicts for %d listings — re-judging one at a time", len(verdicts), len(batch))
        out: dict[str, dict] = {}
        for li in batch:
            out.update(await _one_batch(client, intent, [li]))
        return out
    return {li["source_id"]: {
        "ok": bool(v.get("ok")),
        "score": max(0, min(100, int(v.get("score") or 0))),
        "reason": (v.get("reason") or "").strip()[:120],
    } for li, v in zip(batch, verdicts)}


async def judge(client: httpx.AsyncClient, intent: str, listings: list[dict]) -> dict[str, dict]:
    """Judge listings against the search intent, keyed by source_id.

    Fails open: a listing the model never judged comes back missing and the
    caller lets it through. Ollama being down must not swallow a bargain.
    """
    if not intent.strip() or not listings:
        return {}
    verdicts: dict[str, dict] = {}
    for i in range(0, len(listings), BATCH):
        batch = listings[i:i + BATCH]
        try:
            verdicts.update(await _one_batch(client, intent, batch))
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            log.warning("judge failed for %d listings: %s", len(batch), exc)

    # the guard overrides the model, including listings it never answered for
    for li in listings:
        why = numeric_reject(intent, li.get("title") or "")
        if why:
            verdicts[li["source_id"]] = {"ok": False, "score": 0, "reason": why}
    return verdicts


def passes(verdict: dict | None, min_score: int = DEFAULT_MIN_SCORE) -> bool:
    """Unjudged -> True (fail open).

    Both conditions are required because the model contradicts itself on
    numbers: it will write "4 TB" in the reason, set score 0 and still say
    ok=true. The score carried the real signal in every test batch.
    """
    if verdict is None:
        return True
    return bool(verdict["ok"]) and verdict["score"] >= min_score
