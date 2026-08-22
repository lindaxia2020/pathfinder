# Task Tracker

## Open follow-ups (as of 2026-08-21 — each is its own small plan; start a fresh session per item)

Context: all items below were found during the 2026-08-20/21 job-agent result review (BUG-79~82, REQ-163~166 — see CHANGELOG 2026-08-20/21). Code for those is in the working tree (commit it first if not yet committed).

- [ ] **Google labeling** (`agents/job_agent.py:_fetch_google_jobs`): ignores the Career URL's `company=` param and searches all of Google US → the first-processed Google row ("Google DeepMind", AI-native) absorbs all ~51 Google-wide TPM postings (0 titles mention DeepMind); the "Google" (Mid-large Tech) row then finds nothing new; Job Domain forced "AI"; plain-PM titles leak via the AI-native rule. Fix idea: honor `company=` (filter payload rows by company field) or merge the two rows.
- [ ] **Microsoft list adapter**: `careers.microsoft.com` routes to Path B (Firecrawl + Crawl4AI on a JS SPA) → ~nothing (1 row total). Needs an adapter like Amazon/Google. `gcsservices.careers.microsoft.com/search/api/v1/search` gave a TLS name mismatch from this host on 2026-08-20 — find the endpoint the SPA actually calls. Locations are "City, Washington, United States" (now handled by BUG-79).
- [ ] **Freshness gate policy**: ≥15-day-old postings are never written on first sight (`_apply_prescrape_freshness_gate`, REQ-004-10). Cowboy Space "TPM, Avionics" (07-17) was lost this way. Decide whether small/vertical companies get a longer first-seen window.
- [ ] **Salesforce Workday sanity**: `searchText="Program Manager"` returns 421 postings but 0 TPM titles after `_tpm_filter` — verify the search covers "Technical Program Manager" titles (compare with a direct `searchText="Technical Program Manager"` probe).
- [ ] **Ashby prefetched JD**: the Ashby job-board API already returns `descriptionHtml`; carrying it as `_prefetched_md` (like Amazon) would remove the Crawl4AI render for Ashby JDs (Cowboy Space took 3 browser passes). Optimization, low priority.

## Completed (2026-08-21): Auto-archive feature removed (REQ-167)

**Trigger**: user decision — never skip a company (new openings appear any time); the REQ-063 archiver was also one bug-affected run away from silently archiving 334 companies, and counted triaged-out companies as "no jobs".

- [x] `agents/job_agent.py`: archive skip list + post-run archive phase removed, imports cleaned
- [x] `shared/excel_store.py`: 5 archive helpers deleted; `COMPANY_HEADERS` 7 cols; migration strips legacy `No TPM Count`/`Auto Archived` columns (`_LEGACY_ARCHIVE_COLUMNS`); `upsert_companies` defaults `[0, 0]`
- [x] `shared/config.py`: `AUTO_ARCHIVE_THRESHOLD` removed
- [x] Tests: REQ-063 classes → `TestAutoArchiveRemoved` (excel_store + job_agent); P0-5 / column-count tests adapted
- [x] Workbook migrated (backup `pathfinder_dashboard.backup-20260821.xlsx`)
- [x] Docs: REQUIREMENTS §9.16 REQ-167 (+REQ-063 marked removed, REQ-029 note, v2.10), ARCHITECTURE v2.5, CLAUDE.md, `.claude/agents/observability.md`, CHANGELOG

## Completed (2026-08-21): Result review round 2 — BUG-82 + YoE policy REQ-166

**Trigger**: after the 08-20 fixes re-run, Cowboy Space 08-14 TPM and most Blue Origin WA TPMs were still absent. Reproduced scrape → extract → gate offline per JD.

- [x] Diagnosis: Cowboy Space = YoE gate (2-5 yrs → min 2 ≤ 3, by design); Blue Origin = write-time geo drop on verbatim `"WA - Landmark (Ride East), …"` (LLM formatting), plus `CA - Remote` / `US - Remote` forms → Other
- [x] BUG-82 `classify_region`: separator normalization, leading state code, token-based remote rule (`Remote, Canada` still Other)
- [x] BUG-82 `_gate_and_finalize(list_location=…)`: drop only when extracted AND list-API locations are both out of scope (threaded from `process_company` list_meta)
- [x] REQ-166: YoE gate skip only stated min ≥11 (was ≤3 or ≥12)
- [x] Tests first (5 failing → green), full suite
- [x] Docs: BUGS (BUG-82 + policy note), CHANGELOG, REQUIREMENTS §9.15 (REQ-165/166, v2.9), ARCHITECTURE v2.4

