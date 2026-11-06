"""
Daily job fetcher for Maya's job board.

Sources (all allowed to use):
  - Adzuna job search API: wide coverage across many boards, including roles
    at Apple, Google, Disney and Tesla.
  - OpenAI's public Ashby job board feed.
  - Optional Greenhouse / Lever company feeds (add your own in config.json).

Keeps California (or remote-US) roles that match the target job families,
scores each one against the skills from the resume, and writes docs/jobs.json
for the website to display.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
OUT_FILE = ROOT / "docs" / "jobs.json"
NOW = datetime.now(timezone.utc)
HEADERS = {"User-Agent": "personal-job-board/1.0"}

# ---------------------------------------------------------------- helpers

def clean(text):
    """Strip HTML tags and extra whitespace."""
    text = re.sub(r"<[^>]+>", " ", unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def matches_any(text, patterns):
    return any(re.search(p, text, re.I) for p in patterns)


def role_family(title):
    for family, patterns in CONFIG["roles"].items():
        if matches_any(title, patterns):
            return family
    return None


LA_AREA = (
    r"Los Angeles|\bLA\b|Santa Monica|Culver City|Burbank|Glendale|Pasadena|"
    r"Hawthorne|El Segundo|Playa Vista|Long Beach|Irvine|Costa Mesa|Torrance|"
    r"Venice|Marina del Rey|Hollywood|Beverly Hills|Century City|"
    r"Manhattan Beach|Van Nuys|Woodland Hills|Sherman Oaks|Thousand Oaks|"
    r"Calabasas|Newport Beach|Anaheim|Orange County|Inglewood|Carson|"
    r"Downey|Santa Clarita|Westlake Village|Redondo Beach|Encino"
)
LA_PATTERN = re.compile(LA_AREA, re.I)
CA_PATTERN = re.compile(
    r"\b(CA|California)\b|San Francisco|San Jose|San Diego|"
    r"Palo Alto|Mountain View|Sunnyvale|Cupertino|Santa Clara|"
    r"Oakland|Berkeley|Fremont|Menlo Park|Redwood City|Sacramento|"
    r"San Mateo|Emeryville|San Luis Obispo|Bay Area|" + LA_AREA,
    re.I,
)


def location_ok(location, is_remote=False):
    if CA_PATTERN.search(location or ""):
        return True
    if CONFIG.get("include_remote_us") and (
        is_remote or re.search(r"remote", location or "", re.I)
    ):
        return not re.search(r"(UK|London|Canada|India|Europe|EMEA|APAC)", location or "", re.I)
    return False


def is_target_company(company):
    # Whole words only, so "apple" doesn't match "Applebee's"
    return any(re.search(rf"\b{re.escape(t)}\b", company, re.I)
               for t in CONFIG["target_companies"])


# ---------------------------------------------------------------- requirements

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10}
NUM = r"(\d{1,2}|" + "|".join(WORDS) + r")"
YEARS_RE = re.compile(
    rf"(?:minimum of|at least|min\.?)?\s*{NUM}\s*(?:\+|plus)?\s*"
    rf"(?:(?:-|–|to)\s*\d{{1,2}}\s*)?(?:\+\s*)?(?:or more\s+)?years?"
    rf"(?=[^.;]{{0,80}}?(?:experienc|work|industry|professional|role))",
    re.I,
)


def years_required(text):
    """Smallest number of years of experience the posting asks for, or None."""
    found = []
    for m in YEARS_RE.finditer(text):
        n = m.group(1).lower()
        n = WORDS.get(n, None) if not n.isdigit() else int(n)
        if n is not None and n <= 15:
            found.append(n)
    return min(found) if found else None


def phd_required(text):
    """True when a PhD is asked for and no lower degree is accepted."""
    if not re.search(r"\bph\.?\s?d\b|doctora", text, re.I):
        return False
    lower_ok = re.search(
        r"master'?s|\bM\.?S\.?\b|bachelor'?s|\bB\.?S\.?\b|\bB\.?A\.?\b|"
        r"MBA|equivalent (?:practical )?experience|phd (?:is )?(?:a )?(?:plus|preferred)|"
        r"preferred[^.]{0,40}ph\.?d",
        text, re.I)
    return not lower_ok


# ---------------------------------------------------------------- scoring

def score_job(job):
    """0-100 fit score built from the resume's skills and experience level."""
    text = f"{job['title']} {job['description']}".lower()
    title = job["title"].lower()
    p = CONFIG["profile"]

    matched = [s for s, pats in p["skills"].items() if matches_any(text, pats)]
    score = min(len(matched) * 8, 48)

    if matches_any(title, p["early_career"]) or matches_any(text, p["early_career"]):
        score += 25
    if matches_any(title, p["too_senior"]):
        score -= 35
    years = job["years_required"]
    if years is not None:
        if years <= 2:
            score += 10
        elif years >= 3:
            score -= 30
    if job["phd_required"]:
        score -= 30
    if matches_any(text, p["degree"]):
        score += 10
    if job["target_company"]:
        score += 10
    if job["la_area"]:
        score += CONFIG["la_area_boost"]

    return max(0, min(100, score)), matched


