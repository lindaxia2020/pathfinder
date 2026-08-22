# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment Setup

The project uses a Python 3.11 virtualenv at `./venv`. Always activate it before running code:

```bash
source venv/bin/activate
```

API keys go in `.env` (already present but empty). Required keys:
- `GEMINI_API_KEY` — Google Gemini (via `google-generativeai`)
- `TAVILY_API_KEY` — Tavily search API
- `FIRECRAWL_API_KEY` — Firecrawl web scraping

Optional: `GEMINI_API_KEY_2` — second Gemini key for key-pool rotation (all 4 agents consume it when set).
Optional: `FIRECRAWL_API_KEY_2` — second Firecrawl key; `shared/firecrawl_pool.py` rotates to it on 402/429 (credits exhausted) and prints one loud warning when all keys are exhausted.
Optional: `TAVILY_API_KEY_2` — second Tavily key; `shared/tavily_pool.py` rotates to it on 402/429/usage-limit errors and, once all keys are exhausted, prints one loud warning then raises `TavilyQuotaExhausted` (callers abort their Tavily-dependent loops and retry next run — there is no free fallback for search).

Load env vars in scripts with `python-dotenv` (`from dotenv import load_dotenv; load_dotenv()`).

## Project Structure

```
agents/              # 4 Runtime Agents (the product)
  company_agent.py   # AI company discovery + career URL finding
  job_agent.py       # TPM job discovery + JD extraction (incl. ats_keywords)
  match_agent.py     # Resume-to-JD matching (3-dim: ATS / Recruiter / HM)
  resume_optimizer.py # Resume tailoring + 3-dim re-scoring
shared/              # Shared utilities reused across agents
  excel_store.py     # Unified Excel persistence layer
  gemini_pool.py     # Gemini API key rotation + transient retry
  firecrawl_pool.py  # Firecrawl key rotation on 402/429 + exhaustion warning (BUG-68)
  tavily_pool.py     # Tavily key rotation on 402/429/usage-limit + exhaustion raise (BUG-70)
  run_lock.py        # Cross-process run lock — one agent at a time (BUG-73); manual runs fail fast, pipeline queues via PATHFINDER_LOCK_WAIT=1
  rate_limiter.py    # Token-bucket rate limiter
  config.py          # Shared constants (MODEL, TRACK_ORDER, JD_CACHE_DIR)
  prompts.py         # Shared LLM system prompts (RECRUITER, HM, TAILOR)
  schemas.py         # Pydantic response schemas for Gemini
  exceptions.py      # GeminiTransientError / GeminiStructuralError
  run_summary.py     # Structured run-log dataclass
  ats_matcher.py     # Deterministic ATS keyword coverage (PRJ-002)
  ats_synonyms.py    # Hand-curated ATS keyword synonym table (PRJ-002)
tests/               # Unit tests (945+ cases)
docs/
  sdlc/              # SDLC project documents (index + per-project dirs)
.claude/
  agents/            # 11 Custom Agents (Planning / Quality / Evaluation groups; no Edit/Write tools)
  skills/            # 8 Skills (SDLC coordination + operational execution)
profile/             # Candidate resume (.md/.txt/.pdf — picker priority in that order)
profile/.cache/      # Auto-generated MD from PDF input (deterministic, hash-keyed)
templates/           # PDF rendering assets
  resume.css         # ATS-safe CSS for tailored-resume PDF output
jd_cache/            # JD Markdown cache (auto-created)
tailored_resumes/    # Tailored resume output (auto-created; .md + .pdf per JD)
venv/                # Python 3.11 virtualenv
.env                 # API keys (not committed)
```

## Architecture Intent

This is a **multi-agent AI project**. The expected pattern:
- `agents/` — each file or subdirectory is a self-contained agent with a specific role
- `shared/` — cross-agent utilities: tool wrappers, prompt templates, common helpers

### Manual-entry override rule

The agents accept manually-inserted rows in `pathfinder_dashboard.xlsx` and
will enrich them on the next pipeline run:

- **Company_List** — insert a row with just `Company Name` + `Track` (blank
  Career URL). `company_agent.run_phase_1_5` detects the blank URL and calls
  `find_career_url` to discover an ATS/Workday URL, writing it back. Tavily
  must be available; rows that resist discovery are reported and left blank
  for retry. `Track` should be one of the 6 buckets (AI-native / Mid-large
  Tech / Robotics / Fintech / Space / Defense); unknown/custom values take
  the strict Mid-large-Tech per-JD classifier path with a logged warning.
  **Filled Career URLs are user-verified data (BUG-74/75): no pipeline step
  ever rewrites them** — blank-URL backfill is the only Career URL write.
  The correction loop: `python agents/company_agent.py --audit`
  (report-only → transient `Company_Audit` tab; options `--audit-limit N`,
  `--audit-skip-http`; add `--suggest-urls` [+`--suggest-non-ats`,
  `--suggest-limit N`] to fill Suggested URL/Evidence columns for flagged
  rows) → user reviews, types Y in "Approve URL?" where accepted →
  `--apply-audit` writes proposed Track/Focus and ONLY user-approved
  suggested URLs back to Company_List (`--urls-only` skips Track/Focus).
  Anti-oscillation: applied Track/Focus decisions are recorded in
  `logs/company_audit_ledger.json`; audits never re-propose changes away
  from a user-confirmed value (hand-edit the cell or delete the ledger to
  re-open).
