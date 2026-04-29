# Bug Tracker

> Last updated: 2026-03-16

## Summary

| Priority | Open | Fixed |
|----------|------|-------|
| P0 Critical | 0 | 5 (BUG-01~03, BUG-32~33) |
| P1 High      | 0 | 15 (BUG-04~11, BUG-27~28, BUG-34~38) |
| P2 Medium    | 0 | 23 (BUG-12~20, BUG-29~30, BUG-39~49, BUG-55) |
| P3 Low       | 0 | 12 (BUG-21~26, BUG-31, BUG-50~54) |
| **Total** | **0** | **55** |

---

## P0 — Critical

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-01 | `.env` | Real API Keys (Gemini x2, Tavily, Firecrawl) stored in plaintext. `.gitignore` already includes `.env` to prevent commits; `.env.example` created as placeholder. Project was never git pushed, keys were not leaked, no rotation needed. | Fixed |
| BUG-02 | `requirements.txt` | Missing core runtime dependencies: `pycountry`, `openpyxl`, `firecrawl-py`, `tavily-python`. Added to requirements.txt (versions 26.2.16 / 3.1.5 / 4.18.1 / 0.7.23). | Fixed |
| BUG-03 | `agents/job_agent.py` `_is_us_segment()` | `_is_us_segment()` changed to only match 2-letter state codes in "City, ST" format (comma prefix) or as standalone segments, eliminating ambiguous false matches for "in/or/de/me/ok". Added 13 regression tests, 52 tests all passed. | Fixed |

---

## P1 — High

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-27 | `shared/excel_store.py` `get_jd_url_meta()` / `agents/job_agent.py` `retry_one()` | `get_jd_url_meta()` only excluded records with invalid company field, causing incomplete records with valid company but missing location/tech/resp to enter `fresh_set`; main loop skipped them, relying only on retry phase. Additionally, `retry_one` would write "None" (`_JD_MISSING` member) when Gemini extraction returned all three fields empty, causing infinite retry loops. Fix: `get_jd_url_meta` now checks location/tech/resp completeness; incomplete records no longer enter `fresh_set`. `retry_one` adds validation to not overwrite old records when extraction results are all empty. 122 tests all passed. | Fixed |
| BUG-28 | `agents/job_agent.py:1425` | retry phase (Retrying incomplete JD records) was located **outside** the `async with AsyncWebCrawler(...) as crawler:` block, causing `crawler.__aexit__` to have already executed and `crawler.browser` to be `None` when `retry_one` internally called `scrape_jd(url, crawler)`, triggering `'NoneType' object has no attribute 'new_context'` error. All retry URLs depending on crawl4ai failed. Fix: Moved the entire retry block (lines 1425-1511) inside the `async with` indentation level, ensuring crawler remains active. 61 tests all passed. | Fixed |
| BUG-04 | `agents/job_agent.py:1163+` | `process_company()` had multiple coroutines writing to `known_url_meta` dictionary concurrently via `asyncio.gather()` without lock protection, potentially causing duplicate URL fetches. Added `seen_urls: set[str] = set()` dedup guard before `fetch_one` closure; asyncio single-thread guarantees check+add atomicity. Added `TestProcessCompanyNoDuplicateFetch` regression test, 53 tests all passed. | Fixed |
| BUG-05 | `agents/company_agent.py:522-536` | `run_phase_1_5` used enumerate index to call `update_company_career_url(xlsx_path, i, ...)`, but `get_company_rows()` skips empty rows, causing enumeration index to misalign with actual Excel row numbers. Career URL could be written to the wrong company row. Fix: Added `get_company_rows_with_row_num()` returning actual Excel row numbers; `update_company_career_url` changed to accept actual row numbers; `run_phase_1_5` uses new function. Added 2 regression tests, 162 tests all passed. | Fixed |
| BUG-06 | `shared/excel_store.py:30-35` | `get_or_create_excel()` called itself recursively during exception recovery; if `shutil.move` also failed, infinite recursion until stack overflow. Fix: Added try-except around `shutil.move`; on failure raises `RuntimeError` to prevent recursive death loop. Added 2 regression tests, 125 tests all passed. | Fixed |
| BUG-07 | `agents/match_agent.py:24-25` | `hashlib` imported twice (`import hashlib` and `import hashlib as _hashlib`), with inconsistent usage between the two. | Fixed |
| BUG-08 | `tests/test_job_agent.py:92-105` | Cache round-trip test only asserted file existence, never asserted that read-back content matched written content, making the test effectively invalid. | Fixed |
| BUG-09 | `tests/test_company_agent.py:116+` | `TestValidateCareerUrl`, `TestCheckAtsSlug`, `TestFindAtsUrl` test classes made real HTTP requests without mock or `@skipUnless` protection, causing random failures in CI/offline environments and consuming real API quota. Replaced with `patch("agents.company_agent.requests.get")` and `patch("agents.company_agent._check_ats_slug")` mocks, 164 tests all passed. | Fixed |
| BUG-10 | `agents/company_agent.py:56-86` / `agents/match_agent.py:105-138` | `_GeminiKeyPool` did not error on empty key list, but first access to `.current` property threw `IndexError: list index out of range`. | Fixed |
| BUG-11 | `CLAUDE.md` | Documentation claimed Python 3.9, but actual venv is Python 3.11 (`venv/lib/python3.11/`). Code heavily uses 3.10+ syntax (`str | None`, `list[str]`, etc.), causing `TypeError` under real 3.9, misleading new contributors. | Fixed |

