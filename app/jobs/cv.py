"""The user's CV: PDF in, plain text + a short profile out.

The profile is what the model reads alongside every posting, so it is kept to
facts the CV states (roles, skills, languages, years). Code never trusts the
numbers in it for a filter; they only feed the fit judgement.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re

import httpx

from ..ai import MODEL, OLLAMA, TIMEOUT

log = logging.getLogger("jobs.cv")

MAX_PDF = 8 * 1024 * 1024
MAX_TEXT = 12000            # chars sent to the model; a CV longer than this is mostly padding

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "headline": {"type": "string"},
        "roles": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "years": {"type": "integer"},
        "level": {"type": "string", "enum": ["junior", "mid", "senior", "lead"]},
        "languages": {"type": "array", "items": {"type": "string"}},
        "location": {"type": "string"},
        "education": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["name", "headline", "roles", "skills", "years", "level", "languages", "location",
                 "education", "summary"],
}

SYSTEM = """Du liest einen Lebenslauf und fasst ihn fuer eine Jobsuche zusammen.

- name: voller Name, wie im Lebenslauf
- headline: aktuelle bzw. letzte Rolle, kurz (z.B. "Senior Backend Engineer bei Firma X")
- roles: 3-6 Stellenbezeichnungen, unter denen diese Person realistisch sucht, deutsch UND englisch gemischt,
  so wie sie in Stellenanzeigen stehen (z.B. "Backend Developer", "Softwareentwickler Python")
- skills: die 10-20 wichtigsten fachlichen Kenntnisse/Werkzeuge, kurz (z.B. "Python", "Kubernetes", "SAP FI")
- years: Jahre Berufserfahrung insgesamt (Ausbildung/Studium nicht mitzaehlen), ganze Zahl
- level: junior (<2 J.), mid (2-5), senior (5+), lead (fuehrt Teams/Bereiche)
- languages: Sprachen mit Niveau, z.B. "Deutsch (Muttersprache)", "Englisch (C1)"
- location: Wohnort, wenn genannt, sonst ""
- education: hoechster Abschluss, kurz
- summary: 2-3 Saetze, deutsch: was die Person kann und wofuer sie steht

Nur was im Lebenslauf steht. Nichts erfinden. Antworte nur mit JSON."""


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader          # only the upload path needs it

    reader = PdfReader(io.BytesIO(data))
    pages = [(p.extract_text() or "") for p in reader.pages[:12]]
    t = "\n".join(pages)
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def cv_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


async def profile(client: httpx.AsyncClient, text: str) -> dict:
    body = {
        "model": MODEL, "stream": False, "think": False, "format": SCHEMA,
        "options": {"temperature": 0.1},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": text[:MAX_TEXT]}],
    }
    r = await client.post(f"{OLLAMA}/api/chat", json=body, headers={"X-Job": "jobs-cv"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = json.loads(r.json()["message"]["content"])
    for k in ("roles", "skills", "languages"):
        out[k] = [s.strip() for s in out.get(k) or [] if isinstance(s, str) and s.strip()][:20]
    return out


def brief(p: dict) -> str:
    """The profile as the few lines every judgement prompt carries."""
    if not p:
        return "-"
    return "\n".join(x for x in (
        f"Aktuell: {p.get('headline') or '-'}",
        f"Erfahrung: {p.get('years', '?')} Jahre, Level {p.get('level') or '?'}",
        f"Passende Rollen: {', '.join(p.get('roles') or []) or '-'}",
        f"Kenntnisse: {', '.join(p.get('skills') or []) or '-'}",
        f"Sprachen: {', '.join(p.get('languages') or []) or '-'}",
        f"Abschluss: {p.get('education') or '-'}",
        f"Kurz: {p.get('summary') or '-'}",
    ))
