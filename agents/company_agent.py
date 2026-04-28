"""
Company Agent — runs the least frequently (one-time or as needed).

Responsibilities:
  1. Read existing companies from Excel (avoid duplicates)
  2. Discover up to 50 NEW North American AI companies via Tavily + Gemini
     (capped at 200 total companies in the sheet)
  3. Find real career URLs via multi-strategy lookup (NO Gemini URL guessing)
  4. Phase 1.5: Validate & upgrade career URLs to ATS board URLs

URL discovery strategy (per company, in order):
  1. KNOWN_CAREER_URLS exact match
  2. Tavily targeted search → ATS/career URL extracted from results
  3. ATS slug probing (Greenhouse → Lever)
  4. Company homepage scraping → find careers link

Search distribution (designed to surface TPM-heavy roles):
  - Big Tech (AI Investment) : 50%
  - Top AI Startups          : 25%
  - AI Infra / Compute / GPU : 15%
  - Large Model Labs         : 10%

Run:
  python agents/company_agent.py
"""
import os
import sys
import json
import re
import logging
import time
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
    update_company_career_url, get_company_names_without_tpm,
)
from shared.gemini_pool import _GeminiKeyPoolBase
from shared.config import MODEL
from shared.run_summary import RunSummary

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# BUG-31: use _GeminiKeyPoolBase directly with genai_mod parameter
_GeminiKeyPool = _GeminiKeyPoolBase  # alias for backward compat (tests)

MAX_TOTAL  = 200   # hard cap on total companies in the sheet
BATCH_SIZE = 50    # new companies to discover each run

_KEY_POOL: "_GeminiKeyPool | None" = None  # initialised in main()

# ── Pydantic schemas ──────────────────────────────────────────────────────────
class AICompanyInfo(BaseModel):
    """Company info extracted by Gemini — NO career URL (found separately)."""
    company_name:   str = Field(description="The official name of the AI company.")
    ai_domain:      str = Field(
        description="Exactly one of: AI Startups / Large Model Labs / "
                    "Big Tech (AI Investment) / AI Infrastructure & Compute"
    )
    business_focus: str = Field(
        description=(
            "3-4 sentence description covering: (1) what the company builds or sells, "
            "(2) who their primary customers are, (3) their key differentiator or "
            "competitive edge in the AI ecosystem, and (4) notable products, funding, "
            "or market traction."
        )
    )

class CompanyInfoList(BaseModel):
    companies: list[AICompanyInfo] = Field(description="A list of discovered AI companies.")

