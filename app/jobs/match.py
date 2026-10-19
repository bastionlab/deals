"""Does a job fit a search? Pure code, no model, no I/O.

Two passes:
- check(): the portal's own facts, before anything is read. hard = not what you
  asked for (too old, excluded word/company, wrong work mode, too far, salary
  stated below your floor) -> never shown. soft = aussortiert, one click away.
- after_reading(): the model's reading of the posting (mode, level, agency,
  remote region, fit). Everything here is soft: the model can be wrong, and a
  wrongly hidden job must stay findable.

Anything unknown passes. A posting that does not state its work mode or salary
must not vanish because of it.
"""
from __future__ import annotations

import re
import time

from ..flats import geo

MODE_DE = {"onsite": "vor Ort", "hybrid": "hybrid", "remote": "remote"}
LEVEL_DE = {"junior": "Junior", "mid": "Mid-Level", "senior": "Senior", "lead": "Lead"}
# remote "from anywhere in the US" is not remote for someone in Berlin
_FAR_REGION = re.compile(r"(?i)\b(usa?|united states|americas?|north america|canada|latam|latin america|"
                         r"apac|asia|india|australia|uk only|brazil|mexico|est|pst|us time ?zones?)\b")
_NEAR_REGION = re.compile(r"(?i)\b(deutschland|germany|dach|eu|europ\w*|emea|cet|cest|weltweit|worldwide|"
                          r"anywhere|global)\b")
EMPLOYMENT_KINDS = {"vollzeit", "teilzeit", "werkstudent", "praktikum", "ausbildung", "freelance", "minijob"}


_KIND_WORDS = {
    "werkstudent": r"werkstudent|working student|studentische|student job|studentenjob",
    "praktikum": r"praktik|intern\b|internship",
    "ausbildung": r"ausbildung|azubi|auszubildende|apprentice",
    "minijob": r"minijob|mini-job|450|520|556|603",
    "freelance": r"freelance|freiberuf|contractor",
    "teilzeit": r"teilzeit|part[- ]time",
}


def title_kinds(title: str) -> set[str]:
    return {k for k, rx in _KIND_WORDS.items() if re.search(rx, title or "", re.I)}


def _words(spec_list: list[str] | None) -> list[str]:
    return [w.strip().lower() for w in spec_list or [] if w and w.strip()]


def _eur(v: float) -> str:
    return f"{v:,.0f} €".replace(",", ".")


def check(job: dict, spec: dict, now: float | None = None) -> tuple[str, str | None, float | None]:
    """-> (ok | soft | hard, reason, km from the search's place)."""
    now = now or time.time()
    km = None
    born = job.get("posted_at") or job.get("first_seen_at") or now
    if now - born > (spec.get("max_age_days") or 21) * 86400:
        return "hard", "zu alt", km

    title = (job.get("title") or "").lower()
    company = (job.get("company") or "").lower()
    for w in _words(spec.get("exclude")):
        # whole words only: "IT" must not throw out "Security" or "Digital"
        if re.search(rf"(?<![a-zäöüß0-9]){re.escape(w)}(?![a-zäöüß0-9])", title):
            return "hard", f"„{w}“ im Titel", km
    for c in _words(spec.get("exclude_companies")):
        if c in company:
            return "hard", f"Firma {job.get('company')}", km

    modes = set(spec.get("modes") or MODE_DE)
    mode = job.get("work_mode")
    if mode and mode not in modes:
        return "hard", MODE_DE.get(mode, mode), km

    # distance only binds what you would commute to; a remote job can be anywhere
    if mode != "remote" and spec.get("lat") is not None and job.get("lat") is not None:
        km = round(geo.km_between(spec["lat"], spec["lon"], job["lat"], job["lon"]), 1)
        limit = max(spec.get("radius_km") or 0, 10) + 5   # a city's own extent
        if km > limit:
            if "remote" not in modes or mode == "onsite":
                return "hard", f"{km:g} km entfernt", km
            # mode unknown: could still turn out remote once read; after_reading decides

    want = set(spec.get("employment") or [])
    have = set(job.get("employment") or []) & EMPLOYMENT_KINDS
    # portals file student jobs as "Teilzeit" too; the title saying so wins
    if want and have and not want & have and not want & title_kinds(title):
        return "hard", ", ".join(sorted(have)), km
    if spec.get("permanent_only") and "befristet" in (job.get("employment") or []):
        return "hard", "befristet", km

    floor = spec.get("min_salary")
    top = job.get("salary_max") or job.get("salary_min")
    if floor and top and top < floor:
        return "hard", f"Gehalt bis {_eur(top)}", km

    if spec.get("no_agency", True) and job.get("agency"):
        return "soft", "Personalvermittlung / Zeitarbeit", km
    langs = set(spec.get("langs") or [])
    if langs and job.get("lang") and job["lang"] not in langs:
        return "soft", f"Anzeige auf {job['lang'].upper()}", km
    return "ok", None, km


def after_reading(job: dict, reading: dict, spec: dict, km: float | None) -> tuple[bool, str | None]:
    """The model's reading against the search. -> (passed, reason when not)."""
    modes = set(spec.get("modes") or MODE_DE)
    mode = job.get("work_mode") or (reading.get("arbeitsmodell") if reading.get("arbeitsmodell") in MODE_DE
                                     else None)
    if mode and mode not in modes:
        return False, f"laut Anzeige {MODE_DE[mode]}"
    if km is not None and mode != "remote":
        limit = max(spec.get("radius_km") or 0, 10) + 5
        if km > limit:
            return False, f"{km:g} km entfernt, nicht remote"
    if mode == "remote":
        region = reading.get("remote_raum") or ""
        if _FAR_REGION.search(region) and not _NEAR_REGION.search(region):
            return False, f"remote nur {region}"

    # the full posting (detail) often adds what the card did not say, e.g. LinkedIn's "Full-time"
    want = set(spec.get("employment") or [])
    if want:
        have = (set(job.get("employment") or []) | set(reading.get("anstellung") or [])) & EMPLOYMENT_KINDS
        if have and not want & (have | title_kinds(job.get("title") or "")):
            return False, ", ".join(sorted(have))

    levels = set(spec.get("levels") or [])
    lvl = reading.get("level")
    if levels and lvl in LEVEL_DE and lvl not in levels:
        return False, f"{LEVEL_DE[lvl]}-Stelle"
    if spec.get("no_agency", True) and reading.get("vermittler"):
        return False, "Personalvermittlung / Zeitarbeit"
    langs = set(spec.get("langs") or [])
    if langs and reading.get("sprache") and reading["sprache"] not in langs:
        return False, f"Anzeige auf {reading['sprache'].upper()}"

    floor = spec.get("min_fit") or 60
    if reading.get("fit", 0) < floor:
        return False, f"Match {reading.get('fit', 0)}: {reading.get('fazit') or ''}".strip(": ")
    return True, None
