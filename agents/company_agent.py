"""
Company Agent — runs the least frequently (one-time or as needed).

Responsibilities:
  1. Read existing companies from Excel (avoid duplicates)
  2. Discover NEW companies via Tavily + Gemini in batches of 50, looping
     until every track bucket reaches quota (500 total), a batch yields
     nothing new (Tavily quota exhausted / query-pool convergence), or the
     runaway iteration cap is hit
  3. Find real career URLs via multi-strategy lookup (NO Gemini URL guessing)
  4. Phase 1.5: Backfill blank Career URLs on manually-inserted rows.
     Filled Career URLs are user-verified and never rewritten (BUG-74/75);
     URL-quality findings are report-only via `--audit`
  5. Enrich incomplete rows: blank Business Focus (BUG-69) and blank Track
     are filled every run, so manual name-only rows self-heal
  6. Sort Company_List by Track (canonical 6-bucket order) as the last step

URL discovery strategy (per company, in order):
  1. KNOWN_CAREER_URLS exact match
  2. Tavily targeted search → ATS/career URL extracted from results
  3. ATS slug probing (Greenhouse → Lever)
  4. Company homepage scraping → find careers link

Search distribution (rebalanced 2026-05 toward AI-TPM yield):
  - Big Tech (AI Investment)  : 25%
  - Consumer ML Tech          : 20%
  - AI Startups               : 25%
  - AI Infra / Compute / GPU  : 20%
  - Large Model Labs          : 10%

Run:
  python agents/company_agent.py
"""
import os
import sys
import json
import re
import logging
import time
from typing import Literal
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ── Project root on path so shared.* is importable ───────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.excel_store import (
    EXCEL_PATH, get_or_create_excel, count_company_rows,
    get_company_rows, get_company_rows_with_row_num, upsert_companies,
    update_company_career_url, update_company_track,
    get_company_names_without_tpm,
    get_incomplete_company_rows, update_company_business_focus,
    sort_company_list_by_track,
    AUDIT_SHEET, replace_audit_sheet, get_audit_proposals,
)
from shared.gemini_pool import _GeminiKeyPoolBase
from shared.tavily_pool import build_pool_from_env as build_tavily_pool_from_env
from shared.config import MODEL, TRACK_ORDER
from shared.prompts import SECURITY_CLAUSE
from shared.run_summary import RunSummary
from shared.run_lock import acquire_run_lock, AgentAlreadyRunning

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# BUG-31: use _GeminiKeyPoolBase directly with genai_mod parameter
_GeminiKeyPool = _GeminiKeyPoolBase  # alias for backward compat (tests)

# PRJ-004 REQ-004-01: 6-track taxonomy at 500-company scale with per-bucket
# quotas. Quotas constrain NEW discovery only — migrated survivors over quota
# are grandfathered (D-12); a full bucket simply gets 0 new-discovery slots.
MAX_TOTAL  = 500   # sum of TRACK_QUOTAS
BATCH_SIZE = 50    # new companies to discover per loop iteration

TRACK_VALUES = TRACK_ORDER  # canonical 6-bucket order lives in shared/config.py
TRACK_QUOTAS = {"AI-native": 150, "Mid-large Tech": 150, "Robotics": 50,
                "Fintech": 50, "Space": 50, "Defense": 50}
VERTICAL_TRACKS = frozenset({"AI-native", "Robotics", "Fintech", "Space", "Defense"})

# REQ-004-03: defense legacy primes are hard-excluded deterministically —
# the exclusion must not depend on LLM compliance. Palantir is the explicit
# allowlist exception to the early-company rule.
DEFENSE_EXCLUDED_PRIMES = frozenset({
    "boeing", "lockheed martin", "lockheed", "raytheon", "rtx",
    "northrop grumman", "northrop", "general dynamics", "bae", "bae systems",
    "l3harris", "l3 harris",
})
DEFENSE_ALLOWLIST = frozenset({"palantir", "palantir technologies"})

_KEY_POOL: "_GeminiKeyPool | None" = None  # initialised in main()

# ── Pydantic schemas ──────────────────────────────────────────────────────────
class CompanyInfo(BaseModel):
    """Company info extracted by Gemini — NO career URL (found separately)."""
    company_name:   str = Field(description="The official name of the company.")
    track:          Literal[
        "AI-native",
        "Mid-large Tech",
        "Robotics",
        "Fintech",
        "Space",
        "Defense",
    ] = Field(
        description=(
            "The track bucket this company belongs to. "
            "Must be exactly one of the 6 listed values."
        )
    )
    business_focus: str = Field(
        description=(
            "3-4 sentence description covering: (1) what the company builds or sells, "
            "(2) who their primary customers are, (3) their key differentiator or "
            "competitive edge, and (4) notable products, funding, or market traction. "
            "Empty string if the search context is too thin to write one — do not guess."
        )
    )
    # BUG-76: mirror of the repair paths' confident gate — discovery used to
    # be the only path with no do-not-guess protection.
    confident:      bool = Field(
        description=(
            "False when the search context is too thin to be sure of the track "
            "and business_focus — do not guess."
        )
    )

# Deprecated alias (pre-PRJ-004 name) — kept so external references fail soft.
AICompanyInfo = CompanyInfo

class CompanyInfoList(BaseModel):
    companies: list[CompanyInfo] = Field(description="A list of discovered companies.")


class BusinessFocusItem(BaseModel):
    """One row of the BUG-69 Business Focus re-enrich pass."""
    company_name:   str = Field(description="Company name exactly as given in the input list.")
    business_focus: str = Field(
        description=(
            "3-4 sentence description covering: (1) what the company builds or sells, "
            "(2) who their primary customers are, (3) their key differentiator or "
            "competitive edge, and (4) notable products, funding, or market traction. "
            "Empty string if the search context is too thin to write one — do not guess."
        )
    )


class BusinessFocusList(BaseModel):
    focuses: list[BusinessFocusItem]


class TrackClassification(BaseModel):
    """One row of the REQ-004-06 --migrate-tracks re-bucketing pass."""
    company_name: str = Field(description="Company name exactly as given in the input list.")
    track:        Literal[
        "AI-native", "Mid-large Tech", "Robotics", "Fintech", "Space", "Defense",
    ] = Field(description="The best-fit track bucket for this company.")
    rationale:    str = Field(description="One sentence explaining the classification.")
    confident:    bool = Field(description="False if the classification is a guess.")


class TrackClassificationList(BaseModel):
    classifications: list[TrackClassification]


class CompanyAuditItem(BaseModel):
    """One row of the --audit Track/Business-Focus review pass (BUG-76)."""
    company_name:    str = Field(description="Company name exactly as given in the input list.")
    track:           Literal[
        "AI-native", "Mid-large Tech", "Robotics", "Fintech", "Space", "Defense",
    ] = Field(description="The best-fit track bucket for this company.")
    track_confident: bool = Field(description="False when you do not know the company well enough to classify it — do not guess.")
    focus_accurate:  bool = Field(description="True when the current business_focus accurately describes this company.")
    proposed_focus:  str = Field(
        description=(
            "Corrected 3-4 sentence business focus, ONLY when focus_accurate is "
            "false AND you know the company well. Empty string otherwise — do "
            "not guess."
        )
    )
    focus_confident: bool = Field(description="False when you cannot judge the current description's accuracy.")
    notes:           str = Field(description="One short sentence of rationale.")


class CompanyAuditList(BaseModel):
    items: list[CompanyAuditItem]

# ── ATS slug-validation config (public APIs) ──────────────────────────────────
# BUG-75: `org_api`+`org_key` (JSON) or `org_html` (page <title>) give each
# platform a way to fetch the board OWNER's name, so a slug probe can verify
# the board actually belongs to the company (existence alone is not identity:
# "physical" is *somebody's* board, just not Physical Intelligence's).
ATS_VALIDATORS = [
    {
        "platform":      "greenhouse",
        "api_template":  "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "board_template":"https://job-boards.greenhouse.io/{slug}",
        "jobs_key":      "jobs",
        "org_api":       "https://boards-api.greenhouse.io/v1/boards/{slug}",
        "org_key":       "name",
    },
    {
        "platform":      "lever",
        "api_template":  "https://api.lever.co/v0/postings/{slug}?mode=json",
        "board_template":"https://jobs.lever.co/{slug}",
        "jobs_key":      None,
        # Lever's postings API carries no org name — the board page title does
        # (e.g. "Palantir Technologies").
        "org_html":      "https://jobs.lever.co/{slug}",
    },
    {
        "platform":      "ashby",
        "api_template":  "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "board_template":"https://jobs.ashbyhq.com/{slug}",
        "jobs_key":      "jobs",
        # Ashby's posting API carries no org name — the board page title does
        # (e.g. "OpenAI Jobs").
        "org_html":      "https://jobs.ashbyhq.com/{slug}",
    },
    {
        "platform":      "workable",
        "api_template":  "https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "board_template":"https://apply.workable.com/{slug}/",
        "jobs_key":      "jobs",
        # The widget endpoint itself returns the account name.
        "org_api":       "https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "org_key":       "name",
    },
]

# Hard-coded overrides for companies with non-standard ATS URLs (e.g. Workday)
KNOWN_ATS_OVERRIDES = {
    "nvidia":          "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "tesla":           "https://www.tesla.com/careers",
    "google deepmind": "https://www.deepmind.com/careers",
    "deepmind":        "https://www.deepmind.com/careers",
    "xai":             "https://xai.com/careers",
}

KNOWN_CAREER_URLS = {
    "Google":          "https://www.google.com/about/careers/applications/",
    "Google DeepMind": "https://www.deepmind.com/careers",
    "Meta":            "https://www.metacareers.com/",
    "Microsoft":       "https://careers.microsoft.com/",
    "Amazon":          "https://www.amazon.jobs/",
    "Apple":           "https://jobs.apple.com/",
    "Tesla":           "https://www.tesla.com/careers",
    "xAI":             "https://xai.com/careers",
    "NVIDIA":          "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "Salesforce":      "https://careers.salesforce.com/",
    "IBM":             "https://www.ibm.com/employment/",
    "Intel":           "https://jobs.intel.com/",
    "AMD":             "https://jobs.amd.com/",
    "Qualcomm":        "https://careers.qualcomm.com/",
    "Oracle":          "https://careers.oracle.com/",
    "SAP":             "https://jobs.sap.com/",
    "Palantir":        "https://www.palantir.com/careers/",
    "Databricks":      "https://www.databricks.com/company/careers",
    "Snowflake":       "https://careers.snowflake.com/",
    "Stripe":          "https://stripe.com/jobs",
    "Uber":            "https://www.uber.com/us/en/careers/",
    "Lyft":            "https://www.lyft.com/careers",
    "Airbnb":          "https://careers.airbnb.com/",
    "Twitter/X":       "https://careers.x.com/",
    "LinkedIn":        "https://careers.linkedin.com/",
    # Consumer ML Tech
    "Netflix":         "https://jobs.netflix.com/",
    "Spotify":         "https://www.lifeatspotify.com/jobs",
    "Pinterest":       "https://www.pinterestcareers.com/",
    "Disney":          "https://jobs.disneycareers.com/",
    "Roblox":          "https://corp.roblox.com/careers/",
    "eBay":            "https://careers.ebayinc.com/",
    "Snap":            "https://careers.snap.com/",
    "DoorDash":        "https://careers.doordash.com/",
    "Reddit":          "https://www.redditinc.com/careers",
    "OpenAI":          "https://openai.com/careers",
    "Anthropic":       "https://www.anthropic.com/careers",
    "Cohere":          "https://cohere.com/about/careers",
    "Scale AI":        "https://scale.com/careers",
    "Hugging Face":    "https://apply.workable.com/huggingface/",
}

# ── Tavily search queries (PRJ-004: 6 tracks) ─────────────────────────────────
# REQ-161 geo alignment (2026-08-19): one uniform geo tail — WA/CA/TX/US-remote,
# matching the job-side keep-set exactly, no per-track geo differentiation —
# appended to every query by construction. "Washington state" (not bare
# "Washington") avoids D.C. noise.
_QUERY_GEO_TAIL = "Washington state California Texas remote"

