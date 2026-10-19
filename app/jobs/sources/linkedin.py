"""LinkedIn public (logged-out) job search. No login, no cookies, ever.

Verified 2026-10-19 from the home IP (pve / CT 107):
  search: GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
          -> 200 + HTML fragment of <li> base-cards, 10 per page (start=0,10,20 ...).
          keywords   free text
          location   "Berlin" / "Germany"; geoId=101282230 pins Germany (used when place is None)
          distance   miles; the UI offers 0/5/10/25/50/100, we round up to those
          f_WT       1 onsite, 2 remote, 3 hybrid (comma list ok)
          f_JT       F full-time, P part-time, C contract, T temporary, I internship
          f_TPR      r<seconds> posted within (r86400 = 24 h, r604800 = 7 d)
          sortBy=DD  newest first (without it relevance pulls in year-old postings)
  card:   data-entity-urn="urn:li:jobPosting:<id>", base-search-card__title/__subtitle,
          job-search-card__location, <time datetime="YYYY-MM-DD"> (date only),
          job-search-card__salary-info (rare in DE). No work mode on the card.
  detail: GET https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>
          -> top card (company, "Berlin, Berlin, Germany", "9 hours ago"),
          show-more-less-html__markup (full description), description__job-criteria-*
          (Seniority level, Employment type, Job function, Industries). The primary button's
          data-tracking-control-name tells Easy Apply (public_jobs_apply-link-[simple_]onsite) from
          offsite (public_jobs_apply-link-offsite_...).
  apply:  logged-out pages used to carry <code id="applyUrl"><!--"https://www.linkedin.com/jobs/view/
          externalApply/<id>?url=<encoded>..."--></code>. In Oct 2026 none of the sampled postings had it:
          offsite jobs only show apply-button__offsite-apply-icon behind a sign-in modal, and
          /jobs/view/externalApply/<id> answers 400 to guests. We still parse applyUrl if it shows up,
          else fall back to an ATS link found inside the description (attrs.apply_from="description").
  quirks: Accept-Language decides the labels ("Vollzeit" vs "Full-time", "Deutschland" vs "Germany"),
          we ask for English so the criteria map is stable. Throttling comes as 429 with a JS-challenge
          page (sometimes even at ~8-10 s spacing), 999 (classic LinkedIn block) or an empty 200.
          The same posting can appear twice with slightly different location text.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

import httpx

from .common import HEADERS, TIMEOUT, Limiter, clean, guess_mode, job, text, yearly

SOURCE = "linkedin"
LABEL = "LinkedIn"
INTERVAL = 10.0  # guest endpoints 429 sporadically even at 8 s

log = logging.getLogger(__name__)

BASE = "https://www.linkedin.com"
SEARCH_URL = BASE + "/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = BASE + "/jobs-guest/jobs/api/jobPosting/{id}"
GERMANY_GEO = "101282230"
PAGE = 10
MILES = (0, 5, 10, 25, 50, 100)
BLOCKED = (429, 999)

# English labels: the criteria parser and card locations depend on them.
LI_HEADERS = {**HEADERS, "Accept-Language": "en-US,en;q=0.9"}

EMPLOYMENT_MAP = {
    "full-time": "vollzeit", "part-time": "teilzeit", "internship": "praktikum",
    "contract": "freelance", "temporary": "befristet",
}
# Hosts that are an employer's application system, not a job board.
ATS_HOSTS = re.compile(
    r"(?i)(^|\.)(join\.com|personio\.(de|com)|jobs\.personio\.de|greenhouse\.io|lever\.co|"
    r"myworkdayjobs\.com|workday\.com|smartrecruiters\.com|recruitee\.com|ashbyhq\.com|"
    r"workable\.com|bamboohr\.com|softgarden\.(io|de)|successfactors\.(com|eu)|teamtailor\.com|"
    r"jobs\.ashbyhq\.com|onlyfy\.jobs|prescreen\.io|rexx-systems\.com|dvinci-easy\.com|"
    r"d\.vinci|umantis\.com|haufe\.com|concludis\.de|jobbase\.io|pinpointhq\.com|"
    r"breezy\.hr|jazzhr\.com|icims\.com|taleo\.net|oraclecloud\.com|eightfold\.ai|"
    r"homerun\.co|factorialhr\.(com|de)|heyrecruit\.de|kenjo\.io|rippling\.com)$")


class Blocked(httpx.HTTPStatusError):
    """LinkedIn throttled us (429 / 999). Poller should back off this source."""


async def _get(client: httpx.AsyncClient, url: str, params: dict | None, limiter: Limiter) -> httpx.Response:
    """One polite GET; a single retry on throttling, then Blocked."""
    for attempt in (0, 1):
        await limiter.wait()
        r = await client.get(url, params=params, headers=LI_HEADERS, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code not in BLOCKED:
            r.raise_for_status()
            return r
        if attempt == 0:
            # a lone 429 often clears after a longer pause; two in a row = really throttled
            await asyncio.sleep(limiter.interval * 3)
    raise Blocked(f"linkedin throttled ({r.status_code})", request=r.request, response=r)


def _miles(km: float | None) -> int | None:
    if km is None:
        return None
    mi = km / 1.609
    return next((m for m in MILES if m >= mi - 0.01), MILES[-1])


def _params(kw: str, q: dict) -> dict:
    p: dict = {"keywords": kw, "sortBy": "DD"}
    place = (q.get("place") or "").strip()
    if place and place.lower() not in ("germany", "deutschland"):
        p["location"] = place
        d = _miles(q.get("radius_km"))
        if d is not None:
            p["distance"] = d
    else:
        p["location"] = "Germany"
        p["geoId"] = GERMANY_GEO
    if q.get("remote"):
        p["f_WT"] = "2"
    if q.get("max_age_days"):
        p["f_TPR"] = f"r{int(q['max_age_days']) * 86400}"
    return p


_CARD_SPLIT = re.compile(r"(?=<div[^>]+data-entity-urn=\"urn:li:jobPosting:)")
_URN = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_TITLE = re.compile(r'base-search-card__title[^>]*>(.*?)</h3>', re.S)
_COMPANY = re.compile(r'base-search-card__subtitle[^>]*>(.*?)</h4>', re.S)
_LOC = re.compile(r'job-search-card__location[^>]*>(.*?)</span>', re.S)
_TIME = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"[^>]*>(.*?)</time>', re.S)
_SALARY = re.compile(r'(?:job-search-card__salary-info|compensation__salary)[^>]*>(.*?)</', re.S)
_MONEY = re.compile(r"([€$£])\s?([\d.,]+)\s?([kK])?")


def _date(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _salary(s: str) -> tuple[float | None, float | None]:
    """'€60,000.00 - €80,000.00/yr' -> yearly EUR; other currencies are left as text only."""
    nums = []
    for cur, n, k in _MONEY.findall(s):
        if cur != "€":
            return None, None
        n = n.replace(",", "")
        try:
            v = float(n) * (1000 if k else 1)
        except ValueError:
            continue
        nums.append(v)
    if not nums:
        return None, None
    unit = "hour" if re.search(r"/h(r|our)", s) else "month" if re.search(r"/mo", s) else "year"
    return yearly(min(nums), unit), yearly(max(nums), unit)


def parse_cards(html_: str, remote: bool) -> list[dict]:
    out = []
    for card in _CARD_SPLIT.split(html_)[1:]:
        m = _URN.search(card)
        t = _TITLE.search(card)
        if not m or not t:
            continue
        jid = m.group(1)
        title = clean(text(t.group(1)))
        company = _COMPANY.search(card)
        loc = _LOC.search(card)
        when = _TIME.search(card)
        location = clean(text(loc.group(1))) if loc else None
        j = job(SOURCE, jid, f"{BASE}/jobs/view/{jid}/", title,
                company=clean(text(company.group(1))) or None if company else None,
                location=location,
                # "5 hours ago" inside <time> is sharper than its date-only datetime
                posted_at=(_ago(clean(text(when.group(2)))) or _date(when.group(1))) if when else None,
                # f_WT=2 is the only place LinkedIn tells guests the work mode
                work_mode="remote" if remote else guess_mode(title, location))
        sal = _SALARY.search(card)
        if sal:
            j["salary_text"] = clean(text(sal.group(1)))
            j["salary_min"], j["salary_max"] = _salary(j["salary_text"])
        out.append(j)
    return out


async def search(client: httpx.AsyncClient, q: dict, limiter: Limiter) -> list[dict]:
    seen: dict[str, dict] = {}
    remote = bool(q.get("remote"))
    for kw in q.get("keywords") or []:
        params = _params(kw, q)
        for page in range(max(1, int(q.get("max_pages") or 1))):
            params["start"] = page * PAGE
            try:
                r = await _get(client, SEARCH_URL, params, limiter)
            except Blocked:
                if not seen:
                    raise
                # keep what we have; the next poll gets the rest
                log.warning("linkedin throttled after %d jobs (%r page %d)", len(seen), kw, page)
                return list(seen.values())
            cards = parse_cards(r.text, remote)
            if not cards:
                if page == 0 and len(r.text.strip()) < 50:
                    log.info("linkedin: empty 200 for %r (no hits or soft block)", kw)
                break
            new = [c for c in cards if c["source_id"] not in seen]
            for c in new:
                seen[c["source_id"]] = c
            if len(cards) < PAGE or not new:
                break
    return list(seen.values())


_TOP_LOC = re.compile(r'topcard__flavor--bullet[^>]*>(.*?)</span>', re.S)
_TOP_COMPANY = re.compile(r'topcard__org-name-link[^>]*>(.*?)</a>', re.S)
_AGO = re.compile(r'posted-time-ago__text[^>]*>(.*?)</span>', re.S)
_APPLICANTS = re.compile(r'num-applicants__caption[^>]*>(.*?)<', re.S)
_DESC = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</section>', re.S)
_CRIT = re.compile(r'description__job-criteria-subheader[^>]*>(.*?)</h3>\s*'
                   r'<span[^>]*description__job-criteria-text[^>]*>(.*?)</span>', re.S)
_APPLY_CODE = re.compile(r'<code id="applyUrl"[^>]*>\s*<!--\s*"?(.*?)"?\s*-->', re.S)
_EXT_HREF = re.compile(r'href="([^"]*externalApply[^"]*)"')
_HREF = re.compile(r'href="(https?://[^"]+)"')
_UNITS = {"minute": 60, "hour": 3600}


def _ago(s: str) -> int | None:
    # only sub-day precision is worth it; the card's date is exact for older postings
    m = re.search(r"(\d+)\s+(minute|hour)", s)
    if not m:
        return None
    return int(time.time() - int(m.group(1)) * _UNITS[m.group(2)])


def _unwrap(url: str) -> str | None:
    """externalApply / redirect wrappers -> the real target; tracking params dropped."""
    url = html.unescape(url).strip()
    u = urllib.parse.urlsplit(url)
    if u.hostname and u.hostname.endswith("linkedin.com"):
        inner = urllib.parse.parse_qs(u.query).get("url")
        if not inner:
            return None
        url = inner[0]
        u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
          if not k.lower().startswith(("utm_", "trk", "refid", "trackingid"))]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(qs), u.fragment))


def _is_ats(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return bool(ATS_HOSTS.search(host))


async def detail(client: httpx.AsyncClient, j: dict, limiter: Limiter) -> dict | None:
    try:
        r = await _get(client, DETAIL_URL.format(id=j["source_id"]), None, limiter)
    except Blocked:
        raise
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 410):  # posting withdrawn
            return None
        raise
    h = r.text
    if "top-card-layout" not in h:
        return None
    d = dict(j)
    d["attrs"] = dict(j.get("attrs") or {})

    if m := _TOP_COMPANY.search(h):
        d["company"] = clean(text(m.group(1))) or d["company"]
    if m := _TOP_LOC.search(h):
        d["location"] = clean(text(m.group(1))) or d["location"]
    if m := _AGO.search(h):
        # "9 hours ago" beats the card's date-only stamp
        d["posted_at"] = _ago(clean(text(m.group(1)))) or d["posted_at"]
    if m := _APPLICANTS.search(h):
        d["attrs"]["applicants"] = clean(m.group(1))

    desc_html = ""
    if m := _DESC.search(h):
        desc_html = re.split(r"<button", m.group(1), maxsplit=1)[0]
        d["description"] = text(desc_html) or d["description"]

    for k, v in _CRIT.findall(h):
        k, v = clean(text(k)).lower(), clean(text(v))
        if k == "seniority level":
            d["attrs"]["seniority"] = v
        elif k == "employment type":
            d["attrs"]["employment_type"] = v
            emp = EMPLOYMENT_MAP.get(v.lower())
            if emp and emp not in d["employment"]:
                d["employment"] = [*d["employment"], emp]
        elif k == "job function":
            d["attrs"]["function"] = v
        elif k == "industries":
            # e.g. "Staffing and Recruiting": a hint for the reader, not proof of an agency posting
            d["attrs"]["industries"] = v

    if (sal := _SALARY.search(h)) and not d["salary_text"]:
        d["salary_text"] = clean(text(sal.group(1)))
        d["salary_min"], d["salary_max"] = _salary(d["salary_text"])

    if d["work_mode"] is None:
        d["work_mode"] = guess_mode(d["title"], d["location"])

    # tracking names: apply-link-offsite = employer site; apply-link-onsite / -simple_onsite = Easy Apply
    offsite = "apply-link-offsite" in h
    if offsite or re.search(r"apply-link-(simple_)?onsite", h):
        d["attrs"]["apply"] = "offsite" if offsite else "easy_apply"
    apply = None
    if m := _APPLY_CODE.search(h) or _EXT_HREF.search(h):
        apply = _unwrap(m.group(1))
    if not apply and offsite and desc_html:
        # the description often links the ATS page itself
        apply = next((u for u in map(_unwrap, _HREF.findall(desc_html)) if u and _is_ats(u)), None)
        if apply:
            d["attrs"]["apply_from"] = "description"
    d["apply_url"] = apply
    if d["description"] and re.search(r"(?i)\b(und|wir|sie|du|für)\b", d["description"][:600]):
        d["lang"] = "de"
    elif d["description"]:
        d["lang"] = "en"
    return d


if __name__ == "__main__":
    from datetime import datetime as _dt

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    def line(j: dict) -> str:
        p = _dt.fromtimestamp(j["posted_at"]).strftime("%m-%d %H:%M") if j["posted_at"] else "-"
        return (f"  {j['title'][:55]} | {j['company']} | {j['location']} | {j['work_mode']} | {p}"
                f" | {j['apply_url'] or '-'} | {j['attrs'].get('apply', '')}")

    async def main():
        lim = Limiter(INTERVAL)
        async with httpx.AsyncClient(http2=False) as c:
            berlin = await search(c, {"keywords": ["python developer"], "place": "Berlin", "radius_km": 25,
                                      "remote": False, "max_age_days": 7, "max_pages": 1}, lim)
            print(f"berlin: {len(berlin)}")
            for j in berlin:
                print(line(j))
            rem = await search(c, {"keywords": ["python developer"], "place": None, "radius_km": 0,
                                   "remote": True, "max_age_days": 7, "max_pages": 1}, lim)
            print(f"remote DE: {len(rem)}")
            for j in rem:
                print(line(j))
            print("details:")
            for j in (berlin[:3] + rem[:3]):
                try:
                    d = await detail(c, j, lim)
                except Blocked as e:
                    print(f"  {j['source_id']}: {e}")
                    continue
                if d:
                    print(line(d))
                    print(f"    {d['employment']} {d['attrs']} lang={d['lang']} desc={len(d['description'] or '')}ch")

    asyncio.run(main())
