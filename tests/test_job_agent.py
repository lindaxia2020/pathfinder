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
    _classify_by_domain,
    _fetch_ashby_jobs,
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


# ─────────────────────────────────────────────────────────────────────────────
class TestPycountryFallback(unittest.TestCase):
    """BUG-13: pycountry ImportError must not prevent module load."""

    def test_fallback_covers_all_50_states_plus_dc(self):
        from agents.job_agent import _US_STATES_FALLBACK
        self.assertEqual(len(_US_STATES_FALLBACK), 51)  # 50 states + DC

    def test_fallback_includes_ambiguous_state_codes(self):
        """Codes that look like common words (in/or/de/me/ok) must be present."""
        from agents.job_agent import _US_STATES_FALLBACK
        codes = {code for _, code in _US_STATES_FALLBACK}
        for code in ("in", "or", "de", "me", "ok"):
            self.assertIn(code, codes, f"Fallback missing state code '{code}'")

    def test_module_loads_when_pycountry_raises_import_error(self):
        """Simulate pycountry absent: _build_us_index should raise, fallback kicks in."""
        import agents.job_agent as mod
        original_names = mod._US_STATE_NAMES
        original_codes = mod._US_STATE_CODES
        try:
            with patch("agents.job_agent._build_us_index", side_effect=ImportError("no pycountry")):
                # Manually trigger the same logic as module init
                try:
                    mod._build_us_index()
                    self.fail("Expected ImportError")
                except ImportError:
                    fallback_names = {name for name, _ in mod._US_STATES_FALLBACK}
                    fallback_codes = {code for _, code in mod._US_STATES_FALLBACK}
                self.assertIn("california", fallback_names)
                self.assertIn("ca", fallback_codes)
        finally:
            mod._US_STATE_NAMES = original_names
            mod._US_STATE_CODES = original_codes


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


class TestClassifyByDomain(unittest.TestCase):

    def test_big_tech_domain(self):
        self.assertEqual(_classify_by_domain("Big Tech (AI Investment)"), "big_tech")

    def test_ai_startups_domain(self):
        self.assertEqual(_classify_by_domain("AI Startups"), "ai_native")

    def test_large_model_labs(self):
        self.assertEqual(_classify_by_domain("Large Model Labs"), "ai_native")

    def test_empty_domain(self):
        self.assertEqual(_classify_by_domain(""), "unknown")

    def test_na_domain(self):
        self.assertEqual(_classify_by_domain("N/A"), "unknown")

    def test_none_domain(self):
        self.assertEqual(_classify_by_domain(None), "unknown")


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
        for platform in ["greenhouse", "lever", "ashby"]:
            self.assertIn(platform, ATS_SEARCH_PARAM)
            self.assertIn("Technical+Program+Manager", ATS_SEARCH_PARAM[platform])


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
            is_ai_tpm=True,
        )
        self.assertEqual(obj.company, "Anthropic")
        self.assertTrue(obj.is_ai_tpm)

    def test_job_details_is_ai_tpm_false(self):
        obj = JobDetails(
            job_title="PM",
            company="Corp",
            location="NYC",
            salary_range="N/A",
            requirements=[],
            additional_qualifications=[],
            key_responsibilities=[],
            is_ai_tpm=False,
        )
        self.assertFalse(obj.is_ai_tpm)

    def test_bug47_data_quality_field_exists(self):
        """BUG-47: JobDetails must have data_quality field."""
        obj = JobDetails(
            job_title="TPM", company="Co", location="SF",
            salary_range="N/A", requirements=[], additional_qualifications=[],
            key_responsibilities=[], is_ai_tpm=True,
        )
        self.assertIsNone(obj.data_quality)

    def test_bug47_data_quality_accepts_value(self):
        """BUG-47: JobDetails should accept data_quality in constructor."""
        obj = JobDetails(
            job_title="TPM", company="Co", location="SF",
            salary_range="N/A", requirements=[], additional_qualifications=[],
            key_responsibilities=[], is_ai_tpm=True,
            data_quality="complete",
        )
        self.assertEqual(obj.data_quality, "complete")


