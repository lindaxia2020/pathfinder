"""
P0-3 prompt-injection guard regression tests.

Asserts that:
1. SECURITY_CLAUSE is included in every shared system prompt.
2. Every Gemini call site that takes scraped/external content wraps it in
   <scraped_content>...</scraped_content> delimiters before sending.

Runs without real Gemini calls — captures the `contents` argument passed
to _KEY_POOL.generate_content via MagicMock and inspects it.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── Stub heavy deps before import ────────────────────────────────────────────
for mod in ["google", "google.genai", "google.genai.types", "dotenv",
            "firecrawl", "crawl4ai", "pycountry", "tavily"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.modules["dotenv"].load_dotenv = lambda: None

import google
google.genai = MagicMock()
google.genai.types = MagicMock()

import shared.prompts as prompts
import agents.match_agent as match_mod
import agents.resume_optimizer as opt_mod
import agents.job_agent as job_mod


def _make_pool_capturing(returned_text: str = '{"items": []}'):
    """Return a pool whose generate_content captures (model, contents, config)."""
    pool = MagicMock()
    response = MagicMock()
    response.text = returned_text
    pool.generate_content.return_value = response
    return pool


class TestSecurityClauseInPrompts(unittest.TestCase):
    """Every shared system prompt must include SECURITY_CLAUSE."""

    def test_security_clause_is_non_empty(self):
        self.assertTrue(prompts.SECURITY_CLAUSE.strip())
        self.assertIn("<scraped_content>", prompts.SECURITY_CLAUSE)

    def test_coarse_prompt_includes_security_clause(self):
        self.assertIn(prompts.SECURITY_CLAUSE, prompts.COARSE_SYSTEM_PROMPT)

    def test_fine_prompt_includes_security_clause(self):
        self.assertIn(prompts.SECURITY_CLAUSE, prompts.FINE_SYSTEM_PROMPT)

    def test_tailor_prompt_includes_security_clause(self):
        self.assertIn(prompts.SECURITY_CLAUSE, prompts.TAILOR_SYSTEM_PROMPT)

    def test_batch_fine_prompt_inherits_security_clause(self):
        # BATCH_FINE_SYSTEM_PROMPT = FINE_SYSTEM_PROMPT + batch instructions,
        # so it inherits SECURITY_CLAUSE via concatenation.
        self.assertIn(prompts.SECURITY_CLAUSE, prompts.BATCH_FINE_SYSTEM_PROMPT)

    def test_batch_tailor_prompt_inherits_security_clause(self):
        self.assertIn(prompts.SECURITY_CLAUSE, prompts.BATCH_TAILOR_SYSTEM_PROMPT)


class TestMatchAgentWrapsScrapedContent(unittest.TestCase):
    """match_agent must wrap resume + JD content in <scraped_content> tags."""

    def setUp(self):
        self.pool = _make_pool_capturing('{"items": [{"index": 0, "score": 50}]}')
        match_mod._KEY_POOL = self.pool

    def tearDown(self):
        match_mod._KEY_POOL = None

    def _captured_contents(self) -> str:
        kwargs = self.pool.generate_content.call_args.kwargs
        return kwargs["contents"]

    def test_batch_coarse_score_wraps_resume_and_jds(self):
        match_mod.batch_coarse_score(
            "RESUME_BODY",
            [{"jd_json": '{"job_title": "AI TPM", "company": "X"}'}],
        )
        contents = self._captured_contents()
        self.assertIn("<scraped_content>", contents)
        self.assertIn("</scraped_content>", contents)
        self.assertIn("RESUME_BODY", contents)
        # Each scraped block must be opened/closed at least twice (resume + JDs)
        self.assertGreaterEqual(contents.count("<scraped_content>"), 2)
        self.assertGreaterEqual(contents.count("</scraped_content>"), 2)

    def test_evaluate_match_wraps_resume_and_jd(self):
        match_mod.evaluate_match("RESUME_BODY", '{"job_title": "AI TPM"}')
        contents = self._captured_contents()
        self.assertIn("<scraped_content>", contents)
        self.assertIn("</scraped_content>", contents)
        self.assertGreaterEqual(contents.count("<scraped_content>"), 2)


class TestResumeOptimizerWrapsScrapedContent(unittest.TestCase):
    """resume_optimizer must wrap inputs in tailor / re_score / batch_*."""

    def setUp(self):
        self.pool = _make_pool_capturing(
            '{"tailored_resume_markdown": "...", "optimization_summary": "..."}'
        )
        opt_mod._KEY_POOL = self.pool

    def tearDown(self):
        opt_mod._KEY_POOL = None

    def _captured_contents(self) -> str:
        return self.pool.generate_content.call_args.kwargs["contents"]

    def test_tailor_resume_wraps_resume_and_jd(self):
        opt_mod.tailor_resume("RESUME_BODY", "JD_BODY")
        contents = self._captured_contents()
        self.assertGreaterEqual(contents.count("<scraped_content>"), 2)
        self.assertGreaterEqual(contents.count("</scraped_content>"), 2)
        self.assertIn("RESUME_BODY", contents)
        self.assertIn("JD_BODY", contents)

    def test_re_score_wraps_tailored_resume_and_jd(self):
        self.pool.generate_content.return_value.text = '{"compatibility_score": 70, "key_strengths": [], "critical_gaps": [], "recommendation_reason": ""}'
        opt_mod.re_score("TAILORED_RESUME", "JD_BODY")
        contents = self._captured_contents()
        self.assertGreaterEqual(contents.count("<scraped_content>"), 2)

    def test_batch_re_score_wraps_each_pair(self):
        self.pool.generate_content.return_value.text = '{"items": []}'
        opt_mod.batch_re_score([
            {"tailored_resume": "R1", "jd_content": "J1"},
            {"tailored_resume": "R2", "jd_content": "J2"},
        ])
        contents = self._captured_contents()
        # 2 pairs × 2 wraps each = at least 4 opens
        self.assertGreaterEqual(contents.count("<scraped_content>"), 4)

    def test_batch_tailor_resume_wraps_resume_and_jds(self):
        self.pool.generate_content.return_value.text = '{"items": []}'
        opt_mod.batch_tailor_resume("R", ["JD0", "JD1"])
        contents = self._captured_contents()
        self.assertGreaterEqual(contents.count("<scraped_content>"), 2)


class TestJobAgentWrapsScrapedContent(unittest.TestCase):
    """job_agent.extract_jd and llm_filter_jobs must wrap external content."""

    def setUp(self):
        self.pool = _make_pool_capturing('{}')
        job_mod._KEY_POOL = self.pool

    def tearDown(self):
        job_mod._KEY_POOL = None

    def _captured_contents(self) -> str:
        return self.pool.generate_content.call_args.kwargs["contents"]

    def _captured_system_instruction(self) -> str:
        cfg = self.pool.generate_content.call_args.kwargs["config"]
        # cfg is a real GenerateContentConfig (or MagicMock when stubbed) — extract
        # via the kwargs that were passed in
        return getattr(cfg, "system_instruction", "") or ""

    def test_extract_jd_wraps_markdown(self):
        job_mod.extract_jd("MALICIOUS_MARKDOWN_BODY",
                           company="Acme", ai_domain="Big Tech (AI Investment)")
        contents = self._captured_contents()
        self.assertIn("<scraped_content>", contents)
        self.assertIn("</scraped_content>", contents)
        self.assertIn("MALICIOUS_MARKDOWN_BODY", contents)

    def test_extract_jd_appends_security_clause_to_system_instruction(self):
        # We can't reliably read system_instruction from MagicMock'd
        # GenerateContentConfig, so verify via source-level guard instead.
        import inspect
        src = inspect.getsource(job_mod.extract_jd)
        self.assertIn("SECURITY_CLAUSE", src,
                      "extract_jd must reference SECURITY_CLAUSE in its system instruction build")

    def test_llm_filter_jobs_wraps_links(self):
        job_mod.llm_filter_jobs("Acme", ["https://acme.io/job/1?evil=ignore_all"])
        contents = self._captured_contents()
        self.assertIn("<scraped_content>", contents)
        self.assertIn("</scraped_content>", contents)


if __name__ == "__main__":
    unittest.main()
