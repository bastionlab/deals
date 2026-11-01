"""What the model does for jobs: portal keywords from a wish, and reading one posting.

Same division of labour as deals and flats: the model reads (work mode, level,
agency, language, how well it fits the CV); code decides what is filtered.
One posting per call -- postings are long, and a batch is where small models
mix verdicts up.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from ..ai import MODEL, OLLAMA, TIMEOUT
from . import cv

log = logging.getLogger("jobs.ai")

# --- keywords for the portals -----------------------------------------------

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}},
        "level": {"type": "array", "items": {"type": "string", "enum": ["junior", "mid", "senior", "lead"]}},
        "note": {"type": "string"},
    },
    "required": ["keywords", "level", "note"],
}

SUGGEST_SYSTEM = """Du hilfst bei einer Jobsuche. Aus dem WUNSCH (und dem Lebenslauf-Profil, falls vorhanden)
machst du Suchbegriffe fuer Jobportale (StepStone, LinkedIn, Arbeitsagentur, XING).

- keywords: 2-4 kurze Suchbegriffe, je 1-3 Woerter, genau so wie Stellentitel in Anzeigen heissen.
  Deutsch und Englisch gemischt, wenn beides ueblich ist (z.B. "Backend Entwickler", "Backend Developer").
  KEINE Ortsnamen, KEIN "remote", KEINE Woerter wie "Job", "Stelle", "m/w/d", keine Firmennamen.
  Jedes Portal sucht woertlich: lieber "Python Developer" als "Python Backend Developer Cloud".
  Mindestens ein Begriff auf Deutsch. Ist die Rolle durch ein Werkzeug oder Fach klar bestimmt, nimm
  zusaetzlich genau EINEN Ein-Wort-Begriff dazu (z.B. "Python", "SAP", "Buchhaltung"): die Arbeitsagentur
  verlangt jedes Wort und findet mit englischen Mehrwort-Titeln oft gar nichts.
- level: welche Karrierestufen passen (aus Wunsch und Erfahrung), leer wenn offen
- note: 1 Satz, was du angenommen hast
Antworte nur mit JSON.

Beispiel: WUNSCH "irgendwas mit Daten, gerne Python, kein Consulting" + Profil Data Analyst 3 Jahre
-> {"keywords": ["Data Analyst", "Datenanalyst", "Data Engineer", "SQL"], "level": ["mid"],
    "note": "Data-Rollen auf Mid-Level; Consulting wird beim Lesen aussortiert."}"""

_JUNK = re.compile(r"(?i)\b(remote|homeoffice|home office|hybrid|berlin|m/w/d|job|jobs|stelle|stellen)\b")


async def suggest(client: httpx.AsyncClient, wish: str, profile: dict | None) -> dict:
    body = {
        "model": MODEL, "stream": False, "think": False, "format": SUGGEST_SCHEMA,
        "options": {"temperature": 0.2},
        "messages": [{"role": "system", "content": SUGGEST_SYSTEM},
                     {"role": "user", "content": f"WUNSCH: {wish or '-'}\n\nPROFIL:\n{cv.brief(profile or {})}"}],
    }
    r = await client.post(f"{OLLAMA}/api/chat", json=body, headers={"X-Job": "jobs"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = json.loads(r.json()["message"]["content"])
    out["keywords"] = tidy_keywords(out.get("keywords") or [])
    return out


def tidy_keywords(words: list[str]) -> list[str]:
    """Portals match literally: strip what the model adds anyway, cap at 3 words, dedupe."""
    seen, out = set(), []
    for w in words:
        w = " ".join(_JUNK.sub(" ", w or "").split()[:3])
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out[:4]


# --- one posting against CV + wish ------------------------------------------

LEVELS = ("junior", "mid", "senior", "lead", "unbekannt")
MODES = ("onsite", "hybrid", "remote", "unbekannt")
KINDS = ("vollzeit", "teilzeit", "werkstudent", "praktikum", "ausbildung", "freelance", "minijob")

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "arbeitsmodell": {"type": "string", "enum": list(MODES)},
        "remote_raum": {"type": "string"},
        "level": {"type": "string", "enum": list(LEVELS)},
        "anstellung": {"type": "array", "items": {"type": "string", "enum": list(KINDS)}},
        "vermittler": {"type": "boolean"},
        "sprache": {"type": "string", "enum": ["de", "en", "fr"]},
        "deutsch": {"type": "string", "enum": ["keins", "grundkenntnisse", "gut", "fliessend", "unbekannt"]},
        "gehalt": {"type": "string"},
        "bewerbung": {"type": "string"},
        "fit": {"type": "integer"},
        "passt": {"type": "array", "items": {"type": "string"}},
        "fehlt": {"type": "array", "items": {"type": "string"}},
        "fazit": {"type": "string"},
    },
    "required": ["arbeitsmodell", "remote_raum", "level", "anstellung", "vermittler", "sprache", "deutsch", "gehalt",
                 "bewerbung", "fit", "passt", "fehlt", "fazit"],
}

READ_SYSTEM = """Du liest eine Stellenanzeige fuer eine Person, die einen Job sucht.
Du bekommst ihr PROFIL (aus dem Lebenslauf), ihren WUNSCH und die ANZEIGE.