## Completed (2026-08-20): Job-agent result review → recall fixes (BUG-79~81)

**Trigger**: user reviewed the 2026-08-19 manual job_agent run — Seattle big tech under-counted, Cowboy Space TPM missing.

- [x] Review: run completed normally (378 companies, 143 JD rows); root-caused via live API probes (Amazon search.json, Ashby, Workday CXS)
- [x] BUG-79 `classify_region`: full-state-name WA forms + WA city hints (D.C.-guarded, other-state veto); CA/TX/FL tokens word-bounded (", canada" ≠ CA)
- [x] BUG-80 `_fetch_workday_jobs`: skip `/en-US/` locale segment in site slug (13/28 Workday rows were 404 → 0 jobs)
- [x] BUG-81 `_fetch_workday_jobs`: `total` from page 0 only (later pages report 0) — pagination was capped at 40 for every Workday company
- [x] Repro tests first (3 failing → green); full suite run
- [x] Docs: BUGS (BUG-79~81 + summary), CHANGELOG, REQUIREMENTS §9.14 (REQ-163/164, v2.8), ARCHITECTURE v2.3, lessons
- [ ] Follow-ups — see "## Open follow-ups" section at top of this file


## Completed (2026-08-19): Filter widening — titles + WA geo (REQ-160/161)

**Trigger**: user decision — leave more results at the cheap title/geo pre-filter; write-time gates keep final precision.

- [x] `TPM_KW` + "Technical Project Manager" (+ mgr/tech variants) on all tracks
- [x] `PM_TITLE_OK_TRACKS` (AI-native/Robotics/Space/Defense): plain "Program Manager" accepted in `_tpm_filter`; track threaded `process_company → discover_jobs → _discover_via_api`
- [x] `llm_filter_jobs` three-way rule split (PM-OK vertical / Fintech / mid-large); mid-large also accepts Technical Project Manager
- [x] Workday `searchText` + Firecrawl map query → "Program Manager"; Amazon/Google kept narrow (pagination-cap risk, documented in code)
- [x] Geo: `classify_region` "Seattle" → "WA" (whole state via `,\s*wa\b` regex + state forms; bare "Washington"/D.C. still Other); keep-set WA/CA/TX/US-Remote; sort tier WA+Remote > CA/TX
- [x] Tests: +track-title matrix, WA-state matrix (incl. Wales/D.C. false-positive guards), LLM rule split — full suite 1,145 passed / 1 skipped
- [x] Docs: CHANGELOG, REQUIREMENTS (REQ-160/161, §9.13), ARCHITECTURE v2.2
- [x] Company discovery aligned to WA/CA/TX/US-Remote: GEOGRAPHY prompt clause (WA state + US-remote qualification) + `TAVILY_QUERIES` rebuilt with uniform `_QUERY_GEO_TAIL` (no per-track geo differentiation); 233 company tests green
- [x] Space-track regional enhancement: `_SPACE_REGION_QUERIES` (+4 geo-targeted queries: Greater Seattle / SoCal / TX / FL Space Coast, outside the uniform tail)
- [x] REQ-162: FL = Space-track-only target region — `classify_region` "FL" (city hints, no bare "melbourne"), shared `_geo_out_of_scope(loc, track)` gate in `_tpm_filter` + `_gate_and_finalize`, GEOGRAPHY clause Space-FL exception, sort tier FL~CA/TX; full suite 1,149 passed / 1 skipped

## In progress (2026-08-19): Company agent URL/track quality fixes (BUG-74~77) + staged Company_List audit

**Plan**: `.claude/plans/twinkly-mixing-adleman.md` | **Trigger**: user manually verified all 380 Company_List rows; found job-posting-as-career-URL, guessed ATS URLs, inaccurate Track/Focus.
**HARD CONSTRAINT**: the 380 Company Name + Career URL values are user-verified — never modified by any code path.