_TAVILY_QUERY_BASES = [
    # ── AI-native (labs, startups, infra) ─────────────────────────────────────
    (
        "frontier AI labs and AI startups hiring 2026 OpenAI Anthropic xAI Perplexity "
        "Scale AI Glean Sierra careers technical program manager"
    ),
    (
        "AI infrastructure compute GPU cloud 2026 CoreWeave Lambda Together AI Groq "
        "Cerebras Crusoe Fireworks Modal careers hiring TPM"
    ),
    # ── Mid-large tech (incl. early-bet divisions) ─────────────────────────────
    (
        "big tech new-bet divisions hiring TPM 2026 Amazon Kuiper Leo Azure Government "
        "Google Cloud Meta Reality Labs Apple special projects careers"
    ),
    (
        "mid-size tech companies technical program manager hiring 2026 Databricks Snowflake "
        "Uber Netflix Salesforce ServiceNow careers"
    ),
    # ── Robotics ───────────────────────────────────────────────────────────────
    (
        "robotics companies hiring 2026 Figure AI Apptronik Physical Intelligence Zipline "
        "Nuro Waymo Skild humanoid autonomy careers technical program manager"
    ),
    # ── Fintech ────────────────────────────────────────────────────────────────
    (
        "fintech companies hiring technical program manager 2026 Stripe Plaid Ramp Brex "
        "Chime Affirm Block Coinbase careers"
    ),
    # ── Space ──────────────────────────────────────────────────────────────────
    (
        "space startups hiring 2026 SpaceX Relativity Stoke Space Rocket Lab Firefly "
        "Astranis Varda True Anomaly careers program manager"
    ),
    # ── Defense ────────────────────────────────────────────────────────────────
    (
        "venture-backed defense tech companies hiring 2026 Anduril Shield AI Saronic "
        "Castelion Epirus Vannevar Palantir careers technical program manager"
    ),
    # ── Launch tune (G1 yield): list-style variants for the thin verticals ────
    "list of robotics startups 2026 well funded series B C hiring",
    "list of fintech startups 2026 series B C payments infrastructure hiring",
    "new space companies startups list 2026 funded satellites launch hiring",
    "defense tech startup list 2026 funded autonomous systems hiring careers",
    "AI startups list 2026 series A B C enterprise infrastructure hiring careers",
    "mid size technology companies 2026 hiring technical program managers cloud platform",
]

# Space-track regional enhancement (2026-08-19, user request): the space
# industry clusters in specific metros, so dedicated per-region queries beat
# the generic tail for this one track — Greater Seattle, SoCal, TX, and FL
# (Space Coast). Deliberately OUTSIDE the uniform-tail construction: these
# are geo-targeted by design. FL is a full target region for Space only
# (REQ-162): the extraction GEOGRAPHY clause carries a Space-track FL
# exception, and the job-side geo gates keep FL rows on the Space track.
_SPACE_REGION_QUERIES = [
    "space companies hiring 2026 Greater Seattle Redmond Kent Washington "
    "careers program manager",
    "space startups Southern California El Segundo Hawthorne Long Beach Torrance "
    "hiring 2026 careers program manager",
    "space companies Texas Austin Houston hiring 2026 careers program manager",
    "space companies Florida Cape Canaveral Melbourne Space Coast hiring 2026 "
    "careers program manager",
]

TAVILY_QUERIES = ([f"{q} {_QUERY_GEO_TAIL}" for q in _TAVILY_QUERY_BASES]
                  + _SPACE_REGION_QUERIES)

# ── URL helpers ───────────────────────────────────────────────────────────────
_CAREER_DOMAINS   = ["greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                     "job-boards.greenhouse.io", "jobs.lever.co",
                     "workable.com", "apply.workable.com"]
_CAREER_PATH_KWDS = ["/careers", "/jobs", "/hiring", "/work-with-us",
                     "/join-us", "/join", "/opportunities", "/open-roles"]

# BUG-74: single-job-posting URL shapes per ATS. A posting URL is NOT a
# career page — storing one freezes the sheet on a single (soon-dead) job.
# Each entry: (compiled pattern, board-root template or None when the board
# root is not derivable from the URL alone). Order matters: specific
# platform patterns (with a derivable root) come before the generic
# flag-only patterns so _posting_url_to_board_root prefers them.
_UUID_RE = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
_JOB_POSTING_PATTERNS: list[tuple] = [
    (re.compile(r'greenhouse\.io/(?P<slug>[^/?#]+)/jobs/\d+', re.I),
     "https://job-boards.greenhouse.io/{slug}"),
    (re.compile(r'jobs\.lever\.co/(?P<slug>[^/?#]+)/' + _UUID_RE, re.I),
     "https://jobs.lever.co/{slug}"),
    (re.compile(r'jobs\.ashbyhq\.com/(?P<slug>[^/?#]+)/' + _UUID_RE, re.I),
     "https://jobs.ashbyhq.com/{slug}"),
    (re.compile(r'apply\.workable\.com/(?P<slug>[^/?#]+)/j/[A-Za-z0-9]+', re.I),
     "https://apply.workable.com/{slug}/"),
    (re.compile(r'(?P<root>https?://[^/]+\.myworkdayjobs\.com/[^?#]*?)/job/', re.I),
     "{root}"),
    # Newer Workday CxS sites use /details/ instead of /job/ (e.g.
    # ffive.wd5.../f5jobs/details/NGINX-..._RP1029028).
    (re.compile(r'(?P<root>https?://[^/]+\.myworkdayjobs\.com/[^?#]*?)/details/', re.I),
     "{root}"),
    # Embedded Greenhouse board (gh_jid on the company's own domain) — the
    # board root is the page without the query, but we can't verify that
    # deterministically, so flag-only.
    (re.compile(r'[?&]gh_jid=\d+', re.I), None),
    # Generic posting shapes (numeric job ids, explicit job-id params).
    (re.compile(r'/jobs?/\d{5,}(?:[/?#]|$)', re.I), None),
    (re.compile(r'[?&](jobid|job_id|jid)=', re.I), None),
]


def _is_job_posting_url(url: str) -> bool:
    """BUG-74: True when the URL points at ONE job posting, not a board."""
    return any(pat.search(url) for pat, _tmpl in _JOB_POSTING_PATTERNS)


def _posting_url_to_board_root(url: str) -> str | None:
    """BUG-74: derive the company's job-board root from a posting URL.

    Returns None when the URL is not a recognized posting or the board root
    is not derivable (embedded gh_jid boards, generic numeric-id paths).
    """
    for pat, tmpl in _JOB_POSTING_PATTERNS:
        m = pat.search(url)
        if m:
            return tmpl.format(**m.groupdict()) if tmpl else None
    return None


# BUG-77: third-party job aggregators / VC talent boards whose URLs pass the
# substring heuristics but are NOT the company's own career site (stale,
# incomplete, or multi-company listings). Rejected at discovery time only —
# existing sheet rows are user-owned and merely flagged by --audit.
# Deliberately NOT listed: jobs.gem.com (Gem is a real ATS product — user-
# verified boards exist on it) and the _VC_PORTFOLIO_HOSTS unwrap path.
_AGGREGATOR_HOSTS = frozenset({
    "builtin.com", "ycombinator.com", "workatastartup.com", "iitjobs.com",
    "consider.com", "wellfound.com", "angel.co", "indeed.com",
    "glassdoor.com", "ziprecruiter.com", "simplyhired.com", "otta.com",
    "linkedin.com", "instahyre.com",
})


def _is_aggregator_url(url: str) -> bool:
    """BUG-77: hostname suffix match against _AGGREGATOR_HOSTS (never
    substring — 'builtin.com' must not match 'rebuiltin.com')."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS)


def _is_likely_career_url(url: str) -> bool:
    """Heuristic: does this URL look like a career/jobs page?"""
    u = url.lower()
    if _is_job_posting_url(u):      # BUG-74: single posting ≠ career page
        return False
    if _is_aggregator_url(u):       # BUG-77: aggregator ≠ company's own site
        return False
    return (any(d in u for d in _CAREER_DOMAINS) or
            any(kw in u for kw in _CAREER_PATH_KWDS))


# Hosts whose /jobs/<slug> path wraps a single underlying portfolio company.
# When career_url points at one of these, the slug names the real company —
# extract it and re-resolve to that company's actual ATS via slug-probing.
_VC_PORTFOLIO_HOSTS = {
    "jobs.a16z.com", "jobs.battery.com", "jobs.gaingels.com", "jobs.01a.com",
}

def _unwrap_career_url(url: str) -> str | None:
    """Extract underlying company name hint from a wrapper career URL.

    Handles two wrapper families that appear in our company list but aren't
    real ATS endpoints:
      - linkedin.com/jobs/<slug>-jobs           → "<slug>" (hyphens → spaces)
      - linkedin.com/company/<slug>/jobs[/...]  → "<slug>"
      - jobs.<vc>.com/jobs/<slug>[?...]         → "<slug>"  (vc in _VC_PORTFOLIO_HOSTS)

    Returns None for normal ATS URLs, raw company sites, or unsupported
    LinkedIn forms (e.g. linkedin.com/jobs/view/<id> has no name to extract).
    """
    if not url:
        return None
    u = url.lower().strip()
    # LinkedIn jobs page: linkedin.com/jobs/<slug>-jobs (trailing "-jobs")
    m = re.search(r"linkedin\.com/jobs/([a-z0-9][a-z0-9\-]*?)-jobs(?:/|$|\?)", u)
    if m:
        return m.group(1).replace("-", " ").strip()
    # LinkedIn company-jobs page: linkedin.com/company/<slug>/jobs
    m = re.search(r"linkedin\.com/company/([a-z0-9][a-z0-9\-]*)/jobs", u)
    if m:
        return m.group(1).replace("-", " ").strip()
    # VC-portfolio wrappers: jobs.<vc>.com/jobs/<slug>
    for host in _VC_PORTFOLIO_HOSTS:
        m = re.search(re.escape(host) + r"/jobs/([^/?#]+)", u)
        if m:
            return m.group(1).replace("-", " ").strip()
    return None


def validate_career_url(url: str) -> bool:
    """Return True if the URL resolves successfully (HTTP < 400)."""
    if not url or url in ("N/A", "") or not url.startswith("http"):
        return False
    try:
        r = requests.get(
            url, timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 Chrome/120 Safari/537.36"},
            allow_redirects=True,
        )
        if r.status_code < 400:
            return True
        logging.warning(f"[URLCheck] {url} → HTTP {r.status_code}")
        return False
    except Exception as e:
        logging.warning(f"[URLCheck] {url} → {e}")
        return False


# ── ATS slug helpers ──────────────────────────────────────────────────────────
def _slug_candidates(company_name: str) -> list:
    name = company_name.lower().strip()

    # Generate slugs BEFORE stripping suffixes so "Scale AI" → "scale-ai" is kept
    pre_hyphen  = re.sub(r'[^a-z0-9]+', '-', name).strip('-')
    pre_nospace = re.sub(r'[^a-z0-9]+', '', name)

    # endswith-strip (longest first, repeated for stacked suffixes), NOT
    # str.replace: replace(" corp") also eats the front of " corporation"
    # mid-name ("rocket lab corporation" → "rocket laboration"), producing
    # corrupted slugs (BUG-75).
    _suffixes = [" corporation", " technologies", " inc.", " inc", " llc",
                 " ltd", " corp", " ai"]
    stripped = True
    while stripped:
        stripped = False
        for suffix in _suffixes:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)].strip()
                stripped = True
    name     = name.strip()
    hyphen   = re.sub(r'[^a-z0-9]+', '-', name).strip('-')
    nospace  = re.sub(r'[^a-z0-9]+', '', name)
    parts    = hyphen.split('-')

    # BUG-75: bare single words from multi-word names ("Physical Intelligence"
    # → "physical", "intelligence") collide with unrelated companies' boards.
    # Only emit them when the whole (suffix-stripped) name IS one word — in
    # which case they equal `hyphen` and dedup away anyway.
    seen, out = set(), []
    bare = [parts[-1], parts[0]] if len(parts) == 1 else []
    for c in [hyphen, nospace, pre_hyphen, pre_nospace] + bare:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out


def _check_ats_slug(slug: str, validator: dict) -> tuple:
    url = validator["api_template"].format(slug=slug)
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "PathFinder/1.0"})
        if r.status_code != 200:
            return False, 0
        data = r.json()
        key  = validator["jobs_key"]
        n    = len(data) if key is None else len(data.get(key, []))
        return n > 0, n
    except Exception:
        return False, 0


def _find_ats_url(company_name: str) -> str | None:
    """Try Greenhouse then Lever slug patterns for the company."""
    for slug in _slug_candidates(company_name)[:3]:
        for v in ATS_VALIDATORS:
            hit, _ = _check_ats_slug(slug, v)
            if hit:
                # BUG-75: the board exists — but is it THIS company's?
                if not _ats_board_belongs_to_company(slug, v, company_name):
                    continue
                return v["board_template"].format(slug=slug)
            time.sleep(0.2)
    return None


# ── Homepage scraping ─────────────────────────────────────────────────────────
def _homepage_belongs_to_company(html: str, company_name: str) -> bool:
    """BUG-75: does this homepage identify as `company_name`?

    The domain was GUESSED from the name ("Aether" → aethergroup.com), so any
    200 response may be a completely unrelated site. Require a compact form
    of some full-name slug candidate to appear in the page <title> or
    og:site_name. No identity text → reject (the domain is only a guess —
    unlike ATS slug probes there is no existence signal to fall back on).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False
    texts = []
    if soup.title and soup.title.string:
        texts.append(soup.title.string)
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content"):
        texts.append(og["content"])
    blob = re.sub(r'[^a-z0-9]', '', " ".join(texts).lower())
    if not blob:
        return False
    for c in _slug_candidates(company_name):
        cc = re.sub(r'[^a-z0-9]', '', c)
        if cc and cc in blob:
            return True
    return False