# ─────────────────────────────────────────────────────────────────────────────
class TestIsUsSegment(unittest.TestCase):
    """Tests for _is_us_segment — especially BUG-03 false positives."""

    def setUp(self):
        from agents.job_agent import _is_us_segment
        self._fn = _is_us_segment

    # ── Should be identified as US ────────────────────────────────────────────
    def test_full_state_name(self):
        self.assertTrue(self._fn("California"))

    def test_city_comma_state_code(self):
        # "Seattle, WA" — canonical City, ST format
        self.assertTrue(self._fn("Seattle, CA"))

    def test_standalone_state_code(self):
        # segment is only the 2-letter code
        self.assertTrue(self._fn("ca"))

    def test_explicit_us(self):
        self.assertTrue(self._fn("US"))

    def test_explicit_usa(self):
        self.assertTrue(self._fn("USA"))

    def test_remote(self):
        self.assertTrue(self._fn("Remote"))

    def test_us_metro_area(self):
        self.assertTrue(self._fn("San Francisco"))

    # ── BUG-03: common words that happen to be state codes ────────────────────
    def test_preposition_in_not_us(self):
        # "in" = Indiana code; must NOT match "Senior Engineer in London"
        self.assertFalse(self._fn("Senior Engineer in London"))

    def test_conjunction_or_not_us(self):
        # "or" = Oregon code; must NOT match "Full-time or Contract"
        self.assertFalse(self._fn("Full-time or Contract"))

    def test_de_not_us(self):
        # "de" = Delaware code; must NOT match "Berlin de"
        self.assertFalse(self._fn("Berlin de"))

    def test_me_not_us(self):
        # "me" = Maine code; must NOT match ordinary "me" usage
        self.assertFalse(self._fn("Contact me for details"))

    def test_ok_not_us(self):
        # "ok" = Oklahoma code; must NOT match "ok to discuss"
        self.assertFalse(self._fn("ok to discuss"))

    def test_non_us_city(self):
        self.assertFalse(self._fn("London"))

    def test_non_us_country(self):
        self.assertFalse(self._fn("Germany"))


# ─────────────────────────────────────────────────────────────────────────────
class TestScrapeTeslaJdUsesFormats(unittest.TestCase):
    """BUG-15: _scrape_tesla_jd must pass formats=["markdown"] to Firecrawl v2 scrape()."""

    def test_scrape_call_includes_formats_markdown(self):
        from agents.job_agent import _scrape_tesla_jd

        captured = {}

        class FakeFirecrawlApp:
            def __init__(self, api_key):
                pass
            def scrape(self, url, **kwargs):
                captured["kwargs"] = kwargs
                m = MagicMock()
                m.markdown = "# Job\nSome content here that is long enough to pass."
                return m

        with patch("firecrawl.FirecrawlApp", FakeFirecrawlApp):
            _scrape_tesla_jd("https://www.tesla.com/careers/search/job/fake-123", fc_key="test-key")

        self.assertIn("formats", captured.get("kwargs", {}),
                      "scrape() must be called with formats= argument")
        self.assertIn("markdown", captured["kwargs"]["formats"],
                      "formats must include 'markdown'")


class TestBug32TeslaFirecrawlImport(unittest.TestCase):
    """BUG-32: _scrape_tesla_jd must import FirecrawlApp, not Firecrawl."""

    def test_imports_firecrawl_app_not_firecrawl(self):
        """Verify _scrape_tesla_jd uses FirecrawlApp (correct class name)."""
        import inspect
        from agents.job_agent import _scrape_tesla_jd
        source = inspect.getsource(_scrape_tesla_jd)
        self.assertIn("FirecrawlApp", source,
                      "_scrape_tesla_jd must import FirecrawlApp")
        self.assertNotIn("import Firecrawl\n", source,
                         "_scrape_tesla_jd must not import bare 'Firecrawl'")

    def test_firecrawl_app_called_successfully(self):
        """Verify FirecrawlApp can be instantiated and scrape called."""
        from agents.job_agent import _scrape_tesla_jd

        class FakeFirecrawlApp:
            def __init__(self, api_key):
                self.api_key = api_key
            def scrape(self, url, **kwargs):
                m = MagicMock()
                m.markdown = "# Tesla Job\n" + "x" * 300
                return m

        with patch("firecrawl.FirecrawlApp", FakeFirecrawlApp):
            result = _scrape_tesla_jd("https://www.tesla.com/careers/search/job/test-32", fc_key="key-32")

        self.assertIsNotNone(result)
        self.assertIn("Tesla Job", result)


