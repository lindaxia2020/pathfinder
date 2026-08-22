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
from datetime import datetime, timedelta
from typing import Literal
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.excel_store import (
    EXCEL_PATH, PROJECT_ROOT, get_or_create_excel, get_company_rows,
    get_jd_url_meta, get_triaged_jd_urls, canonical_jd_url, batch_update_jd_timestamps,
    get_jd_urls, upsert_jd_record, batch_upsert_jd_records,
    get_incomplete_jd_rows, count_tpm_jobs_by_company, update_company_job_counts,
    sort_jd_tracker_by_tier,
    JOB_DOMAIN_VALUES, classify_region, compute_freshness_tier,
)

from shared.gemini_pool import _GeminiKeyPoolBase
from shared.firecrawl_pool import build_pool_from_env
from shared.rate_limiter import _RateLimiter
from shared.config import MODEL, JD_CACHE_DIR
from shared.prompts import SECURITY_CLAUSE
from shared.run_summary import RunSummary
from shared.exceptions import GeminiTransientError


# BUG-31: use _GeminiKeyPoolBase directly with genai_mod parameter
_GeminiKeyPool = _GeminiKeyPoolBase  # alias for backward compat (tests)

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

FRESH_DAYS = 5

# ── JD Markdown cache helpers ──────────────────────────────────────────────────
# JD_CACHE_DIR sourced from shared.config (P1-5).

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
    ats_kws  = jd_dict.get("ats_keywords", []) or []

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

    # PRJ-002 Phase 4: persist Gemini-extracted ATS keywords here too so the
    # structured.md is self-contained and human-readable. Excel JD_Tracker
    # remains the canonical source for match_agent / resume_optimizer reads.
    if ats_kws:
        lines.append("## ATS Keywords")
        lines.extend(f"- {k}" for k in ats_kws)
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# ── Rate limiters ─────────────────────────────────────────────────────────────
_GEMINI_LIMITER = _RateLimiter(rpm=10)  # conservative: 15 RPM hard limit

_KEY_POOL: "_GeminiKeyPool | None" = None  # initialised in main()
_FC_POOL = None  # BUG-68: FirecrawlKeyPool, initialised in main()
_FC_MAP_LIMITER  = _RateLimiter(rpm=1)  # hard limit: 1 crawl/min (Firecrawl map)

# ── HTTP retry helper (audit P1: ATS API silent failures) ─────────────────────
# Transient 5xx and 429 used to return [] on first attempt with no retry,
# making "API down" indistinguishable from "zero jobs." Exponential backoff
# at 0.5/1.5/3.5s — total worst case ~5.5s per slug, acceptable for our
# ~150 companies × weekly cadence.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_BASE_SLEEP_SECS = 0.5  # tests can monkey-patch to 0 to skip backoff

