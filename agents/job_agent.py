"""
Job Agent — runs ~weekly to refresh TPM job listings.

Responsibilities:
  1. Read company list from Excel
  2. For each company, discover Technical Program Manager job postings:
       Path A — Greenhouse/Lever/Ashby ATS API (fast, no crawl needed)
       Path B — Firecrawl + Crawl4AI for non-ATS or Workday fallback sites
  3. Scrape and extract structured JD data via Gemini
  4. Write new JDs to JD_Tracker sheet (skip already-tracked URLs)

Run:
  python agents/job_agent.py
"""
import os
import sys
import json
import asyncio
import hashlib
import re
import logging
import time
import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.excel_store import (
    EXCEL_PATH, PROJECT_ROOT, get_or_create_excel, get_company_rows,
    get_jd_url_meta, batch_update_jd_timestamps,
    get_jd_urls, upsert_jd_record, batch_upsert_jd_records,
    get_incomplete_jd_rows, count_tpm_jobs_by_company, update_company_job_counts,
    get_archived_companies, get_company_archive_info, update_archive_status,
    count_valid_tpm_jobs_by_company,
)

from shared.gemini_pool import _GeminiKeyPoolBase
from shared.rate_limiter import _RateLimiter
from shared.config import MODEL, AUTO_ARCHIVE_THRESHOLD
from shared.prompts import SECURITY_CLAUSE


# BUG-31: use _GeminiKeyPoolBase directly with genai_mod parameter
_GeminiKeyPool = _GeminiKeyPoolBase  # alias for backward compat (tests)

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

FRESH_DAYS = 5

# ── JD Markdown cache helpers ──────────────────────────────────────────────────
JD_CACHE_DIR = os.path.join(PROJECT_ROOT, "jd_cache")

def _cache_path(url: str) -> str:
    return os.path.join(JD_CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".md")

def _save_md_to_cache(url: str, markdown: str) -> None:
    os.makedirs(JD_CACHE_DIR, exist_ok=True)
    with open(_cache_path(url), "w", encoding="utf-8") as f:
        f.write(markdown)

def _load_md_from_cache(url: str) -> str | None:
    p = _cache_path(url)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return None

def _save_structured_jd_md(url: str, jd_dict: dict) -> None:
    """Save extracted structured JD fields as a readable markdown file.
    Stored at jd_cache/{md5}_structured.md for use by match_agent."""
    os.makedirs(JD_CACHE_DIR, exist_ok=True)
    h    = hashlib.md5(url.encode()).hexdigest()
    path = os.path.join(JD_CACHE_DIR, h + "_structured.md")

    title    = jd_dict.get("job_title", "")
    company  = jd_dict.get("company", "")
    location = jd_dict.get("location", "")
    salary   = jd_dict.get("salary_range", "")
    reqs     = jd_dict.get("requirements", [])
    addqs    = jd_dict.get("additional_qualifications", [])
    resps    = jd_dict.get("key_responsibilities", [])

    lines = [f"# {title} — {company}"]
    if location: lines.append(f"**Location:** {location}")
    if salary:   lines.append(f"**Salary:** {salary}")
    lines.append(f"**URL:** {url}")
    lines.append("")

    if reqs:
        lines.append("## Requirements")
        lines.extend(f"- {r}" for r in reqs)
        lines.append("")

    if addqs:
        lines.append("## Additional Qualifications")
        lines.extend(f"- {q}" for q in addqs)
        lines.append("")

    if resps:
        lines.append("## Responsibilities")
        lines.extend(f"- {r}" for r in resps)
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# ── Rate limiters ─────────────────────────────────────────────────────────────
_GEMINI_LIMITER = _RateLimiter(rpm=10)  # conservative: 15 RPM hard limit

_KEY_POOL: "_GeminiKeyPool | None" = None  # initialised in main()
_FC_MAP_LIMITER  = _RateLimiter(rpm=1)  # hard limit: 1 crawl/min (Firecrawl map)

# ── Pydantic schemas ──────────────────────────────────────────────────────────
class TargetJobURLs(BaseModel):
    urls: list[str] = Field(description="Filtered list of AI TPM job URLs.")

class JobDetails(BaseModel):
    job_title:                str
    company:                  str
    location:                 str   # All US locations joined with "; " (include Remote)
    salary_range:             str
    requirements:             list[str]  # Must-have qualifications (inferred regardless of heading)
    additional_qualifications: list[str] # Nice-to-have / preferred (inferred regardless of heading)
    key_responsibilities:     list[str]
    is_ai_tpm:                bool
    data_quality:             str | None = None  # BUG-47: populated by _assess_jd_quality

# ── ATS config (declarative routing table — REQ-062) ─────────────────────────
# Each entry defines: domains for URL matching, board/slug config for ATS
# detection, and routing functions for job listing (list_fn) and JD scraping
# (jd_fn).  Strategy is informational only.
# - list_fn / jd_fn: function name string (resolved at call time) or None
#   for default (crawler for list, generic scrape_jd for JD).
# - jd_domains: optional separate domain list for JD URL matching (when
#   JD URLs differ from career page URLs, e.g. Google, Tesla).
ATS_PLATFORMS = {
    "greenhouse": {
        "domains":            ["greenhouse.io", "job-boards.greenhouse.io"],
        "board_url_template": "https://job-boards.greenhouse.io/{slug}",
        "slug_pattern":       r"greenhouse\.io/([^/?#]+)",
        "strategy":           "json_api",
        "list_fn":            "_fetch_ats_jobs",
        "jd_fn":              None,  # gh_jid handled separately in _route_scraper
    },
    "lever": {
        "domains":            ["lever.co", "jobs.lever.co"],
        "board_url_template": "https://jobs.lever.co/{slug}",
        "slug_pattern":       r"lever\.co/([^/?#]+)",
        "strategy":           "json_api",
        "list_fn":            "_fetch_ats_jobs",
        "jd_fn":              None,  # generic scrape_jd
    },
    "ashby": {
        "domains":            ["ashbyhq.com", "jobs.ashbyhq.com"],
        "board_url_template": "https://jobs.ashbyhq.com/{slug}",
        "slug_pattern":       r"ashbyhq\.com/([^/?#]+)",
        "strategy":           "json_api",
        "list_fn":            "_fetch_ashby_jobs",
        "jd_fn":              None,  # generic scrape_jd
    },
    "workday": {
        "domains":            ["myworkdayjobs.com"],
        "board_url_template": None,
        "slug_pattern":       r"(https://[^/?#]*myworkdayjobs\.com/[^/?#]+)",
        "strategy":           "internal_api",
        "list_fn":            "_fetch_workday_jobs",
        "jd_fn":              "_scrape_workday_jd",
    },
    "google": {
        "domains":            [],  # Google has no ATS board URLs for discovery
        "jd_domains":         ["careers.google.com", "google.com/about/careers"],
        "board_url_template": None,
        "slug_pattern":       None,
        "strategy":           "custom",
        "list_fn":            None,
        "jd_fn":              "_scrape_google_jd",
    },
    "tesla": {
        "domains":            [],  # Tesla has no ATS board URLs for discovery
        "jd_domains":         ["tesla.com/careers"],
        "board_url_template": None,
        "slug_pattern":       None,
        "strategy":           "custom",
        "list_fn":            None,
        "jd_fn":              "_scrape_tesla_jd",
    },
}

# Derived constants from ATS_PLATFORMS for backward compatibility.
# API_ATS: primary domain per json_api platform (used by ALL_ATS for path splitting).
API_ATS = [cfg["domains"][0]
           for cfg in ATS_PLATFORMS.values()
           if cfg["strategy"] == "json_api" and cfg["domains"]]
WORKDAY_ATS = ATS_PLATFORMS["workday"]["domains"][:]
CRAWLER_ATS = []  # Currently unused; kept for backward compatibility
ALL_ATS = API_ATS + WORKDAY_ATS + CRAWLER_ATS


def _match_ats(url: str) -> tuple[str, dict] | None:
    """Match a URL against ATS_PLATFORMS routing table.

    Checks both 'domains' and 'jd_domains' lists.
    Returns (platform_name, config_dict) or None if no match.
    """
    for name, cfg in ATS_PLATFORMS.items():
        all_domains = cfg.get("domains", []) + cfg.get("jd_domains", [])
        if any(d in url for d in all_domains):
            return name, cfg
    return None

ATS_SEARCH_PARAM = {
    "greenhouse": "keyword=Technical+Program+Manager",
    "lever":      "search=Technical+Program+Manager",
    "ashby":      "search=Technical+Program+Manager",
}

# ── Company classification (affects is_ai_tpm prompt logic) ──────────────────
AI_NATIVE = {
    "openai", "anthropic", "cohere", "mistral", "inflection", "adept",
    "stability ai", "midjourney", "runway", "character.ai", "perplexity",
    "scale ai", "hugging face", "together ai", "replicate", "groq",
    "cerebras", "sambanova", "modal", "anyscale", "weights & biases",
    "langchain", "pinecone", "weaviate", "qdrant", "chroma",
    "harvey", "cognition", "cursor", "codium", "tabnine",
    "deepmind", "google deepmind", "xai", "x.ai",
    "lambda labs", "coreweave",
}
BIG_TECH = {"google", "meta", "microsoft", "amazon", "apple", "tesla"}