---

## P2 — Medium

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-12 | `shared/excel_store.py` (entire file) | All `load_workbook()` calls (20 locations) never called `wb.close()`, causing file handle leaks during long-running execution. Fix: Added `try/finally: wb.close()` to all 19 functions; both paths in `get_or_create_excel` (load and create) now have close calls. Added `TestBug12WorkbookClose` (18 test cases) covering all functions including `upsert_match_record` early-return path; 61 tests all passed, no regression. | Fixed |
| BUG-13 | `agents/job_agent.py:203` | `_build_us_index()` called at module level with internal `import pycountry`; if not installed, entire module import fails and even tests cannot run. Fix: Wrapped module-level call in `try/except ImportError`; on failure, builds `_US_STATE_NAMES/_US_STATE_CODES` from embedded `_US_STATES_FALLBACK` (50 states + DC). Added 3 regression tests. | Fixed |
| BUG-14 | `agents/match_agent.py:89` vs `agents/job_agent.py:62` | Two `_RateLimiter` implementations were inconsistent (`match_agent` version creates Lock in `__init__`, `job_agent` version lazy-creates). Tests only covered one version's behavior. Fix: Removed `job_agent.py` lazy-lock logic (Python 3.9 compatibility comment was outdated), unified to eager loading in `__init__`; updated `test_job_agent.py` corresponding tests and added symmetric `test_lock_created_eagerly` test in `test_match_agent.py`. | Fixed |
| BUG-15 | `fix_tesla_records.py:33-36` / `agents/job_agent.py:822` | `Firecrawl.scrape()` did not specify `formats=["markdown"]`; v2 API does not return markdown field by default, causing silent failure and skipped records. Fix: Added `formats=["markdown"]` to both `app.scrape()` calls. Added `TestScrapeTeslaJdUsesFormats` regression test to verify formats parameter. | Fixed |
| BUG-16 | `agents/job_agent.py:530-543` | In `discover_jobs()`, when Workday API call succeeded but `_tpm_filter` returned empty list, it did not fall back to crawler, potentially missing actual positions. Fix: Workday path now triggers `_crawl_page` fallback when `_tpm_filter` returns empty. Added 2 regression tests. | Fixed |
| BUG-17 | `agents/job_agent.py:474-482` | `_firecrawl_map()` retry logic only retried on 429/rate limit; transient errors like network interruptions were immediately abandoned returning empty list — inverted logic. Fix: All errors now retry (3 times); 429 uses 30s x attempt long sleep, other errors use 5s x attempt short sleep; returns [] only after all 3 retries fail. Added 2 regression tests to verify behavior. | Fixed |
| BUG-18 | `.gitignore` | `jd_cache/` directory was not in `.gitignore`; this directory contains URL-MD5-named job description caches that should not be committed to VCS. | Fixed |
| BUG-19 | `CLAUDE.md` | Key Libraries table was missing core dependencies `pycountry`, `openpyxl`, `tavily-python`, violating the "new dependencies must be documented with rationale" convention. | Fixed |
| BUG-20 | `CHANGELOG.md` | Project specification documents `REQUIREMENTS.md` / `ARCHITECTURE.md` / `CHANGELOG.md` were empty or missing, violating project maintenance conventions. All three documents created; CHANGELOG backfilled with missing historical records for BUG-12/18/19/21/25. | Fixed |

