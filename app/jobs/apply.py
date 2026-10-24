"""Where to apply: the employer's own channel behind a portal posting.

resolve() is the poller's one call, after a source's detail() filled the job.
Order (first that answers wins):
    1. job['apply_url'] on a known ATS / career page          via=posting
    2. job['apply_url'] anywhere off the portals              via=posting
    3. ATS / career links inside the description text         via=description
    4. application e-mail (job['apply_email'] or description) channel=email
    5. the same job found via SearXNG on the employer's ATS   via=search
    6. the portal posting itself                              channel=portal
Returns {"channel","label","url","email","via","retry"}; retry=True means the search
leg could not run (SearXNG down / engines suspended) and a later resolve() may do better.

Verified 2026-10-19 from CT 107 against the self-hosted SearXNG (192.168.1.55:8888):
- /search?q=..&format=json answers in 0.5-1.5 s. Its default web engines are useless
  for this: google cse got "Suspended: too many requests" after ~50 queries in a few
  minutes (SearXNG then parks it for a while), brave 429s, duckduckgo CAPTCHAs.
  The off-by-default engines yahoo, "duckduckgo web" and yep answer when named via
  engines=...; bing returns zhihu.com junk. Hence ENGINES below.
- A quoted legal name ("LBBW Landesbank Baden-Württemberg") returns 0 results, so the
  company goes in unquoted, without GmbH/AG/SE.
- 25 real (title, company) pairs from stepstone.de/jobs/python/in-berlin: 15 found on
  the employer's channel (SmartRecruiters x3, Personio x2, BITE, career pages x9),
  0 wrong employer; misses are agencies (Computer Futures), brand != domain
  (GMX -> united-internet.de, Bundesnotarkammer -> bnotk.de), JS-only career pages,
  or no own posting indexed. 9/10 JOIN/Personio postings found from title+company
  (the 10th: brand "Caspar Health" != legal name GoReha). JOIN redirects an older
  repost id to the current one, so a stale JOIN hit still lands right.
- Aggregators (talent.de, nejo.com, jobs.heise.de, wearedevelopers ...) quote the
  title and company verbatim, so a career-page hit needs the company in the HOST;
  an ATS hit may show it in url, title or snippet (b-ite urls are bare hashes).
Stdlib + httpx only.
"""
from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from urllib.parse import urlsplit

import httpx

SEARX = os.environ.get("DEALS_SEARX_URL", "http://192.168.1.55:8888")
SEARX_TIMEOUT = 15.0
_searx_gate = asyncio.Semaphore(2)          # LAN box, upstream engines 429 on bursts
# the instance's default web engines are google cse (quota'd, 429 after ~50 quick
# queries) + brave/duckduckgo (blocked); these off-by-default ones answer and are
# picked per request. bing is off on purpose: it returns zhihu.com junk for German queries.
ENGINES = "yahoo,duckduckgo web,yep,google cse"