def _scrape_homepage_for_career_link(company_name: str) -> str | None:
    """Guess company domain, scrape homepage, extract first valid career link."""
    name_clean = re.sub(r'[^a-z0-9]', '', company_name.lower().strip())
    domain_guesses = [
        f"https://www.{name_clean}.com",
        f"https://{name_clean}.com",
        f"https://{name_clean}.ai",
    ]
    hdrs = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    for base in domain_guesses:
        try:
            r = requests.get(base, timeout=10, headers=hdrs, allow_redirects=True)
            if r.status_code >= 400:
                continue
            if not _homepage_belongs_to_company(r.text, company_name):
                logging.info(f"[Homepage] {base}: page does not identify as "
                             f"{company_name!r} — skipped (BUG-75)")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True).lower()
                href_lower = href.lower()
                if any(kw in href_lower or kw in text
                       for kw in ["career", "job", "hiring", "work with us", "join us", "join"]):
                    full = urljoin(r.url, href)
                    if full.startswith("http") and validate_career_url(full):
                        return full
        except Exception:
            pass
    return None


# ── Multi-strategy career URL finder ─────────────────────────────────────────
def find_career_url(company_name: str, tavily_client) -> str | None:
    """
    Find a valid career URL for a company using multiple strategies:
      1. KNOWN_CAREER_URLS exact match
      2. Tavily targeted ATS search
      3. Tavily general careers search
      4. ATS slug probing (Greenhouse + Lever)
      5. Company homepage scraping
    """
    name_lower = company_name.lower().strip()

    # 1. Known career URLs (case-insensitive exact match)
    for k, v in KNOWN_CAREER_URLS.items():
        if k.lower() == name_lower:
            if validate_career_url(v):
                logging.info(f"[CareerURL] {company_name}: known URL → {v}")
                return v

    # 2. Tavily: ATS-specific search
    ats_query = (
        f'"{company_name}" careers jobs '
        f'site:greenhouse.io OR site:lever.co OR site:ashbyhq.com OR site:myworkdayjobs.com'
    )
    url = _tavily_extract_career_url(company_name, ats_query, tavily_client)
    if url:
        logging.info(f"[CareerURL] {company_name}: Tavily ATS → {url}")
        return url

    # 3. Tavily: general careers search
    gen_query = f'"{company_name}" careers hiring jobs 2026 official career page'
    url = _tavily_extract_career_url(company_name, gen_query, tavily_client)
    if url:
        logging.info(f"[CareerURL] {company_name}: Tavily general → {url}")
        return url

    # 4. ATS slug probing
    url = _find_ats_url(company_name)
    if url:
        logging.info(f"[CareerURL] {company_name}: ATS slug → {url}")
        return url

    # 5. Homepage scraping
    url = _scrape_homepage_for_career_link(company_name)
    if url:
        logging.info(f"[CareerURL] {company_name}: homepage scrape → {url}")
        return url

    return None


def _tavily_result_matches_company(url: str, company_name: str) -> bool:
    """BUG-78: identity check for a Tavily search-result URL.

    Tavily happily returns career pages of OTHER companies that merely
    mention the query term (observed live: "Zep AI" → Automattic's
    greenhouse, "Akamai" → Advance Auto Parts' Workday, "Meta" → QVC's
    Workday). Slug probes and homepage guesses got ownership gates in
    BUG-75; this closes the same hole on the search path:
      - ATS board URL   → org-name ownership check (fail-open on fetch error)
      - Workday URL     → subdomain equality check
      - anything else   → a company slug candidate must appear in the
                          hostname (positive-evidence requirement; rejection
                          just falls through to the next strategy)
    """
    parsed = _parse_ats_board(url)
    if parsed:
        v, slug = parsed
        return _ats_board_belongs_to_company(slug, v, company_name)
    if re.match(r"https?://[^.]+\.wd\d+\.myworkdayjobs\.com", url, re.IGNORECASE):
        return _workday_subdomain_matches_company(url, company_name)
    host = (urlparse(url).hostname or "").lower()
    host_compact = re.sub(r'[^a-z0-9]', '', host)
    # Registrable-domain core, e.g. "careers.doordash.com" → "doordash",
    # "cadence.com" → "cadence".
    labels = host.split('.')
    core = re.sub(r'[^a-z0-9]', '', labels[-2] if len(labels) >= 2 else labels[0])
    candidates = set(_slug_candidates(company_name))
    candidates.add(_normalize_company_name(company_name))
    for c in candidates:
        cc = re.sub(r'[^a-z0-9]', '', c)
        if not cc:
            continue
        # Company name appears in the hostname, OR the domain core is the
        # leading part of the company name ("cadence.com" ↔ "Cadence Design
        # Systems"). Never the reverse (host core starting with the name
        # would accept "claycounty.gov" for "Clay").
        if cc in host_compact:
            return True
        if core and (cc == core or (len(core) >= 4 and cc.startswith(core))):
            return True
    return False


def _tavily_extract_career_url(company_name: str, query: str, client) -> str | None:
    """Run one Tavily query and return the first validated career URL found."""
    try:
        r = client.search(query=query, search_depth="basic", max_results=5)
        for item in r.get("results", []):
            url = item.get("url", "")
            # BUG-74: a posting URL is upgraded to its board root when
            # derivable, instead of being stored raw (pre-fix) or lost.
            root = _posting_url_to_board_root(url)
            candidate = root or url
            if (_is_likely_career_url(candidate)
                    and _tavily_result_matches_company(candidate, company_name)  # BUG-78
                    and validate_career_url(candidate)):
                return candidate
    except Exception as e:
        err_str = str(e)
        if "402" in err_str or "429" in err_str or "quota" in err_str.lower():
            logging.error(f"[CareerURL] Tavily quota exhausted for {company_name}: {e}")
        else:
            logging.warning(f"[CareerURL] Tavily query failed for {company_name}: {e}")
    return None


# BUG-75: words that board pages append around the org name ("OpenAI Jobs",
# "Careers at Stripe") — stripped before comparing identities.
_BOARD_TITLE_NOISE = re.compile(
    r'\b(jobs?|careers?|job board|hiring|current openings|openings|at)\b',
    re.IGNORECASE,
)


def _org_name_matches_company(org_name: str, company_name: str) -> bool:
    """BUG-75: does a board's org name identify `company_name`?

    Generalization of the Workday subdomain matcher: compare the compact
    forms of every slug candidate of both names for exact EQUALITY only —
    no substring/prefix matching. Substring rules let "apple" → "applebank",
    "clay" → "claycountybcc", "western" → "westernunion" all leak through,
    which produces wrong-company URLs.
    """
    if not org_name or not company_name:
        return False
    cleaned = _BOARD_TITLE_NOISE.sub(' ', org_name)

    def _compacts(name: str) -> set:
        # Slug candidates + the dedup-normalized form (word-boundary suffix
        # strip: labs/systems/platform/… — so "Lambda Labs" matches a board
        # named "Lambda"). Still equality-only after normalization.
        out = {re.sub(r'[^a-z0-9]', '', c) for c in _slug_candidates(name)}
        out.add(re.sub(r'[^a-z0-9]', '', _normalize_company_name(name)))
        out.discard('')
        return out

    return bool(_compacts(cleaned) & _compacts(company_name))


def _workday_subdomain_matches_company(url: str, company_name: str) -> bool:
    """Validate that a Workday URL's subdomain belongs to `company_name`.

    Tavily's `site:myworkdayjobs.com` query happily returns URLs from
    *other* companies that mention `company_name` somewhere in their JD
    (e.g. "AMD" → `argonne.wd1.myworkdayjobs.com`, "Oracle" → `pwc.wd3...`).
    Reject any result whose subdomain doesn't approximately match the
    company name's slug candidates. Delegates to _org_name_matches_company
    (the subdomain IS the org identity here).
    """
    m = re.match(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com", url, re.IGNORECASE)
    if not m:
        return False
    return _org_name_matches_company(m.group(1), company_name)


def _fetch_ats_org_name(slug: str, validator: dict) -> str | None:
    """BUG-75: fetch the org/account name that owns `slug` on this platform.

    Returns None when the platform config has no org source or the fetch /
    parse fails — callers treat None as \"unknown\" (fail-open), never as a
    mismatch.
    """
    try:
        if "org_api" in validator:
            r = requests.get(validator["org_api"].format(slug=slug),
                             timeout=8, headers={"User-Agent": "PathFinder/1.0"})
            if r.status_code != 200:
                return None
            name = r.json().get(validator["org_key"]) or ""
            return name.strip() or None
        if "org_html" in validator:
            r = requests.get(
                validator["org_html"].format(slug=slug), timeout=8,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                       "AppleWebKit/537.36 Chrome/120 Safari/537.36"},
                allow_redirects=True,
            )
            if r.status_code != 200:
                return None
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
            if not m:
                return None
            return re.sub(r'\s+', ' ', m.group(1)).strip() or None
    except Exception:
        pass
    return None


def _ats_board_belongs_to_company(slug: str, validator: dict, company_name: str) -> bool:
    """BUG-75: ownership gate for a slug-probe hit.

    Fail-closed on a fetched org name that mismatches (that IS the bug being
    fixed); fail-open when the org name can't be fetched — existence was
    already verified by _check_ats_slug, and _slug_candidates no longer emits
    the collision-prone bare single words.
    """
    org = _fetch_ats_org_name(slug, validator)
    if org is None:
        return True
    # A noise-only title ("Jobs", "Careers") carries no identity → unknown,
    # not a mismatch.
    if not re.sub(r'[^a-z0-9]', '', _BOARD_TITLE_NOISE.sub('', org.lower())):
        return True
    if _org_name_matches_company(org, company_name):
        return True
    logging.info(f"[ATSOwner] {validator['platform']}/{slug}: board owner "
                 f"{org!r} != company {company_name!r} — rejected")
    return False


def _find_workday_url(company_name: str, tavily_client) -> str | None:
    """Tavily-search for a company's Workday board URL.

    Workday subdomains are unguessable (e.g. `nvidia.wd5`, `arista.wd1`,
    `intel.wd1`) so slug-probing can't find them. This helper queries Tavily
    with a Workday-scoped site filter and returns the first result whose URL
    is on `myworkdayjobs.com` AND whose subdomain matches the company name.
    The subdomain check is critical: Tavily will otherwise return any
    Workday URL whose page mentions the company (e.g. "AMD" → Argonne Lab
    job postings that reference AMD silicon).

    Returns None if Tavily yields no validated Workday match or on any error.
    """
    if not tavily_client:
        return None
    query = f'"{company_name}" careers site:myworkdayjobs.com'
    try:
        r = tavily_client.search(query=query, search_depth="basic", max_results=5)
        for item in r.get("results", []):
            url = (item.get("url") or "").strip()
            if ("myworkdayjobs.com" in url
                    and _workday_subdomain_matches_company(url, company_name)
                    and validate_career_url(url)):
                return url
    except Exception as e:
        err_str = str(e)
        if "402" in err_str or "429" in err_str or "quota" in err_str.lower():
            logging.error(f"[Workday] Tavily quota exhausted for {company_name}: {e}")
        else:
            logging.warning(f"[Workday] Tavily query failed for {company_name}: {e}")
    return None


