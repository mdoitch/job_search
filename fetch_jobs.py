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
    text = text or ""
    for _ in range(3):                 # Greenhouse encodes entities twice
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"<[^>]+>", " ", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def matches_any(text, patterns):
    return any(re.search(p, text, re.I) for p in patterns)


def role_family(title):
    for family, patterns in CONFIG["roles"].items():
        if matches_any(title, patterns):
            return family
    return None


LA_AREA = (
    # Los Angeles County
    r"Los Angeles|Santa Monica|Culver City|Burbank|Glendale|Pasadena|"
    r"Hawthorne|El Segundo|Playa Vista|Playa del Rey|Long Beach|Torrance|"
    r"Venice|Marina del Rey|Hollywood|Beverly Hills|Century City|Westwood|"
    r"Brentwood|Malibu|Manhattan Beach|Hermosa Beach|Redondo Beach|Van Nuys|"
    r"Woodland Hills|Sherman Oaks|Studio City|Encino|Tarzana|Northridge|"
    r"Chatsworth|Canoga Park|West Hills|Porter Ranch|Calabasas|Agoura Hills|"
    r"Westlake Village|Inglewood|Carson|Downey|Santa Clarita|Valencia|"
    r"Monrovia|Arcadia|Alhambra|Lancaster|Palmdale|Universal City|"
    # Ventura County / Conejo Valley
    r"Thousand Oaks|Newbury Park|Oak Park|Moorpark|Simi Valley|Camarillo|"
    r"Oxnard|Ventura|Port Hueneme"
)
LA_PATTERN = re.compile(LA_AREA, re.I)
CA_PATTERN = re.compile(
    r"\b(CA|California)\b|San Francisco|San Jose|San Diego|"
    r"Palo Alto|Mountain View|Sunnyvale|Cupertino|Santa Clara|"
    r"Oakland|Berkeley|Fremont|Menlo Park|Redwood City|Sacramento|"
    r"Irvine|Costa Mesa|Newport Beach|Anaheim|Orange County|"
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
    # not "for over 70 years", "more than 35 years", "nearly 20 years"
    rf"(?<!over )(?<!than )(?<!nearly )(?<!almost )(?<!past )"
    rf"\b{NUM}\s*(?:\+|plus)?\s*"
    rf"(?:(?:-|–|—|to)\s*\d{{1,2}}\s*)?(?:\+\s*)?(?:or more\s+)?(?:years?|yrs?)\b"
    # not "25 years later", "5 years in a row", "a 10 year-long contract"
    rf"(?!\s*(?:later|ago|old|in a row|in business|of history|-long|warranty))"
    rf"(?=[^.;]{{0,80}}?(?:experienc|work|industry|professional|role|\bin\s|"
    rf"manag|lead|build|analy|hands-on|as an?\b|post|full[- ]time))",
    re.I,
)


DEGREE_PATH_RE = re.compile(
    r"\b(?:for|with)\s+(?:a\s+|an\s+)?(?:(ph\.?\s?d|doctora)|"
    r"(master'?s|m\.?s\.?\b|graduate degree|advanced degree)|"
    r"(bachelor'?s|b\.?s\.?\b|b\.?a\.?\b|undergraduate))",
    re.I)
PREFERRED_HEADING = re.compile(
    r"(?:preferred|nice[- ]to[- ]have|bonus|desired|pluses|ideal(?:ly)?)\s*"
    r"(?:qualifications|skills|experience|requirements|points)?\s*:", re.I)
REQUIRED_HEADING = re.compile(
    r"(?:minimum|basic|required|must[- ]have|key)\s*(?:qualifications|requirements|skills|experience)"
    r"|(?<!preferred)(?<!desired)(?<!bonus)(?<!additional)(?<!nice to have)"
    r"(?:^|\s)(?:requirements|qualifications|what you(?:'ll)? (?:need|bring))\s*:", re.I)
INLINE_PREFERRED = re.compile(r"preferred|\ba plus\b|nice to have|\bideal|desired|bonus", re.I)


