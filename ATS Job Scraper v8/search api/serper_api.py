import os
import json
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ==========================================================
# CONFIG
# ==========================================================

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

INPUT_CSV = "entity_results_filtered.csv"
OUTPUT_CSV = "entity_results_completed.csv"

CACHE_FILE = "career_cache.json"

HEADERS = {
    "X-API-KEY": SERPER_API_KEY,
    "Content-Type": "application/json",
}

# ==========================================================
# ATS providers
# ==========================================================

ATS_PATTERNS = [
    "myworkdayjobs.com",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "careers.smartrecruiters.com",
    "smartrecruiters.com",
    "icims.com",
    "jobvite.com",
    "successfactors.com",
    "oraclecloud.com",
    "recruitee.com",
    "teamtailor.com",
    "bamboohr.com",
    "taleo.net",
]

BLACKLIST = [
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "jobstreet.com",
    "mycareersfuture.gov.sg",
    "monster.com",
    "ziprecruiter.com",
]

CAREER_HINTS = [
    "/careers",
    "/career",
    "/jobs",
    "/join-us",
    "/vacancies",
    "/work-with-us",
]

JOB_WORDS = [
    "search jobs",
    "current openings",
    "open positions",
    "join our team",
    "vacancies",
    "career opportunities",
    "job search",
]

# ==========================================================
# Cache
# ==========================================================

if Path(CACHE_FILE).exists():
    with open(CACHE_FILE, "r", encoding="utf8") as f:
        cache = json.load(f)
else:
    cache = {}

session = requests.Session()

# ==========================================================
# Serper search
# ==========================================================


def search_serper(company):
    payload = {
        "q": f"{company} careers",
        "num": 10,
    }

    r = session.post(
        "https://google.serper.dev/search",
        headers=HEADERS,
        json=payload,
        timeout=30,
    )

    r.raise_for_status()

    return r.json().get("organic", [])


# ==========================================================
# Helpers
# ==========================================================


def is_blacklisted(url):
    url = url.lower()
    return any(site in url for site in BLACKLIST)


def ats_score(url):
    url = url.lower()

    for i, ats in enumerate(ATS_PATTERNS):
        if ats in url:
            return 100 - i

    score = 0

    for hint in CAREER_HINTS:
        if hint in url:
            score += 20

    return score


def verify(url):
    try:
        r = session.get(
            url,
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            allow_redirects=True,
        )

        if r.status_code != 200:
            return False

        text = BeautifulSoup(
            r.text,
            "html.parser"
        ).get_text(" ", strip=True).lower()

        if "404" in text:
            return False

        if "page not found" in text:
            return False

        return any(word in text for word in JOB_WORDS)

    except Exception:
        return False


# ==========================================================
# Find best URL
# ==========================================================


def find_career_page(company):
    if company in cache:
        return cache[company]

    try:
        results = search_serper(company)
    except Exception:
        cache[company] = ""
        return ""

    candidates = []

    for result in results:
        url = result.get("link", "")

        if not url:
            continue

        if is_blacklisted(url):
            continue

        score = ats_score(url)

        candidates.append((score, url))

    candidates.sort(reverse=True)

    career_url = ""

    for score, url in candidates:
        if verify(url):
            career_url = url
            break

    cache[company] = career_url

    with open(CACHE_FILE, "w", encoding="utf8") as f:
        json.dump(cache, f, indent=2)

    return career_url


# ==========================================================
# Main
# ==========================================================

if not SERPER_API_KEY:
    raise ValueError(
        "SERPER_API_KEY not set."
    )

df = pd.read_csv(INPUT_CSV)

df.columns = df.columns.str.strip().str.lower()

if "career_page" not in df.columns:
    df["career_page"] = ""

for idx, row in tqdm(df.iterrows(), total=len(df)):

    existing = str(row["career_page"]).strip()

    if existing:
        continue

    company = str(row["entity"]).strip()

    if not company:
        continue

    url = find_career_page(company)

    df.at[idx, "career_page"] = url

    if idx % 20 == 0:
        df.to_csv(OUTPUT_CSV, index=False)

    time.sleep(0.25)

df.to_csv(OUTPUT_CSV, index=False)

print("Done!")