# ── Company name dedup helpers ────────────────────────────────────────────────
_COMPANY_SUFFIXES = re.compile(
    r'\b(inc|corp|corporation|llc|ltd|technologies|labs?|'
    r'ai|platform|systems|computing)\b\.?',
    re.IGNORECASE,
)


def _normalize_company_name(name: str) -> str:
    """Normalize company name for dedup: lowercase, strip, remove common suffixes."""
    n = name.lower().strip()
    n = _COMPANY_SUFFIXES.sub('', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _is_duplicate_company(
    candidate: str,
    existing_names: set[str],
    existing_normalized: set[str] | None = None,
) -> bool:
    """Check if candidate is a duplicate of any existing company name.

    Strategy:
      1. Exact case-insensitive match
      2. Normalized name match (stripped common suffixes)
      3. Bidirectional startswith check (min 4 chars to avoid short-name collisions)
    """
    cand_lower = candidate.lower().strip()

    # 1. Exact case-insensitive
    if cand_lower in {n.lower() for n in existing_names}:
        return True

    # 2. Normalized match
    cand_norm = _normalize_company_name(candidate)
    if existing_normalized is None:
        existing_normalized = {_normalize_company_name(n) for n in existing_names}
    if cand_norm in existing_normalized:
        return True

    # 3. Bidirectional startswith (only if shortest name >= 4 chars)
    for existing in existing_names:
        ex_lower = existing.lower().strip()
        shorter = min(len(cand_lower), len(ex_lower))
        if shorter < 4:
            continue
        if cand_lower.startswith(ex_lower) or ex_lower.startswith(cand_lower):
            return True

    return False


# ── Company discovery ─────────────────────────────────────────────────────────
def compute_need_by_track(existing_rows: list) -> dict:
    """PRJ-004 REQ-004-01: open discovery slots per bucket.

    need[bucket] = max(0, quota - current_count) — a bucket at/over quota
    (grandfathered survivors, D-12) gets 0 and is never trimmed. Rows whose
    Track value is not one of the 6 buckets (unmigrated) count toward no
    bucket; they are surfaced with a warning and resolved by --migrate-tracks.
    """
    counts = {t: 0 for t in TRACK_VALUES}
    unmigrated = 0
    for row in existing_rows:
        track = str(row[1] if len(row) > 1 else "").strip()
        if track in counts:
            counts[track] += 1
        elif row and row[0]:
            unmigrated += 1
    if unmigrated:
        logging.warning(f"[Track] {unmigrated} Company_List row(s) have an "
                        "unmigrated Track value — run "
                        "`python agents/company_agent.py --migrate-tracks`.")
    return {t: max(0, TRACK_QUOTAS[t] - counts[t]) for t in TRACK_VALUES}


def allocate_batch(need_by_track: dict, batch_size: int) -> dict:
    """Allocate one run's discovery budget across open buckets, proportional
    to open slots (largest-remainder rounding), capped at batch_size total."""
    total_open = sum(need_by_track.values())
    if total_open <= batch_size:
        return dict(need_by_track)
    shares = {t: n * batch_size / total_open for t, n in need_by_track.items()}
    alloc  = {t: int(s) for t, s in shares.items()}
    leftover = batch_size - sum(alloc.values())
    for t in sorted(shares, key=lambda t: shares[t] - alloc[t], reverse=True):
        if leftover <= 0:
            break
        if alloc[t] < need_by_track[t]:
            alloc[t] += 1
            leftover -= 1
    return alloc


def _apply_bucket_rules(companies: list, need_by_track: dict) -> list:
    """PRJ-004 REQ-004-03 + quota trim, deterministic (never trust the LLM for
    hard exclusions): drop Defense-bucket legacy primes (unless allowlisted),
    and cap accepted companies per bucket at that bucket's open slots."""
    remaining = dict(need_by_track)
    out = []
    for c in companies:
        name  = _normalize_company_name(c.get("company_name", ""))
        track = c.get("track", "")
        if track == "Defense" and name in DEFENSE_EXCLUDED_PRIMES \
                and name not in DEFENSE_ALLOWLIST:
            logging.info(f"[BucketRules] Dropped defense legacy prime: {name}")
            continue
        if remaining.get(track, 0) <= 0:
            logging.info(f"[BucketRules] Bucket {track!r} full — dropped {name}")
            continue
        remaining[track] -= 1
        out.append(c)
    return out


def discover_ai_companies(tavily_client, existing_names: set,
                          need_by_track: dict) -> list:
    """
    Discover new companies not in `existing_names`, up to each track's open
    slots in `need_by_track` ({track: slots}). Returns a list of dicts with
    keys: company_name, track, business_focus, career_url.

    `tavily_client` is any TavilyClient-like object with .search() — normally
    a shared.tavily_pool.TavilyKeyPool (key rotation on quota errors).

    Flow:
      1. Tavily batch search → raw article/news results
      2. Gemini extracts structured company info (NO URL generation)
      3. Deterministic bucket rules (defense prime exclusion, quota trim)
      4. Per-company multi-strategy career URL lookup
    """
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking discover_ai_companies()")

    need = sum(need_by_track.values())
    logging.info(f"Searching for {need} new companies across "
                 f"{sum(1 for v in need_by_track.values() if v)} open buckets "
                 f"(existing: {len(existing_names)})...")
    client   = tavily_client
    raw, seen_urls = [], set()

    # Step 1: Batch Tavily search for company discovery
    for q in TAVILY_QUERIES:
        try:
            # Launch tune (G1 yield): 20 results/query — static queries exhaust
            # fast against a large known-list; wider fetch feeds dedup better.
            r = client.search(query=q, search_depth="advanced", max_results=20)
            for item in r.get("results", []):
                u = item.get("url", "")
                if u not in seen_urls:
                    seen_urls.add(u)
                    raw.append(item)
        except Exception as e:
            err_str = str(e)
            if "402" in err_str or "429" in err_str or "quota" in err_str.lower():
                print(f"  ⚠️  Tavily API quota exhausted: {e}")
                logging.error(f"Tavily quota exhausted: {e}")
                break  # no point continuing with more queries
            logging.warning(f"Tavily query failed: {e}")

    results = raw[:120]
    logging.info(f"Tavily: {len(results)} deduplicated results collected.")

    # Step 2: Gemini extracts company info only — no URL generation
    existing_list = sorted(existing_names)[:450]  # launch tune: cover the full known universe
    logging.info("Feeding context to Gemini for company extraction (no URL generation)...")
    _bucket_guidance = {
        "AI-native": (
            "AI-native companies of any size — frontier labs, AI product startups, "
            "AI infrastructure/compute (OpenAI, Anthropic, xAI, Perplexity, Scale AI, "
            "Glean, Sierra, CoreWeave, Groq, Together AI, Cerebras, etc.). Founded "
            "~2000 or later. NO tiny pre-seed companies — real headcount and a "
            "public job board."),
        "Mid-large Tech": (
            "established mid-large tech companies with strong TPM organizations, "
            "especially those with early-bet divisions (Google, Microsoft, Amazon, "
            "Meta, Apple, NVIDIA, Databricks, Snowflake, Uber, Netflix, Salesforce, "
            "ServiceNow, etc.). No founding-year restriction — this is the only "
            "bucket where legacy incumbents belong."),
        "Robotics": (
            "robotics companies founded ~2000 or later (Figure AI, Apptronik, "
            "Physical Intelligence, Skild, Zipline, Nuro, Waymo, etc.). EXCLUDE "
            "legacy incumbents (e.g. ABB, Boston Dynamics' parent conglomerates) — "
            "those belong in Mid-large Tech if anywhere."),
        "Fintech": (
            "fintech companies founded ~2000 or later (Stripe, Plaid, Ramp, Brex, "
            "Chime, Affirm, Block, Coinbase, etc.). EXCLUDE legacy incumbents "
            "(Visa, PayPal, Intuit, Mastercard) — those belong in Mid-large Tech "
            "if anywhere."),
        "Space": (
            "space companies founded ~2000 or later (SpaceX, Relativity Space, "
            "Stoke Space, Rocket Lab, Firefly, Astranis, Varda, True Anomaly, "
            "etc.) — prefer these over incumbent space divisions (ULA, Boeing "
            "space)."),
        "Defense": (
            "venture-backed defense tech founded roughly 2010 or later (Anduril, "
            "Shield AI, Saronic, Castelion, Epirus, Vannevar Labs, etc.). "
            "Palantir IS in scope (explicit exception). NEVER include legacy "
            "primes: Boeing, Lockheed Martin, Raytheon/RTX, Northrop Grumman, "
            "General Dynamics, BAE, L3Harris."),
    }
    distribution = "\n".join(
        f'  - "{track}": {n} companies — {_bucket_guidance[track]}'
        for track, n in need_by_track.items() if n > 0
    )
    config = types.GenerateContentConfig(
        system_instruction=(
            "You are an expert technology-industry analyst. From the web search "
            f"context, extract exactly {need} distinct companies that are NOT in "
            "the EXISTING_COMPANIES list. Every company must have a real TPM "
            "(Technical Program Manager) hiring function.\n"
            "GEOGRAPHY: a company qualifies if it HIRES TPMs in Washington state "
            "(Seattle metro or anywhere in WA), California (Bay Area or Southern "
            "California — El Segundo, Hawthorne, Long Beach, Irvine, San Diego), "
            "or Texas, OR hires TPMs into US-remote roles. \"Space\"-track "
            "companies additionally qualify via Florida hiring (Space Coast — "
            "Cape Canaveral, Merritt Island, Titusville, Melbourne). "
            "Qualification is by hiring footprint in these regions, NOT by HQ "
            "location.\n"
            "Follow this bucket distribution strictly (use the EXACT label string "
            "for the track field):\n"
            f"{distribution}\n"
            "The track field MUST be one of the 6 exact strings — do not invent "
            "variants.\n"
            "For business_focus: write 3-4 sentences covering what the company "
            "builds, who their customers are, their key competitive differentiator, "
            "and notable products/funding/traction.\n"
            "Set confident=false when the search context is too thin to determine "
            "the track or write an accurate business_focus — do not guess. When "
            "confident=false, return an empty business_focus rather than a guessed "
            "one.\n"
            "DO NOT include any career_url or URL fields — URLs are handled separately."
            + SECURITY_CLAUSE  # P0-3: Tavily snippets are untrusted third-party content
        ),
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=CompanyInfoList,
    )
    prompt = (
        f"Search context:\n<scraped_content>\n{json.dumps(results)}\n</scraped_content>\n\n"
        f"EXISTING_COMPANIES (DO NOT include these):\n{json.dumps(existing_list)}\n\n"
        f"Extract exactly {need} NEW companies with: company_name, "
        f"track (one of: {' / '.join(TRACK_VALUES)}), "
        "business_focus (3-4 sentences)."
    )
    try:
        resp      = _KEY_POOL.generate_content(model=MODEL, contents=prompt, config=config)
        companies = json.loads(resp.text).get("companies", [])
    except Exception as e:
        logging.error(f"Company extraction failed: {e}")
        return []

    # Filter out any Gemini accidentally returned in existing list
    # BUG-49: use local copies to avoid mutating the caller's set
    local_names = set(existing_names)
    existing_normalized = {_normalize_company_name(n) for n in local_names}
    filtered = []
    for c in companies:
        name = c.get("company_name", "").strip()
        if not name:
            continue
        if _is_duplicate_company(name, local_names, existing_normalized):
            logging.info(f"[Dedup] Skipped duplicate: {name}")
            continue
        # Add to tracking sets so later items in this batch also dedup
        local_names.add(name)
        existing_normalized.add(_normalize_company_name(name))
        filtered.append(c)
    logging.info(f"After dedup filter: {len(filtered)} new companies.")

    # Step 2.5: deterministic bucket rules (defense prime exclusion + quota
    # trim) BEFORE career-URL discovery, so excluded companies never spend
    # Tavily/validation calls.
    filtered = _apply_bucket_rules(filtered, need_by_track)
    logging.info(f"After bucket rules: {len(filtered)} companies.")

    # BUG-76: unconfident extractions keep the row (name + URL are still
    # useful) but blank Track/Business Focus so the confident-gated repair
    # paths (run_reenrich_business_focus / run_enrich_missing_tracks) refill
    # them with real search context. Blanked AFTER bucket rules on purpose:
    # the guessed bucket's slot is consumed this run; the next run's recount
    # treats the row as unmigrated and self-corrects.
    for c in filtered:
        if not c.get("confident", False):
            logging.info(f"[Confident] {c.get('company_name', '?')}: unconfident "
                         "extraction — Track/Business Focus blanked for re-enrich.")
            c["track"] = ""
            c["business_focus"] = ""

    # Step 3: Per-company career URL discovery
    validated = []
    for c in filtered:
        name = c.get("company_name", "")
        logging.info(f"[CareerURL] Finding URL for: {name}")
        url = find_career_url(name, client)
        if url:
            c["career_url"] = url
            validated.append(c)
            logging.info(f"[CareerURL] ✅ {name} → {url}")
        else:
            logging.warning(f"[CareerURL] ❌ {name}: no valid career URL found — SKIPPED")
        time.sleep(0.5)

    logging.info(f"Career URL discovery: {len(validated)}/{len(filtered)} companies kept.")
    return validated


# ── ATS upgrade (Phase 1.5) ───────────────────────────────────────────────────
def validate_and_upgrade_ats_url(company_name: str, current_url: str,
                                  tavily_client=None) -> str:
    """Return upgraded ATS board URL, or original if no ATS found.

    When `tavily_client` is provided, falls back to a Workday-scoped Tavily
    search for companies whose slug-probe yields no Greenhouse/Lever/Ashby/
    Workable match — recovers cos with unguessable Workday subdomains
    (e.g. `nvidia.wd5`, `arista.wd1`).
    """
    name_lower = company_name.lower().strip()

    # 1. Hard-coded overrides (highest priority)
    # Use word-boundary matching (not substring) to avoid "xai" matching "MaxAI" etc.
    for kw, url in KNOWN_ATS_OVERRIDES.items():
        if re.search(r'(?:^|\s)' + re.escape(kw) + r'(?:\s|$)', name_lower):
            logging.info(f"[Phase1.5] {company_name}: override → {url}")
            return url

    # 1.5. Wrapper URL (LinkedIn / VC-portfolio) → re-resolve via underlying slug
    hint = _unwrap_career_url(current_url)
    if hint:
        for slug in _slug_candidates(hint):
            for v in ATS_VALIDATORS:
                hit, n = _check_ats_slug(slug, v)
                if hit and _ats_board_belongs_to_company(slug, v, hint):  # BUG-75
                    upgraded = v["board_template"].format(slug=slug)
                    logging.info(f"[Phase1.5] {company_name}: unwrapped "
                                 f"'{current_url}' → {v['platform']}/{slug} "
                                 f"jobs={n} → {upgraded}")
                    return upgraded
                time.sleep(0.3)
        logging.info(f"[Phase1.5] {company_name}: unwrap hint '{hint}' "
                     f"yielded no ATS match")

    # 2. Already an ATS URL
    if any(d in current_url for d in ["greenhouse.io", "lever.co", "ashbyhq.com",
                                       "myworkdayjobs.com", "workable.com"]):
        # BUG-74: a single-posting URL on an ATS domain used to short-circuit
        # here unchanged, freezing it forever. Strip it to its board root
        # when derivable and reachable; otherwise leave it (flag-only case).
        if _is_job_posting_url(current_url):
            root = _posting_url_to_board_root(current_url)
            if root and validate_career_url(root):
                logging.info(f"[Phase1.5] {company_name}: posting URL "
                             f"stripped to board root → {root}")
                return root
        return current_url

    # 3. Probe Greenhouse + Lever + Ashby + Workable slugs
    for slug in _slug_candidates(company_name):
        for v in ATS_VALIDATORS:
            hit, n = _check_ats_slug(slug, v)
            if hit and _ats_board_belongs_to_company(slug, v, company_name):  # BUG-75
                upgraded = v["board_template"].format(slug=slug)
                logging.info(f"[Phase1.5] {company_name}: {v['platform']} "
                             f"slug='{slug}' jobs={n} → {upgraded}")
                return upgraded
            time.sleep(0.3)

    # 4. Workday-via-Tavily fallback (unguessable subdomains)
    if tavily_client is not None:
        wd_url = _find_workday_url(company_name, tavily_client)
        if wd_url:
            logging.info(f"[Phase1.5] {company_name}: workday-via-tavily → {wd_url}")
            return wd_url

    return current_url


def run_phase_1_5(xlsx_path: str, tavily_client=None):
    """Backfill blank Career URLs on manually-inserted rows.

    BUG-74/75: filled Career URLs are user-verified data and are NEVER
    rewritten here (this step used to probe-and-overwrite every filled
    non-ATS URL). URL-quality findings for filled rows are report-only via
    `--audit`.

    `tavily_client` enables find_career_url's Tavily strategies. When None
    and TAVILY_API_KEY is set in env, a key pool is created automatically
    (BUG-70 rotation).
    """
    if tavily_client is None:
        try:
            tavily_client = build_tavily_pool_from_env()
        except Exception as e:
            logging.warning(f"[Phase1.5] Tavily pool init failed: {e}")
            tavily_client = None

    print("\n" + "="*60)
    print("PHASE 1.5: BLANK CAREER-URL BACKFILL (filled URLs are never rewritten)")
    print("="*60)
    if tavily_client is None:
        print("  ⚠️  No Tavily client — blank-URL backfill disabled this run.")
    rows = get_company_rows_with_row_num(xlsx_path)
    if not rows:
        print("⚠️  No companies found. Skipping.")
        return

    skipped_filled = backfilled = backfill_failed = 0

    for excel_row, row in rows:
        name = str(row[0]).strip() if row[0] else ""
        url  = str(row[3]).strip() if row[3] else ""
        if not name:
            continue

        # Manual-row override: row exists with a name but no Career URL
        # (typically inserted by hand). Discover one via find_career_url.
        if not url or url == "N/A":
            if tavily_client is None:
                print(f"  ⚠️  {name}: Career URL blank — Tavily disabled, skipping backfill.")
                backfill_failed += 1
                continue
            print(f"  🆕 {name}: Career URL blank — backfilling...")
            new_url = find_career_url(name, tavily_client)
            if new_url:
                update_company_career_url(xlsx_path, excel_row, new_url)
                print(f"  ✅ {name}: backfilled → {new_url}")
                backfilled += 1
            else:
                print(f"  ❌ {name}: backfill failed (no valid URL found).")
                backfill_failed += 1
            time.sleep(1)
            continue

        # BUG-74/75 write guard: FILLED Career URLs are user-verified data —
        # phase 1.5 never rewrites them (it used to probe-and-overwrite every
        # non-ATS URL here, silently replacing user-verified links with
        # unowned slug-probe hits). Blank-URL backfill above is the ONLY
        # write path. Upgrade opportunities are surfaced report-only by
        # `python agents/company_agent.py --audit` (URL Flags column).
        skipped_filled += 1

    print(f"\n  Backfilled={backfilled}  Backfill failed={backfill_failed}  "
          f"Filled rows left untouched={skipped_filled} (report-only via --audit)")
    print("="*60 + "\n")


def run_reenrich_business_focus(xlsx_path: str, tavily_client=None) -> dict:
    """BUG-69: fill blank/N-A Business Focus cells on existing Company_List rows.

    Blank focus rows arise when a manual insert or a mid-run Tavily/Gemini
    failure left the cell empty; unlike blank Career URLs (backfilled every
    run by run_phase_1_5) they previously never self-healed. Per incomplete
    row: one Tavily search for context, then one batched Gemini call (the
    migrate_tracks batch pattern) writes the 3-4 sentence focus back.

    Returns {"filled": int, "failed": int, "skipped": int}.
    """
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or "
                           "set _KEY_POOL before invoking run_reenrich_business_focus()")
    counts = {"filled": 0, "failed": 0, "skipped": 0}
    rows = get_incomplete_company_rows(xlsx_path)
    if not rows:
        return counts

    print("\n" + "="*60)
    print(f"BUSINESS FOCUS RE-ENRICH (BUG-69): {len(rows)} blank row(s)")
    print("="*60)

    if tavily_client is None:
        try:
            tavily_client = build_tavily_pool_from_env()
        except Exception as e:
            logging.warning(f"[Reenrich] Tavily pool init failed: {e}")
            tavily_client = None
    if tavily_client is None:
        print("  ⚠️  No Tavily client — Business Focus re-enrich skipped this run.")
        counts["skipped"] = len(rows)
        return counts

    # Step 1: one Tavily search per blank row; quota error aborts the loop
    # (BUG-44 pattern) but keeps whatever context was already gathered.
    contexts = []  # (excel_row, name, search_context)
    for rec in rows:
        name = rec["name"]
        try:
            r = tavily_client.search(
                query=f'"{name}" company products customers', max_results=3)
            snippets = [f"{item.get('title', '')}: {item.get('content', '')}"
                        for item in r.get("results", [])]
            contexts.append((rec["excel_row"], name, " | ".join(snippets)[:1500]))
            time.sleep(1)
        except Exception as e:
            err_str = str(e)
            if "402" in err_str or "429" in err_str or "quota" in err_str.lower():
                print(f"  ⚠️  Tavily API quota exhausted: {e} — "
                      f"re-enrich continues next run.")
                logging.error(f"[Reenrich] Tavily quota exhausted: {e}")
                break
            logging.warning(f"[Reenrich] Tavily search failed for {name}: {e}")
    counts["skipped"] = len(rows) - len(contexts)
    if not contexts:
        return counts

    # Step 2: batched Gemini extraction (migrate_tracks batch pattern).
    system_instruction = (
        "You write company profiles for a TPM job-search pipeline. For each "
        "input company, use ONLY its search context to write business_focus: "
        "3-4 sentences covering what the company builds, who their customers "
        "are, their key competitive differentiator, and notable "
        "products/funding/traction. Copy company_name verbatim. Return an "
        "empty business_focus when the context is too thin — do not guess."
        + SECURITY_CLAUSE  # Tavily snippets are untrusted third-party content
    )
    valid_names = {name for _, name, _ in contexts}
    BATCH = 25
    for b in range(0, len(contexts), BATCH):
        chunk = contexts[b : b + BATCH]
        payload = json.dumps([{"company_name": n, "search_context": ctx}
                              for _, n, ctx in chunk])
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=BusinessFocusList,
        )
        try:
            resp = _KEY_POOL.generate_content(
                model=MODEL,
                contents=("Write business_focus for these companies:\n"
                          f"<scraped_content>\n{payload}\n</scraped_content>"),
                config=config,
            )
            results = {f.get("company_name", "").strip(): f.get("business_focus", "")
                       for f in json.loads(resp.text).get("focuses", [])
                       if f.get("company_name", "").strip() in valid_names}
        except Exception as e:
            logging.error(f"[Reenrich] batch {b // BATCH + 1} failed: {e} — "
                          "rows stay blank; re-run to retry.")
            results = {}
        for excel_row, name, _ctx in chunk:
            focus = (results.get(name) or "").strip()
            if focus:
                update_company_business_focus(xlsx_path, excel_row, focus)
                print(f"  ✅ {name}: focus filled.")
                counts["filled"] += 1
            else:
                print(f"  ⚪ {name}: no usable focus (retried next run).")
                counts["failed"] += 1
        time.sleep(0.5)

    print(f"\n  Filled={counts['filled']}  Failed={counts['failed']}  "
          f"Skipped={counts['skipped']}")
    print("="*60 + "\n")
    return counts