def _classify(name: str) -> str:
    n = name.lower()
    if any(k in n for k in AI_NATIVE): return "ai_native"
    if any(k in n for k in BIG_TECH):  return "big_tech"
    if "ai" in n.split():               return "ai_native"
    return "unknown"

def _classify_by_domain(ai_domain: str) -> str:
    """Classify using the ai_domain field stored in the company Excel row.
    Takes precedence over the hardcoded name-matching fallback."""
    d = (ai_domain or "").lower().strip()
    if not d or d in ("n/a", "unknown", ""):
        return "unknown"
    if "big tech" in d:
        return "big_tech"
    # All explicit AI domain categories → treat as ai_native
    return "ai_native"

# ── US location detection via pycountry (no hardcoded non-US lists) ───────────
def _build_us_index() -> tuple:
    """Build sets of US state names and 2-letter codes from pycountry."""
    import pycountry
    names, codes = set(), set()
    for sub in pycountry.subdivisions.get(country_code='US'):
        names.add(sub.name.lower())
        codes.add(sub.code.split('-')[1].lower())  # e.g. "US-CA" → "ca"
    return names, codes

# Hardcoded fallback used when pycountry is not installed.
_US_STATES_FALLBACK = [
    ("alabama", "al"), ("alaska", "ak"), ("arizona", "az"), ("arkansas", "ar"),
    ("california", "ca"), ("colorado", "co"), ("connecticut", "ct"),
    ("delaware", "de"), ("florida", "fl"), ("georgia", "ga"), ("hawaii", "hi"),
    ("idaho", "id"), ("illinois", "il"), ("indiana", "in"), ("iowa", "ia"),
    ("kansas", "ks"), ("kentucky", "ky"), ("louisiana", "la"), ("maine", "me"),
    ("maryland", "md"), ("massachusetts", "ma"), ("michigan", "mi"),
    ("minnesota", "mn"), ("mississippi", "ms"), ("missouri", "mo"),
    ("montana", "mt"), ("nebraska", "ne"), ("nevada", "nv"),
    ("new hampshire", "nh"), ("new jersey", "nj"), ("new mexico", "nm"),
    ("new york", "ny"), ("north carolina", "nc"), ("north dakota", "nd"),
    ("ohio", "oh"), ("oklahoma", "ok"), ("oregon", "or"),
    ("pennsylvania", "pa"), ("rhode island", "ri"), ("south carolina", "sc"),
    ("south dakota", "sd"), ("tennessee", "tn"), ("texas", "tx"),
    ("utah", "ut"), ("vermont", "vt"), ("virginia", "va"),
    ("washington", "wa"), ("west virginia", "wv"), ("wisconsin", "wi"),
    ("wyoming", "wy"), ("district of columbia", "dc"),
]

try:
    _US_STATE_NAMES, _US_STATE_CODES = _build_us_index()
except ImportError:
    _US_STATE_NAMES = {name for name, _ in _US_STATES_FALLBACK}
    _US_STATE_CODES = {code for _, code in _US_STATES_FALLBACK}
# A few well-known US metro areas not captured by state names alone
_US_METRO = {
    "bay area", "silicon valley", "nyc", "sf", "d.c.", "greater seattle",
    # Common US cities that don't match state names/codes alone
    "san francisco", "new york", "new york city", "los angeles", "seattle",
    "chicago", "boston", "austin", "denver", "atlanta", "dallas", "houston",
    "portland", "miami", "san jose", "palo alto", "menlo park",
    "mountain view", "sunnyvale", "redwood city", "bellevue", "brooklyn",
    "manhattan", "san diego", "phoenix", "las vegas", "detroit",
    "minneapolis", "st. louis", "pittsburgh", "philadelphia",
}


def _is_us_segment(seg: str) -> bool:
    """Return True if a single location segment is a US location."""
    s = seg.lower().strip()
    if not s:
        return False
    if "remote" in s:
        return True
    if re.search(r'\bus\b|\busa\b|\bu\.s\.a?\b|united states', s):
        return True
    # Full state name (word-boundary)
    for name in _US_STATE_NAMES:
        if re.search(r'\b' + re.escape(name) + r'\b', s):
            return True
    # 2-letter state code: require "City, ST" pattern or standalone segment.
    # Simple \b matching causes false positives for common words like "in"
    # (Indiana), "or" (Oregon), "de" (Delaware), "me" (Maine), "ok" (Oklahoma).
    for code in _US_STATE_CODES:
        if s == code or re.search(r',\s*' + re.escape(code) + r'\b', s):
            return True
    # Metro aliases
    if any(m in s for m in _US_METRO):
        return True
    return False


def _is_us(location: str) -> bool:
    """
    Return True if the location string should be kept (US or unknown).
    - Empty / None  → keep (unknown)
    - "N Locations" → keep (resolved earlier via URL path)
    - Single location → keep only if confirmed US
    - Multiple locations (semicolon/pipe separated) → keep if any segment is US
    """
    if not location:
        return True
    loc = location.strip()
    _UNKNOWN_PLACEHOLDERS = frozenset([
        "not specified", "n/a", "unknown", "not available",
        "not listed", "unspecified", "tbd", "tba",
    ])
    if loc.lower() in _UNKNOWN_PLACEHOLDERS:
        return True
    if re.match(r'^\d+\s+location', loc.lower()):
        return True  # already resolved via URL path fallback
    # Split on common multi-location separators
    segments = re.split(r'[;|]', loc)
    return any(_is_us_segment(seg) for seg in segments)

TPM_KW = ["technical program manager", "tpm", "technical program mgr",
          "tech program manager"]

# ── ATS API fetch (Path A: Greenhouse / Lever) ────────────────────────────────
_ATS_FETCH = {
    "greenhouse.io": {
        "slug_pattern":  r"greenhouse\.io/([^/?#]+)",
        "api_template":  "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "jobs_key":      "jobs",
        "title_field":   "title",
        "url_field":     "absolute_url",
        "id_field":      "id",
        "id_url":        "https://job-boards.greenhouse.io/{slug}/jobs/{id}",
    },
    "lever.co": {
        "slug_pattern":  r"lever\.co/([^/?#]+)",
        "api_template":  "https://api.lever.co/v0/postings/{slug}?mode=json",
        "jobs_key":      None,
        "title_field":   "text",
        "url_field":     "hostedUrl",
        "id_field":      "id",
        "id_url":        "https://jobs.lever.co/{slug}/{id}",
    },
}

def _fetch_ats_jobs(career_url: str) -> list:
    cfg = domain = None
    for d, c in _ATS_FETCH.items():
        if d in career_url:
            cfg, domain = c, d; break
    if not cfg:
        return []
    m = re.search(cfg["slug_pattern"], career_url)
    if not m:
        return []
    slug    = m.group(1).split('/')[0]
    api_url = cfg["api_template"].format(slug=slug)
    try:
        r    = requests.get(api_url, timeout=10, headers={"User-Agent": "PathFinder/1.0"})
        if r.status_code != 200:
            return []
        data     = r.json()
        job_list = data if cfg["jobs_key"] is None else data.get(cfg["jobs_key"], [])
        results  = []
        for job in job_list:
            title    = (job.get(cfg["title_field"]) or "").strip()
            job_url  = (job.get(cfg["url_field"])   or "").strip()
            job_id   = str(job.get(cfg["id_field"], "") or "").strip()
            if not job_url and job_id:
                job_url = cfg["id_url"].format(slug=slug, id=job_id)
            loc = job.get("location", {})
            loc = loc.get("name", "") if isinstance(loc, dict) else str(loc or "")
            if job_url and title:
                results.append({"url": job_url, "title": title, "location": loc.strip()})
        logging.info(f"[ATS API] {len(results)} jobs for {slug}")
        return results
    except Exception as e:
        logging.error(f"[ATS API] {e}")
        return []

def _fetch_ashby_jobs(career_url: str) -> list:
    """Fetch jobs from Ashby's public Job Posting API.

    Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
    Returns structured data matching Greenhouse/Lever format:
        [{"url": ..., "title": ..., "location": ...}, ...]
    """
    m = re.search(r"ashbyhq\.com/([^/?#]+)", career_url)
    if not m:
        logging.warning(f"[Ashby API] Cannot extract slug from {career_url}")
        return []
    slug = m.group(1).split('/')[0]
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    logging.info(f"[Ashby API] GET {api_url}")
    try:
        r = requests.get(api_url, timeout=10, headers={"User-Agent": "PathFinder/1.0"})
        if r.status_code != 200:
            logging.warning(f"[Ashby API] {r.status_code} for {api_url}")
            return []
        data = r.json()
        jobs = data.get("jobs", [])
        results = []
        for job in jobs:
            title = (job.get("title") or "").strip()
            job_id = job.get("id", "")
            posting_url = f"https://jobs.ashbyhq.com/{slug}/{job_id}" if job_id else ""
            # Prefer publishedUrl / jobUrl if available
            job_url = (job.get("publishedUrl") or job.get("jobUrl") or posting_url).strip()
            loc = job.get("location", "")
            if isinstance(loc, dict):
                loc = loc.get("name", "")
            loc = str(loc or "").strip()
            if job_url and title:
                results.append({"url": job_url, "title": title, "location": loc})
        logging.info(f"[Ashby API] {len(results)} jobs for {slug}")
        return results
    except Exception as e:
        logging.error(f"[Ashby API] {e}")
        return []