---

## P3 — Low

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-21 | `agents/match_agent.py:24-25` | Duplicate `hashlib` import (`import hashlib` + `import hashlib as _hashlib`), code redundancy with no functional impact. | Fixed (cleaned up alongside BUG-07; file now has only one import) |
| BUG-22 | `agents/match_agent.py:431` | `_print_top_results()` hardcoded column number `9` to read Stage column; schema changes would silently read wrong data. Fix: Imported `MATCH_HEADERS`, changed to `MATCH_HEADERS.index("Stage") + 1` dynamic calculation. Added 2 regression tests. | Fixed |
| BUG-23 | `agents/company_agent.py:127-133` | `KNOWN_ATS_OVERRIDES` used substring matching (`in`) instead of exact matching, potentially false-matching other company names containing the same word. Fix: Changed to word-boundary regex matching (`(?:^|\s)kw(?:\s|$)`); "xai" no longer matches "MaxAI" etc. Added 4 regression tests. | Fixed |
| BUG-24 | `tests/test_company_agent.py` | Live network tests had documentation comments stating network required, but lacked `@unittest.skipUnless(os.getenv("INTEGRATION_TEST"), ...)` or similar mechanism to prevent accidental execution in CI. Fix: Updated module docstring to accurately state all HTTP calls are mocked, and added integration test `INTEGRATION_TEST=1` instructions and `skipUnless` usage guidance. | Fixed |
| BUG-25 | `agents/company_agent.py:528-531` | `already_ats` counter variable only used in print; dead code, cleaned up. | Fixed |
| BUG-26 | `tests/test_job_agent.py:40-43` | `pycountry` mock only returned 1 state (California); `_is_us_segment` related tests ran against an extremely reduced dataset with significant behavioral differences from real 50-state data. Fix: Replaced with complete mock of all 50 states + DC; BUG-03 regression tests in TestIsUsSegment now genuinely validate the fix logic for IN/OR/DE/ME/OK state codes. | Fixed |

---

## New Findings (2026-03-16 Audit)

### P2 — Medium

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-29 | `agents/job_agent.py:119-123` | `_GeminiKeyPool.__init__` lacked empty key list protection. Now protected via inheriting `_GeminiKeyPoolBase` (`shared/gemini_pool.py:13-14`) which includes `if not self._keys: raise ValueError(...)` guard. | Fixed (via inheriting `_GeminiKeyPoolBase`) |
| BUG-30 | `agents/match_agent.py:431+` | `_print_top_results()` was missing `wb.close()`. Now has `try/finally: wb.close()` structure. | Fixed |

### P3 — Low

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-31 | `agents/*.py` | `_GeminiKeyPool` subclass was duplicated across 4 agents. Fix: `_GeminiKeyPoolBase.__init__` added `genai_mod` parameter and provides `generate_content` method; subclass definitions in 4 agents changed to `_GeminiKeyPool = _GeminiKeyPoolBase` alias; `main()` directly uses `_GeminiKeyPoolBase(keys, genai_mod=genai)`. Added `TestBug31NoSubclassNeeded` (4 test cases), 485 tests all passed. | Fixed |

---

## Full Code Audit (2026-03-16)

> Parallel review by 6 development-layer agents: agent-reviewer, api-debugger, schema-validator, test-analyzer, doc-sync, bug-tracker