# ── Track classification (shared by --migrate-tracks, per-run enrich, --audit) ─
_TRACK_BUCKET_DEFINITIONS = (
    '  - "AI-native": AI labs / AI product startups / AI infra-compute, founded ~2000+\n'
    '  - "Mid-large Tech": established mid-large tech companies (the only bucket '
    "where legacy incumbents belong, e.g. Boeing, Visa, PayPal, Intuit)\n"
    '  - "Robotics": robotics companies founded ~2000+\n'
    '  - "Fintech": fintech companies founded ~2000+\n'
    '  - "Space": space companies founded ~2000+\n'
    '  - "Defense": venture-backed defense tech founded roughly 2010+. Palantir '
    "belongs here (explicit exception). Legacy primes (Boeing, Lockheed Martin, "
    "Raytheon/RTX, Northrop Grumman, General Dynamics, BAE, L3Harris) NEVER "
    "belong here — classify them as Mid-large Tech.\n"
)

_TRACK_CLASSIFY_SYSTEM_INSTRUCTION = (
    "You classify companies into exactly one of 6 track buckets for a "
    "TPM job-search pipeline. Buckets:\n"
    + _TRACK_BUCKET_DEFINITIONS +
    "Return one TrackClassification per input company, company_name copied "
    "verbatim. Set confident=false when the business description is too thin "
    "to decide — do not guess."
    + SECURITY_CLAUSE  # P0-3: business_focus text originated from scraped web content
)


