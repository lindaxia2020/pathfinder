"""
P0-4: _do_generate must classify Gemini failures as transient or structural.

Transient (GeminiTransientError) bubbles up to main() so the run fails
loudly. Structural (GeminiStructuralError) signals "this record is bad,
drop it" — the orchestrator does not write a fake score in its place.
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

from shared.gemini_pool import _GeminiKeyPoolBase, _is_transient
from shared.exceptions import GeminiTransientError, GeminiStructuralError


class TestIsTransientHeuristic(unittest.TestCase):
    """The string-based classifier should recognize common transient patterns."""

    def test_429_is_transient(self):
        self.assertTrue(_is_transient("Got HTTP 429 from server"))

    def test_resource_exhausted_is_transient(self):
        self.assertTrue(_is_transient("RESOURCE_EXHAUSTED: quota"))

    def test_503_is_transient(self):
        self.assertTrue(_is_transient("503 Service Unavailable"))

    def test_500_is_transient(self):
        self.assertTrue(_is_transient("Internal server error 500"))

    def test_deadline_exceeded_is_transient(self):
        self.assertTrue(_is_transient("DEADLINE_EXCEEDED"))

    def test_timeout_is_transient(self):
        self.assertTrue(_is_transient("Request timeout"))

    def test_quota_keyword_is_transient(self):
        self.assertTrue(_is_transient("Daily quota exhausted"))

    def test_json_parse_is_not_transient(self):
        self.assertFalse(_is_transient("JSONDecodeError: Expecting value"))

    def test_validation_error_is_not_transient(self):
        self.assertFalse(_is_transient("ValidationError: schema mismatch"))

    def test_safety_block_is_not_transient(self):
        self.assertFalse(_is_transient("Response blocked by safety filter"))


class TestPoolClassifiesTransient(unittest.TestCase):
    """When SDK raises a transient-pattern error, _do_generate must rotate
    keys (for 429) and ultimately raise GeminiTransientError."""

    def _build_pool_with_failing_sdk(self, sdk_error: Exception, n_keys: int = 1):
        # Build a fake genai module whose .Client(...).models.generate_content
        # always raises sdk_error.
        client = MagicMock()
        client.models.generate_content.side_effect = sdk_error
        genai_mod = MagicMock()
        genai_mod.Client.return_value = client
        keys = [f"k{i}" for i in range(n_keys)]
        return _GeminiKeyPoolBase(keys, genai_mod=genai_mod), client

    def test_429_with_single_key_raises_transient(self):
        pool, _ = self._build_pool_with_failing_sdk(Exception("429 RESOURCE_EXHAUSTED"))
        with self.assertRaises(GeminiTransientError):
            pool.generate_content(model="m", contents="c", config=None)

    def test_429_with_multiple_keys_rotates_then_raises_transient(self):
        pool, client = self._build_pool_with_failing_sdk(
            Exception("429 RESOURCE_EXHAUSTED"), n_keys=2)
        with self.assertRaises(GeminiTransientError):
            pool.generate_content(model="m", contents="c", config=None)
        # Should have tried at least both keys before giving up.
        self.assertGreaterEqual(client.models.generate_content.call_count, 2)

    def test_503_raises_transient_no_rotation(self):
        pool, client = self._build_pool_with_failing_sdk(
            Exception("503 Service Unavailable"))
        with self.assertRaises(GeminiTransientError):
            pool.generate_content(model="m", contents="c", config=None)
        # 5xx is not key-specific, so no retry-rotation.
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_deadline_exceeded_raises_transient(self):
        pool, _ = self._build_pool_with_failing_sdk(Exception("DEADLINE_EXCEEDED on request"))
        with self.assertRaises(GeminiTransientError):
            pool.generate_content(model="m", contents="c", config=None)


class TestPoolClassifiesStructural(unittest.TestCase):
    """Non-transient errors must raise GeminiStructuralError."""

    def _build_pool_with_failing_sdk(self, sdk_error: Exception):
        client = MagicMock()
        client.models.generate_content.side_effect = sdk_error
        genai_mod = MagicMock()
        genai_mod.Client.return_value = client
        return _GeminiKeyPoolBase(["k0"], genai_mod=genai_mod)

    def test_json_parse_error_raises_structural(self):
        pool = self._build_pool_with_failing_sdk(ValueError("Expecting value"))
        with self.assertRaises(GeminiStructuralError):
            pool.generate_content(model="m", contents="c", config=None)

    def test_safety_block_raises_structural(self):
        pool = self._build_pool_with_failing_sdk(
            Exception("Response blocked by content filter"))
        with self.assertRaises(GeminiStructuralError):
            pool.generate_content(model="m", contents="c", config=None)

    def test_generic_error_raises_structural(self):
        pool = self._build_pool_with_failing_sdk(RuntimeError("API error"))
        with self.assertRaises(GeminiStructuralError):
            pool.generate_content(model="m", contents="c", config=None)


class TestPoolPropagatesTypedExceptions(unittest.TestCase):
    """The typed exceptions must wrap the original error via __cause__ so the
    underlying SDK message is still observable."""

    def test_transient_wraps_original(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("503 oh no")
        genai_mod = MagicMock()
        genai_mod.Client.return_value = client
        pool = _GeminiKeyPoolBase(["k0"], genai_mod=genai_mod)
        try:
            pool.generate_content(model="m", contents="c", config=None)
            self.fail("expected GeminiTransientError")
        except GeminiTransientError as e:
            self.assertIsNotNone(e.__cause__)
            self.assertIn("503", str(e.__cause__))

    def test_structural_wraps_original(self):
        client = MagicMock()
        client.models.generate_content.side_effect = ValueError("bad json")
        genai_mod = MagicMock()
        genai_mod.Client.return_value = client
        pool = _GeminiKeyPoolBase(["k0"], genai_mod=genai_mod)
        try:
            pool.generate_content(model="m", contents="c", config=None)
            self.fail("expected GeminiStructuralError")
        except GeminiStructuralError as e:
            self.assertIsNotNone(e.__cause__)


if __name__ == "__main__":
    unittest.main()
