"""
pipeline.py — one-click orchestrator with a bounded self-healing loop
=====================================================================
Runs the whole thing:

  ROUND 0   full scrape -> jobs_output.xlsx
  ROUND 1..N (default 2):
      1. read the Diagnostics sheet, collect dud companies
         (URL/SITE ERROR + SITE EMPTY OR EXTRACTION FAILED)
      2. re-discover better URLs for them
         (ai_discover.py if ANTHROPIC_API_KEY is set, else discover_ats sniff)
      3. persist verified candidates and promote new active portal records
      4. re-scrape only the patched companies, merge into jobs_output.xlsx
      5. companies that got no new URL, or failed again, go on a blacklist
         (pipeline_state.json) and are never retried — that's the loop bound.

  STOPS when: no duds left, no new URLs found, or --max-rounds reached.
  It cannot loop forever.

USAGE:
  python pipeline.py                     # full run, 2 fix rounds
  python pipeline.py --max-rounds 1
  python pipeline.py --skip-initial      # skip round 0, start from existing xlsx
  python pipeline.py --fresh             # forget the blacklist and start over
  (or just double-click run_pipeline.bat on Windows)
"""

import argparse, csv, glob, json, os, re, shlex, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from discovery_store import DEFAULT_DATABASE, DiscoveryStore, canonical_url
from self_healing import (
    FailureCode,
    RepairState,
    SelfHealingStore,
    VerificationOutcome,
    classify_diagnostic,
)

STATE_FILE = "pipeline_state.json"
Path("logs").mkdir(exist_ok=True)
LOG_PATH = Path(f"logs/pipeline_{datetime.now():%d%m%Y%H%M}.log")
_logf = open(LOG_PATH, "w", encoding="utf-8", errors="replace")

def say(*a):
    """Print to console AND append to the dated pipeline log."""
    msg = " ".join(str(x) for x in a)
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    _logf.write(msg + "\n")
    _logf.flush()


DUD_VERDICTS = ("URL/SITE ERROR", "SITE EMPTY OR EXTRACTION FAILED")
PY = sys.executable


def run(cmd, **kw):
    say(f"\n>>> {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", **kw)
    for line in proc.stdout:
        say(line.rstrip("\n"))
    return proc.wait()


def load_state(fresh):
    if fresh or not Path(STATE_FILE).exists():
        return {"blacklist": {}, "rounds_done": 0}
    st = json.load(open(STATE_FILE, encoding="utf-8"))
    if isinstance(st.get("blacklist"), list):     # migrate old format
        st["blacklist"] = {b: {"count": 1, "last": ""} for b in st["blacklist"]}
    return st


def blacklist_hit(st, brand):
    e = st["blacklist"].setdefault(brand, {"count": 0, "last": ""})
    e["count"] += 1
    e["last"] = f"{datetime.now():%Y-%m-%d %H:%M}"


def blocked(st, brand, max_attempts):
    return st["blacklist"].get(brand, {}).get("count", 0) >= max_attempts


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), indent=1)


def read_duds(xlsx, blacklist, verdicts=DUD_VERDICTS):
    try:
        d = pd.read_excel(xlsx, "Diagnostics")
    except Exception:
        say("!! no Diagnostics sheet found — nothing to retry")
        return []
    duds = d[d["Verdict"].isin(verdicts)]["Company"].astype(str).tolist()
    return [c for c in duds if c not in blacklist]


def read_diagnostics(xlsx):
    try:
        return pd.read_excel(xlsx, "Diagnostics").fillna("").to_dict("records")
    except Exception:
        return []


def legacy_registry(path="scraper.py"):
    src = Path(path).read_text(encoding="utf-8")
    body = re.search(r"CAREER_PAGES\s*=\s*\{(.*?)\n\}", src, re.S).group(1)
    return dict(re.findall(r'^\s*"([^"]+)"\s*:\s*"([^"]*)"', body, re.M))


def registry(store):
    store.import_legacy_registry(legacy_registry())
    return store.active_portals()