# ---------------------------------------------------------------- sources

def fetch_adzuna():
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("Adzuna keys missing; skipping Adzuna.")
        return []

    url = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
    base = {
        "app_id": app_id,
        "app_key": app_key,
        "where": "California",
        "results_per_page": 50,
        "max_days_old": CONFIG["max_days_old"],
        "content-type": "application/json",
    }
    searches = [{"what_phrase": q} for q in CONFIG["adzuna_queries"]]
    # The same searches again, focused on the LA area
    searches += [{"what_phrase": q, "where": "Los Angeles", "distance": 60}
                 for q in CONFIG["adzuna_queries"]]
    # Extra searches aimed at each target company
    for company in CONFIG["target_company_searches"]:
        searches.append({"what_and": company,
                         "what_or": "analyst analytics scientist project"})

    jobs = []
    for s in searches:
        for page in (1, 2):
            try:
                r = requests.get(url.format(page=page), params={**base, **s},
                                 headers=HEADERS, timeout=30)
                r.raise_for_status()
                results = r.json().get("results", [])
            except Exception as e:
                print(f"Adzuna error for {s}: {e}")
                if "401" in str(e) or "403" in str(e):
                    print("Adzuna rejected the keys. Check the two secrets.")
                    return jobs
                break
            for j in results:
                jobs.append({
                    "id": f"adzuna-{j['id']}",
                    "title": clean(j.get("title")),
                    "company": clean(j.get("company", {}).get("display_name", "")),
                    "location": clean(j.get("location", {}).get("display_name", "")),
                    "description": clean(j.get("description")),
                    "url": j.get("redirect_url"),
                    "posted": j.get("created"),
                    "salary_min": j.get("salary_min"),
                    "salary_max": j.get("salary_max"),
                    "source": "Adzuna",
                    "remote": False,
                })
            if len(results) < 50:
                break
            time.sleep(1)  # stay well under Adzuna's rate limits
    return jobs


def fetch_ashby(board, company_name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    try:
        r = requests.get(url, params={"includeCompensation": "true"},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"Ashby error for {board}: {e}")
        return []
    jobs = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location", "")] + [
            l.get("location", "") for l in j.get("secondaryLocations", []) or []
        ]
        jobs.append({
            "id": f"ashby-{board}-{j.get('id') or j.get('jobUrl')}",
            "title": j.get("title", ""),
            "company": company_name,
            "location": " / ".join(l for l in locs if l),
            "description": clean(j.get("descriptionPlain") or j.get("descriptionHtml")),
            "url": j.get("jobUrl") or j.get("applyUrl"),
            "posted": j.get("publishedAt"),
            "source": "Company site",
            "remote": bool(j.get("isRemote")),
        })
    return jobs