def _tpm_filter(links: list) -> list:
    filtered = []
    skipped  = 0
    for lnk in links:
        if not any(kw in lnk.get("title","").lower() for kw in TPM_KW):
            continue
        if not _is_us(lnk.get("location", "")):
            skipped += 1; continue
        filtered.append(lnk)
    if skipped:
        print(f"    Location filter: skipped {skipped} non-US jobs.")
    return filtered

# Title keywords that flag clearly non-AI TPM roles at Big Tech companies
_TITLE_BLOCK_KW = frozenset([
    "finance", "legal", "hr", "human resources", "supply chain",
    "logistics", "retail", "marketing", "sales", "operations",
    "recruiting", "facilities", "real estate", "accounting",
    "tax", "treasury", "procurement", "customer success",
])

def _ai_title_prefilter(links: list, ai_domain: str) -> list:
    """Block TPM roles with explicit non-AI domain keywords in the title.
    Only applied to Big Tech companies; AI-native companies pass all through."""
    if "big tech" not in ai_domain.lower():
        return links
    filtered, blocked = [], 0
    for lnk in links:
        if any(kw in lnk.get("title", "").lower() for kw in _TITLE_BLOCK_KW):
            blocked += 1
            continue
        filtered.append(lnk)
    if blocked:
        print(f"    AI title pre-filter: removed {blocked} non-AI-domain TPM roles.")
    return filtered

# ── Path W: Workday JSON API ──────────────────────────────────────────────────
def _fetch_workday_jobs(career_url: str) -> list:
    """
    Workday exposes an undocumented but widely-used POST JSON API.
    URL pattern: https://{company}[.wd5].myworkdayjobs.com/{site}
    API endpoint: https://{company}[.wd5].myworkdayjobs.com/wday/cxs/{company}/{site}/jobs
    Supports both standard (company.wd5.myworkdayjobs.com) and
    no-wd-prefix (company.myworkdayjobs.com) URL formats.
    """
    m = re.match(r"https://([^.]+)(?:\.wd\d+)?\.myworkdayjobs\.com/([^/?#]+)", career_url)
    if not m:
        logging.warning(f"[Workday] Cannot parse slug from {career_url}")
        return []
    company_slug = m.group(1)
    site_slug    = m.group(2)
    # Strip any sub-path (keep only the site name)
    site_slug    = site_slug.split('/')[0]

    # Preserve the original host (with or without wd prefix)
    host_m = re.match(r"(https://[^/]+)", career_url)
    host   = host_m.group(1)

    api_url = f"{host}/wday/cxs/{company_slug}/{site_slug}/jobs"
    payload = {"limit": 20, "offset": 0, "searchText": "Technical Program Manager"}
    logging.info(f"[Workday API] POST {api_url}")
    try:
        r = requests.post(
            api_url, json=payload, timeout=12,
            headers={"Content-Type": "application/json", "User-Agent": "PathFinder/1.0"},
        )
        if r.status_code != 200:
            logging.warning(f"[Workday API] {r.status_code} for {api_url}")
            return []
        base     = f"{host}/{site_slug}"
        postings = r.json().get("jobPostings", [])
        results  = []
        for p in postings:
            path  = p.get("externalPath", "")
            title = p.get("title", "").strip()
            loc   = p.get("locationsText", "") or ""
            # For "N Locations", fall back to the country/city in the URL path
            # e.g. /job/Israel-Yokneam/... → "Israel-Yokneam"
            if re.match(r'^\d+\s+location', loc.lower()):
                url_loc_m = re.search(r'/job/([^/]+)/', path)
                if url_loc_m:
                    loc = url_loc_m.group(1).replace('-', ', ')
            if path and title:
                results.append({"url": base + path, "title": title, "location": loc,
                                 "_workday": True})
        logging.info(f"[Workday API] {len(results)} postings for {company_slug}/{site_slug}")
        return results
    except Exception as e:
        logging.error(f"[Workday API] {e}")
        return []

# ── ATS link detection in rendered pages ─────────────────────────────────────
def _detect_ats(links: list) -> dict:
    for lnk in links:
        href = lnk.get("url","") or lnk.get("href","")
        for pname, pcfg in ATS_PLATFORMS.items():
            if any(d in href for d in pcfg["domains"]):
                m = re.search(pcfg["slug_pattern"], href)
                if not m: continue
                board = m.group(1) if pname == "workday" else \
                        pcfg["board_url_template"].format(slug=m.group(1).split('/')[0])
                return {"platform": pname, "board_url": board}
    return {}

# ── Async crawlers (Path B) ───────────────────────────────────────────────────
async def _crawl_page(url: str, crawler) -> list:
    from crawl4ai import CrawlerRunConfig, CacheMode
    logging.info(f"[Crawl4AI] Rendering page (timeout 60s): {url}")
    scroll_js = """
    (async () => {
        try {
            let last = 0, n = 0;
            while (n < 3) {
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise(r => setTimeout(r, 1500));
                let h = document.body.scrollHeight;
                n = h === last ? n + 1 : 0; last = h;
            }
        } catch(e) {}
    })();
    """
    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, page_timeout=60000,
        magic=True, js_code=scroll_js, delay_before_return_html=3.0,
    )
    try:
        res = await crawler.arun(url=url, config=cfg)
        if not res.success: return []
        out = []
        if hasattr(res, "links") and isinstance(res.links, dict):
            for lnk in res.links.get("internal",[]) + res.links.get("external",[]):
                href = lnk.get("href","") or lnk.get("url","")
                text = lnk.get("text","") or lnk.get("title","")
                if href and href.startswith("http"):
                    out.append({"url": href.strip(), "title": text.strip()})
        return out
    except Exception as e:
        logging.error(f"[Crawl4AI] {e}")
        return []

async def _crawl_ats_board(board_url: str, platform: str, crawler) -> list:
    param = ATS_SEARCH_PARAM.get(platform, "")
    url   = f"{board_url}?{param}" if param else board_url
    return await _crawl_page(url, crawler)

def _firecrawl_map(career_url: str, fc_key: str) -> list:
    from firecrawl import FirecrawlApp
    app = FirecrawlApp(api_key=fc_key)
    for attempt in range(3):
        try:
            logging.info(f"[Firecrawl] map attempt {attempt + 1}/3: {career_url}")
            res = app.map(url=career_url, search="Technical Program Manager", limit=100)
            urls = getattr(res, "links", None) or (res.get("links",[]) if isinstance(res,dict) else res if isinstance(res,list) else [])
            out  = []
            for u in urls:
                if hasattr(u, "url"):
                    out.append({"url": u.url, "title": getattr(u,"title","") or ""})
                elif isinstance(u, dict) and "url" in u:
                    out.append({"url": u["url"], "title": u.get("title","")})
                elif isinstance(u, str):
                    out.append({"url": u, "title": ""})
            return out
        except Exception as e:
            err = str(e)
            logging.error(f"[Firecrawl] attempt {attempt + 1}/3 failed: {err}")
            # Rate-limit errors need a long back-off; other transient errors
            # (network blip, timeout) deserve a short retry rather than giving up.
            if attempt < 2:
                sleep_s = 30 * (attempt + 1) if ("Rate limit" in err or "429" in err) else 5 * (attempt + 1)
                logging.info(f"[Firecrawl] retrying in {sleep_s}s...")
                time.sleep(sleep_s)
    return []

def _merge_filter(batches: list) -> list:
    URL_KW   = ["job","career","role","opening","position","greenhouse","lever","ashby","workday","jobs"]
    TITLE_KW = ["tpm","manager","technical","program","product"]
    DEAD_KW  = ["404","not found","closed","filled","no longer available"]
    seen, out = set(), []
    for batch in batches:
        for lnk in batch:
            u = lnk.get("url","").strip()
            if u and u not in seen:
                seen.add(u); out.append(lnk)
    return [l for l in out
            if not any(k in l.get("title","").lower() for k in DEAD_KW)
            and (any(k in l["url"].lower() for k in URL_KW)
                 or any(k in l.get("title","").lower() for k in TITLE_KW))]

# ── Job discovery helpers (routing table lookup) ─────────────────────────────
def _resolve_list_fn(fn_name: str):
    """Resolve a list_fn name string to the actual callable."""
    return {
        "_fetch_ats_jobs":     _fetch_ats_jobs,
        "_fetch_ashby_jobs":   _fetch_ashby_jobs,
        "_fetch_workday_jobs": _fetch_workday_jobs,
    }[fn_name]


