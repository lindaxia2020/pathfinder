# PathFinder — System Architecture Document

---

## 1. System Overview

PathFinder is a multi-agent AI system for TPM job seekers, consisting of four independently runnable Agents that collaborate through shared local Excel files, completing the full pipeline of AI company discovery → job scraping → resume matching → resume tailoring and optimization.

**Run mode**: Local command line (`python agents/<agent>.py`), no Colab or cloud platform required
**Key dependencies**: Gemini LLM, Tavily search, Crawl4AI/Playwright, local openpyxl Excel

---

## 2. System Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        PathFinder                               │
│                                                                 │
│  ┌────────────────┐ ┌────────────────┐ ┌─────────────┐ ┌──────────────────┐│
│  │ company_agent  │ │  job_agent     │ │ match_agent │ │resume_optimizer  ││
│  │                │ │                │ │             │ │                  ││
│  │· Company       │ │· Path A (ATS)  │ │· Resume     │ │· Resume tailoring││
│  │  discovery     │ │· Path B        │ │  loading    │ │  and rewriting   ││
│  │· Career URL    │ │  (crawler)     │ │· ATS cover. │ │· Re-scoring      ││
│  │  finding       │ │· JD extraction │ │· Recruiter  │ │  verification    ││
│  │· ATS URL       │ │  + caching     │ │  scan(batch)│ │· Score comparison││
│  │  upgrade       │ │                │ │· HM eval    │ │                  ││
│  │                │ │                │ │  (UNION 60%)│ │                  ││
│  └───────┬────────┘ └───────┬────────┘ └──────┬──────┘ └────────┬─────────┘│
│          │                  │                  │                 │          │
│          └─────────┬────────┘                  └────────┬───────┘          │
│                    │                                    │                  │
│           ┌──────────▼───────────────────────────────▼──────┐   │
│           │          shared/excel_store.py                   │   │
│           │  (pathfinder_dashboard.xlsx — local Excel)        │   │
│           └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Agent Details

### 3.1 Company Agent (`agents/company_agent.py`)

**Run frequency**: One-time or on-demand
**Responsibilities**: Discover North American AI companies, find valid Career URLs, upgrade to ATS board URLs

**Internal flow**:
```
1. Discovery loop (run_discovery_loop) — repeats until every track bucket is
   at quota (500 total), a batch yields nothing new (Tavily quota exhausted /
   query-pool convergence), or the MAX_TOTAL/BATCH_SIZE iteration cap fires.
   Per iteration:
     a. Re-read existing companies (Company_List + Company_Without_TPM) → exclusion list
     b. compute_need_by_track → allocate_batch (≤ BATCH_SIZE across open buckets)
     c. Tavily batch search (14 track-biased queries)
     d. Gemini LLM extracts company info (name, track, business focus,
        confident flag — BUG-76), does not generate URLs
     e. Deterministic bucket rules (defense-prime exclusion, quota trim);
        unconfident extractions then get Track/Business Focus blanked so the
        confident-gated repair steps (3/4 below) refill them (BUG-76)
     f. Per-company multi-strategy Career URL finding:
          ① KNOWN_CAREER_URLS hardcoded → ② Tavily ATS-targeted search
          → ③ Tavily general search → ④ Greenhouse/Lever/Ashby/Workable slug probing
          → ⑤ Homepage crawling
        Companies without a found URL are skipped (not written)
        URL-quality gates (BUG-74/75/77) applied on every strategy:
          - job-posting URLs rejected / stripped to their board root
            (_is_job_posting_url / _posting_url_to_board_root)
          - aggregator hosts rejected (_AGGREGATOR_HOSTS suffix blocklist;
            jobs.gem.com deliberately allowed — real ATS)
          - slug-probe hits must pass the board-OWNERSHIP check
            (_ats_board_belongs_to_company: org name via ATS API/board title,
            equality-only matching generalized from the Workday matcher)
          - homepage guesses must identify as the company
            (_homepage_belongs_to_company: <title>/og:site_name)
     g. Upsert batch to Company_List
2. Phase 1.5: Backfill blank Career URLs on manually-inserted rows (Company
     Name present, Career URL blank) via the full find_career_url pipeline.
     FILLED Career URLs are user-verified data and are NEVER rewritten
     (BUG-74/75 write guard; pre-fix this step probe-and-overwrote every
     filled non-ATS URL). URL findings for filled rows: report-only --audit.
3. Business Focus re-enrich (BUG-69): fill blank/N-A Business Focus cells.
4. Track enrich (run_enrich_missing_tracks): fill blank Track cells via the
     shared batched Gemini classifier; unconfident rows stay blank for retry;
     custom values (incl. "UNMIGRATED — manual review") untouched.
5. Sort Company_List by Track (sort_company_list_by_track, canonical
     TRACK_ORDER; unknown tracks sink last). Always the final step — earlier
     steps write by captured excel_row, which sorting invalidates.
```

**Key constants**:
- `MAX_TOTAL = 500` (sum of per-track quotas, `TRACK_QUOTAS`)
- `BATCH_SIZE = 50` (new companies per discovery-loop iteration)
- `TRACK_ORDER` (`shared/config.py`): canonical 6-bucket order shared by
  company_agent and the excel_store sorter

