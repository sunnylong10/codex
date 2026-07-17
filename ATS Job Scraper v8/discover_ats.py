"""
discover_ats.py — auto-discover career sites via ATS APIs
=========================================================
Takes a list of company brands (e.g. mnc_shortlist.csv) and probes the free,
unauthenticated ATS endpoints to find which ATS each company uses:

  Greenhouse:      GET https://boards-api.greenhouse.io/v1/boards/{slug}
  Lever:           GET https://api.lever.co/v0/postings/{slug}?mode=json
  SmartRecruiters: GET https://api.smartrecruiters.com/v1/companies/{slug}
  Workday:         GET https://{slug}.{wd1|wd3|wd5|wd12}.myworkdayjobs.com
                   (follows redirect to discover the board name)

Outputs:
  ats_discovered.csv          — every hit with job counts + verified names
  career_pages_additions.py   — ready-to-paste entries for scraper.py's
                                CAREER_PAGES dict

Usage:
  python discover_ats.py                              # high-confidence brands
  python discover_ats.py --confidence high medium     # widen the net
  python discover_ats.py --limit 50                   # quick test run
  python discover_ats.py --no-workday                 # skip slow Workday probes
  python discover_ats.py --input my_brands.csv --column Entity

Notes:
  • Skips brands already registered in scraper.py's CAREER_PAGES (same dir).
  • A slug hit is NOT proof it's the right company — same-name startups exist.
    Check the `verified_name` column before pasting entries in. Rows where
    verified_name obviously mismatches the brand are marked needs_review=1.
  • Be polite: default 8 workers + tiny delays. Don't crank this to 100.
"""

import argparse, csv, re, sys, time, random
import html as html_lib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from ats_detection import DetectionContext, DetectionEvidence, default_registry
from career_url_discovery import DiscoveryCache, rank_candidates

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}
WD_VERSIONS = ["wd3", "wd1", "wd5", "wd12"]  # most common first

SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|llp|"
    r"group|holdings?|international|global|technologies|technology)\b\.?", re.I)


# ── slug candidates ───────────────────────────────────────────────────────────

def slug_variants(brand):
    """Generate plausible ATS slugs for a brand name, most likely first."""
    b = brand.strip()
    base = SUFFIXES.sub(" ", b)
    base = re.sub(r"[&+]", " and ", base)
    base = re.sub(r"[^a-zA-Z0-9 ]", "", base).strip()
    words = base.lower().split()
    if not words:
        return []
    cands = []
    joined = "".join(words)
    cands.append(joined)                      # goldmansachs
    if len(words) > 1:
        cands.append("-".join(words))         # goldman-sachs
        cands.append(words[0])                # goldman
    raw_joined = re.sub(r"[^a-z0-9]", "", b.lower())
    cands.append(raw_joined)                  # keeps digits like 3m
    # dedupe, keep order, drop too-short/generic
    out, seen = [], set()
    for c in cands:
        if len(c) >= 2 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ── per-ATS probes (return dict on hit, None on miss) ─────────────────────────

def probe_greenhouse(brand, slug):
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                         headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        name = (r.json() or {}).get("name", "")
        rj = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                          headers=HEADERS, timeout=8)
        jobs = rj.json().get("jobs", []) if rj.status_code == 200 else []
        sg = sum(1 for j in jobs
                 if "singapore" in str(j.get("location", {}).get("name", "")).lower())
        return {"ats": "greenhouse", "identifier": slug,
                "url": f"https://boards.greenhouse.io/{slug}",
                "verified_name": name, "jobs": len(jobs), "sg_jobs": sg}
    except Exception:
        return None


def probe_lever(brand, slug):
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list):
            return None
        sg = sum(1 for j in data
                 if "singapore" in str(j.get("categories", {}).get("location", "")).lower())
        return {"ats": "lever", "identifier": slug,
                "url": f"https://jobs.lever.co/{slug}",
                "verified_name": "", "jobs": len(data), "sg_jobs": sg}
    except Exception:
        return None


