"""XING Jobs, logged out — everything comes from the Apollo cache the server embeds in the page.

Verified 2026-10-19 from the home IP (no login, no cookies):
  GET https://www.xing.com/jobs/search?keywords=..&location=Berlin  -> 301 -> /jobs/search/ki?.. (200, ~390 KB, noindex)
      The page carries `<script id="runtime-config">window.crate={..}` — a JS literal (contains bare
      `undefined`), with serverData.APOLLO_STATE: ROOT_QUERY['jobSearchByQuery({..})'].collection[] ->
      {jobDetail: {__ref: 'VisibleJob:<id>.<hash>'}}. The ROOT_QUERY key echoes the variables the
      server actually used, which is how every param below was checked.
  params (names from the search bundle's URL map):
      keywords, location (free text), radius (km, any int; UI offers 0/10/20/50/70/100/200),
      page (20 per page, offset = 20*(page-1)), remoteOption=FULL_REMOTE.050e26 (the id NEEDS the hash
      suffix — bare FULL_REMOTE makes the query come back null; PARTLY_REMOTE.ca71ca / NON_REMOTE.3ca273),
      since=<ISO datetime> (filters on refreshedAt; a bare date is rejected), sincePeriod=LAST_24_HOURS|
      LAST_WEEK|LAST_MONTH (upper case only), sort=date is accepted but ignored: /ki is always
      searchMode SEMANTIC, so results are relevance-ordered and fuzzy (a "Programmiertutor" shows up
      for "python entwickler"), and the set is shallow (~50 hits for Berlin r0, then empty pages).
      When a page is empty XING adds a second, widened jobSearchByQuery (radius 70, trackRecent false);
      we only read the one with trackRecent true.
  teaser (VisibleJob on the search page): id, url, title, refreshedAt, location(s).city,
      employmentType.localizationValue, companyInfo.companyNameOverride, salary, application.
      salary.__typename: SalaryEstimate (XING's guess, has median) vs SalaryRange (employer-stated).
      application.__typename: UrlApplication{applyUrl} (employer site / ATS, already on the teaser),
      JobXingApplication / JobXingCustomApplication (apply on XING), EmailApplication{applyEmail},
      MessageApplication{contactUserId}. redirectsToThirdPartyUrl=true marks tracker links (appcast).
  detail GET https://www.xing.com/jobs/<slug>-<id> -> same window.crate, ROOT_QUERY['job({..slug..})']:
      activatedAt, remoteOptions [FULL_REMOTE|PARTLY_REMOTE|NON_REMOTE], location.zipCode, language,
      description = HtmlDescription{content} | TemplateData{genericDescription, *Content} |
      ExternalUrlDescription{url, proxyUrl, htmlContent}.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .common import HEADERS, TIMEOUT, Limiter, guess_mode, job, text, yearly

SOURCE = "xing"
LABEL = "XING"
INTERVAL = 7.0

BASE = "https://www.xing.com"
SEARCH = BASE + "/jobs/search/ki"   # call the redirect target directly: saves a 301 per page
PAGE_SIZE = 20
REMOTE_ID = "FULL_REMOTE.050e26"

_CRATE = re.compile(r'<script id="runtime-config">\s*window\.crate=(.*?);?\s*</script>', re.S)
_UNDEF = re.compile(r"(?<=[:,\[])undefined(?=[,}\]])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
_AGENCY = re.compile(r"(?i)vermittelt von|personalvermittlung|arbeitsvermittlung|personaldienst|"
                     r"zeitarbeit|arbeitnehmerüberlassung|recruiting|headhunt|personalberatung")
# hosts that only bounce to the real ATS: worth one extra request to learn the employer's host.
# click.appcast.io is NOT here: it answers 403 + DataDome captcha to anything but a browser.
_TRACKER = re.compile(r"(?i)(^|\.)(clickcast\.\w+|jobg8\.com|jobsyn\.\w+|joveo\.com|recruitics\.com)$")

_EMPLOYMENT = {  # XING's localized labels (teaser has no ids) -> ours
    "Vollzeit": ["vollzeit"], "Teilzeit": ["teilzeit"], "Selbstständig": ["freelance"],
    "Saisonarbeit": ["befristet"], "Aushilfe": ["befristet"],
}
_MODE = {"FULL_REMOTE": "remote", "PARTLY_REMOTE": "hybrid", "NON_REMOTE": "onsite"}


def _state(page: str) -> dict:
    m = _CRATE.search(page)
    if not m:
        raise RuntimeError("xing: no window.crate in page (layout change or wall)")
    crate = json.loads(_UNDEF.sub("null", m.group(1)))
    if crate.get("loginState") not in (None, "LOGGED_OUT"):
        raise RuntimeError(f"xing: unexpected loginState {crate.get('loginState')}")
    return crate["serverData"]["APOLLO_STATE"]


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _strip_utm(url: str) -> str:
    p = urlsplit(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.startswith("utm_")]
    return urlunsplit(p._replace(query=urlencode(qs)))


def _employment(label: str | None, title: str) -> list[str]:
    if label == "Studierende":   # XING lumps Praktikum + Werkstudent together
        return ["praktikum"] if re.search(r"(?i)praktik|intern", title) else ["werkstudent"]
    return list(_EMPLOYMENT.get(label or "", []))


def _salary(sal: dict | None) -> dict:
    if not sal or sal.get("minimum") is None:
        return {}
    lo, hi, cur = sal.get("minimum"), sal.get("maximum"), sal.get("currency") or "EUR"
    txt = f"{cur} {lo:,} – {hi:,}".replace(",", ".") if hi else f"{cur} {lo:,}".replace(",", ".")
    if sal.get("__typename") == "SalaryEstimate":
        return {"attrs": {"salary_estimate": txt + " (XING-Schätzung)"}}
    out = {"salary_text": txt}
    # XING asks employers for a yearly figure, but some type monthly ones (500–1500 seen):
    # only trust it as yearly when it looks like one, otherwise keep the text alone.
    if cur == "EUR" and lo >= 10000:
        out["salary_min"] = yearly(lo, "year")
        out["salary_max"] = yearly(hi, "year") if hi else None
    return out


def _application(app: dict | None) -> dict:
    app = app or {}
    t = app.get("__typename")
    if t == "UrlApplication" and app.get("applyUrl"):
        return {"apply_url": _strip_utm(app["applyUrl"]), "apply_kind": "url"}
    if t == "EmailApplication":
        return {"apply_email": app.get("applyEmail"), "apply_kind": "email"}
    if t in ("JobXingApplication", "JobXingCustomApplication"):
        return {"apply_kind": "xing"}   # Schnellbewerbung on XING itself
    if t == "MessageApplication":
        return {"apply_kind": "message"}
    return {}


def _to_job(v: dict, st: dict) -> dict:
    title = v.get("title") or ""
    ci = v.get("companyInfo") or {}
    company = ci.get("companyNameOverride")
    if not company and (ref := (ci.get("company") or {}).get("__ref")):
        company = (st.get(ref) or {}).get("companyName")
    cities = []
    for loc in v.get("locations") or [v.get("location") or {}]:
        if loc and loc.get("city") and loc["city"] not in cities:
            cities.append(loc["city"])
    location = ", ".join(cities) or None
    et = v.get("employmentType") or {}
    if "__ref" in et:
        et = st.get(et["__ref"]) or {}
    app = _application(v.get("application"))
    sal = _salary(v.get("salary"))
    attrs = {**sal.pop("attrs", {})}
    if app.get("apply_kind"):
        attrs["apply"] = app["apply_kind"]
    if v.get("redirectsToThirdPartyUrl"):
        attrs["third_party"] = "1"
    if v.get("topJob") or v.get("paid"):
        attrs["paid"] = "1"
    return job(
        SOURCE, str(v["id"]).split(".")[0], v.get("url") or f"{BASE}/jobs/{v['slug']}", title,
        company=company,
        location=location,
        work_mode=guess_mode(title, location),   # "Homeoffice" shows up as a city
        employment=_employment(et.get("localizationValue"), title),
        posted_at=_ts(v.get("activatedAt") or v.get("refreshedAt")),
        apply_url=app.get("apply_url"),
        apply_email=app.get("apply_email"),
        agency=True if company and _AGENCY.search(company) else None,
        attrs=attrs,
        **sal,
    )


async def _get(client: httpx.AsyncClient, url: str, limiter: Limiter, **params) -> httpx.Response:
    await limiter.wait()
    r = await client.get(url, params=params or None, headers=HEADERS, timeout=TIMEOUT,
                         follow_redirects=True)
    if r.status_code in (403, 429) or "login.xing.com" in str(r.url):
        raise RuntimeError(f"xing: wall {r.status_code} at {r.url}")
    r.raise_for_status()
    return r


def _result(st: dict) -> tuple[bool, list[dict]]:
    """(query ok, jobs) from the search page; skips XING's widened zero-result fallback query."""
    rq = st.get("ROOT_QUERY") or {}
    key = next((k for k in rq if k.startswith("jobSearchByQuery(") and '"trackRecent":true' in k), None)
    if key is None or rq[key] is None:
        return False, []
    out = []
    for item in rq[key].get("collection") or []:
        ref = (item.get("jobDetail") or {}).get("__ref")
        v = st.get(ref) if ref else None
        if v and v.get("__typename") == "VisibleJob":
            out.append(_to_job(v, st))
    return True, out


