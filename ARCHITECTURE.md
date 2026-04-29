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
│  │· Career URL    │ │  (crawler)     │ │· Coarse     │ │· Re-scoring      ││
│  │  finding       │ │· JD extraction │ │  screening  │ │  verification    ││
│  │· ATS URL       │ │  + caching     │ │  (batch)    │ │· Score comparison││
│  │  upgrade       │ │                │ │· Fine eval  │ │                  ││
│  │                │ │                │ │  (Top 20)   │ │                  ││
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
1. Read existing companies from Excel (Company_List + Company_Without_TPM) → build exclusion list
2. Tavily batch search (7 queries covering 4 categories)
3. Gemini LLM extracts company info (name, domain, business description), does not generate URLs
4. Per-company multi-strategy Career URL finding:
     ① KNOWN_CAREER_URLS hardcoded → ② Tavily ATS-targeted search
     → ③ Tavily general search → ④ Greenhouse/Lever slug probing → ⑤ Homepage crawling
5. Companies without a found URL are skipped (not written)
6. Upsert company data to Company_List
7. Phase 1.5: Probe and upgrade non-ATS URLs → write back
```

**Key constants**:
- `MAX_TOTAL = 200` (max total companies)
- `BATCH_SIZE = 50` (max new additions per run)
- Search distribution: Large tech 50% / AI startups 25% / AI infrastructure 15% / Large model labs 10%

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
1. Load resume from profile/ (.md/.txt), compute resume MD5
2. Read all JDs from JD_Tracker where is_ai_tpm=True
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
1. Load resume from profile/, compute MD5
2. Read fine-stage matches from Match_Results (stage="fine"), including
   per-dim originals (ATS Coverage %, Recruiter Score, HM Score) via the
   PR 4 extension to get_scored_matches
3. Read existing optimized records from Tailored_Match_Results, skip pairs
   where resume_hash is unchanged

Phase 1 — Batch tailor:
  Batch size 2 — Gemini generates tailored resume (reorders / emphasizes /
  mirrors JD keywords; never fabricates new experience). Save to
  tailored_resumes/{resume_id}/{url_md5}.md. Per-item fallback if batch failed.

Phase 2 — 3-dimension rescore (per JD, concurrency=3):
  ├── ATS:       compute_coverage(ats_keywords, tailored_md) — deterministic, no LLM
  ├── Recruiter: batch_coarse_score(tailored_md, [jd_dict]) — 1 Gemini call (cross-agent)
  └── HM:        re_score(tailored_md, jd_content) — 1 Gemini call, FINE_SYSTEM_PROMPT
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
| `Company_List` | Company Name | Company Name, AI Domain, Business Focus, Career URL, Updated At, TPM Jobs, AI TPM Jobs, No TPM Count, Auto Archived |
| `Company_Without_TPM` | Company Name | Company Name, AI Domain, Business Focus, Career URL, Updated At, TPM Jobs, AI TPM Jobs |
| `JD_Tracker` | JD URL | JD URL, Job Title, Company, Location, Salary, Requirements, Additional Qualifications, Responsibilities, Is AI TPM, Updated At, MD Hash, Data Quality |
| `Match_Results` | Resume ID + JD URL | Resume ID, JD URL, Score, Strengths, Gaps, Reason, Updated At, Resume Hash, Stage, **ATS Coverage %, Recruiter Score, HM Score, ATS Missing** (PRJ-002) |
| `Tailored_Match_Results` | Resume ID + JD URL | Resume ID, JD URL, Job Title, Company, Original Score, Tailored Score, Score Delta, Tailored Resume Path, Optimization Summary, Updated At, Resume Hash, Regression, **Original ATS, Tailored ATS, ATS Delta, Original Recruiter, Tailored Recruiter, Recruiter Delta, Original HM, Tailored HM, HM Delta** (PRJ-002) |

### Key Design

- **Write operation pattern**: Each time load → modify → save (single-process safe, job_agent wraps with asyncio.Lock)
- **Batch write**: `batch_upsert_jd_records`, `batch_upsert_match_records` (single save, reduced I/O)
- **Dynamic column mapping**: `_JD_COL = {h: i+1 for i, h in enumerate(JD_HEADERS)}`, JD_Tracker read functions look up column index by field name
- **Archive management**: `get_archived_companies()`, `update_archive_status()`, `unarchive_company()`, `get_company_archive_info()`, `count_valid_tpm_jobs_by_company()`
- **Schema migration**: Automatically detects and adds missing columns on startup (backward compatible with historical data)
- **Corruption recovery**: Automatically renames corrupted files to `.bak` and recreates

---

## 5. External Service Dependencies

| Service | Purpose | Auth | Key Limitations |
|---------|---------|------|-----------------|
| Tavily API | AI company web search, Career URL search | `TAVILY_API_KEY` | Limited free quota |
| Google Gemini (`gemini-3.1-flash-lite-preview`) | Company extraction, JD structuring, coarse screening, fine evaluation | `GEMINI_API_KEY` / `GEMINI_API_KEY_2` | Supports multi-key rotation; 13 RPM rate control |
| Greenhouse Public API | ATS job listings | No auth required | Public endpoint |
| Lever Public API | ATS job listings | No auth required | Public endpoint |
| Firecrawl | Web crawling/Map | `FIRECRAWL_API_KEY` | Has quota, retry on 429 |
| Ashby Public API | ATS job listings (added in REQ-058) | No auth required | Public endpoint: `https://api.ashbyhq.com/posting-api/job-board/{slug}` |
| Crawl4AI + Playwright | JS-rendering crawler | None | Async only, requires local Chromium |

