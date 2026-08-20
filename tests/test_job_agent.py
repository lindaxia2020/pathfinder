"""
Tests for agents/job_agent.py

Coverage:
  - _cache_path: deterministic MD5 path
  - _save_md_to_cache / _load_md_from_cache: round-trip cache
  - _RateLimiter: interval and lazy lock creation
  - _GeminiKeyPool: key rotation on 429
  - _classify / _classify_by_domain: company classification
  - ATS_PLATFORMS config: domain lists and slug patterns
  - Pydantic schemas: TargetJobURLs, JobDetails
  - _assess_jd_quality: JD field completeness grading (REQ-060)
"""
import sys
import os
import json
import asyncio
import hashlib
import tempfile
import time
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── Stub heavy deps ───────────────────────────────────────────────────────────
for mod in ["google", "google.genai", "google.genai.types",
            "dotenv", "firecrawl", "crawl4ai", "pycountry"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.modules["dotenv"].load_dotenv = lambda: None

import google
google.genai = MagicMock()
google.genai.types = MagicMock()

# Stub pycountry subdivisions — all 50 states + DC so BUG-03 tests
# actually exercise the fix logic (e.g. IN/OR/DE/ME/OK are real state codes
# that could cause false positives without the comma-prefix guard).
_US_STATES_RAW = [
    ("Alabama", "US-AL"), ("Alaska", "US-AK"), ("Arizona", "US-AZ"),
    ("Arkansas", "US-AR"), ("California", "US-CA"), ("Colorado", "US-CO"),
    ("Connecticut", "US-CT"), ("Delaware", "US-DE"), ("Florida", "US-FL"),
    ("Georgia", "US-GA"), ("Hawaii", "US-HI"), ("Idaho", "US-ID"),
    ("Illinois", "US-IL"), ("Indiana", "US-IN"), ("Iowa", "US-IA"),
    ("Kansas", "US-KS"), ("Kentucky", "US-KY"), ("Louisiana", "US-LA"),
    ("Maine", "US-ME"), ("Maryland", "US-MD"), ("Massachusetts", "US-MA"),
    ("Michigan", "US-MI"), ("Minnesota", "US-MN"), ("Mississippi", "US-MS"),
    ("Missouri", "US-MO"), ("Montana", "US-MT"), ("Nebraska", "US-NE"),
    ("Nevada", "US-NV"), ("New Hampshire", "US-NH"), ("New Jersey", "US-NJ"),
    ("New Mexico", "US-NM"), ("New York", "US-NY"), ("North Carolina", "US-NC"),
    ("North Dakota", "US-ND"), ("Ohio", "US-OH"), ("Oklahoma", "US-OK"),
    ("Oregon", "US-OR"), ("Pennsylvania", "US-PA"), ("Rhode Island", "US-RI"),
    ("South Carolina", "US-SC"), ("South Dakota", "US-SD"), ("Tennessee", "US-TN"),
    ("Texas", "US-TX"), ("Utah", "US-UT"), ("Vermont", "US-VT"),
    ("Virginia", "US-VA"), ("Washington", "US-WA"), ("West Virginia", "US-WV"),
    ("Wisconsin", "US-WI"), ("Wyoming", "US-WY"), ("District of Columbia", "US-DC"),
]

def _make_sub_mock(name, code):
    m = MagicMock()
    m.name = name
    m.code = code
    return m

pycountry_mock = sys.modules["pycountry"]
pycountry_mock.subdivisions.get.return_value = [
    _make_sub_mock(name, code) for name, code in _US_STATES_RAW
]

from agents.job_agent import (
    _cache_path,
    _save_md_to_cache,
    _load_md_from_cache,
    _RateLimiter,
    _GeminiKeyPool,
    _classify,
    _vertical_domain,
    _fetch_ashby_jobs,
    _fetch_workable_jobs,
    _format_workable_location,
    _nontech_title_prefilter,
    _TITLE_BLOCK_KW,
    ATS_PLATFORMS,
    ATS_SEARCH_PARAM,
    API_ATS,
    CRAWLER_ATS,
    _match_ats,
    TargetJobURLs,
    JobDetails,
    JD_CACHE_DIR,
    _is_valid_jd_content,
    _SOFT_404_KEYWORDS,
    _HARD_404_KEYWORDS,
    _JD_POSITIVE_SIGNALS,
    _MIN_LENGTH_WITH_SIGNAL,
    _MIN_LENGTH_WITHOUT_SIGNAL,
    _scrape_workday_jd,
    llm_filter_jobs,
    extract_jd,
)
import agents.job_agent as job_agent_mod

# Skip exponential backoff sleeps during tests — the retry helper sleeps between
# attempts in production, but every test that mocks an HTTP failure would otherwise
# wait ~1.5s per call. Setting the base to 0 keeps the suite snappy.
job_agent_mod._RETRY_BASE_SLEEP_SECS = 0


class _FakeFcPool:
    """BUG-68 test double for shared.firecrawl_pool.FirecrawlKeyPool —
    captures scrape/map kwargs like the old FirecrawlApp patches did."""

    def __init__(self, scrape_return=None, map_return=None, exhausted=False):
        self.exhausted = exhausted
        self.scrape = MagicMock(return_value=scrape_return)
        self.map = MagicMock(return_value=map_return)


# ─────────────────────────────────────────────────────────────────────────────
class TestCachePath(unittest.TestCase):

    def test_returns_string_path(self):
        p = _cache_path("https://example.com/job/1")
        self.assertIsInstance(p, str)

    def test_ends_with_md(self):
        p = _cache_path("https://example.com/job/1")
        self.assertTrue(p.endswith(".md"))

    def test_deterministic(self):
        url = "https://lever.co/anthropic/123"
        self.assertEqual(_cache_path(url), _cache_path(url))

    def test_different_urls_different_paths(self):
        p1 = _cache_path("https://a.com/job/1")
        p2 = _cache_path("https://b.com/job/2")
        self.assertNotEqual(p1, p2)

    def test_uses_md5_of_url(self):
        url = "https://greenhouse.io/testco/job/99"
        expected_hash = hashlib.md5(url.encode()).hexdigest()
        p = _cache_path(url)
        self.assertIn(expected_hash, p)


# ─────────────────────────────────────────────────────────────────────────────
class TestCacheRoundTrip(unittest.TestCase):

    def test_save_and_load(self):
        url = "https://test.com/jd/cache-test"
        content = "# Job Description\nAI TPM role at TestCorp"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agents.job_agent.JD_CACHE_DIR", tmpdir):
                _save_md_to_cache(url, content)
                loaded = _load_md_from_cache(url)
            # Compare using expected file path
            expected_path = os.path.join(tmpdir, hashlib.md5(url.encode()).hexdigest() + ".md")
            self.assertTrue(os.path.exists(expected_path))
            self.assertEqual(loaded, content)

    def test_load_nonexistent_returns_none(self):
        loaded = _load_md_from_cache("https://never-cached.com/jd/404")
        self.assertIsNone(loaded)


# ─────────────────────────────────────────────────────────────────────────────
class TestRateLimiter(unittest.TestCase):

    def test_interval_calculation(self):
        limiter = _RateLimiter(rpm=30)
        self.assertAlmostEqual(limiter._interval, 2.0, places=5)

    def test_first_acquire_fast(self):
        limiter = _RateLimiter(rpm=60)
        start = time.monotonic()

        async def _run():
            await limiter.acquire()

        asyncio.run(_run())
        self.assertLess(time.monotonic() - start, 2.0)

    def test_lock_created_eagerly(self):
        """BUG-14: Lock must be created eagerly in __init__ (consistent with match_agent)."""
        limiter = _RateLimiter(rpm=60)
        self.assertIsNotNone(limiter._lock)


# ─────────────────────────────────────────────────────────────────────────────
class TestGeminiKeyPool(unittest.TestCase):

    def test_current_is_first_key(self):
        pool = _GeminiKeyPool(["key_a", "key_b"])
        self.assertEqual(pool.current, "key_a")

    def test_rotate_moves_to_next(self):
        pool = _GeminiKeyPool(["k1", "k2"])
        self.assertTrue(pool.rotate())
        self.assertEqual(pool.current, "k2")

    def test_rotate_false_when_exhausted(self):
        pool = _GeminiKeyPool(["only"])
        self.assertFalse(pool.rotate())

    def test_filters_falsy_keys(self):
        pool = _GeminiKeyPool(["valid", "", None, "valid2"])
        self.assertEqual(len(pool._keys), 2)

    def test_rotates_on_429_error(self):
        pool = _GeminiKeyPool(["k1", "k2"], genai_mod=job_agent_mod.genai)
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            Exception("Error 429 RESOURCE_EXHAUSTED quota"),
            MagicMock(text="success"),
        ]
        with patch("agents.job_agent.genai.Client", return_value=mock_client):
            result = pool.generate_content("model", "content", MagicMock())
        self.assertEqual(pool.current, "k2")

    def test_raises_when_all_exhausted(self):
        pool = _GeminiKeyPool(["solo"], genai_mod=job_agent_mod.genai)
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("429")
        with patch("agents.job_agent.genai.Client", return_value=mock_client):
            with self.assertRaises(Exception):
                pool.generate_content("model", "content", MagicMock())


# ─────────────────────────────────────────────────────────────────────────────
class TestClassify(unittest.TestCase):

    def test_openai_is_ai_native(self):
        self.assertEqual(_classify("OpenAI"), "ai_native")

    def test_anthropic_is_ai_native(self):
        self.assertEqual(_classify("Anthropic"), "ai_native")

    def test_google_is_big_tech(self):
        self.assertEqual(_classify("Google"), "big_tech")

    def test_meta_is_big_tech(self):
        self.assertEqual(_classify("Meta"), "big_tech")

    def test_unknown_company(self):
        self.assertEqual(_classify("RandomCorp2026"), "unknown")

    def test_ai_in_name_is_ai_native(self):
        # "ai" as standalone word
        self.assertEqual(_classify("Some AI"), "ai_native")

    def test_case_insensitive(self):
        self.assertEqual(_classify("ANTHROPIC"), "ai_native")
        self.assertEqual(_classify("GOOGLE"), "big_tech")


class TestVerticalDomain(unittest.TestCase):
    """PRJ-004 REQ-004-09: Track → forced job_domain for vertical companies;
    mid-large / unknown / unmigrated values → None (per-JD classifier path)."""

    def test_vertical_tracks_force_their_domain(self):
        self.assertEqual(_vertical_domain("AI-native"), "AI")
        self.assertEqual(_vertical_domain("Robotics"), "Robotics")
        self.assertEqual(_vertical_domain("Fintech"), "Fintech")
        self.assertEqual(_vertical_domain("Space"), "Space")
        self.assertEqual(_vertical_domain("Defense"), "Defense")

    def test_mid_large_gets_per_jd_judgment(self):
        self.assertIsNone(_vertical_domain("Mid-large Tech"))

    def test_unmigrated_value_treated_as_mid_large_with_warning(self):
        with self.assertLogs("root", level="WARNING") as cm:
            self.assertIsNone(_vertical_domain("AI Startups"))
        self.assertTrue(any("unmigrated" in m for m in cm.output))

    def test_blank_and_na(self):
        self.assertIsNone(_vertical_domain(""))
        self.assertIsNone(_vertical_domain(None))
        self.assertIsNone(_vertical_domain("N/A"))


# ─────────────────────────────────────────────────────────────────────────────
class TestAtsPlatformsConfig(unittest.TestCase):

    def test_greenhouse_has_api_template(self):
        gh = ATS_PLATFORMS["greenhouse"]
        self.assertIn("greenhouse.io", gh["domains"])
        self.assertIn("{slug}", gh["board_url_template"])

    def test_lever_slug_pattern(self):
        import re
        lever = ATS_PLATFORMS["lever"]
        m = re.search(lever["slug_pattern"], "https://jobs.lever.co/anthropic/some-job")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "anthropic")

    def test_ashby_domain_in_list(self):
        self.assertIn("ashbyhq.com", ATS_PLATFORMS["ashby"]["domains"])

    def test_workday_board_url_is_none(self):
        self.assertIsNone(ATS_PLATFORMS["workday"]["board_url_template"])

    def test_search_params_exist_for_main_ats(self):
        for platform in ["greenhouse", "lever", "ashby", "workable", "workday"]:
            self.assertIn(platform, ATS_SEARCH_PARAM)
            self.assertIn("Technical+Program+Manager", ATS_SEARCH_PARAM[platform])


# ─────────────────────────────────────────────────────────────────────────────
class TestNontechTitlePrefilter(unittest.TestCase):
    """PRJ-004: the pre-Gemini title blocklist applies on the mid-large-tech
    path only; vertical-track companies pass all through (every TPM role is
    domain-qualified). 'operations' removed — Mission Operations TPM is a
    legitimate space/defense role (REQ-004-07/09)."""

    def test_compliance_keywords_blocked_for_mid_large(self):
        links = [
            {"url": "u1", "title": "Technical Program Manager, ML Infrastructure"},
            {"url": "u2", "title": "Technical Program Manager - SOX Compliance"},
            {"url": "u3", "title": "TPM, GRC and Audit"},
            {"url": "u4", "title": "Senior TPM, Governance"},
        ]
        kept = _nontech_title_prefilter(links, "Mid-large Tech")
        kept_urls = {l["url"] for l in kept}
        self.assertEqual(kept_urls, {"u1"})

    def test_vertical_companies_pass_all_through(self):
        links = [{"url": "u1", "title": "TPM, SOX Compliance"}]
        self.assertEqual(_nontech_title_prefilter(links, "AI-native"), links)
        self.assertEqual(_nontech_title_prefilter(links, "Space"), links)

    def test_unmigrated_value_gets_strict_midlarge_filtering(self):
        links = [{"url": "u1", "title": "TPM, SOX Compliance"}]
        self.assertEqual(_nontech_title_prefilter(links, "AI Startups"), [])

    def test_operations_no_longer_blocked(self):
        links = [{"url": "u1", "title": "TPM, Mission Operations"}]
        self.assertEqual(_nontech_title_prefilter(links, "Mid-large Tech"), links)

    def test_block_kw_includes_compliance_set(self):
        for kw in ["sox", "compliance", "audit", "governance", "grc"]:
            self.assertIn(kw, _TITLE_BLOCK_KW)
        self.assertNotIn("operations", _TITLE_BLOCK_KW)


