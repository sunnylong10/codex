# MNC Job Scraper v8 — Setup & Usage (Windows / Mac)

Pipeline for finding Singapore job openings at MNCs:

```
mnc_shortlist.csv  →  discover_ats.py  →  career_pages_additions.py  →  scraper.py  →  jobs_output.xlsx
   (company list)      (finds career        (paste into scraper's         (scrapes       (All Jobs +
                        sites via ATS        CAREER_PAGES dict)            everything)     Summary sheets)
                        APIs)
```

## ATS detection

ATS identification is implemented as plugins under `ats_detection/`. Detection
runs against original URLs, redirects, final URLs, HTML, and optional network
request URLs. It can return multiple evidence-backed results and distinguishes
known vendors, `custom`, and unresolved `unknown` pages.

Current plugins cover Workday, SAP SuccessFactors, Greenhouse, Lever, Oracle
Recruiting Cloud, and SmartRecruiters. Detection output includes confidence,
verification status, tenant/board identifiers, detector version, conflicts, and
structured evidence. Vendor API probes add explicit `api_verified` evidence.

Each vendor is a concrete plugin implementing the same `detect`, `verify`, and
metadata contract. Verification uses a shared bounded HTTP client so timeouts,
rate limits, access denial, and retryable failures have consistent outcomes.
The existing discovery probe functions remain compatibility adapters during
the incremental migration.

## Career URL discovery

Discovery now keeps and ranks multiple URL candidates per company instead of
discarding every result after the first ATS hit. The existing
`ats_discovered.csv` remains a one-primary-URL compatibility report, while
`career_url_candidates.csv` contains all normalized candidates, confidence
scores, validation states, scopes, and ranking reasons.

Use country-scoped discovery for regional portals:

```bash
python discover_ats.py --country Singapore --language en-SG --include-registered
```

Results are cached in `career_url_cache.db`. Positive results default to a
14-day TTL, misses to three days, and transient failures to one hour. Use
`--refresh` to bypass the cache. AI discovery writes all alternatives to
`career_url_candidates_ai.csv` and keeps only each scoped primary in
`ai_discovered.csv` for compatibility.

### Evidence-driven portal resolution

`portal_discovery.py` combines trusted official-domain crawling, scoped Serper
queries, ATS plugin detection, independent identity/recruitment/authority/scope
validation, negative-match memory, and canonical portal selection.

Input CSV columns are `brand`, optional `aliases`, `official_domains`, `country`,
and `language`. Multiple aliases or domains use `|` or `;` separators.

```bash
set SERPER_API_KEY=your-key
python portal_discovery.py --input companies.csv
```

The SQLite evidence ledger stores candidates, structured evidence, versioned
rejections, search cache entries, and scoped portal selections. Search results
generate candidates but cannot become authoritative without independent
identity, recruitment, and domain evidence.

## Persistent discovery registry

The pipeline now uses `ats_discovery.db` as its operational source of truth for
company IDs, recruitment portals, discovery requests, candidates, scrape
observations, and pipeline runs. On first use, the existing `CAREER_PAGES`
dictionary is imported automatically and idempotently.

Newly rediscovered portals are promoted in SQLite; `pipeline.py` no longer
modifies `scraper.py`. Excel and CSV files remain compatibility reports rather
than the authoritative registry. Use `--database PATH` on `pipeline.py` or
`scraper.py` to select another database.

## Canonical job normalization

All ATS and browser extractor dictionaries are normalized through
`job_normalization.py` immediately before export. Extractors retain their
existing behavior while the `All Jobs` sheet receives stable job/company IDs,
structured country and city fields, canonical employment and workplace types,
normalized dates and URLs, ATS provenance, confidence, and validation issues.

The current schema version is `1.0`. Missing optional values remain unknown
rather than being invented, and records with invalid titles or URLs are not
exported. Existing `Company`, `Job Title`, `Location`, `Job Type`, and `Job URL`
columns remain available for compatibility.

## Files

| File | What it is |
|------|------------|
| `scraper.py` | The scraper. Workday/Greenhouse/Lever/SmartRecruiters APIs first, headless-browser crawler as fallback. Filters to Singapore jobs by default. |
| `discover_ats.py` | Probes free ATS APIs to auto-find career sites for companies not yet registered in `scraper.py`. |
| `mnc_shortlist.csv` | Cleaned company list derived from your ACRA data (brand, confidence tier, SG entity counts). |
| `requirements.txt` | Python dependencies. |

---

## One-time setup

You need **Python 3.10+**. Check with `python --version` (Windows) or `python3 --version` (Mac).
If missing: Windows → https://www.python.org/downloads/ (tick **"Add python.exe to PATH"** during install); Mac → `brew install python` or the python.org installer.

### Windows (PowerShell or CMD)

```powershell
cd C:\path\to\your\scraper\folder

# create + activate a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate

# install dependencies + the headless browser
pip install -r requirements.txt
playwright install chromium
```

If PowerShell blocks `activate` with an execution-policy error, run this once and retry:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Mac (Terminal)

