"""Exception taxonomy for Gemini API errors.

P0-4: previously every Gemini exception was caught and replaced with a
fake fallback score (1 or 0), which silently polluted Excel with data
indistinguishable from real low scores.

After classification:
  - GeminiTransientError: 429 / RESOURCE_EXHAUSTED / 5xx / UNAVAILABLE /
    DEADLINE_EXCEEDED / INTERNAL. The pool retries with key rotation for
    quota errors; if exhausted, raises this. Callers should bubble this
    up to main() and let the run fail loudly so the user knows to retry.
  - GeminiStructuralError: JSON parse failure, schema validation failure,
    safety / content filter blocks, malformed response. Callers log and
    return None (or empty list / sentinel for batch APIs); the orchestrator
    drops the affected record(s) without writing fake data.
"""


class GeminiTransientError(Exception):
    """Transient Gemini error — quota, server overload, timeout. Bubble up."""


class GeminiStructuralError(Exception):
    """Non-retriable Gemini error — bad JSON, filtered content, schema mismatch.
    Caller should drop the record, never write a fake score."""
