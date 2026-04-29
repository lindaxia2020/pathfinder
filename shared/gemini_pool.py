"""Shared Gemini API key pool with auto-rotation on quota exhaustion."""
import logging
import random
import threading
import time

from shared.exceptions import GeminiTransientError, GeminiStructuralError

# Backoff for non-quota transient errors (5xx / UNAVAILABLE / timeout). The
# Gemini API explicitly tells callers to retry on 503; a single overload spike
# would otherwise abort the whole run. Quota/429 stays on the key-rotation
# path — it's key-specific, not server-wide, so backoff there is wasted.
_TRANSIENT_RETRY_BACKOFFS = (2.0, 4.0, 8.0)  # seconds; jitter added at use site


# Substrings (case-insensitive) that mark a transient (server / quota) error
# worth distinguishing from a structural one. Anything else falls through to
# GeminiStructuralError so the caller drops the record instead of writing a
# fake fallback score.
_TRANSIENT_PATTERNS = (
    "429",
    "resource_exhausted",
    "rate limit",
    "quota",
    "503",
    "unavailable",
    "service_unavailable",
    "504",
    "deadline_exceeded",
    "timeout",
    "500",
    "internal_error",
    "internal server",
)


def _is_transient(err_text: str) -> bool:
    low = err_text.lower()
    return any(p in low for p in _TRANSIENT_PATTERNS)


class _GeminiKeyPoolBase:
    """Holds multiple Gemini API keys and rotates to the next on quota exhaustion.

    Can be used directly by passing ``genai_mod`` (the ``google.genai`` module)
    to the constructor, or subclassed with an overridden ``generate_content``.
    """
    def __init__(self, keys: list[str], genai_mod=None):
        self._keys = [k for k in keys if k]
        if not self._keys:
            raise ValueError("[KeyPool] No valid Gemini API keys provided.")
        self._idx = 0
        self._genai_mod = genai_mod  # BUG-31: store genai module for direct use
        self._clients: dict[str, object] = {}  # BUG-34: cache Client per key
        self._lock = threading.Lock()  # BUG-36: thread-safe rotation

    @property
    def current(self) -> str:
        return self._keys[self._idx]

    def rotate(self) -> bool:
        """Round-robin to the next key. Returns False only if pool has a single key."""
        if len(self._keys) <= 1:
            return False
        self._idx = (self._idx + 1) % len(self._keys)
        logging.warning(f"[KeyPool] Switched to API key #{self._idx + 1}")
        return True

    def _get_client(self, genai_mod) -> object:
        """Return a cached Client for the current key, creating one if needed."""
        key = self.current
        if key not in self._clients:
            self._clients[key] = genai_mod.Client(api_key=key)
        return self._clients[key]

    def _do_generate(self, model: str, contents, config, genai_mod) -> object:
        """Core generate_content logic. ``genai_mod`` is the google.genai module.

        Classifies failures (P0-4):
          * Quota / 429 → rotate keys; raise GeminiTransientError if all exhausted.
          * Other transient (5xx, timeout) → bounded exponential backoff retry,
            then raise GeminiTransientError if still failing.
          * Anything else (JSON parse, content filter, schema) → raise
            GeminiStructuralError so the caller drops the record.
        """
        tried_count = 0
        transient_attempt = 0
        while True:
            with self._lock:
                gc = self._get_client(genai_mod)
            try:
                return gc.models.generate_content(model=model, contents=contents, config=config)
            except Exception as e:
                err = str(e)
                # Quota-specific path: rotate keys, then bubble up as transient.
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    with self._lock:
                        logging.warning(f"[KeyPool] Key #{self._idx + 1} quota exhausted.")
                        tried_count += 1
                        if not self.rotate() or tried_count >= len(self._keys):
                            logging.error("[KeyPool] All Gemini API keys exhausted.")
                            raise GeminiTransientError(f"All Gemini keys quota exhausted: {err}") from e
                    continue
                # Server-side transient (5xx / UNAVAILABLE / timeout): retry on
                # the same key with bounded exponential backoff before failing.
                if _is_transient(err):
                    if transient_attempt < len(_TRANSIENT_RETRY_BACKOFFS):
                        delay = _TRANSIENT_RETRY_BACKOFFS[transient_attempt] + random.uniform(0, 0.5)
                        transient_attempt += 1
                        logging.warning(
                            f"[KeyPool] Transient error (attempt {transient_attempt}/"
                            f"{len(_TRANSIENT_RETRY_BACKOFFS)}); retrying in {delay:.1f}s: {err}"
                        )
                        time.sleep(delay)
                        continue
                    raise GeminiTransientError(err) from e
                # Everything else is structural — caller should drop the record.
                raise GeminiStructuralError(err) from e

    def generate_content(self, model, contents, config):
        """BUG-31: unified generate_content using stored genai module."""
        if self._genai_mod is None:
            raise RuntimeError(
                "genai_mod not provided — pass genai module to constructor "
                "or subclass and override generate_content()"
            )
        return self._do_generate(model, contents, config, self._genai_mod)

    # ── P0-1: Context Caching ────────────────────────────────────────────────
    def create_cache(self, model: str, system_instruction: str,
                     contents: list, ttl: str = "3600s",
                     display_name: str | None = None) -> str | None:
        """Create a Gemini context cache (resume + system prompt) on the
        current key. Returns the cache name (e.g. 'cachedContents/abc123')
        or None if caching is unsupported for this model / content size.

        Failures are intentionally non-fatal so callers fall back to
        uncached generation rather than aborting the run.
        """
        if self._genai_mod is None:
            return None
        try:
            with self._lock:
                gc = self._get_client(self._genai_mod)
            types_mod = getattr(self._genai_mod, "types", None)
            if types_mod is None or not hasattr(types_mod, "CreateCachedContentConfig"):
                return None
            cfg = types_mod.CreateCachedContentConfig(
                contents=contents,
                system_instruction=system_instruction,
                ttl=ttl,
                display_name=display_name,
            )
            cache = gc.caches.create(model=model, config=cfg)
            name = getattr(cache, "name", None)
            if name:
                logging.info(f"[KeyPool] Created cache: {name}")
            return name
        except Exception as e:
            # Common reasons: model doesn't support caching, content too small,
            # quota on caches API. None of these should kill the run.
            logging.warning(f"[KeyPool] Context caching unavailable; falling back: {e}")
            return None

    def delete_cache(self, cache_name: str) -> None:
        """Best-effort cache cleanup. Never raises."""
        if not cache_name or self._genai_mod is None:
            return
        try:
            with self._lock:
                gc = self._get_client(self._genai_mod)
            gc.caches.delete(name=cache_name)
            logging.info(f"[KeyPool] Deleted cache: {cache_name}")
        except Exception as e:
            logging.warning(f"[KeyPool] Cache delete failed for {cache_name}: {e}")