def probe_smartrecruiters(brand, slug):
    # NOTE: /v1/companies/{slug} requires auth (always 401) — go straight to
    # the public /postings endpoint instead.
    try:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                         params={"limit": 100}, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        body = r.json()
        content = body.get("content", [])
        total = body.get("totalFound", len(content))
        name = ""
        if content:
            name = ((content[0].get("company") or {}).get("name", "")) or ""
        sg = 0
        for j in content:
            loc = j.get("location", {}) or {}
            blob = " ".join(str(loc.get(k, "")) for k in ("city", "country")).lower()
            if "singapore" in blob or str(loc.get("country", "")).lower() == "sg":
                sg += 1
        return {"ats": "smartrecruiters", "identifier": slug,
                "url": f"https://careers.smartrecruiters.com/{slug}",
                "verified_name": name, "jobs": total, "sg_jobs": sg}
    except Exception:
        return None


WD_URL_RE = re.compile(
    r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#\s]+)")
COMMON_BOARDS = ["External", "Careers", "External_Careers", "Jobs", "careers",
                 "external", "Global", "Search"]
# Workday's edge returns "406 Not Acceptable" to requests without browser-like
# headers. These make the root GET behave like a real browser visit.
BROWSER_PAGE_HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _cxs_probe(tenant, wdver, board):
    """One CXS call. Returns (status, total).
    Empirically: 422 = tenant doesn't exist on this wdN at all,
                 404 = tenant exists but this board name is wrong,
                 200 = board found (total = job count)."""
    base = f"https://{tenant}.{wdver}.myworkdayjobs.com"
    try:
        r = requests.post(
            f"{base}/wday/cxs/{tenant}/{board}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={**HEADERS, "Content-Type": "application/json",
                     "Origin": base, "Referer": f"{base}/{board}"},
            timeout=8)
        total = 0
        if r.status_code == 200:
            try:
                total = r.json().get("total", 0)
            except Exception:
                pass
        return r.status_code, total
    except Exception:
        return None, 0


def _dynamic_boards(slug):
    cap = slug.capitalize()
    return [cap, f"{cap}Careers", f"{cap}_Careers", "External", "Careers",
            "External_Careers", f"{slug}careers", "Jobs", "Global"]


def probe_workday(brand, slug):
    """Find a live Workday tenant + board for a slug.

    DNS is wildcard (resolves for anything) and the root GET is rejected with
    406 unless browser headers are sent — so the ground truth is the CXS API,
    which we always fall back to with brand-derived + common board names.
    """
    for wd in WD_VERSIONS:
        host = f"https://{slug}.{wd}.myworkdayjobs.com"
        candidates = []
        try:
            r = requests.get(host, headers=BROWSER_PAGE_HEADERS,
                             timeout=6, allow_redirects=True)
            text = html_lib.unescape(r.text or "")
            m = WD_URL_RE.match(r.url)              # layer 1: redirect
            if m:
                candidates.append(m.group(3))
            for mm in re.finditer(                   # layer 2: boards in HTML
                    r"myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]{2,40})",
                    text):
                candidates.append(mm.group(1))
        except Exception:
            pass  # root GET is best-effort only

        candidates += _dynamic_boards(slug)          # layer 3: always try CXS

        seen, tried, dead_422 = set(), 0, 0
        for board in candidates:
            bl = board.lower()
            if bl in seen or bl in ("wday", "en-us", "login", "wd", "api"):
                continue
            seen.add(bl)
            tried += 1
            if tried > 12:                           # cap CXS calls per host
                break
            status, total = _cxs_probe(slug, wd, board)
            if status == 200:
                board_url = f"https://{slug}.{wd}.myworkdayjobs.com/{board}"
                return {"ats": "workday", "identifier": board_url,
                        "url": board_url, "verified_name": slug,
                        "jobs": total, "sg_jobs": -1}
            # 422 = tenant-level rejection. Two in a row -> no tenant on this
            # wdN, move on immediately instead of burning the whole board list.
            dead_422 = dead_422 + 1 if status == 422 else 0
            if dead_422 >= 2:
                break
            time.sleep(0.1)
    return None



# ── homepage sniffing (finds non-guessable ATSes like Oracle Recruiting Cloud) ─

ORC_RE = re.compile(
    r"https://[^/\s\"']+\.oraclecloud\.com/hcmUI/CandidateExperience/[a-zA-Z-]+/sites/[^/?#\s\"']+")