async def _discover_via_api(career_url: str, platform: str, cfg: dict, crawler) -> list | None:
    """Attempt API-based job discovery for a matched ATS platform.

    Returns a candidate list on success, or None to signal fallback needed.
    """
    fn_name = cfg.get("list_fn")
    if not fn_name:
        return None  # No list function — platform is JD-only (Google, Tesla)

    list_fn = _resolve_list_fn(fn_name)
    strategy = cfg.get("strategy", "")

    if strategy == "json_api":
        print(f"    → Path A: ATS API ({platform.title()})...")
        jobs = list_fn(career_url)
        if jobs:
            cands = _tpm_filter(jobs)
            print(f"    → {len(jobs)} total, {len(cands)} TPM candidates")
            return cands
        print("    → API failed, falling back to crawler")
        return None  # Signal: fall through to crawler path

    if strategy == "internal_api":
        # Workday: API → filter → crawler fallback
        print("    → Path W: Workday JSON API...")
        jobs = list_fn(career_url)
        if jobs:
            cands = _tpm_filter(jobs)
            print(f"    → {len(jobs)} total, {len(cands)} TPM candidates")
            if cands:
                return cands
            print("    → Workday API: no TPM matches in API results, falling back to crawler")
        else:
            print("    → Workday API failed, falling back to crawler")
        links = await _crawl_page(career_url, crawler)
        cands = _tpm_filter(links)
        print(f"    → Crawled {len(links)} links, {len(cands)} TPM candidates")
        return cands

    return None


# ── Job discovery router ──────────────────────────────────────────────────────
async def discover_jobs(career_url: str, fc_key: str, crawler) -> list:
    # 1. Direct URL match against routing table
    match = _match_ats(career_url)
    if match:
        platform, cfg = match
        result = await _discover_via_api(career_url, platform, cfg, crawler)
        if result is not None:
            return result
        # json_api fallback: API returned empty, fall through to crawler path below

    # 2. Company website — Firecrawl (rate-limited) + Crawl4AI, then detect ATS
    print("    → Path B: crawling career page...")
    print("    ⏳ Waiting for Firecrawl rate limiter (1 RPM)...")
    await _FC_MAP_LIMITER.acquire()
    print(f"    🌐 Firecrawl map: {career_url}")
    fc_links  = await asyncio.to_thread(_firecrawl_map, career_url, fc_key)
    print(f"    🕷️ Crawl4AI: rendering page (timeout 60s)...")
    c4_links  = await _crawl_page(career_url, crawler)
    ats       = _detect_ats(fc_links + c4_links)

    if ats:
        platform  = ats["platform"]
        board_url = ats["board_url"]
        print(f"    → ATS detected: {platform.upper()} | {board_url}")
        # Try API fetch for detected ATS board
        board_match = _match_ats(board_url)
        if board_match:
            _, board_cfg = board_match
            board_fn_name = board_cfg.get("list_fn")
            if board_fn_name and board_cfg.get("strategy") == "json_api":
                jobs = _resolve_list_fn(board_fn_name)(board_url)
                if jobs:
                    cands = _tpm_filter(jobs)
                    print(f"    → {len(jobs)} total, {len(cands)} TPM candidates")
                    return cands
        links = await _crawl_ats_board(board_url, platform, crawler)
        cands = _tpm_filter(links)
        print(f"    → Crawled {len(links)} links, {len(cands)} TPM candidates")
        return cands

    merged = _merge_filter([fc_links + c4_links])
    print(f"    → No ATS. Firecrawl={len(fc_links)}, Crawl4AI={len(c4_links)}, merged={len(merged)}")
    return merged

# ── LLM job filter (Path B non-ATS) ──────────────────────────────────────────
def llm_filter_jobs(company: str, links: list) -> list:
    if not links: return []
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking llm_filter_jobs()")
    cfg = types.GenerateContentConfig(
        system_instruction=(
            "You are a strict Technical Recruiter specializing in AI. "
            "Return ONLY URLs for Technical Program Manager / TPM roles that are "
            "directly related to one or more of: AI/ML models (LLM, GenAI, foundation models), "
            "AI products or platforms, AI/ML infrastructure (model serving, MLOps, GPU clusters), "
            "compute or cloud infrastructure for AI, or chips/semiconductors. "
            "Reject TPM roles in Finance, HR, Legal, Marketing, or general non-AI engineering. "
            "Reject Product Manager, Engineering Manager, Project Manager, and similar titles. "
            "Reject ghost/closed/404 links."
            + SECURITY_CLAUSE
        ),
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=TargetJobURLs,
    )
    try:
        resp = _KEY_POOL.generate_content(
            model=MODEL,
            contents=(
                f"Company: {company}\nJobs:\n"
                f"<scraped_content>\n{json.dumps(links)}\n</scraped_content>\n\n"
                "Extract AI TPM URLs."
            ),
            config=cfg,
        )
        return json.loads(resp.text).get("urls", [])
    except Exception as e:
        logging.error(f"LLM filter failed: {e}")
        return []

# ── Google Careers JD scraper (Firecrawl only_main_content avoids sidebar) ─────
def _scrape_google_jd(url: str, fc_key: str = "") -> str:
    """
    Google Careers pages are JS SPAs — server-rendered HTML contains no JSON-LD.
    Crawl4AI renders the full page including the sidebar job list, which causes
    Gemini to extract info from the wrong (sidebar) job.

    Fix: use Firecrawl with only_main_content=True to strip navigation/sidebars
    and return just the main job description area.
    Falls back to plain requests + JSON-LD check (in case Google ever adds it).
    """
    # ── 1. Firecrawl with only_main_content (primary) ────────────────────────
    if fc_key:
        try:
            from firecrawl import FirecrawlApp
            app = FirecrawlApp(api_key=fc_key)
            result = app.scrape(url, formats=["markdown"], only_main_content=True)
            md = getattr(result, "markdown", None) or (
                result.get("markdown", "") if isinstance(result, dict) else "")
            if md and len(md) > 200:
                logging.info(f"[Google JD] Firecrawl fetched: {url}")
                return md[:8000]
        except Exception as e:
            logging.debug(f"[Google JD] Firecrawl failed: {e}")

    # ── 2. Plain HTTP + JSON-LD (future-proof fallback) ───────────────────────
    _headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = requests.get(url, timeout=15, headers=_headers)
        if r.status_code == 200:
            blocks = re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                r.text, re.DOTALL | re.IGNORECASE,
            )
            for block in blocks:
                try:
                    d = json.loads(block)
                except Exception:
                    continue
                if d.get("@type") != "JobPosting":
                    continue
                desc = d.get("description", "")
                if not desc:
                    continue
                try:
                    desc = desc.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
                desc = re.sub(r"<[^>]+>", " ", desc)
                desc = re.sub(r"&[a-z]+;|&#\d+;", " ", desc)
                desc = re.sub(r"\s+", " ", desc).strip()
                job_loc = d.get("jobLocation", {})
                if isinstance(job_loc, list):
                    loc_parts = []
                    for loc in job_loc:
                        if isinstance(loc, dict):
                            addr = loc.get("address", {})
                            s = ", ".join(p for p in [addr.get("addressLocality",""),
                                                       addr.get("addressRegion",""),
                                                       addr.get("addressCountry","")] if p)
                            if s: loc_parts.append(s)
                    location = "; ".join(loc_parts)
                elif isinstance(job_loc, dict):
                    addr = job_loc.get("address", {})
                    location = ", ".join(p for p in [addr.get("addressLocality",""),
                                                      addr.get("addressRegion",""),
                                                      addr.get("addressCountry","")] if p)
                else:
                    location = ""
                sal_m = re.search(
                    r'\$[\d,]+\s*(?:USD)?\s*(?:[-–]|to)\s*\$[\d,]+\s*(?:USD)?',
                    desc, re.IGNORECASE,
                )
                salary = sal_m.group(0) if sal_m else ""
                lines  = []
                title  = d.get("title", "")
                if title:    lines.append(f"Title: {title}")
                if location: lines.append(f"Location: {location}")
                if salary:   lines.append(f"Salary: {salary}")
                lines.append("")
                lines.append(desc[:6000])
                logging.info(f"[Google JD] JSON-LD fetched: {url}")
                return "\n".join(lines)
    except Exception as e:
        logging.debug(f"[Google JD] requests failed: {e}")

    return ""


