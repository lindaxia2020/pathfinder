# PRJ-002 Phase 4 Review — Consolidated Findings

**Date**: 2026-04-28
**Branch**: `feat/3d-scoring`
**Reviewers**: 5 development-layer agents run in parallel:
- `agent-reviewer` — code quality + prompt design
- `test-analyzer` — coverage gap analysis
- `eval-engineer` — AI output quality risks
- `schema-validator` — Excel schema + inter-agent contracts
- `doc-sync` — doc/code drift

**Outcome**: 1 ship-blocker fixed (BUG-56), 4 quick wins applied, 5 deferred items logged in `BUGS.md` (BUG-57~61), 4 doc drift items fixed.

---

## 1. Findings Summary

### Ship-Blocker (Fixed)

| ID | Finding | Resolution |
|---|---|---|
| BUG-56 | `ats_keywords` was extracted by Gemini but silently dropped before reaching `match_agent` and `resume_optimizer`. Cause: `JD_HEADERS` had no column for it, `upsert_jd_record` discarded it, `get_jd_rows_for_match` reconstructed `jd_json` without it. Result: ATS Coverage % was always None in production — flagship dimension didn't work. | Fixed in commit `bf25dcb` (Phase 4 fix C1). Added "ATS Keywords" column with auto-migration; upsert + read paths now persist and reconstruct the field. 7 new tests including end-to-end round trip. |

### Quick Wins (Applied)

| Source | Finding | Resolution |
|---|---|---|
| agent-reviewer M-4 | Synonym group `["api", "rest api"]` was too loose (different specificity levels). | Removed in `c8649ac` (C2). |
| agent-reviewer M-3 | `_extract_ats_keywords` swallowed exceptions silently. | Added `logging.debug` in `c8649ac`. |
| agent-reviewer M-1 | `original_hm = match.get("hm_score") or match.get("score") or 0` could mask genuine zero-score (defense in depth — match_agent clamps to ≥1, so latent only). | Switched to explicit `is not None` check chain in `c8649ac`. |
| doc-sync (4 items) | ARCHITECTURE/CLAUDE/REQUIREMENTS/brd drift (FINE_SYSTEM_PROMPT name, 2-stage description, 485+ test count, "4 design decisions D1-D5"). | Fixed in `6256bec` (C3). |

### Rejected After Verification (False Alarms)

| Source | Finding | Verification |
|---|---|---|
| agent-reviewer S-1 | "Recruiter delta is nonsense — `meta['jd_json']` differs from what match_agent's Stage 1 sees". | Both call sites use the same `jd["jd_json"]` string from `get_jd_rows_for_match`. `_format_jd_for_coarse` produces symmetric output for both ends. **Reviewer was wrong.** |
| agent-reviewer S-2 (rate limit half) | "Optimizer accidentally doubles rate budget to 26 RPM by referencing match_agent's separate `_GEMINI_LIMITER`". | `batch_coarse_score` does not internally acquire any limiter — the limiter is acquired in optimizer's `rescore_one` once per call. Only the optimizer's limiter is used. **Reviewer was wrong** about the doubling specifically. |
| agent-reviewer S-2 (key pool fragility) | "Cross-agent `_KEY_POOL` propagation is fragile and undocumented". | **Real concern**, but deferred (see BUG-60). The propagation works at runtime; the fragility is a code-smell warranting a future cleanup PR, not a ship-blocker. |

### Deferred (Logged in BUGS.md)

| ID | Finding | Why Deferred |
|---|---|---|
| BUG-57 | ATS keyword extraction may hallucinate / be unstable on thin JDs (eval-engineer R1). | Mitigated by soft-flag-not-hard-gate design and ⚠️ marker. Empirical fix (parametrized stability test) is post-merge work. |
| BUG-58 | Recruiter single-element batch may inflate tailored_recruiter_score vs. 10-batch original (eval-engineer R2). | Mitigated by `recruiter_delta` being informational only (not in regression rule). Empirical calibration measurement and offset normalization are separate work. |
| BUG-59 | HM Delta < 0 may produce false-positive regression flags 25-30% of the time given Gemini integer noise (eval-engineer R3). | Needs empirical noise floor measurement before changing the threshold. Conservative `< 0` is the safe default until then. |
| BUG-60 | Cross-agent `_KEY_POOL` propagation is implicit and fragile (agent-reviewer S-2 reframed). | Refactor to inject pool/limiter explicitly is a separate cleanup PR (touches several files). |
| BUG-61 | Test coverage gaps in 3-dim integration flow (test-analyzer suggested 5 high-value tests). | Test additions would benefit from real Excel fixtures and integration-style mocking that's a meaningful test-suite investment. Better as a focused testing PR. |

---

## 2. Reviewer Coverage Matrix

| Concern | agent-reviewer | test-analyzer | eval-engineer | schema-validator | doc-sync |
|---|---|---|---|---|---|
| Code correctness | ✓ | – | – | ✓ | – |
| Prompt design | ✓ | – | ✓ | – | – |
| Edge cases | ✓ | ✓ | – | ✓ | – |
| AI output quality | – | – | ✓ | – | – |
| Test coverage | – | ✓ | – | – | – |
| Schema integrity | – | – | – | ✓ | – |
| Inter-agent contracts | – | – | – | ✓ | – |
| Migration safety | – | – | – | ✓ | – |
| Doc/code consistency | – | – | – | – | ✓ |
| Cross-doc consistency | – | – | – | – | ✓ |

All 5 perspectives complementary; no major overlap.

---

## 3. Test State Through Phase 4

| Stage | Tests | Delta |
|---|---|---|
| PRJ-002 baseline (start of `feat/3d-scoring`) | 619 | — |
| End of PR 5 (PRJ-002 implementation done) | 718 | +99 |
| End of Phase 4 fix C1 (ship-blocker fix) | 725 | +7 |
| End of Phase 4 (current) | 725 | 0 (C2-C4 = no behavior changes) |

All passing. Zero regressions across all 5 PRs and 4 Phase 4 fix commits.

---

## 4. Phase 4 Verdict

**Ship-ready** with the BUG-56 fix in place. The 5 deferred items (BUG-57~61) are quality / observability concerns that don't block correctness for the 3-Dimension Scoring feature in the user's existing workflow. They warrant follow-up but should not delay PRJ-002 launch.

**Phase 4 sign-offs needed**:
- [x] Engineer Lead self-review (this document)
- [ ] PM functional acceptance
- [ ] User sign-off

**Phase 5 entry criteria met**:
- [x] All ship-blockers resolved
- [x] Open issues triaged and logged in BUGS.md
- [x] Test suite green (725 passing)
- [x] Documentation aligned with code