ATS_LINK_RES = {
    "oracle":         ORC_RE,
    # classic SF boards always carry ?company=<id>; performancemanager* hosts
    # are SF's login/admin side and /verp/ paths are shared modules — never boards
    "successfactors": re.compile(r"https://(?:career|jobs)[a-z0-9]*\.(?:successfactors|sapsf)\.[a-z.]{2,6}/[^\s\"']*company=[^\s\"'&]+[^\s\"']*"),
    # icims boards live under /jobs; media./static./cdn. hosts are asset servers
    "icims":          re.compile(r"https://(?!media\.|static\.|cdn\.)[a-z0-9-]+\.icims\.com/[^\s\"']*jobs[^\s\"']*"),
    "taleo":          re.compile(r"https://[a-z0-9.-]+\.taleo\.net[^\s\"']*"),
    "eightfold":      re.compile(r"https://[a-z0-9.-]+\.eightfold\.ai[^\s\"']*"),
    "phenom":         re.compile(r"https://careers\.[a-z0-9.-]+/(?:[a-z]{2,5}/[a-z]{2}/)?search-results[^\s\"']*"),
    "avature":        re.compile(r"https://[a-z0-9.-]+\.avature\.net[^\s\"']*"),
    "workable":       re.compile(r"https://apply\.workable\.com/[a-z0-9-]+"),
    "ashby":          re.compile(r"https://jobs\.ashbyhq\.com/[a-zA-Z0-9-]+"),
    "recruitee":      re.compile(r"https://[a-z0-9-]+\.recruitee\.com"),
}
def meaningful_ats_url(url):
    """Reject bare hosts (resource hints like <link rel=preconnect
    href='https://career55.sapsf.eu/'>) — real boards have a path or query."""
    from urllib.parse import urlparse
    p = urlparse(url)
    return bool((p.path and p.path.strip("/")) or p.query)


CAREER_HREF_RE = re.compile(
    r"href=[\"']([^\"']*(?:career|job|join-?us|work-?with|vacan)[^\"']*)[\"']", re.I)



SEARCH_JUNK = ("bing.", "microsoft.", "msn.", "doubleclick", "adservice",
               "javascript", "linkedin.", "glassdoor.", "indeed.", "youtube.",
               "facebook.", "wikipedia.", "instagram.")


def _search_career_urls(brand, country=""):
    """Bing HTML search for '{brand} careers' -> candidate URLs from results."""
    try:
        r = requests.get("https://www.bing.com/search",
                         params={"q": " ".join(p for p in (brand, country, "careers") if p)},
                         headers=BROWSER_PAGE_HEADERS, timeout=8)
        if r.status_code != 200:
            return []
        urls = re.findall(r"https?://[^\s\"'<>]+", html_lib.unescape(r.text))
        out, seen = [], set()
        for u in urls:
            l = u.lower()
            if any(j in l for j in SEARCH_JUNK):
                continue
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out
    except Exception:
        return []


def _with_ats_detection(hit, detection, verified=False):
    if not hit:
        return hit
    if verified and not any(item.evidence_type == "api_verified"
                            for item in detection.evidence):
        detection.evidence.append(
            DetectionEvidence(
                evidence_type="api_verified",
                value=detection.vendor,
                source_url=hit.get("url", ""),
                confidence=1.0,
                verified=True,
            )
        )
    hit["ats_confidence"] = 1.0 if verified else detection.confidence_score
    hit["ats_verification_status"] = "verified" if verified else detection.verification_status
    hit["ats_evidence"] = ";".join(
        f"{item.evidence_type}:{item.value}" for item in detection.evidence
    )
    return hit


def detect_ats(url, html="", final_url="", redirect_chain=(), network_urls=()):
    """Return all plugin detection results, strongest first."""
    return default_registry.detect(
        DetectionContext(
            original_url=url,
            final_url=final_url,
            redirect_chain=tuple(redirect_chain),
            html=html,
            network_urls=tuple(network_urls),
        )
    )


