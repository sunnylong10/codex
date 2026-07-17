"""
workday_debug.py — verbose diagnosis of Workday tenant detection
================================================================
Run:   python workday_debug.py chevron shell bp ubs pfizer
       python workday_debug.py            (uses that default list)

For each slug it prints, per wdN host: DNS result, HTTP status, final URL
after redirects, page size, whether it looks like a Workday page, every
board candidate found, and the CXS API response for each candidate.
Paste the full output back to Claude to pinpoint where detection fails.

pfizer is included as a positive control — scraper.py already pulls jobs
from Pfizer's Workday API, so at least one layer MUST light up for it.
"""

import json, re, socket, sys, time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
WD_VERSIONS = ["wd1", "wd3", "wd5", "wd12"]
COMMON_BOARDS = ["External", "Careers", "External_Careers", "Jobs", "careers",
                 "external", "Global", "Search"]
WD_URL_RE = re.compile(
    r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#\s]+)")


def cxs(tenant, wd, board):
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    try:
        r = requests.post(
            f"{base}/wday/cxs/{tenant}/{board}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"User-Agent": UA, "Content-Type": "application/json",
                     "Accept": "application/json",
                     "Origin": base, "Referer": f"{base}/{board}"},
            timeout=10)
        total = ""
        if r.status_code == 200:
            try:
                total = r.json().get("total", "?")
            except Exception:
                total = "(non-JSON 200)"
        return r.status_code, total
    except Exception as e:
        return f"EXC {type(e).__name__}", str(e)[:80]


def debug_slug(slug):
    print(f"\n{'=' * 70}\nSLUG: {slug}")
    for wd in WD_VERSIONS:
        host = f"{slug}.{wd}.myworkdayjobs.com"
        print(f"\n--- {host}")
        try:
            ip = socket.gethostbyname(host)
            print(f"  DNS: {ip}")
        except Exception as e:
            print(f"  DNS: FAILED ({e}) -> no tenant here")
            continue
        try:
            r = requests.get(f"https://{host}", headers={
                    "User-Agent": UA,
                    "Accept": ("text/html,application/xhtml+xml,application/xml;"
                               "q=0.9,image/avif,image/webp,*/*;q=0.8"),
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                }, timeout=10, allow_redirects=True)
        except Exception as e:
            print(f"  GET root: EXCEPTION {type(e).__name__}: {str(e)[:100]}")
            continue
        text = r.text or ""
        print(f"  GET root: status={r.status_code}  final_url={r.url}")
        print(f"  html: {len(text)} bytes | mentions 'workday': "
              f"{'yes' if 'workday' in text.lower() else 'NO'}")

        cands = []
        m = WD_URL_RE.match(r.url)
        if m:
            cands.append(("redirect", m.group(3)))
        for mm in re.finditer(
                r"myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]{2,40})",
                text):
            b = mm.group(1)
            if b.lower() not in ("wday", "en-us", "login", "wd", "api"):
                cands.append(("html", b))
        seen = set()
        cands = [(src, b) for src, b in cands
                 if not (b.lower() in seen or seen.add(b.lower()))]
        print(f"  board candidates: {cands if cands else 'NONE'}")

        cap = slug.capitalize()
        to_try = [b for _, b in cands][:6]
        if not to_try:
            to_try = [cap, f"{cap}Careers", f"{cap}_Careers"] + COMMON_BOARDS
            print("  no candidates -> trying brand-derived + common board names via CXS")
        for b in to_try:
            status, total = cxs(slug, wd, b)
            marker = "  <<< HIT" if status == 200 and str(total).isdigit() and int(total) > 0 else ""
            print(f"    CXS {b}: status={status} total={total}{marker}")
            time.sleep(0.2)


if __name__ == "__main__":
    slugs = sys.argv[1:] or ["chevron", "shell", "bp", "ubs", "pfizer"]
    print(f"requests {requests.__version__} | python {sys.version.split()[0]}")
    for s in slugs:
        debug_slug(s)
    print("\nDone. Paste this whole output back for diagnosis.")
