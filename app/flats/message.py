"""A first message to the landlord, written for one ad from the user's own profile.

Division of labour, as everywhere in flats/:
- code decides the salutation: whether the contact is a company or a person,
  and what name to put there. The model never guesses a gender ("Herr"/"Frau")
  and never picks a name the ad did not give.
- the model writes the body: in the ad's language, picking up what the ad asks
  applicants to send, and using ONLY facts from the profile. Whatever the ad
  asks for that the profile does not cover comes back as `missing`, so the
  user can fill it in by hand instead of the model inventing an age or income.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

import httpx

from ..ai import MODEL, OLLAMA, TIMEOUT

log = logging.getLogger("flats.message")

SCHEMA = {
    "type": "object",
    "properties": {
        "sprache": {"type": "string", "enum": ["de", "en"]},
        "absaetze": {"type": "array", "items": {"type": "string"}},
        "gruss": {"type": "string"},
        "unterschrift": {"type": "array", "items": {"type": "string"}},
        "fehlt": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sprache", "absaetze", "gruss", "unterschrift", "fehlt"],
}

SYSTEM = """Du schreibst fuer einen Wohnungssuchenden die erste Nachricht an den Anbieter einer Mietwohnung.

Du bekommst das Inserat und das PROFIL des Suchenden. Schreibe:
- in der Sprache des Inserats: "de" fuer Deutsch, "en" nur wenn das Inserat ueberwiegend englisch ist
- hoeflich per "Sie", warm und knapp: 60 bis 120 Woerter, aus Sicht des Suchenden ("wir" bei mehreren, sonst "ich")
- absaetze: 2-3 Absaetze Fliesstext. OHNE Anrede und OHNE Gruss: "Guten Tag ..." wird davor gesetzt,
  der erste Absatz beginnt direkt mit dem Inhalt ("ich interessiere mich ...", "mit grossem Interesse ...")
- mit Bezug auf diese konkrete Wohnung: nenne 1-2 echte Details aus dem Inserat (Lage/Kiez, Zimmer, Balkon, Einzugstermin ...)
- stelle den Suchenden mit den Angaben aus dem PROFIL vor (wer, was beruflich, wie viele Personen, warum passend)
- verlangt das Inserat bestimmte Angaben oder Unterlagen (Alter, Personenzahl, Einkommen, Schufa, Selbstauskunft,
  Einzugstermin, Haustiere ...), beantworte sie, SOWEIT das PROFIL sie enthaelt
- bitte um einen Besichtigungstermin
- gruss: nur die Grussformel, z.B. "Viele Grüße" / "Best regards"
- unterschrift: Zeilen darunter: Name(n) aus dem PROFIL, dann Telefon und E-Mail, falls im PROFIL, je eine Zeile
- Laeuft die Bewerbung ueber ein Portal oder Formular: die Nachricht IST der Text fuer dessen Freitextfeld.
  Nicht auf das Portal verweisen, nicht ankuendigen, sich dort zu bewerben.

STRENG:
- Stehen im PROFIL mehrere Personen: durchgehend "wir", auch im allerersten Satz ("wir interessieren uns ...").
- Erfinde NICHTS ueber den Suchenden. Nur was im PROFIL steht. Kein Alter, kein Einkommen, kein Beruf, keine Haustiere,
  die nicht im PROFIL stehen. Wuenscht sich das Inserat Eigenschaften (Nichtraucher, queer, Studenten, ruhig,
  Frauen ...), behaupte sie NUR, wenn das PROFIL sie nennt - sonst lass das Thema weg.
- Keine Termine oder Daten erfinden, die weder im PROFIL noch im Inserat stehen.
- fehlt: Liste der Angaben, die das Inserat ausdruecklich verlangt, die aber NICHT im PROFIL stehen
  (kurz, z.B. "Alter", "Nettoeinkommen", "Schufa"). Sonst leere Liste.
- Kein "noch verfügbar?" - das Inserat ist online.
- Keine Platzhalter in eckigen Klammern.

STIL-BEISPIEL (nur fuer Ton und Aufbau - die Fakten darin gehoeren NICHT zum Suchenden):
absaetze: [
 "wir interessieren uns sehr für Ihre helle 2-Zimmer-Wohnung mit Balkon in Friedrichshain. Wir sind Jonas (Elektriker, unbefristet angestellt) und meine Partnerin Mira, die als Pflegerin arbeitet. Wir suchen ein Zuhause, in dem wir langfristig bleiben möchten.",
 "Alle Unterlagen (Einkommensnachweise, SCHUFA, Mietschuldenfreiheit) bringen wir gerne zur Besichtigung mit. Über einen Termin würden wir uns sehr freuen, wir sind zeitlich flexibel."
]
gruss: "Viele Grüße"
unterschrift: ["Jonas Beispiel & Mira Muster", "Tel. 0170 0000000"]