**Staged audit mode (`--audit`, BUG-74~77)**: report-only review of every
Company_List row — batched Gemini re-evaluation of Track/Business Focus
(temperature 0, do-not-guess confidence gates, valid-names filter; zero
Tavily) plus URL health flags (`POSTING_URL(→root)` / `AGGREGATOR` /
`OWNERSHIP_MISMATCH` / `HTTP_FAIL` / informational `NON_ATS`). Results land
in the transient **`Company_Audit`** tab (`replace_audit_sheet`:
create-or-replace, never auto-created by `get_or_create_excel`); Company_List
is never written. Flags: `--audit-limit N` (dry run), `--audit-skip-http`.
**`--suggest-urls`** (with `--audit`) additionally looks up a replacement
URL for rows with actionable flags (NON_ATS rows only via
`--suggest-non-ats`; `--suggest-limit N` caps Tavily spend), cheapest-first:
posting-root strip → known table → ownership-gated ATS slug probe → Tavily →
identity-gated homepage. Suggestions land in Suggested URL / URL Evidence /
Approve URL? columns.

After user review, **`--apply-audit`** applies the tab's proposed Track/
Business Focus values and — only where the user set "Approve URL?" to Y —
the suggested Career URL (the sole sanctioned URL write outside blank-row
backfill). Matched by Company Name (never excel_row: sorts and user
deletions shift rows); Names never written; invalid buckets and
since-deleted companies skipped; canonical Track sort last.

---

### 3.2 Job Agent (`agents/job_agent.py`)

**Run frequency**: Roughly weekly
**Responsibilities**: Discover TPM positions from company career pages, extract structured JDs, cache locally

**Internal flow**:
```
Path A (ATS API, for Greenhouse/Lever/Ashby):
  → Call public JSON API to get full job listings (no auth required)
  → Local filtering: job title contains TPM keywords + location in North America

Path B (Crawler, for non-standard URLs):
  → Firecrawl map / Crawl4AI + Playwright JS rendering
  → If ATS signatures detected from links → switch to Path A strategy
  → Otherwise parse crawl results to extract job links

JD extraction (per job URL):
  → Crawl4AI fetches JD page → convert to Markdown → cache to jd_cache/{md5}.md
  → Soft 404 detection (skip closed positions)
  → Gemini extracts structured fields (job title/company/location/salary/tech stack/responsibilities/is AI TPM)
  → Generate content MD5 hash (deduplication & change detection)
  → If hash unchanged and within 5 days → update timestamp only, skip re-extraction
  → Batch upsert to JD_Tracker

On completion: Update TPM Jobs / AI TPM Jobs counts in Company_List for each company
```

**Key constants**:
- `FRESH_DAYS = 5` (JD refresh interval)
- JD cache directory: `jd_cache/` (named by URL MD5)

---

### 3.3 Match Agent (`agents/match_agent.py`)

**Run frequency**: After each resume update
**Responsibilities**: Score all AI TPM JDs against the candidate's resume on three parallel dimensions (PRJ-002).

**Three scoring dimensions** (PRJ-002 / 2026-04-28):
| Dimension | Source | Cost | Maps to real-world filter |
|---|---|---|---|
| ATS Coverage % | Deterministic keyword match (`shared/ats_matcher.py`) | Free, no LLM | ATS / recruiter keyword search pass-through |
| Recruiter Score (1-100) | Gemini, `RECRUITER_SYSTEM_PROMPT` (was COARSE) | 1 batch call per 10 JDs | Recruiter 30-second resume scan |
| HM Score (1-100) | Gemini, `HM_SYSTEM_PROMPT` (was FINE), 4 weighted criteria | 1 single call per JD | Hiring manager deep evaluation |

**Internal flow**:
```
1. Load resume from profile/ (.md / .txt / .pdf — picker priority in that order; PDF auto-converted via `shared/resume_io.py`), compute resume MD5
2. Read all valid JDs from JD_Tracker (every written row is domain-qualified — PRJ-004; each carries its Job Domain for per-track prompt routing)
3. Detect stale pairs (resume hash changed) → mark for re-scoring

ATS dim (PRJ-002, runs first, no LLM):
  → For each pending JD: parse ats_keywords from cached JobDetails JSON
  → compute_coverage(ats_keywords, resume_text) → {percent, matched, missing}
  → Coverage <30% prints with ⚠️ marker (soft signal, JD NOT dropped)

Stage 1 — Recruiter scoring:
  Batch scoring: each Gemini call processes 10 JDs → returns 1-100 score
  Records written as dicts to Match_Results with stage=coarse and per-dim
  values populated (ATS Coverage %, Recruiter Score, HM Score=None)

Stage 2 — HM evaluation (UNION of threshold + top-N%):
  Selection = (score >= MATCH_FINE_SCORE_THRESHOLD, default 60) ∪
              (top MATCH_FINE_TOP_PERCENT% of run, default 60%)
  Concurrent evaluation (max 3 concurrent, 13 RPM rate limit):
    → Prefer reading Markdown from jd_cache/, otherwise use JD JSON
    → Gemini scores on 4 weighted dimensions (AI tech depth 30% / TPM fit 30% / Domain 20% / Growth 20%)
    → Results include: score / strengths / gaps / recommendation reason
  Records written with stage=fine and ONLY hm_score key — the upsert's
  "key absent → preserve" semantic keeps Stage 1's ATS / Recruiter values intact

4. Console outputs Top 5 matches (★=HM evaluated / ~=Recruiter only)
```

