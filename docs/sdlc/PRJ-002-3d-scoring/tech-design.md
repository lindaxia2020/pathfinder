# Tech Design: 3-Dimension Scoring

**Project ID**: PRJ-002
**Author**: Engineer Lead
**Status**: Draft
**Date**: 2026-04-28

## 1. Architecture Overview

```
                    ┌──────────────────────────────────────┐
JD ingest           │ job_agent.py                         │
(Gemini)   ────────▶│   JobDetails Pydantic now includes:  │
                    │     - requirements                   │
                    │     - additional_qualifications      │
                    │     - key_responsibilities           │
                    │     - ats_keywords (NEW)             │
                    └──────────────────────────────────────┘
                                     │
                                     ▼
                    ┌──────────────────────────────────────┐
                    │ match_agent.py                       │
                    │                                      │
Resume     ────────▶│  ATS dim:        ats_matcher (det.)  │  ── coverage % + missing
                    │  Recruiter dim:  RECRUITER prompt    │  ── 1-100 LLM score
                    │  HM dim:         HM prompt (gated)   │  ── 1-100 LLM score
                    └──────────────────────────────────────┘
                                     │
                                     ▼
                    ┌──────────────────────────────────────┐
                    │ Match_Results sheet                  │
                    │   ATS Coverage % | Recruiter | HM    │
                    └──────────────────────────────────────┘
                                     │
                                     ▼
                    ┌──────────────────────────────────────┐
                    │ resume_optimizer.py                  │
                    │   tailor → rescore all 3 dims        │
                    │   delta_ats / delta_rec / delta_hm   │
                    │   regression = (delta_hm < 0)        │
                    └──────────────────────────────────────┘
```

## 2. Module Inventory

### New files
| File | Purpose | LOC est. |
|---|---|---|
| `shared/ats_matcher.py` | Deterministic keyword coverage matcher | ~120 |
| `shared/ats_synonyms.py` | Hand-curated synonym dict | ~40 |
| `tests/test_ats_matcher.py` | Stem / case / synonym / coverage edge cases | ~250 |

### Modified files (by PR)

**PR 1 — Foundation**
- `shared/schemas.py`: add `ATSCoverageResult`; `JobDetails` gains `ats_keywords: list[str] = Field(default_factory=list)` (default empty for backward-compat with cached JDs)
- `agents/job_agent.py`: extraction prompt instructs Gemini to populate `ats_keywords` (8-15 keywords, named entities only, no adjectives)
- `shared/ats_matcher.py` (NEW)
- `shared/ats_synonyms.py` (NEW)
- `tests/test_ats_matcher.py` (NEW)

**PR 2 — Excel + Prompts**
- `shared/excel_store.py`: extend `MATCH_HEADERS` (add ATS Coverage %, Recruiter Score, HM Score, ATS Missing); extend `TAILORED_HEADERS` (per-dim orig/tailored/delta); auto-migration in `get_or_create_excel`
- `shared/prompts.py`: rename `COARSE_SYSTEM_PROMPT → RECRUITER_SYSTEM_PROMPT`, `FINE_SYSTEM_PROMPT → HM_SYSTEM_PROMPT`; keep old names as aliases pointing to new constants
- `tests/test_excel_store.py`, `tests/test_shared_prompts.py`

**PR 3 — Match agent**
- `agents/match_agent.py`: add ATS pass before Stage 1; rename internal vars; write all 3 dims to Excel
- `tests/test_match_agent.py`

**PR 4 — Optimizer**
- `agents/resume_optimizer.py`: rescore 3 dims; switch regression rule to HM-delta-only; update `_print_summary`
- `tests/test_resume_optimizer.py`

**PR 5 — Docs**
- `CHANGELOG.md`, `ARCHITECTURE.md`, `REQUIREMENTS.md`
- `shared/prompts.py`: remove `COARSE_SYSTEM_PROMPT` / `FINE_SYSTEM_PROMPT` aliases (final cleanup)