# ── Workday JD scraper (plain HTTP + JSON-LD, no browser needed) ──────────────
def _scrape_workday_jd(url: str) -> str:
    """
    Workday job pages embed the full JD in a <script type="application/ld+json"> block.
    Fetch with plain requests — no JS rendering required.
    Returns cleaned text ready for extract_jd().
    """
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if r.status_code != 200:
            return ""
        html = r.text
        # Extract JSON-LD block
        blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
        # BUG-53: moved out of loop — pure utility, no closure over loop vars
        def _fmt_addr(addr: dict) -> str:
            parts = [
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("addressCountry", ""),
            ]
            return ", ".join(p for p in parts if p)

        for block in blocks:
            try:
                d = json.loads(block)
            except Exception:
                continue
            desc = d.get("description", "")
            if not desc:
                continue
            # Fix common Mojibake (UTF-8 bytes stored as Latin-1 JSON escapes)
            try:
                desc = desc.encode("latin-1").decode("utf-8")
            except Exception:
                pass
            # Strip HTML tags
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"&[a-z]+;|&#\d+;", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            # Build context string for extract_jd
            title    = d.get("title", "")
            job_loc_raw = d.get("jobLocation", {})

            if isinstance(job_loc_raw, list):
                loc_parts = []
                for _loc in job_loc_raw:
                    if isinstance(_loc, dict):
                        loc_str = _fmt_addr(_loc.get("address", {}))
                        if loc_str:
                            loc_parts.append(loc_str)
                location = "; ".join(loc_parts)
            elif isinstance(job_loc_raw, dict):
                location = _fmt_addr(job_loc_raw.get("address", {}))
            else:
                location = ""
            # Salary: prefer baseSalary JSON-LD field, fall back to regex in desc
            salary_text = ""
            base_sal = d.get("baseSalary", {})
            if isinstance(base_sal, dict):
                val = base_sal.get("value", {})
                if isinstance(val, dict):
                    min_v = val.get("minValue", "")
                    max_v = val.get("maxValue", "")
                    currency = base_sal.get("currency", "USD")
                    if min_v and max_v:
                        try:
                            salary_text = f"${int(float(min_v)):,} - ${int(float(max_v)):,} {currency}"
                        except Exception:
                            salary_text = f"{min_v} - {max_v} {currency}"
            if not salary_text:
                sal_m = re.search(
                    r'\$[\d,]+\s*(?:USD)?\s*(?:[-–]|to)\s*\$[\d,]+\s*(?:USD)?',
                    desc, re.IGNORECASE,
                )
                if sal_m:
                    salary_text = sal_m.group(0)
            lines = []
            if title:    lines.append(f"Title: {title}")
            if location: lines.append(f"Location: {location}")
            if salary_text: lines.append(f"Salary: {salary_text}")
            lines.append("")
            lines.append(desc[:4000])
            return "\n".join(lines)
    except Exception as e:
        logging.error(f"[Workday scrape] {e}")
    return ""


# ── Tesla JD scraper (Firecrawl → CUA API → __NEXT_DATA__ → JSON-LD → browser) ─
def _scrape_tesla_jd(url: str, fc_key: str = "") -> str:
    """
    Tesla career pages are Next.js apps — direct HTTP is blocked by Akamai (403).
    Tries (in order):
      0. Firecrawl      — bypasses bot protection, returns clean markdown
      1. Tesla CUA API  — fast, structured JSON (may 403)
      2. __NEXT_DATA__  — embedded Next.js page props (may 403)
      3. JSON-LD block  — structured data with location/salary
    Remaining fallback to browser is handled by the caller.
    """
    # ── 0. Firecrawl (primary — bypasses Akamai 403 bot protection) ──────────
    if fc_key:
        try:
            from firecrawl import FirecrawlApp
            app = FirecrawlApp(api_key=fc_key)
            result = app.scrape(url=url, formats=["markdown"])
            md = getattr(result, "markdown", None) or ""
            if md and len(md) > 200:
                logging.info(f"[Tesla Firecrawl] Fetched: {url}")
                return md
        except Exception as e:
            logging.debug(f"[Tesla Firecrawl] {e}")
    _headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def _build_tesla_text(title, location, salary, desc) -> str:
        desc = re.sub(r"<[^>]+>", " ", desc or "")
        desc = re.sub(r"&[a-z]+;|&#\d+;", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        if not (title or desc):
            return ""
        lines = []
        if title:    lines.append(f"Title: {title}")
        if location: lines.append(f"Location: {location}")
        if salary:   lines.append(f"Salary: {salary}")
        lines.append("")
        lines.append(desc[:6000])
        return "\n".join(lines)

    # ── 1. Tesla CUA API ──────────────────────────────────────────────────────
    job_id_m = re.search(r'-(\d+)(?:[/?#]|$)', url)
    if job_id_m:
        job_id = job_id_m.group(1)
        try:
            api_url = f"https://www.tesla.com/cua-api/tesla-jobs/job/{job_id}"
            ra = requests.get(api_url, timeout=10, headers={
                **_headers, "Accept": "application/json, text/plain, */*",
            })
            if ra.status_code == 200:
                d = ra.json()
                title  = d.get("title", "") or d.get("jobTitle", "")
                desc   = d.get("description", "") or d.get("jobDescription", "")
                salary = (d.get("compensation", "") or d.get("salaryRange", "")
                          or d.get("pay", ""))
                # Location: various shapes in Tesla API response
                loc_raw = d.get("location", d.get("locations", d.get("jobLocations", {})))
                if isinstance(loc_raw, list):
                    loc_parts = []
                    for l in loc_raw:
                        if isinstance(l, dict):
                            parts = [l.get("city",""), l.get("state",""), l.get("country","")]
                            loc_parts.append(", ".join(p for p in parts if p))
                        elif isinstance(l, str):
                            loc_parts.append(l)
                    location = "; ".join(loc_parts)
                elif isinstance(loc_raw, dict):
                    parts = [loc_raw.get("city",""), loc_raw.get("state",""),
                             loc_raw.get("country","")]
                    location = ", ".join(p for p in parts if p)
                else:
                    location = str(loc_raw or "")
                text = _build_tesla_text(title, location, salary, desc)
                if text:
                    logging.info(f"[Tesla CUA API] Fetched: {url}")
                    return text
        except Exception as e:
            logging.debug(f"[Tesla CUA API] {e}")

    # ── 2. Plain HTTP: __NEXT_DATA__ ──────────────────────────────────────────
    try:
        r = requests.get(url, timeout=15, headers=_headers)
        if r.status_code != 200:
            return ""
        html = r.text

        m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                      html, re.DOTALL)
        if m:
            try:
                data  = json.loads(m.group(1))
                props = data.get("props", {}).get("pageProps", {})
                job   = (props.get("job")
                         or props.get("jobDetail")
                         or props.get("data", {}).get("job", {}))
                if job:
                    title = job.get("title", "")
                    loc_raw = job.get("location", job.get("locations", {}))
                    if isinstance(loc_raw, list):
                        loc_parts = []
                        for l in loc_raw:
                            if isinstance(l, dict):
                                parts = [l.get("city",""), l.get("state",""), l.get("country","")]
                                loc_parts.append(", ".join(p for p in parts if p))
                            elif isinstance(l, str):
                                loc_parts.append(l)
                        location = "; ".join(loc_parts)
                    elif isinstance(loc_raw, dict):
                        parts = [loc_raw.get("city",""), loc_raw.get("state",""),
                                 loc_raw.get("country","")]
                        location = ", ".join(p for p in parts if p)
                    else:
                        location = str(loc_raw or "")
                    salary = job.get("compensation", job.get("salary", ""))
                    desc   = job.get("description", job.get("body", job.get("content", "")))
                    text = _build_tesla_text(title, location, salary, desc)
                    if text:
                        return text
            except Exception as e:
                logging.error(f"[Tesla NEXT_DATA] {e}")

        # ── 3. JSON-LD fallback (with location + salary) ──────────────────────
        blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
        for block in blocks:
            try:
                d = json.loads(block)
            except Exception:
                continue
            desc = d.get("description", "")
            if not desc:
                continue
            try:
                desc = desc.encode("latin-1").decode("utf-8")
            except Exception:
                pass
            title = d.get("title", "")
            # Location from JSON-LD
            job_loc = d.get("jobLocation", {})
            if isinstance(job_loc, dict):
                addr = job_loc.get("address", {})
                parts = [addr.get("addressLocality", ""), addr.get("addressRegion", "")]
                location = ", ".join(p for p in parts if p)
            elif isinstance(job_loc, list):
                loc_parts = []
                for _loc in job_loc:
                    if isinstance(_loc, dict):
                        addr = _loc.get("address", {})
                        parts = [addr.get("addressLocality", ""), addr.get("addressRegion", "")]
                        s = ", ".join(p for p in parts if p)
                        if s:
                            loc_parts.append(s)
                location = "; ".join(loc_parts)
            else:
                location = ""
            # Salary from JSON-LD or regex
            salary_text = ""
            base_sal = d.get("baseSalary", {})
            if isinstance(base_sal, dict):
                val = base_sal.get("value", {})
                if isinstance(val, dict):
                    min_v = val.get("minValue", "")
                    max_v = val.get("maxValue", "")
                    currency = base_sal.get("currency", "USD")
                    if min_v and max_v:
                        try:
                            salary_text = f"${int(float(min_v)):,} - ${int(float(max_v)):,} {currency}"
                        except Exception:
                            salary_text = f"{min_v} - {max_v} {currency}"
            if not salary_text:
                sal_m = re.search(
                    r'\$[\d,]+\s*(?:USD)?\s*(?:[-–]|to)\s*\$[\d,]+\s*(?:USD)?',
                    desc, re.IGNORECASE,
                )
                if sal_m:
                    salary_text = sal_m.group(0)
            text = _build_tesla_text(title, location, salary_text, desc)
            if text:
                return text
    except Exception as e:
        logging.error(f"[Tesla scrape] {e}")
    return ""