# ── ATS slug-validation config (Greenhouse + Lever only — public APIs) ────────
ATS_VALIDATORS = [
    {
        "platform":      "greenhouse",
        "api_template":  "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "board_template":"https://job-boards.greenhouse.io/{slug}",
        "jobs_key":      "jobs",
    },
    {
        "platform":      "lever",
        "api_template":  "https://api.lever.co/v0/postings/{slug}?mode=json",
        "board_template":"https://jobs.lever.co/{slug}",
        "jobs_key":      None,
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
    "OpenAI":          "https://openai.com/careers",
    "Anthropic":       "https://www.anthropic.com/careers",
    "Cohere":          "https://cohere.com/about/careers",
    "Scale AI":        "https://scale.com/careers",
    "Hugging Face":    "https://apply.workable.com/huggingface/",
}

# ── Tavily search queries (weighted by target distribution) ────────────────────
TAVILY_QUERIES = [
    # ── Big Tech AI Investment (50%) ──────────────────────────────────────────
    (
        "big tech AI division jobs 2026 Google Microsoft Amazon Meta Apple IBM Oracle Salesforce "
        "AI investment hiring TPM product manager careers"
    ),
    (
        "traditional tech companies AI transformation 2026 Intel Qualcomm AMD Cisco Adobe "
        "Salesforce ServiceNow Workday AI careers job openings"
    ),
    (
        "enterprise software AI 2026 SAP Oracle IBM Palantir Databricks Snowflake DataRobot "
        "C3.ai career page hiring technical program manager"
    ),
    # ── Top AI Startups (25%) ─────────────────────────────────────────────────
    (
        "top AI startups hiring 2026 North America Scale AI Cohere Adept Inflection Runway "
        "Midjourney Character.ai Perplexity Hugging Face careers job openings"
    ),
    (
        "well-funded AI startups 2026 USA hiring TPM product manager Glean Harvey Writer "
        "Jasper Tome Imbue Pika Labs Sierra careers"
    ),
    # ── AI Infra / Compute / GPU (15%) ────────────────────────────────────────
    (
        "AI infrastructure compute GPU cloud 2026 CoreWeave Lambda Labs Together AI Groq "
        "Cerebras SambaNova Graphcore Mosaicml careers hiring"
    ),
    # ── Large Model Labs (10%) ────────────────────────────────────────────────
    (
        "frontier AI research labs 2026 OpenAI Anthropic xAI Google DeepMind Mistral "
        "Cohere Aleph Alpha careers hiring site:jobs.lever.co OR site:greenhouse.io"
    ),
]

# ── URL helpers ───────────────────────────────────────────────────────────────
_CAREER_DOMAINS   = ["greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                     "job-boards.greenhouse.io", "jobs.lever.co"]
_CAREER_PATH_KWDS = ["/careers", "/jobs", "/hiring", "/work-with-us",
                     "/join-us", "/join", "/opportunities", "/open-roles"]

def _is_likely_career_url(url: str) -> bool:
    """Heuristic: does this URL look like a career/jobs page?"""
    u = url.lower()
    return (any(d in u for d in _CAREER_DOMAINS) or
            any(kw in u for kw in _CAREER_PATH_KWDS))


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

    for suffix in [" inc", " inc.", " llc", " ltd", " corp", " corporation",
                   " technologies", " ai"]:
        name = name.replace(suffix, "")
    name     = name.strip()
    hyphen   = re.sub(r'[^a-z0-9]+', '-', name).strip('-')
    nospace  = re.sub(r'[^a-z0-9]+', '', name)
    parts    = hyphen.split('-')

    seen, out = set(), []
    for c in [hyphen, nospace, pre_hyphen, pre_nospace,
              parts[-1] if parts else '', parts[0] if parts else '']:
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
                return v["board_template"].format(slug=slug)
            time.sleep(0.2)
    return None


# ── Homepage scraping ─────────────────────────────────────────────────────────
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


def _tavily_extract_career_url(company_name: str, query: str, client) -> str | None:
    """Run one Tavily query and return the first validated career URL found."""
    try:
        r = client.search(query=query, search_depth="basic", max_results=5)
        for item in r.get("results", []):
            url = item.get("url", "")
            if _is_likely_career_url(url) and validate_career_url(url):
                return url
    except Exception as e:
        err_str = str(e)
        if "402" in err_str or "429" in err_str or "quota" in err_str.lower():
            logging.error(f"[CareerURL] Tavily quota exhausted for {company_name}: {e}")
        else:
            logging.warning(f"[CareerURL] Tavily query failed for {company_name}: {e}")
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
def discover_ai_companies(tavily_key: str, existing_names: set, need: int) -> list:
    """
    Discover up to `need` new AI companies not in `existing_names`.
    Returns a list of dicts with keys: company_name, ai_domain, business_focus, career_url.

    Flow:
      1. Tavily batch search → raw article/news results
      2. Gemini extracts structured company info (NO URL generation)
      3. Per-company multi-strategy career URL lookup
    """
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking discover_ai_companies()")
    from tavily import TavilyClient

    logging.info(f"Searching for {need} new US AI companies (existing: {len(existing_names)})...")
    client   = TavilyClient(api_key=tavily_key)
    raw, seen_urls = [], set()

    # Step 1: Batch Tavily search for company discovery
    for q in TAVILY_QUERIES:
        try:
            r = client.search(query=q, search_depth="advanced", max_results=10)
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
    existing_list = sorted(existing_names)[:150]
    logging.info("Feeding context to Gemini for company extraction (no URL generation)...")
    config = types.GenerateContentConfig(
        system_instruction=(
            "You are an expert AI industry analyst. From the web search context, "
            f"extract exactly {need} distinct AI companies headquartered in the United States "
            "that are NOT in the EXISTING_COMPANIES list.\n"
            "Follow this category distribution strictly:\n"
            f"  - Big Tech (AI Investment): {round(need*0.50)} companies — large established tech "
            "companies (Google, Microsoft, Amazon, Meta, Apple, IBM, Oracle, Salesforce, SAP, "
            "Intel, Qualcomm, AMD, Cisco, Adobe, Palantir, Databricks, Snowflake, Stripe, etc.)\n"
            f"  - Top AI Startups: {round(need*0.25)} companies — well-funded AI-native startups "
            "with real headcount (not tiny pre-seed; must have public job boards)\n"
            f"  - AI Infra/Compute/GPU: {round(need*0.15)} companies — GPU cloud, AI compute, "
            "model serving, AI chip companies\n"
            f"  - Large Model Labs: {round(need*0.10)} companies — frontier AI research labs "
            "(OpenAI, Anthropic, xAI, DeepMind, Cohere, Mistral, etc.)\n"
            "STRICTLY exclude non-US companies (Canadian, European, Asian).\n"
            "For business_focus: write 3-4 sentences covering what the company builds, "
            "who their customers are, their key competitive differentiator, and notable "
            "products/funding/traction.\n"
            "DO NOT include any career_url or URL fields — URLs are handled separately."
        ),
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=CompanyInfoList,
    )
    prompt = (
        f"Search context:\n{json.dumps(results)}\n\n"
        f"EXISTING_COMPANIES (DO NOT include these):\n{json.dumps(existing_list)}\n\n"
        f"Extract exactly {need} NEW US-headquartered AI companies with: company_name, "
        "ai_domain (one of: AI Startups / Large Model Labs / "
        "Big Tech (AI Investment) / AI Infrastructure & Compute), "
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
def validate_and_upgrade_ats_url(company_name: str, current_url: str) -> str:
    """Return upgraded ATS board URL, or original if no ATS found."""
    name_lower = company_name.lower().strip()

    # 1. Hard-coded overrides (highest priority)
    # Use word-boundary matching (not substring) to avoid "xai" matching "MaxAI" etc.
    for kw, url in KNOWN_ATS_OVERRIDES.items():
        if re.search(r'(?:^|\s)' + re.escape(kw) + r'(?:\s|$)', name_lower):
            logging.info(f"[Phase1.5] {company_name}: override → {url}")
            return url

    # 2. Already an ATS URL
    if any(d in current_url for d in ["greenhouse.io", "lever.co", "ashbyhq.com",
                                       "myworkdayjobs.com"]):
        return current_url

    # 3. Probe Greenhouse + Lever slugs
    for slug in _slug_candidates(company_name):
        for v in ATS_VALIDATORS:
            hit, n = _check_ats_slug(slug, v)
            if hit:
                upgraded = v["board_template"].format(slug=slug)
                logging.info(f"[Phase1.5] {company_name}: {v['platform']} "
                             f"slug='{slug}' jobs={n} → {upgraded}")
                return upgraded
            time.sleep(0.3)

    return current_url


def run_phase_1_5(xlsx_path: str):
    print("\n" + "="*60)
    print("PHASE 1.5: ATS URL VALIDATION & UPGRADE")
    print("="*60)
    rows = get_company_rows_with_row_num(xlsx_path)
    if not rows:
        print("⚠️  No companies found. Skipping.")
        return

    upgraded = no_ats = 0
    ATS_DOMAINS = ["greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com"]

    for excel_row, row in rows:
        name = str(row[0]).strip() if row[0] else ""
        url  = str(row[3]).strip() if row[3] else ""
        if not name or not url or url == "N/A":
            continue

        if any(d in url for d in ATS_DOMAINS):
            print(f"  ⏭️  {name}: Already ATS → {url}")
            continue

        print(f"  🔍 {name}...")
        new_url = validate_and_upgrade_ats_url(name, url)
        if new_url != url:
            update_company_career_url(xlsx_path, excel_row, new_url)
            print(f"  ✅ {name}: {url} → {new_url}")
            upgraded += 1
        else:
            print(f"  ⚪ {name}: No ATS found.")
            no_ats += 1
        time.sleep(1)

    print(f"\n  Upgraded={upgraded}  No ATS={no_ats}")
    print("="*60 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _KEY_POOL
    summary = RunSummary(agent="company")
    try:
        tavily_key  = os.getenv("TAVILY_API_KEY")
        gemini_keys = [k for k in [
            os.getenv("GEMINI_API_KEY"),
            os.getenv("GEMINI_API_KEY_2"),
        ] if k]
        missing = []
        if not gemini_keys:
            missing.append("GEMINI_API_KEY")
        if not tavily_key:
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

        # ── Step 1: Read existing companies (both lists) ──────────────────────────
        current_count     = count_company_rows(xlsx_path)
        existing_rows     = get_company_rows(xlsx_path)
        names_main        = {str(r[0]).strip() for r in existing_rows if r[0]}
        names_without_tpm = get_company_names_without_tpm(xlsx_path)
        existing_names    = names_main | names_without_tpm
        print(f"📋 Existing companies in Company_List: {current_count}")
        print(f"📋 Companies in Company_Without_TPM: {len(names_without_tpm)}")
        print(f"📋 Total known companies (dedup exclusion list): {len(existing_names)}")

        # ── Step 2: Determine how many to fetch ───────────────────────────────────
        if current_count >= MAX_TOTAL:
            print(f"✅ Already at max capacity ({MAX_TOTAL} companies). Skipping discovery.")
            summary.note(f"At capacity ({MAX_TOTAL}); discovery skipped.")
        else:
            slots_left = MAX_TOTAL - current_count
            need       = min(BATCH_SIZE, slots_left)
            print(f"🔍 Will discover up to {need} new companies "
                  f"(cap: {MAX_TOTAL}, current: {current_count})...")
            summary.attempted = need

            companies = discover_ai_companies(tavily_key, existing_names, need)

            if companies:
                companies = companies[:need]
                data = [c.model_dump() if hasattr(c, "model_dump") else dict(c)
                        for c in companies]
                upsert_companies(xlsx_path, data)
                new_count = count_company_rows(xlsx_path)
                print(f"✅ Added {len(data)} companies. Total: {new_count}")
                summary.succeeded = len(data)
                summary.failed = max(0, need - len(data))
            else:
                print("⚠️  No new companies returned.")
                summary.failed = need

        # ── Step 3: Phase 1.5 — upgrade ATS URLs ─────────────────────────────────
        run_phase_1_5(xlsx_path)

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
        summary.mark_finished()
        log_path = summary.write()
        print(f"📊 Run summary: {log_path}")
        print(summary.to_json())


if __name__ == "__main__":
    main()