## 3. Key Contracts

### `shared/ats_matcher.py`

```python
def normalize(token: str) -> str:
    """Lowercase + hand-rolled stem rules (-ing/-ed/-s/-es/-ies suffix removal)."""

def expand_synonyms(keyword: str) -> set[str]:
    """Expand a keyword to its synonym set; include the keyword itself."""

def compute_coverage(
    ats_keywords: list[str],
    resume_text: str,
) -> dict:
    """
    Returns:
      {
        "percent": float,                # matched / total * 100, rounded to 1 decimal
        "matched": list[str],            # original keywords that matched (de-duped)
        "missing": list[str],            # original keywords with no match
        "keyword_count": int,            # len(ats_keywords) after de-dup
      }
    Empty/None ats_keywords → percent=None signaling "no data" (legacy JD).
    """
```

### `shared/schemas.py` additions

```python
class ATSCoverageResult(BaseModel):
    percent: float | None
    matched: list[str]
    missing: list[str]
    keyword_count: int
```

### Excel column changes

`MATCH_HEADERS` evolves from:
```
[Resume ID, JD URL, Score, Strengths, Gaps, Reason, Updated At, Resume Hash, Stage]
```
to:
```
[Resume ID, JD URL, Score, Strengths, Gaps, Reason, Updated At, Resume Hash, Stage,
 ATS Coverage %, Recruiter Score, HM Score, ATS Missing]
```

`Score` column kept for backward-compat; will mirror `HM Score` (or `Recruiter Score` if no HM).

`TAILORED_HEADERS` adds 9 columns:
```
[..., Original ATS, Tailored ATS, ATS Delta,
      Original Recruiter, Tailored Recruiter, Recruiter Delta,
      Original HM, Tailored HM, HM Delta]
```

(Existing `Original Score`, `Tailored Score`, `Score Delta` mirror the HM dim.)

## 4. Migration Strategy

- Excel: `get_or_create_excel()` detects missing columns and appends + backfills (where derivable)
- Old cached JDs (`jd_cache/*.md`, `jd_cache/*_structured.md`): no `ats_keywords` field → matcher returns `percent=None`, downstream code treats as "no ATS data, run match agent on these JDs to repopulate"
- Old `Match_Results` rows: ATS columns blank; user re-runs match agent to populate
- Old `Tailored_Match_Results` rows: per-dim delta columns blank; user re-runs optimizer to populate

## 5. Testing Strategy

- **Unit**: `shared/ats_matcher.py` covered by `tests/test_ats_matcher.py` — case, stem, synonym, dedup, edge cases (empty, all matched, all missing)
- **Schema**: `JobDetails.ats_keywords` round-trips through Pydantic + JSON
- **Migration**: existing-file → new-schema migration test in `test_excel_store.py`
- **Integration**: match agent end-to-end test exercises 3-dim path with mock Gemini
- **Regression**: optimizer regression rule change — assert `regression=False` when ATS / Recruiter delta < 0 but HM delta >= 0; `regression=True` only when HM delta < 0
- All 619 existing tests must remain green; new tests bring total up to ~700

## 6. Sequencing & Branch Strategy

- Base branch: `feat/3d-scoring` (off `fix/p0-review-2026-04-28`; rebases onto `main` once P0 PR merges)
- Each PR commits on `feat/3d-scoring` sequentially, awaiting user review between
- Tests run after each PR commit; full suite must pass before next PR begins

## 7. Open Questions Resolved

| Question | Resolution |
|---|---|
| Where do ATS keywords come from? | Gemini extracts at JD ingest, stored in `JobDetails.ats_keywords` |
| Matching strategy? | Lowercase + hand-rolled stem + small synonym table; no nltk |
| Hard gate vs soft? | Soft: <30% gets ⚠️ marker, never drops |
| Coarse stage fate? | Renamed to Recruiter; kept logic; updated prompt language |