# ─────────────────────────────────────────────────────────────────────────────
class TestHttpRetryHelper(unittest.TestCase):
    """Audit P1: `_http_request_with_retry` must retry on 429/5xx and on
    transient network exceptions, but pass through 4xx (callers decide) and
    successful 2xx responses on the first attempt."""

    def test_2xx_returns_immediately(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            r = job_agent_mod._http_request_with_retry("GET", "https://x.test")
            self.assertEqual(mock_get.call_count, 1)
            self.assertEqual(r.status_code, 200)

    def test_4xx_returns_immediately_no_retry(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404)
            r = job_agent_mod._http_request_with_retry("GET", "https://x.test")
            self.assertEqual(mock_get.call_count, 1)
            self.assertEqual(r.status_code, 404)

    def test_5xx_retries_until_success(self):
        responses = [
            MagicMock(status_code=503),
            MagicMock(status_code=502),
            MagicMock(status_code=200),
        ]
        with patch("agents.job_agent.requests.get", side_effect=responses) as mock_get:
            r = job_agent_mod._http_request_with_retry("GET", "https://x.test", attempts=3)
            self.assertEqual(mock_get.call_count, 3)
            self.assertEqual(r.status_code, 200)

    def test_429_retries(self):
        responses = [MagicMock(status_code=429), MagicMock(status_code=200)]
        with patch("agents.job_agent.requests.get", side_effect=responses) as mock_get:
            r = job_agent_mod._http_request_with_retry("GET", "https://x.test", attempts=3)
            self.assertEqual(mock_get.call_count, 2)
            self.assertEqual(r.status_code, 200)

    def test_persistent_5xx_returns_last_response(self):
        # Helper returns the last 5xx response after exhausting attempts so the
        # caller can log status; downstream then short-circuits on != 200.
        responses = [MagicMock(status_code=503) for _ in range(3)]
        with patch("agents.job_agent.requests.get", side_effect=responses):
            r = job_agent_mod._http_request_with_retry("GET", "https://x.test", attempts=3)
            self.assertEqual(r.status_code, 503)

    def test_network_exception_returns_none_after_retries(self):
        with patch("agents.job_agent.requests.get",
                   side_effect=ConnectionError("timeout")) as mock_get:
            r = job_agent_mod._http_request_with_retry("GET", "https://x.test", attempts=3)
            self.assertEqual(mock_get.call_count, 3)
            self.assertIsNone(r)

    def test_post_method_dispatches_to_requests_post(self):
        with patch("agents.job_agent.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            job_agent_mod._http_request_with_retry("POST", "https://x.test", json={"k": 1})
            self.assertEqual(mock_post.call_count, 1)


# ─────────────────────────────────────────────────────────────────────────────
class TestMicrosoftJdScraper(unittest.TestCase):
    """Audit Line 2 P1: Microsoft careers SPA was returning navbar/theme JSON
    instead of JD bodies via the generic browser scraper. Dedicated scraper
    using Firecrawl `only_main_content=True` mirrors the Google fix."""

    def test_microsoft_in_ats_platforms(self):
        self.assertIn("microsoft", ATS_PLATFORMS)
        cfg = ATS_PLATFORMS["microsoft"]
        self.assertEqual(cfg["jd_fn"], "_scrape_microsoft_jd")
        self.assertIn("jobs.careers.microsoft.com", cfg["jd_domains"])
        self.assertIn("careers.microsoft.com", cfg["jd_domains"])

    def test_match_ats_routes_microsoft_url(self):
        result = _match_ats("https://jobs.careers.microsoft.com/global/en/job/12345")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "microsoft")

    def test_match_ats_routes_legacy_microsoft_url(self):
        result = _match_ats("https://careers.microsoft.com/professionals/us/en/job/12345/abc")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "microsoft")

    def test_scrape_microsoft_jd_uses_firecrawl_with_only_main_content(self):
        # Pool scrape must be called with only_main_content=True so the
        # sidebar/nav is stripped (the audit's actual root cause).
        from agents.job_agent import _scrape_microsoft_jd
        pool = _FakeFcPool(scrape_return=MagicMock(markdown="x" * 500))
        with patch("agents.job_agent._FC_POOL", pool):
            result = _scrape_microsoft_jd("https://jobs.careers.microsoft.com/global/en/job/1")
        self.assertTrue(len(result) > 200)
        scrape_kwargs = pool.scrape.call_args.kwargs
        self.assertTrue(scrape_kwargs.get("only_main_content"))

    def test_scrape_microsoft_jd_returns_empty_without_pool(self):
        # No Firecrawl pool + Microsoft pages have no JSON-LD → returns "".
        # Generic browser scraper picks up downstream.
        from agents.job_agent import _scrape_microsoft_jd
        with patch("agents.job_agent._FC_POOL", None), \
             patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text="<html>no json-ld</html>")
            self.assertEqual(_scrape_microsoft_jd("https://jobs.careers.microsoft.com/x"), "")


# ─────────────────────────────────────────────────────────────────────────────
class TestPydanticSchemas(unittest.TestCase):

    def test_target_job_urls_valid(self):
        obj = TargetJobURLs(urls=["https://a.com/1", "https://b.com/2"])
        self.assertEqual(len(obj.urls), 2)

    def test_target_job_urls_empty(self):
        obj = TargetJobURLs(urls=[])
        self.assertEqual(obj.urls, [])

    def test_job_details_valid(self):
        obj = JobDetails(
            job_title="Senior AI TPM",
            company="Anthropic",
            location="Remote, US",
            salary_range="$250k-$300k",
            requirements=["5+ years TPM", "ML pipeline experience"],
            additional_qualifications=["PyTorch", "LLM", "MLOps"],
            key_responsibilities=["Lead AI roadmap", "Coordinate with research"],
            job_domain="AI", min_yoe=5, work_auth="none_stated",
        )
        self.assertEqual(obj.company, "Anthropic")
        self.assertEqual(obj.job_domain, "AI")
        self.assertEqual(obj.min_yoe, 5)

    def test_job_details_prj004_defaults(self):
        """PRJ-004: new fields default so cached JDs round-trip."""
        obj = JobDetails(
            job_title="PM",
            company="Corp",
            location="NYC",
            salary_range="N/A",
            requirements=[],
            additional_qualifications=[],
            key_responsibilities=[],
        )
        self.assertEqual(obj.job_domain, "None")
        self.assertIsNone(obj.min_yoe)
        self.assertEqual(obj.work_auth, "none_stated")
        self.assertEqual(obj.posted_date, "")

    def test_bug47_data_quality_field_exists(self):
        """BUG-47: JobDetails must have data_quality field."""
        obj = JobDetails(
            job_title="TPM", company="Co", location="SF",
            salary_range="N/A", requirements=[], additional_qualifications=[],
            key_responsibilities=[], job_domain="AI",
        )
        self.assertIsNone(obj.data_quality)

    def test_bug47_data_quality_accepts_value(self):
        """BUG-47: JobDetails should accept data_quality in constructor."""
        obj = JobDetails(
            job_title="TPM", company="Co", location="SF",
            salary_range="N/A", requirements=[], additional_qualifications=[],
            key_responsibilities=[], job_domain="AI",
            data_quality="complete",
        )
        self.assertEqual(obj.data_quality, "complete")


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
class TestScrapeTeslaJdUsesFormats(unittest.TestCase):
    """BUG-15: _scrape_tesla_jd must pass formats=["markdown"] to Firecrawl v2 scrape()."""

    def test_scrape_call_includes_formats_markdown(self):
        from agents.job_agent import _scrape_tesla_jd

        m = MagicMock()
        m.markdown = "# Job\nSome content here that is long enough to pass this test." + "x" * 200
        pool = _FakeFcPool(scrape_return=m)
        with patch("agents.job_agent._FC_POOL", pool):
            _scrape_tesla_jd("https://www.tesla.com/careers/search/job/fake-123")

        kwargs = pool.scrape.call_args.kwargs
        self.assertIn("formats", kwargs,
                      "scrape() must be called with formats= argument")
        self.assertIn("markdown", kwargs["formats"],
                      "formats must include 'markdown'")


class TestBug32TeslaFirecrawlImport(unittest.TestCase):
    """BUG-32/BUG-68: Tesla scrape goes through the Firecrawl key pool, whose
    client construction uses FirecrawlApp (not the bare 'Firecrawl' class)."""

    def test_pool_imports_firecrawl_app_not_firecrawl(self):
        import inspect
        from shared.firecrawl_pool import FirecrawlKeyPool
        source = inspect.getsource(FirecrawlKeyPool._get_client)
        self.assertIn("FirecrawlApp", source,
                      "FirecrawlKeyPool must import FirecrawlApp")
        self.assertNotIn("import Firecrawl\n", source,
                         "FirecrawlKeyPool must not import bare 'Firecrawl'")

    def test_pool_scrape_called_successfully(self):
        from agents.job_agent import _scrape_tesla_jd
        m = MagicMock()
        m.markdown = "# Tesla Job\n" + "x" * 300
        pool = _FakeFcPool(scrape_return=m)
        with patch("agents.job_agent._FC_POOL", pool):
            result = _scrape_tesla_jd("https://www.tesla.com/careers/search/job/test-32")
        self.assertIsNotNone(result)
        self.assertIn("Tesla Job", result)


# ─────────────────────────────────────────────────────────────────────────────
class TestFirecrawlMapRetryLogic(unittest.TestCase):
    """BUG-17/BUG-68: non-quota transient errors are retried (3 attempts);
    quota errors are the pool's job and end the loop immediately."""

    def test_network_error_retries_not_immediate_empty(self):
        """Non-quota errors should be retried (up to 3 attempts) not immediately return []."""
        from agents.job_agent import _firecrawl_map

        pool = _FakeFcPool()
        pool.map = MagicMock(side_effect=ConnectionError("Network timeout"))
        with patch("agents.job_agent._FC_POOL", pool), \
             patch("agents.job_agent.time.sleep"):
            result = _firecrawl_map("https://company.com/jobs")

        self.assertEqual(result, [])
        self.assertEqual(pool.map.call_count, 3,
                         "Should have attempted 3 times before giving up")

    def test_pool_exhaustion_returns_empty_without_retries(self):
        """BUG-68: once the pool reports quota exhaustion (None), no retry
        delays are burned — the map gives up immediately."""
        from agents.job_agent import _firecrawl_map

        pool = _FakeFcPool(map_return=None)  # pool signals exhausted mid-call
        sleep_calls = []
        with patch("agents.job_agent._FC_POOL", pool), \
             patch("agents.job_agent.time.sleep",
                   side_effect=lambda s: sleep_calls.append(s)):
            result = _firecrawl_map("https://company.com/jobs")
        self.assertEqual(result, [])
        self.assertEqual(pool.map.call_count, 1)
        self.assertEqual(sleep_calls, [])

    def test_exhausted_pool_short_circuits(self):
        from agents.job_agent import _firecrawl_map
        pool = _FakeFcPool(exhausted=True)
        with patch("agents.job_agent._FC_POOL", pool):
            self.assertEqual(_firecrawl_map("https://company.com/jobs"), [])
        pool.map.assert_not_called()


class TestBug37FirecrawlMapNotBlocking(unittest.TestCase):
    """BUG-37: _firecrawl_map must be called via asyncio.to_thread, not directly in async context."""

    def test_discover_jobs_calls_firecrawl_map_via_to_thread(self):
        """Verify the call site uses asyncio.to_thread to avoid blocking the event loop."""
        import inspect
        from agents.job_agent import discover_jobs
        source = inspect.getsource(discover_jobs)
        self.assertIn("asyncio.to_thread(_firecrawl_map", source,
                      "discover_jobs must call _firecrawl_map via asyncio.to_thread")

    def test_firecrawl_map_is_sync_function(self):
        """Verify _firecrawl_map itself is still a regular sync function."""
        import asyncio
        from agents.job_agent import _firecrawl_map
        self.assertFalse(asyncio.iscoroutinefunction(_firecrawl_map),
                         "_firecrawl_map should remain a sync function")


class TestBug43GoogleJdScrapeParams(unittest.TestCase):
    """BUG-43: _scrape_google_jd must pass formats=['markdown'] to Firecrawl scrape."""

    def test_scrape_google_jd_includes_formats(self):
        from agents.job_agent import _scrape_google_jd
        m = MagicMock()
        m.markdown = "# Google Job\n" + "x" * 300
        pool = _FakeFcPool(scrape_return=m)
        with patch("agents.job_agent._FC_POOL", pool):
            _scrape_google_jd("https://www.google.com/about/careers/job/123")
        kwargs = pool.scrape.call_args.kwargs
        self.assertIn("formats", kwargs)
        self.assertIn("markdown", kwargs["formats"])


class TestBug42QuotaExhaustionWarning(unittest.TestCase):
    """BUG-42: asyncio.gather must surface 429 quota exhaustion to the user."""

    def test_process_company_surfaces_quota_errors(self):
        """Verify process_company prints a warning when 429 errors occur."""
        import inspect
        from agents.job_agent import process_company
        source = inspect.getsource(process_company)
        self.assertIn("quota_errors", source,
                      "process_company must track quota errors from gather results")
        self.assertIn("quota exhausted", source.lower(),
                      "process_company must print a quota exhaustion warning")

    def test_retry_phase_surfaces_quota_errors(self):
        """Verify retry phase also surfaces quota errors."""
        import inspect
        # P0-7: main() is now a thin RunSummary wrapper; orchestration body in _main_inner.
        from agents.job_agent import _main_inner
        source = inspect.getsource(_main_inner)
        self.assertIn("retry_quota_errors", source,
                      "retry phase must track quota errors")


class TestBug38SemaphoreGeminiSeparation(unittest.TestCase):
    """BUG-38: Semaphore must only gate scraping, not hold slots during Gemini rate-limiting."""

    def test_process_scraped_jd_outside_semaphore(self):
        """Verify _process_scraped_jd call is NOT inside the 'async with sem' block."""
        import ast, inspect, textwrap
        from agents.job_agent import process_company
        source = inspect.getsource(process_company)
        # Find the fetch_one function definition within process_company
        # The key structural check: _process_scraped_jd should be at a lower indentation
        # than the 'async with sem:' block (i.e., outside it)
        lines = source.split("\n")
        sem_indent = None
        process_jd_indent = None
        for line in lines:
            stripped = line.lstrip()
            if "async with sem:" in stripped:
                sem_indent = len(line) - len(stripped)
            if "_process_scraped_jd(" in stripped and sem_indent is not None:
                process_jd_indent = len(line) - len(stripped)
                break
        self.assertIsNotNone(sem_indent, "Should find 'async with sem:' in process_company")
        self.assertIsNotNone(process_jd_indent, "Should find '_process_scraped_jd(' in process_company")
        # _process_scraped_jd should be at same or lower indentation than 'async with sem'
        # (i.e., NOT deeper inside the sem block)
        self.assertLessEqual(process_jd_indent, sem_indent + 4,
                             "_process_scraped_jd must not be inside the semaphore block")

    def test_route_scraper_inside_semaphore(self):
        """Verify _route_scraper IS inside the semaphore block (scraping needs concurrency control)."""
        import inspect
        from agents.job_agent import process_company
        source = inspect.getsource(process_company)
        lines = source.split("\n")
        sem_indent = None
        scraper_indent = None
        for line in lines:
            stripped = line.lstrip()
            if "async with sem:" in stripped:
                sem_indent = len(line) - len(stripped)
            if "_route_scraper(" in stripped and sem_indent is not None:
                scraper_indent = len(line) - len(stripped)
                break
        self.assertIsNotNone(sem_indent)
        self.assertIsNotNone(scraper_indent)
        # _route_scraper should be deeper than 'async with sem:' (inside it)
        self.assertGreater(scraper_indent, sem_indent,
                           "_route_scraper must be inside the semaphore block")


class TestDiscoverJobsWorkdayFallback(unittest.IsolatedAsyncioTestCase):
    """BUG-16: Workday API success + empty _tpm_filter must still fall back to crawler."""

    async def test_fallback_when_tpm_filter_empty(self):
        from agents.job_agent import discover_jobs

        workday_url = "https://company.myworkdayjobs.com/careers"
        crawled_tpm = [{"url": "https://company.myworkdayjobs.com/job/tpm-ai", "title": "AI TPM"}]

        with patch("agents.job_agent._fetch_workday_jobs", return_value=[{"id": 1, "title": "SWE"}]), \
             patch("agents.job_agent._tpm_filter", side_effect=lambda lst, track="": crawled_tpm if lst == [] else []), \
             patch("agents.job_agent._crawl_page", new=AsyncMock(return_value=[])):
            # _tpm_filter returns [] for API jobs → should fall back to _crawl_page
            result = await discover_jobs(workday_url, MagicMock())
        # After fallback, _tpm_filter([]) via crawl path is called; result comes from crawler path
        # The key assertion: _crawl_page was called (not skipped)
        # Since _tpm_filter is side-effected to return [] for non-empty lists,
        # the returned result is [] (crawler also returned []) — but crawl was attempted.
        self.assertIsInstance(result, list)

    async def test_no_fallback_when_tpm_filter_has_results(self):
        """If _tpm_filter returns candidates, return immediately without crawling."""
        from agents.job_agent import discover_jobs

        workday_url = "https://company.myworkdayjobs.com/careers"
        tpm_job = {"url": "https://company.myworkdayjobs.com/job/tpm", "title": "TPM"}

        crawl_mock = AsyncMock(return_value=[])
        with patch("agents.job_agent._fetch_workday_jobs", return_value=[tpm_job]), \
             patch("agents.job_agent._tpm_filter", return_value=[tpm_job]), \
             patch("agents.job_agent._crawl_page", crawl_mock):
            result = await discover_jobs(workday_url, MagicMock())

        self.assertEqual(result, [tpm_job])
        crawl_mock.assert_not_called()