def _year_mentions(text):
    """Each experience requirement in a posting as (years, path, preferred).

    path is the degree it applies to ("phd", "masters", "bachelors") or None,
    e.g. "1-4+ years for PhDs or 3-5+ years for Master's".
    preferred is True for nice-to-haves ("5+ years preferred", or anything
    under a "Preferred qualifications" heading).
    """
    headings = sorted([(m.start(), "pref") for m in PREFERRED_HEADING.finditer(text)] +
                      [(m.start(), "req") for m in REQUIRED_HEADING.finditer(text)])
    out = []
    for m in YEARS_RE.finditer(text):
        n = m.group(1).lower()
        n = WORDS.get(n, None) if not n.isdigit() else int(n)
        if n is None or n > 15:
            continue
        tail = text[m.end():m.end() + 70]
        nxt = re.search(r"\d|[.;]", tail)     # stop at the next number or sentence end
        if nxt:
            tail = tail[:nxt.start()]
        d = DEGREE_PATH_RE.search(tail)
        path = None if not d else ("phd" if d.group(1) else "masters" if d.group(2) else "bachelors")
        section = [kind for pos, kind in headings if pos < m.start()]
        preferred = bool(INLINE_PREFERRED.search(tail)) or (section[-1:] == ["pref"])
        out.append((n, path, preferred))
    return out


# "3+ years with a Master's" (no "experience" right after the number)
MASTERS_PATH_YEARS_RE = re.compile(
    rf"{NUM}\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{{1,2}}\s*)?\+?\s*years?[^.;\d]{{0,40}}?"
    r"\b(?:for|with)\s+(?:a\s+|an\s+)?(?:master'?s|m\.?s\.?\b|graduate degree|advanced degree)",
    re.I)


def requirement_sentences(text, limit=1200):
    """The sentences about years of experience or degrees, kept so a job can be
    re-scored later after its description has been shortened."""
    parts = re.split(r"(?<=[.;•])\s+", text)
    keep = [x for x in parts if re.search(
        r"\byears?\b|degree|master|ph\.?\s?d|bachelor|\bM\.?S\.?\b|\bB\.?S\.?\b|"
        r"part[- ]time|contract|temporary|internship", x, re.I)]
    return " ".join(keep)[:limit]


def years_required(text):
    """Years of experience the posting requires (not counting a PhD path or
    nice-to-haves). Requirements in a list all apply at once, so this is the
    LARGEST one: "6+ years of analytics and 1+ year managing people" is 6."""
    found = [n for n, path, pref in _year_mentions(text) if path is None and not pref]
    if not found:   # only degree-specific numbers, e.g. "5+ years with a Bachelor's"
        found = [n for n, path, pref in _year_mentions(text) if path == "bachelors" and not pref]
    return max(found) if found else None


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


MASTERS_RE = re.compile(
    r"master'?s|\bM\.?S\.?\b(?! (?:Office|Excel|Word))|\bMSBA\b|\bMBA\b|"
    r"advanced degree|graduate degree", re.I)
# "Bachelor's + 3 years, or Master's + 1 year" -> years needed with a master's
MS_DEGREE = (r"(?:master'?s|\bM\.?S\.?(?:/M\.?A\.?)?\b"
             r"(?!\s*(?:Office|Excel|Word|Teams|Project|Access|SQL|Power))"
             r"|advanced degree|graduate degree)")
MASTERS_YEARS_RE = re.compile(
    MS_DEGREE + rf"[^.;]{{0,60}}?(?:with|and|plus|\+)\s*{NUM}\+?\s*(?:or more\s+)?(?:years?|yrs?)",
    re.I)
# "2 years of experience or a Master's degree" -> a master's replaces the experience
MASTERS_INSTEAD_RE = re.compile(
    r"years?[^.;]{0,50}\bor\b[^.;]{0,25}" + MS_DEGREE + r"(?![^.;]{0,30}\d)", re.I)


def masters_valued(text):
    """True when the posting asks for, prefers, or credits a master's degree."""
    return bool(MASTERS_RE.search(text))


def years_with_masters(text, years):
    """Experience required of someone with a master's degree."""
    masters_path = [n for n, path, pref in _year_mentions(text) if path == "masters" and not pref]
    for m in list(MASTERS_PATH_YEARS_RE.finditer(text)) + list(MASTERS_YEARS_RE.finditer(text)):
        n = m.group(1).lower()
        n = int(n) if n.isdigit() else WORDS.get(n)
        if n is not None:
            masters_path.append(n)
    if masters_path:            # "3-5+ years for Master's", "Master's + 1 year"
        return min(masters_path)
    if MASTERS_INSTEAD_RE.search(text):
        return 0                # "2 years of experience or a Master's degree"
    return years


# Wording that only fits experienced hires. Two or more of these = senior role.
SENIOR_LANGUAGE = re.compile(
    r"c-suite|c-level|vp-level|\bVPs?\b and|executive (?:stakeholder|leadership|presence|audiences?)|"
    r"present(?:ing)? to (?:and influenc\w+ )?(?:executives|senior leadership|the board)|"
    r"deep (?:experience|expertise)|extensive (?:experience|expertise)|seasoned|"
    r"track record of (?:driving|leading|building|scaling|delivering|managing|owning)|"
    r"subject matter expert|thought leader|mentor(?:ing)? (?:junior|other)|"
    r"(?:scaled|scaling) (?:a |an )?(?:team|organization|org|business)", re.I)