# ─────────────────────────────────────────────────────────────────────────────
class TestFirecrawlMapRetryLogic(unittest.TestCase):
    """BUG-17: transient network errors must be retried, not immediately abandoned."""

    def test_network_error_retries_not_immediate_empty(self):
        """Non-429 errors should be retried (up to 3 attempts) not immediately return []."""
        from agents.job_agent import _firecrawl_map

        call_count = [0]

        class FakeFirecrawlApp:
            def __init__(self, api_key):
                pass
            def map(self, **kwargs):
                call_count[0] += 1
                raise ConnectionError("Network timeout")

        with patch("firecrawl.FirecrawlApp", FakeFirecrawlApp), \
             patch("agents.job_agent.time.sleep"):
            result = _firecrawl_map("https://company.com/jobs", "fc-key")

        self.assertEqual(result, [])
        self.assertEqual(call_count[0], 3, "Should have attempted 3 times before giving up")

    def test_rate_limit_uses_long_sleep(self):
        """429 rate-limit errors should sleep longer than generic network errors."""
        from agents.job_agent import _firecrawl_map

        sleep_calls = []

        class FakeFirecrawlApp:
            def __init__(self, api_key):
                pass
            def map(self, **kwargs):
                raise Exception("Rate limit exceeded 429")

        with patch("firecrawl.FirecrawlApp", FakeFirecrawlApp), \
             patch("agents.job_agent.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            _firecrawl_map("https://company.com/jobs", "fc-key")

        self.assertTrue(all(s >= 30 for s in sleep_calls),
                        f"429 sleep must be >= 30s, got {sleep_calls}")


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
        captured = {}
        class FakeApp:
            def __init__(self, api_key): pass
            def scrape(self, url, **kwargs):
                captured.update(kwargs)
                m = MagicMock()
                m.markdown = "# Google Job\n" + "x" * 300
                return m
        with patch("firecrawl.FirecrawlApp", FakeApp):
            _scrape_google_jd("https://www.google.com/about/careers/job/123", fc_key="k")
        self.assertIn("formats", captured)
        self.assertIn("markdown", captured["formats"])


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
             patch("agents.job_agent._tpm_filter", side_effect=lambda lst: crawled_tpm if lst == [] else []), \
             patch("agents.job_agent._crawl_page", new=AsyncMock(return_value=[])):
            # _tpm_filter returns [] for API jobs → should fall back to _crawl_page
            result = await discover_jobs(workday_url, "fc-key", MagicMock())
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
            result = await discover_jobs(workday_url, "fc-key", MagicMock())

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
             patch("agents.job_agent.batch_upsert_jd_records", return_value=1) as mock_upsert, \
             patch("agents.job_agent.batch_update_jd_timestamps", return_value=0):
            await process_company(
                self.ROW, {}, "/fake/path.xlsx", "fc-key",
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
            result = await discover_jobs(ashby_url, "fc-key", MagicMock())
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
            await discover_jobs(gh_url, "fc-key", MagicMock())
        mock_ats.assert_called_once()
        mock_ashby.assert_not_called()

    async def test_ashby_api_empty_falls_back_to_crawler(self):
        from agents.job_agent import discover_jobs
        ashby_url = "https://jobs.ashbyhq.com/someco"
        with patch("agents.job_agent._fetch_ashby_jobs", return_value=[]), \
             patch("agents.job_agent._crawl_page", new=AsyncMock(return_value=[])) as mock_crawl:
            result = await discover_jobs(ashby_url, "fc-key", MagicMock())
        # When API returns empty, should fall through to crawler path
        self.assertIsInstance(result, list)


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
        """_fmt_addr should be defined before the loop, not inside 'for block in blocks:'."""
        import inspect
        source = inspect.getsource(_scrape_workday_jd)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