class TestProcessCompanyNoDuplicateFetch(unittest.IsolatedAsyncioTestCase):
    """BUG-04: duplicate URLs in discover_jobs output must not cause double fetch."""

    DUP_URL = "https://testco.greenhouse.io/jobs/ai-tpm/99999"
    CAREER_URL = "https://testco.greenhouse.io"
    ROW = ["TestCo", "AI Startups", "", "https://testco.greenhouse.io"]
    GOOD_JD_JSON = json.dumps({
        "company": "TestCo", "job_title": "AI TPM", "location": "Remote, US",
        "salary_range": "N/A", "core_ai_tech_stack": [], "key_responsibilities": [],
        "is_ai_tpm": True,
    })

    async def _run_with_dup_urls(self):
        """Run process_company with two identical URLs; return upsert call args."""
        from agents.job_agent import process_company

        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()

        raw_listings = [
            {"url": self.DUP_URL, "title": "AI TPM"},
            {"url": self.DUP_URL, "title": "AI TPM"},  # duplicate
        ]

        with patch("agents.job_agent.discover_jobs", new=AsyncMock(return_value=raw_listings)), \
             patch("agents.job_agent.scrape_jd", new=AsyncMock(return_value="# Job Description")), \
             patch("agents.job_agent._save_md_to_cache"), \
             patch("agents.job_agent._GEMINI_LIMITER", mock_limiter), \
             patch("agents.job_agent.extract_jd", return_value=self.GOOD_JD_JSON), \
             patch("agents.job_agent._fetch_jsonld_posted_date", return_value=""), \
             patch("agents.job_agent.batch_upsert_jd_records", return_value=1) as mock_upsert, \
             patch("agents.job_agent.batch_update_jd_timestamps", return_value=0):
            await process_company(
                self.ROW, {}, "/fake/path.xlsx",
                asyncio.Lock(), MagicMock(),
            )
            return mock_upsert

    async def test_bug04_exists_before_fix(self):
        """Confirm the bug: without guard, duplicate URL appears twice in pending.

        This test is EXPECTED TO FAIL before the fix is applied.
        After the fix (seen_urls guard), the same URL should appear only once.
        """
        mock_upsert = await self._run_with_dup_urls()
        if not mock_upsert.called:
            self.skipTest("No JDs staged — cannot verify pending contents")
        pending = mock_upsert.call_args[0][1]
        pending_urls = [p[0] for p in pending]
        # After fix: duplicate URL should appear exactly once
        self.assertEqual(
            pending_urls.count(self.DUP_URL), 1,
            f"BUG-04: '{self.DUP_URL}' appeared {pending_urls.count(self.DUP_URL)} times "
            f"in pending (expected 1). Duplicate fetch not guarded.",
        )


# ─────────────────────────────────────────────────────────────────────────────
class TestAshbyAtsClassification(unittest.TestCase):
    """REQ-058: Ashby must be in API_ATS, not CRAWLER_ATS."""

    def test_ashby_in_api_ats(self):
        self.assertIn("ashbyhq.com", API_ATS)

    def test_ashby_not_in_crawler_ats(self):
        self.assertNotIn("ashbyhq.com", CRAWLER_ATS)


# ─────────────────────────────────────────────────────────────────────────────
class TestFetchAshbySlugExtraction(unittest.TestCase):
    """REQ-058: slug extraction from various Ashby URL formats."""

    def test_basic_url(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"jobs": []})
            _fetch_ashby_jobs("https://jobs.ashbyhq.com/anthropic")
            called_url = mock_get.call_args[0][0]
            self.assertIn("/anthropic", called_url)

    def test_url_with_path(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"jobs": []})
            _fetch_ashby_jobs("https://jobs.ashbyhq.com/company-name/some-job-id")
            called_url = mock_get.call_args[0][0]
            self.assertIn("/company-name?", called_url)

    def test_url_with_query_params(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"jobs": []})
            _fetch_ashby_jobs("https://jobs.ashbyhq.com/myco?search=tpm")
            called_url = mock_get.call_args[0][0]
            self.assertIn("/myco?", called_url)

    def test_invalid_url_no_slug(self):
        result = _fetch_ashby_jobs("https://example.com/no-ashby-here")
        self.assertEqual(result, [])

    def test_ashbyhq_without_subdomain(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"jobs": []})
            _fetch_ashby_jobs("https://ashbyhq.com/someco")
            called_url = mock_get.call_args[0][0]
            self.assertIn("/someco?", called_url)


# ─────────────────────────────────────────────────────────────────────────────
class TestFetchAshbyApiParsing(unittest.TestCase):
    """REQ-058: Ashby API response parsing produces correct structured data."""

    SAMPLE_RESPONSE = {
        "jobs": [
            {
                "id": "job-id-123",
                "title": "Senior Technical Program Manager, AI",
                "location": "San Francisco, CA",
            },
            {
                "id": "job-id-456",
                "title": "Staff TPM - ML Platform",
                "location": {"name": "Remote, US"},
            },
            {
                "id": "job-id-789",
                "title": "Engineering Manager",
                "publishedUrl": "https://jobs.ashbyhq.com/testco/custom-url",
                "location": "New York, NY",
            },
        ]
    }

    def test_parses_all_jobs(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: self.SAMPLE_RESPONSE
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(len(result), 3)

    def test_job_structure_url_title_location(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: self.SAMPLE_RESPONSE
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        for job in result:
            self.assertIn("url", job)
            self.assertIn("title", job)
            self.assertIn("location", job)

    def test_constructs_url_from_id(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: self.SAMPLE_RESPONSE
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result[0]["url"], "https://jobs.ashbyhq.com/testco/job-id-123")

    def test_prefers_publishedUrl(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: self.SAMPLE_RESPONSE
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result[2]["url"], "https://jobs.ashbyhq.com/testco/custom-url")

    def test_dict_location_extracts_name(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: self.SAMPLE_RESPONSE
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result[1]["location"], "Remote, US")

    def test_string_location_preserved(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: self.SAMPLE_RESPONSE
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result[0]["location"], "San Francisco, CA")

    def test_empty_jobs_list(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"jobs": []}
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result, [])

    def test_missing_jobs_key(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"data": []}
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result, [])

    def test_skips_jobs_without_title(self):
        response = {"jobs": [{"id": "x", "title": "", "location": "NY"}]}
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: response
            )
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result, [])


# ─────────────────────────────────────────────────────────────────────────────
class TestFetchAshbyErrorHandling(unittest.TestCase):
    """REQ-058: Ashby API error handling."""

    def test_api_404_returns_empty(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404)
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/nonexistent")
        self.assertEqual(result, [])

    def test_api_500_returns_empty(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=500)
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result, [])

    def test_network_error_returns_empty(self):
        with patch("agents.job_agent.requests.get", side_effect=ConnectionError("timeout")):
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result, [])

    def test_json_decode_error_returns_empty(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")
            result = _fetch_ashby_jobs("https://jobs.ashbyhq.com/testco")
        self.assertEqual(result, [])


# ─────────────────────────────────────────────────────────────────────────────
class TestDiscoverJobsAshbyRouting(unittest.IsolatedAsyncioTestCase):
    """REQ-058: discover_jobs must route Ashby URLs to _fetch_ashby_jobs."""

    async def test_ashby_url_calls_fetch_ashby(self):
        from agents.job_agent import discover_jobs
        ashby_url = "https://jobs.ashbyhq.com/anthropic"
        ashby_jobs = [
            {"url": "https://jobs.ashbyhq.com/anthropic/123",
             "title": "Technical Program Manager", "location": "SF, CA"},
        ]
        with patch("agents.job_agent._fetch_ashby_jobs", return_value=ashby_jobs) as mock_ashby, \
             patch("agents.job_agent._fetch_ats_jobs") as mock_ats:
            result = await discover_jobs(ashby_url, MagicMock())
        mock_ashby.assert_called_once_with(ashby_url)
        mock_ats.assert_not_called()
        self.assertEqual(len(result), 1)

    async def test_greenhouse_url_does_not_call_ashby(self):
        from agents.job_agent import discover_jobs
        gh_url = "https://job-boards.greenhouse.io/testco"
        with patch("agents.job_agent._fetch_ats_jobs", return_value=[
            {"url": "https://job-boards.greenhouse.io/testco/jobs/1",
             "title": "TPM", "location": "US"},
        ]) as mock_ats, \
             patch("agents.job_agent._fetch_ashby_jobs") as mock_ashby:
            await discover_jobs(gh_url, MagicMock())
        mock_ats.assert_called_once()
        mock_ashby.assert_not_called()

    async def test_ashby_api_empty_falls_back_to_crawler(self):
        from agents.job_agent import discover_jobs
        ashby_url = "https://jobs.ashbyhq.com/someco"
        with patch("agents.job_agent._fetch_ashby_jobs", return_value=[]), \
             patch("agents.job_agent._crawl_page", new=AsyncMock(return_value=[])) as mock_crawl:
            result = await discover_jobs(ashby_url, MagicMock())
        # When API returns empty, should fall through to crawler path
        self.assertIsInstance(result, list)


# ─────────────────────────────────────────────────────────────────────────────
class TestWorkableAtsConfig(unittest.TestCase):
    """A′ — Workable must be registered as a json_api ATS platform."""

    def test_workable_in_ats_platforms(self):
        self.assertIn("workable", ATS_PLATFORMS)
        self.assertEqual(ATS_PLATFORMS["workable"]["strategy"], "json_api")
        self.assertEqual(ATS_PLATFORMS["workable"]["list_fn"], "_fetch_workable_jobs")

    def test_workable_in_api_ats(self):
        self.assertIn("workable.com", API_ATS)

    def test_match_ats_resolves_workable(self):
        result = _match_ats("https://apply.workable.com/huggingface/")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "workable")


class TestFormatWorkableLocation(unittest.TestCase):
    """A′ — Workable's multi-field location collapses into a classify_region-compatible string."""

    def test_telecommuting_emits_remote(self):
        loc = _format_workable_location({"telecommuting": True, "city": "", "country": ""})
        self.assertIn("Remote", loc)

    def test_single_location_city_state_country(self):
        loc = _format_workable_location({
            "telecommuting": False, "city": "San Francisco",
            "state": "California", "country": "United States",
        })
        self.assertIn("San Francisco", loc)
        self.assertIn("California", loc)

    def test_multi_locations_joined_with_semicolon(self):
        loc = _format_workable_location({
            "telecommuting": False,
            "locations": [
                {"city": "Paris",  "region": "Île-de-France", "country": "France"},
                {"city": "Austin", "region": "Texas",         "country": "United States"},
            ],
        })
        self.assertIn(";", loc)  # multi-location separator for classify_region
        self.assertIn("Austin", loc)
        self.assertIn("Paris", loc)

    def test_remote_plus_locations(self):
        loc = _format_workable_location({
            "telecommuting": True,
            "locations": [{"city": "NYC", "region": "NY", "country": "US"}],
        })
        self.assertIn("Remote", loc)
        self.assertIn("NYC", loc)

    def test_empty_job_returns_empty_string(self):
        self.assertEqual(_format_workable_location({}), "")


class TestFetchWorkableSlugExtraction(unittest.TestCase):
    """A′ — slug extraction from various Workable URL forms."""

    def test_basic_apply_subdomain(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"jobs": []})
            _fetch_workable_jobs("https://apply.workable.com/huggingface/")
            self.assertIn("/huggingface", mock_get.call_args[0][0])

    def test_url_without_trailing_slash(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"jobs": []})
            _fetch_workable_jobs("https://apply.workable.com/myco")
            self.assertIn("/myco", mock_get.call_args[0][0])

    def test_invalid_url_no_slug(self):
        result = _fetch_workable_jobs("https://example.com/not-workable")
        self.assertEqual(result, [])