- **JD_Tracker** — insert a row with just `JD URL` + `Company`. The job_agent
  picks it up via `get_incomplete_jd_rows` and runs full extraction. Note:
  manually-inserted rows pass the same write-time gates as discovery
  (geo/domain/YoE/work-auth, BUG-66) — a non-US JD URL will be skipped.

### User triage tabs (BUG-65)

`JD_ToApply` and `Skipped JD` are user-owned tabs (same columns as
JD_Tracker). Moving a reviewed row out of JD_Tracker into either tab is a
FINAL decision: those JD URLs are permanently excluded from re-scraping and
re-insertion. The pipeline never writes to or deletes from these tabs (it
only auto-creates them when missing). Do not rename them — the exclusion is
keyed on the exact tab names (`TRIAGE_SHEETS` in `shared/excel_store.py`).
Exclusion matches on canonical URL form (BUG-71,
`shared/excel_store.py:canonical_jd_url`), so tracking-param or host/path
variants of a triaged posting are also excluded.

## Key Libraries Available (installed in venv)

| Library | Purpose |
|---|---|
| `google-generativeai` | Gemini LLM (primary model) |
| `litellm` | Unified LLM API gateway |
| `openai` | OpenAI-compatible API calls |
| `tavily-python` | Web search via Tavily API |
| `firecrawl-py` | Web scraping/crawling |
| `crawl4ai` | AI-optimized web crawling |
| `aiohttp` / `httpx` | Async HTTP requests |
| `beautifulsoup4` | HTML parsing |
| `huggingface_hub` | HuggingFace model access |
| `pycountry` | Installed but unused since 2026-07-09 (BUG-66): the legacy `_is_us*` geo filter was removed — `shared/excel_store.py:classify_region` is the live geo filter |
| `pydantic` | Structured output schemas for Gemini JSON response validation |
| `requests` | HTTP requests for ATS API calls and JD scrape fallback |
| `openpyxl` | Excel file read/write (core data store) |
| `pdfplumber` | Deterministic PDF→text extraction with layout/font metadata (PRJ-003) |
| `weasyprint` | MD→PDF rendering for tailored resumes (PRJ-003) — requires `brew install pango` on macOS |

## Running Agents

No build step. Run agents directly:

```bash
source venv/bin/activate
python agents/<agent_name>.py
```

Agents hold an exclusive run lock (`logs/pathfinder.lock`, BUG-73) — a manual
run while another agent (e.g. the 04:00 scheduled pipeline) is still going
fails fast with a message naming the holder. Set `PATHFINDER_LOCK_WAIT=1` to
queue behind it instead; the scheduled pipeline sets this itself.

## Custom Agents (`.claude/agents/`)

Development-time agents invoked via the Agent tool, organized into three groups under the user (Architect / Product Owner), alongside the Implementation group (the Claude Code main thread itself):

| Agent | Model | Group | Purpose |
|---|---|---|---|
| `product-manager` | sonnet | 📋 Planning | Research, feasibility analysis, BRD writing, testing sign-off, decision support |
| `tpm` | opus | 📋 Planning | Task decomposition, coordination, risk management, progress reporting, launch readiness |
| `agent-reviewer` | opus | 🔍 Quality | Review agent code quality, prompt design, cross-agent consistency |
| `api-debugger` | sonnet | 🔍 Quality | Debug Gemini/Tavily/Firecrawl/ATS API issues |
| `schema-validator` | sonnet | 🔍 Quality | Validate Excel sheet schemas and inter-agent data contracts |
| `test-analyzer` | sonnet | 🔍 Quality | Analyze test failures, identify coverage gaps |
| `doc-sync` | sonnet | 🔍 Quality | Detect documentation drift (REQUIREMENTS/ARCHITECTURE/BUGS/CHANGELOG) |
| `bug-tracker` | sonnet | 🔍 Quality | Manage BUGS.md: verify status, scan new bugs, suggest regression tests |
| `eval-engineer` | sonnet | 📊 Evaluation | AI output quality evaluation: scoring calibration, prompt regression, hallucination detection |
| `observability` | sonnet | 📊 Evaluation | Pipeline run reporting, output quality drift detection, anomaly alerting |
| `cost` | sonnet | 📊 Evaluation | API token usage estimation, quota monitoring, cost optimization recommendations |

All 11 agents carry no Edit/Write tools (analysis-only by design; `allowed-tools: Read, Grep, Glob, Bash`). Each group authors its own deliverables (Planning: requirements & milestones; Quality: test plans & review reports; Evaluation: eval & cost reports), drafted by the agents and persisted via the Claude Code main thread — which alone holds code write access.

## SDLC Workflow (`docs/sdlc/`)

5-phase workflow coordinated by TPM Agent: BRD → Tech Design → Implementation → Testing → Launch.

Each project lives in `docs/sdlc/PRJ-xxx-<name>/` with `status.md` as single source of truth.

| Skill | Purpose |
|---|---|
| `/sdlc-init` | Initialize new SDLC project with ID, directory, and templates |
| `/sdlc-status` | View project status (single project or all active) |
| `/sdlc-review` | Trigger stage-specific reviews (brd/design/testing/launch) |

## Installing New Dependencies

```bash
source venv/bin/activate
pip install <package>
```