### P0 — Critical

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-32 | `agents/job_agent.py:957` | `_scrape_tesla_jd` imported non-existent `Firecrawl` class (`from firecrawl import Firecrawl`); correct class name is `FirecrawlApp` (used correctly at lines 591, 769 in same file). Tesla JD scraping path always triggered `ImportError`, fell back to HTTP request which was blocked by Akamai 403; all Tesla JD extraction completely failed. Fix: Changed to `from firecrawl import FirecrawlApp` and `FirecrawlApp(api_key=fc_key)`, consistent with other two locations in the file. Added `TestBug32TeslaFirecrawlImport` (2 test cases) + updated `TestScrapeTeslaJdUsesFormats` mock, 162 tests all passed. | Fixed |
| BUG-33 | `shared/excel_store.py:429` | `d.get("requirements", [])` returns `None` (not default `[]`) when Gemini returns `{"requirements": null}`, causing subsequent `"\n".join(f"* {x}" for x in None)` to throw `TypeError`. `additional_qualifications` (line 430) and `key_responsibilities` (line 431) had the same issue. Fix: All three changed to `(d.get(...) or [])` pattern, ensuring `null` values fall back to empty list. Added `TestBug33NullListFields` (5 test cases), 107 excel_store tests all passed. | Fixed |

### P1 — High

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-34 | `shared/gemini_pool.py:32` | Each `_do_generate` call created a new `genai.Client(api_key=...)` instance without reusing the underlying HTTP connection pool. During job_agent batch processing (hundreds of Gemini calls), this significantly increased TCP/TLS handshake latency. Fix: Added `_clients` dictionary to cache Client instances per key; `_do_generate` changed to obtain cached instance via `_get_client()`. Added `TestBug34ClientCaching` (3 test cases), 234 related tests all passed. | Fixed |
| BUG-35 | `shared/gemini_pool.py:21-27` | `rotate()` incremented `_idx` one-way; after first key got 429, it never wrapped back. Even if key #1's quota recovered seconds later, all subsequent calls still only used key #2. After first 429 in long-running agent, permanently lost half of quota capacity. Fix: `rotate()` changed to `(self._idx + 1) % len(self._keys)` round-robin; `_do_generate` tracks `tried_count` to prevent infinite loops. Added `TestBug35RoundRobinRotation` (5 test cases), 239 related tests all passed. | Fixed |
| BUG-36 | `shared/gemini_pool.py` | `_idx` and `rotate()` had no lock protection. `match_agent` and `resume_optimizer` call `generate_content` concurrently in thread pool via `asyncio.to_thread`; two threads hitting 429 simultaneously and calling `rotate()` could increment `_idx` twice, skipping a key. Fix: `__init__` creates `threading.Lock`; `_do_generate` executes `_get_client()` and `rotate()` under `with self._lock` protection. Added `TestBug36ThreadSafety` (3 test cases), 242 related tests all passed. | Fixed |
| BUG-37 | `agents/job_agent.py:611-613` | `_firecrawl_map` is a synchronous function called directly in async context (`discover_jobs` → `process_company` → `asyncio.gather`). Internal `time.sleep(sleep_s)` (up to 60s) blocked the entire asyncio event loop, completely pausing all other coroutines in the same batch. Fix: Call site changed to `await asyncio.to_thread(_firecrawl_map, career_url, fc_key)`; `_firecrawl_map` itself remains synchronous. Added `TestBug37FirecrawlMapNotBlocking` (2 test cases), 164 job_agent tests all passed. | Fixed |
| BUG-38 | `agents/job_agent.py:1512-1534` | `asyncio.Semaphore(3)` allowed 3 coroutines to concurrently execute `_process_scraped_jd`, each calling `_GEMINI_LIMITER.acquire()` independently. Token-bucket issues tokens at minimum intervals; 3 coroutines could obtain tokens in rapid succession, with actual RPM reaching 3x the configured 10 RPM limit, triggering Gemini 429. Fix: Moved `_process_scraped_jd` (containing Gemini call) outside the `async with sem:` block; Semaphore now only controls scraping I/O concurrency, while Gemini calls are independently controlled by `_GEMINI_LIMITER`. Added `TestBug38SemaphoreGeminiSeparation` (2 test cases), 166 job_agent tests all passed. | Fixed |

