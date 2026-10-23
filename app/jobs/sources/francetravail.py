"""France Travail (candidat.francetravail.fr, ex-Pôle emploi): the public employment service's offer search.

Verified 2026-10-22 from CT 107 (home IP, desktop Chrome UA, no cookies) — all 200, no bot wall.

Search  GET /offres/recherche?motsCles=<what>&lieux=<where>&rayon=<km>&tri=1&emission=7
            &offresPartenaires=true&range=0-49
- lieux = a commune's INSEE code (75056 Paris, 69123 Lyon — the whole city, arrondissements
  included), a département "75D", a région "11R"; no lieux = all of France. We resolve the
  search centre to its INSEE code with geo.api.gouv.fr (free, no key; empty answer = not France).
- rayon = 0..100 km around a commune (portal default 10).
- tri=1 = newest first (tri=0 relevance).
- emission = 1 | 3 | 7 | 14 | 31 days ("Publié depuis"); absent = any age.
- offresPartenaires=true adds partner offers (HelloWork, APEC, TalentPlug, Indeed-fed boards…),
  i.e. many postings also come in through the hellowork source — same ad, different id.
- range=<from>-<to> (0-based, inclusive) pages the list; the site asks 20 at a time, 50 works.
  The header says "1515 offres".
- No remote / télétravail filter exists -> the remote leg returns [].
- Cards: <li data-id-offre="<id>"> title, "Company - <span>75 - Paris 1er Arrondissement</span>",
  a 200-char teaser, contract ("CDI" + "Temps plein" | "Non renseigné"), "Publié il y a 3 jours",
  logo alt "Origine de l'offre : APEC" (partner name). Ids are "215LTGB" (France Travail's own)
  or digits (partner offers).
Posting
- /offres/recherche/detail/<id> is only a shell; the text comes from a web component that reads
  GET /api-descriptifoffre/offre/<id> (JSON, no auth): descriptif (plain text), salaire
  ("Annuel de 50000.0 Euros à 60000.0 Euros"), syntheseContrat, conditionTravailPoste,
  etablissement, lieuTravail, listeCompetence, syntheseSecteurActivite. No publication date.
- Where to apply: GET /offres/recherche/detail.detailstandalone.postuler.contact:chargercontact?t:ac=<id>
  (Tapestry JSON with an HTML fragment): an "external-link" = the recruiter's own site / ATS,
  idLienPartenaire = the partner board carrying it, mailto: = apply by e-mail.
"""
from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from urllib.parse import urlencode

from .common import Limiter, clean, job, text, yearly

SOURCE = "francetravail"
LABEL = "France Travail"
INTERVAL = 6.0
COUNTRY = "fr"              # the poller only asks this portal for searches located in France

BASE = "https://candidat.francetravail.fr"
GEO_API = "https://geo.api.gouv.fr/communes"
PER_PAGE = 50
EMISSION = (1, 3, 7, 14, 31)

_CONTRACT = {"cdi": [], "cdd": ["befristet"], "mis": ["befristet"], "intérim": ["befristet"],
             "mission intérimaire": ["befristet"], "saisonnier": ["befristet"],
             "profession libérale": ["freelance"], "franchise": ["freelance"],
             "apprentissage": ["ausbildung"], "contrat d'apprentissage": ["ausbildung"],
             "professionnalisation": ["ausbildung"], "stage": ["praktikum"]}
_UNIT = {"annuel": "year", "mensuel": "month", "horaire": "hour", "journalier": "day"}
_AGENCY = re.compile(r"(?i)travail temporaire|intérim|interim|placement de main|cabinet de recrutement|"
                     r"pour (le compte d['’]|l['’]un de )?(un|une|nos|notre) client")
_REMOTE_FR = re.compile(r"(?i)\b(100 ?% (en )?t[ée]l[ée]travail|full[ -]remote|t[ée]l[ée]travail (total|complet|int[ée]gral))")
_HYBRID_FR = re.compile(r"(?i)t[ée]l[ée]travail|\bhybride?\b|\d ?(j|jours?) (de )?(remote|t[ée]l[ée]travail)")