def fetch_greenhouse(board, company_name):
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    try:
        r = requests.get(url, params={"content": "true"}, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"Greenhouse error for {board}: {e}")
        return []
    return [{
        "id": f"gh-{board}-{j['id']}",
        "title": j.get("title", ""),
        "company": company_name,
        "location": (j.get("location") or {}).get("name", ""),
        "description": clean(j.get("content")),
        "url": j.get("absolute_url"),
        "posted": j.get("updated_at"),
        "source": "Company site",
        "remote": False,
    } for j in r.json().get("jobs", [])]


def fetch_lever(board, company_name):
    url = f"https://api.lever.co/v0/postings/{board}"
    try:
        r = requests.get(url, params={"mode": "json"}, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"Lever error for {board}: {e}")
        return []
    return [{
        "id": f"lever-{board}-{j['id']}",
        "title": j.get("text", ""),
        "company": company_name,
        "location": (j.get("categories") or {}).get("location", ""),
        "description": clean(j.get("descriptionPlain")),
        "url": j.get("hostedUrl"),
        "posted": datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat()
                  if j.get("createdAt") else None,
        "source": "Company site",
        "remote": j.get("workplaceType") == "remote",
    } for j in r.json()]


FETCHERS = {"ashby": fetch_ashby, "greenhouse": fetch_greenhouse, "lever": fetch_lever}

# ---------------------------------------------------------------- main

def main():
    raw = fetch_adzuna()
    print(f"Adzuna: {len(raw)} postings")
    for feed in CONFIG["company_feeds"]:
        got = FETCHERS[feed["platform"]](feed["board"], feed["company"])
        print(f"{feed['company']}: {len(got)} postings")
        raw += got
    print(f"Fetched {len(raw)} raw postings")

    # Keep the jobs from earlier runs so "first seen" dates survive
    old = {}
    if OUT_FILE.exists():
        old = {j["id"]: j for j in json.loads(OUT_FILE.read_text()).get("jobs", [])}

    today = NOW.date().isoformat()
    kept, seen_keys = {}, set()
    for job in raw:
        family = role_family(job["title"])
        if not family or not location_ok(job["location"], job["remote"]):
            continue
        if matches_any(job["title"], CONFIG["exclude_titles"]):
            continue
        # Same title + company from two sources counts as one posting
        key = (job["title"].lower(), job["company"].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)

        job["role"] = family
        job["la_area"] = bool(LA_PATTERN.search(job["location"]))
        job["years_required"] = years_required(job["description"])
        job["phd_required"] = phd_required(job["description"])
        job["senior_title"] = matches_any(job["title"], CONFIG["profile"]["too_senior"])
        job["fits_experience"] = (
            not job["senior_title"]
            and not job["phd_required"]
            and (job["years_required"] is None
                 or job["years_required"] <= CONFIG["max_years_experience"])
        )
        job["target_company"] = is_target_company(job["company"])
        job["score"], job["matched_skills"] = score_job(job)
        job["first_seen"] = old.get(job["id"], {}).get("first_seen", today)
        job["description"] = job["description"][:600]
        kept[job["id"]] = job

    # Keep older postings for a while even if a source stops returning them
    cutoff = (NOW - timedelta(days=CONFIG["keep_days"])).date().isoformat()
    for jid, job in old.items():
        if jid not in kept and job.get("first_seen", today) >= cutoff:
            kept[jid] = job

    jobs = sorted(kept.values(), key=lambda j: (-j["score"], j["first_seen"]))
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps({"updated": NOW.isoformat(), "jobs": jobs}, indent=1))
    new_today = sum(j["first_seen"] == today for j in jobs)
    fits = sum(j.get("fits_experience", False) for j in jobs)
    print(f"{fits} jobs fit your experience level (0-{CONFIG['max_years_experience']} years, no PhD)")
    companies = len({j["company"] for j in jobs})
    print(f"Saved {len(jobs)} jobs from {companies} companies ({new_today} new today)")


if __name__ == "__main__":
    main()
