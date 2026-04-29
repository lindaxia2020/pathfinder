"""Retry behavior for transient Gemini errors in shared.gemini_pool.

Covers the backoff added on top of P0-4 classification:
  - 503 (UNAVAILABLE) followed by success -> returns response, retries logged
  - 503 every attempt -> raises GeminiTransientError after exhausting backoffs
  - 429 (quota) -> takes the key-rotation path, NOT the backoff sleep loop
  - Structural errors -> still raise immediately, no retry
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

for mod in ["google", "google.genai", "google.genai.types", "dotenv"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda: None

import google
google.genai = MagicMock()
google.genai.types = MagicMock()

from shared.exceptions import GeminiTransientError, GeminiStructuralError
from shared.gemini_pool import _GeminiKeyPoolBase, _TRANSIENT_RETRY_BACKOFFS


def _build_pool(side_effects, num_keys=1):
    """Build a pool whose generate_content yields values from side_effects in
    order. Exception instances are raised; everything else is returned."""
    client = MagicMock()
    client.models.generate_content.side_effect = side_effects
    genai_mod = MagicMock()
    genai_mod.Client.return_value = client
    keys = [f"k{i}" for i in range(num_keys)]
    pool = _GeminiKeyPoolBase(keys, genai_mod=genai_mod)
    return pool, client


class TestTransientRetry(unittest.TestCase):

    def test_503_then_success_returns_response(self):
        ok = MagicMock(name="response")
        err = Exception("503 UNAVAILABLE: high demand")
        pool, client = _build_pool([err, ok])
        with patch("shared.gemini_pool.time.sleep") as mock_sleep:
            resp = pool.generate_content(model="m", contents="c", config={})
        self.assertIs(resp, ok)
        self.assertEqual(client.models.generate_content.call_count, 2)
        mock_sleep.assert_called_once()
        # First backoff window is _TRANSIENT_RETRY_BACKOFFS[0] + jitter [0, 0.5)
        slept = mock_sleep.call_args.args[0]
        self.assertGreaterEqual(slept, _TRANSIENT_RETRY_BACKOFFS[0])
        self.assertLess(slept, _TRANSIENT_RETRY_BACKOFFS[0] + 0.5)

    def test_503_every_attempt_raises_transient(self):
        err = Exception("503 UNAVAILABLE")
        # 1 initial call + len(backoffs) retries = total attempts
        side_effects = [err] * (1 + len(_TRANSIENT_RETRY_BACKOFFS))
        pool, client = _build_pool(side_effects)
        with patch("shared.gemini_pool.time.sleep"):
            with self.assertRaises(GeminiTransientError):
                pool.generate_content(model="m", contents="c", config={})
        self.assertEqual(
            client.models.generate_content.call_count,
            1 + len(_TRANSIENT_RETRY_BACKOFFS),
        )

    def test_timeout_also_retries(self):
        ok = MagicMock(name="response")
        err = Exception("DEADLINE_EXCEEDED: timeout")
        pool, client = _build_pool([err, ok])
        with patch("shared.gemini_pool.time.sleep"):
            resp = pool.generate_content(model="m", contents="c", config={})
        self.assertIs(resp, ok)
        self.assertEqual(client.models.generate_content.call_count, 2)


class TestQuotaPathUnchanged(unittest.TestCase):
    """429 must still rotate keys and not engage the backoff sleep loop."""

    def test_429_rotates_keys_no_sleep(self):
        ok = MagicMock(name="response")
        err = Exception("429 RESOURCE_EXHAUSTED")
        pool, client = _build_pool([err, ok], num_keys=2)
        with patch("shared.gemini_pool.time.sleep") as mock_sleep:
            resp = pool.generate_content(model="m", contents="c", config={})
        self.assertIs(resp, ok)
        mock_sleep.assert_not_called()  # quota path doesn't sleep
        self.assertEqual(pool._idx, 1)  # rotated

    def test_429_all_keys_exhausted_raises_transient(self):
        err = Exception("429 RESOURCE_EXHAUSTED")
        pool, client = _build_pool([err, err], num_keys=2)
        with patch("shared.gemini_pool.time.sleep") as mock_sleep:
            with self.assertRaises(GeminiTransientError):
                pool.generate_content(model="m", contents="c", config={})
        mock_sleep.assert_not_called()


class TestStructuralStillRaisesImmediately(unittest.TestCase):

    def test_json_parse_error_no_retry(self):
        err = Exception("Invalid JSON in response")
        pool, client = _build_pool([err])
        with patch("shared.gemini_pool.time.sleep") as mock_sleep:
            with self.assertRaises(GeminiStructuralError):
                pool.generate_content(model="m", contents="c", config={})
        # Exactly one call — no retry on structural errors.
        self.assertEqual(client.models.generate_content.call_count, 1)
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