**Key constants**:
- `ATS_COVERAGE_LOW_THRESHOLD = 30.0` — soft ⚠️ flag threshold for ATS dim
- `MATCH_FINE_SCORE_THRESHOLD = 60` (env-overridable) — absolute gate for HM eval
- `MATCH_FINE_TOP_PERCENT = 60.0` (env-overridable) — relative gate for HM eval

---

### 3.4 Resume Optimizer Agent (`agents/resume_optimizer.py`)

**Run frequency**: After Match Agent completes
**Responsibilities**: Tailor the resume for each matched JD, re-score on all 3 dimensions to verify improvement (PRJ-002).

**Internal flow**:
```
1. Load resume from profile/ (.md / .txt / .pdf via `shared/resume_io.py`), compute MD5
2. Read fine-stage matches from Match_Results (stage="fine"), including
   per-dim originals (ATS Coverage %, Recruiter Score, HM Score) via the
   PR 4 extension to get_scored_matches
3. Read existing optimized records from Tailored_Match_Results, skip pairs
   where resume_hash is unchanged

Phase 1 — Batch tailor:
  Batch size 2 — Gemini generates tailored resume (reorders / emphasizes /
  mirrors JD keywords; never fabricates new experience). Save to
  tailored_resumes/{resume_id}/{url_md5}.md AND a sibling .pdf rendered via
  WeasyPrint (ATS-safe CSS, PRJ-003). Per-item fallback if batch failed.
  User-edit protection: if the on-disk .md sha256 disagrees with the last
  recorded `Tailored_Match_Results.Last Written Hash`, the write is skipped
  and the pair is dropped from the rescore + Excel update so the row stays
  aligned with the user's hand-edited file (--force-rewrite bypasses).

Phase 2 — 3-dimension rescore (per JD, concurrency=3):
  ├── ATS:       compute_coverage(ats_keywords, tailored_md) — deterministic, no LLM
  ├── Recruiter: batch_coarse_score(tailored_md, [jd_dict]) — 1 Gemini call (cross-agent)
  └── HM:        re_score(tailored_md, jd_content) — 1 Gemini call, HM_SYSTEM_PROMPT
  Per-JD prints: ATS o→t (Δ) | Rec o→t (Δ) | HM o→t (Δ)

Phase 3 — Assemble + write:
  9 per-dim record keys + legacy mirroring (Original Score = Original HM, etc.)
  Regression flag = (HM Delta < 0) only — ATS / Recruiter drops are info,
  not regressions (REQ-108)
  Batch upsert to Tailored_Match_Results

4. Console outputs summary table sorted by Score Delta
```

**Cost model** (per N tailored JDs):
- Tailor: ⌈N/2⌉ Gemini batch calls + per-item fallbacks
- Recruiter rescore: N Gemini calls (1 per JD)
- HM rescore: N Gemini calls (1 per JD)
- ATS rescore: 0 calls (deterministic)
- Total: ~2.5N Gemini calls, where pre-PRJ-002 was ~1.5N

**Concurrency model**:
- `BATCH_TAILOR_SIZE=2` (tailor batches), `RESCORE_CONCURRENCY=3` (rescore concurrent)
- `_RateLimiter(rpm=13)` shared rate limiter (across both Recruiter and HM Phase 2 calls)
- Cross-agent key pool sharing: optimizer's `main` propagates `_KEY_POOL` into the `match_agent` module so `batch_coarse_score` (used for Recruiter rescore) has a pool

---

## 4. Shared Persistence Layer (`shared/excel_store.py`)

**File**: `pathfinder_dashboard.xlsx` (project root)

### Excel Worksheet Structure

| Worksheet | Primary Key | Fields |
|-----------|-------------|--------|
| `Company_List` | Company Name | Company Name, **Track** (6-bucket taxonomy — PRJ-004), Business Focus, Career URL, Updated At, TPM Jobs, **Qualified Jobs** (PRJ-004), No TPM Count, Auto Archived |
| `Company_Without_TPM` | Company Name | Company Name, Track, Business Focus, Career URL, Updated At, TPM Jobs, Qualified Jobs |
| `JD_Tracker` | JD URL | JD URL, Job Title, Company, Location, Salary, Requirements, Additional Qualifications, Responsibilities, **Job Domain** (AI/Robotics/Fintech/Space/Defense — PRJ-004, replaces Is AI TPM), Updated At, MD Hash, Data Quality, **ATS Keywords** (8-15 per JD, extracted once at ingest — PRJ-002), **Sort Tier** (combined 1–6 freshness×region index, 9 = unknown/aged/other sink — PRJ-004, replaces Location Tier), **Posted Date, Freshness Tier, Min YoE, YoE Flag, Work-Auth Status, Date Flag** (PRJ-004) |
| `Match_Results` | Resume ID + JD URL | Resume ID, JD URL, Score, Strengths, Gaps, Reason, Updated At, Resume Hash, Stage, **ATS Coverage %, Recruiter Score, HM Score, ATS Missing** (PRJ-002) |
| `Tailored_Match_Results` | Resume ID + JD URL | Resume ID, JD URL, Job Title, Company, Original Score, Tailored Score, Score Delta, Tailored Resume Path, Optimization Summary, Updated At, Resume Hash, Regression, **Original ATS, Tailored ATS, ATS Delta, Original Recruiter, Tailored Recruiter, Recruiter Delta, Original HM, Tailored HM, HM Delta** (PRJ-002), **Last Written Hash** (sha256 of last-written .md — used by optimizer to detect user hand-edits) |
| `JD_ToApply` / `Skipped JD` | JD URL | **User triage tabs (BUG-65, `TRIAGE_SHEETS`)** — same columns as JD_Tracker. User-owned: the user moves reviewed JD_Tracker rows here; `get_triaged_jd_urls()` feeds a permanent exclusion so these URLs are never re-scraped or re-added. Pipeline never writes/deletes rows here (auto-creates the empty tabs only). ⚠️ Renaming the tabs disables the exclusion. |