_communes: dict = {}


class Blocked(RuntimeError):
    pass


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in s if not unicodedata.combining(ch))).strip()


async def _commune(client, q: dict) -> dict | None:
    """The French commune at the search's centre (geo.api.gouv.fr, free, no key); None = not in France."""
    lat, lon, place = q.get("lat"), q.get("lon"), (q.get("place") or "").strip()
    ck = (round(lat, 4), round(lon, 4)) if lat is not None and lon is not None else place.lower()
    if not ck:
        return None
    if ck in _communes:
        return _communes[ck]
    fields = "nom,code,codeDepartement,codesPostaux,centre"
    if lat is not None and lon is not None:
        r = await client.get(GEO_API, params={"lat": lat, "lon": lon, "fields": fields})
    else:                                        # name only: exact name, biggest commune
        r = await client.get(GEO_API, params={"nom": place, "boost": "population", "limit": 5, "fields": fields})
    r.raise_for_status()
    rows = r.json()
    if lat is None or lon is None:
        rows = [c for c in rows if _fold(c["nom"]) == _fold(place)]
    _communes[ck] = c = rows[0] if rows else None
    return c


def search_url(kw: str, q: dict, insee: str, start: int) -> str:
    params = {"motsCles": kw, "lieux": insee, "rayon": int(min(100, q.get("radius_km") or 0)),
              "tri": 1, "offresPartenaires": "true"}
    days = q.get("max_age_days")
    if days and days <= EMISSION[-1]:
        params["emission"] = next(e for e in EMISSION if e >= days)
    params["range"] = f"{start}-{start + PER_PAGE - 1}"
    return f"{BASE}/offres/recherche?{urlencode(params)}"


def _check(r, what: str) -> str:
    if r.status_code in (403, 429) or "captcha" in r.text[:5000].lower():
        raise Blocked(f"France Travail {what}: blocked ({r.status_code})")
    r.raise_for_status()
    return r.text


def _ago(s: str) -> float | None:
    s = s.lower()
    if "aujourd" in s:
        return time.time()
    if "hier" in s:
        return time.time() - 86400
    if "plus de" in s:
        return None
    m = re.search(r"il y a (\d+) jour", s)
    return time.time() - int(m.group(1)) * 86400 if m else None


def _employment(contract: str, hours: str = "") -> list[str]:
    out = list(_CONTRACT.get(contract.strip().lower(), []))
    h = hours.lower()
    if "temps plein" in h or "temps complet" in h:
        out.append("vollzeit")
    if "temps partiel" in h:
        out.append("teilzeit")
    return list(dict.fromkeys(out))


def _card(oid: str, chunk: str) -> dict | None:
    t = re.search(r'<span class="media-heading-title">(.*?)</span>', chunk, re.S)
    if not t:
        return None
    sub = re.search(r'<p[^>]*class="subtext"[^>]*>(.*?)</p>', chunk, re.S)
    company = loc = None
    if sub:
        loc_m = re.search(r"<span>(.*?)</span>", sub.group(1), re.S)
        loc = clean(text(loc_m.group(1))) if loc_m else None
        company = clean(text(sub.group(1).split("<span>")[0])).rstrip(" -").strip() or None
    teaser = re.search(r'<p class="description">(.*?)</p>', chunk, re.S)
    when = re.search(r'<p class="date">(.*?)</p>', chunk, re.S)
    con = re.search(r'<p class="contrat">(.*?)<span class="type-contrat">(.*?)</span>', chunk, re.S)
    contract, hours = (clean(text(con.group(1))), clean(text(con.group(2)))) if con else ("", "")
    origin = re.search(r"Origine de l'offre : ([^\"]+)\"", html.unescape(chunk))
    desc = text(teaser.group(1)) if teaser else None
    attrs = {k: v for k, v in {
        "contract": " · ".join(x for x in (contract, hours) if x and x != "Non renseigné"),
        "origin": origin.group(1).strip() if origin else None,
    }.items() if v}
    return job(
        SOURCE, oid, f"{BASE}/offres/recherche/detail/{oid}", text(t.group(1)),
        company=company, location=loc,          # "75 - Paris 1er": a département number, not a postal code
        employment=_employment(contract, hours),
        posted_at=_ago(clean(text(when.group(1)))) if when else None,
        description=desc,
        agency=True if contract.lower() in ("mis", "intérim", "mission intérimaire") else None,
        attrs=attrs,
    )


