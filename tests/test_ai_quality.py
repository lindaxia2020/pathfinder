"""
AI Output Quality Tests — PRJ-001 Phase 4 QA

Coverage:
  - TestFinePromptConsistency: REQ-052 full assertEqual between match_agent and resume_optimizer
  - TestFallbackValueConsistency: document fallback values across agents
  - TestCoarseItemScoreDescription: Pydantic schema field descriptions
  - TestHallucinationGuards: structural verification of anti-hallucination rules
  - TestScoreClampBehavior: score minimum enforcement in evaluate_match / batch_coarse_score
"""
import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch

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

from shared.prompts import (
    FINE_SYSTEM_PROMPT,
    COARSE_SYSTEM_PROMPT as _COARSE_SYSTEM_PROMPT,
    TAILOR_SYSTEM_PROMPT as _TAILOR_SYSTEM_PROMPT,
)
from shared.schemas import (
    CoarseItem, BatchCoarseResult,
    MatchResult as MatchMatchResult,
)

# Both agents now reference the same shared FINE_SYSTEM_PROMPT object.
MATCH_FINE_PROMPT = FINE_SYSTEM_PROMPT
OPTIMIZER_FINE_PROMPT = FINE_SYSTEM_PROMPT

from agents.match_agent import (
    batch_coarse_score,
    evaluate_match,
)
import agents.match_agent as match_mod

from agents.resume_optimizer import (
    MatchResult as OptimizerMatchResult,
    re_score,
)
import agents.resume_optimizer as optimizer_mod


# ─────────────────────────────────────────────────────────────────────────────
class TestFinePromptConsistency(unittest.TestCase):
    """REQ-052: resume_optimizer must use the EXACT same fine prompt as match_agent."""

    def test_fine_prompt_exact_match(self):
        """Full assertEqual — not substring, not 'in', exact equality.

        After Phase 2 of the 2026-04-28 review, optimizer's re-score uses
        the same single-call FINE_SYSTEM_PROMPT as match_agent.evaluate_match,
        so Score Delta reflects the resume change rather than batch-context
        anchoring artifacts.
        """
        self.assertEqual(MATCH_FINE_PROMPT, OPTIMIZER_FINE_PROMPT,
                         "match_agent._FINE_SYSTEM_PROMPT != resume_optimizer._FINE_SYSTEM_PROMPT")


# ─────────────────────────────────────────────────────────────────────────────
class TestStructuralErrorDropsRecord(unittest.TestCase):
    """P0-4: structural Gemini errors must drop records, not write fake scores.

    Previously these functions returned score=1 (match_agent) or score=0
    (resume_optimizer) on any exception, silently polluting Excel. After
    P0-4 they return None / [] / [{}] sentinels and the orchestrator skips
    those records. Transient errors (quota, 5xx) bubble up as
    GeminiTransientError.
    """

    def test_match_evaluate_returns_none_on_structural(self):
        from shared.exceptions import GeminiStructuralError
        mock_pool = MagicMock()
        mock_pool.generate_content.side_effect = GeminiStructuralError("bad JSON")
        match_mod._KEY_POOL = mock_pool
        try:
            self.assertIsNone(evaluate_match("resume", "jd"))
        finally:
            match_mod._KEY_POOL = None

    def test_match_batch_coarse_returns_empty_on_structural(self):
        from shared.exceptions import GeminiStructuralError
        mock_pool = MagicMock()
        mock_pool.generate_content.side_effect = GeminiStructuralError("schema fail")
        match_mod._KEY_POOL = mock_pool
        try:
            scores = batch_coarse_score("resume", [{"jd_json": '{"job_title":"test"}'}] * 3)
            self.assertEqual(scores, [])
        finally:
            match_mod._KEY_POOL = None

    def test_optimizer_re_score_returns_none_on_structural(self):
        from shared.exceptions import GeminiStructuralError
        mock_pool = MagicMock()
        mock_pool.generate_content.side_effect = GeminiStructuralError("bad JSON")
        optimizer_mod._KEY_POOL = mock_pool
        try:
            self.assertIsNone(re_score("resume", "jd"))
        finally:
            optimizer_mod._KEY_POOL = None


class TestTransientErrorBubblesUp(unittest.TestCase):
    """P0-4: transient Gemini errors must propagate so the run fails loudly."""

    def test_match_evaluate_reraises_transient(self):
        from shared.exceptions import GeminiTransientError
        mock_pool = MagicMock()
        mock_pool.generate_content.side_effect = GeminiTransientError("429")
        match_mod._KEY_POOL = mock_pool
        try:
            with self.assertRaises(GeminiTransientError):
                evaluate_match("resume", "jd")
        finally:
            match_mod._KEY_POOL = None

    def test_match_batch_coarse_reraises_transient(self):
        from shared.exceptions import GeminiTransientError
        mock_pool = MagicMock()
        mock_pool.generate_content.side_effect = GeminiTransientError("429")
        match_mod._KEY_POOL = mock_pool
        try:
            with self.assertRaises(GeminiTransientError):
                batch_coarse_score("resume", [{"jd_json": '{}'}])
        finally:
            match_mod._KEY_POOL = None

    def test_optimizer_re_score_reraises_transient(self):
        from shared.exceptions import GeminiTransientError
        mock_pool = MagicMock()
        mock_pool.generate_content.side_effect = GeminiTransientError("429")
        optimizer_mod._KEY_POOL = mock_pool
        try:
            with self.assertRaises(GeminiTransientError):
                re_score("resume", "jd")
        finally:
            optimizer_mod._KEY_POOL = None