def _http_request_with_retry(method: str, url: str, *, attempts: int = 3,
                             timeout: int = 12, **kwargs) -> "requests.Response | None":
    """Issue an HTTP request with exponential backoff on transient failures.

    Returns the final Response on 2xx/4xx (caller decides), None if all
    attempts hit a transient error or exception. Logs every retry with
    URL + status + attempt number so silent failures become visible.

    Dispatches to requests.get / requests.post (rather than requests.request)
    so existing test patches that mock those names continue to work.
    """
    method_upper = method.upper()
    if method_upper == "GET":
        fn = requests.get
    elif method_upper == "POST":
        fn = requests.post
    else:
        fn = lambda u, **kw: requests.request(method_upper, u, **kw)  # noqa: E731
    last_status: int | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = fn(url, timeout=timeout, **kwargs)
            if r.status_code in _RETRY_STATUSES and attempt < attempts:
                wait = _RETRY_BASE_SLEEP_SECS * (2 ** (attempt - 1))
                logging.warning(
                    f"[HTTP retry] {method} {url} → {r.status_code} "
                    f"(attempt {attempt}/{attempts}); sleeping {wait:.1f}s"
                )
                time.sleep(wait)
                last_status = r.status_code
                continue
            return r
        except Exception as e:
            # Broad except: network failures bubble up as requests.RequestException,
            # builtin ConnectionError/TimeoutError, urllib3.* errors, or socket.timeout
            # depending on transport layer. Distinguishing them at the call site has
            # no value — all are "retry-able transient HTTP failure."
            if attempt < attempts:
                wait = _RETRY_BASE_SLEEP_SECS * (2 ** (attempt - 1))
                logging.warning(
                    f"[HTTP retry] {method} {url} → {type(e).__name__}: {e} "
                    f"(attempt {attempt}/{attempts}); sleeping {wait:.1f}s"
                )
                time.sleep(wait)
                continue
            logging.error(f"[HTTP] {method} {url} failed after {attempts} attempts: "
                          f"{type(e).__name__}: {e}")
            return None
    if last_status is not None:
        logging.error(f"[HTTP] {method} {url} gave up after {attempts} attempts; last status={last_status}")
    return None


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
    # PRJ-002 / REQ-100: 8-15 high-signal keywords that an ATS scanner / recruiter
    # search would key on. Default empty so cached JDs without this field round-trip.
    ats_keywords:             list[str] = Field(default_factory=list)
    # PRJ-004 REQ-004-09: 5-track domain classifier replaces is_ai_tpm.
    # "None" = mid-large-tech role matching no track → skipped at write time.
    job_domain:               Literal["AI", "Robotics", "Fintech", "Space",
                                      "Defense", "None"] = "None"
    # PRJ-004 REQ-004-08: minimum stated years of experience; None if unstated.
    min_yoe:                  int | None = None
    # PRJ-004 REQ-004-11: work-authorization screen result.
    work_auth:                Literal["citizenship_required", "clearance_required",
                                      "us_person_ok", "none_stated"] = "none_stated"
    # PRJ-004 REQ-004-10: NOT LLM-extracted — injected by code from ATS list
    # metadata; declared here so cached JDs round-trip through the schema.
    posted_date:              str = ""
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
    "workable": {
        "domains":            ["workable.com", "apply.workable.com"],
        "board_url_template": "https://apply.workable.com/{slug}/",
        "slug_pattern":       r"workable\.com/([^/?#]+)",
        "strategy":           "json_api",
        "list_fn":            "_fetch_workable_jobs",
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
    # PRJ-004 REQ-004-17 (G6b): Amazon.jobs public JSON search endpoint —
    # replaces the chronically-failing generic browser-crawl path. search.json
    # returns full JD text, so candidates carry _prefetched_md and never touch
    # Firecrawl or the crawler. Same undocumented-API risk class as Workday/
    # Ashby (R-09); adapter failure falls back to the crawler path as before.
    "amazon": {
        "domains":            ["amazon.jobs"],
        "board_url_template": None,
        "slug_pattern":       r"(amazon\.jobs)",
        "strategy":           "json_api",
        "list_fn":            "_fetch_amazon_jobs",
        "jd_fn":              None,
    },
    "google": {
        # T16 / REQ-004-18: discovery via the server-rendered careers search
        # page (AF_initDataCallback payload) — see _fetch_google_jobs for the
        # documented deviation from design.md's dead v3 API endpoint.
        "domains":            ["google.com/about/careers", "careers.google.com"],
        "jd_domains":         ["careers.google.com", "google.com/about/careers"],
        "board_url_template": None,
        "slug_pattern":       None,
        "strategy":           "json_api",
        "list_fn":            "_fetch_google_jobs",
        "jd_fn":              "_scrape_google_jd",
    },
    "microsoft": {
        # Microsoft careers is a JS SPA at jobs.careers.microsoft.com (current)
        # and careers.microsoft.com (legacy). Like Google, Crawl4AI captures
        # the sidebar list which contaminates Gemini extraction. Firecrawl with
        # only_main_content=True isolates the actual JD body.
        "domains":            [],
        "jd_domains":         ["jobs.careers.microsoft.com", "careers.microsoft.com"],
        "board_url_template": None,
        "slug_pattern":       None,
        "strategy":           "custom",
        "list_fn":            None,
        "jd_fn":              "_scrape_microsoft_jd",
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
    "workable":   "query=Technical+Program+Manager",
    "workday":    "q=Technical+Program+Manager",
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

# PRJ-004 REQ-004-09: Track → job_domain mapping for vertical-bucket
# companies. Every TPM role at a vertical company qualifies with the
# company's own track domain; mid-large-tech roles get per-JD judgment.
_TRACK_TO_DOMAIN = {"AI-native": "AI", "Robotics": "Robotics",
                    "Fintech": "Fintech", "Space": "Space", "Defense": "Defense"}
VERTICAL_TRACKS = frozenset(_TRACK_TO_DOMAIN)


def _vertical_domain(track: str) -> str | None:
    """Return the forced job_domain for a vertical-track company, or None for
    mid-large-tech / unknown values (per-JD classifier path).

    Unknown/unmigrated Track values route through the strict mid-large path
    with a logged warning (D-17) — never silently coerced, never skipped.
    A blank value falls back to the legacy name-based _classify() so manual
    rows without a Track still behave sensibly.
    """
    t = (track or "").strip()
    if t in VERTICAL_TRACKS:
        return _TRACK_TO_DOMAIN[t]
    if t and t not in ("Mid-large Tech", "N/A"):
        logging.warning(f"[Track] unmigrated/unknown Track value {t!r} — "
                        "treating as Mid-large Tech (strict per-JD classification). "
                        "Run `python agents/company_agent.py --migrate-tracks`.")
    return None

TPM_KW = ["technical program manager", "tpm", "technical program mgr",
          "tech program manager",
          # 2026-08-19 widening: many companies title the same role
          # "Technical Project Manager" — accepted on every track.
          "technical project manager", "technical project mgr",
          "tech project manager"]

# 2026-08-19 widening: at these vertical tracks a plain "Program Manager"
# title is usually a technical program role (small orgs, loose titling), so
# it passes the title filter. Fintech and Mid-large Tech keep the strict
# TPM-title rule — their generic-PM population is largely non-technical.
PM_TITLE_OK_TRACKS = frozenset({"AI-native", "Robotics", "Space", "Defense"})

# ── ATS API fetch (Path A: Greenhouse / Lever) ────────────────────────────────
def _parse_iso_date(value) -> str:
    """PRJ-004 REQ-004-10: normalize an ATS date field to 'YYYY-MM-DD'.
    Accepts ISO strings (Greenhouse updated_at, Ashby publishedDate) and
    epoch-milliseconds (Lever createdAt). Unparseable → '' (unknown-date
    keep+flag path, never treated as aged)."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, (int, float)):  # epoch ms (Lever)
            return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
        s = str(value).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s[:10]
        if s.isdigit():  # epoch ms as string
            return datetime.fromtimestamp(int(s) / 1000).strftime("%Y-%m-%d")
        # Amazon.jobs style: "July 1, 2026"
        return datetime.strptime(s, "%B %d, %Y").strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        pass
    return ""


_ATS_FETCH = {
    "greenhouse.io": {
        "slug_pattern":  r"greenhouse\.io/([^/?#]+)",
        "api_template":  "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "jobs_key":      "jobs",
        "title_field":   "title",
        "url_field":     "absolute_url",
        "id_field":      "id",
        "id_url":        "https://job-boards.greenhouse.io/{slug}/jobs/{id}",
        "date_field":    "updated_at",       # PRJ-004 REQ-004-10
    },
    "lever.co": {
        "slug_pattern":  r"lever\.co/([^/?#]+)",
        "api_template":  "https://api.lever.co/v0/postings/{slug}?mode=json",
        "jobs_key":      None,
        "title_field":   "text",
        "url_field":     "hostedUrl",
        "id_field":      "id",
        "id_url":        "https://jobs.lever.co/{slug}/{id}",
        "date_field":    "createdAt",        # epoch-ms
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
    r = _http_request_with_retry("GET", api_url, timeout=10,
                                 headers={"User-Agent": "PathFinder/1.0"})
    if r is None:
        return []
    if r.status_code != 200:
        logging.warning(f"[ATS API] {r.status_code} for {api_url}")
        return []
    try:
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
                results.append({"url": job_url, "title": title, "location": loc.strip(),
                                "posted_date": _parse_iso_date(job.get(cfg["date_field"]))})
        logging.info(f"[ATS API] {len(results)} jobs for {slug}")
        return results
    except (ValueError, KeyError, TypeError) as e:
        logging.error(f"[ATS API] parse error for {api_url}: {type(e).__name__}: {e}")
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
    r = _http_request_with_retry("GET", api_url, timeout=10,
                                 headers={"User-Agent": "PathFinder/1.0"})
    if r is None:
        return []
    if r.status_code != 200:
        logging.warning(f"[Ashby API] {r.status_code} for {api_url}")
        return []
    try:
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
                results.append({"url": job_url, "title": title, "location": loc,
                                "posted_date": _parse_iso_date(
                                    job.get("publishedDate") or job.get("publishedAt"))})
        logging.info(f"[Ashby API] {len(results)} jobs for {slug}")
        return results
    except (ValueError, KeyError, TypeError) as e:
        logging.error(f"[Ashby API] parse error for {api_url}: {type(e).__name__}: {e}")
        return []

def _format_workable_location(job: dict) -> str:
    """Build a location string from Workable's multi-field shape.

    Workable returns city / state / country plus an optional locations[] array
    for multi-location postings, and a `telecommuting` bool for remote roles.
    Multiple segments are joined with "; " so classify_region's multi-segment
    parsing works.
    """
    parts: list[str] = []
    if job.get("telecommuting"):
        parts.append("Remote")
    locs = job.get("locations") or []
    if locs:
        for loc in locs:
            seg = ", ".join(s for s in (loc.get("city"), loc.get("region"), loc.get("country")) if s)
            if seg:
                parts.append(seg)
    else:
        seg = ", ".join(s for s in (job.get("city"), job.get("state"), job.get("country")) if s)
        if seg:
            parts.append(seg)
    return "; ".join(parts)

def _fetch_workable_jobs(career_url: str) -> list:
    """Fetch jobs from Workable's public widget API.

    Endpoint: GET https://apply.workable.com/api/v1/widget/accounts/{slug}
    Returns structured data matching Greenhouse/Lever/Ashby format:
        [{"url": ..., "title": ..., "location": ...}, ...]
    """
    m = re.search(r"workable\.com/([^/?#]+)", career_url)
    if not m:
        logging.warning(f"[Workable API] Cannot extract slug from {career_url}")
        return []
    slug = m.group(1).split('/')[0]
    api_url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    logging.info(f"[Workable API] GET {api_url}")
    r = _http_request_with_retry("GET", api_url, timeout=10,
                                 headers={"User-Agent": "PathFinder/1.0"})
    if r is None:
        return []
    if r.status_code != 200:
        logging.warning(f"[Workable API] {r.status_code} for {api_url}")
        return []
    try:
        data = r.json()
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        results = []
        for job in jobs:
            title = (job.get("title") or "").strip()
            shortcode = (job.get("shortcode") or "").strip()
            job_url = (job.get("url") or job.get("shortlink") or "").strip()
            if not job_url and shortcode:
                job_url = f"https://apply.workable.com/j/{shortcode}"
            location = _format_workable_location(job)
            if job_url and title:
                results.append({"url": job_url, "title": title, "location": location,
                                "posted_date": _parse_iso_date(job.get("published_on"))})
        logging.info(f"[Workable API] {len(results)} jobs for {slug}")
        return results
    except (ValueError, KeyError, TypeError) as e:
        logging.error(f"[Workable API] parse error for {api_url}: {type(e).__name__}: {e}")
        return []

# BUG-72: student roles are never in scope (user is a 10-yr professional).
# Filtered by title pre-scrape (_tpm_filter) and at write time (_gate_and_finalize).
_INTERN_TITLE_RE = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|\bnew\s+grad(uate)?\b", re.I)

def _geo_out_of_scope(location: str, track: str = "") -> bool:
    """REQ-161/162 geo keep-set, shared by _tpm_filter and _gate_and_finalize:
    WA / CA / TX / US-Remote on every track; FL additionally kept on the
    Space track only (Space Coast hiring). "Unknown" always keeps
    (conservative — only confirmed out-of-region rows drop)."""
    region = classify_region(location)
    if region == "FL":
        return (track or "").strip() != "Space"
    return region == "Other"

def _tpm_filter(links: list, track: str = "") -> list:
    # 2026-08-19 widening: PM_TITLE_OK_TRACKS additionally accept plain
    # "Program Manager" titles (substring also covers "Technical Program
    # Manager"); all other tracks stay on the strict TPM_KW list.
    title_kws = list(TPM_KW)
    if (track or "").strip() in PM_TITLE_OK_TRACKS:
        title_kws.append("program manager")
    filtered = []
    skipped  = 0
    interns  = 0
    for lnk in links:
        if not any(kw in lnk.get("title","").lower() for kw in title_kws):
            continue
        if _INTERN_TITLE_RE.search(lnk.get("title", "")):
            interns += 1; continue
        # PRJ-004 REQ-004-12: tightened geo — keep-set per _geo_out_of_scope
        # (WA/CA/TX/US-Remote; +FL on Space track). "Unknown"
        # (blank/placeholder) keeps, conservative as before; dropped rows are
        # logged for early post-launch spot checks (R-10 mitigation).
        loc = lnk.get("location", "")
        if _geo_out_of_scope(loc, track):
            logging.info(f"[GeoFilter] Dropped out-of-region job "
                         f"({loc!r}): {lnk.get('title', '?')}")
            skipped += 1; continue
        filtered.append(lnk)
    if skipped:
        print(f"    [GeoFilter] skipped {skipped} out-of-region jobs (kept: WA/CA/TX/US-Remote, +FL for Space).")
    if interns:
        print(f"    [InternFilter] skipped {interns} intern/co-op/new-grad roles.")
    return filtered

# Title keywords that flag clearly non-track TPM roles at mid-large-tech
# companies (pre-Gemini cost saver). PRJ-004: "operations" removed from the
# blocklist — space/defense "Mission Operations TPM" is a legitimate track role.
_TITLE_BLOCK_KW = frozenset([
    "finance", "legal", "hr", "human resources", "supply chain",
    "logistics", "retail", "marketing", "sales",
    "recruiting", "facilities", "real estate", "accounting",
    "tax", "treasury", "procurement", "customer success",
    "sox", "compliance", "audit", "governance", "grc",
])

def _nontech_title_prefilter(links: list, track: str) -> list:
    """Block TPM roles with explicitly non-technical domain keywords in the
    title. Applied only on the mid-large-tech path (incl. unmigrated values —
    the strict path per D-17); vertical-track companies pass all through
    because every TPM role there is domain-qualified (REQ-004-09)."""
    if (track or "").strip() in VERTICAL_TRACKS:
        return links
    filtered, blocked = [], 0
    for lnk in links:
        if any(kw in lnk.get("title", "").lower() for kw in _TITLE_BLOCK_KW):
            blocked += 1
            continue
        filtered.append(lnk)
    if blocked:
        print(f"    Non-tech title pre-filter: removed {blocked} out-of-track TPM roles.")
    return filtered

# ── Path W: Workday JSON API ──────────────────────────────────────────────────
def _parse_workday_posted_on(text: str, today=None) -> str:
    """PRJ-004 REQ-004-10: deterministic parse of Workday's relative postedOn
    strings ("Posted Today", "Posted 2 Days Ago", "Posted 30+ Days Ago") into
    an ISO date string. "30+" maps to 31 days back, which correctly fails the
    15-day freshness gate. Unparseable/blank text → "" (unknown date — the
    keep+flag path, never treated as aged)."""
    if not text:
        return ""
    t = str(text).strip().lower()
    today = today or datetime.now().date()
    if "today" in t or "just posted" in t or "just now" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)(\+?)\s*days?\s*ago", t)
        if not m:
            return ""
        days = int(m.group(1)) + (1 if m.group(2) else 0)
    return (today - timedelta(days=days)).strftime("%Y-%m-%d")


def _fetch_workday_jobs(career_url: str) -> list:
    """
    Workday exposes an undocumented but widely-used POST JSON API.
    URL pattern: https://{company}[.wd5].myworkdayjobs.com/{site}
    API endpoint: https://{company}[.wd5].myworkdayjobs.com/wday/cxs/{company}/{site}/jobs
    Supports both standard (company.wd5.myworkdayjobs.com) and
    no-wd-prefix (company.myworkdayjobs.com) URL formats.
    """
    m = re.match(r"https://([^.]+)(?:\.wd\d+)?\.myworkdayjobs\.com/([^?#]*)", career_url)
    if not m:
        logging.warning(f"[Workday] Cannot parse slug from {career_url}")
        return []
    company_slug = m.group(1)
    path_segs    = [seg for seg in m.group(2).split('/') if seg]
    # BUG-80: "/en-US/<site>" URLs carry a locale segment that is NOT part of
    # the site slug — using it produced a CXS 404 (zero postings) for 13 of
    # 28 Workday rows. Skip it; the site is the next path segment.
    if path_segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", path_segs[0]):
        path_segs = path_segs[1:]
    if not path_segs:
        logging.warning(f"[Workday] Cannot parse site slug from {career_url}")
        return []
    site_slug    = path_segs[0]   # keep only the site name, drop sub-paths

    # Preserve the original host (with or without wd prefix)
    host_m = re.match(r"(https://[^/]+)", career_url)
    host   = host_m.group(1)

    api_url = f"{host}/wday/cxs/{company_slug}/{site_slug}/jobs"
    base    = f"{host}/{site_slug}"
    # PRJ-004 REQ-004-13: paginate instead of the old hardcoded limit:20 —
    # no artificial cap on scraped job count. The page-count guard is a
    # runaway/corruption backstop (5,000 postings), not a result cap.
    # BUG-64 (found at G6a live check): Workday CXS rejects limit>20 with
    # HTTP 400 — 20 is the API's hard page-size cap, so we paginate at 20.
    _PAGE_SIZE, _MAX_PAGES = 20, 250
    results, offset = [], 0
    total = None  # BUG-81: CXS reports `total` on page 0 only (0 afterwards)
    for page in range(_MAX_PAGES):
        # 2026-08-19 widening: search "Program Manager" (superset — also
        # surfaces Technical Project Manager and plain PM titles); precision
        # is restored client-side by _tpm_filter's track-aware keyword list.
        payload = {"limit": _PAGE_SIZE, "offset": offset,
                   "searchText": "Program Manager"}
        logging.info(f"[Workday API] POST {api_url} offset={offset}")
        r = _http_request_with_retry(
            "POST", api_url, timeout=12, json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "PathFinder/1.0"},
        )
        if r is None:
            break
        if r.status_code != 200:
            logging.warning(f"[Workday API] {r.status_code} for {api_url}")
            break
        try:
            data     = r.json()
            postings = data.get("jobPostings", [])
            # BUG-81: only the first page carries the real total — later pages
            # return total=0, which made `offset >= total` stop every Workday
            # fetch at 40 postings (Salesforce: 422 matches, 40 examined).
            if total is None and isinstance(data.get("total"), int) and data.get("total") > 0:
                total = data.get("total")
        except (ValueError, KeyError, TypeError) as e:
            logging.error(f"[Workday API] parse error for {api_url}: {type(e).__name__}: {e}")
            break
        for p in postings:
            path  = p.get("externalPath", "")
            title = (p.get("title", "") or "").strip()
            loc   = p.get("locationsText", "") or ""
            # For "N Locations", fall back to the country/city in the URL path
            # e.g. /job/Israel-Yokneam/... → "Israel-Yokneam"
            if re.match(r'^\d+\s+location', loc.lower()):
                url_loc_m = re.search(r'/job/([^/]+)/', path)
                if url_loc_m:
                    loc = url_loc_m.group(1).replace('-', ', ')
            if path and title:
                results.append({"url": base + path, "title": title, "location": loc,
                                 "posted_date": _parse_workday_posted_on(p.get("postedOn", "")),
                                 "_workday": True})
        offset += _PAGE_SIZE
        if len(postings) < _PAGE_SIZE:
            break
        if isinstance(total, int) and offset >= total:
            break
    else:
        logging.error(f"[Workday API] hit {_MAX_PAGES}-page runaway guard for "
                      f"{company_slug}/{site_slug} — response likely corrupt")
    logging.info(f"[Workday API] {len(results)} postings for {company_slug}/{site_slug}")
    return results

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
        if not res.success:
            logging.warning(f"[Crawl4AI] render failed for {url}: success=False")
            return []
        out = []
        if hasattr(res, "links") and isinstance(res.links, dict):
            for lnk in res.links.get("internal",[]) + res.links.get("external",[]):
                href = lnk.get("href","") or lnk.get("url","")
                text = lnk.get("text","") or lnk.get("title","")
                if href and href.startswith("http"):
                    out.append({"url": href.strip(), "title": text.strip()})
        return out
    except Exception as e:
        logging.error(f"[Crawl4AI] {url}: {type(e).__name__}: {e}")
        return []

async def _crawl_ats_board(board_url: str, platform: str, crawler) -> list:
    param = ATS_SEARCH_PARAM.get(platform, "")
    url   = f"{board_url}?{param}" if param else board_url
    return await _crawl_page(url, crawler)

def _firecrawl_map(career_url: str) -> list:
    # BUG-68: quota/credit errors (402/429) are handled inside the key pool —
    # rotation first, then a run-wide skip with one visible warning. Only
    # non-quota transients (network blips, timeouts) reach the retry loop.
    if _FC_POOL is None or _FC_POOL.exhausted:
        return []
    for attempt in range(3):
        try:
            logging.info(f"[Firecrawl] map attempt {attempt + 1}/3: {career_url}")
            # PRJ-004 REQ-004-13: no artificial cap on mapped URLs.
            # 2026-08-19 widening: "Program Manager" superset query; the
            # LLM filter / _merge_filter downstream keep precision.
            res = _FC_POOL.map(url=career_url, search="Program Manager")
            if res is None:  # pool exhausted mid-call
                return []
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
            logging.error(f"[Firecrawl] attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                sleep_s = 5 * (attempt + 1)
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

def _fetch_amazon_jobs(career_url: str) -> list:
    """PRJ-004 REQ-004-17 (G6b): fetch TPM jobs from Amazon.jobs' public JSON
    search endpoint, paginated (no artificial cap, REQ-004-13). search.json
    returns titles, locations, posting dates AND full JD text, so each
    candidate carries `_prefetched_md` — zero browser/Firecrawl calls and no
    generic-crawler fallback for Amazon JDs."""
    results, offset = [], 0
    _PAGE, _MAX_PAGES = 100, 50  # runaway guard: 5,000 postings
    for _page in range(_MAX_PAGES):
        # 2026-08-19 title widening: query deliberately NOT broadened to
        # "program manager" — Amazon is Mid-large Tech (generic PM titles
        # stay out of scope) and a broader query would push real TPM rows
        # past the 5,000-posting runaway guard. base_query is fuzzy, so
        # "Technical Project Manager" titles still surface.
        api_url = ("https://www.amazon.jobs/en/search.json"
                   "?base_query=technical+program+manager&country=USA"
                   f"&result_limit={_PAGE}&offset={offset}")
        logging.info(f"[Amazon API] GET offset={offset}")
        r = _http_request_with_retry("GET", api_url, timeout=12,
                                     headers={"User-Agent": "PathFinder/1.0"})
        if r is None:
            break
        if r.status_code != 200:
            logging.warning(f"[Amazon API] {r.status_code} for {api_url}")
            break
        try:
            data  = r.json()
            jobs  = data.get("jobs", [])
            total = data.get("hits")
        except (ValueError, KeyError, TypeError) as e:
            logging.error(f"[Amazon API] parse error: {type(e).__name__}: {e}")
            break
        for job in jobs:
            title = (job.get("title") or "").strip()
            path  = (job.get("job_path") or "").strip()
            url   = f"https://www.amazon.jobs{path}" if path else ""
            loc   = job.get("normalized_location") or job.get("location") or ""
            if isinstance(loc, list):
                loc = "; ".join(str(x) for x in loc)
            if not (url and title):
                continue
            md_parts = [f"# {title}", "**Company:** Amazon",
                        f"**Location:** {loc}"]
            for label, key in (("Description", "description"),
                               ("Basic Qualifications", "basic_qualifications"),
                               ("Preferred Qualifications", "preferred_qualifications")):
                v = job.get(key)
                if v:
                    md_parts.append(f"## {label}\n{v}")
            results.append({"url": url, "title": title, "location": str(loc),
                            "posted_date": _parse_iso_date(job.get("posted_date")),
                            "_prefetched_md": "\n\n".join(md_parts),
                            "_platform": "Amazon"})
        offset += _PAGE
        if len(jobs) < _PAGE:
            break
        if isinstance(total, int) and offset >= total:
            break
    logging.info(f"[Amazon API] {len(results)} postings total")
    return results


def _fetch_google_jobs(career_url: str) -> list:
    """T16 / REQ-004-18: fetch TPM jobs from Google Careers.

    Design deviation from design.md §T16: the documented
    careers.google.com/api/v3/search/ endpoint is dead (404, verified
    2026-07-09). Instead, the search results page at
    google.com/about/careers/applications/jobs/results is SERVER-RENDERED —
    plain requests, no JS — and embeds an AF_initDataCallback JSON payload
    with, per job: id[0], title[1], responsibilities-HTML[3], min-quals[4],
    preferred-quals[19], location list[9] (display strings with state +
    country), and publish epoch-seconds [12][0]. 20 jobs/page via page=N.

    Like Amazon (REQ-004-17), each candidate carries `_prefetched_md` +
    `posted_date` — zero browser/Firecrawl calls for Google JDs. The payload
    is undocumented and index-based, so every job is parsed defensively; a
    total parse failure falls back to href-regex URL harvesting, and an HTTP
    failure returns [] (existing crawler fallback takes over).

    Note: Google clamps page=N to the last page (repeats its results), so
    pagination stops when a page yields no NEW job ids, not on an empty page.
    """
    # 2026-08-19 title widening: exact-phrase query deliberately kept —
    # Google is Mid-large Tech (generic PM out of scope) and titles TPM
    # roles "Technical Program Manager" consistently; unquoting would
    # multiply result pages for no recall gain.
    _BASE = ("https://www.google.com/about/careers/applications/jobs/results"
             "?q=%22technical+program+manager%22&location=United+States")
    _HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"}
    _MAX_PAGES = 25  # runaway guard: 500 postings
    results, seen_ids = [], set()
    html_pages = []

    def _strip_html(fragment) -> str:
        # payload HTML fields are [None, "<html>"] pairs
        if isinstance(fragment, list):
            fragment = next((x for x in fragment if isinstance(x, str)), "")
        text = re.sub(r"<[^>]+>", " ", fragment or "")
        text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
        return re.sub(r"[ \t]+", " ", text).strip()

    for page in range(1, _MAX_PAGES + 1):
        logging.info(f"[Google Careers] GET page={page}")
        r = _http_request_with_retry("GET", f"{_BASE}&page={page}",
                                     timeout=15, headers=_HEADERS)
        if r is None or r.status_code != 200:
            if r is not None:
                logging.warning(f"[Google Careers] HTTP {r.status_code} on page {page}")
            break
        html_pages.append(r.text)
        try:
            blocks = re.findall(r"AF_initDataCallback\((\{.*?\})\);",
                                r.text, re.DOTALL)
            if not blocks:
                break
            payload_src = max(blocks, key=len)
            m = re.search(r"data:\s*", payload_src)
            payload, _ = json.JSONDecoder().raw_decode(payload_src[m.end():])
            jobs = payload[0] or []
        except Exception as e:
            logging.error(f"[Google Careers] payload parse failed on page {page}: "
                          f"{type(e).__name__}: {e}")
            break
        new_this_page = 0
        for job in jobs:
            try:
                job_id = str(job[0] or "").strip()
                title  = str(job[1] or "").strip()
                if not job_id or not title or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                new_this_page += 1
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                url  = (f"https://www.google.com/about/careers/applications/"
                        f"jobs/results/{job_id}-{slug}")
                loc_entries = job[9] or []
                location = "; ".join(str(l[0]) for l in loc_entries
                                     if isinstance(l, list) and l and l[0])
                posted = ""
                ts = job[12]
                if isinstance(ts, list) and ts and isinstance(ts[0], (int, float)):
                    posted = datetime.fromtimestamp(ts[0]).strftime("%Y-%m-%d")
                md_parts = [f"# {title}", "**Company:** Google",
                            f"**Location:** {location}"]
                for label, idx in (("Minimum Qualifications", 4),
                                   ("Preferred Qualifications", 19),
                                   ("Responsibilities", 3)):
                    v = _strip_html(job[idx]) if len(job) > idx else ""
                    if v:
                        md_parts.append(f"## {label}\n{v}")
                results.append({"url": url, "title": title,
                                "location": location, "posted_date": posted,
                                "_prefetched_md": "\n\n".join(md_parts),
                                "_platform": "Google"})
            except Exception as e:
                logging.warning(f"[Google Careers] skipped one malformed job on "
                                f"page {page}: {type(e).__name__}: {e}")
        if new_this_page == 0:
            break  # page=N clamps to the last page — no new ids means done
    if not results and html_pages:
        # Secondary fallback: harvest job URLs from hrefs so discovery still
        # yields candidates even if the payload layout shifts.
        hrefs = set()
        for html in html_pages:
            hrefs.update(re.findall(r"jobs/results/(\d+[a-z0-9-]*)", html))
        results = [{"url": f"https://www.google.com/about/careers/applications/jobs/results/{h}",
                    "title": h.split("-", 1)[1].replace("-", " ") if "-" in h else "",
                    "location": "", "posted_date": ""}
                   for h in sorted(hrefs)]
        logging.warning(f"[Google Careers] payload unusable — href fallback "
                        f"yielded {len(results)} candidates")
    logging.info(f"[Google Careers] {len(results)} postings total")
    return results


# ── Job discovery helpers (routing table lookup) ─────────────────────────────
def _resolve_list_fn(fn_name: str):
    """Resolve a list_fn name string to the actual callable."""
    return {
        "_fetch_ats_jobs":      _fetch_ats_jobs,
        "_fetch_ashby_jobs":    _fetch_ashby_jobs,
        "_fetch_workable_jobs": _fetch_workable_jobs,
        "_fetch_workday_jobs":  _fetch_workday_jobs,
        "_fetch_amazon_jobs":   _fetch_amazon_jobs,
        "_fetch_google_jobs":   _fetch_google_jobs,
    }[fn_name]


async def _discover_via_api(career_url: str, platform: str, cfg: dict, crawler,
                            track: str = "") -> list | None:
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
            cands = _tpm_filter(jobs, track)
            print(f"    → {len(jobs)} total, {len(cands)} TPM candidates")
            return cands
        print("    → API failed, falling back to crawler")
        return None  # Signal: fall through to crawler path

    if strategy == "internal_api":
        # Workday: API → filter → crawler fallback
        print("    → Path W: Workday JSON API...")
        jobs = list_fn(career_url)
        if jobs:
            cands = _tpm_filter(jobs, track)
            print(f"    → {len(jobs)} total, {len(cands)} TPM candidates")
            if cands:
                return cands
            print("    → Workday API: no TPM matches in API results, falling back to crawler")
        else:
            print("    → Workday API failed, falling back to crawler")
        links = await _crawl_page(career_url, crawler)
        cands = _tpm_filter(links, track)
        print(f"    → Crawled {len(links)} links, {len(cands)} TPM candidates")
        return cands

    return None


# ── Job discovery router ──────────────────────────────────────────────────────
async def discover_jobs(career_url: str, crawler, track: str = "") -> list:
    # 1. Direct URL match against routing table
    match = _match_ats(career_url)
    if match:
        platform, cfg = match
        result = await _discover_via_api(career_url, platform, cfg, crawler, track)
        if result is not None:
            return result
        # json_api fallback: API returned empty, fall through to crawler path below

    # 2. Company website — Firecrawl (rate-limited) + Crawl4AI, then detect ATS
    print("    → Path B: crawling career page...")
    print("    ⏳ Waiting for Firecrawl rate limiter (1 RPM)...")
    await _FC_MAP_LIMITER.acquire()
    print(f"    🌐 Firecrawl map: {career_url}")
    fc_links  = await asyncio.to_thread(_firecrawl_map, career_url)
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
                    cands = _tpm_filter(jobs, track)
                    print(f"    → {len(jobs)} total, {len(cands)} TPM candidates")
                    return cands
        links = await _crawl_ats_board(board_url, platform, crawler)
        cands = _tpm_filter(links, track)
        print(f"    → Crawled {len(links)} links, {len(cands)} TPM candidates")
        return cands

    merged = _merge_filter([fc_links + c4_links])
    print(f"    → No ATS. Firecrawl={len(fc_links)}, Crawl4AI={len(c4_links)}, merged={len(merged)}")
    return merged

# ── LLM job filter (Path B non-ATS) ──────────────────────────────────────────
def llm_filter_jobs(company: str, links: list, track: str = "") -> list:
    if not links: return []
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking llm_filter_jobs()")
    forced_domain = _vertical_domain(track) if track else None
    if forced_domain and (track or "").strip() in PM_TITLE_OK_TRACKS:
        # PRJ-004 REQ-004-09 + 2026-08-19 widening: at AI/Robotics/Space/
        # Defense companies plain "Program Manager" titles also qualify —
        # filter only for role type and link validity.
        rule = (f"Company: {company} is a {forced_domain}-track company; every "
                "genuine Technical Program Manager / TPM role qualifies. "
                "Return ONLY URLs for Technical Program Manager, Program "
                "Manager, and Technical Project Manager roles. "
                "Reject Product Manager, Engineering Manager, and clearly "
                "non-technical Project Manager titles (e.g. Finance/HR/"
                "Marketing project managers). Reject ghost/closed/404 links.")
    elif forced_domain:
        # Fintech vertical: every genuine TPM role qualifies, but generic
        # "Program Manager" stays out (large non-technical PM population).
        rule = (f"Company: {company} is a {forced_domain}-track company; every "
                "genuine Technical Program Manager / TPM role qualifies. "
                "Return ONLY URLs for Technical Program Manager / TPM and "
                "Technical Project Manager roles. "
                "Reject Product Manager, Engineering Manager, generic Program "
                "Manager, and non-technical Project Manager titles. "
                "Reject ghost/closed/404 links.")
    else:
        rule = (
            "You are a strict Technical Recruiter at a mid-large tech company. "
            "Return ONLY URLs for Technical Program Manager / TPM and "
            "Technical Project Manager roles that "
            "belong to one of five tracks:\n"
            "  - AI: AI/ML models, AI products/platforms, AI/ML infrastructure, "
            "cloud/compute infrastructure (datacenter, silicon, GPU fleet)\n"
            "  - Fintech: payments orgs (Google Pay, Apple Pay, Amazon Payments, "
            "checkout/risk/ledger platforms)\n"
            "  - Robotics: robotics sub-orgs (fulfillment robotics, "
            "autonomous-mobility hardware, humanoid programs)\n"
            "  - Space: space sub-orgs (Project Kuiper, Amazon Leo, Azure Space, "
            "satellite/ground-segment programs)\n"
            "  - Defense: defense/gov sub-orgs (Azure Government, AWS GovCloud/DoD, "
            "mission-systems teams)\n"
            "Reject TPM roles in Finance, HR, Legal, Marketing, or generic non-track "
            "engineering. Reject Product Manager, Engineering Manager, generic "
            "Program Manager, and non-technical Project Manager titles. "
            "Reject ghost/closed/404 links.")
    cfg = types.GenerateContentConfig(
        system_instruction=rule + SECURITY_CLAUSE,
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
                "Extract qualifying TPM URLs."
            ),
            config=cfg,
        )
        return json.loads(resp.text).get("urls", [])
    except Exception as e:
        logging.error(f"[LLM filter] failed for {company} ({len(links)} links): "
                      f"{type(e).__name__}: {e}")
        return []

# ── Google Careers JD scraper (Firecrawl only_main_content avoids sidebar) ─────
# BUG-67: side-channel for posted dates found in scraped pages' JSON-LD.
# Scrapers return plain markdown text, so a structured date discovered during
# the scrape is stashed here and consumed by the date chain in
# _gate_and_finalize. Keyed by JD URL; cleared at the start of each run.
_JSONLD_DATE_BY_URL: dict = {}


def _parse_jsonld_jobposting(html: str) -> dict:
    """Parse the first JobPosting JSON-LD block found in `html`.

    Returns {"title", "location", "salary", "description", "date_posted"}
    (all "" when absent). Consolidates the near-identical blocks that lived
    in the Workday / Google / Microsoft / Tesla scrapers, and — new for
    BUG-67 — reads `datePosted`, which those blocks all ignored.
    """
    empty = {"title": "", "location": "", "salary": "",
             "description": "", "date_posted": ""}
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "", re.DOTALL | re.IGNORECASE,
    )

    def _fmt_addr(addr: dict) -> str:
        parts = [addr.get("addressLocality", ""),
                 addr.get("addressRegion", ""),
                 addr.get("addressCountry", "")]
        return ", ".join(p for p in parts if p)

    for block in blocks:
        try:
            d = json.loads(block)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        # Some Workday tenants omit @type; require a description instead,
        # but reject blocks explicitly typed as something else.
        if d.get("@type") not in (None, "JobPosting"):
            continue
        desc = d.get("description", "")
        if not desc:
            continue
        # Fix common Mojibake (UTF-8 bytes stored as Latin-1 JSON escapes)
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
            for _loc in job_loc:
                if isinstance(_loc, dict):
                    s = _fmt_addr(_loc.get("address", {}))
                    if s:
                        loc_parts.append(s)
            location = "; ".join(loc_parts)
        elif isinstance(job_loc, dict):
            location = _fmt_addr(job_loc.get("address", {}))
        else:
            location = ""

        # Salary: prefer the structured baseSalary field, fall back to regex.
        salary = ""
        base_sal = d.get("baseSalary", {})
        if isinstance(base_sal, dict):
            val = base_sal.get("value", {})
            if isinstance(val, dict):
                min_v = val.get("minValue", "")
                max_v = val.get("maxValue", "")
                currency = base_sal.get("currency", "USD")
                if min_v and max_v:
                    try:
                        salary = f"${int(float(min_v)):,} - ${int(float(max_v)):,} {currency}"
                    except Exception:
                        salary = f"{min_v} - {max_v} {currency}"
        if not salary:
            sal_m = re.search(
                r'\$[\d,]+\s*(?:USD)?\s*(?:[-–]|to)\s*\$[\d,]+\s*(?:USD)?',
                desc, re.IGNORECASE,
            )
            if sal_m:
                salary = sal_m.group(0)

        return {"title": d.get("title", ""), "location": location,
                "salary": salary, "description": desc,
                "date_posted": _parse_iso_date(d.get("datePosted", ""))}
    return empty


def _format_jd_text(parsed: dict, desc_limit: int) -> str:
    """Assemble the Title/Location/Salary + description text that the
    JSON-LD-based scrapers feed to extract_jd()."""
    if not parsed.get("description"):
        return ""
    lines = []
    if parsed.get("title"):
        lines.append(f"Title: {parsed['title']}")
    if parsed.get("location"):
        lines.append(f"Location: {parsed['location']}")
    if parsed.get("salary"):
        lines.append(f"Salary: {parsed['salary']}")
    lines.append("")
    lines.append(parsed["description"][:desc_limit])
    return "\n".join(lines)


def _stash_jsonld_date(url: str, parsed: dict) -> None:
    """BUG-67: remember the structured datePosted found while scraping `url`.
    Records "" too — that marks the page's JSON-LD as already examined, so
    the date chain won't fetch the same page a second time."""
    _JSONLD_DATE_BY_URL[url] = parsed.get("date_posted", "")


def _scrape_google_jd(url: str) -> str:
    """
    Google Careers pages are JS SPAs — server-rendered HTML contains no JSON-LD.
    Crawl4AI renders the full page including the sidebar job list, which causes
    Gemini to extract info from the wrong (sidebar) job.

    Fix: use Firecrawl with only_main_content=True to strip navigation/sidebars
    and return just the main job description area.
    Falls back to plain requests + JSON-LD check (in case Google ever adds it).
    """
    # ── 1. Firecrawl with only_main_content (primary) ────────────────────────
    if _FC_POOL is not None and not _FC_POOL.exhausted:
        try:
            result = _FC_POOL.scrape(url, formats=["markdown"], only_main_content=True)
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
            parsed = _parse_jsonld_jobposting(r.text)
            text = _format_jd_text(parsed, desc_limit=6000)
            if text:
                _stash_jsonld_date(url, parsed)
                logging.info(f"[Google JD] JSON-LD fetched: {url}")
                return text
    except Exception as e:
        logging.debug(f"[Google JD] requests failed: {e}")

    return ""


# ── Microsoft Careers JD scraper (Firecrawl only_main_content) ────────────────
def _scrape_microsoft_jd(url: str) -> str:
    """
    Microsoft careers (jobs.careers.microsoft.com / careers.microsoft.com) are
    JS SPAs whose server-rendered HTML is mostly navbar / theme JSON. Crawl4AI
    captures the sidebar job list, contaminating Gemini's extraction with
    "Other open roles" content (audit Line 2 — ~40-50% of MS cached JDs were
    nav-chrome rather than JD bodies).

    Mirrors _scrape_google_jd: Firecrawl with only_main_content=True is
    primary; falls back to plain requests + JSON-LD if Firecrawl is
    unavailable (Microsoft typically does not embed JSON-LD, so the fallback
    will usually return "" — that's fine, the generic browser scraper picks
    up downstream).
    """
    if _FC_POOL is not None and not _FC_POOL.exhausted:
        try:
            result = _FC_POOL.scrape(url, formats=["markdown"], only_main_content=True)
            md = getattr(result, "markdown", None) or (
                result.get("markdown", "") if isinstance(result, dict) else "")
            if md and len(md) > 200:
                logging.info(f"[Microsoft JD] Firecrawl fetched: {url}")
                return md[:8000]
        except Exception as e:
            logging.debug(f"[Microsoft JD] Firecrawl failed: {e}")

    # Future-proof JSON-LD fallback (Microsoft doesn't currently embed one,
    # but if they ever add it we'll pick it up automatically).
    _headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = requests.get(url, timeout=15, headers=_headers)
        if r.status_code == 200:
            parsed = _parse_jsonld_jobposting(r.text)
            text = _format_jd_text(parsed, desc_limit=6000)
            if text:
                _stash_jsonld_date(url, parsed)
                logging.info(f"[Microsoft JD] JSON-LD fetched: {url}")
                return text
    except Exception as e:
        logging.debug(f"[Microsoft JD] requests failed: {e}")

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
        parsed = _parse_jsonld_jobposting(r.text)
        text = _format_jd_text(parsed, desc_limit=4000)
        if text:
            _stash_jsonld_date(url, parsed)
            return text
    except Exception as e:
        logging.error(f"[Workday scrape] {e}")
    return ""


# ── Tesla JD scraper (Firecrawl → CUA API → __NEXT_DATA__ → JSON-LD → browser) ─
def _scrape_tesla_jd(url: str) -> str:
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
    if _FC_POOL is not None and not _FC_POOL.exhausted:
        try:
            result = _FC_POOL.scrape(url=url, formats=["markdown"])
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
        parsed = _parse_jsonld_jobposting(html)
        text = _format_jd_text(parsed, desc_limit=6000)
        if text:
            _stash_jsonld_date(url, parsed)
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
            logging.warning(f"[Crawl] {url}: success={res.success} status={res.status_code}")
            return ""
        md = res.markdown or ""
        valid, reason = _is_valid_jd_content(md)
        if not valid:
            logging.warning(f"[Crawl] Invalid JD content ({reason}, {len(md)} chars): {url}")
            return ""
        return md[:8000]
    except Exception as e:
        logging.error(f"[Crawl] {url}: {type(e).__name__}: {e}")
        return ""

def md5(text: str) -> str:
    t = re.sub(r'https?://\S+', '', text)
    t = re.sub(r'!\[.*?\]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return hashlib.md5(t.encode()).hexdigest()

def extract_jd(markdown: str, company: str = "", track: str = "") -> str:
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking extract_jd()")
    if track:
        forced_domain = _vertical_domain(track)
    else:
        # Legacy name-based fallback for manual rows without a Track value.
        forced_domain = "AI" if _classify(company) == "ai_native" else None
    _common_instr = (
        " Extract the job_title from the posting's title or main heading."
        " Extract the salary or compensation range if mentioned anywhere in the posting "
        "(look for '$', 'USD', 'per year', 'annually', 'base pay', 'compensation', "
        "'salary range'). If no salary is present, leave the field empty — do not fabricate."
        " Extract ALL locations for this specific job posting verbatim as written —"
        " city, state/region, and country when shown, including 'Remote' if applicable."
        " Do not omit or filter out non-US locations. If the role is available at"
        " multiple locations, join them with '; '"
        " (e.g. 'Santa Clara, CA; Austin, TX; Bangalore, India; Remote')."
        " Ignore sidebar navigation,"
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
        " For 'ats_keywords': extract 8-15 high-signal keywords that an ATS scanner or"
        " recruiter keyword search would key on for this role. Each keyword must be a"
        " noun phrase or proper noun, 1-4 words, lowercase or canonical casing."
        " Prefer named entities: tool / library / framework names (PyTorch, Kubernetes,"
        " BigQuery, Airflow), specific technologies / techniques (LLM, RAG, MLOps, fine-tuning),"
        " platforms (AWS, GCP), domains (computer vision, NLP), certifications (PMP, CSM),"
        " methodologies (Agile, OKRs). EXCLUDE adjectives ('strong', 'excellent'),"
        " generic verbs ('build', 'lead', 'collaborate'), full sentences, and soft skills."
        " Deduplicate. If the JD explicitly lists tools or skills, prioritize those."
        # PRJ-004 REQ-004-08: YoE extraction rides the same call — no extra round-trip.
        " For 'min_yoe': extract the minimum required years of experience as an integer"
        " if the JD states one (e.g. '7+ years of program management' → 7). Use the"
        " requirements section's stated minimum; if multiple minimums are stated, use the"
        " one attached to overall/program-management experience. null if no numeric"
        " minimum is stated — never guess."
        # PRJ-004 REQ-004-11: work-authorization screen.
        " For 'work_auth', classify the posting's work-authorization requirement:"
        " 'citizenship_required' if US citizenship is required;"
        " 'clearance_required' if an active security clearance OR the ability to"
        " obtain one is required;"
        " 'us_person_ok' if the JD says US person / permanent resident / green card"
        " is acceptable (ITAR standard);"
        " 'none_stated' if no authorization requirement appears."
        " Leave 'posted_date' as an empty string — it is filled by the pipeline, not you."
    )
    if forced_domain:
        sys_instr = (f"Extract structured JD data. Company: {company} is a "
                     f"{forced_domain}-track company; every TPM role here is "
                     f"domain-qualified — set job_domain to \"{forced_domain}\"."
                     + _common_instr)
    else:
        # PRJ-004 REQ-004-09 (D-10): mid-large-tech per-JD judgment with
        # explicit mapping anchors for all five tracks.
        sys_instr = (
            "Extract structured JD data. Classify job_domain into exactly one of "
            "AI / Robotics / Fintech / Space / Defense — or \"None\" if the role "
            "matches no track. Mapping anchors:\n"
            "  - Cloud/compute infrastructure (AWS/GCP/Azure infra, datacenter, "
            "silicon, GPU fleet), AI/ML models, AI products/platforms, MLOps → \"AI\"\n"
            "  - Payments orgs (Google Pay, Apple Pay, Amazon Payments, checkout/"
            "risk/ledger platforms) → \"Fintech\"\n"
            "  - Robotics sub-orgs (Amazon Robotics, fulfillment/warehouse robotics, "
            "autonomous-mobility hardware, humanoid programs) → \"Robotics\"\n"
            "  - Space sub-orgs (Project Kuiper, Amazon Leo, Azure Space, satellite/"
            "ground-segment programs) → \"Space\"\n"
            "  - Defense/gov sub-orgs (Azure Government, AWS GovCloud/DoD programs, "
            "mission-systems teams) → \"Defense\"\n"
            "  - Anything else (Office 365, general finance/HR/retail TPM, generic "
            "web products, SOX/audit/GRC) → \"None\"\n"
            "Examples: ACCEPT 'TPM, ML Infrastructure' → AI; 'Sr TPM, Project Kuiper' "
            "→ Space; 'TPM, Google Pay Risk' → Fintech. REJECT 'TPM, SOX Compliance' "
            "→ None; 'TPM, Marketing Technology' → None (unless NLP/ML is explicit)."
            + _common_instr)
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
        logging.error(f"[Extract] failed for {company} ({len(markdown)} chars, "
                      f"track={track!r}): {type(e).__name__}: {e}")
        return "{}"

# ── JD scraper function registry (for routing table lookup) ──────────────────
_JD_FN_REGISTRY = {
    "_scrape_google_jd":         lambda url, **kw: _scrape_google_jd(url),
    "_scrape_microsoft_jd":      lambda url, **kw: _scrape_microsoft_jd(url),
    "_scrape_workday_jd":        lambda url, **kw: _scrape_workday_jd(url),
    "_scrape_tesla_jd":          lambda url, **kw: _scrape_tesla_jd(url),
    "_scrape_greenhouse_api_jd": None,  # special: needs (board, jid), handled separately
}

# ── Shared scraper routing & JD processing pipeline ──────────────────────────
async def _route_scraper(url: str, crawler, list_meta: dict | None = None) -> tuple[str, str]:
    """Route URL to the appropriate scraper.  Returns (markdown, label).

    list_meta (PRJ-004): per-URL metadata from the list-API phase — carries
    prefetched JD markdown (Amazon) and the Workday marker; generalizes the
    pre-PRJ-004 workday_meta parameter."""
    meta = (list_meta or {}).get(url) or {}
    # Priority 0 (REQ-004-17/G6b): JD text already came with the list API —
    # zero additional calls, never falls to the generic crawler.
    if meta.get("_prefetched_md"):
        return meta["_prefetched_md"], meta.get("_platform", "prefetched")

    # Priority 1: Workday discovery metadata (no browser fallback)
    if meta.get("_workday"):
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
                markdown = jd_fn(url)
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


def _apply_prescrape_freshness_gate(to_process: list, list_meta: dict,
                                    known_url_meta: dict) -> tuple:
    """PRJ-004 REQ-004-10 (D-11/D-18): pre-scrape freshness gate.

    Skips a URL iff ALL of: it is NOT already in the sheet, the list API
    supplied a date that PARSES as ISO (YYYY-MM-DD), and that date is ≥15 days
    old. Unparseable/blank dates are explicitly NOT treated as aged — they take
    the unknown-date keep+flag path (never a silent drop), so a future adapter
    forwarding raw date text cannot cause aged-drops. Rows already tracked are
    never retroactively touched. Returns (kept_urls, aged_skipped_count)."""
    kept, aged_skipped = [], 0
    for u in to_process:
        posted = str((list_meta.get(u) or {}).get("posted_date", "") or "").strip()
        is_parseable_date = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", posted))
        if u not in known_url_meta and is_parseable_date \
                and compute_freshness_tier(posted) is None:
            logging.info(f"[FreshnessGate] Skipped ≥15-day-old posting "
                         f"({posted}): {u}")
            aged_skipped += 1
            continue
        kept.append(u)
    return kept, aged_skipped


# PRJ-004 REQ-004-08: Senior/Staff-prefix titles auto-qualify when YoE is unstated.
_SENIOR_TITLE_RE = re.compile(r"\b(senior|sr\.?|staff|principal|director)\b", re.I)

# PRJ-004 REQ-004-10: optional Tavily client for posting-date backfill.
_TAVILY_CLIENT = None


def _backfill_posted_date(company: str, title: str) -> str:
    """Best-effort posting-date backfill via a Tavily search scoped to
    site:linkedin.com/jobs (REQ-004-19: LinkedIn is a search signal only —
    result snippets are parsed, pages are never fetched). Returns '' when
    unavailable/unfound; the caller leaves the manual-review flag in place."""
    if _TAVILY_CLIENT is None or not (company and title):
        return ""
    try:
        r = _TAVILY_CLIENT.search(
            query=f'site:linkedin.com/jobs "{title}" "{company}"', max_results=3)
        for item in r.get("results", []):
            text = f"{item.get('title', '')} {item.get('content', '')}"
            m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
            if m:
                return m.group(1)
            m = re.search(r"(\d+)\s+(day|week)s?\s+ago", text.lower())
            if m:
                days = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
                return (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception as e:
        logging.debug(f"[Tavily backfill] failed for {company}/{title}: {e}")
    return ""


def _fetch_jsonld_posted_date(url: str) -> str:
    """BUG-67: last-chance deterministic date recovery — one plain GET for the
    page's JSON-LD `datePosted`. No API quota; '' on any failure. Sync call on
    the staging path, same as the Tavily backfill it precedes."""
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if r.status_code == 200:
            return _parse_jsonld_jobposting(r.text)["date_posted"]
    except Exception as e:
        logging.debug(f"[JSON-LD date] fetch failed for {url}: {e}")
    return ""


def _gate_and_finalize(parsed: dict, track: str, url: str,
                       posted_date: str = "", list_location: str = "") -> dict | None:
    """PRJ-004 write-time gates + record finalization (REQ-004-08/09/10/11).

    Returns the finalized record, or None when a gate skips the row. Runs on
    EVERY staging path — discovery (all scraper labels) and the incomplete-row
    retry alike (BUG-66: the geo/domain/YoE/work-auth gates previously only
    covered the generic-crawler path)."""
    # ── Gate 0 (REQ-004-12, BUG-66): geo — unconditional on all paths.
    # Keep-set per _geo_out_of_scope (WA/CA/TX/US-Remote; +FL on Space
    # track, REQ-162). Blank/unrecognized locations classify as "Unknown"
    # and are kept for review (they surface via Data Quality="partial");
    # only rows whose location confirmably matches no target region drop.
    loc = parsed.get("location", "")
    # BUG-82: the list API's structured location (when present) already
    # passed the pre-scrape geo gate; the LLM's verbatim/reformatted string is
    # a second, differently-shaped reading of the same posting. Drop only
    # when BOTH are out of scope — never on LLM formatting alone.
    if _geo_out_of_scope(loc, track) and (
            not list_location or _geo_out_of_scope(list_location, track)):
        print(f"      🌍 [GeoFilter] Skipped out-of-region ({loc}): {url}")
        return None
    # ── Gate 1 (REQ-004-09): domain. Vertical companies can't hit "None"
    # because the classifier output is deterministically overridden.
    forced = _vertical_domain(track) if track else None
    if forced:
        parsed["job_domain"] = forced
    domain = parsed.get("job_domain") or "None"
    if domain not in JOB_DOMAIN_VALUES:
        print(f"      🚫 [DomainGate] No track match (domain={domain!r}): {url}")
        return None
    # ── Gate 1.5 (BUG-72): intern/co-op/new-grad titles — never in scope.
    # Belt-and-braces behind _tpm_filter: covers the generic-crawler/LLM path
    # and incomplete-row retries, where no title prefilter runs.
    if _INTERN_TITLE_RE.search(parsed.get("job_title", "") or ""):
        print(f"      🚫 [InternGate] student role: {url}")
        return None
    # ── Gate 2 (REQ-004-08, amended REQ-166 2026-08-21): YoE — skip iff
    # stated min ≥11. No lower cut: a "2-5 yrs" startup TPM is in scope.
    # ("10+ years" keeps, "12+ years" skips). Unstated → keep + flag.
    min_yoe = parsed.get("min_yoe")
    if isinstance(min_yoe, int):
        if min_yoe >= 11:
            print(f"      🚫 [YoEGate] stated min {min_yoe} yrs (skip rule: ≥11): {url}")
            return None
        parsed["yoe_flag"] = ""
    else:
        title = parsed.get("job_title", "") or ""
        parsed["yoe_flag"] = ("auto-qualified (title)" if _SENIOR_TITLE_RE.search(title)
                              else "manual review — YoE unstated")
    # ── Gate 3 (REQ-004-11): work authorization (global, all buckets).
    work_auth = parsed.get("work_auth", "none_stated")
    if work_auth in ("citizenship_required", "clearance_required"):
        print(f"      🚫 [WorkAuthGate] {work_auth}: {url}")
        return None
    # PRJ-004 REQ-004-10 / BUG-67: posted date is never LLM-extracted. Chain:
    # ATS list metadata → JSON-LD date stashed during the scrape → one plain
    # GET for the page's JSON-LD → Tavily backfill → keep + flag. Never a
    # drop condition (design.md §2.2.3 F3).
    if posted_date:
        parsed["posted_date"] = posted_date
    if not parsed.get("posted_date"):
        if url in _JSONLD_DATE_BY_URL:
            parsed["posted_date"] = _JSONLD_DATE_BY_URL[url]
        else:
            parsed["posted_date"] = _fetch_jsonld_posted_date(url)
    if not parsed.get("posted_date"):
        backfilled = _backfill_posted_date(parsed.get("company", ""),
                                           parsed.get("job_title", ""))
        if backfilled:
            parsed["posted_date"] = backfilled
    if not parsed.get("posted_date"):
        parsed["date_flag"] = "manual review — unknown posted date"
    # REQ-060: assess JD field completeness and inject into record
    parsed["data_quality"] = _assess_jd_quality(parsed)
    return parsed


async def _process_scraped_jd(
    url: str, markdown: str, company: str, track: str,
    stale_set: set, known_url_meta: dict,
    pending: list, timestamp_only: list,
    label: str,
    posted_date: str = "",
    list_location: str = "",
) -> None:
    """Common pipeline: hash → stale check → cache → extract → gates → stage.

    PRJ-004 write-time gates (REQ-004-08/09/11): skipped rows are never
    written; existing sheet rows are never touched by these gates (they run
    only on the staging path — no auto-delete, REQ-004-16)."""
    hash_val = md5(markdown)
    if url in stale_set and known_url_meta.get(url, {}).get("hash") == hash_val:
        timestamp_only.append(url)
        known_url_meta[url]["age_days"] = 0
        print(f"      ♻️  Unchanged (timestamp-only): {url}")
        return
    _save_md_to_cache(url, markdown)
    await _GEMINI_LIMITER.acquire()
    jd_json = extract_jd(markdown, company=company, track=track)
    try:
        parsed = json.loads(jd_json)
    except Exception:
        parsed = {}
    if not parsed.get("company"):
        # BUG-62: stage an audit row instead of skipping silently. The
        # non-JSON sentinel fails json.loads inside batch_upsert_jd_records,
        # which writes the existing _JD_ERROR_ROW (company="JSON ERROR",
        # data_quality="failed") — sheet-visible, excluded from matching, and
        # still retried next run via the same URL-keyed row (bounded growth).
        logging.warning(f"[Extract] Gemini returned empty data for {url} — "
                        f"staging JSON-ERROR audit row")
        pending.append((url, "EXTRACTION EMPTY — NOT JSON", hash_val))
        return
    parsed = _gate_and_finalize(parsed, track, url, posted_date=posted_date,
                                list_location=list_location)
    if parsed is None:
        return
    jd_json = json.dumps(parsed)
    _save_structured_jd_md(url, parsed)
    pending.append((url, jd_json, hash_val))
    known_url_meta[url] = {"hash": hash_val, "age_days": 0, "title": ""}
    suffix = f" ({label})" if label != "generic" else ""
    print(f"      ✅ Staged{suffix} [{parsed.get('job_domain')}]: {url}")


# ── Per-company async pipeline ────────────────────────────────────────────────
async def process_company(row: list, known_url_meta: dict, xlsx_path: str,
                           excel_lock: asyncio.Lock, crawler,
                           triaged_set: set | None = None) -> None:
    if len(row) < 4: return
    name       = str(row[0]).strip() if row[0] else ""
    track      = str(row[1]).strip() if len(row) > 1 and row[1] else ""
    career_url = str(row[3]).strip() if row[3] else ""
    if not career_url or career_url == "N/A": return
    if not career_url.startswith("http"):
        career_url = "https://" + career_url

    print(f"\n{'─'*55}")
    print(f"🏢  {name}  [{track}]  ({career_url})")

    # Rate-limit delay for crawler path
    if not any(d in career_url for d in ALL_ATS):
        print("    ⏳ Path B rate-limit delay (15s)...")
        await asyncio.sleep(15)

    raw = await discover_jobs(career_url, crawler, track)
    if not raw:
        print("    No TPM jobs found.")
        return

    if any(d in career_url for d in ALL_ATS):
        raw  = _nontech_title_prefilter(raw, track)
        urls = [l["url"] for l in raw]
        print(f"    ✅ {len(urls)} TPM roles (ATS-trusted, title-filtered).")
    else:
        await _GEMINI_LIMITER.acquire()
        urls = llm_filter_jobs(name, raw, track)
        if not urls:
            print("    LLM filtered out all URLs.")
            return
        print(f"    LLM identified {len(urls)} qualifying TPM roles.")

    await asyncio.sleep(4)

    # PRJ-004 REQ-004-10: generalize the Workday-only prefetch lookup into
    # list-level metadata for ALL platforms — carries posted_date (and, for
    # adapters like Amazon, prefetched JD markdown).
    list_meta = {l["url"]: l for l in raw if l.get("url")}

    fresh_set = {u for u, m in known_url_meta.items() if m["age_days"] < FRESH_DAYS}
    stale_set = {u for u, m in known_url_meta.items() if m["age_days"] >= FRESH_DAYS}
    # BUG-71: all known/triaged membership tests run on canonical URLs so the
    # same posting rediscovered under a different URL string still matches.
    fresh_canon = {canonical_jd_url(u) for u in fresh_set}
    stale_canon = {canonical_jd_url(u) for u in stale_set}

    to_process = [u for u in urls if canonical_jd_url(u) not in fresh_canon]

    # BUG-65: user-triaged URLs (JD_ToApply / Skipped JD tabs) are final —
    # never re-scrape or re-add them to JD_Tracker. triaged_set holds
    # canonical URLs (BUG-71).
    if triaged_set:
        before_triage = len(to_process)
        to_process = [u for u in to_process if canonical_jd_url(u) not in triaged_set]
        if before_triage - len(to_process):
            print(f"    🗂️  Triage gate: skipped {before_triage - len(to_process)} "
                  f"user-triaged posting(s).")

    to_process, aged_skipped = _apply_prescrape_freshness_gate(
        to_process, list_meta, known_url_meta)
    if aged_skipped:
        print(f"    🕰️  Freshness gate: skipped {aged_skipped} posting(s) ≥15 days old.")

    if not to_process:
        print(f"    All {len(urls)} JDs fresh (< {FRESH_DAYS} days) or aged out. Skipping.")
        return

    new_urls_list   = [u for u in to_process if canonical_jd_url(u) not in stale_canon]
    stale_urls_list = [u for u in to_process if canonical_jd_url(u) in stale_canon]
    print(f"    {len(new_urls_list)} new, {len(stale_urls_list)} stale JDs to process.")

    sem            = asyncio.Semaphore(3)
    pending        = []   # (url, jd_json, hash_val) — collected, written once at end
    timestamp_only = []   # urls where hash unchanged; only timestamp update needed
    seen_urls: set[str] = set()  # BUG-04: guard against duplicate URLs in to_process

    async def fetch_one(url: str) -> None:
        # ── Scrape phase: concurrency-limited by semaphore (I/O bound) ──
        async with sem:
            canon = canonical_jd_url(url)  # BUG-71: dedupe on canonical form
            if canon in seen_urls:  # BUG-04: skip duplicate (atomic check+add, no await between)
                return
            seen_urls.add(canon)
            print(f"      🕷️  {url}")
            markdown, label = await _route_scraper(url, crawler, list_meta)
        if not markdown:
            tag = "Scrape" if label == "generic" else label
            logging.warning(f"[{tag}] Empty {'markdown' if label == 'generic' else 'JD'} for {url}")
            return
        # ── Gemini phase: rate-limited only (semaphore released, won't block scraping) ──
        await _process_scraped_jd(
            url, markdown, name, track,
            stale_set, known_url_meta,
            pending, timestamp_only, label,
            posted_date=(list_meta.get(url) or {}).get("posted_date", ""),
            list_location=(list_meta.get(url) or {}).get("location", ""),
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
    summary = RunSummary(agent="job")
    try:
        await _main_inner(summary)
    except GeminiTransientError as e:
        summary.transient_errors += 1
        summary.note(f"Run aborted (transient): {e}")
        raise
    except Exception as e:
        summary.note(f"Run aborted: {type(e).__name__}: {e}")
        raise
    finally:
        # PRJ-004 REQ-004-25/26: token-usage snapshot in every run log —
        # the measurement carrier for the trial-run cost gate.
        try:
            from shared.gemini_pool import get_usage_summary
            summary.note(f"gemini usage: {get_usage_summary()}")
        except Exception:
            pass
        summary.mark_finished()
        log_path = summary.write()
        print(f"📊 Run summary: {log_path}")
        print(summary.to_json())


async def _main_inner(summary: RunSummary):
    global _KEY_POOL, _FC_POOL
    gemini_keys = [k for k in [
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
    ] if k]
    if not gemini_keys:
        print("❌ Missing env var: GEMINI_API_KEY")
        summary.note("Missing GEMINI_API_KEY")
        return
    # BUG-68: key pool with 402/429 rotation + one loud exhaustion warning.
    _FC_POOL = build_pool_from_env()
    if _FC_POOL is None:
        print("❌ Missing env var: FIRECRAWL_API_KEY")
        summary.note("Missing FIRECRAWL_API_KEY")
        return
    _KEY_POOL = _GeminiKeyPoolBase(gemini_keys, genai_mod=genai)
    logging.info(f"[KeyPool] Loaded {len(gemini_keys)} Gemini API key(s).")

    # PRJ-004 REQ-004-10/19 (D-19): optional Tavily client for posting-date
    # backfill (LinkedIn-scoped SEARCH only — pages are never fetched).
    # Degrades gracefully: without the key the keep+flag path still satisfies
    # the requirement.
    global _TAVILY_CLIENT
    try:
        # Aliased: plain build_pool_from_env is the FIRECRAWL pool builder in
        # this function's scope (imported above) — shadowing it breaks _FC_POOL.
        from shared.tavily_pool import build_pool_from_env as _build_tavily_pool
        _TAVILY_CLIENT = _build_tavily_pool()  # BUG-70: key rotation; None = off
        if _TAVILY_CLIENT is not None:
            logging.info("[Tavily] posting-date backfill enabled.")
    except Exception as e:
        logging.warning(f"[Tavily] backfill unavailable: {e}")

    class _SuppressPageEvalFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "Execution context was destroyed" not in record.getMessage()
    logging.getLogger().addFilter(_SuppressPageEvalFilter())

    print("\n" + "="*60)
    print("JOB AGENT")
    print("="*60 + "\n")

    _JSONLD_DATE_BY_URL.clear()  # BUG-67: per-run scrape-date side-channel
    xlsx_path      = get_or_create_excel()
    companies      = get_company_rows(xlsx_path)
    known_url_meta = get_jd_url_meta(xlsx_path)
    triaged_set    = get_triaged_jd_urls(xlsx_path)
    if triaged_set:
        print(f"🗂️  {len(triaged_set)} user-triaged JD URL(s) permanently excluded "
              f"(JD_ToApply / Skipped JD).")
    lock           = asyncio.Lock()

    if not companies:
        print("⚠️  No companies in list. Run company_agent.py first.")
        summary.note("No companies in list")
        return
    summary.attempted = len(companies)

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
                    process_company(r, known_url_meta, xlsx_path, lock, crawler,
                                    triaged_set=triaged_set)
                    for r in chunk
                ], return_exceptions=True)
                print(f"  ✅ Batch {i//batch+1} complete.")
                for res in batch_results:
                    if isinstance(res, Exception):
                        logging.error(f"[ProcessCompany] Error: {res}")
                        summary.failed += 1
                    else:
                        summary.succeeded += 1

        # ── Phase: Retry incomplete JD records ─────────────────────────────────
        incomplete = get_incomplete_jd_rows(xlsx_path)
        # BUG-65: belt-and-braces — a triaged URL that also (still) sits in
        # JD_Tracker must not be re-extracted/overwritten. Canonical match (BUG-71).
        incomplete = [r for r in incomplete if canonical_jd_url(r["url"]) not in triaged_set]
        if incomplete:
            print(f"\n{'='*60}")
            print(f"🔄 Retrying {len(incomplete)} incomplete JD records...")
            # Build company→track lookup (case-insensitive)
            company_track_map = {}
            for r in companies:
                cname  = str(r[0]).strip() if r[0] else ""
                ctrack = str(r[1]).strip() if len(r) > 1 and r[1] else ""
                if cname:
                    company_track_map[cname.lower()] = ctrack

            retry_pending = []
            retry_lock    = asyncio.Lock()

            async def retry_one(rec: dict) -> None:
                url          = rec["url"]
                company_name = rec["company"]
                track        = company_track_map.get(company_name.lower(), "")
                print(f"  🔄 {url}")

                markdown, _ = await _route_scraper(url, crawler)
                if not markdown:
                    logging.warning(f"[Retry] Empty markdown for {url}")
                    return
                _save_md_to_cache(url, markdown)
                hash_val = md5(markdown)
                await _GEMINI_LIMITER.acquire()
                jd_json  = extract_jd(markdown, company=company_name, track=track)
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
                # BUG-67: preserve the row's existing Posted Date — the retry
                # rewrite previously wiped it (the LLM never returns one).
                parsed["posted_date"] = rec.get("posted_date", "")
                # BUG-66: retries pass the same write-time gates as discovery
                # (geo / domain / YoE / work-auth) — this path previously
                # bypassed all of them.
                parsed = _gate_and_finalize(parsed, track, url)
                if parsed is None:
                    return
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
    total_tpm       = sum(v["tpm"]       for v in counts.values())
    total_qualified = sum(v["qualified"] for v in counts.values())
    print(f"   Total TPM jobs tracked:       {total_tpm}")
    print(f"   Total qualified jobs tracked: {total_qualified}")
    for cname, v in sorted(counts.items(), key=lambda x: -x[1]["qualified"]):
        if v["tpm"] > 0:
            print(f"   {cname}: {v['tpm']} TPM | {v['qualified']} qualified")

    # ── Phase: Sort JD_Tracker by location tier ───────────────────────────────
    # Combined 1–6 sort tier: freshness primary, WA/Remote > CA/TX secondary.
    # Wrapped in try/except so a sort failure (e.g. file open in Excel) does not
    # discard the run's actual JD writes.
    print(f"\n{'='*60}")
    print("🗺️  Sorting JD_Tracker by sort tier (freshness × WA/Remote > CA/TX)...")
    try:
        n_sorted = sort_jd_tracker_by_tier(xlsx_path)
        print(f"   Sorted {n_sorted} JD rows + applied tier highlights.")
    except Exception as e:
        logging.warning(f"[LocationTier] Sort skipped: {type(e).__name__}: {e}")
        print(f"   ⚠️  Sort skipped ({type(e).__name__}). Close the file in Excel and re-run if needed.")

    print("\n🎉 Job Agent complete.")
    print(f"📊 Results: {xlsx_path}")

if __name__ == "__main__":
    from shared.run_lock import acquire_run_lock, AgentAlreadyRunning
    try:
        _run_lock = acquire_run_lock("job_agent")
    except AgentAlreadyRunning as e:
        raise SystemExit(f"🔒 {e}")
    asyncio.run(main())