# ── Greenhouse API JD fetcher (for gh_jid-embedded URLs, e.g. CoreWeave) ──────
def _scrape_greenhouse_api_jd(board: str, job_id: str) -> str:
    """
    Fetch a single job description via the Greenhouse public API.
    Used when a career page embeds a Greenhouse job via ?gh_jid= query param.
    """
    api_url = (f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
               "?questions=true")
    try:
        r = requests.get(api_url, timeout=10, headers={"User-Agent": "PathFinder/1.0"})
        if r.status_code != 200:
            return ""
        d        = r.json()
        title    = d.get("title", "")
        location = d.get("location", {}).get("name", "")
        content  = d.get("content", "")
        content  = re.sub(r"<[^>]+>", " ", content)
        content  = re.sub(r"&[a-z]+;|&#\d+;", " ", content)
        content  = re.sub(r"\s+", " ", content).strip()
        # Check keyed_custom_fields for salary (some GH boards store it there)
        salary_text = ""
        kcf = d.get("keyed_custom_fields", {})
        for field_key in ("salary_range", "compensation", "pay_range", "base_salary"):
            field = kcf.get(field_key, {})
            if isinstance(field, dict):
                val = field.get("value", "")
                if val:
                    salary_text = str(val)
                    break
        # Also try regex on content (catches "$X to $Y" and "$X - $Y" formats)
        if not salary_text:
            sal_m = re.search(
                r'\$[\d,]+\s*(?:USD)?\s*(?:[-–]|to)\s*\$[\d,]+\s*(?:USD)?',
                content, re.IGNORECASE,
            )
            if sal_m:
                salary_text = sal_m.group(0)
        lines = []
        if title:        lines.append(f"Title: {title}")
        if location:     lines.append(f"Location: {location}")
        if salary_text:  lines.append(f"Salary: {salary_text}")
        lines.append("")
        lines.append(content[:8000])
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"[GH API JD] {e}")
    return ""


def _extract_gh_jid(url: str) -> tuple:
    """
    If a URL contains gh_jid and board query params (e.g. CoreWeave career pages),
    return (board, job_id). Otherwise return ("", "").
    """
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        jid    = params.get("gh_jid", [""])[0]
        board  = params.get("board",  [""])[0]
        return board, jid
    except Exception:
        return "", ""


# ── JD content validation ─────────────────────────────────────────────────────
_SOFT_404_KEYWORDS = [
    "no longer available", "job has been filled", "position has been closed",
    "posting has expired", "this role is no longer open", "we've filled this role",
    "application closed", "hiring paused", "job not available",
    "this position has been filled", "this job is no longer accepting",
    "no longer accepting applications", "role has been filled",
]

_HARD_404_KEYWORDS = [
    "page not found", "404 not found", "error 404", "the page you",
    "something went wrong", "not found",
]

_JD_POSITIVE_SIGNALS = [
    "responsibilities", "qualifications", "requirements", "what you'll do",
    "what we're looking for", "about the role", "about this role",
    "job description", "who you are", "what you bring", "your background",
    "what we expect", "key responsibilities", "preferred qualifications",
]

_MIN_LENGTH_WITH_SIGNAL = 80
_MIN_LENGTH_WITHOUT_SIGNAL = 200


def _is_valid_jd_content(markdown: str) -> tuple[bool, str]:
    """Validate scraped content as a real JD.

    Returns (is_valid, reason) where reason is one of:
        "too_short", "soft_404", "hard_404", "no_jd_content", "ok"
    """
    if not markdown or len(markdown.strip()) < _MIN_LENGTH_WITH_SIGNAL:
        return False, "too_short"

    low = markdown.lower()

    if any(kw in low for kw in _SOFT_404_KEYWORDS):
        return False, "soft_404"

    if any(kw in low for kw in _HARD_404_KEYWORDS):
        return False, "hard_404"

    has_signal = any(kw in low for kw in _JD_POSITIVE_SIGNALS)

    if not has_signal and len(markdown.strip()) < _MIN_LENGTH_WITHOUT_SIGNAL:
        return False, "too_short"

    if not has_signal:
        return False, "no_jd_content"

    return True, "ok"


# ── JD scraping & extraction ──────────────────────────────────────────────────
async def scrape_jd(url: str, crawler) -> str:
    from crawl4ai import CrawlerRunConfig, CacheMode
    logging.info(f"[Crawl] Browser rendering (timeout 35s): {url}")
    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, page_timeout=35000,
        magic=True, delay_before_return_html=3.0,
    )
    try:
        res = await crawler.arun(url=url, config=cfg)
        if not res.success or res.status_code == 404:
            return ""
        md = res.markdown or ""
        valid, reason = _is_valid_jd_content(md)
        if not valid:
            logging.debug(f"[Crawl] Invalid JD content ({reason}): {url}")
            return ""
        return md[:8000]
    except Exception as e:
        logging.error(f"[Crawl] {e}")
        return ""