def _classify_tracks_batch(chunk: list[tuple[str, str]]) -> dict:
    """One batched Gemini classification call for [(name, business_focus)].

    Returns {company_name: classification dict}. Raises on API/parse failure —
    callers decide how a failed batch degrades (rows stay pending, retried on
    the next invocation).
    """
    payload = json.dumps([{"company_name": n, "business_focus": (f or "")[:600]}
                          for n, f in chunk])
    config = types.GenerateContentConfig(
        system_instruction=_TRACK_CLASSIFY_SYSTEM_INSTRUCTION,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=TrackClassificationList,
    )
    resp = _KEY_POOL.generate_content(
        model=MODEL,
        contents=("Classify these companies:\n"
                  f"<scraped_content>\n{payload}\n</scraped_content>"),
        config=config,
    )
    return {c.get("company_name", "").strip(): c
            for c in json.loads(resp.text).get("classifications", [])}


def run_enrich_missing_tracks(xlsx_path: str) -> dict:
    """Fill blank Track cells on existing Company_List rows.

    Manual name-only inserts have no Track; without one the job_agent falls
    to the strict Mid-large-Tech classifier path with a warning. Only blank/
    N-A cells are touched — custom values (including 'UNMIGRATED — manual
    review') are user decisions / the --migrate-tracks CLI's domain.
    Unconfident classifications stay blank and are retried next run; defense
    legacy primes are deterministically forced to Mid-large Tech.

    Runs after run_reenrich_business_focus so a freshly-filled Business Focus
    feeds the classifier. Returns {"filled": int, "failed": int}.
    """
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or "
                           "set _KEY_POOL before invoking run_enrich_missing_tracks()")
    counts = {"filled": 0, "failed": 0}
    pending = []   # (excel_row, name, business_focus)
    for excel_row, row in get_company_rows_with_row_num(xlsx_path):
        name  = str(row[0]).strip() if row and row[0] else ""
        track = str(row[1]).strip() if len(row) > 1 else ""
        focus = str(row[2]).strip() if len(row) > 2 else ""
        if not name or track.lower() not in ("", "n/a", "none"):
            continue
        pending.append((excel_row, name, focus))
    if not pending:
        return counts

    print("\n" + "="*60)
    print(f"TRACK ENRICH: {len(pending)} blank-Track row(s)")
    print("="*60)

    BATCH = 25
    for b in range(0, len(pending), BATCH):
        chunk = pending[b : b + BATCH]
        try:
            results = _classify_tracks_batch([(n, f) for _, n, f in chunk])
        except Exception as e:
            logging.error(f"[TrackEnrich] batch {b // BATCH + 1} failed: {e} — "
                          "rows stay blank; re-run to retry.")
            results = {}
        for excel_row, name, _focus in chunk:
            r = results.get(name)
            if r is None:
                print(f"  ⚪ {name}: no classification returned (retried next run).")
                counts["failed"] += 1
                continue
            if _normalize_company_name(name) in DEFENSE_EXCLUDED_PRIMES:
                # Deterministic: a prime can survive only in Mid-large Tech.
                new_track = "Mid-large Tech"
            elif not r.get("confident", False):
                print(f"  ⚪ {name}: unconfident classification (retried next run).")
                counts["failed"] += 1
                continue
            else:
                new_track = r["track"]
            update_company_track(xlsx_path, excel_row, new_track)
            print(f"  ✅ {name}: Track → {new_track}")
            counts["filled"] += 1
        time.sleep(0.5)

    print(f"\n  Filled={counts['filled']}  Failed={counts['failed']}")
    print("="*60 + "\n")
    return counts


# ── Discovery loop ────────────────────────────────────────────────────────────
def run_discovery_loop(xlsx_path: str, tavily_client, summary: RunSummary) -> int:
    """Discover new companies in BATCH_SIZE batches until every bucket hits
    quota (MAX_TOTAL total), a batch yields nothing new (Tavily quota
    exhausted / query-pool convergence / Gemini failure — all retried next
    run), or the MAX_TOTAL // BATCH_SIZE runaway cap is reached.

    `tavily_client` is any TavilyClient-like object with .search() — normally
    a shared.tavily_pool.TavilyKeyPool. The exclusion list is re-read from
    the sheet each iteration so freshly upserted companies dedup against the
    next batch. Returns total added.
    """
    total_added = 0
    max_iters = MAX_TOTAL // BATCH_SIZE
    iteration = 0
    while True:
        existing_rows     = get_company_rows(xlsx_path)
        names_main        = {str(r[0]).strip() for r in existing_rows if r[0]}
        names_without_tpm = get_company_names_without_tpm(xlsx_path)
        existing_names    = names_main | names_without_tpm

        need_by_track = compute_need_by_track(existing_rows)
        total_open = sum(need_by_track.values())
        if total_open <= 0:
            print(f"✅ All buckets at quota ({MAX_TOTAL} companies).")
            summary.note("Discovery stop: all buckets at quota.")
            break
        iteration += 1
        if iteration > max_iters:
            summary.note(f"Discovery stop: iteration cap ({max_iters}) reached "
                         f"({total_open} slots still open).")
            break

        alloc = allocate_batch(need_by_track, BATCH_SIZE)
        need  = sum(alloc.values())
        open_desc = ", ".join(f"{t}: {n}" for t, n in alloc.items() if n)
        print(f"🔍 Discovery batch {iteration}/{max_iters}: up to {need} new "
              f"companies ({open_desc}; total open slots: {total_open})...")
        summary.attempted += need

        companies = discover_ai_companies(tavily_client, existing_names, alloc)
        if not companies:
            print("⚠️  Batch yielded no new companies — stopping discovery this run.")
            summary.failed += need
            summary.note(f"Discovery stop: zero-yield batch {iteration} "
                         f"({total_open} slots still open).")
            break

        data = [c.model_dump() if hasattr(c, "model_dump") else dict(c)
                for c in companies]
        upsert_companies(xlsx_path, data)
        total_added += len(data)
        summary.succeeded += len(data)
        summary.failed += max(0, need - len(data))
        print(f"✅ Batch {iteration}: added {len(data)} companies. "
              f"Total: {count_company_rows(xlsx_path)}")
    return total_added


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _KEY_POOL
    summary = RunSummary(agent="company")
    try:
        tavily_pool = build_tavily_pool_from_env()  # BUG-70: key rotation
        gemini_keys = [k for k in [
            os.getenv("GEMINI_API_KEY"),
            os.getenv("GEMINI_API_KEY_2"),
        ] if k]
        missing = []
        if not gemini_keys:
            missing.append("GEMINI_API_KEY")
        if tavily_pool is None:
            missing.append("TAVILY_API_KEY")
        if missing:
            print(f"❌ Missing env vars: {missing}")
            summary.note(f"Missing env vars: {missing}")
            return

        _KEY_POOL = _GeminiKeyPoolBase(gemini_keys, genai_mod=genai)
        logging.info(f"[KeyPool] Loaded {len(gemini_keys)} Gemini API key(s).")

        print("\n" + "="*60)
        print("COMPANY AGENT")
        print("="*60 + "\n")

        xlsx_path = get_or_create_excel()
        print(f"📊 Dashboard: {xlsx_path}")

        # ── Step 1: Discovery loop to quota (PRJ-004 REQ-004-01 / D-12) ──────────
        # Per iteration: recount per-bucket slots (a bucket at/over quota gets
        # 0 slots and is never trimmed), allocate a BATCH_SIZE batch across
        # open buckets, discover + upsert. Loops until quota / zero-yield /
        # iteration cap — see run_discovery_loop.
        current_count = count_company_rows(xlsx_path)
        print(f"📋 Existing companies in Company_List: {current_count}")
        added = run_discovery_loop(xlsx_path, tavily_pool, summary)
        print(f"📈 Discovery loop finished: {added} new companies this run.")

        # ── Step 2: Phase 1.5 — upgrade ATS URLs + backfill blank Career URLs ────
        # The one shared pool flows through every step so quota-exhaustion
        # state carries over (an exhausted pool aborts instantly, no re-spin).
        run_phase_1_5(xlsx_path, tavily_client=tavily_pool)

        # ── Step 3: BUG-69 — re-enrich blank Business Focus cells ────────────────
        # Runs after Phase 1.5 so freshly-backfilled manual rows get a focus
        # in the same run.
        run_reenrich_business_focus(xlsx_path, tavily_client=tavily_pool)

        # ── Step 4: fill blank Track cells ────────────────────────────────────────
        # Runs after the focus re-enrich so the classifier sees a Business
        # Focus for manual name-only rows.
        run_enrich_missing_tracks(xlsx_path)

        # ── Step 5: sort Company_List by Track ───────────────────────────────────
        # MUST stay last: earlier steps write by captured excel_row, which a
        # sort invalidates.
        n_sorted = sort_company_list_by_track(xlsx_path)
        print(f"↕️  Company_List sorted by Track ({n_sorted} rows).")

        print("🎉 Company Agent complete.")
    except Exception as e:
        # P0-7: capture pre-finally so summary reflects the failure mode.
        from shared.exceptions import GeminiTransientError
        if isinstance(e, GeminiTransientError):
            summary.transient_errors += 1
            summary.note(f"Run aborted (transient): {e}")
        else:
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


def migrate_tracks(xlsx_path: str | None = None) -> dict:
    """PRJ-004 REQ-004-06: one-time re-bucketing of existing Company_List rows
    into the 6-track taxonomy via a Gemini classification pass.

    Precondition (user-owned): the user has manually pruned Company_List.
    Idempotent — rows whose Track is already one of the 6 values are skipped,
    so a partial failure can simply be re-run. Nothing is ever deleted:
    unconfident classifications get 'UNMIGRATED — manual review' and defense
    legacy primes are deterministically forced to Mid-large Tech (a prime can
    survive only there). Prints a full audit table for the user spot-check.

    Returns {"migrated": n, "skipped": n, "flagged": n} for tests/reporting.
    """
    global _KEY_POOL
    gemini_keys = [k for k in [os.getenv("GEMINI_API_KEY"),
                               os.getenv("GEMINI_API_KEY_2")] if k]
    if not gemini_keys:
        print("❌ Missing GEMINI_API_KEY in .env")
        return {"migrated": 0, "skipped": 0, "flagged": 0}
    if _KEY_POOL is None:
        _KEY_POOL = _GeminiKeyPoolBase(gemini_keys, genai_mod=genai)

    xlsx_path = xlsx_path or get_or_create_excel()  # header renames self-heal here
    rows = get_company_rows_with_row_num(xlsx_path)
    pending = []   # (excel_row, name, old_value, business_focus)
    skipped = 0
    for excel_row, row in rows:
        name  = str(row[0]).strip() if row and row[0] else ""
        track = str(row[1]).strip() if len(row) > 1 else ""
        focus = str(row[2]).strip() if len(row) > 2 else ""
        if not name:
            continue
        if track in TRACK_VALUES:
            skipped += 1
            continue
        pending.append((excel_row, name, track, focus))

    print(f"🔀 Track migration: {len(pending)} row(s) to classify, "
          f"{skipped} already migrated (skipped).")
    if not pending:
        return {"migrated": 0, "skipped": skipped, "flagged": 0}

    migrated, flagged = 0, 0
    audit: list[tuple] = []
    tally: dict = {}
    BATCH = 25
    for b in range(0, len(pending), BATCH):
        chunk = pending[b : b + BATCH]
        try:
            results = _classify_tracks_batch([(n, f) for _, n, _, f in chunk])
        except Exception as e:
            logging.error(f"[Migrate] batch {b // BATCH + 1} failed: {e} — "
                          "rows left unmigrated; re-run to retry.")
            results = {}
        for excel_row, name, old_value, _focus in chunk:
            r = results.get(name)
            if r is None:
                new_track, rationale = "UNMIGRATED — manual review", "no classification returned"
            elif _normalize_company_name(name) in DEFENSE_EXCLUDED_PRIMES:
                # Deterministic: a prime can survive only in Mid-large Tech.
                new_track, rationale = "Mid-large Tech", "defense legacy prime (forced)"
            elif not r.get("confident", False):
                new_track, rationale = "UNMIGRATED — manual review", r.get("rationale", "unconfident")
            else:
                new_track, rationale = r["track"], r.get("rationale", "")
            update_company_track(xlsx_path, excel_row, new_track)
            audit.append((name, old_value, new_track, rationale))
            if new_track in TRACK_VALUES:
                migrated += 1
                tally[new_track] = tally.get(new_track, 0) + 1
            else:
                flagged += 1
        time.sleep(0.5)

    print("\n── Migration audit (spot-check me) ─────────────────────────────")
    for name, old, new, why in audit:
        print(f"  {name}: {old or '(blank)'} → {new}   [{why}]")
    print("\n── Per-bucket tally ─────────────────────────────────────────────")
    for track in TRACK_VALUES:
        print(f"  {track}: +{tally.get(track, 0)}")
    if flagged:
        print(f"  ⚠️  UNMIGRATED — manual review: {flagged} row(s)")
    print(f"\n✅ Migrated {migrated}, flagged {flagged}, already-done {skipped}. "
          "Re-run after fixing flagged rows — the pass is idempotent.")
    return {"migrated": migrated, "skipped": skipped, "flagged": flagged}