### P2 — Medium

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-39 | `agents/match_agent.py:43-48` | `_load_jd_markdown` only looked for `{md5}.md` (original crawled markdown), not `{md5}_structured.md` (structured version). Fix: Changed to prioritize `_structured.md` with fallback to `.md`, consistent with `resume_optimizer.py` logic. Added `TestBug39LoadJdMarkdownStructured` (3 test cases), 52 match_agent tests all passed. | Fixed |
| BUG-40 | `shared/excel_store.py:608-609,702-703` | Python operator precedence: `ws.cell(r, 8).value or "" if ws.max_column >= 8 else ""` actually parses as `value or ("" if cond else "")`. Fix: Added parentheses at all 4 locations to `(value or "") if cond else ""`, clarifying logical intent. Added `TestBug40OperatorPrecedence` (2 test cases), 109 excel_store tests all passed. | Fixed |
| BUG-41 | `agents/match_agent.py:61,338,392` / `resume_optimizer.py:37,372` | Stage 1 and Stage 2 each created independent `_RateLimiter(rpm=13)` instances with independent `_last` timestamps. Fix: Both files now create `_GEMINI_LIMITER = _RateLimiter(rpm=13)` shared instance at module level; Stage 1/2 reference the same object via `limiter = _GEMINI_LIMITER`. Added `TestBug41SharedRateLimiter` (3 test cases), 462 tests all passed. | Fixed |
| BUG-42 | `agents/job_agent.py:1538-1547,1694-1697` | `asyncio.gather(return_exceptions=True)` silently swallowed Gemini 429 errors. Fix: After gather, iterates results to detect 429/RESOURCE_EXHAUSTED, accumulates `quota_errors` count; prints user-visible warning when non-zero. Retry phase also adds `retry_quota_errors` count. Added `TestBug42QuotaExhaustionWarning` (2 test cases), 462 tests all passed. | Fixed |
| BUG-43 | `agents/job_agent.py:771` | `_scrape_google_jd` was missing `formats=["markdown"]` parameter in `FirecrawlApp.scrape()`. Fix: Added `formats=["markdown"]` to ensure markdown content is returned. Added `TestBug43GoogleJdScrapeParams` (1 test case), 462 tests all passed. | Fixed |
| BUG-44 | `agents/company_agent.py:350-353,438-441` | Tavily quota exhaustion (402/429/quota) was not distinguished from other errors. Fix: Both Tavily call sites now detect 402/429/quota; on match, `logging.error` + user-visible `print` warning. `discover_ai_companies` also `break`s to terminate subsequent queries. Added `TestBug44TavilyQuotaDetection` (4 test cases), 462 tests all passed. | Fixed |
| BUG-45 | `shared/excel_store.py:204-205` | `upsert_companies` only wrote 5 columns; remaining 4 columns were uninitialized. Fix: Row expanded to `[name, domain, focus, url, now, 0, 0, 0, "No"]` with 9 columns covering all `COMPANY_HEADERS`. Added `TestBug45UpsertCompaniesInitAllColumns` (1 test case), 462 tests all passed. | Fixed |
| BUG-46 | `shared/excel_store.py:376` | `batch_update_jd_timestamps` docstring said "col 9" but code wrote col 10. Fix: Docstring corrected to "col 10". Added `TestBug46DocstringColumnNumber` (1 test case), 462 tests all passed. | Fixed |
| BUG-47 | `agents/job_agent.py:129` | `data_quality` field was not in the `JobDetails` Pydantic model; it was only injected as a dict key. Fix: Added `data_quality: str | None = None` to `JobDetails(BaseModel)`. Added 2 Pydantic schema test cases, 462 tests all passed. | Fixed |
| BUG-48 | `agents/resume_optimizer.py:235,245,267,279` | `[{}] * N` created shared references. Fix: All 4 locations changed to `[{} for _ in range(N)]` list comprehension, each dict independent. Added `TestBug48ListComprehension` (2 test cases), 462 tests all passed. | Fixed |
| BUG-49 | `agents/company_agent.py:492-504` | `discover_ai_companies` directly modified the caller's `existing_names` set. Fix: Created `local_names = set(existing_names)` local copy; all subsequent operations use `local_names`. Added `TestBug49DiscoverCompaniesSetMutation` (2 test cases), 462 tests all passed. | Fixed |
| BUG-55 | `shared/excel_store.py:18,111-120` | `WITHOUT_TPM_HEADERS` defined 5 columns, but actual Excel had 7 columns. Fix: Constant expanded to 7 columns (including "TPM Jobs", "AI TPM Jobs"); `get_or_create_excel` adds migration logic to detect old 5-column header and append missing columns. Added `TestBug55WithoutTpmHeadersMigration` (3 test cases), 462 tests all passed. | Fixed |