def _legacy_patch_registry_disabled(updates, round_no, path="scraper.py"):
    """Replace/add entries in CAREER_PAGES; backup first; must still compile."""
    shutil.copy(path, f"{path}.bak_round{round_no}")
    src = Path(path).read_text(encoding="utf-8")
    lines = src.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("CAREER_PAGES = {"))
    end = next(i for i, l in enumerate(lines) if l == "}" and i > start)
    body = "\n".join(lines[start + 1:end])
    entries = dict(re.findall(r'^\s*"([^"]+)"\s*:\s*"([^"]*)"', body, re.M))
    entries.update({k: v.replace('"', '').replace('\\', '') for k, v in updates.items()})
    pad = max(len(n) for n in entries) + 2
    new_body = [f'    "{n}":{" " * (pad - len(n))}"{entries[n]}",'
                for n in sorted(entries, key=str.lower)]
    Path(path).write_text("\n".join(lines[:start] + ["CAREER_PAGES = {"]
                                    + new_body + ["}"] + lines[end + 1:]),
                          encoding="utf-8")
    if run([PY, "-m", "py_compile", path]) != 0:
        say("!! patched scraper failed to compile — restoring backup")
        shutil.copy(f"{path}.bak_round{round_no}", path)
        return False
    return True


def promote_updates(store, updates, requests):
    """Persist verified candidates and promote them without modifying source."""
    for brand, hit in updates.items():
        candidate_id = store.record_candidate(
            requests[brand],
            brand,
            hit["url"],
            strategy=hit.get("strategy", "pipeline"),
            ats_vendor=hit.get("ats") or None,
            ats_identifier=hit.get("identifier") or None,
            confidence=hit.get("confidence"),
            evidence={
                "verified_name": hit.get("verified_name", ""),
                "jobs": hit.get("jobs", ""),
                "sg_jobs": hit.get("sg_jobs", ""),
            },
        )
        store.promote_candidate(candidate_id)


def rediscover(duds, round_no):
    """Find new URLs for dud companies. Returns candidate rows by brand."""
    infile = f"pipeline_retry_r{round_no}.csv"
    with open(infile, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["brand"])
        for c in duds:
            w.writerow([c])

    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if have_key and run([PY, "-m", "py_compile", "ai_discover.py"]) != 0:
        say("!! ai_discover.py has a syntax error — re-download it. "
              "Falling back to free sniff discovery for this round.")
        have_key = False
    # protect the user's main AI progress/result files from being clobbered
    for fn in ("ai_progress.json", "ai_discovered.csv"):
        if Path(fn).exists():
            shutil.move(fn, fn + ".main")
    try:
        if have_key:
            rc = run([PY, "ai_discover.py", "--input", infile, "--scraper", "none",
                      "--reset", "--max-calls", str(min(len(duds), 300))])
            src_csv = "ai_discovered.csv"
        else:
            say("(no ANTHROPIC_API_KEY set — using sniff discovery instead)")
            rc = run([PY, "discover_ats.py", "--input", infile, "--scraper", "none",
                      "--ats", "sniff", "--workers", "4"])
            src_csv = "ats_discovered.csv"
        if rc != 0:
            say("!! discovery subprocess FAILED — not blacklisting anyone this round")
            return None
        found = {}
        if Path(src_csv).exists():
            for r in csv.DictReader(open(src_csv, newline="", encoding="utf-8")):
                b, u = r.get("brand", "").strip(), r.get("url", "").strip()
                if b and u:
                    r["strategy"] = "ai" if have_key else "homepage_sniff"
                    confidence = r.get("ai_confidence", "").casefold()
                    r["confidence"] = {
                        "high": 0.9,
                        "medium": 0.7,
                        "low": 0.4,
                    }.get(confidence, 0.8)
                    found[b] = r
        if Path(src_csv).exists():
            shutil.move(src_csv, f"pipeline_retry_r{round_no}_found.csv")
    finally:
        for fn in ("ai_progress.json", "ai_discovered.csv"):
            if Path(fn + ".main").exists():
                if Path(fn).exists():
                    Path(fn).unlink()
                shutil.move(fn + ".main", fn)
    return found


def merge_results(main_xlsx, patch_xlsx, companies):
    """Replace `companies` rows in the main workbook with fresh rescrape rows."""
    def sheet(x, name):
        try:
            return pd.read_excel(x, name)
        except Exception:
            return pd.DataFrame()

    aj, dg = sheet(main_xlsx, "All Jobs"), sheet(main_xlsx, "Diagnostics")
    new_jobs, new_diag = sheet(patch_xlsx, "All Jobs"), sheet(patch_xlsx, "Diagnostics")

    if not aj.empty:
        aj = aj[~aj["Company"].isin(companies)]
    aj = pd.concat([aj, new_jobs], ignore_index=True)
    summary_keys = ["Company ID", "Company"] if "Company ID" in aj else ["Company"]
    summary = (aj.groupby(summary_keys).size().reset_index(name="Jobs Found")
                 .sort_values("Jobs Found", ascending=False))
    if not dg.empty:
        dg = dg[~dg["Company"].isin(companies)]
    dg = pd.concat([dg, new_diag], ignore_index=True)
    if not dg.empty:
        dg = dg.sort_values("Company")

    with pd.ExcelWriter(main_xlsx, engine="openpyxl") as w:
        aj.to_excel(w, sheet_name="All Jobs", index=False)
        summary.to_excel(w, sheet_name="Summary", index=False)
        dg.to_excel(w, sheet_name="Diagnostics", index=False)
    return len(new_jobs)