```bash
cd /path/to/your/scraper/folder

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

Everything below is identical on both platforms — just remember: **Windows uses `python`, Mac uses `python3`**, and re-activate the venv (`venv\Scripts\activate` / `source venv/bin/activate`) whenever you open a new terminal.

---

## Step 1 — Discover career sites (expand beyond the 129 registered companies)

```bash
# quick sanity test: 30 brands, skip slow Workday probing
python discover_ats.py --limit 30 --no-workday

# full run over high-confidence brands (grab a coffee — Workday probing is slow)
python discover_ats.py

# widen the net later
python discover_ats.py --confidence high medium
```

Outputs:

- `ats_discovered.csv` — every hit: which ATS, job counts, SG job counts, and the company name the ATS itself reports.
- `career_pages_additions.py` — ready-to-paste lines for `scraper.py`. Only "clean" hits are included; anything whose verified name doesn't match the brand is flagged `needs_review=1` and held back in the CSV.

**Before pasting: skim the entries.** Slugs can collide — `atlas` on Greenhouse might be an unrelated startup. The `verified_name` column exists so you can eyeball this. Then open `scraper.py`, find `CAREER_PAGES = {`, and paste the new lines anywhere inside the dict (before the closing `}`).

`discover_ats.py` automatically skips brands already in `CAREER_PAGES`, so re-running it after pasting is safe.

## Step 2 — Scrape

```bash
# test run: top 20 companies from your entity CSV
python scraper.py --top 20

# full run (Singapore jobs only, the default)
python scraper.py

# useful flags
python scraper.py --location "Hong Kong"     # different location filter
python scraper.py --all-locations            # no location filter (huge output)
python scraper.py --workers 5                # more parallel browser tabs
python scraper.py --output myrun.xlsx        # custom output name
python scraper.py --companies my_list.csv    # different input list (needs an "Entity" column)
```

Output: `jobs_output.xlsx` with an **All Jobs** sheet (Company, Title, Location, URL, Posted Date, Source...) and a **Summary** sheet (jobs per company). Note the Summary only lists companies that returned ≥1 job — with the Singapore filter on, companies with no current SG openings won't appear.

---

## What "good" looks like / troubleshooting

**Companies stuck at exactly 40 jobs** — fixed in v8 (Workday pagination bug). If you still see a suspicious 40, send me the log lines.

**`Workday 404/422 ... falling back to browser`** — the board name in `CAREER_PAGES` is stale. v8 tries to self-heal by following the redirect; if it still fails, open that company's career page in your own browser and copy the final URL (after all redirects) into `CAREER_PAGES`.

**`Target page, context or browser has been closed`** — a browser tab crashed; v8 retries once automatically. If it happens a lot, lower `--workers` to 2.

**Lots of companies with 0 jobs** — three common causes: (1) they genuinely have no SG openings right now; (2) their career page doesn't expose a location field the crawler can read (run with `--all-locations` on that company to check); (3) their site is a custom/JS-heavy one the crawler can't parse — those are candidates for the aggregator-API route instead.

**`playwright` errors about missing browser** — you skipped `playwright install chromium`, or you're in a different venv than the one you installed into.

**`ModuleNotFoundError`** — venv not activated. Run the activate command for your OS and retry.

**Windows garbled characters in console** — cosmetic only; the script already forces UTF-8 output, and the Excel file is unaffected.

**Be polite.** Default worker counts and delays are deliberately modest. Cranking `--workers` way up gets you rate-limited or IP-blocked by exactly the sites you care about.

---

## Coverage expectations

Discovery finds companies on Greenhouse, Lever, SmartRecruiters, and Workday — realistically 15–30% of the shortlist. Companies on Taleo, SuccessFactors, iCIMS, or fully custom career sites won't be auto-discovered; for that long tail, either add URLs to `CAREER_PAGES` by hand (paste the career page URL with any strategy — the crawler will try to figure it out) or use a job-aggregator API (Adzuna, JSearch) filtered against your company list.

---

## Step 3 (optional) — Aggregator for crawler-resistant companies

Google, Amazon, Deloitte, HSBC and similar custom career portals defeat the
generic crawler. `aggregator_jobs.py` covers them via Adzuna's job API instead:

```bash
# one-time: get free credentials at https://developer.adzuna.com
# then paste them into the constants at the top of aggregator_jobs.py
# (or set ADZUNA_APP_ID / ADZUNA_APP_KEY env vars)

python aggregator_jobs.py --limit 20        # test
python aggregator_jobs.py                    # high-confidence brands
python aggregator_jobs.py --brands fixme.csv # any CSV with a brand/Entity column
```

It searches Adzuna Singapore per brand and keeps only postings whose employer
name actually matches the brand (so "Meta" won't pick up "Metalworks").
Free-tier keys are rate-limited: the script stops after `--max-calls`
(default 200) and saves progress to `aggregator_progress.json` — re-run it
to resume. Output `aggregator_jobs.xlsx` uses the same columns as
`jobs_output.xlsx`, with an extra "Employer (as posted)" column so you can
spot-check matches.