### P3 — Low

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-50 | `agents/resume_optimizer.py:549-553` | `_print_summary` hardcoded column numbers to read `Tailored_Match_Results` table (col 3=Job Title, col 4=Company, col 5=Original Score, etc.). Fix: Imported `TAILORED_HEADERS`; all 6 column numbers changed to `TAILORED_HEADERS.index("field_name") + 1` dynamic lookup. Added `TestBug50PrintSummaryDynamicColumns` (2 test cases), 464 tests all passed. | Fixed |
| BUG-51 | `agents/match_agent.py:427-431` | `_print_top_results` used hardcoded column numbers for Resume ID (col 1), JD URL (col 2), Score (col 3). Fix: All 4 column numbers changed to `MATCH_HEADERS.index("field_name") + 1` dynamic lookup. Added `TestBug51PrintTopResultsDynamicColumns` (2 test cases), 466 tests all passed. | Fixed |
| BUG-52 | `shared/excel_store.py` multiple locations | 7 JD_Tracker read functions all used hardcoded column numbers. Fix: Added `_JD_COL = {h: i+1 for i, h in enumerate(JD_HEADERS)}` mapping; all 7 functions changed to dynamic lookup via `_JD_COL["field_name"]`. Added `TestBug52JdTrackerDynamicColumns` (4 test cases), 470 tests all passed. | Fixed |
| BUG-53 | `agents/job_agent.py:890` | `_fmt_addr` function was defined inside the `for block in blocks:` loop body, rebuilding the function object on each iteration. Fix: Moved `_fmt_addr` before the loop (still inside `_scrape_workday_jd`), eliminating repeated creation overhead. Added `TestBug53FmtAddrOutsideLoop` (2 test cases), 472 tests all passed. | Fixed |
| BUG-54 | `agents/*.py` | `_KEY_POOL` initial value is `None` across 4 agents; external calls throw contextless `AttributeError`. Fix: Added `if _KEY_POOL is None: raise RuntimeError(...)` guard at the beginning of all 9 functions using `_KEY_POOL.generate_content()`, providing clear error message and fix suggestion. Added `TestBug54KeyPoolNoneGuard` (9 test cases covering all 9 functions), 481 tests all passed. | Fixed |

---

## PRJ-002 Phase 4 Review (2026-04-28)

> Parallel review by 5 development-layer agents (agent-reviewer, test-analyzer, eval-engineer, schema-validator, doc-sync) of the `feat/3d-scoring` branch implementing 3-Dimension Scoring. See `docs/sdlc/PRJ-002-3d-scoring/reviews/phase4-review.md` for the consolidated report.

### P0 — Critical

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-56 | `shared/excel_store.py` `JD_HEADERS` / `upsert_jd_record` / `get_jd_rows_for_match`; `agents/job_agent.py` | **PRJ-002 ship-blocker**: `ats_keywords` was correctly extracted by Gemini and validated by the `JobDetails` Pydantic schema, but JD_Tracker had no column for it. The upsert path silently dropped the field, then `get_jd_rows_for_match` reconstructed `jd_json` from individual Excel cells without it. `match_agent._extract_ats_keywords` and `resume_optimizer`'s identical call path therefore always saw `[]` → `compute_coverage` returned `percent=None` → ATS Coverage % was always None in production. The flagship dimension didn't actually work. Fix (commit `bf25dcb`): JD_HEADERS gains "ATS Keywords" column at end; auto-migration appends it on existing workbooks; upsert / batch_upsert serialize as bullet list (sibling pattern); get_jd_rows_for_match reads it back into `jd_json`. `_save_structured_jd_md` also persists in human-readable cache. Added 7 tests including end-to-end round trip and migration. | Fixed |

### P3 — Low (Deferred / Quality / Testing)