- [x] BUG-74: `_is_job_posting_url` + `_posting_url_to_board_root`; wired into `_is_likely_career_url`, `_tavily_extract_career_url`, `validate_and_upgrade_ats_url` step 2
- [x] BUG-77: `_AGGREGATOR_HOSTS` blocklist (suffix match; gem.com deliberately excluded — legit ATS) in `_is_likely_career_url`
- [x] BUG-75: `_org_name_matches_company` (generalized Workday matcher) + per-ATS org-name fetch + ownership gate in `_find_ats_url`/`validate_and_upgrade_ats_url`; `_slug_candidates` drops bare single words for multi-word names; homepage identity gate
- [x] Phase 1.5 write guard: filled non-ATS rows skipped (never rewritten) — was silently upgrading user-verified URLs every run
- [x] BUG-76: `confident` field + do-not-guess clause in discovery; unconfident → blank track/focus → repair paths backfill
- [x] Staged audit: `--audit` CLI → Gemini re-eval of Track/Focus (batches of 20, ~19 calls, zero Tavily) + URL health flags → `Company_Audit` tab only (`replace_audit_sheet` in excel_store); apply flow deferred until user reviews
- [x] Tests: full suite 1104 passed / 1 skipped (+~55 new)
- [x] Verify: dry-run (5 rows, skip-http) ✅; 15-row live ✅ (caught real wrong-company URL: Applied Digital → Apply Digital's lever board); full 380 running
- [x] Docs: BUGS BUG-74~77, CHANGELOG, ARCHITECTURE, REQUIREMENTS v2.7 (REQ-153~157), CLAUDE.md
- [x] Full 380-row audit ×2 (matcher refined between runs) + Company_List sha256 identical; final suite 1108 passed / 1 skipped
- [x] Bonus fix found during verification: `_slug_candidates` str.replace(" corp") corrupted " corporation" names ("rocket laboration"); matcher now also compares dedup-normalized forms (Lambda Labs ↔ "Lambda") — mismatch flags 38→30
- [x] Real wrong-company URLs surfaced (report-only, user decides): Applied Digital→Apply Digital's lever, Figure AI→Figure Lending, Runway→cfo.ai, Safe Superintelligence→Safe Security
- [x] User approvals applied via `--apply-audit --urls-only` (Applied Digital / Zep AI / 6sense); oscillating track/focus re-proposals skipped per user choice; anti-oscillation ledger (REQ-159, logs/company_audit_ledger.json) seeded with all 371 rows; convergence proven — re-audit now proposes 0 track / 0 focus (16 suppressed); 474 tests green
- [x] URL correction loop shipped (user-designed, REQ-158): `--suggest-urls` (+`--suggest-non-ats`, `--suggest-limit`) fills Suggested URL/Evidence/Approve columns for flagged rows; `--apply-audit` writes only Y-approved suggestions; +12 tests (462 green); upsert_companies storage-layer URL guard (+2 tests, 2 legacy tests updated to new invariant)
- [x] `--apply-audit` shipped + run after user review (user hand-fixed URLs, deleted 9 cos): 27 track + 21 focus applied, 2 skipped-deleted; PayPal reverted to Mid-large Tech (taxonomy rule); Name+URL set verified identical to user's hand-edited state; backup pathfinder_dashboard.backup-20260819.xlsx

## Completed (2026-07-16): Concurrent-run collision fix (BUG-73)

**Trigger**: manual `job_agent` run (09:57) crashed with EOFError — it overlapped the still-running 04:00 scheduled pipeline (6h long from company-agent API retries); the pipeline's match agent rewrote the xlsx mid-read. Both job agents also scraped the same 387 JDs (duplicate spend).

- [x] `shared/run_lock.py` — exclusive flock on `logs/pathfinder.lock` per agent run; fail-fast naming holder, `PATHFINDER_LOCK_WAIT=1` queues; kernel-released on any exit
- [x] Wired into all 4 agents' `__main__`; `run_pipeline_scheduled.sh` exports `PATHFINDER_LOCK_WAIT=1`
- [x] `shared/excel_store.py` — `load_workbook_readonly` (BytesIO snapshot + 3-attempt retry) at all 17 read-only sites + 2 agent-local sites; `get_company_archive_info` → single `iter_rows` pass
- [x] Tests +9 (lock contention/wait/release, snapshot survives truncation, retry heal, pipeline env export); full suite 1042 passed / 1 skipped
- [x] Live check: held lock + real `python agents/job_agent.py` → immediate exit 1 with 🔒 holder message
- [x] Docs: BUGS BUG-73 (P1), CHANGELOG, REQUIREMENTS v2.6 (REQ-151/152 §9.11), CLAUDE.md (structure + Running Agents note)
- [ ] Follow-up (noted in BUG-73): remaining 16 read helpers still use O(rows²) per-cell access on read-only sheets — functional + snapshot-protected, rewrite to `iter_rows` if runs slow down

## Completed (2026-07-13): JD dedup + intern filter (BUG-71/72, user Excel review)
- [x] Dedupe `Skipped JD`: removed 3 duplicate rows (Uber×2 LinkedIn tracking-param variants, Tesla×1 apply/slug variant); backup `pathfinder_dashboard.backup-20260713.xlsx`
- [x] `shared/excel_store.py` — `canonical_jd_url()`; `get_triaged_jd_urls` returns canonical set; `batch_upsert_jd_records` indexes by canonical URL
- [x] `agents/job_agent.py` — canonical comparison at triage/fresh/stale/seen/incomplete-retry layers; `_INTERN_TITLE_RE` in `_tpm_filter` + Gate 1.5 in `_gate_and_finalize`
- [x] Tests: +18 (canonicalization, canonical upsert/triage, intern filter/gate); docs: BUGS BUG-71/72, CHANGELOG, REQUIREMENTS v2.5 (REQ-149/150), CLAUDE.md triage note

## Completed (2026-07-10): Company agent — discover-to-500 loop + self-heal + Track sort

**Plan**: `.claude/plans/tingly-riding-meerkat.md` | **Trigger**: 2026-07-09 run attempted 50 / succeeded 0
**Root cause of 0 successes**: Tavily plan-limit exhaustion (not code) — watch quota reset (already on the step-9 audit list).

- [x] `shared/config.py` — `TRACK_ORDER` canonical 6-bucket order (shared source of truth)
- [x] `run_discovery_loop` — batches of 50 until 500 quota / zero-yield batch / 10-iteration cap; exclusion list re-read per iteration; stop reason in run summary (REQ-145)
- [x] `run_enrich_missing_tracks` — blank/N-A Track filled via `_classify_tracks_batch` (factored out of `migrate_tracks`); unconfident stays blank for retry; custom values untouched; primes forced Mid-large Tech (REQ-146)
- [x] `sort_company_list_by_track` — canonical order, name secondary, unknowns sink last; count-preserving in-place rewrite; final main() step (REQ-147)
- [x] main() order locked by test: discovery → phase 1.5 → focus re-enrich → track enrich → sort
- [x] Tests +13 (loop stop matrix, track-enrich matrix, sort integrity); full suite 1003 passed / 1 skipped
- [x] Real-data check: sorted a copy of the live dashboard — 355 rows grouped correctly, 2 blank-track rows sink last, idempotent
- [x] Docs: CHANGELOG, REQUIREMENTS (§9.9 REQ-145–147, v2.4), ARCHITECTURE §3.1

### Follow-up same day: Tavily key pool (BUG-70)
- [x] `shared/tavily_pool.py` — mirror of Firecrawl pool; rotates on 402/429/quota/**usage-limit** ("exceeds your plan's set usage limit" — the real Tavily error text that evaded all BUG-44 abort checks on 2026-07-09); exhaustion → one warning + `TavilyQuotaExhausted` (message contains "429"+"quota" so call sites work unchanged)
- [x] Wired: company_agent main() builds ONE pool threaded through discovery → phase 1.5 → focus re-enrich (shared exhaustion state); job_agent backfill client; `TAVILY_API_KEY_2` added to `.env`
- [x] Tests: tests/test_tavily_pool.py (14) + discover_ai_companies tests now pass client directly (signature: key → client-like)
- [x] Docs: BUGS.md BUG-70, CLAUDE.md env keys + structure, CHANGELOG, REQUIREMENTS REQ-148/v2.4, ARCHITECTURE §5
- [x] Live smoke: key #1 raised the real "exceeds your plan's set usage limit" text → pool rotated → search succeeded on key #2 (1 result). Rotation + usage-limit detection confirmed live.

---

## Active (2026-07-07): PRJ-004 Multi-Track Expansion — Phase 3 Implementation

**Source of truth**: `docs/sdlc/PRJ-004-multi-track-expansion/design.md` §6 (T1–T17)
**Branch**: `prj-004-multi-track-expansion`
**Critical path**: T1 → T5 → T6 → T9 → T17

### P0 (launch-blocking)
- [x] T1 excel_store schema (commit 49e3b1b — landed atomically with T2/T9; suite 876 green)
- [x] T2 row selector all-valid-rows + job_domain passthrough; qualified-count rework (49e3b1b)
- [x] T3 company_agent taxonomy/quotas/rules (suite 902 green)
- [x] T4 --migrate-tracks CLI (code done; RUN still blocked on user pruning; suite 907)
- [x] T5 job_agent extraction core (suite 923)
- [x] T6 posting dates + freshness gate + backfill (with T5 commit)
- [x] T7 geo tighten (with T5 commit)
- [x] T8 Workday pagination; Firecrawl limit removal (6ffd36c; suite 885)
- [x] T9 sort_jd_tracker_by_tier rewrite (49e3b1b — landed with T1; recompute-at-sort verified by test)
- [x] T10 prompts.py per-track pairs + accessors (986500b; suite 885)
- [x] T11 match_agent per-track routing + per-track caches (5fab05a + fixups)
- [x] T12 resume_optimizer per-track routing (suite 891 green)

### P1/P2 (non-blocking)
- [x] T13 launchd failure surfacing (2577913)
- [x] T14 Amazon.jobs adapter + prefetch routing (suite 930)
- [x] T15 Tesla regression verification (registry + schema tests)
- [x] T16 Google Careers adapter (done 2026-07-09; design deviation — v3 API dead, uses server-rendered AF_initDataCallback payload; live check: 291 postings → 106 TPM candidates)

### Rollout (T17 — Phase 5 launch checklist; see docs/sdlc/PRJ-004-multi-track-expansion/launch-readiness.md §2)
- [x] User pruned; migrate-tracks 201/201, 0 UNMIGRATED, spot-check accepted
- [x] JD_Tracker was already wiped; schema upgraded cleanly
- [x] 10 discovery runs (347/500, Tavily quota-truncated; G1 option (a) accepted) → trial run clean (122 JDs, 93 tailored, ~$0.3-1 Gemini) → user GO 2026-07-08 → launchd daily 04:00 LIVE
- [ ] Step-9 early audits (first few daily cycles): G3 YoE 20+ spot-check, G5 work-auth sample, R-10 geo drops, R-02 domain boundaries, D-07 space-rule review; watch Tavily quota reset

**Working rules**: full suite after each task, never left red; tests in tests/; bugs found en route go to BUGS.md.

---

## Backlog (stale, 2026-04-28): REVIEW_2026-04-28 follow-up

**Source doc**: `docs/REVIEW_2026-04-28.md`
**Status**: P0 done (7/8, P0-8 WONTFIX); P1 5 fixed today + 2 fixed earlier; P2 3 fixed earlier
**Verification**: 8 read-only sub-agents re-checked remaining P1/P2 → results appended to `docs/REVIEW_2026-04-28.md` "验证回访" section
**Remaining**: 15 P1 STILL + 4 P1 PARTIAL + 9 P2 STILL + 1 P2 PARTIAL (24 STILL items total)
**Top-6 ROI** for next batch (per verification report):
- [ ] P1-22 — refactor 27 `inspect.getsource` tests to behavioral
- [ ] P1-7 + P1-8 — Excel header lookup + Resume Hash migration in `Tailored_Match_Results`
- [ ] P1-15 — gate `is_ai_tpm` on JD content even for ai_native companies
- [ ] P1-2 / P1-12 — RateLimiter: lazy Lock create + don't hold lock across `sleep`
- [ ] P1-13 — Tailored vs original diff (substring containment + length ratio)
- [ ] P2 doc drift — `excel_store.py:2` / `REQUIREMENTS.md:13` / `ARCHITECTURE.md archive/` (one commit)

---

## Completed: PRJ-002 — 3-Dimension Scoring (ATS / Recruiter / HM)

**Branch**: `feat/3d-scoring` (merged via PR #2 → main)
**SDLC**: `docs/sdlc/PRJ-002-3d-scoring/`
**Started**: 2026-04-28

### PR 1 — Foundation: ATS matcher + JD schema ✅ DONE (commit ba7ed51)
- [x] `shared/ats_synonyms.py` — 18 entries
- [x] `shared/ats_matcher.py` — normalize/expand_synonyms/compute_coverage
- [x] `shared/schemas.py` — added `ATSCoverageResult`
- [x] `agents/job_agent.py` — `JobDetails.ats_keywords` field + extraction prompt
- [x] `tests/test_ats_matcher.py` — 50 new tests
- [x] Full suite 669 passed (was 619, +50 new, 0 regression)
- [x] Committed (not yet pushed — awaiting user review)

### PR 2 — Excel + prompt rename ✅ DONE (commit 28eee7e)
- [x] `shared/prompts.py` — RECRUITER/HM names + back-compat aliases (pure rename, no content drift)
- [x] `shared/excel_store.py` — MATCH_HEADERS +4 cols, TAILORED_HEADERS +9 cols, migration logic
- [x] `tests/test_shared_prompts.py` — alias identity tests + content-drift guard
- [x] `tests/test_excel_store.py` — migration tests for both sheets + headers tests
- [x] Full suite 686 passed (was 669, +17 new, 0 regression)
- [x] Committed (not yet pushed — awaiting user review)
### PR 3 — Match agent 3-dim scoring ✅ DONE (commit 09df10b)
- [x] `agents/match_agent.py` — ATS dim helpers + 3-dim coarse/fine record writes
- [x] `shared/excel_store.py` — `batch_upsert_match_records` accepts dict (preserves on key-absent)
- [x] `tests/test_match_agent.py` — 9 new tests (extract / compute_for_jds / threshold)
- [x] `tests/test_excel_store.py` — 7 new tests (dict format, preservation, mixed)
- [x] Full suite 703 passed (was 686, +17 new, 0 regression)
- [x] Committed (not yet pushed — awaiting user review)
### PR 4 — Optimizer 3-dim rescore ✅ DONE (commit 32afd46)
- [x] `shared/excel_store.py` — `get_scored_matches` surfaces per-dim; `batch_upsert_tailored_records` accepts 9 per-dim keys; regression precedence (explicit > hm_delta > score_delta)
- [x] `agents/resume_optimizer.py` — 3-dim rescore (ATS det. + Recruiter LLM + HM LLM); regression = `hm_delta < 0`; per-JD print shows all 3 dims; cross-agent key pool sharing
- [x] `tests/test_resume_optimizer.py` — 5 new tests (imports / call sites / record keys / legacy mirroring / pool sharing) + updated regression test
- [x] `tests/test_excel_store.py` — 10 new tests (per-dim writes, regression precedence, ATS drop ≠ regression)
- [x] Full suite 718 passed (was 703, +15 new, 0 regression)
- [x] Committed (not yet pushed — awaiting user review)
### PR 5 — Documentation + alias deprecation ✅ DONE (commit 9834b46)
- [x] `CHANGELOG.md` — 2026-04-28 entry covering PRJ-002 + 4 P0 follow-ups
- [x] `REQUIREMENTS.md` — new section 9 (REQ-100~112), fixed REQ-033/035/052 drift, v1.9 history
- [x] `ARCHITECTURE.md` — 3.3 Match / 3.4 Optimizer flows updated; Excel schema table; v1.6 history
- [x] `shared/prompts.py` — alias DEPRECATED note (NOT removed; cross-cutting rename deferred to dedicated future PR)
- [x] Full suite 718 passed (unchanged from PR 4)
- [x] Committed (not yet pushed)

All 5 PRs complete; merged to `main` via PR #2 (commit 3a1f86b closed the SDLC project).

## Open lessons
See `tasks/lessons.md` (created when first lesson lands).