def main():
    ap = argparse.ArgumentParser(description="One-click scrape + self-heal pipeline")
    ap.add_argument("--max-rounds", type=int, default=2,
                    help="Max fix rounds after the initial scrape (default 2)")
    ap.add_argument("--skip-initial", action="store_true",
                    help="Skip round 0; start healing from the existing xlsx")
    ap.add_argument("--fresh", action="store_true", help="Forget the blacklist")
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="Discovery attempts per company before it's blocked "
                         "(default 2 = everyone gets a second chance)")
    ap.add_argument("--scraper-args", default="",
                    help='Extra args for scraper.py, e.g. "--workers 5"')
    ap.add_argument("--database", default=str(DEFAULT_DATABASE),
                    help="SQLite discovery database (default: ats_discovery.db)")
    args = ap.parse_args()

    store = DiscoveryStore(args.database)
    healing = SelfHealingStore(args.database)
    run_id = store.start_run(
        "resume" if args.skip_initial else "scheduled",
        vars(args),
    )
    say(f"Pipeline run: {run_id}")
    st = load_state(args.fresh)
    extra = shlex.split(args.scraper_args) if args.scraper_args else []
    extra += ["--database", args.database, "--run-id", run_id]

    if args.skip_initial:
        cands = sorted(glob.glob("jobs_output*.xlsx"), key=os.path.getmtime)
        if not cands:
            sys.exit("--skip-initial: no existing jobs_output*.xlsx found")
        OUTPUT = cands[-1]
        say(f"Continuing from newest output: {OUTPUT}")
    else:
        OUTPUT = f"jobs_output_{datetime.now():%d%m%Y%H%M}.xlsx"

    if not args.skip_initial:
        say("=" * 60 + "\nROUND 0: full scrape\n" + "=" * 60)
        if run([PY, "scraper.py", "--output", OUTPUT] + extra) != 0:
            sys.exit("Initial scrape failed — aborting.")

    for rnd in range(1, args.max_rounds + 1):
        blocked_now = {b for b in st["blacklist"]
                       if blocked(st, b, args.max_attempts)}
        # SPLIT BY FAILURE CLASS:
        # url errors -> re-discovery can fix -> healing loop
        # extraction failures -> URL is fine, crawler can't parse ->
        #   re-discovery is useless; route to backlog for aggregator/hand-tuning
        duds = read_duds(OUTPUT, blocked_now, verdicts=("URL/SITE ERROR",))
        extraction = read_duds(OUTPUT, set(),
                               verdicts=("SITE EMPTY OR EXTRACTION FAILED",))
        repair_ids = {}
        for diagnostic in read_diagnostics(OUTPUT):
            observation = classify_diagnostic(diagnostic, run_id)
            repair_id = healing.ingest(observation)
            if repair_id:
                repair_ids[observation.company_name] = repair_id
        if extraction:
            reg_now = registry(store)
            with open("extraction_backlog.csv", "w", newline="",
                      encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["brand", "url", "note"])
                for c in sorted(extraction):
                    w.writerow([c, reg_now.get(c, ""),
                                "URL loads but no jobs extracted — aggregator "
                                "or per-site tuning, NOT re-discovery"])
            say(f"{len(extraction)} extraction-failure companies -> "
                f"extraction_backlog.csv (URL is fine; re-discovery won't help; "
                f"these are NOT blacklisted)")
        say(f"\n{'=' * 60}\nROUND {rnd}: {len(duds)} dud companies "
            f"({len(blocked_now)} blocked after {args.max_attempts} attempts)"
            f"\n{'=' * 60}")
        if not duds:
            say("No duds left — pipeline healthy. Done.")
            break

        old = registry(store)
        requests = {
            company: store.create_request(
                run_id, company, "url_site_error", args.max_attempts
            )
            for company in duds
        }
        attempt_ids = {}
        for company in duds:
            repair_id = repair_ids.get(company)
            if not repair_id:
                continue
            state = healing.state(repair_id)
            if state == RepairState.RETRY_WAIT:
                healing.transition(repair_id, RepairState.CORRECTION_PLANNED)
                state = RepairState.CORRECTION_PLANNED
            if state == RepairState.CORRECTION_PLANNED:
                attempt_ids[company] = healing.start_attempt(
                    repair_id,
                    {"round": rnd, "strategy": "portal_rediscovery"},
                )
        found = rediscover(duds, rnd)
        if found is None:
            for attempt_id in attempt_ids.values():
                healing.complete_attempt(
                    attempt_id, error_code="discovery_tool_failed"
                )
            say("Discovery tooling failed — fix it and re-run "
                "(no companies were blacklisted). Stopping.")
            break
        updates = {
            brand: hit
            for brand, hit in found.items()
            if hit.get("url") and canonical_url(hit["url"]) != old.get(brand)
        }
        no_fix = [c for c in duds if c not in updates]
        for c in no_fix:
            blacklist_hit(st, c)          # +1 strike; blocked only at max_attempts
            store.finish_request(requests[c], "exhausted", "no_candidate")
            if c in attempt_ids:
                healing.complete_attempt(attempt_ids[c], error_code="no_candidate")
        snap = f"pipeline_blacklist_{datetime.now():%d%m%Y%H%M}.csv"
        with open(snap, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["brand", "failed_attempts", "last_attempt"])
            for b, e in sorted(st["blacklist"].items()):
                w.writerow([b, e["count"], e["last"]])
        say(f"new URLs found: {len(updates)} | struck (attempt +1): {len(no_fix)} "
            f"| snapshot: {snap}")
        if not updates:
            save_state(st)
            say("Nothing fixable this round — stopping.")
            break

        promote_updates(store, updates, requests)
        for company, hit in updates.items():
            if company in attempt_ids:
                healing.complete_attempt(
                    attempt_ids[company],
                    {"candidate_url": hit["url"], "round": rnd},
                )

        fixlist = f"pipeline_fixlist_r{rnd}.csv"
        with open(fixlist, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["brand"])
            for b in updates:
                w.writerow([b])
        patch_out = f"pipeline_rescrape_r{rnd}.xlsx"
        scrape_succeeded = (
            run([PY, "scraper.py", "--companies", fixlist,
                 "--output", patch_out] + extra) == 0
            and Path(patch_out).exists()
        )
        if scrape_succeeded:
            added = merge_results(OUTPUT, patch_out, list(updates))
            say(f"merged {added} fresh rows into {OUTPUT}")
        patch_diagnostics = (
            {
                str(row.get("Company", "")): row
                for row in read_diagnostics(patch_out)
            }
            if scrape_succeeded
            else {}
        )
        for company in updates:
            repair_id = repair_ids.get(company)
            if not repair_id or healing.state(repair_id) != RepairState.VERIFYING:
                continue
            diagnostic = patch_diagnostics.get(company)
            if diagnostic is None:
                outcome = VerificationOutcome(
                    False,
                    retryable=True,
                    message="rescrape failed or produced no diagnostic",
                )
            else:
                observed = classify_diagnostic(diagnostic, run_id)
                fixed = observed.failure_code in {
                    FailureCode.JOBS_EXTRACTED,
                    FailureCode.NO_COUNTRY_MATCHES,
                    FailureCode.NO_OPEN_JOBS,
                    FailureCode.PORTAL_HEALTHY,
                }
                outcome = VerificationOutcome(
                    fixed=fixed,
                    healthy_empty=observed.failure_code in {
                        FailureCode.NO_COUNTRY_MATCHES,
                        FailureCode.NO_OPEN_JOBS,
                    },
                    retryable=observed.retryable,
                    evidence={"failure_code": observed.failure_code},
                    message=observed.message,
                )
            healing.verify(repair_id, outcome)
        st["rounds_done"] = rnd
        save_state(st)

    fully = sum(1 for b in st["blacklist"] if blocked(st, b, args.max_attempts))
    say(f"\nPipeline finished. Output: {OUTPUT}")
    say(f"Blacklist: {len(st['blacklist'])} companies with strikes, {fully} blocked "
        f"(>= {args.max_attempts} attempts). Log: {LOG_PATH}")
    store.finish_run(run_id, "completed")


if __name__ == "__main__":
    main()