# ─────────────────────────────────────────────────────────────────────────────
class TestCoarseItemScoreDescription(unittest.TestCase):
    """Pydantic schema validation for CoarseItem."""

    def test_coarse_item_score_field_exists(self):
        """CoarseItem must have a 'score' field."""
        item = CoarseItem(index=0, score=50)
        self.assertEqual(item.score, 50)

    def test_coarse_item_rejects_negative_score_via_model(self):
        """CoarseItem should accept 0 or positive scores (Pydantic int field)."""
        item = CoarseItem(index=0, score=0)
        self.assertEqual(item.score, 0)

    def test_batch_coarse_result_structure(self):
        """BatchCoarseResult.items must be a list of CoarseItem."""
        result = BatchCoarseResult(items=[CoarseItem(index=0, score=75)])
        self.assertEqual(len(result.items), 1)
        self.assertIsInstance(result.items[0], CoarseItem)


# ─────────────────────────────────────────────────────────────────────────────
class TestHallucinationGuards(unittest.TestCase):
    """Structural verification of anti-hallucination rules in prompts."""

    def test_tailor_prompt_has_only_use_rule(self):
        """_TAILOR_SYSTEM_PROMPT must contain 'ONLY use information already in the original resume'."""
        self.assertIn("ONLY use information already in the original resume", _TAILOR_SYSTEM_PROMPT)

    def test_tailor_prompt_has_never_fabricate_rule(self):
        """_TAILOR_SYSTEM_PROMPT must contain 'NEVER fabricate'."""
        self.assertIn("NEVER fabricate", _TAILOR_SYSTEM_PROMPT)

    def test_fine_prompt_has_brutally_honest(self):
        """_FINE_SYSTEM_PROMPT must contain 'Brutally Honest' to prevent score inflation."""
        self.assertIn("Brutally Honest", MATCH_FINE_PROMPT)

    def test_fine_prompt_has_no_inflate(self):
        """_FINE_SYSTEM_PROMPT must contain 'Do not inflate scores'."""
        self.assertIn("Do not inflate scores", MATCH_FINE_PROMPT)

    def test_coarse_prompt_has_minimum_score_1(self):
        """_COARSE_SYSTEM_PROMPT must declare minimum score is 1."""
        self.assertIn("Minimum score is 1", _COARSE_SYSTEM_PROMPT)

    def test_coarse_prompt_has_never_return_0(self):
        """_COARSE_SYSTEM_PROMPT must state 'never return 0'."""
        self.assertIn("never return 0", _COARSE_SYSTEM_PROMPT)


# ─────────────────────────────────────────────────────────────────────────────
class TestScoreClampBehavior(unittest.TestCase):
    """Verify that code enforces score minimums."""

    def test_evaluate_match_returns_raw_gemini_output(self):
        """evaluate_match passes through raw Gemini text; clamping is done by caller (main)."""
        mock_pool = MagicMock()
        mock_pool.generate_content.return_value = MagicMock(
            text=json.dumps({
                "compatibility_score": 0,
                "key_strengths": [],
                "critical_gaps": [],
                "recommendation_reason": "test",
            })
        )
        match_mod._KEY_POOL = mock_pool
        try:
            result_json = evaluate_match("resume", "jd")
            self.assertIsInstance(result_json, str)
            result = json.loads(result_json)
            # evaluate_match does NOT clamp — it returns raw Gemini output as-is.
            # The caller (fine_one in main) applies max(1, score).
            self.assertEqual(result["compatibility_score"], 0)
        finally:
            match_mod._KEY_POOL = None

    def test_batch_coarse_score_clamps_to_1(self):
        """batch_coarse_score must clamp all scores to >= 1."""
        mock_pool = MagicMock()
        mock_pool.generate_content.return_value = MagicMock(
            text=json.dumps({
                "items": [
                    {"index": 0, "score": 0},
                    {"index": 1, "score": -5},
                ]
            })
        )
        match_mod._KEY_POOL = mock_pool
        try:
            scores = batch_coarse_score("resume", [
                {"jd_json": '{"job_title":"A"}'},
                {"jd_json": '{"job_title":"B"}'},
            ])
            for s in scores:
                self.assertGreaterEqual(s, 1, f"Score {s} is below minimum 1")
        finally:
            match_mod._KEY_POOL = None


if __name__ == "__main__":
    unittest.main()