# host suffix -> (ats id, label). Matched on the host and every parent domain.
_HOSTS = {
    "join.com": ("join", "JOIN"),
    "jobs.personio.de": ("personio", "Personio"),
    "jobs.personio.com": ("personio", "Personio"),
    "greenhouse.io": ("greenhouse", "Greenhouse"),
    "lever.co": ("lever", "Lever"),
    "ashbyhq.com": ("ashby", "Ashby"),
    "workable.com": ("workable", "Workable"),
    "smartrecruiters.com": ("smartrecruiters", "SmartRecruiters"),
    "recruitee.com": ("recruitee", "Recruitee"),
    "softgarden.io": ("softgarden", "softgarden"),
    "softgarden.de": ("softgarden", "softgarden"),
    "successfactors.com": ("successfactors", "SAP SuccessFactors"),
    "successfactors.eu": ("successfactors", "SAP SuccessFactors"),
    "myworkdayjobs.com": ("workday", "Workday"),
    "myworkdaysite.com": ("workday", "Workday"),
    "dvinci-hr.com": ("dvinci", "d.vinci"),
    "dvinci.de": ("dvinci", "d.vinci"),
    "onlyfy.jobs": ("onlyfy", "onlyfy"),
    "onlyfy.com": ("onlyfy", "onlyfy"),
    "prescreen.io": ("onlyfy", "onlyfy"),
    "rexx-systems.com": ("rexx", "rexx"),
    "rexx-recruitment.com": ("rexx", "rexx"),
    "umantis.com": ("umantis", "Haufe umantis"),
    "b-ite.de": ("bite", "BITE"),
    "b-ite.com": ("bite", "BITE"),
    "bite-it.de": ("bite", "BITE"),
    "concludis.de": ("concludis", "concludis"),
    "concludis.com": ("concludis", "concludis"),
    "teamtailor.com": ("teamtailor", "Teamtailor"),
    "comeet.com": ("comeet", "Comeet"),
    "comeet.co": ("comeet", "Comeet"),
    "breezy.hr": ("breezy", "Breezy HR"),
    "bamboohr.com": ("bamboohr", "BambooHR"),
    "heyrecruit.de": ("heyrecruit", "heyrecruit"),
    "coveto.de": ("coveto", "coveto"),
    "talention.de": ("talention", "talention"),
    "talention.com": ("talention", "talention"),
    "taleo.net": ("taleo", "Oracle Taleo"),
    "icims.com": ("icims", "iCIMS"),
    "jobvite.com": ("jobvite", "Jobvite"),
    "pinpointhq.com": ("pinpoint", "Pinpoint"),
    "homerun.co": ("homerun", "Homerun"),
    "polymer.co": ("polymer", "Polymer"),
    "factorialhr.com": ("factorial", "Factorial"),
    "factorialhr.de": ("factorial", "Factorial"),
    "kenjo.io": ("kenjo", "Kenjo"),
    "recruiterbox.com": ("recruiterbox", "Recruiterbox"),
}
# vendors that also run on the customer's own domain: a host label is enough
_HOST_WORDS = (("personio", "personio", "Personio"), ("dvinci", "dvinci", "d.vinci"),
               ("umantis", "umantis", "Haufe umantis"), ("rexx", "rexx", "rexx"),
               ("concludis", "concludis", "concludis"), ("softgarden", "softgarden", "softgarden"),
               ("prescreen", "onlyfy", "onlyfy"), ("onlyfy", "onlyfy", "onlyfy"),
               ("icims", "icims", "iCIMS"), ("jobvite", "jobvite", "Jobvite"))
