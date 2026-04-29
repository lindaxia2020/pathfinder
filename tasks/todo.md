# Task Tracker

## Active: PRJ-002 — 3-Dimension Scoring (ATS / Recruiter / HM)

**Branch**: `feat/3d-scoring`
**SDLC**: `docs/sdlc/PRJ-002-3d-scoring/`
**Started**: 2026-04-28

### PR 1 — Foundation: ATS matcher + JD schema
- [ ] `shared/ats_synonyms.py` — hand-curated synonym dict (≤20 entries to start)
- [ ] `shared/ats_matcher.py` — `normalize()`, `expand_synonyms()`, `compute_coverage()`
- [ ] `shared/schemas.py` — add `ATSCoverageResult`; extend `JobDetails` with `ats_keywords`
- [ ] `agents/job_agent.py` — extraction prompt instructs Gemini to populate `ats_keywords`
- [ ] `tests/test_ats_matcher.py` — case / stem / synonym / coverage edge cases
- [ ] Run full test suite; verify 619+ pass
- [ ] Commit + push

### PR 2 — Excel + prompt rename (NOT STARTED)
### PR 3 — Match agent 3-dim scoring (NOT STARTED)
### PR 4 — Optimizer 3-dim rescore (NOT STARTED)
### PR 5 — Documentation + alias cleanup (NOT STARTED)

## Open lessons
See `tasks/lessons.md` (created when first lesson lands).

## Completed
*(none yet)*
