"""
Single-source-of-truth enforcement for shared prompts and schemas.

REQ-052 requires the fine-evaluation prompt and the MatchResult schema used
for the original score (match_agent) and the tailored re-score (resume_optimizer)
to be byte-identical so deltas are comparable. After the P0-2 refactor both
agents must reference the SAME object from shared/. These tests assert that
identity (via 'is'), not just equality.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── Stub heavy deps before import ────────────────────────────────────────────
for mod in ["google", "google.genai", "google.genai.types", "dotenv"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.modules["dotenv"].load_dotenv = lambda: None

import google
google.genai = MagicMock()
google.genai.types = MagicMock()

import shared.prompts as prompts
import shared.schemas as schemas
import agents.match_agent as match_mod
import agents.resume_optimizer as optimizer_mod


class TestPromptSingleSource(unittest.TestCase):
    """Both agents must refer to the SAME prompt object (is, not ==)."""

    def test_fine_prompt_is_shared_singleton(self):
        # Whatever match_agent's module-level reference to FINE_SYSTEM_PROMPT
        # resolves to, it must be the shared.prompts.FINE_SYSTEM_PROMPT object.
        self.assertIs(match_mod.FINE_SYSTEM_PROMPT, prompts.FINE_SYSTEM_PROMPT)
        self.assertIs(optimizer_mod.FINE_SYSTEM_PROMPT, prompts.FINE_SYSTEM_PROMPT)

    def test_match_and_optimizer_share_same_fine_prompt_object(self):
        self.assertIs(match_mod.FINE_SYSTEM_PROMPT, optimizer_mod.FINE_SYSTEM_PROMPT)

    def test_coarse_prompt_lives_in_shared(self):
        self.assertIs(match_mod.COARSE_SYSTEM_PROMPT, prompts.COARSE_SYSTEM_PROMPT)

    def test_tailor_prompt_lives_in_shared(self):
        self.assertIs(optimizer_mod.TAILOR_SYSTEM_PROMPT, prompts.TAILOR_SYSTEM_PROMPT)


class TestSchemaSingleSource(unittest.TestCase):
    """MatchResult and other schemas must reference the SAME class object."""

    def test_match_result_is_shared_singleton(self):
        self.assertIs(match_mod.MatchResult, schemas.MatchResult)
        self.assertIs(optimizer_mod.MatchResult, schemas.MatchResult)

    def test_match_and_optimizer_share_same_match_result_class(self):
        self.assertIs(match_mod.MatchResult, optimizer_mod.MatchResult)

    def test_coarse_schemas_live_in_shared(self):
        self.assertIs(match_mod.CoarseItem, schemas.CoarseItem)
        self.assertIs(match_mod.BatchCoarseResult, schemas.BatchCoarseResult)

    def test_optimizer_batch_schemas_live_in_shared(self):
        self.assertIs(optimizer_mod.TailoredResume, schemas.TailoredResume)
        self.assertIs(optimizer_mod.BatchTailoredResult, schemas.BatchTailoredResult)


class TestNoLocalRedefinition(unittest.TestCase):
    """Source-level guard: agents must not redefine the symbols locally.
    Catches accidental shadowing where someone re-declares a constant locally."""

    def test_match_agent_does_not_define_local_fine_prompt(self):
        import inspect
        src = inspect.getsource(match_mod)
        # No local assignment patterns should exist for the moved symbols.
        for forbidden in ("_FINE_SYSTEM_PROMPT =", "_COARSE_SYSTEM_PROMPT =",
                          "FINE_SYSTEM_PROMPT =", "COARSE_SYSTEM_PROMPT ="):
            self.assertNotIn(forbidden, src,
                             f"match_agent.py must not locally redefine: {forbidden}")

    def test_resume_optimizer_does_not_define_local_fine_prompt(self):
        import inspect
        src = inspect.getsource(optimizer_mod)
        for forbidden in ("_FINE_SYSTEM_PROMPT =", "_TAILOR_SYSTEM_PROMPT =",
                          "_BATCH_TAILOR_SYSTEM_PROMPT =",
                          "FINE_SYSTEM_PROMPT =", "TAILOR_SYSTEM_PROMPT ="):
            self.assertNotIn(forbidden, src,
                             f"resume_optimizer.py must not locally redefine: {forbidden}")

    def test_match_agent_does_not_define_local_match_result_class(self):
        import inspect
        src = inspect.getsource(match_mod)
        self.assertNotIn("class MatchResult", src,
                         "match_agent.py must not redefine MatchResult locally")

    def test_resume_optimizer_does_not_define_local_match_result_class(self):
        import inspect
        src = inspect.getsource(optimizer_mod)
        self.assertNotIn("class MatchResult", src,
                         "resume_optimizer.py must not redefine MatchResult locally")


class TestPRJ002RenamedPrompts(unittest.TestCase):
    """PRJ-002 PR 2 — prompt rename pivot.

    RECRUITER_SYSTEM_PROMPT replaces COARSE_SYSTEM_PROMPT (Stage 1 / recruiter).
    HM_SYSTEM_PROMPT replaces FINE_SYSTEM_PROMPT (Stage 2 / hiring manager).
    Back-compat aliases must point to the SAME string object (is, not ==) so
    callers using either name see byte-identical content.
    """

    def test_recruiter_prompt_exists(self):
        self.assertTrue(hasattr(prompts, "RECRUITER_SYSTEM_PROMPT"))
        self.assertIsInstance(prompts.RECRUITER_SYSTEM_PROMPT, str)
        self.assertGreater(len(prompts.RECRUITER_SYSTEM_PROMPT), 100)

    def test_hm_prompt_exists(self):
        self.assertTrue(hasattr(prompts, "HM_SYSTEM_PROMPT"))
        self.assertIsInstance(prompts.HM_SYSTEM_PROMPT, str)
        self.assertGreater(len(prompts.HM_SYSTEM_PROMPT), 100)

    def test_coarse_alias_is_recruiter(self):
        # Identity check, not equality — they must be the SAME object so
        # 'is' comparisons elsewhere still hold and there's no string-table dup.
        self.assertIs(prompts.COARSE_SYSTEM_PROMPT, prompts.RECRUITER_SYSTEM_PROMPT)

    def test_fine_alias_is_hm(self):
        self.assertIs(prompts.FINE_SYSTEM_PROMPT, prompts.HM_SYSTEM_PROMPT)

    def test_pure_rename_no_content_drift(self):
        # PR 2 promises byte-identical content across the rename so existing
        # scores remain comparable. The recruiter prompt MUST still describe
        # the rapid-screener persona; the HM prompt MUST still mention the
        # 4 weighted criteria. Catches accidental rewrites in this PR.
        self.assertIn("rapid job-fit screener", prompts.RECRUITER_SYSTEM_PROMPT)
        self.assertIn("Score using 4 weighted criteria", prompts.HM_SYSTEM_PROMPT)
        self.assertIn("AI/ML Tech Depth (30%)", prompts.HM_SYSTEM_PROMPT)

    def test_security_clause_on_both_renamed_prompts(self):
        # P0-3 prompt-injection guard must still wrap the renamed prompts.
        self.assertIn("SECURITY:", prompts.RECRUITER_SYSTEM_PROMPT)
        self.assertIn("SECURITY:", prompts.HM_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
