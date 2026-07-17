"""
ai_discover.py — AI-assisted career-URL mapping (plugs the holes)
=================================================================
For brands that API probing and sniffing couldn't resolve, asks Claude
(with web search) to find the official careers page, then VERIFIES every
answer before trusting it:

  - Workday / Greenhouse / Lever / SmartRecruiters / Oracle URLs are
    verified through their APIs (live job counts)
  - anything else must at least fetch HTTP 200 to be recorded (browser
    strategy, jobs=-1)

Unverifiable AI answers are discarded — the model can hallucinate URLs,
so nothing enters your registry on its word alone.

SETUP:
  1. Get an Anthropic API key: https://console.anthropic.com
  2. Set it:   PowerShell:  $env:ANTHROPIC_API_KEY="sk-ant-..."
               Mac/Linux:   export ANTHROPIC_API_KEY="sk-ant-..."
  3. This script must sit in the same folder as discover_ats.py
     (it reuses its verification machinery).

COST: each brand = one Claude call with ~1 web search. Ballpark a few
cents per brand — check current pricing at anthropic.com/pricing and
start with --limit 20 to see your actual burn before a big run.

USAGE:
  python ai_discover.py --limit 20                          # test run
  python ai_discover.py --skip-found all_found.csv          # the holes
  python ai_discover.py --confidence high medium --max-calls 300

Resumes automatically via ai_progress.json (--reset to start over).
Outputs: ai_discovered.csv + career_pages_additions_ai.py
"""

import argparse, csv, json, os, re, sys, time, random
from datetime import datetime
from pathlib import Path

import requests

import discover_ats as d   # reuse probes, verification, brand loading helpers
from career_url_discovery import rank_candidates

Path("logs").mkdir(exist_ok=True)
_LOG = open(f"logs/ai_discover_{datetime.now():%d%m%Y%H%M}.log",
            "w", encoding="utf-8", errors="replace")

def say(*a):
    """Print to console AND append to the dated ai_discover log."""
    msg = " ".join(str(x) for x in a)
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    _LOG.write(msg + "\n")
    _LOG.flush()

PROGRESS_FILE = "ai_progress.json"

# ── Providers ─────────────────────────────────────────────────────────────────
# "anthropic": Anthropic Messages API (native web search).
# "openai":    ANY OpenAI-compatible chat/completions endpoint — OpenRouter,
#              DeepSeek, Together, Groq, Gemini(OpenAI-compat), local vLLM, etc.
#              Point --base-url + --model + the right API key env var at it.
# Since every URL the model returns is verified before use, a cheaper/weaker
# model just means a lower hit rate, never a wrong entry in your registry.
PROVIDERS = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-haiku-4-5",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {   # defaults tuned for OpenRouter + Gemini Flash (cheap)
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "google/gemini-2.0-flash-001",
        "key_env": "OPENROUTER_API_KEY",
    },
}

PROMPT = (
    'What is the official careers/job-listings page URL for the multinational '
    'company "{brand}" (it has a Singapore presence)? I need the page that '
    'actually lists open positions — e.g. a myworkdayjobs.com board, '
    'boards.greenhouse.io page, oraclecloud.com CandidateExperience site, '
    'SuccessFactors portal, or the company\'s own job-search page. '
    'Give up to 3 candidate URLs, most likely first (they will be verified '
    'programmatically, so best guesses are fine). '
    'Respond with ONLY a JSON object, no other text: '
    '{{"urls": ["https://...", "https://..."], "confidence": "high|low"}} '
    'If you have no plausible guess, respond {{"urls": []}}.'
)