# job portals, aggregators and job boards of newspapers: never the employer's channel
PORTALS = {
    "stepstone.de": "StepStone", "stepstone.com": "StepStone", "linkedin.com": "LinkedIn",
    "xing.com": "XING", "arbeitsagentur.de": "Bundesagentur für Arbeit",
    "indeed.com": "Indeed", "indeed.de": "Indeed", "glassdoor.de": "Glassdoor",
    "glassdoor.com": "Glassdoor", "monster.de": "Monster", "kununu.com": "kununu",
    "jobware.de": "Jobware", "stellenanzeigen.de": "stellenanzeigen.de", "jobs.de": "jobs.de",
    "jooble.org": "Jooble", "adzuna.de": "Adzuna", "careerjet.de": "Careerjet",
    "jobrapido.com": "Jobrapido", "talent.com": "talent.com", "kimeta.de": "kimeta",
    "jobted.de": "Jobted", "absolventa.de": "Absolventa", "get-in-it.de": "get in IT",
    "berlinstartupjobs.com": "Berlin Startup Jobs", "germantechjobs.de": "GermanTechJobs",
    "yourfirm.de": "Yourfirm", "meinestadt.de": "meinestadt.de", "heise.de": "heise Jobs",
    "golem.de": "Golem Karrierewelt", "t3n.de": "t3n Jobs", "zeit.de": "ZEIT Stellenmarkt",
    "sueddeutsche.de": "SZ Stellenmarkt", "faz.net": "FAZ Stellenmarkt",
    "tagesspiegel.de": "Tagesspiegel Jobs", "welcometothejungle.com": "Welcome to the Jungle",
    "wellfound.com": "Wellfound", "gulp.de": "GULP", "freelancermap.de": "freelancermap",
    "stellenonline.de": "stellenonline", "jobvector.de": "jobvector", "experteer.de": "Experteer",
    "stellenwerk.de": "Stellenwerk", "jobmensa.de": "jobmensa", "jobteaser.com": "JobTeaser",
    "ingenieur.de": "ingenieur.de", "hokify.de": "hokify", "jobboerse.de": "jobboerse.de",
    "kleinanzeigen.de": "Kleinanzeigen", "jobsuche.de": "jobsuche.de", "jobninja.com": "JobNinja",
    "stellenmarkt.de": "stellenmarkt.de", "careerbuilder.com": "CareerBuilder",
    # BA externeURL often points at these partner boards, not at the employer
    "deutschland-stellenmarkt.de": "Deutschland-Stellenmarkt", "finest-jobs.com": "finest jobs",
    "jobexport.de": "jobexport", "appcast.io": "Appcast",
    "workwise.io": "Workwise", "instaffo.com": "Instaffo", "honeypot.io": "Honeypot",
    "google.com": "Google", "google.de": "Google", "bund.de": "Service des Bundes",
    "interamt.de": "Interamt", "jobs.ch": "jobs.ch", "karriere.at": "karriere.at",
    "jobbörse.de": "jobbörse.de", "jobboersen.net": "jobboersen.net",
    "builtin.com": "Built In", "remoteok.com": "Remote OK", "weworkremotely.com": "We Work Remotely",
    "stackoverflow.com": "Stack Overflow", "efinancialcareers.de": "eFinancialCareers",
    "jobsora.com": "Jobsora", "whatjobs.com": "WhatJobs", "jobleads.com": "JobLeads",
    "jobilize.com": "Jobilize", "simplyhired.de": "SimplyHired", "joblift.de": "Joblift",
    "jobscope.de": "jobscope", "jobstairs.de": "JobStairs",
}
# tracking redirectors in apply links: off-portal but not the employer either
_REDIRECTORS = ("appcast.io", "joveo.com", "pandologic.com", "jobsync.io", "click.")

_CAREER_SUB = {"karriere", "jobs", "job", "careers", "career", "stellen", "stellenangebote",
               "bewerbung", "talent", "join", "work", "arbeiten"}
_CAREER_PATH = re.compile(r"(?i)/(karriere|careers?|jobs?|stellenangebote|stellenmarkt|"
                          r"stellenausschreibungen?|offene-stellen|vacanc(?:y|ies)|join-us|"
                          r"arbeiten-bei|work-with-us|jobboerse|stellen)(?:[/._?#-]|$)")


def _domain(host: str) -> str:
    """Registrable domain, good enough for .de/.com/.co.uk."""
    parts = host.split(".")
    n = 3 if len(parts) > 2 and parts[-2] in ("co", "com", "org", "ac", "gv") and len(parts[-1]) == 2 else 2
    return ".".join(parts[-n:])


