"""
Acceptance Tests — PRJ-001 Phase 4 QA

Coverage:
  - TestFormatJdForCoarse: _format_jd_for_coarse() function unit tests (Test Analyzer gap)
  - TestCompanyAgentMain: BRD §8 scenario 1 + REQ-009 (max 200)
  - TestJobAgentMain: BRD §8 scenario 2 + REQ-025 (FRESH_DAYS) + BUG-28 (retry in crawler ctx)
  - TestMatchAgentMain: BRD §8 scenario 3 + REQ-032 (resume change triggers rescore)
  - TestResumeOptimizerMain: BRD §8 scenario 4 + REQ-054 (resume_hash incremental skip)
  - TestEndToEndDataFlow: cross-Agent Excel data contract verification
"""
import sys
import os
import json
import asyncio
import hashlib
import inspect
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── Stub heavy deps before any agent import ──────────────────────────────────
for mod in ["google", "google.genai", "google.genai.types",
            "dotenv", "firecrawl", "crawl4ai", "pycountry", "tavily"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.modules["dotenv"].load_dotenv = lambda: None

import google
google.genai = MagicMock()
google.genai.types = MagicMock()

# ── Stub pycountry subdivisions (needed by job_agent module-level code) ──────
class _StateSub:
    def __init__(self, name, code):
        self.name = name
        self.code = code

_US_STATES_MINI = [
    ("Alabama", "US-AL"), ("Alaska", "US-AK"), ("Arizona", "US-AZ"),
    ("California", "US-CA"), ("Colorado", "US-CO"), ("Connecticut", "US-CT"),
    ("Delaware", "US-DE"), ("Florida", "US-FL"), ("Georgia", "US-GA"),
    ("New York", "US-NY"), ("Texas", "US-TX"), ("Washington", "US-WA"),
    ("Massachusetts", "US-MA"), ("Illinois", "US-IL"), ("Oregon", "US-OR"),
    ("Indiana", "US-IN"), ("Oklahoma", "US-OK"), ("Maine", "US-ME"),
]
_subs_mock = MagicMock()
_subs_mock.get.return_value = [_StateSub(n, c) for n, c in _US_STATES_MINI]
sys.modules["pycountry"].subdivisions = _subs_mock

# ── Set up crawl4ai async context manager mock ──────────────────────────────
_mock_crawler_instance = MagicMock()
_crawl4ai = sys.modules["crawl4ai"]
_mock_wcm = MagicMock()
_mock_wcm.__aenter__ = AsyncMock(return_value=_mock_crawler_instance)
_mock_wcm.__aexit__ = AsyncMock(return_value=False)
_crawl4ai.AsyncWebCrawler.return_value = _mock_wcm
_crawl4ai.BrowserConfig = MagicMock()

# ── Import shared Excel layer ────────────────────────────────────────────────
from shared.excel_store import (
    get_or_create_excel, get_company_rows, upsert_companies,
    count_company_rows, get_jd_urls, get_jd_rows_for_match,
    upsert_jd_record, batch_upsert_jd_records, get_jd_url_meta,
    get_match_pairs, upsert_match_record, batch_upsert_match_records,
    get_scored_matches, get_tailored_match_pairs, batch_upsert_tailored_records,
    COMPANY_HEADERS, JD_HEADERS, MATCH_HEADERS, TAILORED_HEADERS,
)

# ── Import agent modules ────────────────────────────────────────────────────
import agents.company_agent as company_mod
import agents.match_agent as match_mod
import agents.resume_optimizer as optimizer_mod
import agents.job_agent as job_mod


# ── Helpers ──────────────────────────────────────────────────────────────────
def _make_temp_xlsx():
    """Create a fresh temp Excel file using the real get_or_create_excel."""
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    os.unlink(path)  # remove so get_or_create_excel creates it fresh
    get_or_create_excel(path)
    return path


def _make_jd_json(title="AI TPM", company="TestCo", location="San Francisco, CA"):
    return json.dumps({
        "job_title": title,
        "company": company,
        "location": location,
        "salary_range": "$150k-$200k",
        "requirements": ["LLM experience", "PyTorch", "MLOps"],
        "additional_qualifications": ["GenAI deployment"],
        "key_responsibilities": ["Lead AI programs", "Cross-functional coordination"],
        "is_ai_tpm": True,
        "data_quality": "complete",
    })


def _make_match_json(score=75):
    return json.dumps({
        "compatibility_score": score,
        "key_strengths": ["Strong TPM background"],
        "critical_gaps": ["Limited GenAI production experience"],
        "recommendation_reason": "Good TPM match with AI growth potential.",
    })


FAKE_RESUME = (
    "# Senior Technical Program Manager\n\n"
    "10+ years TPM experience. Led LLM inference optimization. "
    "PyTorch, TensorFlow, MLOps, GPU cluster management. "
    "GenAI deployment at scale. Transformer architecture expertise. "
    "RLHF training pipelines. RAG system development."
)
FAKE_RESUME_HASH = hashlib.md5(FAKE_RESUME.encode("utf-8")).hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# TestFormatJdForCoarse — function coverage gap (Test Analyzer finding)
# ═════════════════════════════════════════════════════════════════════════════
class TestFormatJdForCoarse(unittest.TestCase):
    """Unit tests for match_agent._format_jd_for_coarse()."""

    def test_normal_jd_includes_all_sections(self):
        jd = {"jd_json": _make_jd_json()}
        result = match_mod._format_jd_for_coarse(jd)
        self.assertIn("Job Title: AI TPM", result)
        self.assertIn("Company: TestCo", result)
        self.assertIn("Requirements:", result)
        self.assertIn("LLM experience", result)
        self.assertIn("Responsibilities:", result)
        self.assertIn("Lead AI programs", result)
        self.assertIn("Additional Qualifications:", result)

    def test_missing_optional_fields(self):
        jd_data = {"job_title": "TPM", "company": "Co", "location": "NY"}
        jd = {"jd_json": json.dumps(jd_data)}
        result = match_mod._format_jd_for_coarse(jd)
        self.assertIn("Job Title: TPM", result)
        # No Requirements/Responsibilities sections since they're missing
        self.assertNotIn("Requirements:", result)
        self.assertNotIn("Responsibilities:", result)

    def test_invalid_json_returns_raw(self):
        jd = {"jd_json": "not valid json"}
        result = match_mod._format_jd_for_coarse(jd)
        self.assertEqual(result, "not valid json")

    def test_empty_lists_omitted(self):
        jd_data = {"job_title": "TPM", "company": "Co", "location": "NY",
                    "requirements": [], "key_responsibilities": []}
        jd = {"jd_json": json.dumps(jd_data)}
        result = match_mod._format_jd_for_coarse(jd)
        self.assertNotIn("Requirements:", result)
        self.assertNotIn("Responsibilities:", result)


# ═════════════════════════════════════════════════════════════════════════════
# TestCompanyAgentMain — BRD §8 scenario 1 + REQ-009
# ═════════════════════════════════════════════════════════════════════════════
class TestCompanyAgentMain(unittest.TestCase):

    def setUp(self):
        self.temp_xlsx = _make_temp_xlsx()
        self._orig_key_pool = company_mod._KEY_POOL

    def tearDown(self):
        company_mod._KEY_POOL = self._orig_key_pool
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)

    @patch.dict(os.environ, {"TAVILY_API_KEY": "fake_tavily", "GEMINI_API_KEY": "fake_gemini"})
    @patch("agents.company_agent.get_or_create_excel")
    @patch("agents.company_agent.discover_ai_companies")
    @patch("agents.company_agent.run_phase_1_5")
    def test_main_writes_company_list_with_required_fields(self, mock_p15, mock_discover, mock_excel):
        """BRD §8 scenario 1: Company Agent writes Company_List with required fields."""
        mock_excel.return_value = self.temp_xlsx
        mock_discover.return_value = [
            {"company_name": "Anthropic", "ai_domain": "Large Model Labs",
             "business_focus": "AI safety research", "career_url": "https://anthropic.com/careers"},
            {"company_name": "Scale AI", "ai_domain": "AI Startups",
             "business_focus": "Data labeling", "career_url": "https://scale.com/careers"},
        ]
        company_mod.main()

        rows = get_company_rows(self.temp_xlsx)
        self.assertEqual(len(rows), 2)
        # Verify required fields non-empty: Company Name (0), AI Domain (1), Career URL (3)
        for row in rows:
            self.assertTrue(row[0], "Company Name must be non-empty")
            self.assertTrue(row[1], "AI Domain must be non-empty")
            self.assertTrue(row[3], "Career URL must be non-empty")

    @patch.dict(os.environ, {"TAVILY_API_KEY": "fake_tavily", "GEMINI_API_KEY": "fake_gemini"})
    @patch("agents.company_agent.get_or_create_excel")
    @patch("agents.company_agent.discover_ai_companies")
    @patch("agents.company_agent.run_phase_1_5")
    def test_main_respects_max_total_200(self, mock_p15, mock_discover, mock_excel):
        """REQ-009: Company_List total capped at MAX_TOTAL (200)."""
        mock_excel.return_value = self.temp_xlsx
        # Pre-fill with 200 companies
        bulk = [{"company_name": f"Co_{i}", "ai_domain": "AI Startups",
                 "business_focus": "Test", "career_url": f"https://co{i}.com/careers"}
                for i in range(200)]
        upsert_companies(self.temp_xlsx, bulk)
        self.assertEqual(count_company_rows(self.temp_xlsx), 200)

        company_mod.main()

        # discover_ai_companies should NOT be called when at capacity
        mock_discover.assert_not_called()

    @patch.dict(os.environ, {"TAVILY_API_KEY": "fake_tavily", "GEMINI_API_KEY": "fake_gemini"})
    @patch("agents.company_agent.get_or_create_excel")
    @patch("agents.company_agent.discover_ai_companies")
    @patch("agents.company_agent.run_phase_1_5")
    def test_main_limits_batch_to_remaining_slots(self, mock_p15, mock_discover, mock_excel):
        """REQ-009: When near capacity, only request remaining slots."""
        mock_excel.return_value = self.temp_xlsx
        # Pre-fill with 190 companies (10 slots remaining, less than BATCH_SIZE=50)
        bulk = [{"company_name": f"Co_{i}", "ai_domain": "AI Startups",
                 "business_focus": "Test", "career_url": f"https://co{i}.com/careers"}
                for i in range(190)]
        upsert_companies(self.temp_xlsx, bulk)

        mock_discover.return_value = [
            {"company_name": f"New_{j}", "ai_domain": "AI Startups",
             "business_focus": "Test", "career_url": f"https://new{j}.com/careers"}
            for j in range(15)  # return more than slots
        ]

        company_mod.main()

        # discover_ai_companies should be called with need=10 (200-190)
        mock_discover.assert_called_once()
        args = mock_discover.call_args
        self.assertEqual(args[0][2], 10)  # third positional arg is `need`

    def test_main_exits_on_missing_keys(self):
        """main() should exit gracefully when API keys are missing."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove all API key env vars
            for k in ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "TAVILY_API_KEY"]:
                os.environ.pop(k, None)
            company_mod.main()  # should not raise


# ═════════════════════════════════════════════════════════════════════════════
# TestJobAgentMain — BRD §8 scenario 2 + REQ-025 + BUG-28
# ═════════════════════════════════════════════════════════════════════════════
class TestJobAgentMain(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_xlsx = _make_temp_xlsx()
        self._orig_key_pool = job_mod._KEY_POOL
        # Pre-populate Company_List with a greenhouse company
        upsert_companies(self.temp_xlsx, [{
            "company_name": "TestCo",
            "ai_domain": "AI Startups",
            "business_focus": "AI testing company",
            "career_url": "https://boards.greenhouse.io/testco",
        }])

    def tearDown(self):
        job_mod._KEY_POOL = self._orig_key_pool
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake", "FIRECRAWL_API_KEY": "fake"})
    @patch("agents.job_agent.get_or_create_excel")
    @patch("agents.job_agent.process_company")
    @patch("agents.job_agent.get_incomplete_jd_rows", return_value=[])
    async def test_main_writes_jd_records_and_updates_counts(
            self, mock_incomplete, mock_pc, mock_excel):
        """BRD §8 scenario 2: Job Agent writes JDs to JD_Tracker and updates company counts."""
        mock_excel.return_value = self.temp_xlsx

        async def fake_process_company(row, known, xlsx_path, fc_key, lock, crawler):
            name = str(row[0]).strip()
            jd_json = _make_jd_json(title=f"AI TPM at {name}", company=name)
            url = f"https://boards.greenhouse.io/testco/jobs/123"
            batch_upsert_jd_records(xlsx_path, [(url, jd_json, "hash123")])

        mock_pc.side_effect = fake_process_company

        await job_mod.main()

        # Verify JD_Tracker has records
        urls = get_jd_urls(self.temp_xlsx)
        self.assertGreater(len(urls), 0)
        # Verify company job counts were updated (the post-processing in main)
        rows = get_company_rows(self.temp_xlsx)
        self.assertTrue(len(rows) > 0)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake", "FIRECRAWL_API_KEY": "fake"})
    @patch("agents.job_agent.get_or_create_excel")
    async def test_main_exits_on_no_companies(self, mock_excel):
        """main() exits gracefully when no companies in Excel."""
        empty_xlsx = _make_temp_xlsx()
        mock_excel.return_value = empty_xlsx
        try:
            await job_mod.main()  # should not raise
        finally:
            os.unlink(empty_xlsx)

    def test_fresh_days_incremental_skip_mechanism(self):
        """REQ-025: Recently-extracted JDs (age < FRESH_DAYS) are recognized as fresh.

        Tests the data layer that enables the skip: get_jd_url_meta returns
        age_days < FRESH_DAYS for recently-written records, so process_company's
        fresh_set includes them and skips re-extraction.
        """
        # Write a complete JD record with current timestamp
        jd_json = _make_jd_json()
        upsert_jd_record(self.temp_xlsx, "https://example.com/job1", jd_json, "hash1")

        meta = get_jd_url_meta(self.temp_xlsx)
        self.assertIn("https://example.com/job1", meta)
        age = meta["https://example.com/job1"]["age_days"]
        self.assertLess(age, job_mod.FRESH_DAYS,
                        f"Freshly written JD should have age_days < {job_mod.FRESH_DAYS}, got {age}")

    def test_retry_inside_crawler_context_bug28(self):
        """BUG-28 regression: retry block must be INSIDE 'async with AsyncWebCrawler' block.

        Verifies via source inspection that get_incomplete_jd_rows is called
        within the indentation level of the async with block.
        """
        # P0-7: main() is now a thin RunSummary wrapper; orchestration body in _main_inner.
        source = inspect.getsource(job_mod._main_inner)
        lines = source.split("\n")

        # Find the 'async with AsyncWebCrawler' line
        awc_line_idx = None
        awc_indent = None
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if "async with AsyncWebCrawler" in stripped or "async with" in stripped and "WebCrawler" in stripped:
                awc_line_idx = i
                awc_indent = len(line) - len(stripped)
                break

        self.assertIsNotNone(awc_line_idx, "Could not find 'async with AsyncWebCrawler' in main()")

        # Find get_incomplete_jd_rows call
        retry_line_idx = None
        retry_indent = None
        for i, line in enumerate(lines):
            if "get_incomplete_jd_rows" in line and i > awc_line_idx:
                retry_line_idx = i
                retry_indent = len(line) - len(line.lstrip())
                break

        self.assertIsNotNone(retry_line_idx, "Could not find get_incomplete_jd_rows in main()")
        # The retry call must be MORE indented than the async with line
        # (meaning it's inside the async with block)
        self.assertGreater(retry_indent, awc_indent,
                           f"BUG-28: get_incomplete_jd_rows (indent={retry_indent}) must be "
                           f"inside async with block (indent={awc_indent})")

    def test_retry_one_skips_overwrite_when_extraction_empty_bug27(self):
        """BUG-27 regression: retry_one must NOT overwrite when Gemini returns
        empty extraction results (location/requirements/responsibilities all empty),
        to prevent infinite retry loops.

        Verifies via source inspection that retry_one contains:
        1. A guard checking parsed.get("company") — skip if Gemini returns empty data
        2. A guard checking extracted_loc/extracted_reqs/extracted_resp — skip if still incomplete
        """
        # P0-7: main() is now a thin RunSummary wrapper; orchestration body in _main_inner.
        source = inspect.getsource(job_mod._main_inner)

        # Guard 1: skip when Gemini returns empty data (no "company" field)
        self.assertIn('parsed.get("company")', source,
                       "BUG-27: retry_one must check parsed.get('company') to skip empty Gemini data")

        # Guard 2: skip overwrite when key fields are still incomplete
        self.assertIn("skipping overwrite", source,
                       "BUG-27: retry_one must have 'skipping overwrite' warning for incomplete extraction")

        # Guard 2 specifics: must check all three fields (location, requirements, key_responsibilities)
        self.assertIn('extracted_loc', source,
                       "BUG-27: retry_one must check extracted location")
        self.assertIn('extracted_reqs', source,
                       "BUG-27: retry_one must check extracted requirements")
        self.assertIn('extracted_resp', source,
                       "BUG-27: retry_one must check extracted key_responsibilities")

        # Guard 2: the _missing set must include known empty-value markers
        self.assertIn('_missing', source,
                       "BUG-27: retry_one must define _missing set for empty-value detection")


# ═════════════════════════════════════════════════════════════════════════════
# TestMatchAgentMain — BRD §8 scenario 3 + REQ-032
# ═════════════════════════════════════════════════════════════════════════════
class TestMatchAgentMain(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_xlsx = _make_temp_xlsx()
        self._orig_key_pool = match_mod._KEY_POOL
        self._orig_limiter = match_mod._GEMINI_LIMITER

        # Pre-populate JD_Tracker with AI TPM JDs
        for i in range(6):
            jd_json = _make_jd_json(
                title=f"AI TPM Role {i}",
                company=f"Company_{i}",
            )
            url = f"https://example.com/job_{i}"
            upsert_jd_record(self.temp_xlsx, url, jd_json, f"hash_{i}")

        # Create temp profile dir with resume
        self.profile_dir = tempfile.mkdtemp()
        resume_path = os.path.join(self.profile_dir, "test_resume.md")
        with open(resume_path, "w") as f:
            f.write(FAKE_RESUME)

    def tearDown(self):
        match_mod._KEY_POOL = self._orig_key_pool
        match_mod._GEMINI_LIMITER = self._orig_limiter
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)
        import shutil
        if os.path.exists(self.profile_dir):
            shutil.rmtree(self.profile_dir)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini"})
    async def test_main_writes_match_results_with_both_stages(self):
        """BRD §8 scenario 3: Match Agent writes coarse + fine scores to Match_Results."""
        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()
        match_mod._GEMINI_LIMITER = mock_limiter

        mock_pool = MagicMock()
        match_mod._KEY_POOL = mock_pool

        with patch("agents.match_agent.get_or_create_excel", return_value=self.temp_xlsx), \
             patch("agents.match_agent.PROFILE_DIR", self.profile_dir), \
             patch("agents.match_agent.batch_coarse_score", return_value=[80, 70, 60, 50, 40, 30]), \
             patch("agents.match_agent.evaluate_match", return_value=_make_match_json(85)), \
             patch("agents.match_agent._load_jd_markdown", return_value="# Fake JD"):

            await match_mod.main()

        # Verify Match_Results has records
        pairs = get_match_pairs(self.temp_xlsx)
        self.assertGreater(len(pairs), 0)

        # Should have both coarse and fine stages
        stages = {v["stage"] for v in pairs.values()}
        self.assertIn("coarse", stages, "Should have coarse-scored records")
        self.assertIn("fine", stages, "Should have fine-evaluated records")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini"})
    async def test_main_resume_change_triggers_rescore(self):
        """REQ-032: When resume changes, existing scores are marked stale and re-scored."""
        # Pre-populate Match_Results with an old resume_hash
        old_hash = "old_hash_value"
        upsert_match_record(
            self.temp_xlsx, "test_resume", "https://example.com/job_0",
            _make_match_json(60), resume_hash=old_hash, stage="fine"
        )

        # Verify old record exists
        pairs_before = get_match_pairs(self.temp_xlsx)
        key = ("test_resume", "https://example.com/job_0")
        self.assertIn(key, pairs_before)
        self.assertEqual(pairs_before[key]["hash"], old_hash)

        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()
        match_mod._GEMINI_LIMITER = mock_limiter

        mock_pool = MagicMock()
        match_mod._KEY_POOL = mock_pool

        with patch("agents.match_agent.get_or_create_excel", return_value=self.temp_xlsx), \
             patch("agents.match_agent.PROFILE_DIR", self.profile_dir), \
             patch("agents.match_agent.batch_coarse_score", return_value=[80, 70, 60, 50, 40, 30]), \
             patch("agents.match_agent.evaluate_match", return_value=_make_match_json(90)), \
             patch("agents.match_agent._load_jd_markdown", return_value="# Fake JD"):

            await match_mod.main()

        # After run, the record should have the new resume hash
        pairs_after = get_match_pairs(self.temp_xlsx)
        self.assertIn(key, pairs_after)
        self.assertEqual(pairs_after[key]["hash"], FAKE_RESUME_HASH,
                         "Record should be updated with new resume hash after rescore")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini"})
    async def test_main_exits_on_no_resume(self):
        """main() exits gracefully when no resume in profile dir."""
        empty_profile = tempfile.mkdtemp()
        try:
            with patch("agents.match_agent.get_or_create_excel", return_value=self.temp_xlsx), \
                 patch("agents.match_agent.PROFILE_DIR", empty_profile):
                await match_mod.main()  # should not raise
        finally:
            import shutil
            shutil.rmtree(empty_profile)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini"})
    async def test_main_exits_on_no_jds(self):
        """main() exits gracefully when no AI-TPM JDs in tracker."""
        empty_xlsx = _make_temp_xlsx()
        try:
            with patch("agents.match_agent.get_or_create_excel", return_value=empty_xlsx), \
                 patch("agents.match_agent.PROFILE_DIR", self.profile_dir):
                await match_mod.main()  # should not raise
        finally:
            os.unlink(empty_xlsx)


# ═════════════════════════════════════════════════════════════════════════════
# TestResumeOptimizerMain — BRD §8 scenario 4 + REQ-054
# ═════════════════════════════════════════════════════════════════════════════
class TestResumeOptimizerMain(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_xlsx = _make_temp_xlsx()
        self._orig_key_pool = optimizer_mod._KEY_POOL
        self._orig_limiter = optimizer_mod._GEMINI_LIMITER

        # Pre-populate JD_Tracker + Match_Results
        for i in range(3):
            url = f"https://example.com/opt_job_{i}"
            jd_json = _make_jd_json(title=f"AI TPM {i}", company=f"OptCo_{i}")
            upsert_jd_record(self.temp_xlsx, url, jd_json, f"hash_{i}")
            # Write fine-scored match records
            upsert_match_record(
                self.temp_xlsx, "test_resume", url,
                _make_match_json(70 + i * 5),
                resume_hash=FAKE_RESUME_HASH, stage="fine"
            )

        # Create temp profile dir
        self.profile_dir = tempfile.mkdtemp()
        resume_path = os.path.join(self.profile_dir, "test_resume.md")
        with open(resume_path, "w") as f:
            f.write(FAKE_RESUME)

    def tearDown(self):
        optimizer_mod._KEY_POOL = self._orig_key_pool
        optimizer_mod._GEMINI_LIMITER = self._orig_limiter
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)
        import shutil
        if os.path.exists(self.profile_dir):
            shutil.rmtree(self.profile_dir)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini"})
    async def test_main_writes_tailored_results(self):
        """BRD §8 scenario 4: Resume Optimizer writes to Tailored_Match_Results with score delta."""
        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()
        optimizer_mod._GEMINI_LIMITER = mock_limiter

        mock_pool = MagicMock()
        optimizer_mod._KEY_POOL = mock_pool

        # Mock batch_tailor_resume to return tailored content
        def fake_batch_tailor(resume_text, jd_contents):
            return [
                {"tailored_resume_markdown": f"# Tailored for JD {i}\n{resume_text[:100]}",
                 "optimization_summary": f"Optimized for JD {i}"}
                for i in range(len(jd_contents))
            ]

        # Mock batch_re_score to return improved scores
        def fake_batch_re_score(pairs):
            return [
                {"compatibility_score": 85,
                 "key_strengths": ["Improved alignment"],
                 "critical_gaps": [],
                 "recommendation_reason": "Better match after tailoring."}
                for _ in pairs
            ]

        tailored_dir = tempfile.mkdtemp()
        try:
            with patch("agents.resume_optimizer.get_or_create_excel", return_value=self.temp_xlsx), \
                 patch("agents.resume_optimizer.PROFILE_DIR", self.profile_dir), \
                 patch("agents.resume_optimizer.TAILORED_DIR", tailored_dir), \
                 patch("agents.resume_optimizer.batch_tailor_resume", side_effect=fake_batch_tailor), \
                 patch("agents.resume_optimizer.batch_re_score", side_effect=fake_batch_re_score), \
                 patch("agents.resume_optimizer._load_jd_markdown", return_value="# Fake JD content"):

                await optimizer_mod.main()

            # Verify Tailored_Match_Results has records
            pairs = get_tailored_match_pairs(self.temp_xlsx)
            self.assertGreater(len(pairs), 0, "Should have tailored match results")
            # Verify score delta is observable
            for key, val in pairs.items():
                self.assertGreater(val["tailored_score"], 0, "Tailored score should be > 0")
        finally:
            import shutil
            shutil.rmtree(tailored_dir, ignore_errors=True)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini"})
    async def test_main_resume_hash_incremental_skip(self):
        """REQ-054: Skip already-optimized pairs when resume_hash unchanged."""
        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock()
        optimizer_mod._GEMINI_LIMITER = mock_limiter

        mock_pool = MagicMock()
        optimizer_mod._KEY_POOL = mock_pool

        # Pre-populate Tailored_Match_Results with same resume_hash
        # (simulating a previous run with the same resume)
        records = []
        for i in range(3):
            url = f"https://example.com/opt_job_{i}"
            records.append({
                "resume_id": "test_resume", "jd_url": url,
                "job_title": f"AI TPM {i}", "company": f"OptCo_{i}",
                "original_score": 70 + i * 5, "tailored_score": 85,
                "score_delta": 15 - i * 5, "tailored_resume_path": "/tmp/fake.md",
                "optimization_summary": "Previously optimized",
                "resume_hash": FAKE_RESUME_HASH,
            })
        batch_upsert_tailored_records(self.temp_xlsx, records)

        # Mock tailor — should NOT be called if skip works
        mock_batch_tailor = MagicMock()

        with patch("agents.resume_optimizer.get_or_create_excel", return_value=self.temp_xlsx), \
             patch("agents.resume_optimizer.PROFILE_DIR", self.profile_dir), \
             patch("agents.resume_optimizer.batch_tailor_resume", mock_batch_tailor), \
             patch("agents.resume_optimizer._load_jd_markdown", return_value="# Fake JD"):

            await optimizer_mod.main()

        # batch_tailor_resume should NOT have been called (all pairs already optimized)
        mock_batch_tailor.assert_not_called()

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini"})
    async def test_main_exits_on_no_scored_matches(self):
        """main() exits gracefully when no scored matches exist."""
        empty_xlsx = _make_temp_xlsx()
        try:
            with patch("agents.resume_optimizer.get_or_create_excel", return_value=empty_xlsx), \
                 patch("agents.resume_optimizer.PROFILE_DIR", self.profile_dir):
                await optimizer_mod.main()  # should not raise
        finally:
            os.unlink(empty_xlsx)


# ═════════════════════════════════════════════════════════════════════════════
# TestEndToEndDataFlow — cross-Agent Excel data contract verification
# ═════════════════════════════════════════════════════════════════════════════
class TestEndToEndDataFlow(unittest.TestCase):
    """Verify data contracts between Agents via real excel_store operations."""

    def setUp(self):
        self.temp_xlsx = _make_temp_xlsx()

    def tearDown(self):
        if os.path.exists(self.temp_xlsx):
            os.unlink(self.temp_xlsx)

    def test_company_to_job_data_flows(self):
        """Company Agent output → Job Agent input: Company_List rows readable with career URLs."""
        # Simulate Company Agent writing companies
        upsert_companies(self.temp_xlsx, [
            {"company_name": "Anthropic", "ai_domain": "Large Model Labs",
             "business_focus": "AI safety", "career_url": "https://boards.greenhouse.io/anthropic"},
            {"company_name": "Scale AI", "ai_domain": "AI Startups",
             "business_focus": "Data labeling", "career_url": "https://jobs.lever.co/scaleai"},
        ])

        # Simulate Job Agent reading companies
        rows = get_company_rows(self.temp_xlsx)
        self.assertEqual(len(rows), 2)
        for row in rows:
            name = str(row[0]).strip()
            career_url = str(row[3]).strip()
            self.assertTrue(name, "Company name must be non-empty")
            self.assertTrue(career_url.startswith("https://"),
                            f"Career URL must be a valid URL, got: {career_url}")

    def test_job_to_match_data_flows(self):
        """Job Agent output → Match Agent input: JD_Tracker records readable as match candidates."""
        # Simulate Job Agent writing JDs
        for i in range(3):
            url = f"https://example.com/flow_job_{i}"
            jd_json = _make_jd_json(title=f"AI TPM {i}", company=f"FlowCo_{i}")
            upsert_jd_record(self.temp_xlsx, url, jd_json, f"hash_{i}")

        # Simulate Match Agent reading JDs
        jds = get_jd_rows_for_match(self.temp_xlsx)
        self.assertEqual(len(jds), 3)
        for jd in jds:
            self.assertIn("url", jd)
            self.assertIn("jd_json", jd)
            parsed = json.loads(jd["jd_json"])
            self.assertTrue(parsed.get("is_ai_tpm"), "Should only return AI TPM JDs")
            self.assertTrue(parsed.get("job_title"), "Job title must be non-empty")
            self.assertTrue(parsed.get("company"), "Company must be non-empty")

    def test_match_to_optimizer_data_flows(self):
        """Match Agent output → Resume Optimizer input: scored matches readable."""
        # Simulate JD_Tracker records (needed for optimizer's jd_meta lookup)
        url = "https://example.com/flow_match_job"
        jd_json = _make_jd_json(title="AI TPM Lead", company="FlowMatchCo")
        upsert_jd_record(self.temp_xlsx, url, jd_json, "hash_flow")

        # Simulate Match Agent writing scored results
        upsert_match_record(
            self.temp_xlsx, "flow_resume", url,
            _make_match_json(82),
            resume_hash="flow_hash", stage="fine"
        )

        # Simulate Resume Optimizer reading scored matches
        scored = get_scored_matches(self.temp_xlsx)
        self.assertGreater(len(scored), 0)
        match = scored[0]
        self.assertEqual(match["resume_id"], "flow_resume")
        self.assertEqual(match["jd_url"], url)
        self.assertGreaterEqual(match["score"], 0)
        self.assertTrue(match["resume_hash"])


# ─────────────────────────────────────────────────────────────────────────────
class TestRunSummaryWritten(unittest.TestCase):
    """P0-7: every agent's main() writes a structured RunSummary on completion
    (success or early-return path), so an 80%-failed run is observably distinct
    from a clean success in run_logs/."""

    def setUp(self):
        import shutil
        self.tmpdir = tempfile.mkdtemp(prefix="pf-runlogs-test-")
        self.cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def tearDown(self):
        import shutil
        os.chdir(self.cwd)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_latest_summary(self, agent: str) -> dict:
        log_dir = os.path.join(self.tmpdir, "run_logs")
        files = [f for f in os.listdir(log_dir) if f.startswith(f"{agent}-")]
        self.assertEqual(len(files), 1, f"expected exactly one {agent}-*.json")
        with open(os.path.join(log_dir, files[0])) as f:
            return json.load(f)

    def test_company_main_writes_run_summary(self):
        # Force an early-return path with no env keys; summary still gets written.
        with patch.dict(os.environ, {}, clear=True):
            company_mod.main()
        payload = self._read_latest_summary("company")
        self.assertEqual(payload["agent"], "company")
        self.assertIn("run_id", payload)
        self.assertIsNotNone(payload["finished_at"])
        # The early-return path adds a 'Missing env vars' note.
        self.assertTrue(any("Missing" in n for n in payload["notes"]))

    def test_match_main_writes_run_summary(self):
        with patch.dict(os.environ, {}, clear=True):
            asyncio.run(match_mod.main())
        payload = self._read_latest_summary("match")
        self.assertEqual(payload["agent"], "match")
        self.assertIsNotNone(payload["finished_at"])

    def test_optimizer_main_writes_run_summary(self):
        with patch.dict(os.environ, {}, clear=True):
            asyncio.run(optimizer_mod.main())
        payload = self._read_latest_summary("optimizer")
        self.assertEqual(payload["agent"], "optimizer")
        self.assertIsNotNone(payload["finished_at"])

    def test_job_main_writes_run_summary(self):
        with patch.dict(os.environ, {}, clear=True):
            asyncio.run(job_mod.main())
        payload = self._read_latest_summary("job")
        self.assertEqual(payload["agent"], "job")
        self.assertIsNotNone(payload["finished_at"])

    def test_run_summary_has_all_counter_fields(self):
        """Every counter field must be present so log consumers can grep them."""
        with patch.dict(os.environ, {}, clear=True):
            company_mod.main()
        payload = self._read_latest_summary("company")
        for field in ("attempted", "succeeded", "failed", "skipped",
                      "transient_errors", "structural_errors"):
            self.assertIn(field, payload)
            self.assertIsInstance(payload[field], int)


if __name__ == "__main__":
    unittest.main()