# ── Staged Company_List audit (BUG-74~77, report-only) ───────────────────────
_ATS_DOMAINS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                "workable.com")

# Board-root URL shapes → (platform, slug), for ownership checks on EXISTING
# sheet URLs (the discovery-time gate lives in _find_ats_url).
_BOARD_URL_PATTERNS = [
    ("greenhouse", re.compile(r'(?:job-boards|boards)\.greenhouse\.io/(?P<slug>[^/?#]+)', re.I)),
    ("lever",      re.compile(r'jobs\.lever\.co/(?P<slug>[^/?#]+)', re.I)),
    ("ashby",      re.compile(r'jobs\.ashbyhq\.com/(?P<slug>[^/?#]+)', re.I)),
    ("workable",   re.compile(r'apply\.workable\.com/(?P<slug>[^/?#]+)', re.I)),
]
_VALIDATOR_BY_PLATFORM = {v["platform"]: v for v in ATS_VALIDATORS}


def _parse_ats_board(url: str):
    """Return (validator, slug) for a recognized ATS board URL, else None."""
    for platform, pat in _BOARD_URL_PATTERNS:
        m = pat.search(url)
        if m:
            return _VALIDATOR_BY_PLATFORM[platform], m.group("slug")
    return None


# Anti-oscillation ledger: Gemini flip-flops on borderline companies between
# audit runs (observed live: NVIDIA/Tesla/Physical Intelligence proposed A→B
# one run and B→A the next). Once a Track/Focus value is user-confirmed via
# --apply-audit (or the one-off seeding), audits stop re-proposing changes
# away from it. Re-open a company by editing its cell in Company_List (the
# suppression only fires while current == confirmed) or deleting the ledger.
_AUDIT_LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "company_audit_ledger.json")


def _focus_fingerprint(focus: str) -> str:
    import hashlib
    return hashlib.sha256((focus or "").strip().encode()).hexdigest()[:16]


def _load_audit_ledger(path: str | None = None) -> dict:
    path = path or _AUDIT_LEDGER_PATH
    try:
        with open(path) as fh:
            data = json.load(fh)
        return {"track": dict(data.get("track", {})),
                "focus": dict(data.get("focus", {}))}
    except Exception:
        return {"track": {}, "focus": {}}


def _save_audit_ledger(ledger: dict, path: str | None = None) -> None:
    path = path or _AUDIT_LEDGER_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(ledger, fh, indent=1, sort_keys=True)


_AUDIT_SYSTEM_INSTRUCTION = (
    "You audit an existing company list for a TPM job-search pipeline. Each "
    "input company has its name, current track, and current business_focus. "
    "Using your own knowledge of the company, return per company:\n"
    "- track: the best-fit bucket (definitions below)\n"
    "- track_confident: false when you do not know the company well enough to "
    "classify it — do not guess\n"
    "- focus_accurate: false when the current business_focus describes a "
    "different company, misstates what they build, or invents facts\n"
    "- proposed_focus: when focus_accurate is false AND you know the company "
    "well, a corrected 3-4 sentence description (what they build, customers, "
    "differentiator, traction); empty string otherwise — do not guess\n"
    "- focus_confident: false when you cannot judge the description's accuracy\n"
    "- notes: one short sentence of rationale\n"
    "Copy company_name verbatim. Buckets:\n"
    + _TRACK_BUCKET_DEFINITIONS
    + SECURITY_CLAUSE  # current business_focus text originated from scraped web content
)


def _audit_companies_batch(chunk: list[tuple[str, str, str]]) -> dict:
    """One batched Gemini audit call for [(name, current_track, focus)].

    Returns {company_name: audit dict}. Raises on API/parse failure — the
    caller marks the batch's rows for re-run.
    """
    payload = json.dumps([{"company_name": n, "current_track": t,
                           "business_focus": (f or "")[:600]}
                          for n, t, f in chunk])
    config = types.GenerateContentConfig(
        system_instruction=_AUDIT_SYSTEM_INSTRUCTION,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=CompanyAuditList,
    )
    resp = _KEY_POOL.generate_content(
        model=MODEL,
        contents=("Audit these companies:\n"
                  f"<scraped_content>\n{payload}\n</scraped_content>"),
        config=config,
    )
    return {i.get("company_name", "").strip(): i
            for i in json.loads(resp.text).get("items", [])}


