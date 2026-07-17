"""
aggregator_jobs.py — Singapore jobs via the Adzuna API
======================================================
Covers the companies the direct scraper can't: custom/JS-heavy career sites
(Google, Amazon, Deloitte, HSBC, ...) and the long tail with no known ATS.
Instead of scraping each company's site, this queries Adzuna's job-board
aggregation for Singapore and keeps postings whose employer name matches
your brand list.

ONE-TIME SETUP (free):
  1. Sign up at https://developer.adzuna.com  ->  you get an app_id + app_key
  2. Put them in the two constants below, or pass --app-id / --app-key,
     or set environment variables ADZUNA_APP_ID / ADZUNA_APP_KEY

USAGE:
  python aggregator_jobs.py --limit 20              # test on first 20 brands
  python aggregator_jobs.py                          # high-confidence brands
  python aggregator_jobs.py --confidence high medium # widen
  python aggregator_jobs.py --brands fixme.csv       # any CSV with brand/Entity col

RATE LIMITS & RESUME:
  Free-tier keys are rate-limited (check your dashboard for your quota).
  The script stops after --max-calls API calls (default 200) and records
  progress in aggregator_progress.json — just run it again tomorrow and it
  continues where it left off. Use --reset to start over.

OUTPUT:
  aggregator_jobs.xlsx — same column layout as scraper.py's output, so you
  can copy-paste the rows together or keep them as separate sources.
"""

import argparse, csv, json, os, re, sys, time, random
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

# ── paste your keys here (or use env vars / CLI flags) ────────────────────────
ADZUNA_APP_ID  = ""
ADZUNA_APP_KEY = ""

API = "https://api.adzuna.com/v1/api/jobs/sg/search/{page}"
PROGRESS_FILE = "aggregator_progress.json"
RESULTS_PER_PAGE = 50
MAX_PAGES_PER_BRAND = 4   # 200 postings per brand is plenty


# ── brand matching ────────────────────────────────────────────────────────────

def norm_tokens(s):
    return set(re.findall(r"[a-z0-9]+", str(s).lower()))

def company_matches(brand, employer):
    """True if the posting's employer name plausibly IS the brand.
    'Google' matches 'Google Asia Pacific Pte. Ltd.' but a posting from
    'Googolplex Tuition Centre' does not."""
    if not employer:
        return False
    bt, et = norm_tokens(brand), norm_tokens(employer)
    if not bt:
        return False
    if bt <= et:                      # every brand word appears in employer
        return True
    # long single-word brands: accept prefix match, since ACRA-style names
    # sometimes concatenate ("capitaland" vs "capitalandascott"). Short brands
    # (meta, shell, visa...) require an exact token to avoid metalworks/shellfish.
    if len(bt) == 1:
        b = next(iter(bt))
        if len(b) >= 7:
            return any(t.startswith(b) and len(t) <= len(b) + 10 for t in et)
    return False


# ── adzuna ────────────────────────────────────────────────────────────────────

def fetch_brand(brand, app_id, app_key, call_budget):
    """Query Adzuna for one brand. Returns (jobs, calls_used, hard_stop)."""
    jobs, calls = [], 0
    for page in range(1, MAX_PAGES_PER_BRAND + 1):
        if calls >= call_budget:
            break
        try:
            r = requests.get(
                API.format(page=page),
                params={
                    "app_id": app_id, "app_key": app_key,
                    "what": f'"{brand}"',
                    "results_per_page": RESULTS_PER_PAGE,
                    "content-type": "application/json",
                },
                timeout=15,
            )
            calls += 1
            if r.status_code == 429:
                print("  !! Rate limited by Adzuna — stopping for now. "
                      "Re-run later to resume.")
                return jobs, calls, True
            if r.status_code in (401, 403):
                print("  !! Adzuna rejected your credentials (401/403). "
                      "Check app_id / app_key.")
                sys.exit(1)
            r.raise_for_status()
            body = r.json()
        except SystemExit:
            raise
        except Exception as e:
            print(f"  !! {brand}: {e}")
            return jobs, calls, False

        results = body.get("results", [])
        for j in results:
            employer = (j.get("company") or {}).get("display_name", "")
            if not company_matches(brand, employer):
                continue
            loc = (j.get("location") or {}).get("display_name", "")
            jobs.append({
                "company":     brand,
                "title":       re.sub(r"<[^>]+>", "", j.get("title", "")),
                "location":    loc or "Singapore",
                "department":  (j.get("category") or {}).get("label", ""),
                "job_type":    j.get("contract_time", "") or "",
                "url":         j.get("redirect_url", ""),
                "posted_date": (j.get("created") or "")[:10],
                "source":      "Adzuna",
                "employer_as_posted": employer,
            })
        if len(results) < RESULTS_PER_PAGE:
            break
        time.sleep(random.uniform(0.4, 0.8))
    return jobs, calls, False