def senior_language(text):
    return len({m.group(0).lower() for m in SENIOR_LANGUAGE.finditer(text)}) >= 2


MANAGES_PEOPLE = re.compile(
    r"people management|direct reports|manag(?:e|ing) (?:a|the) team|"
    r"lead(?:ing)? a team of|team management experience|hire,? (?:and )?develop", re.I)


NOT_FULL_TIME_TITLE = re.compile(
    r"\bintern(ship)?\b|co-?op\b|part[- ]time|temporary|\btemp\b|seasonal|"
    r"\bcontract(or)?\b|apprentice|fellowship|\d+[- ]month", re.I)
NOT_FULL_TIME_TEXT = re.compile(
    r"this is a (?:part[- ]time|temporary|contract)|part[- ]time (?:role|position)|"
    r"(?:contract|temporary) (?:role|position|assignment)|\d+[- ]month contract|"
    r"duration:\s*\d+\s*months?", re.I)


def is_full_time(job):
    if job.get("contract_time") == "part_time" or job.get("contract_type") == "contract":
        return False
    if NOT_FULL_TIME_TITLE.search(job["title"]):
        return False
    return not NOT_FULL_TIME_TEXT.search(job["description"])


# ---------------------------------------------------------------- scoring

def score_job(job):
    """0-100 fit score built from the resume's skills and experience level."""
    text = f"{job['title']} {job['description']}".lower()
    title = job["title"].lower()
    p = CONFIG["profile"]

    matched = [s for s, pats in p["skills"].items() if matches_any(text, pats)]
    score = min(len(matched) * 8, 48)

    # Roles where a master's gives you an edge over bachelor's-only grads
    if job["masters_valued"]:
        score += 15
    if matches_any(title, p["masters_titles"]):
        score += 8
    # New-grad programs need no experience, and ones starting in 2027 line up
    # with finishing your master's in May 2027
    if job["new_grad_program"]:
        score += 10
    if job["starts_2027"]:
        score += 10
    if matches_any(title, p["too_senior"]):
        score -= 35
    if job.get("senior_language") or job.get("manages_people"):
        score -= 30
    years = job["effective_years"]
    if years is not None:
        if years == 0:
            score += 20     # your degree alone qualifies you
        elif years == 1:
            score += 8      # a stretch: internships may or may not count
        elif years == 2:
            score -= 5
        else:
            score -= 30
    if job["phd_required"]:
        score -= 30
    if matches_any(text, p["degree"]):
        score += 5
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
    # Each search is (parameters, how many pages of 50 results to read)
    # Statewide: specific job-title phrases
    searches = [({"what_phrase": q}, 2) for q in CONFIG["adzuna_queries"]]
    # Near you: every posting with these words in the title, newest first.
    # This catches all the analyst titles (product, project, pricing, insights...)
    # and the role filter in config.json decides which ones to keep.
    for word in CONFIG["adzuna_local_title_words"]:
        searches.append(({"title_only": word, "where": "Los Angeles",
                          "distance": 50, "sort_by": "date"}, 4))
        searches.append(({"title_only": word, "where": "Thousand Oaks",
                          "distance": 30, "sort_by": "date"}, 2))
    # Companies you like and big local employers without public job feeds
    for company in CONFIG["target_company_searches"] + CONFIG["local_company_searches"]:
        searches.append(({"what_and": company,
                          "what_or": "analyst analytics scientist project program"}, 1))

    jobs = []
    for s, pages in searches:
        for page in range(1, pages + 1):
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
                    "contract_time": j.get("contract_time"),
                    "contract_type": j.get("contract_type"),
                })
            time.sleep(2.5)  # Adzuna allows about 25 requests a minute
            if len(results) < 50:
                break
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
    failed_feeds = set()   # id prefixes of feeds that returned nothing today
    prefix = {"greenhouse": "gh", "ashby": "ashby", "lever": "lever"}
    for feed in CONFIG["company_feeds"]:
        got = FETCHERS[feed["platform"]](feed["board"], feed["company"])
        print(f"{feed['company']}: {len(got)} postings")
        if not got:
            failed_feeds.add(f"{prefix[feed['platform']]}-{feed['board']}-")
        raw += got
    print(f"Fetched {len(raw)} raw postings")

    # Jobs from earlier runs: used for "first seen" dates, and some are carried
    # over. Adzuna jobs are carried over for a while because each search only
    # looks at the last few days; company-site jobs are carried over only if
    # that company's feed failed today (otherwise a missing job means it closed).
    old = {}
    if OUT_FILE.exists():
        old = {j["id"]: j for j in json.loads(OUT_FILE.read_text()).get("jobs", [])}
    cutoff = (NOW - timedelta(days=CONFIG["keep_days"])).date().isoformat()
    today = NOW.date().isoformat()
    fresh_ids = {j["id"] for j in raw}
    carried = [
        j for jid, j in old.items()
        if jid not in fresh_ids
        and j.get("first_seen", today) >= cutoff
        and (jid.startswith("adzuna-") or any(jid.startswith(f) for f in failed_feeds))
    ]

    # Every job, new or carried over, goes through today's filters and scoring
    kept, seen_keys = {}, set()
    for job in raw + carried:
        family = role_family(job["title"])
        if not family or not location_ok(job["location"], job["remote"]):
            continue
        if matches_any(job["title"], CONFIG["exclude_titles"]):
            continue
        if job.get("requirements") and job["requirements"] not in job["description"]:
            job["description"] = f"{job['description']} {job['requirements']}"
        if not is_full_time(job):
            continue
        # Same title + company from two sources counts as one posting
        key = (job["title"].lower(), job["company"].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)

        job["role"] = family
        job["la_area"] = bool(LA_PATTERN.search(job["location"]))
        # Carried-over jobs have a shortened description, so add back the
        # requirement sentences saved on an earlier run
        if "requirements" not in job:
            job["requirements"] = requirement_sentences(job["description"])
        elif job["requirements"] not in job["description"]:
            job["description"] = f"{job['description']} {job['requirements']}"
        job["years_required"] = years_required(job["description"])
        job["masters_valued"] = masters_valued(job["description"])
        job["effective_years"] = years_with_masters(job["description"], job["years_required"])
        job["new_grad_program"] = matches_any(
            f"{job['title']} {job['description'][:400]}", CONFIG["profile"]["new_grad"])
        job["starts_2027"] = bool(re.search(
            r"2027 (?:start|cohort|graduat|new grad)|class of 2027|"
            r"graduat\w* (?:in |by )?(?:spring |summer |may |june |winter |december )?2027|"
            r"start(?:ing|s)? (?:in |date )?(?:summer |fall |spring )?2027",
            f"{job['title']} {job['description']}", re.I))
        # No years in the requirements, but "10+ years" under Preferred: that
        # still shows the level they're hiring at
        if job["effective_years"] is None:
            preferred = [n for n, path, pref in _year_mentions(job["description"])
                         if pref and path != "phd"]
            if preferred and min(preferred) >= 3:
                job["effective_years"] = min(preferred)
        # A new-grad program that doesn't state years is a no-experience role
        if job["effective_years"] is None and job["new_grad_program"]:
            job["effective_years"] = 0
        job["phd_required"] = phd_required(job["description"])
        job["senior_title"] = matches_any(job["title"], CONFIG["profile"]["too_senior"])
        job["manages_people"] = bool(MANAGES_PEOPLE.search(job["description"]))
        job["senior_language"] = senior_language(job["description"])
        job["experience_listed"] = job["effective_years"] is not None
        job["fits_experience"] = (
            not job["senior_title"]
            and not job["manages_people"]
            and not job["senior_language"]
            and not job["phd_required"]
            and (job["effective_years"] is None
                 or job["effective_years"] <= CONFIG["max_years_experience"])
        )
        job["target_company"] = is_target_company(job["company"])
        job["score"], job["matched_skills"] = score_job(job)
        job["first_seen"] = old.get(job["id"], {}).get("first_seen", today)
        job["description"] = job["description"][:600]
        kept[job["id"]] = job

    jobs = sorted(kept.values(), key=lambda j: (-j["score"], j["first_seen"]))
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps({"updated": NOW.isoformat(), "jobs": jobs}, indent=1))
    new_today = sum(j["first_seen"] == today for j in jobs)
    fits = sum(j.get("fits_experience", False) for j in jobs)
    masters = sum(j.get("masters_valued", False) for j in jobs)
    confirmed = sum(j.get("fits_experience") and j.get("experience_listed") for j in jobs)
    print(f"{confirmed} jobs need no experience (counting your master's and new-grad "
          f"programs), {fits - confirmed} more don't list experience, {masters} value a master's")
    companies = len({j["company"] for j in jobs})
    print(f"Saved {len(jobs)} jobs from {companies} companies ({new_today} new today)")


if __name__ == "__main__":
    main()