def _split(url: str) -> tuple[str, str]:
    try:
        u = urlsplit(url.strip() if "://" in url else "https://" + url.strip())
    except ValueError:
        return "", ""
    host = (u.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host, u.path or "/"


def portal(url: str | None) -> str | None:
    """Label of the job portal / aggregator the URL lives on, else None."""
    if not url:
        return None
    host, _ = _split(url)
    return PORTALS.get(_domain(host)) if host else None


def classify(url: str | None) -> dict | None:
    """The ATS / channel behind a URL, or None (portal, unknown site, junk)."""
    if not url or not isinstance(url, str):
        return None
    host, path = _split(url)
    if not host or "." not in host:
        return None
    dom = _domain(host)
    if dom in PORTALS:
        return None
    labels = host.split(".")
    for i in range(len(labels) - 1):
        hit = _HOSTS.get(".".join(labels[i:]))
        if hit:
            if hit[0] == "join" and not path.startswith("/companies/"):
                return None           # join.com itself is JOIN's marketing site
            return {"ats": hit[0], "label": hit[1], "url": url}
    first = path.strip("/").split("/", 1)[0].lower()     # karriere.x.de/dvinci/... = hosted on own domain
    for word, ats, label in _HOST_WORDS:
        if word in host or word == first:
            return {"ats": ats, "label": label, "url": url}
    if labels[0] in _CAREER_SUB and len(labels) > 2 or _CAREER_PATH.search(path):
        return {"ats": "career", "label": "Karriereseite", "url": url}
    return None


# --- e-mails ---------------------------------------------------------------

_AT = re.compile(r"(?i)\s*(?:[\(\[\{<]\s*(?:at|ät|@)\s*[\)\]\}>]|\s@\s|\s+at\s+(?=[\w-]+\s*[\(\[\{]\s*(?:dot|punkt)))\s*")
_DOT = re.compile(r"(?i)\s*[\(\[\{<]\s*(?:dot|punkt|\.)\s*[\)\]\}>]\s*")
_MAIL = re.compile(r"(?<![\w.+-])([\w.+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,24})(?![\w-])", re.I)
_APP = re.compile(r"^(bewerbung|bewerbungen|application|applications|apply|jobs?|karriere|"
                  r"careers?|hr|personal|personalabteilung|recruiting|recruitment|recruiter|"
                  r"talent|talents|people|team|ausbildung|stellen)\b")
_NEVER = re.compile(r"^(no-?reply|do-?not-?reply|datenschutz|privacy|dsb|dpo|gdpr|dsgvo|"
                    r"abuse|postmaster|webmaster|mailer-daemon)\b")
_WEAK = re.compile(r"^(info|kontakt|contact|office|mail|hello|hallo|service|support|post|zentrale)\b")
_FILE = re.compile(r"\.(png|jpe?g|gif|svg|webp|pdf)$", re.I)


def emails(text: str | None) -> list[str]:
    """Application addresses in a description, best first.
    noreply/privacy never; info@-style only when nothing better is there."""
    if not text:
        return []
    s = _DOT.sub(".", _AT.sub("@", text))
    seen: list[str] = []
    for m in _MAIL.finditer(s):
        e = m.group(1).strip(".").lower()
        if _FILE.search(e) or e in seen:
            continue
        seen.append(e)
    keep = [e for e in seen if not _NEVER.match(e)]
    app = [e for e in keep if _APP.match(e)]
    mid = [e for e in keep if not _APP.match(e) and not _WEAK.match(e)]
    weak = [e for e in keep if _WEAK.match(e)]
    return app + mid if app or mid else weak


# --- matching search results against the posting ---------------------------

_GENDER = re.compile(r"(?i)\(\s*(?:(?:[mwfdx]|div)\s*[/|,*]\s*){1,3}(?:[mwfdxi]|div)\s*\)|"
                     r"\(\s*(?:all genders?|gn\*?|alle geschlechter|alle identitäten|d)\s*\)|"
                     r"\b[mwfd]\s*/\s*[mwfd]\s*/\s*[mwfdx]\b|\(\s*[mwfd]\s*\)|"
                     r"(?<=\w)[*:_/]in(?:nen)?\b")          # Werkstudent*in, Absolvent:innen
_LEGAL = re.compile(r"(?i)\b(gmbh\s*&\s*co\.?\s*kgaa?|gmbh\s*&\s*co\.?\s*kg|ggmbh|gmbh|mbh|ag|se|kgaa|kg|"
                    r"ohg|ug|e\.\s?v\.|ev|ltd\.?|inc\.?|llc|plc|s\.a\.|b\.v\.|bv|co\.?|holding)\b")
_STOP = {"und", "and", "oder", "or", "fur", "fuer", "for", "the", "der", "die", "das", "des", "den", "dem",
         "mit", "with", "im", "in", "am", "an", "at", "zu", "zum", "zur", "of", "von", "bei", "a", "als",
         "m", "w", "d", "f", "x", "i", "remote", "berlin", "hybrid", "vollzeit", "teilzeit", "job",
         "stelle", "deutschlandweit", "bundesweit", "germany", "deutschland", "de", "en", "jobs"}
# generic company words: a hit on these alone does not prove the employer
_GENERIC = {"software", "consulting", "solutions", "services", "service", "digital", "group", "gruppe",
            "systems", "system", "technologies", "technology", "tech", "data", "information",
            "communications", "internet", "provider", "management", "partner", "partners", "berlin",
            "deutschland", "germany", "international", "global", "it", "ki", "ai", "bank", "gesellschaft",
            "mail", "com", "net", "web", "media", "online", "cloud", "health", "energy", "the", "und"}
_LEVEL = {"senior", "junior", "lead", "principal", "staff", "head", "werkstudent", "werkstudentin",
          "praktikum", "praktikant", "intern", "internship", "trainee", "ausbildung", "azubi",
          "director", "chief", "teamlead", "teamleiter", "distinguished", "duales", "dual"}


def _fold(s: str) -> str:
    s = s.lower().replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _tokens(s: str | None) -> set[str]:
    s = _GENDER.sub(" ", s or "").replace("\xad", "")
    return {t for t in re.split(r"[^a-z0-9+#]+", _fold(s)) if t and t not in _STOP}


_PLACE = re.compile(r"(?i)\b(remote|homeoffice|home office|hybrid|berlin|hamburg|münchen|munich|köln|"
                    r"frankfurt|stuttgart|düsseldorf|leipzig|dresden|hannover|nürnberg|deutschlandweit|"
                    r"bundesweit|standort|germany|deutschland)\b")


def clean_title(title: str) -> str:
    """Title as an employer would write it: no gender tag, no trailing location/marketing tail."""
    s = _GENDER.sub(" ", title or "").replace("\xad", "")
    # "... - Konstanz, Berlin oder remote": a short tail naming places goes; "- Risk Management
    # (Liquiditäts- oder Marktpreisrisiko)" stays (an earlier "oder" trigger ate it -> wrong zeb job)
    m = re.search(r"\s+[-–|]\s+([^-–|]*)$", s)
    if m and len(m.group(1).split()) <= 6 and _PLACE.search(m.group(1)):
        s = s[:m.start()]
    s = re.sub(r"\(\s*\)", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -–|,")


def company_core(company: str) -> str:
    s = _LEGAL.sub(" ", company or "")
    s = re.sub(r"[\"'„“”]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" ,.-&")


def _company_keys(company: str) -> tuple[str, set[str]]:
    """(squashed slug, distinctive tokens) of the company name."""
    core = _fold(company_core(company))
    slug = re.sub(r"[^a-z0-9]", "", core)
    toks = {t for t in re.split(r"[^a-z0-9]+", core) if len(t) >= 3 and t not in _GENERIC}
    return slug, toks


def _company_in(text: str, slug: str, toks: set[str]) -> bool:
    t = _fold(text)
    squashed = re.sub(r"[^a-z0-9]", "", t)
    if len(slug) >= 4 and slug in squashed:
        return True
    words = set(re.split(r"[^a-z0-9]+", t))
    return bool(toks) and any(k in words or (len(k) >= 5 and k in squashed) for k in toks)


def _overlap(job_title: str, res_title: str, res_url: str) -> float:
    """Share of the posting's title words found in the result (title + url slug).
    A seniority word on only one side = a different job at the same company -> 0."""
    jt = _tokens(job_title)
    if not jt:
        return 0.0
    _, path = _split(res_url)
    rt = _tokens(res_title) | _tokens(path.replace("-", " ").replace("_", " "))
    if (jt & _LEVEL) != (_tokens(res_title) & _LEVEL) and (jt & _LEVEL or _tokens(res_title) & _LEVEL):
        # url slugs are often truncated: also accept the level words found in the slug
        if (jt & _LEVEL) != (rt & _LEVEL):
            return 0.0
    hit = sum(1 for t in jt if t in rt or any(_near(t, r) for r in rt))
    if hit < min(2, len(jt)):
        return 0.0
    return hit / len(jt)


def _near(a: str, b: str) -> bool:
    """Same word give or take a typo ("Date Science") or a truncated url slug ("consult")."""
    if len(a) < 4 or len(b) < 4:
        return False
    if len(a) >= 5 and len(b) >= 5 and (a.startswith(b) or b.startswith(a)):
        return True
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


# short titles must match fully ("Senior Big Data Engineer" != "... Big Data Architect")
def _enough(title: str, score: float) -> bool:
    n = len(_tokens(title))
    miss = 0 if n <= 4 else int(n * 0.3)      # 5-6 words: 1 may differ, 7-9: 2, ...
    return score >= (n - miss) / n if n else False


async def _searx(client: httpx.AsyncClient, searx: str, q: str) -> list[dict]:
    async with _searx_gate:
        r = await client.get(f"{searx.rstrip('/')}/search",
                             params={"q": q, "format": "json", "engines": ENGINES},
                             timeout=SEARX_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    res = d.get("results") or []
    if not res and d.get("unresponsive_engines"):
        # every engine 429/CAPTCHA'd: "nothing found" would be a lie, the caller retries later
        raise RuntimeError(f"searx engines down: {d['unresponsive_engines']}")
    return res


def _pick(results: list[dict], title: str, company: str) -> dict | None:
    slug, toks = _company_keys(company)
    best, best_key = None, None
    for rank, res in enumerate(results):
        url = res.get("url") or ""
        c = classify(url)
        if not c:
            continue
        rtitle = res.get("title") or ""
        host, path = _split(url)
        if c["ats"] == "career":
            # aggregators sit on jobs.<x>.de and quote the company: only the HOST proves it
            if not _company_in(_domain(host), slug, toks):
                continue
        # ATS urls are often bare ids (b-ite, personio): the snippet names the employer
        elif not _company_in(f"{host} {path} {rtitle} {res.get('content') or ''}", slug, toks):
            continue
        score = _overlap(title, rtitle, url)
        if not _enough(title, score):
            continue
        key = (c["ats"] != "career", round(score, 2), -rank)
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best


async def find(client: httpx.AsyncClient, job: dict, searx: str = SEARX) -> dict | None:
    """The same posting on the employer's own ATS / career page, via SearXNG. Never raises."""
    return (await _find(client, job, searx))[0]


async def _find(client: httpx.AsyncClient, job: dict, searx: str) -> tuple[dict | None, bool]:
    """(hit, searched). searched=False: SearXNG / its engines failed, worth another try later."""
    try:
        title, company = clean_title(job.get("title") or ""), (job.get("company") or "").strip()
        core = company_core(company)
        if not title or not core:
            return None
        # quoting a long legal name ("Materna Information & Communications") returns nothing;
        # the quoted title + loose company finds it, the loose second try catches rewording
        queries = [f'"{title}" {core}', f'{title} {core} jobs']
        for q in queries:
            hit = _pick(await _searx(client, searx, q), title, company)
            if hit:
                return {**hit, "via": "search"}, True
        return None, True
    except Exception:              # a lookup helper: the poller must never die on it
        return None, False


# --- the entry point -------------------------------------------------------

_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'()\[\]{}]+")


def _desc_links(text: str | None) -> list[dict]:
    out = []
    for m in _URL.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?")
        if url.lower().startswith("www."):
            url = "https://" + url
        c = classify(url)
        if c:
            out.append(c)
    out.sort(key=lambda c: c["ats"] == "career")   # stable: ATS links first
    return out


def _portal_label(job: dict) -> str:
    return portal(job.get("url")) or (job.get("source") or "Portal")


async def resolve(client: httpx.AsyncClient, job: dict, search: bool = True,
                  searx: str = SEARX) -> dict:
    """Best place to apply for one job. Always returns a dict."""
    desc = job.get("description") or ""
    mails = [e for e in [(job.get("apply_email") or "").strip().lower()] if e] + emails(desc)
    mails = list(dict.fromkeys(mails))
    email = mails[0] if mails else None

    retry = False

    def out(channel, label, url, via):
        # retry: the search leg could not run (SearXNG down / engines suspended) -> resolve again later
        return {"channel": channel, "label": label, "url": url, "email": email, "via": via,
                "retry": retry}

    apply_url = (job.get("apply_url") or "").strip()
    if apply_url:
        c = classify(apply_url)
        if c:
            return out(c["ats"], c["label"], apply_url, "posting")
        host, _ = _split(apply_url)
        if host and not portal(apply_url) and not any(r in host for r in _REDIRECTORS):
            return out("website", "Website des Arbeitgebers", apply_url, "posting")
    links = _desc_links(desc)
    if links:
        return out(links[0]["ats"], links[0]["label"], links[0]["url"], "description")
    if email:
        via = "posting" if job.get("apply_email") and email == job["apply_email"].strip().lower() else "description"
        return out("email", "E-Mail", f"mailto:{email}", via)
    if search and job.get("company"):
        hit, searched = await _find(client, job, searx)
        retry = not searched
        if hit:
            return out(hit["ats"], hit["label"], hit["url"], "search")
    if apply_url:     # a redirector / portal apply link still beats the bare posting
        return out("portal", _portal_label(job), apply_url, "posting")
    return out("portal", _portal_label(job), job.get("url"), "posting")


# --- self-test -------------------------------------------------------------

_CLASSIFY_CASES = [
    ("https://join.com/companies/hivebuy/16820224-senior-full-stack-engineer", "join"),
    ("https://join.com/", None),
    ("https://acme.jobs.personio.de/job/123456?language=de", "personio"),
    ("https://acme.jobs.personio.com/job/1", "personio"),
    ("https://boards.greenhouse.io/acme/jobs/4012345", "greenhouse"),
    ("https://job-boards.eu.greenhouse.io/acme/jobs/4012345", "greenhouse"),
    ("https://jobs.lever.co/acme/1b2c3d", "lever"),
    ("https://jobs.eu.lever.co/acme/1b2c3d", "lever"),
    ("https://jobs.ashbyhq.com/acme/uuid", "ashby"),
    ("https://apply.workable.com/acme/j/ABC123/", "workable"),
    ("https://jobs.smartrecruiters.com/Acme/7445-backend", "smartrecruiters"),
    ("https://acme.recruitee.com/o/backend-dev", "recruitee"),
    ("https://acme.softgarden.io/job/123", "softgarden"),
    ("https://career5.successfactors.eu/career?company=acme", "successfactors"),
    ("https://acme.wd3.myworkdayjobs.com/en-US/External/job/Berlin/Dev_R1", "workday"),
    ("https://acme.dvinci-hr.com/de/jobs/12/backend", "dvinci"),
    ("https://karriere.acme.de/dvinci/jobs/12", "dvinci"),
    ("https://acme.onlyfy.jobs/job/abc", "onlyfy"),
    ("https://acme.prescreen.io/job/abc", "onlyfy"),
    ("https://acme-recruitment.rexx-systems.com/job-123.html", "rexx"),
    ("https://recruitingapp-1234.umantis.com/Vacancies/12/Description/1", "umantis"),
    ("https://jobs.b-ite.com/jobposting/abc", "bite"),
    ("https://acme.concludis.de/prj/shw/abc.htm", "concludis"),
    ("https://acme.teamtailor.com/jobs/123-dev", "teamtailor"),
    ("https://www.comeet.com/jobs/acme/A1.B2/dev/C3.D4", "comeet"),
    ("https://acme.breezy.hr/p/abc-dev", "breezy"),
    ("https://acme.bamboohr.com/careers/12", "bamboohr"),
    ("https://acme.heyrecruit.de/job/1", "heyrecruit"),
    ("https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1", "taleo"),
    ("https://careers-acme.icims.com/jobs/1/dev/job", "icims"),
    ("https://jobs.jobvite.com/acme/job/o1", "jobvite"),
    ("https://acme.pinpointhq.com/postings/uuid", "pinpoint"),
    ("https://acme.homerun.co/backend-dev", "homerun"),
    ("https://jobs.polymer.co/acme/1234", "polymer"),
    ("https://acme.factorialhr.de/job_posting/dev-1", "factorial"),
    ("https://acme.kenjo.io/jobs/dev", "kenjo"),
    ("https://karriere.acme.de/stellen/123", "career"),
    ("https://www.acme.de/karriere/backend-entwickler", "career"),
    ("https://careers.acme.com/job/123", "career"),
    ("https://www.acme.com/en/careers", "career"),
    ("https://www.acme.de/impressum", None),
    ("https://www.stepstone.de/stellenangebote--Dev-Berlin-Acme--123-inline.html", None),
    ("https://de.linkedin.com/jobs/view/123", None),
    ("https://www.xing.com/jobs/berlin-dev-123", None),
    ("https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1-S", None),
    ("https://de.indeed.com/viewjob?jk=abc", None),
    ("https://jobs.heise.de/stellenangebote/dev-123", None),
    ("not a url", None),
]
_EMAIL_CASES = [
    ("Bitte senden Sie Ihre Unterlagen an bewerbung@acme.de. Fragen: info@acme.de, "
     "Datenschutz: datenschutz@acme.de", ["bewerbung@acme.de"]),
    ("Kontakt: Frau Müller, jobs (at) acme-gmbh (dot) de oder noreply@acme.de", ["jobs@acme-gmbh.de"]),
    ("Ihre Bewerbung an karriere[at]acme.de – Ansprechpartnerin anna.schmidt@acme.de",
     ["karriere@acme.de", "anna.schmidt@acme.de"]),
    ("Fragen? info@acme.de. Wir freuen uns!", ["info@acme.de"]),
    ("Nur noreply@acme.de und privacy@acme.de, logo@2x.png", []),
    ("Schreib uns: HR@Acme.com oder recruiting(at)acme.com", ["hr@acme.com", "recruiting@acme.com"]),
]


async def _selftest() -> None:
    bad = 0
    for url, want in _CLASSIFY_CASES:
        got = (classify(url) or {}).get("ats")
        if got != want:
            bad += 1
            print(f"classify FAIL {url}: {got} != {want}")
    for text, want in _EMAIL_CASES:
        got = emails(text)
        if got != want:
            bad += 1
            print(f"emails FAIL {text[:50]!r}: {got} != {want}")
    print(f"unit: {len(_CLASSIFY_CASES) + len(_EMAIL_CASES) - bad} ok, {bad} failed")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await resolve(client, {"source": "stepstone", "url": "https://www.stepstone.de/x",
                                   "title": "Dev", "company": None,
                                   "description": "Bewerbung über https://acme.jobs.personio.de/job/1 "
                                                  "oder bewerbung@acme.de"}, search=False)
        assert r["channel"] == "personio" and r["email"] == "bewerbung@acme.de", r
        r = await resolve(client, {"source": "stepstone", "url": "https://www.stepstone.de/x",
                                   "title": "Dev", "company": None, "description": "nichts"}, search=False)
        assert r["channel"] == "portal" and r["label"] == "StepStone", r
        r = await resolve(client, {"source": "ba", "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/1",
                                   "title": "Dev", "company": "Acme", "apply_url": "https://www.acme.de/apply?id=7",
                                   "description": "info@acme.de"}, search=False)
        assert r["channel"] == "website" and r["email"] == "info@acme.de", r
        r = await resolve(client, {"source": "xing", "url": "https://www.xing.com/jobs/x-1",
                                   "title": "Dev", "company": "Acme",
                                   "apply_url": "https://www.xing.com/jobs/x-1/apply",
                                   "description": "Unterlagen an karriere (at) acme (punkt) de"}, search=False)
        assert r["channel"] == "email" and r["url"] == "mailto:karriere@acme.de", r
        r = await resolve(client, {"source": "stepstone", "url": "https://www.stepstone.de/x",
                                   "title": "Dev", "company": "Acme"}, searx="http://127.0.0.1:9")
        assert r["channel"] == "portal" and r["retry"] is True, r
        print("resolve: offline cases ok")
        r = await resolve(client, {"source": "stepstone", "company": "SEITENBAU GmbH",
                                   "url": "https://www.stepstone.de/stellenangebote--x.html",
                                   "title": "DevOps & Cloud Engineer (m/w/d) - Konstanz, Berlin oder remote"})
        print("resolve live:", r)
        # live: real (title, company) pairs as "Title<TAB>Company" lines on stdin
        import sys
        pairs = [ln.split("\t", 1) for ln in sys.stdin.read().splitlines() if "\t" in ln] \
            if not sys.stdin.isatty() else []
        for title, company in pairs:
            t = asyncio.get_running_loop().time()
            hit = await find(client, {"title": title.strip(), "company": company.strip()})
            dt = asyncio.get_running_loop().time() - t
            print(f"{dt:4.1f}s {title.strip()[:55]:55} | {company.strip()[:22]:22} -> "
                  f"{(hit or {}).get('label')} {(hit or {}).get('url')}")


if __name__ == "__main__":
    asyncio.run(_selftest())
