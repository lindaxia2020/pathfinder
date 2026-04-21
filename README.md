# PathFinder

PathFinder is an autonomous AI-powered job discovery and matching system designed for Technical Program Manager (TPM) candidates. It automates the end-to-end workflow of finding AI companies, discovering TPM positions, evaluating resume-job fit, and tailoring resumes for top matches.

> 📌 **For recruiters & hiring managers** — see **[PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)** for a curated technical overview: why the project exists, what the runtime pipeline does, how it was built with Claude Code + 11 AI subagents, and what competencies it demonstrates.

## At a Glance

- **What it is:** A 4-agent LLM pipeline — **Discover → Extract → Match → Tailor** — that automates AI-TPM job search end-to-end.
- **How it was built:** I made all architecture, scope, and review decisions; used **Claude Code (Opus)** as implementation partner and designed an **11-agent Claude subagent review team** (PM, TPM, 9 QA/Eval/Cost reviewers) for parallel review across a full SDLC.
- **Scale signals:** ~4K lines of Python · 485+ unit tests · Pydantic-typed inter-agent contracts · Gemini API pooling + token-bucket rate limiting · full BRD / Tech Design / Test / Launch artifacts under `docs/sdlc/`.
- **Stack:** Python 3.11 · Gemini · Claude Code · Tavily · Firecrawl · Crawl4AI · Pydantic · openpyxl · pytest.

## How It Works

The system runs four independent agents in sequence:

1. **Company Agent** — Discovers AI companies via web search (Tavily), finds their career/ATS pages, and validates URLs
2. **Job Agent** — Scrapes career pages for TPM openings using ATS APIs (Greenhouse, Lever, Ashby) or web crawlers (Firecrawl, Crawl4AI), extracts structured JD data
3. **Match Agent** — Two-stage resume-to-JD matching: keyword pre-filter → Gemini LLM batch scoring → top 20% fine evaluation (4-dimension weighted scoring)
4. **Resume Optimizer** — Tailors resume for each matched JD using Gemini, then re-scores to measure improvement

All results are persisted to a local Excel dashboard (`pathfinder_dashboard.xlsx`).

## Project Structure

```
agents/              # 4 Runtime Agents
  company_agent.py   # AI company discovery + career URL finding
  job_agent.py       # TPM job discovery + JD extraction
  match_agent.py     # Resume-to-JD matching (2-stage)
  resume_optimizer.py # Resume tailoring + re-scoring
shared/              # Shared utilities
  excel_store.py     # Unified Excel persistence layer
  gemini_pool.py     # Gemini API key rotation
  rate_limiter.py    # Token-bucket rate limiter
  config.py          # Shared constants
tests/               # Unit tests (485+ cases)
profile/             # Candidate resume (.md/.txt)
docs/                # SDLC project documents
```

## Setup

### Prerequisites

- Python 3.11+

### Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### API Keys

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

Required keys:
- `GEMINI_API_KEY` — Google Gemini (primary LLM)
- `TAVILY_API_KEY` — Tavily search API
- `FIRECRAWL_API_KEY` — Firecrawl web scraping

### Usage

```bash
source venv/bin/activate
python agents/company_agent.py    # Step 1: Discover companies
python agents/job_agent.py        # Step 2: Find TPM jobs
python agents/match_agent.py      # Step 3: Score matches
python agents/resume_optimizer.py # Step 4: Tailor resumes
```

Place your resume (`.md` or `.txt`) in the `profile/` directory before running the match agent.

## AI SDLC Development Team

This project is delivered via AI-augmented program management. I hold all architecture, scope, and review authority. **Claude Code (Opus)** serves as my **implementation partner** — executing against my specs, and dispatching the 11 read-only specialist subagents I designed for parallel review across code, prompts, schemas, tests, docs, evaluation, observability, and cost. All agent definitions and skills are in the `.claude/` directory.

### The Team

| Role | Agent | Model | Purpose |
|------|-------|-------|---------|
| **Implementation Partner** | **Claude Code** | **Opus** | **Executes code, tests, and docs against my specs; sole write access; dispatches the 11 review subagents on my priorities** |
| Planning | Product Manager | Sonnet | BRD authoring, feasibility analysis, testing sign-off |
| Planning | TPM | Opus | Task decomposition, cross-team coordination, risk management, launch readiness |
| QA | Agent Reviewer | Opus | Code quality review, prompt design, cross-agent consistency |
| QA | API Debugger | Sonnet | Gemini/Tavily/Firecrawl/ATS API issue diagnosis |
| QA | Schema Validator | Sonnet | Excel sheet schema and inter-agent data contract validation |
| QA | Test Analyzer | Sonnet | Test failure analysis, coverage gaps, edge case suggestions |
| QA | Doc Sync | Sonnet | Documentation-to-code drift detection |
| QA | Bug Tracker | Sonnet | BUGS.md management, new bug scanning, regression test suggestions |
| Eval | Eval Engineer | Sonnet | AI output quality evaluation, scoring calibration, hallucination detection |
| Eval | Observability | Sonnet | Pipeline run reporting, data quality drift, anomaly alerting |
| Eval | Cost | Sonnet | API token usage estimation, quota monitoring, cost optimization |

> **Key distinction:** I hold all design and review authority. Claude Code is the only agent with code write permissions. The other 11 agents are strictly read-only (analysis, reporting, and review), dispatched by Claude Code under my direction.

### Skills

**Operational:**

| Skill | Command | Purpose |
|-------|---------|---------|
| Run Agent | `/run-agent <name>` | Run a specific runtime agent |
| Pipeline | `/pipeline` | Run all 4 agents in sequence |
| Test All | `/test-all` | Run the full test suite |
| Test One | `/test-one <name>` | Run tests for a specific module |
| Check Env | `/check-env` | Verify API key configuration |

**SDLC Management:**

| Skill | Command | Purpose |
|-------|---------|---------|
| SDLC Init | `/sdlc-init <name>` | Initialize a new SDLC project with templates |
| SDLC Status | `/sdlc-status [PRJ-ID]` | View project status (single or all active) |
| SDLC Review | `/sdlc-review <PRJ-ID> <stage>` | Trigger multi-agent stage review |

### SDLC Workflow

```
BRD → Tech Design → Implementation → Testing → Launch
```

Each project lives in `docs/sdlc/PRJ-xxx-<name>/` with `status.md` as the single source of truth. The `/sdlc-review` skill coordinates multiple agents at each stage — for example, the Testing stage dispatches Test Analyzer, Bug Tracker, Doc Sync, API Debugger, and Eval Engineer for a comprehensive review.

## Testing

```bash
source venv/bin/activate
python -m pytest tests/ -v
```