| # | File | Description | Status |
|---|------|-------------|--------|
| BUG-57 | `agents/job_agent.py:1328` (extract_jd ats_keywords prompt), `shared/ats_synonyms.py` | **ATS keyword extraction quality risks** (eval-engineer Risk 1). Gemini at temperature=0.05 may hallucinate keywords from category knowledge ("an AI TPM usually needs Agile/OKRs/roadmap") rather than refuse to meet the 8-15 minimum on thin / non-tech-vertical JDs. Repeated calls on the same JD produce slightly different keyword lists (non-determinism). No current test asserts that emitted keywords are literal substrings of the JD Markdown. Mitigation already in place: ATS Coverage <30% prints with ⚠️ marker so users can spot anomalous coverage; the soft-flag-not-hard-gate design means false low percents don't block fine eval. Recommended fix: parametrized stability test that diffs two extractions and asserts each keyword appears as substring in the source JD. | Open |
| BUG-58 | `agents/match_agent.py:166` (`batch_coarse_score`) called from `agents/resume_optimizer.py:434` rescore_one | **Recruiter single-element batch may inflate tailored_recruiter_score** (eval-engineer Risk 2). Stage 1 in match_agent runs `batch_coarse_score(resume, [10 JDs])` so the model has peer JDs to anchor the bottom of the 1-100 scale. Optimizer's Recruiter rescore runs `batch_coarse_score(tailored_md, [1 JD])` — no peer anchoring, model tends to cluster near 60-70. Result: `recruiter_delta` may be systematically positive even when the tailor didn't meaningfully improve recruiter-relevance. Mitigation: `recruiter_delta` is informational only — does NOT trigger regression flag (REQ-108). Recommended verification: take 10 already-coarse-scored JDs, rerun `batch_coarse_score(resume, [single_jd])` for each individually, compare mean to original 10-batch mean. If shift > 5pt, normalize the offset or down-rank the rec_delta signal in the user-facing summary. | Open |
| BUG-59 | `agents/resume_optimizer.py:516` (`regression = hm_delta < 0`) | **HM Delta < 0 too sensitive given LLM noise floor** (eval-engineer Risk 3). Gemini at temperature=0.0 still varies ±2-3 points on integer 1-100 scores due to context window tokenization differences. A semantically-equivalent tailored resume may produce a -1 or -2 hm_delta by chance ~25-30% of runs, false-positive triggering the "HM regressed → keep base" warning. Recommended fix after empirical noise measurement: change threshold to `hm_delta <= -3` (or whatever the measured noise floor is). Verification: run `re_score(original_resume, jd_content)` 3 times on the same pair for 10 JDs, record max-min spread. | Open |
| BUG-60 | `agents/resume_optimizer.py:266-267` | **Cross-agent _KEY_POOL propagation is fragile** (agent-reviewer S-2 reframed). Optimizer's `_main_inner` mutates `agents.match_agent._KEY_POOL` so `batch_coarse_score` (used for Recruiter rescore) has a pool. Works today because optimizer's main always runs before any cross-agent call. Risks: (a) any test that imports `batch_coarse_score` independently raises a confusing `RuntimeError`; (b) the propagation is implicit and undocumented at the call site. Recommended refactor: inject the pool as an explicit parameter to `batch_coarse_score`, or move the function (along with its limiter) to `shared/` so it owns its own pool reference. Out of scope for PRJ-002 because removal of cross-agent dependency is a separate cleanup PR. | Open |
| BUG-61 | `tests/test_match_agent.py`, `tests/test_resume_optimizer.py`, `tests/test_excel_store.py` | **Test coverage gaps in 3-dim integration flow** (test-analyzer review). Most tests are unit-level or source-text guards; missing integration tests for: (a) Stage 2 structural error paths preserving Stage 1's ATS / Recruiter values in Excel, (b) `rescore_one` writing `None` to Tailored Recruiter when batch_coarse_score returns `[]` for that URL but works for others, (c) behavioral test of `optimizer._KEY_POOL` propagation (currently only source-text checked), (d) mixed batch where some JDs have `ats_keywords` and others don't, (e) resume hash change invalidating coarse rows and recomputing ATS. Detailed list of 5 high-value tests in the Phase 4 review document. | Open |