# ── progress / io ─────────────────────────────────────────────────────────────

def load_progress(reset):
    if reset or not Path(PROGRESS_FILE).exists():
        return {"done_brands": [], "jobs": []}
    with open(PROGRESS_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_progress(p):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f)

def read_brands(path, column_hints, confidence):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        name_col = next((cols[c] for c in column_hints if c in cols), None)
        if not name_col:
            sys.exit(f"{path} has no brand/Entity column (found {reader.fieldnames})")
        rows = list(reader)
    if rows and "confidence" in rows[0] and confidence:
        rows = [r for r in rows if r.get("confidence") in set(confidence)]
    out, seen = [], set()
    for r in rows:
        b = (r.get(name_col) or "").strip()
        if b and b.lower() not in seen:
            seen.add(b.lower())
            out.append(b)
    return out


def export(jobs, path):
    if not jobs:
        print("No matched jobs yet — nothing to export.")
        return
    df = pd.DataFrame(jobs)
    df = df.rename(columns={
        "company": "Company", "title": "Job Title", "location": "Location",
        "department": "Department", "job_type": "Job Type", "url": "Job URL",
        "posted_date": "Posted Date", "source": "Source",
        "employer_as_posted": "Employer (as posted)",
    })
    df["Scraped At"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    df = df.drop_duplicates(subset=["Company", "Job Title", "Job URL"])
    summary = (df.groupby("Company").size().reset_index(name="Jobs Found")
                 .sort_values("Jobs Found", ascending=False))
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="All Jobs", index=False)
        summary.to_excel(w, sheet_name="Summary", index=False)
    print(f"Saved {len(df)} jobs across {len(summary)} companies -> {path}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Singapore jobs via Adzuna, matched to your brand list")
    ap.add_argument("--brands", default="mnc_shortlist.csv")
    ap.add_argument("--confidence", nargs="*", default=["high"])
    ap.add_argument("--limit", type=int, default=0, help="Only first N brands")
    ap.add_argument("--max-calls", type=int, default=200,
                    help="Stop after this many API calls this run (default 200)")
    ap.add_argument("--output", default="aggregator_jobs.xlsx")
    ap.add_argument("--app-id",  default=os.environ.get("ADZUNA_APP_ID",  ADZUNA_APP_ID))
    ap.add_argument("--app-key", default=os.environ.get("ADZUNA_APP_KEY", ADZUNA_APP_KEY))
    ap.add_argument("--reset", action="store_true", help="Forget saved progress")
    args = ap.parse_args()

    if not args.app_id or not args.app_key:
        sys.exit("Missing Adzuna credentials. Sign up free at "
                 "https://developer.adzuna.com then pass --app-id/--app-key, "
                 "set ADZUNA_APP_ID/ADZUNA_APP_KEY, or paste them into the "
                 "constants at the top of this file.")

    brands = read_brands(args.brands,
                         ("brand", "entity", "entity_name", "company", "name"),
                         args.confidence)
    if args.limit:
        brands = brands[:args.limit]

    prog = load_progress(args.reset)
    done = set(prog["done_brands"])
    todo = [b for b in brands if b not in done]
    print(f"{len(todo)} brands to fetch ({len(done)} already done, resuming) | "
          f"budget: {args.max_calls} API calls this run")

    calls_used = 0
    try:
        for i, b in enumerate(todo, 1):
            if calls_used >= args.max_calls:
                print(f"Call budget reached ({args.max_calls}). "
                      f"Run again to continue — progress is saved.")
                break
            jobs, calls, hard_stop = fetch_brand(
                b, args.app_id, args.app_key, args.max_calls - calls_used)
            calls_used += calls
            prog["jobs"].extend(jobs)
            prog["done_brands"].append(b)
            print(f"  [{i}/{len(todo)}] {b}: {len(jobs)} SG jobs "
                  f"(calls used: {calls_used}/{args.max_calls})")
            if hard_stop:
                break
            if i % 10 == 0:
                save_progress(prog)
    finally:
        save_progress(prog)
        export(prog["jobs"], args.output)


if __name__ == "__main__":
    main()