def ask_claude(brand, api_key, provider="anthropic", url=None, model=None, searches=0,
               country=""):
    scoped_brand = f"{brand} ({country})" if country else brand
    prompt = PROMPT.format(brand=scoped_brand)
    if provider == "anthropic":
        body = {"model": model, "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}]}
        if searches > 0:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                              "max_uses": searches}]
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:  # openai-compatible
        body = {"model": model, "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}]}
        if searches > 0:
            # OpenRouter online-search shorthand: append ':online' to the model,
            # or the provider ignores this harmlessly. Providers vary — this is
            # best-effort; verification still gates every URL.
            body["model"] = model + (":online" if ":online" not in model else "")
        headers = {"Authorization": f"Bearer {api_key}",
                   "content-type": "application/json"}

    # retry transient overload/rate-limit (429/503/529) with backoff
    for attempt in range(4):
        try:
            r = requests.post(url, json=body, timeout=120, headers=headers)
        except requests.exceptions.RequestException as e:
            if attempt == 3:
                return None, "neterr"
            time.sleep(2 ** attempt + random.random())
            continue
        if r.status_code in (429, 503, 529):
            if attempt == 3:
                return None, "rate_limited"
            time.sleep(2 ** attempt * 3 + random.random())  # 3,6,12s backoff
            continue
        break
    if r.status_code in (401, 403):
        sys.exit(f"{provider} API rejected your key (401/403). Check the API key.")
    if r.status_code != 200:
        return None, f"http_{r.status_code}"
    try:
        r.raise_for_status()
    except Exception:
        return None, f"http_{r.status_code}"
    data = r.json()
    if provider == "anthropic":
        text = " ".join(b.get("text", "") for b in data.get("content", [])
                        if b.get("type") == "text")
    else:
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    clean = text.replace("```json", "").replace("```", "")
    m = re.search(r'\{[^{}]*"urls?"[^{}]*\}', clean, re.S)
    if not m:
        return None, "no_json"
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None, "bad_json"
    urls = obj.get("urls")
    if urls is None:
        urls = [obj["url"]] if obj.get("url") else []
    obj["urls"] = [u for u in urls if isinstance(u, str) and u.startswith("http")][:3]
    return obj, "ok"


def verify(brand, url):
    """Never trust the model: verify through APIs or at minimum an HTTP 200."""
    slug = re.sub(r"[^a-z0-9]", "", brand.lower())
    hit = d._hit_from_ats_url(brand, slug, url)      # API-grade verification
    if hit:
        return hit
    final, html = d._get_page(url)                    # fetch check for the rest
    if not html:
        return None
    detections = d.detect_ats(url, html=html, final_url=final)
    alternatives = ",".join(
        result.vendor for result in detections if result.vendor != "unknown"
    )
    for detection in detections:
        if detection.vendor in {"unknown", "custom"}:
            continue
        hit = d._hit_from_ats_url(brand, slug, detection.canonical_url)
        if hit:
            hit["ats_alternatives"] = alternatives
            return hit
    if detections[0].vendor == "custom":
        hit = {"ats": "custom", "identifier": final, "url": final,
               "verified_name": "", "jobs": -1, "sg_jobs": -1}
        hit["ats_alternatives"] = alternatives
        return d._with_ats_detection(hit, detections[0])
    # the fetched page may itself reveal a known ATS — check ALL platforms:
    # API-verifiable ones first (workday/oracle/greenhouse/lever/SR), then
    # the browser-strategy platforms (SF, iCIMS, Taleo, Phenom, ...)
    blob = html + " " + final
    for rex in (d.WD_URL_RE, d.ORC_RE,
                re.compile(r"https://boards\.greenhouse\.io/[A-Za-z0-9_-]+"),
                re.compile(r"https://jobs\.lever\.co/[A-Za-z0-9_-]+"),
                re.compile(r"https://careers\.smartrecruiters\.com/[A-Za-z0-9_-]+")):
        m = rex.search(blob)
        if m:
            hit = d._hit_from_ats_url(brand, slug, m.group(0))
            if hit:
                return hit
    for ats, rex in d.ATS_LINK_RES.items():
        m = rex.search(blob)
        if m and ats != "oracle":
            cand = m.group(0)
            # never swap a working URL for a worse one: candidate must be a
            # real board URL (path/query, not a bare preconnect host) AND load
            if d.meaningful_ats_url(cand):
                f2, h2 = d._get_page(cand)
                if h2:
                    return {"ats": f"{ats} (browser)", "identifier": f2,
                            "url": f2, "verified_name": slug,
                            "jobs": -1, "sg_jobs": -1}
            # candidate rejected -> keep the original URL, but note the platform
            return {"ats": f"{ats} (browser)", "identifier": final, "url": final,
                    "verified_name": slug, "jobs": -1, "sg_jobs": -1}
    return {"ats": "ai (browser)", "identifier": final, "url": final,
            "verified_name": "", "jobs": -1, "sg_jobs": -1}


def main():
    ap = argparse.ArgumentParser(description="AI-assisted career URL discovery")
    ap.add_argument("--input", default="mnc_shortlist.csv")
    ap.add_argument("--column", default="brand")
    ap.add_argument("--confidence", nargs="*", default=["high"])
    ap.add_argument("--skip-found", default="")
    ap.add_argument("--scraper", default="scraper.py")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-calls", type=int, default=200,
                    help="Stop after N Claude calls this run (default 200)")
    ap.add_argument("--provider", default="anthropic",
                    choices=["anthropic", "openai"],
                    help="anthropic, or 'openai' for any OpenAI-compatible endpoint "
                         "(OpenRouter/DeepSeek/Together/Groq/Gemini/local)")
    ap.add_argument("--base-url", default="",
                    help="Override the endpoint URL (openai provider)")
    ap.add_argument("--model", default="",
                    help="Model id (default: provider's cheap default)")
    ap.add_argument("--api-key-env", default="",
                    help="Env var holding the key (default: provider's own)")
    ap.add_argument("--searches", type=int, default=0,
                    help="Web searches allowed per brand (default 0 = answer from "
                         "model knowledge only; verification catches wrong guesses)")
    ap.add_argument("--country", default="",
                    help="Country scope, e.g. Singapore, Japan, or Canada")
    ap.add_argument("--language", default="",
                    help="Optional BCP-47 language scope")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="Ignore the progress file's done-list for skipping "
                         "(used by pipeline.py to re-probe dud companies)")
    ap.add_argument("--reclassify", default="",
                    help="Path to a discovered CSV: re-fetch its 'ai (browser)' rows "
                         "and upgrade any that reveal a known ATS. No API calls, free.")
    args = ap.parse_args()

    if args.reclassify:
        rows = list(csv.DictReader(open(args.reclassify, newline="", encoding="utf-8")))
        upgraded = 0
        for i, r in enumerate(rows, 1):
            if r.get("ats") != "ai (browser)":
                continue
            hit = verify(r["brand"], r["url"])
            if hit and hit["ats"] != "ai (browser)":
                say(f"  [{i}] {r['brand']}: ai (browser) -> {hit['ats']} "
                      f"({hit['jobs']} jobs) {hit['url'][:60]}")
                r.update({k: str(hit[k]) for k in
                          ("ats", "identifier", "url", "verified_name", "jobs", "sg_jobs")})
                upgraded += 1
            time.sleep(random.uniform(0.3, 0.8))
        out = Path(args.reclassify).stem + "_reclassified.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        say(f"\n{upgraded} rows upgraded -> {out}")
        return

    pcfg = PROVIDERS[args.provider]
    base_url = args.base_url or pcfg["url"]
    model = args.model or pcfg["model"]
    key_env = args.api_key_env or pcfg["key_env"]
    api_key = os.environ.get(key_env, "")
    if not api_key:
        sys.exit(f"Set {key_env} first (provider={args.provider}).")
    say(f"Provider: {args.provider} | model: {model} | endpoint: {base_url}")

    brands = d.__dict__["slug_variants"] and []  # placeholder; use reader below
    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        name_col = next((cols[c] for c in ("brand", "entity", "company", "name")
                         if c in cols), None)
        rows = list(reader)
    if rows and "confidence" in rows[0] and args.confidence:
        rows = [r for r in rows if r.get("confidence") in set(args.confidence)]
    seen = set()
    brands = []
    for r in rows:
        b = (r.get(name_col) or "").strip()
        if b and b.lower() not in seen:
            seen.add(b.lower())
            brands.append(b)

    already = set() if args.country else d.registered_brands(args.scraper)
    if args.skip_found and Path(args.skip_found).exists():
        with open(args.skip_found, newline="", encoding="utf-8") as f:
            already |= {r.get("brand", "").strip() for r in csv.DictReader(f)}

    prog = {"done": [], "done_scopes": [], "hits": []}
    if not args.reset and Path(PROGRESS_FILE).exists():
        prog = json.load(open(PROGRESS_FILE, encoding="utf-8"))
        prog.setdefault("done_scopes", [])
    if not args.fresh:
        if not args.country and not args.language:
            already |= set(prog["done"])

    completed_scopes = set(prog["done_scopes"])
    scope_key = lambda brand: "|".join(
        (brand.casefold(), args.country.casefold(), args.language.casefold())
    )
    todo = [
        b
        for b in brands
        if b not in already and (args.fresh or scope_key(b) not in completed_scopes)
    ]
    if args.limit:
        todo = todo[:args.limit]
    say(f"{len(todo)} brands for AI lookup | budget {args.max_calls} calls")

    calls = 0
    try:
        for i, b in enumerate(todo, 1):
            if calls >= args.max_calls:
                say("Call budget reached — run again to resume.")
                break
            obj, status = ask_claude(b, api_key, provider=args.provider,
                                     url=base_url, model=model,
                                     searches=args.searches, country=args.country)
            calls += 1
            if status == "rate_limited":
                say("  !! API overloaded/rate-limited after retries — "
                      "stopping now; progress saved, just re-run to resume.")
                break
            if status and status.startswith(("http_", "neterr")):
                say(f"  [{i}/{len(todo)}] {b}: transient error ({status}) — skipped")
                continue
            if not args.country and not args.language:
                prog["done"].append(b)
            prog["done_scopes"].append(scope_key(b))
            urls = (obj or {}).get("urls") or []
            if not urls:
                say(f"  [{i}/{len(todo)}] {b}: no confident answer")
                continue
            verified = []
            for url in urls[:3]:                  # try candidates until one verifies
                try:
                    hit = verify(b, url)
                except Exception as e:
                    say(f"      (verify failed for {url[:50]}: {e})")
                    hit = None
                if hit:
                    hit.update(brand=b, source="ai")
                    verified.append(hit)
            candidates = [
                candidate.to_dict()
                for candidate in rank_candidates(
                    verified, b, args.country, args.language
                )
            ]
            if candidates:
                for hit in candidates:
                    hit["ai_confidence"] = (obj or {}).get("confidence", "")
                    prog["hits"].append(hit)
                primary = candidates[0]
                jd = primary["jobs"] if primary["jobs"] >= 0 else "?"
                say(f"  [{i}/{len(todo)}] {b}: {primary['ats']} — {jd} jobs — "
                    f"{len(candidates)} candidate(s) — {primary['url'][:70]}")
            else:
                say(f"  [{i}/{len(todo)}] {b}: {len(urls)} AI suggestions, none "
                      f"verified — discarded")
            if i % 10 == 0:
                json.dump(prog, open(PROGRESS_FILE, "w", encoding="utf-8"))
            time.sleep(random.uniform(0.5, 1.2))
    finally:
        json.dump(prog, open(PROGRESS_FILE, "w", encoding="utf-8"))
        hits = prog["hits"]
        primary_hits = [h for h in hits if h.get("is_primary", 1)]
        cols = ["brand", "ats", "identifier", "url", "verified_name",
                "jobs", "sg_jobs", "ai_confidence", "ats_confidence",
                "ats_verification_status", "ats_evidence", "ats_alternatives"]
        with open("ai_discovered.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for h in primary_hits:
                w.writerow(h)
        candidate_cols = [
            "brand", "url", "canonical_url", "ats", "identifier",
            "verified_name", "jobs", "sg_jobs", "country", "language",
            "source", "confidence_score", "confidence_level",
            "validation_status", "reasons", "is_primary", "needs_review",
            "ats_confidence", "ats_verification_status", "ats_evidence",
            "ats_alternatives", "ai_confidence",
        ]
        with open("career_url_candidates_ai.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=candidate_cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(hits)
        with open("career_pages_additions_ai.py", "w", encoding="utf-8") as f:
            f.write("# AI-discovered, verification-passed. Review before pasting.\n")
            pad = max((len(h["brand"]) for h in primary_hits), default=10) + 2
            for h in sorted(primary_hits, key=lambda x: x["brand"].lower()):
                f.write(f'    "{h["brand"]}":{" " * (pad - len(h["brand"]))}'
                        f'"{h["url"]}",  # {h["ats"]}, {h["jobs"]} jobs, ai\n')
        say(f"\n{len(hits)} verified hits -> ai_discovered.csv + "
              f"career_pages_additions_ai.py ({calls} API calls this run)")


if __name__ == "__main__":
    main()