async def search(client, q: dict, limiter: Limiter) -> list[dict]:
    if q.get("remote"):
        return []                                # France Travail has no télétravail filter
    c = await _commune(client, q)
    if not c:
        return []                                # no place, or not in France
    out: dict[str, dict] = {}
    for kw in q.get("keywords") or []:
        for page in range(max(1, q.get("max_pages") or 1)):
            await limiter.wait()
            r = await client.get(search_url(kw, q, c["code"], page * PER_PAGE),
                                 headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
            body = _check(r, "search")
            parts = re.split(r'<li data-id-offre="([0-9A-Z]+)" class="result">', body)
            n = 0
            for oid, chunk in zip(parts[1::2], parts[2::2]):
                n += 1
                if oid not in out and (j := _card(oid, chunk)):
                    out[oid] = j
            total = re.search(r'<h1 class="title">\s*(\d+) offres?', body)
            if n < PER_PAGE or (total and (page + 1) * PER_PAGE >= int(total.group(1))):
                break
    return list(out.values())


def _salary(s: str | None) -> tuple[float | None, float | None]:
    """'Annuel de 50000.0 Euros à 60000.0 Euros' / 'Mensuel de 2000.0 Euros sur 12.0 mois' -> yearly."""
    if not s or "euro" not in s.lower():
        return None, None
    unit = _UNIT.get(s.split()[0].lower())
    nums = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?) Euros", s)]
    if not unit or not nums:
        return None, None
    if unit == "month" and nums[0] > 15000:     # "Mensuel de 38000.0 Euros": a yearly figure in the wrong box
        unit = "year"
    if unit == "month" and (m := re.search(r"sur (\d+(?:\.\d+)?) mois", s)):
        return round(nums[0] * float(m.group(1))), round(nums[-1] * float(m.group(1)))
    return yearly(nums[0], unit), yearly(nums[-1], unit)


async def _contact(client, oid: str, limiter: Limiter) -> dict:
    await limiter.wait()
    r = await client.get(f"{BASE}/offres/recherche/detail.detailstandalone.postuler.contact:chargercontact",
                         params={"t:ac": oid}, headers={"X-Requested-With": "XMLHttpRequest",
                                                        "Accept": "application/json"})
    if r.status_code != 200:
        return {}
    try:
        frag = "".join(c[1] for c in r.json()["_tapestry"]["content"] if len(c) > 1)
    except (ValueError, KeyError, TypeError):
        return {}
    out = {}
    if m := re.search(r"class='tel external-link'[^>]*href='([^']+)'|href='([^']+)'[^>]*class='tel external-link'", frag):
        out["apply_url"] = html.unescape(m.group(1) or m.group(2))
    if m := re.search(r"id='idLienPartenaire'[^>]*href='([^']+)'", frag):
        out["partner_url"] = html.unescape(m.group(1))
    if m := re.search(r"mailto:([^'\"?]+)", frag):
        out["apply_email"] = html.unescape(m.group(1)).strip()
    return out