### Key Design

- **Write operation pattern**: Each time load → modify → save (single-process safe, job_agent wraps with asyncio.Lock)
- **Batch write**: `batch_upsert_jd_records`, `batch_upsert_match_records` (single save, reduced I/O)
- **Dynamic column mapping**: `_JD_COL = {h: i+1 for i, h in enumerate(JD_HEADERS)}`, JD_Tracker read functions look up column index by field name
- **Archive management**: `get_archived_companies()`, `update_archive_status()`, `unarchive_company()`, `get_company_archive_info()`, `count_valid_tpm_jobs_by_company()`
- **Schema migration**: Automatically detects and adds missing columns on startup (backward compatible with historical data)
- **Corruption recovery**: Automatically renames corrupted files to `.bak` and recreates
- **Cross-process safety (BUG-73)**: `shared/run_lock.py` — every agent entry point holds an exclusive `flock` on `logs/pathfinder.lock` for its whole run; a second agent fails fast naming the holder, or queues with `PATHFINDER_LOCK_WAIT=1` (exported by the scheduled pipeline). Kernel releases the lock on any exit — no stale locks after a crash
- **Snapshot reads (BUG-73)**: all `read_only` workbook loads go through `load_workbook_readonly` — file bytes copied to memory before parsing (a concurrent in-place `wb.save()` can't crash a reader mid-stream; half-written files retried 3×)

---

## 5. External Service Dependencies

| Service | Purpose | Auth | Key Limitations |
|---------|---------|------|-----------------|
| Tavily API | AI company web search, Career URL search, posting-date backfill | `TAVILY_API_KEY` + optional `TAVILY_API_KEY_2` | Limited free quota. `shared/tavily_pool.py` (BUG-70) rotates keys on 402/429/"usage limit"; when all keys are exhausted it prints one visible warning and every call raises `TavilyQuotaExhausted` (message contains "429"/"quota" so BUG-44 call-site abort checks work unchanged — search has no free fallback). Non-quota errors re-raise to the call site. |
| Google Gemini (`gemini-3.1-flash-lite`) | Company extraction, JD structuring, coarse screening, fine evaluation | `GEMINI_API_KEY` / `GEMINI_API_KEY_2` | Supports multi-key rotation; 13 RPM rate control |
| Greenhouse Public API | ATS job listings | No auth required | Public endpoint |
| Lever Public API | ATS job listings | No auth required | Public endpoint |
| Firecrawl | Web crawling/Map | `FIRECRAWL_API_KEY` + optional `FIRECRAWL_API_KEY_2` | Credit quota. `shared/firecrawl_pool.py` (BUG-68) rotates keys on 402/429; when all keys are exhausted it prints one visible warning and all calls return None instantly (callers fall back to crawl4ai/requests). Non-quota errors re-raise to the call site. |
| Ashby Public API | ATS job listings (added in REQ-058) | No auth required | Public endpoint: `https://api.ashbyhq.com/posting-api/job-board/{slug}` |
| Crawl4AI + Playwright | JS-rendering crawler | None | Async only, requires local Chromium |

---

## 6. Development Tool Layer & SDLC Workflow

PathFinder's development is organized as four agent groups under the user (Architect / Product Owner), coordinated through the SDLC workflow, forming a complete AI-assisted development cycle. Each group authors its own deliverables (requirements & milestones / code / test plans & reviews / eval reports); the Claude Code main thread alone holds code write access and serves as the runtime harness dispatching the other groups:

```
┌──────────────────────────────────────────────────────────────┐
│              User (Architect / Product Owner)                 │
└──────────────────────┬───────────────────────────────────────┘
                       │ Architecture, scope & review decisions
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Claude Code Main Thread (Engineer Lead / Implementation —    │
│  sole code write access; dispatches the groups below)         │
├──────────────┬────────────────────┬─────────────┬────────────┤
│              │                    │             │            │
│ Planning     │   Quality          │ Evaluation  │ Operations │
│ Group        │   Group            │ Group       │ (Skills)   │
│              │                    │             │            │
│ product-     │ agent-reviewer     │ eval-       │ /pipeline  │
│ manager      │ schema-validator   │ engineer    │ /run-agent │
│ (sonnet)     │ test-analyzer      │ observ-     │ /test-all  │
│ tpm (opus)   │ api-debugger       │ ability     │ /test-one  │
│              │ doc-sync           │ cost        │ /check-env │
│ /sdlc-init   │ bug-tracker        │             │            │
│ /sdlc-status │                    │             │            │
│ /sdlc-review │                    │             │            │
│              │                    │             │            │
│ Agent+Skill  │ Agent              │ Agent       │ Skill      │
└──────────────┴────────────────────┴─────────────┴────────────┘
```

### SDLC Workflow (`docs/sdlc/`)

Projects go through 5 phases from requirements to launch, coordinated by the TPM Agent:

```
Phase 1: BRD       → User provides goals → PM writes BRD → User + TPM + Engineer Lead review
Phase 2: Design    → Engineer Lead writes tech design → User + TPM review
Phase 3: Implement → TPM plans dependency order → Engineer Lead develops → TPM coordinates on blockers
Phase 4: Testing   → TPM notifies QA + PM to test → bug report → Engineer Lead fixes → sign-off
Phase 5: Launch    → TPM writes launch assessment → User + Engineer Lead + PM review → complete
```

Each project's documents are located in `docs/sdlc/PRJ-xxx-<name>/`, anchored by `status.md` (single source of truth); stage artifacts (`brd.md`, `tech-design.md`, test plans, launch assessment, `reviews/`) are added as each phase completes, so the artifact set varies by project.

Escalation mechanism:
- L1 Info → Engineer Lead
- L2 Decision → TPM + Engineer Lead discussion
- L3 Business (`[ESCALATE]`) → User
- L4 Blocker (`[BLOCKED]`) → User + Engineer Lead

### Custom Agents (`.claude/agents/`) — Planning / Quality / Evaluation groups, no Edit/Write tools

Each group authors its own deliverables; drafts are persisted via the Claude Code main thread, which alone holds code write access.

| Agent | Model | Group | Responsibilities |
|-------|-------|-------|------------------|
| `product-manager` | sonnet | Planning | Requirements analysis, BRD writing, progress tracking, impact assessment, testing sign-off |
| `tpm` | opus | Planning | Task decomposition, cross-team coordination, risk management, progress reporting, launch assessment |
| `agent-reviewer` | opus | Quality | Review code quality, prompt design, cross-agent consistency |
| `schema-validator` | sonnet | Quality | Validate Excel schema and inter-agent data contracts |
| `test-analyzer` | sonnet | Quality | Analyze test failure causes, identify coverage blind spots |
| `api-debugger` | sonnet | Quality | Debug Gemini/Tavily/Firecrawl/ATS API issues |
| `doc-sync` | sonnet | Quality | Detect drift between code and docs (REQ/ARCH/BUGS/CHANGELOG) |
| `bug-tracker` | sonnet | Quality | Manage BUGS.md: verify status, scan for new bugs, suggest regression tests |
| `eval-engineer` | sonnet | Evaluation | AI output quality evaluation: scoring calibration, prompt regression detection, hallucination detection |
| `observability` | sonnet | Evaluation | Pipeline run reporting, output quality drift detection, anomaly alerting |
| `cost` | sonnet | Evaluation | API token usage estimation, quota monitoring, cost optimization recommendations |

### Skills (`.claude/skills/`) — Operational execution

| Skill | Group | Purpose |
|-------|-------|---------|
| `/sdlc-init` | Planning | Initialize SDLC project (assign ID, create directory and template files) |
| `/sdlc-status` | Planning | View project status (single project details or global overview) |
| `/sdlc-review` | Planning | Trigger stage-specific reviews (BRD/Design/Testing/Launch) |
| `/pipeline` | Operations | Run complete pipeline of all 4 Agents in sequence |
| `/run-agent` | Operations | Run a single specified Agent |
| `/test-all` | Operations | Run full test suite |
| `/test-one` | Operations | Run tests for a specified module |
| `/check-env` | Operations | Verify API key configuration |

---

## 7. Local File Structure

```
pathfinder/
├── agents/                    # 4 Runtime Agents (the product)
│   ├── company_agent.py       # Company discovery + ATS URL upgrade
│   ├── job_agent.py           # Job discovery + JD extraction
│   ├── match_agent.py         # Resume matching (3-dim: ATS / Recruiter / HM)
│   └── resume_optimizer.py    # Resume tailoring optimization + re-scoring
├── shared/                    # Cross-agent shared modules
│   ├── __init__.py
│   ├── excel_store.py         # Unified Excel persistence layer
│   ├── gemini_pool.py         # Gemini API key rotation base class (client caching, round-robin, thread-safe)
│   ├── firecrawl_pool.py      # Firecrawl key pool — 402/429 rotation + loud exhaustion warning (BUG-68)
│   ├── rate_limiter.py        # Token-bucket async rate limiter
│   ├── config.py              # Shared constants (MODEL, AUTO_ARCHIVE_THRESHOLD)
│   ├── prompts.py             # System prompts (RECRUITER, HM, TAILOR)
│   ├── schemas.py             # Pydantic response schemas for Gemini
│   ├── exceptions.py          # GeminiTransientError / GeminiStructuralError
│   ├── run_summary.py         # Structured per-run log dataclass
│   ├── ats_matcher.py         # Deterministic ATS keyword coverage (PRJ-002)
│   ├── ats_synonyms.py        # Hand-curated ATS keyword synonym table (PRJ-002)
│   └── resume_io.py           # Unified resume loader (.md/.txt/.pdf) + MD→PDF render (PRJ-003)
├── templates/                 # PDF rendering assets (PRJ-003)
│   └── resume.css             # ATS-safe CSS for tailored-resume PDF output
├── scripts/                   # Operational scripts
│   ├── run_pipeline_scheduled.sh   # Daily full-pipeline runner (launchd)
│   ├── com.pathfinder.daily.plist  # Sample launchd job definition
│   ├── setup_schedule.md      # launchd scheduling setup guide
│   └── audit_subset_run.py    # One-off 3-company subset run for self-audit validation
├── tests/                     # Unit tests (859+ cases)
├── docs/
│   └── sdlc/                  # SDLC project documents
│       ├── index.md           # Project index table
│       └── PRJ-xxx-<name>/    # Per-project directory
│           ├── status.md      # Project status (single source of truth)
│           ├── brd.md         # Business Requirements Document
│           ├── tech-design.md # Technical Design Document
│           ├── ...            # Other stage artifacts (test plans, launch assessment — set varies by project)
│           └── reviews/       # Review records
├── .claude/
│   ├── agents/                # 11 Custom Agents (Planning / Quality / Evaluation groups, no Edit/Write tools)
│   │   ├── product-manager.md # Planning: requirements analysis, BRD, testing sign-off
│   │   ├── tpm.md             # Planning: task decomposition, risk management, launch assessment
│   │   ├── agent-reviewer.md
│   │   ├── schema-validator.md
│   │   ├── test-analyzer.md
│   │   ├── api-debugger.md
│   │   ├── doc-sync.md
│   │   ├── bug-tracker.md
│   │   ├── eval-engineer.md    # Quality: scoring calibration, prompt regression, hallucination detection
│   │   ├── observability.md    # Quality: run reporting, drift detection, anomaly alerting
│   │   └── cost.md             # Quality: token estimation, quota monitoring, optimization recommendations
│   └── skills/                # 8 Skills (coordination + operations)
│       ├── sdlc-init/         # Planning: SDLC project initialization
│       ├── sdlc-status/       # Planning: project status viewing
│       ├── sdlc-review/       # Planning: stage-specific reviews
│       ├── pipeline/
│       ├── run-agent/
│       ├── test-all/
│       ├── test-one/
│       └── check-env/
├── profile/                   # Candidate resume directory (.md / .txt / .pdf — picker priority in that order)
│   └── .cache/                # PDF→MD conversion cache (auto-created, deterministic, hash-keyed)
├── jd_cache/                  # JD Markdown local cache (auto-created)
├── tailored_resumes/          # Tailored resume output (.md + .pdf per JD, subdirs by resume_id)
├── logs/                      # Pipeline logs (auto-created by launchd runner, gitignored)
├── pathfinder_dashboard.xlsx  # Main data file (auto-created)
├── .env                       # API Keys (not committed)
├── README.md                  # Setup, usage, and team overview
├── PROJECT_OVERVIEW.md        # Curated technical overview for recruiters/reviewers
├── CLAUDE.md                  # Development guide
├── REQUIREMENTS.md            # Requirements tracking (130+ REQ + DEC entries)
├── ARCHITECTURE.md            # System architecture (this document)
├── BUGS.md                    # Bug records
├── CHANGELOG.md               # Change log
└── venv/                      # Python 3.11 virtual environment
```

---

## 8. Architecture Change History

| Version | Date | Changes | Reason |
|---------|------|---------|--------|
| v5 (draft) | Before 2026-03 | Monolithic Colab Notebook, dependent on Google Sheets and Google Drive | Rapid prototype validation |
| v1.0 | 2026-03-12 | Refactored into 3 independent Agents + local Excel persistence; removed Colab/Google Sheets dependencies; added two-stage matching, local JD caching, Gemini key rotation, rate limiting | Break free from Colab constraints, suitable for continuous local execution; reduce API costs (batch coarse screening) |
| v1.1 | 2026-03-16 | Added 4th Agent (resume_optimizer): tailors resume for each matched JD and re-scores; added Tailored_Match_Results worksheet; Excel persistence layer gained 3 new functions | Simulate real job-seeking best practice of tailoring resumes, automated with LLM and quantitatively verified via scores |
| v1.2 | 2026-03-16 | Added development tool layer: 7 Custom Agents (product-manager, agent-reviewer, schema-validator, test-analyzer, api-debugger, doc-sync, bug-tracker) + 5 Skills (pipeline, run-agent, test-all, test-one, check-env). Architecture document added Section 6 "Development Tool Layer". | Establish AI-assisted development cycle: Planning layer (PM) → Quality layer (6 analysis agents) → Operations layer (5 execution skills), with TPM as sole decision maker |
| v1.3 | 2026-03-16 | Added coordination layer: TPM Agent (opus) + 3 SDLC Skills (sdlc-init, sdlc-status, sdlc-review) + `docs/sdlc/` project document directory. PM Agent gained BRD writing and testing sign-off modes. Established 5-phase SDLC workflow (BRD → Design → Implement → Testing → Launch). | Simulate real team SDLC: User only provides high-level goals, PM, TPM, Engineer Lead, and QA Team collaborate to complete the full process from requirements to launch; document-driven inter-agent communication |
| v1.4 | 2026-03-16 | Comprehensive code audit fixing 55 bugs (BUG-01~55); Job Agent enhancements: Ashby upgraded to API_ATS (REQ-058), soft 404 hardening + JD positive validation (REQ-059), JD field completeness grading (REQ-060), Workday URL format expansion (REQ-061); ATS declarative routing table refactoring (REQ-062); auto-archiving companies with no TPM positions (REQ-063); `shared/gemini_pool.py` refactored to unified base class (`_GeminiKeyPoolBase`) with client caching, round-robin rotation, thread safety; `shared/excel_store.py` added `_JD_COL` dynamic column mapping and 5 archive management functions; JD_Tracker schema added Requirements/Additional Qualifications/Data Quality columns; Company_List added No TPM Count/Auto Archived columns. Tests grew from ~120 to 485. | Comprehensive code quality and robustness improvement; ATS extensibility; data quality observability |
| v1.5 | 2026-03-17 | Added Observability Agent (run reporting, quality drift detection, anomaly alerting) and Cost Agent (token usage estimation, quota monitoring, cost optimization recommendations). Custom Agents grew from 9 to 11. | Runtime quality monitoring and cost governance capabilities specific to AI projects, filling gaps in traditional SDLC for AI dimensions |
| v1.6 | 2026-04-28 | **PRJ-002: 3-Dimension Scoring**. Restructured the resume-fit scoring pipeline from a single LLM-derived "fit score" into three parallel dimensions: ATS Coverage (deterministic, `shared/ats_matcher.py`), Recruiter Score (Gemini, was COARSE), HM Score (Gemini, was FINE). New `JobDetails.ats_keywords` field; `Match_Results` +4 cols; `Tailored_Match_Results` +9 cols (auto-migrated). Optimizer rescores all 3 dims; regression flag now means HM Delta < 0 only (was: legacy single-score delta). Tests 619 → 718 (+99 across 5 sequential PRs). Plus P0 follow-ups: P0-9 Stage 2 UNION selection, P0-10 single-JD rescore parity, P0-11 Gemini transient backoff retry, P0-12 persisted Regression column. | Existing single-score Score Delta conflated keyword gains with semantic strength; users had no signal on which one moved. Mapping each dimension to one real-world hiring filter (ATS keyword search → recruiter scan → HM deep eval) makes the system's output match the actual North American funnel. |
| v1.7 | 2026-05-20 | **PRJ-003 + operational hardening**. (a) PDF resume I/O: `shared/resume_io.py` centralizes `load_resume` across match + optimizer; supports `.md/.txt/.pdf` input via `pdfplumber` (deterministic, layout-aware, cached at `profile/.cache/`); every tailored `.md` also rendered as a sibling `.pdf` via WeasyPrint with ATS-safe CSS (`templates/resume.css`). (b) Discovery coverage: Workable as first-class ATS, LinkedIn/VC-portfolio URL unwrapper, Workday-via-Tavily fallback with strict subdomain-equality guard (26 cos recovered). (c) Manual-entry override: `run_phase_1_5` backfills Career URL on hand-inserted `Company_List` rows. (d) Tailored-resume user-edit protection via sha256 stored in `Tailored_Match_Results.Last Written Hash`. (e) `JD_Tracker` auto-sort by Location Tier (Greater Seattle / Remote / Other) with new `Location Tier` column. (f) launchd-based daily pipeline runner under `scripts/`. (g) Gemini model name → GA (`gemini-3.1-flash-lite`, drop `-preview`). Tests: 749 → 859 (+110). | PRJ-003 closes the resume-format gap (real submissions are PDFs, not Markdown). Manual-entry + URL unwrapper unblocks the user dropping in a target list without per-company ATS hunting. User-edit protection prevents the daily launchd runner from silently clobbering hand-polished resumes. Location-tier sort matches the user's actual review workflow. |
| v2.2 (current) | 2026-08-19 | **Filter widening: titles + WA geo**. Title filter: `TPM_KW` gains "Technical Project Manager" (+ variants) on all tracks; new `PM_TITLE_OK_TRACKS` (AI-native/Robotics/Space/Defense) additionally accepts plain "Program Manager" in `_tpm_filter` (track threaded `process_company → discover_jobs → _discover_via_api`) and in the `llm_filter_jobs` rules (three-way split: PM-OK vertical / Fintech / mid-large). Server-side queries: Workday `searchText` + Firecrawl map broadened to "Program Manager"; Amazon/Google deliberately kept narrow (pagination caps). Geo: `classify_region` region "Seattle" → "WA" — any `", WA"` state token (word-boundary regex) or explicit Washington-state form; keep-set now WA/CA/TX/US-Remote, sort-tier grouping WA+Remote > CA/TX. Company discovery aligned to the same scope (GEOGRAPHY prompt clause: WA state + US-remote qualification; `TAVILY_QUERIES` = per-track bases + one uniform `_QUERY_GEO_TAIL`, no per-track geo — both soft biases, no deterministic company-level filter; +`_SPACE_REGION_QUERIES` geo-targeted Space exception). FL = Space-track-only region (REQ-162): `classify_region` gains "FL", track-aware keep via shared `_geo_out_of_scope(location, track)` in both geo gates, GEOGRAPHY clause Space-FL exception, sort tier FL grouped with CA/TX. Tests 945+ → 1,149. | Title conventions vary ("Technical Project Manager" = same role at many companies; vertical-track orgs title technical roles plain "Program Manager" — small orgs, no non-tech PM bureaucracy), and the user's real commutable scope is all of WA (Bellevue/Redmond/Everett/Kent — Boeing, Blue Origin sites), not just the Seattle-metro city list. Recall widened at the cheap pre-scrape stage; write-time gates (domain/YoE/work-auth) keep final precision. |
| v2.1 | 2026-07-16 | **Concurrent-run safety (BUG-73)**. New `shared/run_lock.py`: every agent `__main__` holds an exclusive `flock` on `logs/pathfinder.lock` for its whole run — a second agent fails fast naming the holder (agent/pid/start), or queues when `PATHFINDER_LOCK_WAIT=1` (exported by `run_pipeline_scheduled.sh`); lock release is kernel-guaranteed on any exit. Persistence: all `read_only` workbook loads routed through `load_workbook_readonly` (BytesIO snapshot + 3-attempt retry on half-written zip); `get_company_archive_info` rewritten to a single `iter_rows` pass (read-only per-cell access re-parses sheet XML per call, O(rows²) — remaining 16 read helpers still on that pattern, snapshot-protected, follow-up). | 2026-07-16: a manual job_agent run overlapped the still-running (6h) scheduled pipeline — EOFError crash mid-read when the pipeline saved the workbook, duplicate scraping spend on 387 JDs, and a silent last-writer-wins race. Nothing enforced the persistence layer's single-process assumption across processes. |
| v2.0 | 2026-07-09 | **Excel-review follow-up batch** (BUG-62, BUG-65~69 + T16). Persistence: `TRIAGE_SHEETS` user triage tabs (`JD_ToApply`/`Skipped JD`) with permanent URL exclusion via `get_triaged_jd_urls`; `get_incomplete_company_rows`/`update_company_business_focus`. job_agent: write-time gates factored into `_gate_and_finalize` and applied on every staging path (custom scrapers + retry — closes the India/Europe geo leak); extraction prompt captures ALL locations verbatim; shared `_parse_jsonld_jobposting` consolidates 4 JSON-LD blocks and adds `datePosted` recovery (list-meta → scrape stash → plain-GET → Tavily → keep+flag); retry preserves Posted Date; Google Careers discovery adapter `_fetch_google_jobs` (server-rendered `AF_initDataCallback` payload — design.md's v3 API is dead; prefetched JD text, zero Firecrawl); empty-company extractions write JSON-ERROR audit rows (BUG-62). New `shared/firecrawl_pool.py` replaces per-call-site `FirecrawlApp` construction (`fc_key` threading removed). company_agent: `run_reenrich_business_focus` self-heal step after Phase 1.5. Dead `_is_us*`/pycountry code removed — `classify_region` is the sole live geo filter. | User's post-launch manual review: triage work must be final (not undone by the next run); India/Europe rows and blank posted dates were data-quality bugs; Firecrawl credits ran out invisibly; Google (a target company) was undiscoverable. |
| v1.9 | 2026-07-07 | **PRJ-004: Multi-Track Expansion**. Company universe: 6 AI buckets/200 cap → 6-track taxonomy at 500 (AI-native/Mid-large Tech/Robotics/Fintech/Space/Defense, per-bucket quotas + grandfathering, `--migrate-tracks` one-time re-bucketing). Job filtering: AI-purity layer → 5-track domain classifier with big-tech sub-org anchors; new write-time gates (YoE [stated min ≤3/≥12 skip], global work-auth screen, ≤14-day pre-scrape freshness on parseable dates); geo tightened to Seattle/CA/TX/US-Remote. JD_Tracker: Job Domain + 6 new columns, combined 1–6 Sort Tier recomputed at each sort. Scrapers: Workday pagination (uncapped), Firecrawl map uncapped, Amazon.jobs adapter with prefetched JD text (zero crawler fallback), list_meta generalizes workday_meta. Match layer: 5 per-track Recruiter/HM prompt pairs + tailor emphasis, routing by Job Domain in both match_agent and resume_optimizer, per-track context caches, REQ-052 byte identity preserved per track. Ops: launchd failure markers, RunSummary token-usage notes. Tests 859 → 945. | User's updated thesis: six tracks with 10-year runway; winning play is early companies + early divisions of incumbents. Freshness is first-class (≤15-day postings only); seniority judged by JD content (YoE) not title; ITAR/clearance screen for a green-card holder. |
