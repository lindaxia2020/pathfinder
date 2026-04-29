# PRJ-002: 3-Dimension Scoring (ATS / Recruiter / HM)

**Phase**: Phase 1 — BRD
**Status**: 🟡 In Progress
**Priority**: P1
**Created**: 2026-04-28
**Last Updated**: 2026-04-28

## Task Checklist

### Phase 1: BRD
- [ ] PM researches and writes BRD
- [ ] User reviews BRD
- [ ] TPM reviews BRD
- [ ] Engineer Lead reviews BRD

### Phase 2: Tech Design
- [ ] Engineer Lead writes technical design
- [ ] User reviews technical design
- [ ] TPM reviews technical design

### Phase 3: Implementation
- [ ] Task decomposition and dependency ordering (TPM)
- [ ] PR 1 — Foundation: ATS matcher + JD schema (`shared/ats_matcher.py`, `shared/schemas.py`, `agents/job_agent.py`, `tests/test_ats_matcher.py`)
- [ ] PR 2 — Excel schema + prompt rename (`shared/excel_store.py`, `shared/prompts.py`, tests)
- [ ] PR 3 — Match agent 3-dim scoring (`agents/match_agent.py`, tests)
- [ ] PR 4 — Optimizer 3-dim rescore (`agents/resume_optimizer.py`, tests)
- [ ] PR 5 — Documentation + alias cleanup (CHANGELOG / ARCHITECTURE / REQUIREMENTS)
- [ ] Code self-testing passes (485+ tests)

### Phase 4: Testing & Bug Fix
- [ ] QA Team review
- [ ] PM functional acceptance
- [ ] Bug fixes completed
- [ ] QA sign-off
- [ ] PM sign-off

### Phase 5: Launch Readiness
- [ ] TPM launch readiness report
- [ ] User final sign-off
- [ ] Engineer Lead confirmation
- [ ] PM confirmation

## Risk Register

| ID | Risk Description | Impact | Probability | Mitigation | Status |
|----|------------------|--------|-------------|------------|--------|
| R1 | LLM-extracted ATS keywords drift over JDs (inconsistent quality) | Medium | Medium | Hand-curate synonym table; surface missing-keyword reports for review | Open |
| R2 | Excel schema migration breaks existing user data | High | Low | Auto-migrate on open with backfill logic + tests | Open |
| R3 | Coarse → Recruiter rename breaks downstream callers / tests | Medium | High | Keep aliases for 1 release; remove in PR 5 | Open |
| R4 | Stem rules without nltk too lossy → false misses on covered keywords | Medium | Medium | Start with hand-rolled rules; iterate based on observed misses | Open |
| R5 | P0 branch not merged to main → feat branch needs rebase | Low | High | Rebase feat/3d-scoring onto main once P0 PR merges | Open |

## Decision Log

| ID | Decision | Date | Decision Maker |
|----|----------|------|----------------|
| D1 | ATS as parallel dimension, not sequential stage | 2026-04-28 | User |
| D2 | LLM extracts `ats_keywords` in job_agent (not separate pass) | 2026-04-28 | User |
| D3 | Matching = exact + lowercase + hand-rolled stem + small synonym table (no nltk) | 2026-04-28 | User |
| D4 | Soft dimension: <30% coverage → ⚠️ flag, not drop | 2026-04-28 | User |
| D5 | Keep coarse stage, rename to "Recruiter Score"; HM = current FINE | 2026-04-28 | User |
| D6 | 5-PR sequential split (each PR ≤4 files) | 2026-04-28 | User |
| D7 | Feature branch `feat/3d-scoring` from `fix/p0-review-2026-04-28` | 2026-04-28 | User |

## Blockers

No current blockers.