def md5(text: str) -> str:
    t = re.sub(r'https?://\S+', '', text)
    t = re.sub(r'!\[.*?\]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return hashlib.md5(t.encode()).hexdigest()

def extract_jd(markdown: str, company: str = "", ai_domain: str = "") -> str:
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking extract_jd()")
    cls = _classify_by_domain(ai_domain) if ai_domain else _classify(company)
    _common_instr = (
        " Extract the job_title from the posting's title or main heading."
        " Extract the salary or compensation range if mentioned anywhere in the posting "
        "(look for '$', 'USD', 'per year', 'annually', 'base pay', 'compensation', "
        "'salary range'). If no salary is present, leave the field empty — do not fabricate."
        " Extract ALL US locations for this specific job posting (including 'Remote' if"
        " applicable). If the role is available at multiple US locations, join them with"
        " '; ' (e.g. 'Santa Clara, CA; Austin, TX; Remote'). Ignore sidebar navigation,"
        " job lists, related job suggestions, and 'Other open roles' sections."
        " For 'requirements': extract must-have qualifications regardless of the section heading."
        " Companies use many different labels — treat ALL of the following as requirements:"
        " 'Requirements', 'Required Qualifications', 'Basic Qualifications', 'Minimum Qualifications',"
        " 'Must Have', 'What we need', 'What you need', 'Qualifications', 'What you'll need',"
        " 'You have', 'We're looking for'. Use your judgment if the context implies mandatory skills."
        " For 'additional_qualifications': extract nice-to-have / preferred qualifications."
        " Treat ALL of the following as additional qualifications:"
        " 'Preferred Qualifications', 'Preferred Skills', 'Nice to Have', 'Bonus',"
        " 'Additional Advantage', 'Ways to Stand Out', 'What would be great',"
        " 'Plus', 'Preferred', 'Good to Have', 'Ideally you also have'."
        " If a section is clearly preferred/bonus, classify it as additional_qualifications even if"
        " labeled differently. Leave additional_qualifications empty if none exist."
        " For 'key_responsibilities': extract job duties regardless of heading."
        " Treat 'Responsibilities', 'What you'll do', 'Key Duties', 'Your role', 'The role',"
        " 'What you will do', 'In this role', 'Job duties' as responsibilities."
    )
    if cls == "ai_native":
        sys_instr = (f"Extract structured JD data. Company: {company} is AI-native; "
                     "set is_ai_tpm=true unconditionally." + _common_instr)
    else:
        sys_instr = ("Extract structured JD data. Set is_ai_tpm=true ONLY if the role "
                     "explicitly involves one or more of: AI/ML models (LLM, GenAI, "
                     "foundation models), AI products or platforms, AI/ML infrastructure "
                     "(model serving, MLOps, GPU clusters), compute or cloud infrastructure "
                     "for AI workloads, or chips/semiconductors. "
                     "Be strict — TPM roles in Finance, HR, Legal, Marketing, or general "
                     "software engineering do NOT qualify." + _common_instr)
    sys_instr += SECURITY_CLAUSE  # P0-3: prompt-injection guard
    cfg = types.GenerateContentConfig(
        system_instruction=sys_instr,
        temperature=0.05,
        response_mime_type="application/json",
        response_schema=JobDetails,
    )
    try:
        resp = _KEY_POOL.generate_content(
            model=MODEL,
            contents=(
                f"Company: {company}\n\n"
                f"<scraped_content>\n{markdown}\n</scraped_content>"
            ),
            config=cfg,
        )
        return resp.text
    except Exception as e:
        logging.error(f"[Extract] {e}")
        return "{}"

# ── JD scraper function registry (for routing table lookup) ──────────────────
_JD_FN_REGISTRY = {
    "_scrape_google_jd":         lambda url, **kw: _scrape_google_jd(url, fc_key=kw.get("fc_key", "")),
    "_scrape_workday_jd":        lambda url, **kw: _scrape_workday_jd(url),
    "_scrape_tesla_jd":          lambda url, **kw: _scrape_tesla_jd(url, fc_key=kw.get("fc_key", "")),
    "_scrape_greenhouse_api_jd": None,  # special: needs (board, jid), handled separately
}

# ── Shared scraper routing & JD processing pipeline ──────────────────────────
async def _route_scraper(url: str, fc_key: str, crawler, workday_meta: dict | None = None) -> tuple[str, str]:
    """Route URL to the appropriate scraper.  Returns (markdown, label)."""
    # Priority 1: Workday discovery metadata (no browser fallback)
    if workday_meta is not None and url in workday_meta:
        return _scrape_workday_jd(url), "Workday"

    # Priority 2: Greenhouse-embedded (e.g. CoreWeave ?gh_jid=...), then browser
    gh_board, gh_jid = _extract_gh_jid(url)
    if gh_jid and gh_board:
        markdown = _scrape_greenhouse_api_jd(gh_board, gh_jid)
        if not markdown:
            logging.warning(f"[GH API] Empty JD, falling back to browser: {url}")
            markdown = await scrape_jd(url, crawler)
        return markdown, "GH API"

    # Priority 3: Route via ATS_PLATFORMS declarative table
    match = _match_ats(url)
    if match:
        platform, cfg = match
        jd_fn_name = cfg.get("jd_fn")
        if jd_fn_name and jd_fn_name in _JD_FN_REGISTRY:
            jd_fn = _JD_FN_REGISTRY[jd_fn_name]
            if jd_fn is not None:
                markdown = jd_fn(url, fc_key=fc_key)
                if not markdown:
                    logging.warning(f"[{platform.title()}] Static methods failed, falling back to browser: {url}")
                    markdown = await scrape_jd(url, crawler)
                return markdown, platform.title()

    # Generic: browser crawl
    return await scrape_jd(url, crawler), "generic"


# ── JD field completeness grading (REQ-060) ─────────────────────────────────
_QUALITY_MISSING = {"", "n/a", "none", "not specified", "not available"}


def _assess_jd_quality(jd_data: dict) -> str:
    """Assess JD field completeness and return a quality grade.

    Args:
        jd_data: parsed JD dict from extract_jd (Gemini output).

    Returns:
        "complete" | "partial" | "failed"

    Rules:
        complete — job_title non-empty + location non-empty
                   + key_responsibilities is a non-empty list (>=1 item)
                   + requirements is a non-empty list (>=1 item)
        partial  — job_title non-empty + at least one other key field missing
        failed   — job_title empty, or ALL key fields empty
    """
    title = str(jd_data.get("job_title") or "").strip()
    location = str(jd_data.get("location") or "").strip()
    resp_raw = jd_data.get("key_responsibilities")
    req_raw = jd_data.get("requirements")

    # Normalise list fields: must be a list with at least one non-empty string
    def _has_items(val) -> bool:
        if not isinstance(val, list):
            return False
        return any(str(item).strip() for item in val if item)

    has_title = bool(title) and title.lower() not in _QUALITY_MISSING
    has_loc = bool(location) and location.lower() not in _QUALITY_MISSING
    has_resp = _has_items(resp_raw)
    has_req = _has_items(req_raw)

    if not has_title:
        return "failed"
    if not has_loc and not has_resp and not has_req:
        return "failed"
    if has_loc and has_resp and has_req:
        return "complete"
    return "partial"


async def _process_scraped_jd(
    url: str, markdown: str, company: str, ai_domain: str,
    stale_set: set, known_url_meta: dict,
    pending: list, timestamp_only: list,
    label: str, check_us_location: bool = False,
) -> None:
    """Common pipeline: hash → stale check → cache → extract → validate → stage."""
    hash_val = md5(markdown)
    if url in stale_set and known_url_meta.get(url, {}).get("hash") == hash_val:
        timestamp_only.append(url)
        known_url_meta[url]["age_days"] = 0
        print(f"      ♻️  Unchanged (timestamp-only): {url}")
        return
    _save_md_to_cache(url, markdown)
    await _GEMINI_LIMITER.acquire()
    jd_json = extract_jd(markdown, company=company, ai_domain=ai_domain)
    try:
        parsed = json.loads(jd_json)
    except Exception:
        parsed = {}
    if not parsed.get("company"):
        logging.warning(f"[Extract] Gemini returned empty data for {url}, skipping")
        return
    if check_us_location:
        loc = parsed.get("location", "")
        if loc and not _is_us(loc):
            print(f"      🌍 Skipped non-US ({loc}): {url}")
            return
    # REQ-060: assess JD field completeness and inject into record
    parsed["data_quality"] = _assess_jd_quality(parsed)
    jd_json = json.dumps(parsed)
    _save_structured_jd_md(url, parsed)
    pending.append((url, jd_json, hash_val))
    known_url_meta[url] = {"hash": hash_val, "age_days": 0, "title": ""}
    suffix = f" ({label})" if label != "generic" else ""
    print(f"      ✅ Staged{suffix}: {url}")


# ── Per-company async pipeline ────────────────────────────────────────────────
async def process_company(row: list, known_url_meta: dict, xlsx_path: str,
                           fc_key: str,
                           excel_lock: asyncio.Lock, crawler) -> None:
    if len(row) < 4: return
    name       = str(row[0]).strip() if row[0] else ""
    ai_domain  = str(row[1]).strip() if len(row) > 1 and row[1] else ""
    career_url = str(row[3]).strip() if row[3] else ""
    if not career_url or career_url == "N/A": return
    if not career_url.startswith("http"):
        career_url = "https://" + career_url

    print(f"\n{'─'*55}")
    print(f"🏢  {name}  [{ai_domain}]  ({career_url})")

    # Rate-limit delay for crawler path
    if not any(d in career_url for d in ALL_ATS):
        print("    ⏳ Path B rate-limit delay (15s)...")
        await asyncio.sleep(15)

    raw = await discover_jobs(career_url, fc_key, crawler)
    if not raw:
        print("    No TPM jobs found.")
        return

    if any(d in career_url for d in ALL_ATS):
        raw  = _ai_title_prefilter(raw, ai_domain)
        urls = [l["url"] for l in raw]
        print(f"    ✅ {len(urls)} TPM roles (ATS-trusted, title-filtered).")
    else:
        await _GEMINI_LIMITER.acquire()
        urls = llm_filter_jobs(name, raw)
        if not urls:
            print("    LLM filtered out all URLs.")
            return
        print(f"    LLM identified {len(urls)} AI TPM roles.")

    await asyncio.sleep(4)

    # Build a lookup of prefetched Workday data: url → {title, location}
    workday_meta = {l["url"]: l for l in raw if l.get("_workday")}

    fresh_set = {u for u, m in known_url_meta.items() if m["age_days"] < FRESH_DAYS}
    stale_set = {u for u, m in known_url_meta.items() if m["age_days"] >= FRESH_DAYS}

    to_process = [u for u in urls if u not in fresh_set]
    if not to_process:
        print(f"    All {len(urls)} JDs fresh (< {FRESH_DAYS} days). Skipping.")
        return

    new_urls_list   = [u for u in to_process if u not in stale_set]
    stale_urls_list = [u for u in to_process if u in stale_set]
    print(f"    {len(new_urls_list)} new, {len(stale_urls_list)} stale JDs to process.")

    sem            = asyncio.Semaphore(3)
    pending        = []   # (url, jd_json, hash_val) — collected, written once at end
    timestamp_only = []   # urls where hash unchanged; only timestamp update needed
    seen_urls: set[str] = set()  # BUG-04: guard against duplicate URLs in to_process

    async def fetch_one(url: str) -> None:
        # ── Scrape phase: concurrency-limited by semaphore (I/O bound) ──
        async with sem:
            if url in seen_urls:  # BUG-04: skip duplicate (atomic check+add, no await between)
                return
            seen_urls.add(url)
            print(f"      🕷️  {url}")
            markdown, label = await _route_scraper(url, fc_key, crawler, workday_meta)
        if not markdown:
            tag = "Scrape" if label == "generic" else label
            logging.warning(f"[{tag}] Empty {'markdown' if label == 'generic' else 'JD'} for {url}")
            return
        # ── Gemini phase: rate-limited only (semaphore released, won't block scraping) ──
        await _process_scraped_jd(
            url, markdown, name, ai_domain,
            stale_set, known_url_meta,
            pending, timestamp_only, label,
            check_us_location=(label == "generic"),
        )

    results = await asyncio.gather(*[fetch_one(u) for u in to_process], return_exceptions=True)
    quota_errors = 0
    for res in results:
        if isinstance(res, Exception):
            err_str = str(res)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                quota_errors += 1
            logging.error(f"[FetchOne] Unexpected error: {res}")
    if quota_errors:
        print(f"    ⚠️  {quota_errors} JD(s) skipped: Gemini API quota exhausted across all keys.")

    # Batch-write all collected JDs in a single Excel load→save
    if pending or timestamp_only:
        async with excel_lock:
            try:
                if pending:
                    written = batch_upsert_jd_records(xlsx_path, pending)
                    print(f"    💾 Wrote {written}/{len(pending)} JDs → {xlsx_path}")
                if timestamp_only:
                    updated = batch_update_jd_timestamps(xlsx_path, timestamp_only)
                    print(f"    🕒 Timestamp-only: {updated} JDs")
            except Exception as e:
                logging.error(f"[Excel] Failed to write JDs for {name}: {e}")
                raise
    else:
        print("    ⚠️  No JDs staged (all scraped empty or filtered out).")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global _KEY_POOL
    gemini_keys = [k for k in [
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
    ] if k]
    fc_key = os.getenv("FIRECRAWL_API_KEY")
    if not gemini_keys:
        print("❌ Missing env var: GEMINI_API_KEY")
        return
    if not fc_key:
        print("❌ Missing env var: FIRECRAWL_API_KEY")
        return
    _KEY_POOL = _GeminiKeyPoolBase(gemini_keys, genai_mod=genai)
    logging.info(f"[KeyPool] Loaded {len(gemini_keys)} Gemini API key(s).")

    class _SuppressPageEvalFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "Execution context was destroyed" not in record.getMessage()
    logging.getLogger().addFilter(_SuppressPageEvalFilter())

    print("\n" + "="*60)
    print("JOB AGENT")
    print("="*60 + "\n")

    xlsx_path      = get_or_create_excel()
    companies      = get_company_rows(xlsx_path)
    known_url_meta = get_jd_url_meta(xlsx_path)
    lock           = asyncio.Lock()

    if not companies:
        print("⚠️  No companies in list. Run company_agent.py first.")
        return

    # REQ-063: skip auto-archived companies
    archived_set = get_archived_companies(xlsx_path)
    if archived_set:
        before = len(companies)
        companies = [r for r in companies if str(r[0]).strip() not in archived_set]
        skipped = before - len(companies)
        if skipped:
            logging.info(f"[Archive] Skipped {skipped} auto-archived companies: "
                         f"{sorted(archived_set)}")

    # Split by path for better concurrency control
    path_a = [r for r in companies if len(r) >= 4 and any(d in str(r[3]) for d in ALL_ATS)]
    path_b = [r for r in companies if len(r) >= 4 and not any(d in str(r[3]) for d in ALL_ATS)]
    print(f"Companies: {len(path_a)} Path-A (ATS) | {len(path_b)} Path-B (crawler)")

    from crawl4ai import AsyncWebCrawler, BrowserConfig
    print("🌐 Initializing browser (crawl4ai)...")
    async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
        print("✅ Browser ready.")
        for label, rows, batch in [("Path A", path_a, 5), ("Path B", path_b, 3)]:
            if not rows: continue
            print(f"\n🚀 {label} ({len(rows)} companies, batch={batch})")
            for i in range(0, len(rows), batch):
                chunk = rows[i:i+batch]
                print(f"  Batch {i//batch+1}: {[r[0] for r in chunk]}")
                batch_results = await asyncio.gather(*[
                    process_company(r, known_url_meta, xlsx_path, fc_key, lock, crawler)
                    for r in chunk
                ], return_exceptions=True)
                print(f"  ✅ Batch {i//batch+1} complete.")
                for res in batch_results:
                    if isinstance(res, Exception):
                        logging.error(f"[ProcessCompany] Error: {res}")

        # ── Phase: Retry incomplete JD records ─────────────────────────────────
        incomplete = get_incomplete_jd_rows(xlsx_path)
        if incomplete:
            print(f"\n{'='*60}")
            print(f"🔄 Retrying {len(incomplete)} incomplete JD records...")
            # Build company→ai_domain lookup (case-insensitive)
            company_domain_map = {}
            for r in companies:
                cname = str(r[0]).strip() if r[0] else ""
                cdomain = str(r[1]).strip() if len(r) > 1 and r[1] else ""
                if cname:
                    company_domain_map[cname.lower()] = cdomain

            retry_pending = []
            retry_lock    = asyncio.Lock()

            async def retry_one(rec: dict) -> None:
                url          = rec["url"]
                company_name = rec["company"]
                ai_domain    = company_domain_map.get(company_name.lower(), "")
                print(f"  🔄 {url}")

                markdown, _ = await _route_scraper(url, fc_key, crawler)
                if not markdown:
                    logging.warning(f"[Retry] Empty markdown for {url}")
                    return
                _save_md_to_cache(url, markdown)
                hash_val = md5(markdown)
                await _GEMINI_LIMITER.acquire()
                jd_json  = extract_jd(markdown, company=company_name, ai_domain=ai_domain)
                try:
                    parsed = json.loads(jd_json)
                except Exception:
                    parsed = {}
                if not parsed.get("company"):
                    logging.warning(f"[Retry] Gemini returned empty data for {url}, skipping")
                    return
                # Verify the previously-missing key fields are now populated.
                # If Gemini still returns empty lists, writing "None" would keep the record
                # in _JD_MISSING and cause an infinite retry loop on every run.
                _missing = {"", "n/a", "none", "json error", "not specified", "not available"}
                extracted_loc  = str(parsed.get("location", "")).strip().lower()
                extracted_reqs = parsed.get("requirements", [])
                extracted_resp = parsed.get("key_responsibilities", [])
                if (extracted_loc in _missing
                        and not extracted_reqs
                        and not extracted_resp):
                    logging.warning(f"[Retry] Extraction still incomplete for {url}, skipping overwrite")
                    return
                # REQ-060: assess JD field completeness and inject into record
                parsed["data_quality"] = _assess_jd_quality(parsed)
                jd_json = json.dumps(parsed)
                _save_structured_jd_md(url, parsed)
                async with retry_lock:
                    retry_pending.append((url, jd_json, hash_val))
                print(f"  ✅ Retry staged: {url}")

            # Process retries in batches of 3 to respect rate limits
            sem_retry = asyncio.Semaphore(3)
            async def retry_one_sem(rec):
                async with sem_retry:
                    await retry_one(rec)

            retry_results = await asyncio.gather(*[retry_one_sem(r) for r in incomplete], return_exceptions=True)
            retry_quota_errors = sum(1 for r in retry_results if isinstance(r, Exception) and ("429" in str(r) or "RESOURCE_EXHAUSTED" in str(r)))
            if retry_quota_errors:
                print(f"  ⚠️  Retry: {retry_quota_errors} record(s) skipped due to Gemini API quota exhaustion.")

            if retry_pending:
                written = batch_upsert_jd_records(xlsx_path, retry_pending)
                print(f"  💾 Retry: wrote {written}/{len(retry_pending)} records.")
            else:
                print("  ⚠️  Retry: no records updated.")

    # ── Phase: Update company job-count statistics ────────────────────────────
    print(f"\n{'='*60}")
    print("📊 Computing job counts per company...")
    counts = count_tpm_jobs_by_company(xlsx_path)
    update_company_job_counts(xlsx_path, counts)
    total_tpm    = sum(v["tpm"]    for v in counts.values())
    total_ai_tpm = sum(v["ai_tpm"] for v in counts.values())
    print(f"   Total TPM jobs tracked:    {total_tpm}")
    print(f"   Total AI TPM jobs tracked: {total_ai_tpm}")
    for cname, v in sorted(counts.items(), key=lambda x: -x[1]["ai_tpm"]):
        if v["tpm"] > 0:
            print(f"   {cname}: {v['tpm']} TPM | {v['ai_tpm']} AI TPM")

    # ── Phase: REQ-063 auto-archive companies with no TPM jobs ────────────────
    print(f"\n{'='*60}")
    print("🗄️  Updating archive status...")
    valid_counts = count_valid_tpm_jobs_by_company(xlsx_path)
    archive_info = get_company_archive_info(xlsx_path)
    processed_names = {str(r[0]).strip() for r in companies if r[0]}
    for cname in processed_names:
        has_tpm = valid_counts.get(cname, 0) > 0
        info = archive_info.get(cname, {"no_tpm_count": 0, "archived": ""})
        if has_tpm:
            # Reset counter if it was non-zero
            if info["no_tpm_count"] > 0 or info["archived"] == "yes":
                update_archive_status(xlsx_path, cname, 0, "no")
                logging.info(f"[Archive] {cname}: TPM jobs found, reset counter.")
        else:
            new_count = info["no_tpm_count"] + 1
            if new_count >= AUTO_ARCHIVE_THRESHOLD:
                update_archive_status(xlsx_path, cname, new_count, "yes")
                logging.info(f"[Archive] {cname}: auto-archived (no TPM jobs for "
                             f"{new_count} consecutive runs).")
            else:
                update_archive_status(xlsx_path, cname, new_count, "no")
                logging.info(f"[Archive] {cname}: no TPM jobs ({new_count}/"
                             f"{AUTO_ARCHIVE_THRESHOLD}).")

    print("\n🎉 Job Agent complete.")
    print(f"📊 Results: {xlsx_path}")

if __name__ == "__main__":
    asyncio.run(main())