def _hit_from_ats_url(brand, slug, found_url):
    """Turn a raw ATS URL (from search results or page HTML) into a verified hit."""
    detection = detect_ats(found_url)[0]
    if detection.vendor == "workday":
        m = WD_URL_RE.match(found_url)
        if m:
            tenant, wdver, board = m.groups()
            status, total = _cxs_probe(tenant, wdver, board)
            if status == 200:
                board_url = f"https://{tenant}.{wdver}.myworkdayjobs.com/{board}"
                return _with_ats_detection(
                    {"ats": "workday", "identifier": board_url, "url": board_url,
                     "verified_name": tenant, "jobs": total, "sg_jobs": -1},
                    detection,
                    verified=True,
                )
            return None
    if detection.vendor == "greenhouse":
        return _with_ats_detection(
            probe_greenhouse(brand, detection.tenant_id), detection, verified=True
        )
    if detection.vendor == "lever":
        return _with_ats_detection(
            probe_lever(brand, detection.tenant_id), detection, verified=True
        )
    if detection.vendor == "smartrecruiters":
        return _with_ats_detection(
            probe_smartrecruiters(brand, detection.tenant_id), detection, verified=True
        )
    if detection.vendor == "oracle":
        total = _orc_count(found_url)
        if total is not None:
            return _with_ats_detection(
                {"ats": "oracle", "identifier": found_url, "url": found_url,
                 "verified_name": slug, "jobs": total, "sg_jobs": -1},
                detection,
                verified=True,
            )
        return None
    if detection.vendor == "sap_successfactors":
        return _with_ats_detection(
            {"ats": "successfactors (browser)", "identifier": found_url,
             "url": found_url, "verified_name": detection.tenant_id or slug,
             "jobs": -1, "sg_jobs": -1},
            detection,
        )
    # Workday -> verify board via CXS (catches non-guessable tenant names)
    m = WD_URL_RE.match(found_url)
    if m:
        tenant, wdver, board = m.groups()
        status, total = _cxs_probe(tenant, wdver, board)
        if status == 200 and total > 0:
            board_url = f"https://{tenant}.{wdver}.myworkdayjobs.com/{board}"
            return {"ats": "workday", "identifier": board_url, "url": board_url,
                    "verified_name": tenant, "jobs": total, "sg_jobs": -1}
        return None
    # Greenhouse / Lever / SmartRecruiters -> verify via existing probes
    m = re.search(r"boards\.greenhouse\.io/([A-Za-z0-9_-]+)", found_url)
    if m:
        return probe_greenhouse(brand, m.group(1))
    m = re.search(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", found_url)
    if m:
        return probe_lever(brand, m.group(1))
    m = re.search(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)", found_url)
    if m:
        return probe_smartrecruiters(brand, m.group(1))
    # Oracle -> verify via its API
    if ORC_RE.match(found_url):
        total = _orc_count(found_url)
        if total:
            return {"ats": "oracle", "identifier": found_url, "url": found_url,
                    "verified_name": slug, "jobs": total, "sg_jobs": -1}
        return None
    # SAP / iCIMS / Taleo / Phenom etc. -> browser-strategy entry
    for ats, rex in ATS_LINK_RES.items():
        if ats == "oracle":
            continue
        if rex.match(found_url) and meaningful_ats_url(found_url):
            return {"ats": f"{ats} (browser)", "identifier": found_url,
                    "url": found_url, "verified_name": slug,
                    "jobs": -1, "sg_jobs": -1}
    return None



def _get_page(url):
    try:
        # (connect timeout, read timeout) — a tuple caps BOTH phases so a
        # slow-drip server can't hold the connection open indefinitely
        r = requests.get(url, headers=BROWSER_PAGE_HEADERS, timeout=(5, 8),
                         allow_redirects=True, stream=False)
        if r.status_code == 200 and r.text:
            # decode &amp; etc. so extracted URLs are fetchable as-is
            return r.url, html_lib.unescape(r.text)
    except Exception:
        pass
    return None, ""


def _orc_count(orc_url):
    """Verify an Oracle Recruiting Cloud URL via its public API; returns total or None."""
    m = re.match(r"https://([^/]+)/hcmUI/CandidateExperience/[a-zA-Z-]+/sites/([^/?#]+)",
                 orc_url)
    if not m:
        return None
    host, site = m.groups()
    try:
        r = requests.get(
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
            params={"onlyData": "true",
                    "finder": f"findReqs;siteNumber={site},facetsList=LOCATIONS,"
                              f"limit=1,offset=0,sortBy=POSTING_DATES_DESC"},
            headers={"User-Agent": UA, "Accept": "application/json"}, timeout=10)
        if r.status_code == 200:
            return (r.json().get("items") or [{}])[0].get("TotalJobsCount", 0)
    except Exception:
        pass
    return None


COUNTRY_TLDS = {
    "australia": "com.au",
    "canada": "ca",
    "china": "cn",
    "germany": "de",
    "hong kong": "com.hk",
    "india": "co.in",
    "japan": "co.jp",
    "singapore": "com.sg",
    "united kingdom": "co.uk",
}


def sniff_homepage(brand, slug, country=""):
    """Visit likely company domains, follow the careers link, and harvest ATS
    URLs that can't be guessed from the company name (Oracle, SuccessFactors,
    iCIMS, Taleo, Phenom, ...). Oracle hits are API-verified with job counts;
    the rest are recorded for the scraper's browser strategy (jobs=-1)."""
    pages = []
    suffixes = ["com", "com.sg", "sg"]
    country_tld = COUNTRY_TLDS.get(country.casefold())
    if country_tld:
        suffixes.insert(0, country_tld)
    domains = [
        variant
        for suffix in dict.fromkeys(suffixes)
        for variant in (f"https://www.{slug}.{suffix}", f"https://{slug}.{suffix}")
    ]
    for dom in domains:
        final, html = _get_page(dom)
        if not html:
            continue
        pages.append((final, html))
        # follow up to 2 careers-ish links from the homepage
        links, seen = [], set()
        for m in CAREER_HREF_RE.finditer(html):
            href = m.group(1)
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = final.rstrip("/") + href
            elif not href.startswith("http"):
                continue
            if href not in seen:
                seen.add(href)
                links.append(href)
        for href in links[:2]:
            f2, h2 = _get_page(href)
            if h2:
                pages.append((f2, h2))
        break  # first domain that responds is enough

    def scan_pages(page_list):
        for page_url, html in page_list:
            blob = html + " " + page_url
            # API-verifiable ATS urls embedded in the page
            for rex in (WD_URL_RE, ORC_RE):
                m = rex.search(blob)
                if m:
                    hit = _hit_from_ats_url(brand, slug, m.group(0))
                    if hit:
                        return hit
            for ats, rex in ATS_LINK_RES.items():
                m = rex.search(blob)
                if not m:
                    continue
                found = m.group(0)
                if ats == "oracle":
                    total = _orc_count(found)
                    if total:
                        return {"ats": "oracle", "identifier": found, "url": found,
                                "verified_name": slug, "jobs": total, "sg_jobs": -1}
                    continue
                if not meaningful_ats_url(found):
                    continue   # bare host from a resource hint, not a job board
                return {"ats": f"{ats} (browser)", "identifier": found, "url": found,
                        "verified_name": slug, "jobs": -1, "sg_jobs": -1}
        return None

    hit = scan_pages(pages)
    if hit:
        return hit

    # search-assisted stage: Bing "{brand} careers"
    results = _search_career_urls(brand, country)
    for u in results[:15]:               # 1) ATS urls directly in results
        hit = _hit_from_ats_url(brand, slug, u)
        if hit:
            return hit
    fetched = 0
    for u in results:                     # 2) fetch top career-ish results
        if fetched >= 2:
            break
        if not re.search(r"career|job|vacan|join", u, re.I):
            continue
        final, html = _get_page(u)
        if not html:
            continue
        fetched += 1
        hit = scan_pages([(final, html)])
        if hit:
            return hit
    time.sleep(random.uniform(0.8, 1.5))  # be gentle with the search engine
    return None

# ── per-brand discovery ───────────────────────────────────────────────────────

PROBES = {"greenhouse": probe_greenhouse, "lever": probe_lever,
          "smartrecruiters": probe_smartrecruiters, "workday": probe_workday}


def discover(brand, ats_list):
    """Return the best hit across the selected ATSes, cheapest ATS first.
    Within an ATS, ALL slug variants are tried and the largest hit wins —
    a 2-job legacy account must not shadow the company's real board."""
    variants = slug_variants(brand)
    for name in ats_list:
        if name in ("workday", "sniff"):
            continue  # handled separately below
        probe = PROBES[name]
        vs = variants
        if name == "smartrecruiters":
            # SR identifiers are often CapitalCase ("Thales", "BoschGroup")
            words = re.findall(r"[A-Za-z0-9']+", brand)
            camel = "".join(w[:1].upper() + w[1:] for w in words)
            vs = list(dict.fromkeys(
                variants + [v.capitalize() for v in variants] + [camel]))
        best = None
        for slug in vs:
            hit = probe(brand, slug)
            if hit and hit["jobs"] > 0 and (best is None or hit["jobs"] > best["jobs"]):
                best = hit
            time.sleep(random.uniform(0.05, 0.15))
        if best:
            best["brand"] = brand
            return best
    if "workday" in ats_list:
        for slug in variants[:2]:  # workday probes are expensive; top variants only
            hit = probe_workday(brand, slug)
            if hit and hit["jobs"] > 0:
                hit["brand"] = brand
                return hit
    if "sniff" in ats_list:
        for slug in variants[:2]:
            hit = sniff_homepage(brand, slug)
            if hit:
                hit["brand"] = brand
                return hit
    return None


def discover_all(brand, ats_list, country="", language=""):
    """Return every distinct candidate ranked across all selected strategies."""
    variants = slug_variants(brand)
    hits = []
    for name in ats_list:
        if name in ("workday", "sniff"):
            continue
        probe = PROBES[name]
        probe_variants = variants
        if name == "smartrecruiters":
            words = re.findall(r"[A-Za-z0-9']+", brand)
            camel = "".join(word[:1].upper() + word[1:] for word in words)
            probe_variants = list(
                dict.fromkeys(
                    variants + [value.capitalize() for value in variants] + [camel]
                )
            )
        for slug in probe_variants:
            hit = probe(brand, slug)
            if hit:
                hit.update(brand=brand, source="ats_probe")
                hits.append(hit)
            time.sleep(random.uniform(0.05, 0.15))
    if "workday" in ats_list:
        for slug in variants[:2]:
            hit = probe_workday(brand, slug)
            if hit:
                hit.update(brand=brand, source="ats_probe")
                hits.append(hit)
    if "sniff" in ats_list:
        for slug in variants[:2]:
            hit = sniff_homepage(brand, slug, country)
            if hit:
                hit.update(brand=brand, source="homepage_sniff")
                hits.append(hit)
    for hit in hits:
        if "ats_confidence" not in hit:
            detection = detect_ats(hit["url"])[0]
            verified = hit["ats"].split()[0] in {
                "greenhouse", "lever", "smartrecruiters", "workday", "oracle"
            }
            _with_ats_detection(hit, detection, verified=verified)
    return [
        candidate.to_dict()
        for candidate in rank_candidates(hits, brand, country, language)
    ]


def discover_primary(brand, ats_list, country="", language=""):
    candidates = discover_all(brand, ats_list, country, language)
    return candidates[0] if candidates else None


def looks_mismatched(brand, verified_name):
    """Flag hits whose verified company name shares no word with the brand."""
    if not verified_name:
        return 0
    bw = set(re.findall(r"[a-z0-9]+", brand.lower()))
    vw = set(re.findall(r"[a-z0-9]+", verified_name.lower()))
    return 0 if bw & vw else 1


# ── registry parsing (skip already-registered brands) ─────────────────────────

def registered_brands(scraper_path="scraper.py"):
    p = Path(scraper_path)
    if not p.exists():
        return set()
    src = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"CAREER_PAGES\s*=\s*\{(.*?)\n\}", src, re.S)
    if not m:
        return set()
    return set(re.findall(r'^\s*"([^"]+)"\s*:', m.group(1), re.M))


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="ATS career-site auto-discovery")
    ap.add_argument("--input", default="mnc_shortlist.csv")
    ap.add_argument("--column", default="brand",
                    help="Column holding company names (default: brand)")
    ap.add_argument("--confidence", nargs="*", default=["high"],
                    help="Confidence tiers to include if the column exists")
    ap.add_argument("--limit", type=int, default=0, help="Only first N brands")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--country", default="",
                    help="Country scope, e.g. Singapore, Japan, or Canada")
    ap.add_argument("--language", default="",
                    help="Optional BCP-47 language scope, e.g. en-SG or ja")
    ap.add_argument("--include-registered", action="store_true",
                    help="Discover additional/regional portals for registered companies")
    ap.add_argument("--cache", default="career_url_cache.db",
                    help="SQLite discovery cache")
    ap.add_argument("--cache-ttl-days", type=int, default=14)
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore cached discovery results")
    ap.add_argument("--no-workday", action="store_true",
                    help="Skip Workday tenant probing (much faster)")
    ap.add_argument("--ats", nargs="*",
                    choices=["greenhouse", "lever", "smartrecruiters", "workday", "sniff"],
                    help="Only probe these ATSes (default: all four APIs). "
                         "'sniff' visits company homepages to find non-guessable "
                         "ATSes (Oracle, SuccessFactors, iCIMS, Taleo, Phenom...)")
    ap.add_argument("--scraper", default="scraper.py",
                    help="Path to scraper.py, to skip already-registered brands")
    ap.add_argument("--skip-found", default="",
                    help="Path to a previous ats_discovered.csv — brands in it are skipped")
    args = ap.parse_args()

    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if rows and "confidence" in rows[0] and args.confidence:
        rows = [r for r in rows if r.get("confidence") in set(args.confidence)]
    brands = []
    seen = set()
    for r in rows:
        b = (r.get(args.column) or "").strip()
        if b and b.lower() not in seen:
            seen.add(b.lower())
            brands.append(b)
    already = set() if (args.include_registered or args.country) else registered_brands(args.scraper)
    if args.skip_found and Path(args.skip_found).exists():
        with open(args.skip_found, newline="", encoding="utf-8") as f:
            already |= {r.get("brand", "").strip() for r in csv.DictReader(f)}
    brands = [b for b in brands if b not in already]
    if args.limit:
        brands = brands[:args.limit]

    ats_list = args.ats or ["greenhouse", "lever", "smartrecruiters", "workday"]
    if args.no_workday and "workday" in ats_list:
        ats_list.remove("workday")

    print(f"Probing {len(brands)} brands "
          f"({len(already)} already in CAREER_PAGES, skipped) | "
          f"ats: {', '.join(ats_list)}")

    cache = DiscoveryCache(args.cache)
    hits, all_candidates, done = [], [], 0

    def discover_with_cache(brand):
        cache_key = cache.key(brand, args.country, args.language, ats_list)
        if not args.refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            candidates = discover_all(brand, ats_list, args.country, args.language)
        except Exception:
            cache.put(cache_key, [], outcome="transient_error")
            raise
        cache.put(
            cache_key,
            candidates,
            outcome="success" if candidates else "not_found",
            success_ttl_days=args.cache_ttl_days,
        )
        return candidates

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(discover_with_cache, b): b for b in brands}
        for fut in as_completed(futs):
            done += 1
            b = futs[fut]
            try:
                candidates = fut.result()
                all_candidates.extend(candidates)
                hit = candidates[0] if candidates else None
            except Exception as e:
                print(f"  !! {b}: {e}")
                hit = None
            if hit:
                mism = hit["validation_status"] == "rejected"
                tiny = 0
                hits.append(hit)
                flag = (" (REVIEW: name mismatch)" if mism else
                        " (REVIEW: suspiciously few jobs — check for a bigger"
                        " board elsewhere)" if tiny else "")
                print(f"  [{done}/{len(brands)}] {b}: {hit['ats']} "
                      f"'{hit['identifier']}' — {hit['jobs']} jobs{flag}")
            elif done % 25 == 0:
                print(f"  [{done}/{len(brands)}] ...")

    # CSV report
    cols = ["brand", "ats", "identifier", "url", "verified_name",
            "jobs", "sg_jobs", "needs_review", "ats_confidence",
            "ats_verification_status", "ats_evidence", "ats_alternatives"]
    with open("ats_discovered.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for h in sorted(hits, key=lambda x: (-x["jobs"])):
            w.writerow({k: h.get(k, "") for k in cols})

    candidate_cols = [
        "brand", "url", "canonical_url", "ats", "identifier", "verified_name",
        "jobs", "sg_jobs", "country", "language", "source", "confidence_score",
        "confidence_level", "validation_status", "reasons", "is_primary",
        "needs_review", "ats_confidence", "ats_verification_status",
        "ats_evidence", "ats_alternatives",
    ]
    with open("career_url_candidates.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=candidate_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            sorted(
                all_candidates,
                key=lambda row: (
                    row["brand"].casefold(),
                    -float(row["confidence_score"]),
                ),
            )
        )

    # ready-to-paste registry snippet (clean hits only)
    clean = [h for h in hits if not h["needs_review"]]
    pad = max((len(h["brand"]) for h in clean), default=10) + 2
    with open("career_pages_additions.py", "w", encoding="utf-8") as f:
        f.write("# Paste inside CAREER_PAGES in scraper.py\n")
        f.write(f"# Auto-discovered {time.strftime('%Y-%m-%d')} — "
                f"{len(clean)} clean hits ({len(hits) - len(clean)} "
                f"held back in ats_discovered.csv pending review)\n")
        for h in sorted(clean, key=lambda x: x["brand"].lower()):
            f.write(f'    "{h["brand"]}":{" " * (pad - len(h["brand"]))}"{h["url"]}",'
                    f'  # {h["ats"]}, {h["jobs"]} jobs\n')

    print(f"\nDone. {len(hits)} hits ({len(clean)} clean) "
          f"-> ats_discovered.csv + career_pages_additions.py")


if __name__ == "__main__":
    main()