async def search(client: httpx.AsyncClient, q: dict, limiter: Limiter) -> list[dict]:
    seen: dict[str, dict] = {}
    for kw in q.get("keywords") or []:
        params: dict = {"keywords": kw}
        if q.get("place") and not q.get("remote"):
            params["location"] = q["place"]
            params["radius"] = int(q.get("radius_km") or 0)
        if q.get("remote"):
            params["remoteOption"] = REMOTE_ID
        if q.get("max_age_days"):
            since = datetime.now(timezone.utc) - timedelta(days=q["max_age_days"])
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%S")
        for page in range(1, max(1, q.get("max_pages") or 1) + 1):
            if page > 1:
                params["page"] = page
            st = _state((await _get(client, SEARCH, limiter, **params)).text)
            ok, jobs = _result(st)
            if not ok:   # a filter value XING no longer knows (e.g. the remote id's hash changed)
                raise RuntimeError(f"xing: search query rejected for {params}")
            for j in jobs:
                if q.get("remote"):
                    j["work_mode"] = "remote"   # XING filtered on FULL_REMOTE for us
                seen.setdefault(j["source_id"], j)
            if len(jobs) < PAGE_SIZE:
                break
    return list(seen.values())


def _description(d: dict | None) -> str | None:
    d = d or {}
    t = d.get("__typename")
    if t == "HtmlDescription":
        parts = [d.get("content")]
    elif t == "TemplateData":
        parts = [d.get("genericDescription")]
        for sec in ("companyDescription", "responsibility", "skills", "weOffer", "contactInfo"):
            if d.get(sec + "Content"):
                parts += [f"<h3>{d.get(sec + 'Title') or ''}</h3>", d[sec + "Content"]]
    elif t == "ExternalUrlDescription":
        parts = [d.get("htmlContent")]
    else:
        parts = []
    return text("\n".join(p for p in parts if p)) or None