def _audit_url_flags(name: str, url: str, skip_http: bool = False) -> list[str]:
    """Report-only URL health flags for one Company_List row (BUG-74/75/77)."""
    flags = []
    if not url or url.upper() == "N/A":
        return ["BLANK"]
    if _is_job_posting_url(url):
        root = _posting_url_to_board_root(url)
        flags.append(f"POSTING_URL(→{root})" if root else "POSTING_URL")
    if _is_aggregator_url(url) or _unwrap_career_url(url):
        flags.append("AGGREGATOR")
    if not any(d in url.lower() for d in _ATS_DOMAINS):
        flags.append("NON_ATS")  # informational — many company sites are fine
    if not skip_http:
        parsed = _parse_ats_board(url)
        if parsed:
            v, slug = parsed
            if not _ats_board_belongs_to_company(slug, v, name):
                flags.append("OWNERSHIP_MISMATCH")
        else:
            m = re.match(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com", url, re.I)
            if m and not _org_name_matches_company(m.group(1), name):
                flags.append("OWNERSHIP_MISMATCH")
        if not validate_career_url(url):
            flags.append("HTTP_FAIL")
    return flags


# Flags that mark a URL as actually wrong (vs informational NON_ATS) —
# these rows get a --suggest-urls lookup by default.
_SUGGEST_DEFAULT_FLAGS = ("POSTING_URL", "AGGREGATOR", "OWNERSHIP_MISMATCH",
                          "HTTP_FAIL", "BLANK")


def _same_url(a: str, b: str) -> bool:
    """Loose equality for 'suggestion is not actually a change' checks."""
    norm = lambda u: re.sub(r'^https?://(www\.)?', '', (u or "").lower()).rstrip('/')
    return norm(a) == norm(b)


def _suggest_career_url(company_name: str, current_url: str,
                        tavily_client=None) -> tuple:
    """Find a replacement Career URL for a flagged row, with evidence.

    Returns (url, evidence) or (None, None). REPORT-ONLY — the result goes
    into the audit tab's Suggested URL column for user approval; nothing is
    written to Company_List here.

    Strategy order is cheapest-first (unlike find_career_url, which is
    accuracy-first for discovery): deterministic posting-root strip → known
    URL table → ATS slug probe (free HTTP, ownership-gated) → Tavily ATS
    search → Tavily general search → homepage scrape (identity-gated).
    Every candidate must differ from current_url and pass the BUG-74/75/77
    gates before it is suggested.
    """
    def _ok(url: str) -> bool:
        return bool(url) and not _same_url(url, current_url)

    # 1. Posting URL → board root (deterministic, no search).
    if _is_job_posting_url(current_url):
        root = _posting_url_to_board_root(current_url)
        if _ok(root) and validate_career_url(root):
            return root, "posting URL stripped to its board root (validated)"

    # 2. Known-URL table.
    name_lower = company_name.lower().strip()
    for k, v in KNOWN_CAREER_URLS.items():
        if k.lower() == name_lower and _ok(v) and validate_career_url(v):
            return v, "KNOWN_CAREER_URLS table entry (validated)"

    # 3. ATS slug probe with ownership evidence (free HTTP).
    for slug in _slug_candidates(company_name)[:3]:
        for v in ATS_VALIDATORS:
            hit, n = _check_ats_slug(slug, v)
            if hit:
                org = _fetch_ats_org_name(slug, v)
                if org is not None and not _org_name_matches_company(org, company_name):
                    continue  # exists but owned by someone else
                url = v["board_template"].format(slug=slug)
                if _ok(url):
                    owner = f"org name {org!r}" if org else "org name unavailable"
                    return url, (f"{v['platform']} board '{slug}' — "
                                 f"{owner}, {n} live job(s)")
            time.sleep(0.2)

    # 4/5. Tavily searches (skipped when no client — quota-conscious).
    if tavily_client is not None:
        ats_query = (f'"{company_name}" careers jobs '
                     f'site:greenhouse.io OR site:lever.co OR site:ashbyhq.com '
                     f'OR site:myworkdayjobs.com')
        url = _tavily_extract_career_url(company_name, ats_query, tavily_client)
        if _ok(url):
            return url, "Tavily ATS-scoped search result (validated)"
        gen_query = f'"{company_name}" careers hiring jobs 2026 official career page'
        url = _tavily_extract_career_url(company_name, gen_query, tavily_client)
        if _ok(url):
            return url, "Tavily general search result (validated)"

    # 6. Homepage scrape (identity-gated, BUG-75).
    url = _scrape_homepage_for_career_link(company_name)
    if _ok(url):
        return url, "careers link on the company homepage (identity-checked)"

    return None, None


def run_company_audit(xlsx_path: str | None = None, limit: int | None = None,
                      skip_http: bool = False, suggest_urls: bool = False,
                      suggest_non_ats: bool = False,
                      suggest_limit: int | None = None) -> dict:
    """BUG-74~77 staged review: re-evaluate Track + Business Focus and check
    URL health for every Company_List row, writing findings to the
    Company_Audit tab ONLY.

    REPORT-ONLY BY DESIGN: never calls update_company_track /
    update_company_business_focus / update_company_career_url — the 380
    Company_List rows are user-verified data. The user reviews the tab and
    applying approved changes is a separate follow-up step.

    Gemini only (batches of 20 ≈ 19 calls for 380 rows); zero Tavily calls
    unless `suggest_urls=True`. `skip_http=True` skips the per-row GETs
    (ownership + reachability) for cheap dry runs.

    `suggest_urls=True` additionally runs _suggest_career_url for rows whose
    flags mark the URL as wrong (_SUGGEST_DEFAULT_FLAGS; add informational
    NON_ATS rows with `suggest_non_ats=True`), filling the Suggested URL /
    URL Evidence columns. The user approves suggestions by typing Y in
    "Approve URL?"; only --apply-audit writes them. `suggest_limit` caps how
    many rows get a lookup (Tavily budget control; up to 2 searches/row).
    Returns summary counts.
    """
    global _KEY_POOL
    gemini_keys = [k for k in [os.getenv("GEMINI_API_KEY"),
                               os.getenv("GEMINI_API_KEY_2")] if k]
    if not gemini_keys:
        print("❌ Missing GEMINI_API_KEY in .env")
        return {"audited": 0}
    if _KEY_POOL is None:
        _KEY_POOL = _GeminiKeyPoolBase(gemini_keys, genai_mod=genai)

    xlsx_path = xlsx_path or get_or_create_excel()
    companies = []
    for row in get_company_rows(xlsx_path):
        name = str(row[0]).strip() if row and row[0] else ""
        if not name:
            continue
        companies.append((name,
                          str(row[1] or "").strip() if len(row) > 1 else "",
                          str(row[2] or "").strip() if len(row) > 2 else "",
                          str(row[3] or "").strip() if len(row) > 3 else ""))
    if limit:
        companies = companies[:limit]

    tavily_client = None
    if suggest_urls:
        try:
            tavily_client = build_tavily_pool_from_env()
        except Exception as e:
            logging.warning(f"[Audit] Tavily pool init failed: {e} — "
                            "URL suggestions run without Tavily strategies.")

    print("\n" + "="*60)
    print(f"COMPANY AUDIT (report-only): {len(companies)} row(s)"
          + ("  [HTTP checks skipped]" if skip_http else "")
          + ("  [+URL suggestions]" if suggest_urls else ""))
    print("="*60)
    if not companies:
        return {"audited": 0}

    # Step 1: batched Gemini re-evaluation (valid_names gate drops
    # hallucinated names, run_reenrich_business_focus pattern).
    valid_names = {n for n, _, _, _ in companies}
    results: dict = {}
    BATCH_AUDIT = 20
    n_batches = (len(companies) + BATCH_AUDIT - 1) // BATCH_AUDIT
    for b in range(0, len(companies), BATCH_AUDIT):
        chunk = companies[b : b + BATCH_AUDIT]
        print(f"  🤖 Gemini audit batch {b // BATCH_AUDIT + 1}/{n_batches} "
              f"({len(chunk)} companies)...")
        try:
            batch = _audit_companies_batch([(n, t, f) for n, t, f, _ in chunk])
            results.update({k: v for k, v in batch.items() if k in valid_names})
        except Exception as e:
            logging.error(f"[Audit] batch {b // BATCH_AUDIT + 1} failed: {e} — "
                          "rows marked for re-run.")
        time.sleep(0.5)

    # Step 2: per-row URL flags (+optional suggestions) + report assembly.
    audited_at = time.strftime("%Y-%m-%d %H:%M:%S")
    ledger = _load_audit_ledger()
    out_rows = []
    stats = {"audited": len(companies), "track_changes": 0, "focus_changes": 0,
             "url_flagged": 0, "no_result": 0, "urls_suggested": 0,
             "suppressed": 0}
    for name, track, focus, url in companies:
        flags = _audit_url_flags(name, url, skip_http=skip_http)
        # NON_ATS alone is informational noise — only count actionable flags.
        if any(f != "NON_ATS" for f in flags):
            stats["url_flagged"] += 1
        if not skip_http:
            time.sleep(0.3)

        suggested_url = evidence = ""
        if suggest_urls and (suggest_limit is None
                             or stats["urls_suggested"] < suggest_limit):
            base_flags = {f.split("(")[0] for f in flags}
            wants = bool(base_flags & set(_SUGGEST_DEFAULT_FLAGS)) or \
                    (suggest_non_ats and "NON_ATS" in base_flags)
            if wants:
                print(f"  🔎 {name}: looking for a replacement URL...")
                s_url, s_ev = _suggest_career_url(name, url, tavily_client)
                if s_url:
                    suggested_url, evidence = s_url, s_ev
                    stats["urls_suggested"] += 1
                    print(f"     💡 → {s_url}")
        r = results.get(name)
        if r is None:
            proposed_track = proposed_focus = confidence = ""
            track_changed = focus_changed = ""
            notes = "no audit result — re-run --audit"
            stats["no_result"] += 1
        else:
            tc = bool(r.get("track_confident", False))
            fc = bool(r.get("focus_confident", False))
            new_track = (r.get("track") or "").strip()
            proposed_track = new_track if (tc and new_track and new_track != track) else ""
            pf = (r.get("proposed_focus") or "").strip()
            proposed_focus = pf if (fc and not r.get("focus_accurate", True) and pf) else ""
            notes = (r.get("notes") or "").strip()
            # Anti-oscillation: don't re-propose changes away from a value
            # the user already confirmed via --apply-audit / seeding.
            if proposed_track and ledger["track"].get(name) == track:
                proposed_track = ""
                notes = (notes + " [track re-proposal suppressed: "
                                 "current value is user-confirmed]").strip()
                stats["suppressed"] += 1
            if proposed_focus and ledger["focus"].get(name) == _focus_fingerprint(focus):
                proposed_focus = ""
                notes = (notes + " [focus re-proposal suppressed: "
                                 "current value is user-confirmed]").strip()
                stats["suppressed"] += 1
            track_changed = "YES" if proposed_track else "no"
            focus_changed = "YES" if proposed_focus else "no"
            confidence = (f"track:{'yes' if tc else 'NO'} "
                          f"focus:{'yes' if fc else 'NO'}")
            if proposed_track:
                stats["track_changes"] += 1
            if proposed_focus:
                stats["focus_changes"] += 1
        out_rows.append([name, track, proposed_track, track_changed,
                         focus[:150], proposed_focus, focus_changed,
                         ", ".join(flags), confidence, notes, audited_at,
                         suggested_url, evidence, ""])  # Approve URL? = user's

    replace_audit_sheet(xlsx_path, out_rows)
    print(f"\n  Audited={stats['audited']}  Track changes proposed="
          f"{stats['track_changes']}  Focus changes proposed="
          f"{stats['focus_changes']}  URL-flagged={stats['url_flagged']}  "
          f"URLs suggested={stats['urls_suggested']}  "
          f"Re-proposals suppressed={stats['suppressed']}  "
          f"No result={stats['no_result']}")
    print(f"  📋 Review the '{AUDIT_SHEET}' tab — Company_List was NOT modified.")
    if stats["urls_suggested"]:
        print("  ✍️  Type Y in 'Approve URL?' on suggestions you accept, then "
              "run --apply-audit.")
    print("="*60 + "\n")
    return stats


def apply_audit(xlsx_path: str | None = None, urls_only: bool = False) -> dict:
    """Apply user-approved Company_Audit proposals to Company_List.

    Writes Track (update_company_track) and Business Focus
    (update_company_business_focus) for audit rows carrying a proposal, and —
    the SOLE exception to the never-rewrite-filled-URLs rule (REQ-153) — a
    Suggested URL whose "Approve URL?" cell the user set to Y/yes
    (update_company_career_url). Unapproved suggestions are never written.
    Company Name is never written. Rows are matched by Company Name —
    companies the user deleted since the audit are skipped; a proposed track
    not in TRACK_VALUES is skipped with a warning. Ends with the canonical
    Track sort (all excel_row-keyed writes complete first). Returns summary
    counts.

    `urls_only=True` applies ONLY approved URL suggestions and skips every
    Track/Focus proposal (for when a re-audit's proposals are oscillation
    noise). Applied Track/Focus values are recorded in the anti-oscillation
    ledger so future audits stop re-proposing changes away from them.
    """
    xlsx_path = xlsx_path or get_or_create_excel()
    proposals = get_audit_proposals(xlsx_path)
    counts = {"track_applied": 0, "focus_applied": 0, "url_applied": 0,
              "skipped_missing": 0, "skipped_invalid": 0}
    ledger = _load_audit_ledger()
    if not proposals:
        print(f"⚪ No proposals found in '{AUDIT_SHEET}' — run --audit first.")
        return counts

    row_by_name = {}
    for excel_row, row in get_company_rows_with_row_num(xlsx_path):
        name = str(row[0]).strip() if row and row[0] else ""
        if name:
            row_by_name[name] = excel_row

    print("\n" + "="*60)
    print(f"APPLY AUDIT: {len(proposals)} row(s) with proposals")
    print("="*60)
    for p in proposals:
        name = p["name"]
        excel_row = row_by_name.get(name)
        if excel_row is None:
            print(f"  ⏭️  {name}: no longer in Company_List — skipped.")
            counts["skipped_missing"] += 1
            continue
        if p["proposed_track"] and not urls_only:
            if p["proposed_track"] in TRACK_VALUES:
                update_company_track(xlsx_path, excel_row, p["proposed_track"])
                ledger["track"][name] = p["proposed_track"]
                print(f"  ✅ {name}: Track → {p['proposed_track']}")
                counts["track_applied"] += 1
            else:
                logging.warning(f"[ApplyAudit] {name}: invalid proposed track "
                                f"{p['proposed_track']!r} — skipped.")
                counts["skipped_invalid"] += 1
        if p["proposed_focus"] and not urls_only:
            update_company_business_focus(xlsx_path, excel_row, p["proposed_focus"])
            ledger["focus"][name] = _focus_fingerprint(p["proposed_focus"])
            print(f"  ✅ {name}: Business Focus updated.")
            counts["focus_applied"] += 1
        if p.get("suggested_url"):
            if p.get("url_approved") and p["suggested_url"].startswith("http"):
                # User-approved suggestion — the one sanctioned Career URL
                # write outside blank-row backfill (REQ-153).
                update_company_career_url(xlsx_path, excel_row, p["suggested_url"])
                print(f"  ✅ {name}: Career URL → {p['suggested_url']} (user-approved)")
                counts["url_applied"] += 1
            elif not p.get("url_approved"):
                print(f"  ⏸️  {name}: suggested URL not approved — left unchanged.")

    _save_audit_ledger(ledger)
    n_sorted = sort_company_list_by_track(xlsx_path)
    if urls_only:
        print("  ℹ️  --urls-only: Track/Focus proposals skipped this run.")
    print(f"\n  Track applied={counts['track_applied']}  "
          f"Focus applied={counts['focus_applied']}  "
          f"URLs applied (approved)={counts['url_applied']}  "
          f"Skipped (deleted)={counts['skipped_missing']}  "
          f"Skipped (invalid)={counts['skipped_invalid']}")
    print(f"  ↕️  Company_List re-sorted by Track ({n_sorted} rows). "
          "Company Names untouched; Career URLs written only where you "
          "approved a suggestion.")
    print("="*60 + "\n")
    return counts


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PathFinder company agent")
    parser.add_argument("--migrate-tracks", action="store_true",
                        help="One-time PRJ-004 re-bucketing of Company_List "
                             "into the 6-track taxonomy (REQ-004-06). "
                             "Run AFTER manually pruning Company_List.")
    parser.add_argument("--audit", action="store_true",
                        help="BUG-74~77 report-only review: re-evaluate Track/"
                             "Business Focus (Gemini) + URL health flags for "
                             "every Company_List row into the Company_Audit "
                             "tab. Never modifies Company_List.")
    parser.add_argument("--audit-limit", type=int, default=None,
                        help="Audit only the first N rows (dry run).")
    parser.add_argument("--audit-skip-http", action="store_true",
                        help="Skip per-row HTTP checks during --audit.")
    parser.add_argument("--suggest-urls", action="store_true",
                        help="During --audit, also look up a replacement URL "
                             "for rows with actionable URL flags, into the "
                             "Suggested URL / URL Evidence columns. Approve "
                             "with Y in 'Approve URL?' then --apply-audit.")
    parser.add_argument("--suggest-non-ats", action="store_true",
                        help="Include informational NON_ATS rows in "
                             "--suggest-urls lookups (bigger Tavily spend).")
    parser.add_argument("--suggest-limit", type=int, default=None,
                        help="Max rows to run a URL suggestion for "
                             "(Tavily budget control).")
    parser.add_argument("--apply-audit", action="store_true",
                        help="Apply Company_Audit proposals to Company_List "
                             "(matched by Company Name): Track/Focus proposals, "
                             "plus suggested Career URLs ONLY on rows where "
                             "'Approve URL?' is Y. Names never written.")
    parser.add_argument("--urls-only", action="store_true",
                        help="With --apply-audit: apply only approved URL "
                             "suggestions; skip all Track/Focus proposals.")
    args = parser.parse_args()
    try:
        _run_lock = acquire_run_lock("company_agent")
    except AgentAlreadyRunning as e:
        raise SystemExit(f"🔒 {e}")
    if args.apply_audit:
        apply_audit(urls_only=args.urls_only)
    elif args.audit:
        run_company_audit(limit=args.audit_limit, skip_http=args.audit_skip_http,
                          suggest_urls=args.suggest_urls,
                          suggest_non_ats=args.suggest_non_ats,
                          suggest_limit=args.suggest_limit)
    elif args.migrate_tracks:
        migrate_tracks()
    else:
        main()