Fakten aus der Anzeige (nur was dort steht, sonst "unbekannt" bzw. ""):
- arbeitsmodell: onsite (nur vor Ort), hybrid (teils Homeoffice / einige Tage Buero), remote (voll remote)
- remote_raum: wenn remote: aus welchem Raum, z.B. "Deutschland", "EU", "weltweit", "CET +-2h", sonst ""
- level: junior | mid | senior | lead (Teamleitung, Head of, Principal) | unbekannt
- anstellung: welche Anstellungsarten die Anzeige anbietet: vollzeit, teilzeit, werkstudent (fuer
  eingeschriebene Studierende), praktikum, ausbildung, freelance, minijob. Leer wenn nicht erkennbar.
  "Full-time" = vollzeit. Eine normale Festanstellung ist KEINE Werkstudentenstelle.
  Franzoesisch: CDI/CDD = vollzeit (oder teilzeit, wenn "temps partiel"), "stage" = praktikum,
  "alternance"/"apprentissage" = ausbildung, "freelance"/"indépendant" = freelance.
- vermittler: true wenn die Anzeige von einer Personalvermittlung, Zeitarbeit oder einem Recruiter
  FUER einen ungenannten Kunden ist ("unser Kunde", "im Auftrag", "Arbeitnehmerueberlassung"). Sonst false.
  Stellt die genannte Firma selbst ein, ist es KEIN Vermittler - auch nicht bei Beratungen, IT-Dienstleistern
  oder Konzernen (Accenture, Capgemini, Deloitte ... sind Arbeitgeber).
- sprache: Sprache der Anzeige (de, en, fr)
- deutsch: welches Deutschniveau verlangt wird ("keins" bei franzoesischen Anzeigen ohne Deutsch-Anforderung)
- gehalt: Gehaltsangabe woertlich, wenn genannt, sonst ""
- bewerbung: hoechstens 12 Woerter, WIE man sich laut Anzeige bewirbt (z.B. "Online-Formular, Anschreiben optional",
  "E-Mail an jobs@firma.de mit Zeugnissen"), sonst ""

Passung:
- fit: 0-100, wie gut PROFIL und WUNSCH zur Stelle passen
  90+: genau die Rolle, Kenntnisse decken die Muss-Anforderungen
  70-89: passt gut, kleine Luecken
  50-69: verwandt, deutliche Luecken oder andere Ausrichtung
  unter 50: andere Rolle/Branche, falsches Level, oder widerspricht dem WUNSCH
  Was der WUNSCH ausschliesst (z.B. "kein Consulting", "keine Agentur"), ergibt fit unter 30.
  Verlangt der WUNSCH eine Anstellungsart (z.B. Werkstudent, Praktikum, Teilzeit) und die Anzeige bietet
  sie nicht, ergibt das fit unter 30 - egal wie gut die Kenntnisse passen.
- passt: 1-3 kurze Punkte (je max 6 Woerter), was konkret passt
- fehlt: 0-3 kurze Punkte, welche Muss-Anforderungen das PROFIL nicht erfuellt. Keine Erfindungen.
- fazit: max 12 Woerter, deutsch

Ort, Datum und Portal sind schon geprueft und spielen fuer fit KEINE Rolle.
Antworte nur mit JSON."""


def _prompt(job: dict, profile: dict, wish: str) -> str:
    facts = [f"Titel: {job['title']}", f"Firma: {job.get('company') or '-'}",
             f"Ort: {job.get('location') or '-'}"]
    if job.get("salary_text"):
        facts.append(f"Gehalt laut Portal: {job['salary_text']}")
    for k, v in (job.get("attrs") or {}).items():
        if isinstance(v, (str, int, float)) and len(str(v)) < 120:
            facts.append(f"{k}: {v}")
    desc = (job.get("description") or "")[:6000]
    return (f"PROFIL:\n{cv.brief(profile)}\n\nWUNSCH: {wish or '-'}\n\n"
            f"ANZEIGE:\n" + "\n".join(facts) + f"\n\n{desc or '(nur Titel bekannt)'}")


async def read(client: httpx.AsyncClient, job: dict, profile: dict, wish: str) -> dict | None:
    """The model's reading, or None if it could not be had (the caller decides what that means)."""
    body = {
        "model": MODEL, "stream": False, "think": False, "format": READ_SCHEMA,
        "options": {"temperature": 0.1},
        "messages": [{"role": "system", "content": READ_SYSTEM},
                     {"role": "user", "content": _prompt(job, profile, wish)}],
    }
    try:
        r = await client.post(f"{OLLAMA}/api/chat", json=body, headers={"X-Job": "jobs"}, timeout=TIMEOUT)
        r.raise_for_status()
        out = json.loads(r.json()["message"]["content"])
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
        log.warning("read %s/%s failed: %s", job.get("source"), job.get("source_id"), exc)
        return None
    try:
        out["fit"] = max(0, min(100, int(out.get("fit", 0))))
    except (TypeError, ValueError):
        out["fit"] = 0
    if out.get("arbeitsmodell") not in MODES:
        out["arbeitsmodell"] = "unbekannt"
    if out.get("level") not in LEVELS:
        out["level"] = "unbekannt"
    out["anstellung"] = [k for k in out.get("anstellung") or [] if k in KINDS]
    for k in ("passt", "fehlt"):
        out[k] = [s for s in out.get(k) or [] if isinstance(s, str) and s.strip()][:3]
    return out