async def _unbounce(client: httpx.AsyncClient, url: str, limiter: Limiter) -> str:
    """Tracker link -> the URL it points at (one hop, no body)."""
    if not _TRACKER.search(urlsplit(url).hostname or ""):
        return url
    await limiter.wait()
    try:
        r = await client.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=False)
    except httpx.HTTPError:
        return url
    loc = r.headers.get("location")
    return _strip_utm(str(r.url.join(loc))) if r.is_redirect and loc else url


async def detail(client: httpx.AsyncClient, j: dict, limiter: Limiter) -> dict | None:
    r = await _get(client, j["url"], limiter)
    st = _state(r.text)
    rq = st.get("ROOT_QUERY") or {}
    ref = next((rq[k] for k in rq if k.startswith("job(") and rq[k]), None)
    v = st.get(ref["__ref"]) if isinstance(ref, dict) and "__ref" in ref else ref
    if not v or v.get("__typename") != "VisibleJob":
        return None   # expired postings come back as a different typename
    d = _to_job(v, st)
    modes = set(v.get("remoteOptions") or [])
    for m in ("FULL_REMOTE", "PARTLY_REMOTE", "NON_REMOTE"):
        if m in modes:
            d["work_mode"] = _MODE[m]
            break
    else:
        d["work_mode"] = j.get("work_mode") or d["work_mode"]
    loc = v.get("location") or {}
    d["postal_code"] = loc.get("zipCode")
    d["lang"] = v.get("language") if v.get("language") in ("de", "en") else None
    d["description"] = _description(v.get("description"))
    desc = v.get("description") or {}
    if desc.get("__typename") == "ExternalUrlDescription" and desc.get("url"):
        d["attrs"]["external_ad"] = desc["url"]
    if d["apply_url"]:
        d["apply_url"] = await _unbounce(client, d["apply_url"], limiter)
    if not d["apply_email"] and d["description"]:
        # only an address the ad itself presents as the way to apply
        m = re.search(r"(?is)(bewerb|apply|application|unterlagen|lebenslauf|cv)[^@]{0,200}?("
                      + _EMAIL.pattern + ")", d["description"])
        if m:
            d["apply_email"] = m.group(2)
    d["attrs"] = {**j.get("attrs", {}), **d["attrs"]}
    return d


if __name__ == "__main__":
    async def _main() -> None:
        lim = Limiter(INTERVAL)
        async with httpx.AsyncClient(http2=False) as c:
            base = {"keywords": ["python entwickler"], "place": "Berlin", "radius_km": 25,
                    "max_pages": 1, "max_age_days": 0}
            jobs = await search(c, {**base, "remote": False}, lim)
            print(f"-- Berlin r25: {len(jobs)}")
            remote = await search(c, {**base, "remote": True}, lim)
            print(f"-- remote: {len(remote)}")

            def line(j: dict) -> str:
                ts = datetime.fromtimestamp(j["posted_at"]).date() if j["posted_at"] else None
                sal = j["salary_text"] or j["attrs"].get("salary_estimate")
                return (f"{j['title'][:50]} | {j['company']} | {j['location']} | {j['work_mode']} | {ts} | "
                        f"{j['apply_url'] or j['apply_email'] or j['attrs'].get('apply')} | {sal}")
            for j in jobs + remote:
                print(line(j))
            # one of each apply kind, plus a tracker link when present
            pick, kinds = [], set()
            for j in jobs + remote:
                k = (j["attrs"].get("apply"), "third_party" in j["attrs"])
                if k not in kinds:
                    kinds.add(k)
                    pick.append(j)
            print("-- details")
            pick += [j for j in remote[:6] if j["apply_url"] and j not in pick][:2]
            for j in pick[:6]:
                d = await detail(c, j, lim)
                if d:
                    print(line(d))
                    print(f"   {d['postal_code']} lang={d['lang']} email={d['apply_email']} "
                          f"desc={len(d['description'] or '')}ch attrs={d['attrs']}")
    asyncio.run(_main())