---

## 6. Development Tool Layer & SDLC Workflow

PathFinder's development is supported by four tool layers, coordinated through the SDLC workflow, forming a complete AI-assisted development cycle:

```
┌──────────────────────────────────────────────────────────────┐
│                   User (Business Owner)                       │
└──────────────────────┬───────────────────────────────────────┘
                       │ High-level goals, business decisions, final sign-off
                       ▼
┌──────────────────────────────────────────────────────────────┐
│              Claude Code Main Thread (Engineer Lead)          │
├──────────┬──────────────┬────────────────────┬───────────────┤
│          │              │                    │               │
│ Planning │ Coordination │   Quality          │  Operations   │
│ Layer    │ Layer        │   Layer            │  Layer        │
│          │              │                    │               │
│ product- │ tpm   (opus) │ agent-reviewer     │ /pipeline     │
│ manager  │              │ schema-validator   │ /run-agent    │
│ (sonnet) │ /sdlc-init   │ test-analyzer      │ /test-all     │
│          │ /sdlc-status │ api-debugger       │ /test-one     │
│          │ /sdlc-review │ doc-sync           │ /check-env    │
│          │              │ bug-tracker        │               │
│          │              │ eval-engineer      │               │
│          │              │ observability      │               │
│          │              │ cost               │               │
│          │              │                    │               │
│ Agent    │ Agent+Skill  │ Agent              │ Skill         │
└──────────┴──────────────┴────────────────────┴───────────────┘
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

Each project's documents are located in `docs/sdlc/PRJ-xxx-<name>/`, containing `status.md` (single source of truth), `brd.md`, `tech-design.md`, `launch-readiness.md`, and a `reviews/` directory.

Escalation mechanism:
- L1 Info → Engineer Lead
- L2 Decision → TPM + Engineer Lead discussion
- L3 Business (`[ESCALATE]`) → User
- L4 Blocker (`[BLOCKED]`) → User + Engineer Lead

### Custom Agents (`.claude/agents/`) — Analysis and diagnostics, read-only

| Agent | Model | Layer | Responsibilities |
|-------|-------|-------|------------------|
| `product-manager` | sonnet | Planning | Requirements analysis, BRD writing, progress tracking, impact assessment, testing sign-off |
| `tpm` | opus | Coordination | Task decomposition, cross-team coordination, risk management, progress reporting, launch assessment |
| `agent-reviewer` | opus | Quality | Review code quality, prompt design, cross-agent consistency |
| `schema-validator` | sonnet | Quality | Validate Excel schema and inter-agent data contracts |
| `test-analyzer` | sonnet | Quality | Analyze test failure causes, identify coverage blind spots |
| `api-debugger` | sonnet | Quality | Debug Gemini/Tavily/Firecrawl/ATS API issues |
| `doc-sync` | sonnet | Quality | Detect drift between code and docs (REQ/ARCH/BUGS/CHANGELOG) |
| `bug-tracker` | sonnet | Quality | Manage BUGS.md: verify status, scan for new bugs, suggest regression tests |
| `eval-engineer` | sonnet | Quality | AI output quality evaluation: scoring calibration, prompt regression detection, hallucination detection |
| `observability` | sonnet | Quality | Pipeline run reporting, output quality drift detection, anomaly alerting |
| `cost` | sonnet | Quality | API token usage estimation, quota monitoring, cost optimization recommendations |

### Skills (`.claude/skills/`) — Operational execution

| Skill | Layer | Purpose |
|-------|-------|---------|
| `/sdlc-init` | Coordination | Initialize SDLC project (assign ID, create directory and template files) |
| `/sdlc-status` | Coordination | View project status (single project details or global overview) |
| `/sdlc-review` | Coordination | Trigger stage-specific reviews (BRD/Design/Testing/Launch) |
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
│   ├── match_agent.py         # Resume matching (two-stage)
│   └── resume_optimizer.py    # Resume tailoring optimization + re-scoring
├── shared/                    # Cross-agent shared modules
│   ├── __init__.py
│   ├── excel_store.py         # Unified Excel persistence layer
│   ├── gemini_pool.py         # Gemini API key rotation base class (client caching, round-robin, thread-safe)
│   ├── rate_limiter.py        # Token-bucket async rate limiter
│   └── config.py              # Shared constants (MODEL)
├── tests/                     # Unit tests (485+ cases)
├── docs/
│   └── sdlc/                  # SDLC project documents
│       ├── index.md           # Project index table
│       └── PRJ-xxx-<name>/    # Per-project directory
│           ├── status.md      # Project status (single source of truth)
│           ├── brd.md         # Business Requirements Document
│           ├── tech-design.md # Technical Design Document
│           ├── launch-readiness.md
│           └── reviews/       # Review records
├── .claude/
│   ├── agents/                # 11 Custom Agents (dev-time helpers, read-only)
│   │   ├── product-manager.md # Planning: requirements analysis, BRD, testing sign-off
│   │   ├── tpm.md             # Coordination: task decomposition, risk management, launch assessment
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
│       ├── sdlc-init/         # Coordination: SDLC project initialization
│       ├── sdlc-status/       # Coordination: project status viewing
│       ├── sdlc-review/       # Coordination: stage-specific reviews
│       ├── pipeline/
│       ├── run-agent/
│       ├── test-all/
│       ├── test-one/
│       └── check-env/
├── profile/                   # Candidate resume directory (.md/.txt)
├── jd_cache/                  # JD Markdown local cache (auto-created)
├── tailored_resumes/          # Tailored resume output (subdirectories by resume_id)
├── pathfinder_dashboard.xlsx  # Main data file (auto-created)
├── .env                       # API Keys (not committed)
├── CLAUDE.md                  # Development guide
├── REQUIREMENTS.md            # Requirements tracking (63 REQ + DEC entries)
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
| v1.6 (current) | 2026-04-28 | **PRJ-002: 3-Dimension Scoring**. Restructured the resume-fit scoring pipeline from a single LLM-derived "fit score" into three parallel dimensions: ATS Coverage (deterministic, `shared/ats_matcher.py`), Recruiter Score (Gemini, was COARSE), HM Score (Gemini, was FINE). New `JobDetails.ats_keywords` field; `Match_Results` +4 cols; `Tailored_Match_Results` +9 cols (auto-migrated). Optimizer rescores all 3 dims; regression flag now means HM Delta < 0 only (was: legacy single-score delta). Tests 619 → 718 (+99 across 5 sequential PRs). Plus P0 follow-ups: P0-9 Stage 2 UNION selection, P0-10 single-JD rescore parity, P0-11 Gemini transient backoff retry, P0-12 persisted Regression column. | Existing single-score Score Delta conflated keyword gains with semantic strength; users had no signal on which one moved. Mapping each dimension to one real-world hiring filter (ATS keyword search → recruiter scan → HM deep eval) makes the system's output match the actual North American funnel. |