Antworte nur mit JSON."""

# legal forms and portal words that mark a company, not a person
_COMPANY = re.compile(
    r"\b(gmbh|ag|se|kg|eg|ug|gbr|e\.?\s?g\.|mbh|immobilien|hausverwaltung|verwaltung|wohnungsbau|"
    r"genossenschaft|makler|real estate|properties|living|apartments?|residen[cz]|kundenservice|"
    r"degewo|howoge|gewobag|gesobau|stadt und land|wbm|berlinovo|vonovia|deutsche wohnen|adler|"
    r"tauschwohnung|wohnungsswap|swap|team|service|gruppe|group|berlin)\b", re.I)
_NOT_A_NAME = {"privat", "privatperson", "anbieter", "vermieter", "vermieterin", "eigentümer",
               "eigentuemer", "mieter", "nachmieter", "owner", "landlord", "user", "nutzer"}
_TITLE = re.compile(r"^(herr|frau|hr\.|fr\.|mr\.?|mrs\.?|ms\.?)\s+", re.I)


def addressee(flat: dict) -> str | None:
    """The contact person's name if it is one, None for a company or no name at all."""
    c = flat.get("contact") or {}
    name = (c.get("name") or flat.get("seller") or "").strip()
    if not name or flat.get("commercial") or _COMPANY.search(name):
        return None
    name = re.sub(r"\s+", " ", name.split(" - ")[0]).strip()
    if name.lower() in _NOT_A_NAME:
        return None
    name = re.sub(r"\s+[A-Za-zÀ-ÿ]\.?$", "", name)          # "Molkal H" -> "Molkal"
    if name == name.lower():                                   # "dhiren kumar" -> "Dhiren Kumar"
        name = " ".join(w[:1].upper() + w[1:] for w in name.split())
    # "JP", "M.", "xx": initials are not a name to greet
    if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", _TITLE.sub("", name))) < 3:
        return None
    return name


def greeting(name: str | None, lang: str) -> str:
    if lang == "en":
        return f"Hello {name}," if name else "Dear Sir or Madam,"
    if not name:
        return "Sehr geehrte Damen und Herren,"
    m = _TITLE.match(name)
    if m:                                    # the ad itself said Herr/Frau: that is not a guess
        t = m.group(1).lower().rstrip(".")
        rest = name[m.end():]
        return f"Sehr geehrte Frau {rest}," if t in ("frau", "fr", "mrs", "ms") else f"Sehr geehrter Herr {rest},"
    return f"Guten Tag {name},"


def profile_hash(profile: str) -> str:
    return hashlib.sha1(profile.strip().encode()).hexdigest()[:12]


def _prompt(flat: dict, profile: str) -> str:
    facts = [f"- {k}: {v}" for k, v in (flat.get("attrs") or {}).items()]
    for label, key in (("Zimmer", "rooms"), ("Fläche m²", "area"), ("Bezirk", "district"),
                       ("Adresse", "address"), ("Warmmiete €", "warm"), ("frei ab", "available_from")):
        if flat.get(key):
            facts.append(f"- {label}: {flat[key]}")
    how = (flat.get("contact") or {}).get("how")
    return (f"PROFIL DES SUCHENDEN:\n{profile.strip()}\n\n"
            f"INSERAT\nTITEL: {flat['title']}\n"
            f"WIE BEWERBEN: {how or '-'}\n"
            f"FAKTEN:\n{chr(10).join(facts) or '-'}\n\n"
            f"BESCHREIBUNG:\n{(flat.get('description') or '-')[:3500]}")


_GREET = re.compile(r"^(sehr geehrte[rs]?|guten tag|hallo|hi|hello|dear|liebe[rs]?)\b[^,!\n]{0,40}[,!]\s*", re.I)


def _para(text: str) -> str:
    text = re.sub(r"\[[^\]\n]{1,40}\]", "", str(text))      # stray [Platzhalter]
    return re.sub(r"[ \t]+", " ", text).strip()


def _body(paras: list[str]) -> str:
    paras = [p for p in (_para(x) for x in paras) if p]
    if paras:
        # the model greets anyway now and then; the greeting is ours
        first = _GREET.sub("", paras[0])
        paras[0] = first[:1].upper() + first[1:] if first else first
    return "\n\n".join(p for p in paras if p)


async def write(client: httpx.AsyncClient, flat: dict, profile: str) -> dict:
    body = {
        "model": MODEL, "stream": False, "think": False, "format": SCHEMA,
        "options": {"temperature": 0.3},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": _prompt(flat, profile)}],
    }
    r = await client.post(f"{OLLAMA}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    out = json.loads(r.json()["message"]["content"])
    lang = "en" if out.get("sprache") == "en" else "de"
    sign = [_para(x) for x in (out.get("unterschrift") or []) if _para(x)]
    closing = _para(out.get("gruss") or "") or ("Best regards" if lang == "en" else "Viele Grüße")
    text = f"{greeting(addressee(flat), lang)}\n\n{_body(out.get('absaetze') or [])}\n\n" + \
        "\n".join([closing.rstrip(","), *sign])
    missing = [str(x).strip()[:40] for x in (out.get("fehlt") or []) if str(x).strip()][:6]
    return {"text": text, "missing": missing, "lang": lang}