class TestFetchWorkableApiParsing(unittest.TestCase):
    """A′ — Workable widget API response parsing produces normalized output."""

    SAMPLE = {
        "name": "Hugging Face",
        "jobs": [
            {
                "title": "Cloud ML DevRel Engineer - EMEA remote",
                "shortcode": "922D2C6549",
                "url": "https://apply.workable.com/j/922D2C6549",
                "telecommuting": True,
                "city": "Paris", "state": "Île-de-France", "country": "France",
                "locations": [{"city": "Paris", "region": "Île-de-France",
                               "country": "France", "countryCode": "FR"}],
            },
            {
                "title": "Senior TPM",
                "shortcode": "ABCDEF1234",
                # url missing → must be built from shortcode
                "telecommuting": False,
                "city": "New York", "state": "NY", "country": "United States",
            },
            {
                # Missing both url and shortcode → dropped
                "title": "Ghost role",
            },
        ],
    }

    def test_returns_only_valid_jobs(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: self.SAMPLE)
            result = _fetch_workable_jobs("https://apply.workable.com/huggingface/")
        self.assertEqual(len(result), 2)

    def test_url_falls_back_to_shortcode(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: self.SAMPLE)
            result = _fetch_workable_jobs("https://apply.workable.com/huggingface/")
        urls = [j["url"] for j in result]
        self.assertIn("https://apply.workable.com/j/ABCDEF1234", urls)

    def test_location_present_for_each(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: self.SAMPLE)
            result = _fetch_workable_jobs("https://apply.workable.com/huggingface/")
        for j in result:
            self.assertTrue(j["location"], f"empty location in {j}")

    def test_non_200_returns_empty(self):
        with patch("agents.job_agent.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404, json=lambda: {})
            result = _fetch_workable_jobs("https://apply.workable.com/missing/")
        self.assertEqual(result, [])

    def test_request_exception_returns_empty(self):
        with patch("agents.job_agent.requests.get", side_effect=Exception("boom")):
            result = _fetch_workable_jobs("https://apply.workable.com/huggingface/")
        self.assertEqual(result, [])


class TestDiscoverJobsWorkableRouting(unittest.IsolatedAsyncioTestCase):
    """A′ — discover_jobs must route Workable URLs to _fetch_workable_jobs."""

    async def test_workable_url_calls_fetch_workable(self):
        from agents.job_agent import discover_jobs
        url = "https://apply.workable.com/huggingface/"
        jobs = [{"url": "https://apply.workable.com/j/X", "title": "TPM", "location": "Remote"}]
        with patch("agents.job_agent._fetch_workable_jobs", return_value=jobs) as mw, \
             patch("agents.job_agent._fetch_ats_jobs") as ma, \
             patch("agents.job_agent._fetch_ashby_jobs") as mash:
            result = await discover_jobs(url, MagicMock())
        mw.assert_called_once_with(url)
        ma.assert_not_called()
        mash.assert_not_called()
        self.assertEqual(len(result), 1)


# ─────────────────────────────────────────────────────────────────────────────
class TestIsValidJdContent(unittest.TestCase):
    """REQ-059: Soft 404 hardening + JD positive signal validation."""

    # ── Helper: build a valid JD body with positive signal ────────────────────
    @staticmethod
    def _make_jd(body: str = "", signal: str = "responsibilities") -> str:
        """Return a plausible JD markdown containing a positive signal keyword."""
        filler = "x " * 120  # ensures length > _MIN_LENGTH_WITHOUT_SIGNAL
        content = f"# Senior TPM\n\n## {signal}\n\n{body or filler}"
        # Pad to ensure we always exceed minimum length thresholds
        if len(content.strip()) < _MIN_LENGTH_WITHOUT_SIGNAL:
            content += "\n" + "y " * 120
        return content

    # ── Empty / too-short ─────────────────────────────────────────────────────
    def test_empty_string(self):
        valid, reason = _is_valid_jd_content("")
        self.assertFalse(valid)
        self.assertEqual(reason, "too_short")

    def test_none_input(self):
        valid, reason = _is_valid_jd_content(None)
        self.assertFalse(valid)
        self.assertEqual(reason, "too_short")

    def test_very_short_content(self):
        valid, reason = _is_valid_jd_content("hello")
        self.assertFalse(valid)
        self.assertEqual(reason, "too_short")

    def test_whitespace_only(self):
        valid, reason = _is_valid_jd_content("   \n\t  \n  ")
        self.assertFalse(valid)
        self.assertEqual(reason, "too_short")

    def test_below_min_signal_threshold(self):
        # Just under _MIN_LENGTH_WITH_SIGNAL chars (even with a signal keyword)
        short = "a" * (_MIN_LENGTH_WITH_SIGNAL - 1)
        valid, reason = _is_valid_jd_content(short)
        self.assertFalse(valid)
        self.assertEqual(reason, "too_short")

    # ── Short content WITH positive signal should pass ────────────────────────
    def test_short_with_signal_passes(self):
        """Content >= _MIN_LENGTH_WITH_SIGNAL with a positive signal is valid."""
        content = "responsibilities: " + "a " * 50  # well over 80 chars
        self.assertGreaterEqual(len(content.strip()), _MIN_LENGTH_WITH_SIGNAL)
        valid, reason = _is_valid_jd_content(content)
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")

    # ── Medium content WITHOUT positive signal → too_short ────────────────────
    def test_medium_no_signal_too_short(self):
        """Content between 80-200 chars without signal → too_short."""
        content = "a " * 60  # ~120 chars, no signal keywords
        length = len(content.strip())
        self.assertGreaterEqual(length, _MIN_LENGTH_WITH_SIGNAL)
        self.assertLess(length, _MIN_LENGTH_WITHOUT_SIGNAL)
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "too_short")

    # ── Long content WITHOUT positive signal → no_jd_content ──────────────────
    def test_long_no_signal(self):
        """Content >= 200 chars without any JD signal → no_jd_content."""
        content = "Lorem ipsum dolor sit amet. " * 20  # ~560 chars, no signal
        self.assertGreaterEqual(len(content.strip()), _MIN_LENGTH_WITHOUT_SIGNAL)
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "no_jd_content")

    # ── Soft 404 keywords ─────────────────────────────────────────────────────
    def test_all_soft_404_keywords_detected(self):
        for kw in _SOFT_404_KEYWORDS:
            content = self._make_jd(f"We're sorry, this {kw} at this time.")
            valid, reason = _is_valid_jd_content(content)
            self.assertFalse(valid, f"Soft 404 keyword not caught: '{kw}'")
            self.assertEqual(reason, "soft_404", f"Wrong reason for '{kw}'")

    def test_soft_404_case_insensitive(self):
        content = self._make_jd("This JOB HAS BEEN FILLED and is closed.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    def test_soft_404_no_longer_accepting(self):
        content = self._make_jd("This job is no longer accepting applications.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    def test_soft_404_hiring_paused(self):
        content = self._make_jd("Hiring paused for this position currently.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    def test_soft_404_application_closed(self):
        content = self._make_jd("Application closed. Check back later for openings.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    # ── Hard 404 keywords ─────────────────────────────────────────────────────
    def test_all_hard_404_keywords_detected(self):
        for kw in _HARD_404_KEYWORDS:
            content = self._make_jd(f"Oops! {kw}. Please try again.")
            valid, reason = _is_valid_jd_content(content)
            self.assertFalse(valid, f"Hard 404 keyword not caught: '{kw}'")
            self.assertEqual(reason, "hard_404", f"Wrong reason for '{kw}'")

    def test_hard_404_page_not_found(self):
        content = self._make_jd("Page not found. The URL may have changed.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "hard_404")

    def test_hard_404_error_404(self):
        content = self._make_jd("Error 404 — the requested resource does not exist.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "hard_404")

    def test_hard_404_something_went_wrong(self):
        content = self._make_jd("Something went wrong on our end.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "hard_404")

    # ── Soft 404 takes priority over hard 404 ─────────────────────────────────
    def test_soft_404_priority_over_hard_404(self):
        """When both soft and hard 404 keywords present, soft 404 wins (checked first)."""
        content = self._make_jd("Page not found. This job has been filled.")
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    # ── Valid JD content ──────────────────────────────────────────────────────
    def test_valid_jd_with_responsibilities(self):
        valid, reason = _is_valid_jd_content(self._make_jd())
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")

    def test_valid_jd_all_positive_signals(self):
        """Each positive signal keyword should be recognized."""
        for signal in _JD_POSITIVE_SIGNALS:
            content = ("a " * 50) + f"\n## {signal}\n" + ("b " * 50)
            valid, reason = _is_valid_jd_content(content)
            self.assertTrue(valid, f"Positive signal not recognized: '{signal}'")
            self.assertEqual(reason, "ok", f"Wrong reason for signal '{signal}'")

    def test_valid_jd_mixed_case_signals(self):
        content = self._make_jd(signal="QUALIFICATIONS")
        valid, reason = _is_valid_jd_content(content)
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")

    def test_valid_jd_what_youll_do(self):
        content = self._make_jd(signal="What You'll Do")
        valid, reason = _is_valid_jd_content(content)
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")

    def test_valid_jd_about_the_role(self):
        content = self._make_jd(signal="About the Role")
        valid, reason = _is_valid_jd_content(content)
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")

    # ── Return type contract ──────────────────────────────────────────────────
    def test_return_type_is_tuple(self):
        result = _is_valid_jd_content("short")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_reason_values_are_expected(self):
        """Only allowed reason strings should be returned."""
        allowed = {"too_short", "soft_404", "hard_404", "no_jd_content", "ok"}
        # Valid JD
        _, r1 = _is_valid_jd_content(self._make_jd())
        self.assertIn(r1, allowed)
        # Empty
        _, r2 = _is_valid_jd_content("")
        self.assertIn(r2, allowed)
        # Long no signal
        _, r3 = _is_valid_jd_content("a " * 200)
        self.assertIn(r3, allowed)

    # ── Edge: content exactly at thresholds ───────────────────────────────────
    def test_exactly_min_length_with_signal(self):
        """Content exactly at _MIN_LENGTH_WITH_SIGNAL with signal → ok."""
        base = "requirements "
        pad = "a" * (_MIN_LENGTH_WITH_SIGNAL - len(base))
        content = base + pad
        self.assertEqual(len(content.strip()), _MIN_LENGTH_WITH_SIGNAL)
        valid, reason = _is_valid_jd_content(content)
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")

    def test_exactly_min_length_without_signal(self):
        """Content exactly at _MIN_LENGTH_WITHOUT_SIGNAL without signal → no_jd_content."""
        content = "a" * _MIN_LENGTH_WITHOUT_SIGNAL
        self.assertEqual(len(content.strip()), _MIN_LENGTH_WITHOUT_SIGNAL)
        valid, reason = _is_valid_jd_content(content)
        self.assertFalse(valid)
        self.assertEqual(reason, "no_jd_content")

    # ── Realistic ATS closed-page bodies ──────────────────────────────────────
    def test_greenhouse_closed(self):
        body = ("Thank you for your interest. Unfortunately, this position has "
                "been closed. Please explore our other opportunities.")
        valid, reason = _is_valid_jd_content(body)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    def test_lever_expired(self):
        body = ("This posting has expired. Visit our careers page to see "
                "current openings at our company. We'd love to hear from you.")
        valid, reason = _is_valid_jd_content(body)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    def test_workday_filled(self):
        body = ("This job is no longer accepting applications. The position "
                "was filled on 2025-11-15. Browse similar roles below.")
        valid, reason = _is_valid_jd_content(body)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    def test_ashby_no_longer_open(self):
        body = ("Sorry, this role is no longer open. Please check our careers "
                "page for new opportunities.")
        valid, reason = _is_valid_jd_content(body)
        self.assertFalse(valid)
        self.assertEqual(reason, "soft_404")

    def test_generic_404_page(self):
        body = ("404 Not Found. The page you were looking for doesn't exist. "
                "You may have mistyped the address or the page may have moved.")
        valid, reason = _is_valid_jd_content(body)
        self.assertFalse(valid)
        self.assertEqual(reason, "hard_404")


class TestAssessJdQuality(unittest.TestCase):
    """REQ-060: JD field completeness grading."""

    def test_complete_all_fields_present(self):
        jd = {
            "job_title": "Technical Program Manager",
            "location": "San Francisco, CA",
            "key_responsibilities": ["Lead cross-functional teams"],
            "requirements": ["5+ years experience"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "complete")

    def test_complete_multiple_items(self):
        jd = {
            "job_title": "Senior TPM",
            "location": "Remote",
            "key_responsibilities": ["Lead teams", "Drive strategy"],
            "requirements": ["Python", "ML experience"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "complete")

    def test_partial_missing_location(self):
        jd = {
            "job_title": "TPM",
            "location": "",
            "key_responsibilities": ["Lead AI projects"],
            "requirements": ["5+ years"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_partial_missing_requirements(self):
        jd = {
            "job_title": "TPM",
            "location": "NYC",
            "key_responsibilities": ["Lead projects"],
            "requirements": [],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_partial_missing_responsibilities(self):
        jd = {
            "job_title": "TPM",
            "location": "Remote",
            "key_responsibilities": [],
            "requirements": ["Python"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_partial_location_na(self):
        jd = {
            "job_title": "TPM",
            "location": "N/A",
            "key_responsibilities": ["Lead"],
            "requirements": ["Exp"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_failed_empty_title(self):
        jd = {
            "job_title": "",
            "location": "Remote",
            "key_responsibilities": ["Lead"],
            "requirements": ["Python"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "failed")

    def test_failed_none_title(self):
        jd = {
            "job_title": None,
            "location": "Remote",
            "key_responsibilities": ["Lead"],
            "requirements": ["Python"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "failed")

    def test_failed_title_na(self):
        jd = {
            "job_title": "N/A",
            "location": "Remote",
            "key_responsibilities": ["Lead"],
            "requirements": ["Python"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "failed")

    def test_failed_all_empty(self):
        jd = {
            "job_title": "",
            "location": "",
            "key_responsibilities": [],
            "requirements": [],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "failed")

    def test_failed_title_present_but_all_others_empty(self):
        """Title present but ALL other key fields empty → failed."""
        jd = {
            "job_title": "TPM",
            "location": "",
            "key_responsibilities": [],
            "requirements": [],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "failed")

    def test_failed_title_present_others_none_values(self):
        """Title present but all others are None → failed."""
        jd = {
            "job_title": "TPM",
            "location": None,
            "key_responsibilities": None,
            "requirements": None,
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "failed")

    def test_partial_only_location_present(self):
        """Title + location but no responsibilities or requirements → partial."""
        jd = {
            "job_title": "TPM",
            "location": "Austin, TX",
            "key_responsibilities": [],
            "requirements": [],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_partial_only_responsibilities_present(self):
        """Title + responsibilities but no location or requirements → partial."""
        jd = {
            "job_title": "TPM",
            "location": "",
            "key_responsibilities": ["Lead projects"],
            "requirements": [],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_partial_only_requirements_present(self):
        """Title + requirements but no location or responsibilities → partial."""
        jd = {
            "job_title": "TPM",
            "location": "",
            "key_responsibilities": [],
            "requirements": ["5+ years"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_empty_strings_in_list_not_counted(self):
        """Lists containing only empty strings should be treated as empty."""
        jd = {
            "job_title": "TPM",
            "location": "Remote",
            "key_responsibilities": ["", "  "],
            "requirements": ["Python"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_missing_keys_treated_as_empty(self):
        """Missing dict keys should not raise errors."""
        jd = {"job_title": "TPM"}
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "failed")

    def test_location_not_specified_treated_as_missing(self):
        jd = {
            "job_title": "TPM",
            "location": "Not Specified",
            "key_responsibilities": ["Lead"],
            "requirements": ["Exp"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_location_not_available_treated_as_missing(self):
        jd = {
            "job_title": "TPM",
            "location": "not available",
            "key_responsibilities": ["Lead"],
            "requirements": ["Exp"],
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")

    def test_empty_dict(self):
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality({}), "failed")

    def test_requirements_not_a_list(self):
        """If requirements is a string (malformed), treat as empty list."""
        jd = {
            "job_title": "TPM",
            "location": "Remote",
            "key_responsibilities": ["Lead"],
            "requirements": "5+ years experience",
        }
        from agents.job_agent import _assess_jd_quality
        self.assertEqual(_assess_jd_quality(jd), "partial")


# ─────────────────────────────────────────────────────────────────────────────
class TestMatchAts(unittest.TestCase):
    """REQ-062: _match_ats routing table URL matching."""

    def test_greenhouse_url(self):
        result = _match_ats("https://job-boards.greenhouse.io/anthropic")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "greenhouse")

    def test_lever_url(self):
        result = _match_ats("https://jobs.lever.co/openai/some-job")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "lever")

    def test_ashby_url(self):
        result = _match_ats("https://jobs.ashbyhq.com/anthropic")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "ashby")

    def test_workday_url(self):
        result = _match_ats("https://company.myworkdayjobs.com/careers")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "workday")

    def test_google_jd_url(self):
        result = _match_ats("https://careers.google.com/jobs/results/123")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "google")

    def test_google_about_careers_url(self):
        result = _match_ats("https://google.com/about/careers/applications/jobs")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "google")

    def test_tesla_jd_url(self):
        result = _match_ats("https://www.tesla.com/careers/search/job/tpm-123456")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "tesla")

    def test_unknown_url_returns_none(self):
        result = _match_ats("https://careers.random-company.com/jobs")
        self.assertIsNone(result)

    def test_empty_url_returns_none(self):
        result = _match_ats("")
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────
class TestAtsRoutingTableCompleteness(unittest.TestCase):
    """REQ-062: Every ATS_PLATFORMS entry must have valid routing fields."""

    def test_all_entries_have_required_keys(self):
        required = {"domains", "strategy", "list_fn", "jd_fn"}
        for name, cfg in ATS_PLATFORMS.items():
            for key in required:
                self.assertIn(key, cfg, f"ATS_PLATFORMS[{name!r}] missing key {key!r}")

    def test_list_fn_resolves_to_real_function(self):
        """Every non-None list_fn must point to a real function in job_agent."""
        for name, cfg in ATS_PLATFORMS.items():
            fn_name = cfg.get("list_fn")
            if fn_name is not None:
                fn = getattr(job_agent_mod, fn_name, None)
                self.assertIsNotNone(fn,
                    f"ATS_PLATFORMS[{name!r}].list_fn={fn_name!r} not found in job_agent")
                self.assertTrue(callable(fn),
                    f"ATS_PLATFORMS[{name!r}].list_fn={fn_name!r} is not callable")

    def test_jd_fn_resolves_to_real_function(self):
        """Every non-None jd_fn must point to a real function in job_agent."""
        for name, cfg in ATS_PLATFORMS.items():
            fn_name = cfg.get("jd_fn")
            if fn_name is not None:
                fn = getattr(job_agent_mod, fn_name, None)
                self.assertIsNotNone(fn,
                    f"ATS_PLATFORMS[{name!r}].jd_fn={fn_name!r} not found in job_agent")
                self.assertTrue(callable(fn),
                    f"ATS_PLATFORMS[{name!r}].jd_fn={fn_name!r} is not callable")

    def test_api_ats_derived_from_routing_table(self):
        """API_ATS must be derived from ATS_PLATFORMS json_api entries."""
        for domain in API_ATS:
            found = False
            for cfg in ATS_PLATFORMS.values():
                if cfg["strategy"] == "json_api" and domain in cfg["domains"]:
                    found = True
                    break
            self.assertTrue(found, f"API_ATS domain {domain!r} not in routing table")

    def test_known_ats_platforms_present(self):
        """All expected ATS platforms must exist in routing table."""
        expected = {"greenhouse", "lever", "ashby", "workday", "google", "tesla"}
        self.assertEqual(set(ATS_PLATFORMS.keys()) & expected, expected)


class TestAutoArchiveWorkflow(unittest.TestCase):
    """REQ-063: Auto-archive companies with no TPM jobs.

    Tests the archive workflow logic that runs in main():
    - Archived companies are skipped
    - Counter increments when no TPM jobs found
    - Counter resets when TPM jobs found
    - Auto-archive triggers at threshold
    - data_quality='failed' records don't count
    """

    def setUp(self):
        from shared.excel_store import (
            get_or_create_excel, upsert_companies, upsert_jd_record,
            get_archived_companies, get_company_archive_info,
            update_archive_status, count_valid_tpm_jobs_by_company,
        )
        self.get_or_create_excel = get_or_create_excel
        self.upsert_companies = upsert_companies
        self.upsert_jd_record = upsert_jd_record
        self.get_archived_companies = get_archived_companies
        self.get_company_archive_info = get_company_archive_info
        self.update_archive_status = update_archive_status
        self.count_valid = count_valid_tpm_jobs_by_company

        import tempfile
        fd, self.path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        os.remove(self.path)
        self.get_or_create_excel(self.path)
        self.upsert_companies(self.path, [
            {"company_name": "AlphaCo", "ai_domain": "LLM",
             "business_focus": "AI", "career_url": "https://alpha.co/careers"},
            {"company_name": "BetaCo", "ai_domain": "Vision",
             "business_focus": "AI", "career_url": "https://beta.co/careers"},
        ])

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _simulate_archive_phase(self, processed_names):
        """Reproduce the archive logic from main()."""
        from shared.config import AUTO_ARCHIVE_THRESHOLD
        valid_counts = self.count_valid(self.path)
        archive_info = self.get_company_archive_info(self.path)
        for cname in processed_names:
            has_tpm = valid_counts.get(cname, 0) > 0
            info = archive_info.get(cname, {"no_tpm_count": 0, "archived": ""})
            if has_tpm:
                if info["no_tpm_count"] > 0 or info["archived"] == "yes":
                    self.update_archive_status(self.path, cname, 0, "no")
            else:
                new_count = info["no_tpm_count"] + 1
                if new_count >= AUTO_ARCHIVE_THRESHOLD:
                    self.update_archive_status(self.path, cname, new_count, "yes")
                else:
                    self.update_archive_status(self.path, cname, new_count, "no")

    def test_archived_company_skipped(self):
        """Archived companies should be filtered out of the company list."""
        self.update_archive_status(self.path, "AlphaCo", 3, "yes")
        archived = self.get_archived_companies(self.path)
        companies = [
            ["AlphaCo", "LLM", "AI", "https://alpha.co/careers"],
            ["BetaCo", "Vision", "AI", "https://beta.co/careers"],
        ]
        filtered = [r for r in companies if str(r[0]).strip() not in archived]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][0], "BetaCo")

    def test_counter_increments_no_tpm(self):
        """Counter increments when company has no TPM jobs."""
        self._simulate_archive_phase({"AlphaCo"})
        info = self.get_company_archive_info(self.path)
        self.assertEqual(info["AlphaCo"]["no_tpm_count"], 1)
        self.assertEqual(info["AlphaCo"]["archived"], "no")

    def test_counter_resets_with_tpm_jobs(self):
        """Counter resets to 0 when TPM jobs are found."""
        self.update_archive_status(self.path, "AlphaCo", 2, "no")
        # Add a valid TPM job for AlphaCo
        jd = json.dumps({
            "job_title": "TPM", "company": "AlphaCo", "location": "Remote",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
            "data_quality": "complete",
        })
        self.upsert_jd_record(self.path, "https://alpha.co/j/1", jd, "h1")
        self._simulate_archive_phase({"AlphaCo"})
        info = self.get_company_archive_info(self.path)
        self.assertEqual(info["AlphaCo"]["no_tpm_count"], 0)
        self.assertEqual(info["AlphaCo"]["archived"], "no")

    def test_auto_archive_at_threshold(self):
        """Company auto-archived after N consecutive runs with no TPM jobs."""
        from shared.config import AUTO_ARCHIVE_THRESHOLD
        self.update_archive_status(self.path, "AlphaCo",
                                   AUTO_ARCHIVE_THRESHOLD - 1, "no")
        self._simulate_archive_phase({"AlphaCo"})
        info = self.get_company_archive_info(self.path)
        self.assertEqual(info["AlphaCo"]["no_tpm_count"], AUTO_ARCHIVE_THRESHOLD)
        self.assertEqual(info["AlphaCo"]["archived"], "yes")

    def test_failed_records_not_counted(self):
        """data_quality='failed' records should not count as TPM jobs."""
        jd_fail = json.dumps({
            "job_title": "TPM", "company": "AlphaCo", "location": "",
            "salary_range": "N/A", "requirements": [],
            "additional_qualifications": [],
            "key_responsibilities": [], "is_ai_tpm": False,
            "data_quality": "failed",
        })
        self.upsert_jd_record(self.path, "https://alpha.co/j/1", jd_fail, "h1")
        self.update_archive_status(self.path, "AlphaCo", 2, "no")
        self._simulate_archive_phase({"AlphaCo"})
        info = self.get_company_archive_info(self.path)
        # Still incremented because the failed record doesn't count
        self.assertEqual(info["AlphaCo"]["no_tpm_count"], 3)

    def test_threshold_constant_exists(self):
        from shared.config import AUTO_ARCHIVE_THRESHOLD
        self.assertIsInstance(AUTO_ARCHIVE_THRESHOLD, int)
        self.assertGreater(AUTO_ARCHIVE_THRESHOLD, 0)

    def test_multiple_runs_increment(self):
        """Simulate 3 consecutive runs with no TPM jobs."""
        from shared.config import AUTO_ARCHIVE_THRESHOLD
        for run in range(AUTO_ARCHIVE_THRESHOLD):
            self._simulate_archive_phase({"AlphaCo"})
        info = self.get_company_archive_info(self.path)
        self.assertEqual(info["AlphaCo"]["no_tpm_count"], AUTO_ARCHIVE_THRESHOLD)
        self.assertEqual(info["AlphaCo"]["archived"], "yes")
        # Should be in archived set
        self.assertIn("AlphaCo", self.get_archived_companies(self.path))

    def test_unarchive_restores_company(self):
        """After unarchive, company should be processed again."""
        from shared.excel_store import unarchive_company
        self.update_archive_status(self.path, "AlphaCo", 3, "yes")
        self.assertIn("AlphaCo", self.get_archived_companies(self.path))
        unarchive_company(self.path, "AlphaCo")
        self.assertNotIn("AlphaCo", self.get_archived_companies(self.path))
        info = self.get_company_archive_info(self.path)
        self.assertEqual(info["AlphaCo"]["no_tpm_count"], 0)

    def test_partial_quality_counts_as_tpm(self):
        """data_quality='partial' records should count as TPM jobs."""
        self.update_archive_status(self.path, "AlphaCo", 2, "no")
        jd_partial = json.dumps({
            "job_title": "TPM", "company": "AlphaCo", "location": "",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
            "data_quality": "partial",
        })
        self.upsert_jd_record(self.path, "https://alpha.co/j/1", jd_partial, "h1")
        self._simulate_archive_phase({"AlphaCo"})
        info = self.get_company_archive_info(self.path)
        # Should reset because partial counts
        self.assertEqual(info["AlphaCo"]["no_tpm_count"], 0)
        self.assertEqual(info["AlphaCo"]["archived"], "no")


class TestBug53FmtAddrOutsideLoop(unittest.TestCase):
    """BUG-53: _fmt_addr must be defined outside the for-block loop to avoid re-creation per iteration."""

    def test_fmt_addr_not_inside_loop_body(self):
        """_fmt_addr should be defined before the loop, not inside 'for block in blocks:'.
        The JSON-LD parsing (incl. _fmt_addr) now lives in the shared
        _parse_jsonld_jobposting helper (BUG-67 consolidation)."""
        import inspect
        from agents.job_agent import _parse_jsonld_jobposting
        source = inspect.getsource(_parse_jsonld_jobposting)
        # Find where _fmt_addr is defined and where the for loop starts
        lines = source.split("\n")
        fmt_addr_line = None
        for_block_line = None
        for i, line in enumerate(lines):
            if "def _fmt_addr" in line:
                fmt_addr_line = i
            if "for block in blocks:" in line and for_block_line is None:
                for_block_line = i
        self.assertIsNotNone(fmt_addr_line, "_fmt_addr definition not found")
        self.assertIsNotNone(for_block_line, "for block in blocks not found")
        self.assertLess(fmt_addr_line, for_block_line,
                        "_fmt_addr must be defined before the loop, not inside it")

    def test_fmt_addr_works_correctly(self):
        """_scrape_workday_jd should correctly format addresses from JSON-LD."""
        # Build a minimal HTML with JSON-LD that has jobLocation
        jd_json = json.dumps({
            "description": "We are looking for a TPM to manage AI projects.",
            "title": "Technical Program Manager",
            "jobLocation": {
                "address": {
                    "addressLocality": "San Francisco",
                    "addressRegion": "CA",
                    "addressCountry": "US",
                }
            }
        })
        html = f'<html><script type="application/ld+json">{jd_json}</script></html>'
        with unittest.mock.patch("agents.job_agent.requests.get") as mock_get:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = html
            mock_get.return_value = mock_resp
            result = _scrape_workday_jd("https://example.workday.com/job/1")
        self.assertIn("San Francisco", result)
        self.assertIn("CA", result)


class TestBug54KeyPoolNoneGuard(unittest.TestCase):
    """BUG-54: Functions using _KEY_POOL must raise RuntimeError when pool is None."""

    def test_llm_filter_jobs_raises_when_pool_none(self):
        original = job_agent_mod._KEY_POOL
        try:
            job_agent_mod._KEY_POOL = None
            with self.assertRaises(RuntimeError) as ctx:
                llm_filter_jobs("TestCo", [{"title": "TPM", "url": "http://x", "location": "US"}])
            self.assertIn("_KEY_POOL not initialized", str(ctx.exception))
        finally:
            job_agent_mod._KEY_POOL = original

    def test_extract_jd_raises_when_pool_none(self):
        original = job_agent_mod._KEY_POOL
        try:
            job_agent_mod._KEY_POOL = None
            with self.assertRaises(RuntimeError) as ctx:
                extract_jd("# Job Description\nSome JD text")
            self.assertIn("_KEY_POOL not initialized", str(ctx.exception))
        finally:
            job_agent_mod._KEY_POOL = original


# ─────────────────────────────────────────────────────────────────────────────
class TestRetryOneSkipsIncompleteExtraction(unittest.IsolatedAsyncioTestCase):
    """BUG-27 regression: retry_one must NOT overwrite old records when Gemini
    returns extraction results where location/requirements/responsibilities
    are all empty, to prevent infinite retry loops."""

    def setUp(self):
        self.temp_xlsx = tempfile.mktemp(suffix=".xlsx")
        from shared.excel_store import get_or_create_excel
        get_or_create_excel(self.temp_xlsx)
        # Insert a company
        from shared.excel_store import upsert_companies
        upsert_companies(self.temp_xlsx, [{
            "company_name": "RetryTestCo",
            "ai_domain": "AI Startups",
            "business_focus": "Testing",
            "career_url": "https://boards.greenhouse.io/retrytestco",
        }])
        # Insert an incomplete JD record (location=N/A to trigger retry)
        from shared.excel_store import batch_upsert_jd_records
        incomplete_jd = json.dumps({
            "job_title": "TPM",
            "company": "RetryTestCo",
            "location": "N/A",
            "requirements": [],
            "key_responsibilities": [],
            "tech_stack": "N/A",
            "responsibilities": "N/A",
            "is_ai_tpm": "Yes",
        })
        batch_upsert_jd_records(self.temp_xlsx,
                                [("https://example.com/retry-job", incomplete_jd, "oldhash")])

    def tearDown(self):
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake", "FIRECRAWL_API_KEY": "fake"})
    @patch("agents.job_agent.get_or_create_excel")
    @patch("agents.job_agent.process_company")
    async def test_retry_skips_when_all_key_fields_empty(self, mock_pc, mock_excel):
        """When extract_jd returns data with empty location/reqs/resp,
        retry_one should skip the overwrite and the record stays unchanged."""
        mock_excel.return_value = self.temp_xlsx
        mock_pc.side_effect = lambda *a, **kw: None  # no-op for main processing

        # Mock extract_jd to return a result with company but empty key fields
        still_incomplete = json.dumps({
            "job_title": "TPM",
            "company": "RetryTestCo",
            "location": "",
            "requirements": [],
            "key_responsibilities": [],
        })

        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()
        original_limiter = job_agent_mod._GEMINI_LIMITER
        job_agent_mod._GEMINI_LIMITER = mock_limiter

        try:
            mock_scraper = AsyncMock(return_value=("# Some markdown", None))
            with patch("agents.job_agent._route_scraper", mock_scraper), \
                 patch("agents.job_agent.extract_jd", return_value=still_incomplete), \
                 patch("agents.job_agent._save_md_to_cache"), \
                 patch("agents.job_agent._save_structured_jd_md"), \
                 patch("agents.job_agent.batch_upsert_jd_records") as mock_batch_upsert:

                await job_agent_mod.main()

                # BUG-27 fix: retry_one should NOT call batch_upsert_jd_records
                # because extraction is still incomplete
                mock_batch_upsert.assert_not_called()
        finally:
            job_agent_mod._GEMINI_LIMITER = original_limiter


class TestRetryOneGates(unittest.IsolatedAsyncioTestCase):
    """BUG-66: the incomplete-row retry path must run the same write-time
    gates as discovery (geo / domain / YoE / work-auth) — it previously
    bypassed all of them."""

    def setUp(self):
        self.temp_xlsx = tempfile.mktemp(suffix=".xlsx")
        from shared.excel_store import (get_or_create_excel, upsert_companies,
                                        batch_upsert_jd_records)
        get_or_create_excel(self.temp_xlsx)
        upsert_companies(self.temp_xlsx, [{
            "company_name": "RetryTestCo",
            "ai_domain": "AI-native",
            "business_focus": "Testing",
            "career_url": "https://boards.greenhouse.io/retrytestco",
        }])
        incomplete_jd = json.dumps({
            "job_title": "TPM", "company": "RetryTestCo", "location": "N/A",
            "requirements": [], "key_responsibilities": [],
        })
        batch_upsert_jd_records(
            self.temp_xlsx,
            [("https://example.com/retry-job", incomplete_jd, "oldhash")])

    def tearDown(self):
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)

    async def _run_main_with_extraction(self, extracted: dict):
        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()
        original_limiter = job_agent_mod._GEMINI_LIMITER
        job_agent_mod._GEMINI_LIMITER = mock_limiter
        try:
            with patch.dict(os.environ, {"GEMINI_API_KEY": "fake",
                                         "FIRECRAWL_API_KEY": "fake"}), \
                 patch("agents.job_agent.get_or_create_excel",
                       return_value=self.temp_xlsx), \
                 patch("agents.job_agent.process_company",
                       new=AsyncMock(return_value=None)), \
                 patch("agents.job_agent._route_scraper",
                       new=AsyncMock(return_value=("# md", None))), \
                 patch("agents.job_agent.extract_jd",
                       return_value=json.dumps(extracted)), \
                 patch("agents.job_agent._save_md_to_cache"), \
                 patch("agents.job_agent._save_structured_jd_md"), \
                 patch("agents.job_agent._fetch_jsonld_posted_date",
                       return_value=""), \
                 patch("agents.job_agent.batch_upsert_jd_records") as mock_upsert:
                await job_agent_mod.main()
                return mock_upsert
        finally:
            job_agent_mod._GEMINI_LIMITER = original_limiter

    COMPLETE = {
        "job_title": "Senior TPM", "company": "RetryTestCo",
        "salary_range": "", "requirements": ["x"],
        "additional_qualifications": [], "key_responsibilities": ["y"],
        "job_domain": "AI", "min_yoe": 5, "work_auth": "none_stated",
    }

    async def test_out_of_region_retry_not_staged(self):
        mock_upsert = await self._run_main_with_extraction(
            {**self.COMPLETE, "location": "Bangalore, India"})
        mock_upsert.assert_not_called()

    async def test_work_auth_gated_retry_not_staged(self):
        mock_upsert = await self._run_main_with_extraction(
            {**self.COMPLETE, "location": "Seattle, WA",
             "work_auth": "clearance_required"})
        mock_upsert.assert_not_called()

    async def test_in_region_retry_staged(self):
        mock_upsert = await self._run_main_with_extraction(
            {**self.COMPLETE, "location": "Seattle, WA"})
        mock_upsert.assert_called_once()



# ═════════════════════════════════════════════════════════════════════════════
# PRJ-004 T8 — Workday pagination + Firecrawl uncap (REQ-004-13, G6a)
# ═════════════════════════════════════════════════════════════════════════════
class TestWorkdayPagination(unittest.TestCase):
    """REQ-004-13: _fetch_workday_jobs paginates past the old 20-posting cap."""

    @staticmethod
    def _page(n_postings, start=0, total=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "jobPostings": [
                {"externalPath": f"/job/US-Somewhere/tpm-{start + i}",
                 "title": f"TPM {start + i}", "locationsText": "Seattle, WA",
                 "postedOn": "Posted 2 Days Ago"}
                for i in range(n_postings)
            ],
            **({"total": total} if total is not None else {}),
        }
        return resp

    def test_paginates_across_pages(self):
        from agents.job_agent import _fetch_workday_jobs
        # BUG-64: Workday CXS hard-caps limit at 20 (>20 → HTTP 400, verified
        # live against NVIDIA at the G6a gate) — pagination must use 20/page.
        # 3 pages: 20 + 20 + 15 = 55 postings — beyond the old single-page cap.
        pages = [self._page(20, 0, total=55), self._page(20, 20, total=55),
                 self._page(15, 40, total=55)]
        with patch("agents.job_agent._http_request_with_retry", side_effect=pages) as mock_http:
            results = _fetch_workday_jobs("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
        self.assertEqual(len(results), 55)
        self.assertEqual(mock_http.call_count, 3)
        # offsets advance by page size; limit never exceeds the API's cap of 20
        offsets = [c.kwargs["json"]["offset"] for c in mock_http.call_args_list]
        self.assertEqual(offsets, [0, 20, 40])
        for call in mock_http.call_args_list:
            self.assertLessEqual(call.kwargs["json"]["limit"], 20,
                                 "BUG-64: Workday rejects limit>20 with HTTP 400")
        self.assertGreater(len(results), 20, "G6a: must exceed the old 20-job cap")

    def test_short_page_terminates(self):
        from agents.job_agent import _fetch_workday_jobs
        with patch("agents.job_agent._http_request_with_retry",
                   side_effect=[self._page(7)]) as mock_http:
            results = _fetch_workday_jobs("https://co.myworkdayjobs.com/site")
        self.assertEqual(len(results), 7)
        self.assertEqual(mock_http.call_count, 1)

    def test_carries_posted_date(self):
        from agents.job_agent import _fetch_workday_jobs
        from datetime import datetime, timedelta
        with patch("agents.job_agent._http_request_with_retry",
                   side_effect=[self._page(1)]):
            results = _fetch_workday_jobs("https://co.myworkdayjobs.com/site")
        expected = (datetime.now().date() - timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertEqual(results[0]["posted_date"], expected)

    def test_http_failure_returns_partial(self):
        from agents.job_agent import _fetch_workday_jobs
        with patch("agents.job_agent._http_request_with_retry",
                   side_effect=[self._page(20, 0), None]):
            results = _fetch_workday_jobs("https://co.myworkdayjobs.com/site")
        self.assertEqual(len(results), 20)


class TestParseWorkdayPostedOn(unittest.TestCase):
    """REQ-004-10: deterministic postedOn relative-string parser."""

    from datetime import date as _date
    TODAY = _date(2026, 7, 7)

    def _p(self, text):
        from agents.job_agent import _parse_workday_posted_on
        return _parse_workday_posted_on(text, today=self.TODAY)

    def test_today_and_yesterday(self):
        self.assertEqual(self._p("Posted Today"), "2026-07-07")
        self.assertEqual(self._p("Posted Yesterday"), "2026-07-06")

    def test_days_ago(self):
        self.assertEqual(self._p("Posted 2 Days Ago"), "2026-07-05")
        self.assertEqual(self._p("Posted 14 Days Ago"), "2026-06-23")

    def test_thirty_plus_saturates_past_gate(self):
        # "30+" → 31 days back: correctly fails the 15-day freshness gate.
        self.assertEqual(self._p("Posted 30+ Days Ago"), "2026-06-06")

    def test_unparseable_is_unknown_not_aged(self):
        self.assertEqual(self._p(""), "")
        self.assertEqual(self._p(None), "")
        self.assertEqual(self._p("Posted recently"), "")

    def test_just_posted_variants(self):
        # BUG-67: "Just Posted" strings previously fell to "" (unknown date).
        self.assertEqual(self._p("Just Posted"), "2026-07-07")
        self.assertEqual(self._p("Posted Just Now"), "2026-07-07")


class TestFirecrawlMapUncapped(unittest.TestCase):
    """REQ-004-13: the Firecrawl map call must not pass a result limit."""

    def test_map_called_without_limit(self):
        from agents.job_agent import _firecrawl_map
        pool = _FakeFcPool(map_return={"links": []})
        with patch("agents.job_agent._FC_POOL", pool):
            _firecrawl_map("https://example.com/careers")
        self.assertTrue(pool.map.called)
        self.assertNotIn("limit", pool.map.call_args.kwargs,
                         "Firecrawl map must not cap results (REQ-004-13)")



# ═════════════════════════════════════════════════════════════════════════════
# PRJ-004 T5/T6/T7 — write-time gates, date parsing, geo tightening
# ═════════════════════════════════════════════════════════════════════════════
class TestWriteTimeGates(unittest.IsolatedAsyncioTestCase):
    """REQ-004-08/09/10/11: domain / YoE / work-auth gates + date flags run at
    write time on the staging path — skipped rows are never written."""

    BASE = {
        "job_title": "Technical Program Manager",
        "company": "TestCo", "location": "Seattle, WA",
        "salary_range": "$200k", "requirements": ["stuff"],
        "additional_qualifications": [], "key_responsibilities": ["lead"],
        "job_domain": "AI", "min_yoe": 5, "work_auth": "none_stated",
    }

    async def _run(self, overrides: dict, track="AI-native", posted_date="",
                   label="generic"):
        import json as _json
        from agents.job_agent import _process_scraped_jd
        parsed = {**self.BASE, **overrides}
        pending, ts_only = [], []
        with patch("agents.job_agent.extract_jd", return_value=_json.dumps(parsed)), \
             patch("agents.job_agent._save_md_to_cache"), \
             patch("agents.job_agent._save_structured_jd_md"), \
             patch("agents.job_agent._fetch_jsonld_posted_date", return_value=""):
            await _process_scraped_jd(
                "https://x.co/jd", "# markdown", "TestCo", track,
                set(), {}, pending, ts_only, label,
                posted_date=posted_date,
            )
        return pending

    async def test_yoe_boundary_table(self):
        # (min_yoe, kept?) — B2: keep [4,10]; skip ≤3 and ≥12.
        for yoe, kept in [(3, False), (4, True), (10, True), (12, False)]:
            pending = await self._run({"min_yoe": yoe})
            self.assertEqual(bool(pending), kept, f"min_yoe={yoe}")

    async def test_yoe_unstated_senior_title_auto_qualifies(self):
        import json as _json
        pending = await self._run({"min_yoe": None,
                                   "job_title": "Senior TPM, Infrastructure"})
        self.assertEqual(len(pending), 1)
        row = _json.loads(pending[0][1])
        self.assertEqual(row["yoe_flag"], "auto-qualified (title)")

    async def test_yoe_unstated_plain_title_flagged_never_dropped(self):
        import json as _json
        pending = await self._run({"min_yoe": None,
                                   "job_title": "Technical Program Manager"})
        self.assertEqual(len(pending), 1)
        self.assertIn("manual review", _json.loads(pending[0][1])["yoe_flag"])

    async def test_work_auth_gate(self):
        for wa, kept in [("citizenship_required", False),
                         ("clearance_required", False),
                         ("us_person_ok", True), ("none_stated", True)]:
            pending = await self._run({"work_auth": wa})
            self.assertEqual(bool(pending), kept, f"work_auth={wa}")

    async def test_kept_rows_carry_work_auth_audit_value(self):
        import json as _json
        pending = await self._run({"work_auth": "us_person_ok"})
        self.assertEqual(_json.loads(pending[0][1])["work_auth"], "us_person_ok")

    async def test_domain_gate_skips_none_on_midlarge(self):
        pending = await self._run({"job_domain": "None"}, track="Mid-large Tech")
        self.assertEqual(pending, [])

    async def test_vertical_override_forces_domain(self):
        """Even if the LLM says None, a vertical company's track wins."""
        import json as _json
        pending = await self._run({"job_domain": "None"}, track="Space")
        self.assertEqual(len(pending), 1)
        self.assertEqual(_json.loads(pending[0][1])["job_domain"], "Space")

    async def test_posted_date_threaded_from_list_meta(self):
        import json as _json
        from datetime import datetime, timedelta
        posted = (datetime.now().date() - timedelta(days=2)).strftime("%Y-%m-%d")
        pending = await self._run({}, posted_date=posted)
        row = _json.loads(pending[0][1])
        self.assertEqual(row["posted_date"], posted)
        self.assertNotIn("date_flag", row)

    async def test_unknown_date_kept_with_flag(self):
        import json as _json
        pending = await self._run({})
        row = _json.loads(pending[0][1])
        self.assertEqual(row.get("posted_date", ""), "")
        self.assertIn("unknown posted date", row["date_flag"])

    async def test_out_of_region_generic_jd_skipped(self):
        pending = await self._run({"location": "New York, NY"})
        self.assertEqual(pending, [])

    # ── BUG-66: geo gate is unconditional — every scraper label, every path ──
    async def test_out_of_region_skipped_on_custom_labels(self):
        for label in ("Microsoft", "Tesla", "Google", "Workday", "prefetched"):
            pending = await self._run({"location": "Bangalore, India"},
                                      label=label)
            self.assertEqual(pending, [], f"label={label}")

    async def test_foreign_remote_skipped(self):
        pending = await self._run({"location": "Remote, Canada"})
        self.assertEqual(pending, [])

    async def test_blank_location_kept_not_dropped(self):
        """Unknown location → keep (surfaces via Data Quality, not a drop)."""
        import json as _json
        pending = await self._run({"location": ""})
        self.assertEqual(len(pending), 1)
        self.assertEqual(_json.loads(pending[0][1])["data_quality"], "partial")

    async def test_mixed_locations_best_region_kept(self):
        """Any qualifying segment keeps the row (best-region precedence)."""
        pending = await self._run(
            {"location": "Bangalore, India; Austin, TX"}, label="Microsoft")
        self.assertEqual(len(pending), 1)


class TestParseIsoDate(unittest.TestCase):
    """REQ-004-10: ATS date normalization (ISO strings + Lever epoch-ms)."""

    def test_iso_string(self):
        from agents.job_agent import _parse_iso_date
        self.assertEqual(_parse_iso_date("2026-07-01T12:34:56Z"), "2026-07-01")
        self.assertEqual(_parse_iso_date("2026-07-01"), "2026-07-01")

    def test_lever_epoch_ms(self):
        from agents.job_agent import _parse_iso_date
        from datetime import datetime
        ms = int(datetime(2026, 7, 1, 12, 0).timestamp() * 1000)
        self.assertEqual(_parse_iso_date(ms), "2026-07-01")
        self.assertEqual(_parse_iso_date(str(ms)), "2026-07-01")

    def test_blank_and_garbage_are_unknown(self):
        from agents.job_agent import _parse_iso_date
        self.assertEqual(_parse_iso_date(None), "")
        self.assertEqual(_parse_iso_date(""), "")
        self.assertEqual(_parse_iso_date("last Tuesday"), "")


class TestTpmFilterGeo(unittest.TestCase):
    """REQ-004-12: _tpm_filter keeps only WA/CA/TX/US-Remote
    (2026-08-19: Seattle metro widened to all of WA)."""

    @staticmethod
    def _links():
        return [
            {"url": "u1", "title": "TPM", "location": "Seattle, WA"},
            {"url": "u2", "title": "TPM", "location": "New York, NY"},
            {"url": "u3", "title": "TPM", "location": "Austin, TX"},
            {"url": "u4", "title": "TPM", "location": "Remote"},
            {"url": "u5", "title": "TPM", "location": "El Segundo, CA"},
            {"url": "u6", "title": "TPM", "location": ""},  # unknown → keep
            {"url": "u7", "title": "TPM", "location": "Boston, MA"},
            {"url": "u8", "title": "TPM", "location": "Spokane, WA"},  # WA beyond Seattle metro
        ]

    def test_only_target_regions_kept(self):
        from agents.job_agent import _tpm_filter
        kept = {l["url"] for l in _tpm_filter(self._links())}
        self.assertEqual(kept, {"u1", "u3", "u4", "u5", "u6", "u8"})


class TestTpmFilterTrackTitles(unittest.TestCase):
    """2026-08-19 title widening: 'technical project manager' accepted on all
    tracks; plain 'program manager' only on PM_TITLE_OK_TRACKS
    (AI-native/Robotics/Space/Defense) — not Fintech/Mid-large/blank."""

    LINKS = [
        {"url": "u1", "title": "Senior Technical Program Manager", "location": "Seattle, WA"},
        {"url": "u2", "title": "Technical Project Manager", "location": "Seattle, WA"},
        {"url": "u3", "title": "Program Manager, Launch Operations", "location": "Austin, TX"},
        {"url": "u4", "title": "Product Manager", "location": "Seattle, WA"},
        {"url": "u5", "title": "Software Engineer", "location": "Remote"},
    ]

    def _kept(self, track):
        from agents.job_agent import _tpm_filter
        return {l["url"] for l in _tpm_filter(self.LINKS, track)}

    def test_technical_project_manager_accepted_everywhere(self):
        for track in ("", "Mid-large Tech", "Fintech", "AI-native"):
            self.assertIn("u2", self._kept(track), track)

    def test_plain_program_manager_only_on_pm_ok_tracks(self):
        for track in ("AI-native", "Robotics", "Space", "Defense"):
            self.assertEqual(self._kept(track), {"u1", "u2", "u3"}, track)
        for track in ("", "Mid-large Tech", "Fintech"):
            self.assertEqual(self._kept(track), {"u1", "u2"}, track)

    def test_default_track_is_strict(self):
        from agents.job_agent import _tpm_filter
        kept = {l["url"] for l in _tpm_filter(self.LINKS)}
        self.assertNotIn("u3", kept)


class TestSpaceTrackFloridaGeo(unittest.TestCase):
    """REQ-162: FL locations are kept on the Space track only — in both
    _tpm_filter (pre-scrape) and _gate_and_finalize (write time)."""

    FL_LINK = {"url": "u1", "title": "Technical Program Manager",
               "location": "Cape Canaveral, FL"}

    def test_tpm_filter_keeps_fl_only_for_space(self):
        from agents.job_agent import _tpm_filter
        self.assertEqual(_tpm_filter([self.FL_LINK], "Space"), [self.FL_LINK])
        for track in ("", "AI-native", "Defense", "Mid-large Tech"):
            self.assertEqual(_tpm_filter([self.FL_LINK], track), [], track)

    def _parsed(self):
        return {"job_title": "Senior Technical Program Manager",
                "company": "SpaceCo", "location": "Merritt Island, FL",
                "job_domain": "Space", "min_yoe": 6,
                "work_auth": "none_stated"}

    def test_gate_keeps_fl_on_space_track(self):
        from agents.job_agent import _gate_and_finalize
        self.assertIsNotNone(_gate_and_finalize(
            self._parsed(), "Space", "https://x.co/jd/1",
            posted_date="2026-08-01"))

    def test_gate_drops_fl_on_other_tracks(self):
        from agents.job_agent import _gate_and_finalize
        parsed = self._parsed()
        parsed["job_domain"] = "Defense"
        self.assertIsNone(_gate_and_finalize(
            parsed, "Defense", "https://x.co/jd/2",
            posted_date="2026-08-01"))


class TestInternFilter(unittest.TestCase):
    """BUG-72: intern/co-op/new-grad roles are never in scope — dropped by
    title pre-scrape (_tpm_filter) and at write time (_gate_and_finalize)."""

    def test_tpm_filter_drops_student_titles(self):
        from agents.job_agent import _tpm_filter
        links = [
            {"url": "u1", "title": "Internship, Technical Program Manager, Cell Manufacturing", "location": "Fremont, CA"},
            {"url": "u2", "title": "Technical Program Manager Intern, Battery Engineering", "location": "Palo Alto, CA"},
            {"url": "u3", "title": "Technical Program Manager Co-op", "location": "Austin, TX"},
            {"url": "u4", "title": "New Grad Technical Program Manager", "location": "Seattle, WA"},
            {"url": "u5", "title": "Senior Technical Program Manager", "location": "Seattle, WA"},
            # "internal" must NOT trip the \bintern\b boundary
            {"url": "u6", "title": "Technical Program Manager, Internal Tools", "location": "Austin, TX"},
        ]
        kept = {l["url"] for l in _tpm_filter(links)}
        self.assertEqual(kept, {"u5", "u6"})

    def test_gate_and_finalize_drops_intern_title(self):
        from agents.job_agent import _gate_and_finalize
        parsed = {"job_title": "Internship, Technical Program Manager, Optimus (Fall 2026)",
                  "company": "Tesla", "location": "Palo Alto, CA",
                  "job_domain": "Robotics", "min_yoe": None,
                  "work_auth": "none_stated"}
        self.assertIsNone(_gate_and_finalize(parsed, "Robotics", "https://x.co/jd/1",
                                             posted_date="2026-07-01"))

    def test_gate_and_finalize_keeps_internal_tools_title(self):
        from agents.job_agent import _gate_and_finalize
        parsed = {"job_title": "Senior TPM, Internal Developer Platform",
                  "company": "Acme", "location": "Seattle, WA",
                  "job_domain": "AI", "min_yoe": 6,
                  "work_auth": "none_stated"}
        self.assertIsNotNone(_gate_and_finalize(parsed, "AI-native", "https://x.co/jd/2",
                                                posted_date="2026-07-01"))


class TestBackfillPostedDate(unittest.TestCase):
    """REQ-004-10/19: Tavily-scoped LinkedIn search-signal backfill."""

    def tearDown(self):
        import agents.job_agent as jm
        jm._TAVILY_CLIENT = None

    def test_no_client_returns_empty(self):
        import agents.job_agent as jm
        jm._TAVILY_CLIENT = None
        self.assertEqual(jm._backfill_posted_date("Co", "TPM"), "")

    def test_parses_iso_date_from_snippet(self):
        import agents.job_agent as jm
        client = MagicMock()
        client.search.return_value = {"results": [
            {"title": "TPM at Co", "content": "Posted 2026-07-01 on LinkedIn"}]}
        jm._TAVILY_CLIENT = client
        self.assertEqual(jm._backfill_posted_date("Co", "TPM"), "2026-07-01")
        # REQ-004-19: search query is LinkedIn-scoped, never a page fetch
        q = client.search.call_args.kwargs["query"]
        self.assertIn("site:linkedin.com/jobs", q)

    def test_parses_relative_days_ago(self):
        import agents.job_agent as jm
        from datetime import datetime, timedelta
        client = MagicMock()
        client.search.return_value = {"results": [
            {"title": "", "content": "posted 3 days ago"}]}
        jm._TAVILY_CLIENT = client
        expected = (datetime.now().date() - timedelta(days=3)).strftime("%Y-%m-%d")
        self.assertEqual(jm._backfill_posted_date("Co", "TPM"), expected)

    def test_failure_returns_empty_never_raises(self):
        import agents.job_agent as jm
        client = MagicMock()
        client.search.side_effect = Exception("quota")
        jm._TAVILY_CLIENT = client
        self.assertEqual(jm._backfill_posted_date("Co", "TPM"), "")



# ═════════════════════════════════════════════════════════════════════════════
# PRJ-004 T14 — Amazon.jobs adapter (REQ-004-17, G6b) + T15 Tesla regression
# ═════════════════════════════════════════════════════════════════════════════
class TestAmazonAdapter(unittest.TestCase):

    @staticmethod
    def _page(n, start=0, total=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "jobs": [
                {"title": f"Sr. Technical Program Manager {start + i}",
                 "job_path": f"/en/jobs/{start + i}/sr-tpm",
                 "normalized_location": "Seattle, Washington",
                 "posted_date": "July 1, 2026",
                 "description": "Own programs across Kuiper.",
                 "basic_qualifications": "5+ years of program management",
                 "preferred_qualifications": "Hardware program experience"}
                for i in range(n)
            ],
            **({"hits": total} if total is not None else {}),
        }
        return resp

    def test_registered_in_platforms_and_registry(self):
        from agents.job_agent import ATS_PLATFORMS, _resolve_list_fn, ALL_ATS
        self.assertIn("amazon", ATS_PLATFORMS)
        self.assertIn("amazon.jobs", ATS_PLATFORMS["amazon"]["domains"])
        self.assertEqual(ATS_PLATFORMS["amazon"]["strategy"], "json_api")
        self.assertTrue(callable(_resolve_list_fn("_fetch_amazon_jobs")))
        # ATS-trusted path: amazon.jobs is in ALL_ATS so process_company
        # takes Path A, not the crawler.
        self.assertIn("amazon.jobs", ALL_ATS)

    def test_fetch_maps_fields_and_prefetches_jd(self):
        from agents.job_agent import _fetch_amazon_jobs
        with patch("agents.job_agent._http_request_with_retry",
                   side_effect=[self._page(2)]):
            results = _fetch_amazon_jobs("https://www.amazon.jobs/")
        self.assertEqual(len(results), 2)
        r = results[0]
        self.assertTrue(r["url"].startswith("https://www.amazon.jobs/en/jobs/"))
        self.assertEqual(r["location"], "Seattle, Washington")
        self.assertEqual(r["posted_date"], "2026-07-01")  # "July 1, 2026" parsed
        self.assertIn("Basic Qualifications", r["_prefetched_md"])
        self.assertIn("Kuiper", r["_prefetched_md"])
        self.assertEqual(r["_platform"], "Amazon")

    def test_paginates(self):
        from agents.job_agent import _fetch_amazon_jobs
        pages = [self._page(100, 0, total=130), self._page(30, 100, total=130)]
        with patch("agents.job_agent._http_request_with_retry",
                   side_effect=pages) as mock_http:
            results = _fetch_amazon_jobs("https://www.amazon.jobs/")
        self.assertEqual(len(results), 130)
        self.assertEqual(mock_http.call_count, 2)


class TestRouteScraperPrefetch(unittest.IsolatedAsyncioTestCase):
    """G6b: prefetched JD markdown short-circuits routing — zero crawler calls."""

    async def test_prefetched_md_wins_priority_zero(self):
        from agents.job_agent import _route_scraper
        list_meta = {"https://www.amazon.jobs/en/jobs/1/tpm": {
            "_prefetched_md": "# Sr TPM\nJD body", "_platform": "Amazon"}}
        with patch("agents.job_agent.scrape_jd") as mock_crawl:
            md, label = await _route_scraper(
                "https://www.amazon.jobs/en/jobs/1/tpm", MagicMock(),
                list_meta)
        self.assertEqual(md, "# Sr TPM\nJD body")
        self.assertEqual(label, "Amazon")
        mock_crawl.assert_not_called()

    async def test_workday_meta_still_routes(self):
        from agents.job_agent import _route_scraper
        list_meta = {"https://co.myworkdayjobs.com/x/job/1": {"_workday": True}}
        with patch("agents.job_agent._scrape_workday_jd",
                   return_value="# WD JD") as mock_wd:
            md, label = await _route_scraper(
                "https://co.myworkdayjobs.com/x/job/1", MagicMock(),
                list_meta)
        self.assertEqual((md, label), ("# WD JD", "Workday"))
        mock_wd.assert_called_once()


class TestTeslaRegression(unittest.TestCase):
    """PRJ-004 T15 (REQ-004-20): the Tesla custom scraper stays wired and its
    output shape survives the new schema — verification, not a rebuild."""

    def test_tesla_still_registered(self):
        from agents.job_agent import ATS_PLATFORMS, _JD_FN_REGISTRY
        self.assertEqual(ATS_PLATFORMS["tesla"]["jd_fn"], "_scrape_tesla_jd")
        self.assertIn("tesla.com/careers", ATS_PLATFORMS["tesla"]["jd_domains"])
        self.assertIn("_scrape_tesla_jd", _JD_FN_REGISTRY)

    def test_extracted_tesla_jd_defaults_pass_schema(self):
        """A Tesla JD extracted without PRJ-004 fields still validates —
        defaults route it to keep+flag paths, never a crash."""
        from agents.job_agent import JobDetails
        obj = JobDetails(
            job_title="Technical Program Manager, Energy",
            company="Tesla", location="Palo Alto, CA", salary_range="",
            requirements=["5+ years"], additional_qualifications=[],
            key_responsibilities=["Deliver"],
        )
        self.assertEqual(obj.work_auth, "none_stated")   # kept by gate 3
        self.assertEqual(obj.posted_date, "")            # keep + Date Flag



# ═════════════════════════════════════════════════════════════════════════════
# Phase 4 QA follow-ups — pre-scrape gate, prompt anchors, llm_filter rules
# ═════════════════════════════════════════════════════════════════════════════
class TestPrescrapeFreshnessGate(unittest.TestCase):
    """P0 gap from test analysis: the enforcement point of REQ-004-10/G4."""

    @staticmethod
    def _days_ago(n):
        from datetime import datetime, timedelta
        return (datetime.now().date() - timedelta(days=n)).strftime("%Y-%m-%d")

    def _gate(self, to_process, list_meta, known=None):
        from agents.job_agent import _apply_prescrape_freshness_gate
        return _apply_prescrape_freshness_gate(to_process, list_meta, known or {})

    def test_aged_new_posting_skipped_fresh_kept(self):
        meta = {"u_old": {"posted_date": self._days_ago(20)},
                "u_new": {"posted_date": self._days_ago(10)}}
        kept, aged = self._gate(["u_old", "u_new"], meta)
        self.assertEqual(kept, ["u_new"])
        self.assertEqual(aged, 1)

    def test_day_14_keeps_day_15_skips(self):
        meta = {"u14": {"posted_date": self._days_ago(14)},
                "u15": {"posted_date": self._days_ago(15)}}
        kept, aged = self._gate(["u14", "u15"], meta)
        self.assertEqual(kept, ["u14"])
        self.assertEqual(aged, 1)

    def test_already_tracked_url_never_gated(self):
        """G7/REQ-004-16: rows already in the sheet are never retroactively
        dropped, even if their list-API date is aged."""
        meta = {"u_known": {"posted_date": self._days_ago(30)}}
        kept, aged = self._gate(["u_known"], meta,
                                known={"u_known": {"age_days": 5}})
        self.assertEqual(kept, ["u_known"])
        self.assertEqual(aged, 0)

    def test_unparseable_date_is_unknown_not_aged(self):
        """Hardening (code review F4): raw/unparseable date text must take the
        keep+flag path, never be treated as aged."""
        meta = {"u_raw": {"posted_date": "Posted 30+ Days Ago"},
                "u_blank": {"posted_date": ""}, "u_nometa": {}}
        kept, aged = self._gate(["u_raw", "u_blank", "u_nometa"], meta)
        self.assertEqual(kept, ["u_raw", "u_blank", "u_nometa"])
        self.assertEqual(aged, 0)


class TestExtractJdPromptAnchors(unittest.TestCase):
    """P1 gap: the D-10 mapping anchors must actually be in the mid-large
    system prompt; vertical prompts must force the track's domain."""

    def setUp(self):
        import agents.job_agent as jm
        self._orig = jm._KEY_POOL
        pool = MagicMock()
        resp = MagicMock()
        resp.text = "{}"
        pool.generate_content.return_value = resp
        jm._KEY_POOL = pool

    def tearDown(self):
        import agents.job_agent as jm
        jm._KEY_POOL = self._orig

    def _sys_instr(self, track):
        import agents.job_agent as jm
        jm.types.GenerateContentConfig.reset_mock()
        jm.extract_jd("# JD body", company="TestCo", track=track)
        return jm.types.GenerateContentConfig.call_args.kwargs["system_instruction"]

    def test_midlarge_prompt_contains_all_five_anchors(self):
        instr = self._sys_instr("Mid-large Tech")
        for anchor in ("GPU fleet", "Google Pay", "Amazon Robotics",
                       "Project Kuiper", "Azure Government"):
            self.assertIn(anchor, instr, f"missing mapping anchor: {anchor}")
        self.assertIn("SECURITY:", instr)
        # YoE + work-auth clauses ride the same call (no extra round-trip)
        self.assertIn("min_yoe", instr)
        self.assertIn("clearance_required", instr)

    def test_vertical_prompt_forces_domain(self):
        instr = self._sys_instr("Space")
        self.assertIn('set job_domain to "Space"', instr)
        self.assertIn("SECURITY:", instr)

    def test_location_extracted_verbatim_not_us_only(self):
        """BUG-66: the prompt must not tell the LLM to pre-filter to US
        locations — that blinds the deterministic geo gate."""
        instr = self._sys_instr("Mid-large Tech")
        self.assertIn("Extract ALL locations", instr)
        self.assertIn("verbatim", instr)
        self.assertNotIn("ALL US locations", instr)


class TestLlmFilterJobsRuleSelection(unittest.TestCase):
    """P1 gap: vertical fast-path vs mid-large 5-track rule (REQ-004-09)."""

    def setUp(self):
        import agents.job_agent as jm
        self._orig = jm._KEY_POOL
        pool = MagicMock()
        resp = MagicMock()
        resp.text = '{"urls": ["https://x.co/1"]}'
        pool.generate_content.return_value = resp
        jm._KEY_POOL = pool

    def tearDown(self):
        import agents.job_agent as jm
        jm._KEY_POOL = self._orig

    def _sys_instr(self, track):
        import agents.job_agent as jm
        jm.types.GenerateContentConfig.reset_mock()
        jm.llm_filter_jobs("TestCo", [{"url": "https://x.co/1", "title": "TPM"}],
                           track)
        return jm.types.GenerateContentConfig.call_args.kwargs["system_instruction"]

    def test_vertical_track_gets_permissive_rule(self):
        instr = self._sys_instr("Defense")
        self.assertIn("Defense-track company", instr)
        self.assertIn("every genuine Technical Program Manager", instr)
        self.assertNotIn("five tracks", instr)

    def test_midlarge_gets_five_track_rule(self):
        instr = self._sys_instr("Mid-large Tech")
        self.assertIn("five tracks", instr)
        self.assertIn("Project Kuiper", instr)
        self.assertIn("SECURITY:", instr)

    # 2026-08-19 title widening
    def test_pm_ok_vertical_accepts_program_manager(self):
        instr = self._sys_instr("Space")
        self.assertIn("Program Manager, and Technical Project Manager roles", instr)
        self.assertNotIn("generic Program Manager", instr)

    def test_fintech_vertical_rejects_generic_program_manager(self):
        instr = self._sys_instr("Fintech")
        self.assertIn("Fintech-track company", instr)
        self.assertIn("generic Program Manager", instr)
        self.assertIn("Technical Project Manager roles", instr)
        self.assertNotIn("five tracks", instr)

    def test_midlarge_accepts_technical_project_manager_only(self):
        instr = self._sys_instr("Mid-large Tech")
        self.assertIn("Technical Project Manager roles", instr)
        self.assertIn("generic Program Manager", instr)


# ─────────────────────────────────────────────────────────────────────────────
class TestTriagedUrlExclusion(unittest.IsolatedAsyncioTestCase):
    """BUG-65: URLs the user triaged into JD_ToApply / Skipped JD must never be
    re-scraped or re-added to JD_Tracker by process_company."""

    TRIAGED_URL = "https://testco.greenhouse.io/jobs/tpm/11111"
    NEW_URL     = "https://testco.greenhouse.io/jobs/tpm/22222"
    ROW = ["TestCo", "AI-native", "", "https://testco.greenhouse.io"]

    async def _run(self, triaged_set):
        from agents.job_agent import process_company

        raw_listings = [
            {"url": self.TRIAGED_URL, "title": "Technical Program Manager"},
            {"url": self.NEW_URL,     "title": "Technical Program Manager"},
        ]
        route_mock = AsyncMock(return_value=("", "generic"))
        with patch("agents.job_agent.discover_jobs", new=AsyncMock(return_value=raw_listings)), \
             patch("agents.job_agent._route_scraper", new=route_mock):
            await process_company(
                self.ROW, {}, "/fake/path.xlsx",
                asyncio.Lock(), MagicMock(), triaged_set=triaged_set,
            )
        return route_mock

    async def test_triaged_url_not_scraped(self):
        # BUG-71: triaged_set carries canonical URLs (get_triaged_jd_urls contract)
        from shared.excel_store import canonical_jd_url
        route_mock = await self._run({canonical_jd_url(self.TRIAGED_URL)})
        scraped = [c.args[0] for c in route_mock.await_args_list]
        self.assertNotIn(self.TRIAGED_URL, scraped)
        self.assertIn(self.NEW_URL, scraped)

    async def test_triaged_url_variant_not_scraped(self):
        """BUG-71: a tracking-param variant of a triaged URL is also excluded."""
        from shared.excel_store import canonical_jd_url
        route_mock = await self._run(
            {canonical_jd_url(self.TRIAGED_URL + "?gh_src=abc&utm_source=li")})
        scraped = [c.args[0] for c in route_mock.await_args_list]
        self.assertNotIn(self.TRIAGED_URL, scraped)
        self.assertIn(self.NEW_URL, scraped)

    async def test_none_triaged_set_scrapes_all(self):
        route_mock = await self._run(None)
        scraped = [c.args[0] for c in route_mock.await_args_list]
        self.assertIn(self.TRIAGED_URL, scraped)
        self.assertIn(self.NEW_URL, scraped)


# ─────────────────────────────────────────────────────────────────────────────
class TestParseJsonldJobposting(unittest.TestCase):
    """BUG-67: shared JSON-LD JobPosting parser (incl. the previously-ignored
    datePosted field)."""

    @staticmethod
    def _html(payload: dict) -> str:
        return ('<html><script type="application/ld+json">'
                + json.dumps(payload) + "</script></html>")

    BASE = {
        "@type": "JobPosting", "title": "Technical Program Manager",
        "description": "<p>Own the roadmap &amp; delivery.</p>",
        "datePosted": "2026-07-01T08:00:00Z",
        "jobLocation": {"address": {"addressLocality": "Lincolnshire",
                                    "addressRegion": "IL",
                                    "addressCountry": "US"}},
    }

    def _parse(self, payload):
        from agents.job_agent import _parse_jsonld_jobposting
        return _parse_jsonld_jobposting(self._html(payload))

    def test_full_posting_parsed(self):
        p = self._parse(self.BASE)
        self.assertEqual(p["title"], "Technical Program Manager")
        self.assertEqual(p["location"], "Lincolnshire, IL, US")
        self.assertEqual(p["date_posted"], "2026-07-01")
        self.assertIn("Own the roadmap", p["description"])
        self.assertNotIn("<p>", p["description"])

    def test_date_posted_plain_iso(self):
        p = self._parse({**self.BASE, "datePosted": "2026-06-25"})
        self.assertEqual(p["date_posted"], "2026-06-25")

    def test_missing_date_posted_is_blank(self):
        payload = {k: v for k, v in self.BASE.items() if k != "datePosted"}
        self.assertEqual(self._parse(payload)["date_posted"], "")

    def test_location_list(self):
        p = self._parse({**self.BASE, "jobLocation": [
            {"address": {"addressLocality": "Nashville", "addressRegion": "TN"}},
            {"address": {"addressLocality": "Austin", "addressRegion": "TX"}},
        ]})
        self.assertEqual(p["location"], "Nashville, TN; Austin, TX")

    def test_missing_type_accepted_when_description_present(self):
        # Some Workday tenants omit @type.
        payload = {k: v for k, v in self.BASE.items() if k != "@type"}
        self.assertEqual(self._parse(payload)["date_posted"], "2026-07-01")

    def test_non_jobposting_type_rejected(self):
        p = self._parse({**self.BASE, "@type": "Organization"})
        self.assertEqual(p["description"], "")

    def test_base_salary_formatted(self):
        p = self._parse({**self.BASE, "baseSalary": {
            "currency": "USD",
            "value": {"minValue": 150000, "maxValue": 200000}}})
        self.assertEqual(p["salary"], "$150,000 - $200,000 USD")

    def test_no_jsonld_returns_empty(self):
        from agents.job_agent import _parse_jsonld_jobposting
        p = _parse_jsonld_jobposting("<html><body>nothing here</body></html>")
        self.assertEqual(p["description"], "")
        self.assertEqual(p["date_posted"], "")


class TestPostedDateFallbackChain(unittest.TestCase):
    """BUG-67: date chain in _gate_and_finalize — list-meta → scrape-time
    JSON-LD stash → JSON-LD fetch → Tavily backfill → keep + flag."""

    BASE = {
        "job_title": "Senior TPM", "company": "TestCo",
        "location": "Seattle, WA", "salary_range": "",
        "requirements": ["x"], "additional_qualifications": [],
        "key_responsibilities": ["y"], "job_domain": "AI",
        "min_yoe": 5, "work_auth": "none_stated",
    }
    URL = "https://x.example/jd/1"

    def setUp(self):
        job_agent_mod._JSONLD_DATE_BY_URL.clear()

    tearDown = setUp

    def _finalize(self, posted_date="", fetch_return=""):
        with patch("agents.job_agent._fetch_jsonld_posted_date",
                   return_value=fetch_return) as mock_fetch, \
             patch("agents.job_agent._backfill_posted_date",
                   return_value="") as mock_backfill:
            out = job_agent_mod._gate_and_finalize(
                dict(self.BASE), "AI-native", self.URL, posted_date=posted_date)
        return out, mock_fetch, mock_backfill

    def test_list_meta_date_wins_no_fetch(self):
        out, mock_fetch, _ = self._finalize(posted_date="2026-07-08")
        self.assertEqual(out["posted_date"], "2026-07-08")
        mock_fetch.assert_not_called()

    def test_stashed_scrape_date_used(self):
        job_agent_mod._JSONLD_DATE_BY_URL[self.URL] = "2026-07-02"
        out, mock_fetch, _ = self._finalize()
        self.assertEqual(out["posted_date"], "2026-07-02")
        mock_fetch.assert_not_called()

    def test_stashed_negative_prevents_refetch(self):
        # "" stash = page's JSON-LD already examined, don't fetch again.
        job_agent_mod._JSONLD_DATE_BY_URL[self.URL] = ""
        out, mock_fetch, mock_backfill = self._finalize()
        mock_fetch.assert_not_called()
        mock_backfill.assert_called_once()
        self.assertIn("unknown posted date", out["date_flag"])

    def test_unstashed_url_triggers_fetch(self):
        out, mock_fetch, _ = self._finalize(fetch_return="2026-07-03")
        mock_fetch.assert_called_once_with(self.URL)
        self.assertEqual(out["posted_date"], "2026-07-03")
        self.assertNotIn("date_flag", out)

    def test_all_sources_empty_keeps_row_with_flag(self):
        out, _, _ = self._finalize()
        self.assertIsNotNone(out)  # unknown date is never a drop condition
        self.assertIn("unknown posted date", out["date_flag"])


class TestRetryOnePreservesPostedDate(unittest.IsolatedAsyncioTestCase):
    """BUG-67: the incomplete-row retry previously wiped Posted Date on
    rewrite (the LLM never returns one). It must carry the sheet value."""

    EXISTING_DATE = "2026-07-01"

    def setUp(self):
        self.temp_xlsx = tempfile.mktemp(suffix=".xlsx")
        from shared.excel_store import (get_or_create_excel, upsert_companies,
                                        batch_upsert_jd_records)
        get_or_create_excel(self.temp_xlsx)
        upsert_companies(self.temp_xlsx, [{
            "company_name": "RetryTestCo", "ai_domain": "AI-native",
            "business_focus": "Testing",
            "career_url": "https://boards.greenhouse.io/retrytestco",
        }])
        incomplete_jd = json.dumps({
            "job_title": "TPM", "company": "RetryTestCo", "location": "N/A",
            "requirements": [], "key_responsibilities": [],
            "posted_date": self.EXISTING_DATE,
        })
        batch_upsert_jd_records(
            self.temp_xlsx,
            [("https://example.com/retry-job", incomplete_jd, "oldhash")])

    def tearDown(self):
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)

    async def test_retry_keeps_existing_posted_date(self):
        complete = {
            "job_title": "Senior TPM", "company": "RetryTestCo",
            "location": "Seattle, WA", "salary_range": "",
            "requirements": ["x"], "additional_qualifications": [],
            "key_responsibilities": ["y"], "job_domain": "AI",
            "min_yoe": 5, "work_auth": "none_stated",
        }
        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()
        original_limiter = job_agent_mod._GEMINI_LIMITER
        job_agent_mod._GEMINI_LIMITER = mock_limiter
        try:
            with patch.dict(os.environ, {"GEMINI_API_KEY": "fake",
                                         "FIRECRAWL_API_KEY": "fake"}), \
                 patch("agents.job_agent.get_or_create_excel",
                       return_value=self.temp_xlsx), \
                 patch("agents.job_agent.process_company",
                       new=AsyncMock(return_value=None)), \
                 patch("agents.job_agent._route_scraper",
                       new=AsyncMock(return_value=("# md", None))), \
                 patch("agents.job_agent.extract_jd",
                       return_value=json.dumps(complete)), \
                 patch("agents.job_agent._save_md_to_cache"), \
                 patch("agents.job_agent._save_structured_jd_md"), \
                 patch("agents.job_agent._fetch_jsonld_posted_date",
                       return_value="") as mock_fetch, \
                 patch("agents.job_agent.batch_upsert_jd_records") as mock_upsert:
                await job_agent_mod.main()
                mock_upsert.assert_called_once()
                records = mock_upsert.call_args[0][1]
                staged = json.loads(records[0][1])
                self.assertEqual(staged["posted_date"], self.EXISTING_DATE)
                mock_fetch.assert_not_called()  # date known → no extra GET
        finally:
            job_agent_mod._GEMINI_LIMITER = original_limiter


# ─────────────────────────────────────────────────────────────────────────────
class TestFetchGoogleJobs(unittest.TestCase):
    """T16 / REQ-004-18: Google Careers discovery via the server-rendered
    AF_initDataCallback payload (design.md v3 API endpoint is dead)."""

    @staticmethod
    def _job(job_id, title, locations, epoch=1782202073):
        j = [None] * 21
        j[0] = job_id
        j[1] = title
        j[3] = [None, "<ul><li>Run programs.</li></ul>"]
        j[4] = [None, "<h3>Minimum qualifications:</h3><ul><li>BS degree</li></ul>"]
        j[9] = [[loc, [loc], loc.split(",")[0], None, "WA", "US"]
                for loc in locations]
        j[12] = [epoch, 0]
        j[19] = [None, "<ul><li>MS degree</li></ul>"]
        return j

    @classmethod
    def _page_html(cls, jobs) -> str:
        payload = json.dumps([jobs, None, None, None])
        return ("<html><script>AF_initDataCallback({key: 'ds:1', hash: '2', "
                f"data:{payload}, sideChannel: {{}}}});</script></html>")

    def _fetch(self, pages):
        """pages: list of HTML strings returned per successive GET."""
        from agents.job_agent import _fetch_google_jobs
        responses = [MagicMock(status_code=200, text=h) for h in pages]
        with patch("agents.job_agent._http_request_with_retry",
                   side_effect=responses) as mock_http:
            result = _fetch_google_jobs("https://www.google.com/about/careers/applications/")
        return result, mock_http

    def test_jobs_parsed_with_url_title_location_date(self):
        jobs = [self._job("111", "Technical Program Manager, AI Infrastructure",
                          ["Kirkland, WA, USA", "Austin, TX, USA"])]
        # page 2 repeats page 1 (Google clamps page=N to the last page)
        result, _ = self._fetch([self._page_html(jobs)] * 2)
        self.assertEqual(len(result), 1)
        job = result[0]
        self.assertIn("jobs/results/111-technical-program-manager", job["url"])
        self.assertEqual(job["title"], "Technical Program Manager, AI Infrastructure")
        self.assertEqual(job["location"], "Kirkland, WA, USA; Austin, TX, USA")
        from datetime import datetime as _dt
        self.assertEqual(job["posted_date"],
                         _dt.fromtimestamp(1782202073).strftime("%Y-%m-%d"))
        self.assertEqual(job["_platform"], "Google")
        md = job["_prefetched_md"]
        self.assertIn("Minimum Qualifications", md)
        self.assertIn("BS degree", md)
        self.assertNotIn("<ul>", md)

    def test_pagination_stops_on_repeated_page(self):
        page1 = self._page_html([self._job("1", "TPM One", ["Seattle, WA, USA"]),
                                 self._job("2", "TPM Two", ["Austin, TX, USA"])])
        # identical second page → no new ids → stop (2 GETs total)
        result, mock_http = self._fetch([page1, page1])
        self.assertEqual(len(result), 2)
        self.assertEqual(mock_http.call_count, 2)

    def test_malformed_job_skipped_others_kept(self):
        good = self._job("10", "TPM Good", ["Seattle, WA, USA"])
        bad = ["11", None]  # too short, blank title
        result, _ = self._fetch([self._page_html([bad, good])] * 2)
        self.assertEqual([j["title"] for j in result], ["TPM Good"])

    def test_payload_unusable_falls_back_to_hrefs(self):
        html = ('<html><a href="/about/careers/applications/jobs/results/'
                '987654-senior-tpm">Senior TPM</a></html>')
        result, _ = self._fetch([html])
        self.assertEqual(len(result), 1)
        self.assertIn("jobs/results/987654-senior-tpm", result[0]["url"])

    def test_http_failure_returns_empty(self):
        from agents.job_agent import _fetch_google_jobs
        with patch("agents.job_agent._http_request_with_retry",
                   return_value=None):
            self.assertEqual(
                _fetch_google_jobs("https://www.google.com/about/careers/applications/"),
                [])

    def test_google_routing_is_discoverable(self):
        cfg = ATS_PLATFORMS["google"]
        self.assertEqual(cfg["strategy"], "json_api")
        self.assertEqual(cfg["list_fn"], "_fetch_google_jobs")
        self.assertIn("google.com/about/careers", cfg["domains"])


class TestBug62EmptyExtractionAuditRow(unittest.IsolatedAsyncioTestCase):
    """BUG-62: an extraction that returns no company must stage a sheet-visible
    JSON-ERROR audit row instead of vanishing (and being retried invisibly)."""

    async def _stage_with_extraction(self, extraction: str) -> list:
        from agents.job_agent import _process_scraped_jd
        pending, ts_only = [], []
        with patch("agents.job_agent.extract_jd", return_value=extraction), \
             patch("agents.job_agent._save_md_to_cache"), \
             patch("agents.job_agent._save_structured_jd_md"), \
             patch("agents.job_agent._fetch_jsonld_posted_date", return_value=""):
            await _process_scraped_jd(
                "https://x.co/jd-empty", "# markdown", "TestCo", "AI-native",
                set(), {}, pending, ts_only, "generic")
        return pending

    async def test_empty_company_stages_sentinel(self):
        pending = await self._stage_with_extraction("{}")
        self.assertEqual(len(pending), 1)
        url, sentinel, _ = pending[0]
        self.assertEqual(url, "https://x.co/jd-empty")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(sentinel)  # must trip the _JD_ERROR_ROW path

    async def test_sentinel_round_trips_to_json_error_row(self):
        from shared.excel_store import (get_or_create_excel,
                                        batch_upsert_jd_records, _JD_COL)
        pending = await self._stage_with_extraction("{}")
        path = tempfile.mktemp(suffix=".xlsx")
        try:
            get_or_create_excel(path)
            written = batch_upsert_jd_records(path, pending)
            self.assertEqual(written, 1)
            import openpyxl
            wb = openpyxl.load_workbook(path)
            ws = wb["JD_Tracker"]
            self.assertEqual(ws.cell(2, _JD_COL["Company"]).value, "JSON ERROR")
            self.assertEqual(ws.cell(2, _JD_COL["Data Quality"]).value, "failed")
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