async def detail(client, j: dict, limiter: Limiter) -> dict | None:
    await limiter.wait()
    r = await client.get(f"{BASE}/api-descriptifoffre/offre/{j['source_id']}",
                         headers={"Accept": "application/json"})
    if r.status_code in (204, 404, 410):
        return None
    _check(r, "posting")
    d = r.json()
    desc = clean_lines(d.get("descriptif"))
    comp = [c.get("libelleCompetence") for c in d.get("listeCompetence") or [] if c.get("libelleCompetence")]
    if comp:
        desc = f"{desc}\n\nCompétences\n" + "\n".join(f"• {c}" for c in comp)
    cond = d.get("conditionTravailPoste") or {}
    hours = " ".join(cond.get("listeLibelleTypeHoraire") or [])
    contract = (d.get("syntheseContrat") or "").split(" - ")[0]
    lieu = d.get("lieuTravail") or {}
    smin, smax = _salary(d.get("salaire"))
    sector = d.get("syntheseSecteurActivite") or ""
    attrs = dict(j.get("attrs") or {})
    attrs.update({k: v for k, v in {
        "contract": d.get("syntheseContrat"),
        "hours": hours,
        "sector": sector,
        "experience": (d.get("experienceProfil") or {}).get("experience"),
        "qualification": d.get("qualification"),
        "rome": d.get("appellationRome"),
    }.items() if v and v != "Non disponible"})
    out = {
        "description": desc or None,
        "employment": list(dict.fromkeys((j.get("employment") or []) + _employment(contract, hours))),
        "salary_min": smin, "salary_max": smax,
        "salary_text": d.get("salaire") if smin else None,
    }
    if not j.get("company") and (org := (d.get("etablissement") or {}).get("libelleEnseigne")):
        out["company"] = clean(org)
    if lieu.get("libelleCommuneLieuTravail") and not j.get("location"):
        out["location"] = lieu.get("syntheseLieuTravail")
    if _AGENCY.search(sector) or _AGENCY.search(contract) or _AGENCY.search(desc[:1500]):
        out["agency"] = True
    if _REMOTE_FR.search(desc):
        out["work_mode"] = "remote"
    elif _HYBRID_FR.search(desc):
        out["work_mode"] = "hybrid"
    contact = await _contact(client, j["source_id"], limiter)
    if contact.get("partner_url"):
        attrs["partner_url"] = contact.pop("partner_url")[:300]
    out.update(contact)
    out["attrs"] = attrs
    return out


def clean_lines(s: str | None) -> str:
    """Plain text with line breaks kept, runs of blank lines squeezed."""
    s = html.unescape(s or "").replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", s).strip()


if __name__ == "__main__":
    import asyncio
    import sys

    import httpx

    from .common import HEADERS, TIMEOUT

    async def main():
        lim = Limiter(INTERVAL)
        kw = sys.argv[1] if len(sys.argv) > 1 else "développeur"
        q = {"keywords": [kw], "place": "Paris", "lat": 48.8566, "lon": 2.3522, "radius_km": 20,
             "remote": False, "max_age_days": 7, "max_pages": 1, "no_agency": True}
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            print(search_url(kw, q, "75056", 0))
            found = await search(c, q, lim)
            print(f"{len(found)} jobs")
            for j in found:
                day = time.strftime("%m-%d", time.localtime(j["posted_at"])) if j["posted_at"] else "?"
                print(f"  {j['source_id']} | {j['title'][:50]} | {j['company']} | {j['location']} | {day} | "
                      f"{j['employment']} | ag={j['agency']} | {j['attrs']}")
            for j in found[:3]:
                more = await detail(c, j, lim) or {}
                print(f"* {j['url']} | {more.get('salary_text')} {more.get('salary_min')} | {more.get('work_mode')} | "
                      f"{more.get('employment')} | agency={more.get('agency')} | apply={more.get('apply_url')} "
                      f"mail={more.get('apply_email')} | {more.get('attrs')}\n  {len(more.get('description') or '')} "
                      f"chars: {(more.get('description') or '')[:200]!r}")

    asyncio.run(main())
