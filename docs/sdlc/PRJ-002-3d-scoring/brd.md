# BRD: 3-Dimension Scoring (ATS / Recruiter / HM)

**Project ID**: PRJ-002
**Author**: PM Agent (pending)
**Status**: Draft
**Date**: 2026-04-28

## 1. Background & Problem Statement

PathFinder's current scoring pipeline produces a single 1-100 "fit score" via two sequential LLM stages (coarse → fine). This score reflects a "thoughtful hiring manager" judgment but does **not** map to the real North American hiring funnel, which is a cascade:

1. **ATS keyword filter** — deterministic string match; rejects resumes missing required keywords before any human sees them
2. **Recruiter quick scan** — 30-second holistic skim
3. **Hiring Manager deep dive** — full evaluation against role criteria

Consequences of the current single-score model:
- Score Delta after tailoring conflates "added keywords" with "stronger semantic fit", giving the user no signal on which one moved
- Resumes that would clearly fail an ATS keyword filter still get high scores because LLMs semantically auto-complete missing terms ("led ML projects" matches "machine learning" in semantics, but never in real ATS)
- Optimizer's `TAILOR_SYSTEM_PROMPT` already instructs "mirror keywords from the JD", but no metric verifies whether mirroring succeeded
- No way to detect "tailor improved keyword coverage but degraded semantic strength" or vice versa

## 2. Goals & Success Criteria

**Primary goal**: Replace the single fit score with three independent, parallel dimensions that each map to one real-world hiring filter.

**Measurable success criteria**:
- G1: Every JD in `Match_Results` carries an ATS Coverage % computed deterministically (no LLM)
- G2: Recruiter Score and HM Score appear as separate columns; their values are produced by distinct prompts
- G3: `Tailored_Match_Results` shows per-dimension delta (ATS / Recruiter / HM)
- G4: ATS dimension's mean delta after tailoring > +30 percentage points across a 50-JD sample
- G5: HM dimension's mean delta after tailoring is in `[-2, +5]` range (semantic strength roughly preserved, never inflated)
- G6: Regression detection switches to "HM delta < 0" only — not the conflated single-score delta
- G7: All 619 existing tests pass; new tests added for ATS matcher, prompt rename, schema migration

## 3. Scope

### In-Scope
- ATS keyword extraction in `job_agent` (new `ats_keywords` field on `JobDetails`)
- Deterministic ATS coverage matcher (`shared/ats_matcher.py`)
- Match agent computes 3 dimensions per JD (ATS for all, Recruiter for all, HM gated by current threshold/top-N)
- Optimizer rescores 3 dimensions for tailored resumes
- Excel schema migration (auto-add new columns; backfill where derivable)
- Prompt rename: COARSE → RECRUITER, FINE → HM (with 1-release alias for back-compat)
- Documentation updates (CHANGELOG, ARCHITECTURE, REQUIREMENTS)

### Out-of-Scope
- Hard ATS gate (rejecting JDs below threshold) — soft flag only
- Custom synonyms per company / per industry (start with global hand-curated table)
- ATS-by-vendor modeling (Workday vs Greenhouse vs Lever differ slightly; we model a generic "keyword coverage" baseline)
- Years-of-experience / location / work-authorization filters (separate future feature)
- UI/dashboard rendering changes (Excel-only this iteration)
- Re-extracting `ats_keywords` for already-cached JDs (only new JDs get the field; old JDs degrade gracefully)

## 4. Functional Requirements

| REQ ID | Description | Priority |
|--------|-------------|----------|
| REQ-100 | `JobDetails` Pydantic schema gains `ats_keywords: list[str]` field, populated by Gemini at ingest time | P0 |
| REQ-101 | `shared/ats_matcher.py` provides `compute_coverage(keywords, resume_text) -> {percent: float, matched: list[str], missing: list[str]}` with no LLM dependency | P0 |
| REQ-102 | Matching is case-insensitive, applies hand-rolled stem rules, and consults a hand-curated synonym table | P0 |
| REQ-103 | `Match_Results` sheet gains `ATS Coverage %`, `Recruiter Score`, `HM Score`, `ATS Missing` columns | P0 |
| REQ-104 | `Tailored_Match_Results` sheet gains per-dimension orig/tailored/delta columns (9 new) | P0 |
| REQ-105 | `COARSE_SYSTEM_PROMPT` renamed to `RECRUITER_SYSTEM_PROMPT`; `FINE_SYSTEM_PROMPT` to `HM_SYSTEM_PROMPT`; old names remain as aliases for one release | P0 |
| REQ-106 | Match agent computes ATS for **all** JDs, Recruiter for **all** JDs, HM gated by current threshold/top-N% logic (unchanged) | P0 |
| REQ-107 | Optimizer rescores all 3 dimensions on tailored resume; computes 3 deltas | P0 |
| REQ-108 | Regression flag = `HM delta < 0` only (not single-score delta); ATS / Recruiter regressions not flagged | P0 |
| REQ-109 | Excel migration: existing files auto-add new columns on next open; backfill `Regression` from existing Score Delta where possible; new ATS / Recruiter columns left blank for old rows (re-run match agent to populate) | P0 |
| REQ-110 | ATS coverage <30% surfaces as ⚠️ marker in summary printout; JD is **not** dropped from pipeline | P1 |
| REQ-111 | All 4 design decisions D1-D5 from status.md are reflected in code | P0 |
| REQ-112 | At least one regression test per design decision | P1 |

## 5. Dependencies & Constraints

**Depends on**:
- P0 branch (`fix/p0-review-2026-04-28`) merged to main, OR feature branch rebased after merge
- `gemini-3.1-flash-lite-preview` model availability for Recruiter/HM prompts
- Existing `JobDetails` extraction prompt structure in `agents/job_agent.py`

**Constraints**:
- Must not introduce new pip dependencies (no nltk / spacy) — keep matcher self-contained
- Must not break existing `pathfinder_dashboard.xlsx` files (auto-migration required)
- Must keep existing 619 tests passing
- Memory rule: ≤3-4 files per PR; 5-PR sequential split agreed in design discussion

## 6. Risks & Mitigation

See `status.md` Risk Register. Top risks:
- **R1 (LLM keyword drift)**: Mitigated by hand-curated synonym table + post-hoc review of "missing keyword" reports
- **R2 (Excel migration breaking data)**: Mitigated by additive-only column changes + backfill logic + dedicated migration tests
- **R3 (Rename breaking callers)**: Mitigated by keeping aliases for one release
