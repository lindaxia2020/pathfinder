# PathFinder — Project Overview

> A curated technical overview for recruiters, hiring managers, and reviewers. For setup and usage, see [README.md](README.md).

---

## TL;DR

**PathFinder is an end-to-end, multi-agent AI system I designed and shipped to automate the AI-TPM job search** — it discovers AI companies, extracts TPM job postings, scores resume-to-JD fit, and produces per-role tailored resumes. It also demonstrates AI-augmented delivery: I designed an **11-agent Claude subagent review team** and used **Claude Code (Opus)** as my implementation partner across a full Software Development Life Cycle (BRD → Tech Design → Implementation → Testing → Launch).

**Live repo:** https://github.com/michaellam26/pathfinder
**Stack:** Python 3.11 · Gemini · Claude Code · Tavily · Firecrawl · Crawl4AI · Pydantic · openpyxl · pytest

---

## Why I Built This

I'm a Technical Program Manager at a North American big-tech company, targeting **TPM roles at AI-native companies** and **AI-TPM roles at big-tech**. PathFinder exists to demonstrate hands-on AI engineering depth — LLM orchestration, multi-agent coordination, evaluation rigor, and AI-assisted SDLC — that typically isn't visible from a TPM's day-job artifacts.

Every architectural choice, scoring design, SDLC gate, and review standard in this repo is mine. Claude Code (Opus) served as my implementation partner; an 11-agent Claude review team I designed ran parallel code / prompt / schema / eval / cost review. The runtime pipeline is one AI system; the way it was delivered is another. Both are showcased here.

---

## The Runtime Product — A 4-Agent LLM Pipeline

| Stage | Agent | What It Does | Key Techniques |
|---|---|---|---|
| 1. Discover | [`company_agent.py`](agents/company_agent.py) | Finds AI companies via Tavily search; resolves their ATS (Greenhouse/Lever/Ashby/Workday) career-page URLs | Web search, URL validation, slug-based ATS detection |
| 2. Extract | [`job_agent.py`](agents/job_agent.py) | Pulls TPM openings via ATS JSON APIs; falls back to Firecrawl/Crawl4AI browser scraping for gated sites (Tesla, Workday) | Multi-source scraping, US geo-filtering, `pycountry` subdivisions |
| 3. Match | [`match_agent.py`](agents/match_agent.py) | Two-stage match: keyword pre-filter → Gemini LLM batch scoring → 4-dimension weighted fine-eval on top 20% | Coarse-to-fine filtering, structured Pydantic outputs, batched LLM calls |
| 4. Tailor | [`resume_optimizer.py`](agents/resume_optimizer.py) | Rewrites resume for each JD using Gemini; re-scores output to quantify per-application match-lift | Prompt chaining, before/after evaluation loop |

All agents share a Pydantic-typed contract and persist to a single Excel workbook (`pathfinder_dashboard.xlsx`), which serves as the durable, human-inspectable state store.

---

## How It Was Built — AI-Augmented Delivery

I structured delivery as AI-augmented program management: I made all architecture, scope, and review decisions; Claude Code implemented under my direction; and an 11-agent Claude review team I designed handled parallel QA across specialized domains.

### The Team

```
            ┌──────────────────────────────────────────┐
            │   Me — Architect, PM, Reviewer           │
            │   (all design, scope & review decisions) │
            └────────────────────┬─────────────────────┘
                                 │ direction + specs
            ┌────────────────────▼─────────────────────┐
            │   Claude Code (Opus)                     │
            │   Implementation Partner                 │
            │   (sole write access; executes my spec)  │
            └────────────────────┬─────────────────────┘
                                 │ dispatches for parallel review
       ┌─────────────────────────┼─────────────────────────┐
       │                         │                         │
   Planning                  Quality                   Evaluation
   ─────────               ─────────                 ───────────
   Product Manager         Agent Reviewer            Eval Engineer
   TPM                     API Debugger              Observability
                           Schema Validator          Cost
                           Test Analyzer
                           Doc Sync
                           Bug Tracker
```

- **1 director, 1 writer, 11 reviewers.** I held design and review authority. Claude Code held sole write access. The 11 specialist subagents were strictly read-only — they analyzed, evaluated, and reported; change recommendations flowed through me for prioritization, then to Claude Code for execution.
- **8 reproducible skills** I designed (`/pipeline`, `/run-agent`, `/test-all`, `/sdlc-init`, `/sdlc-review`, etc.) encode the operational workflow so every run is deterministic and every review stage is repeatable.
- **Full SDLC artifacts** under [`docs/sdlc/PRJ-001-pathfinder-v1/`](docs/sdlc/PRJ-001-pathfinder-v1/): BRD, Tech Design, Test Plan, Test Execution Report, Launch Review — authored by agents against my specs and acceptance criteria.

---

## Engineering Rigor

- **485+ unit tests** covering scoring logic, Excel schemas, scrape fallbacks, geo-filter edge cases, and ATS URL extraction
- **Pydantic-typed inter-agent data contracts** — every agent consumes and produces schema-validated structured data
- **Gemini API key pooling + token-bucket rate limiter** ([`shared/gemini_pool.py`](shared/gemini_pool.py), [`shared/rate_limiter.py`](shared/rate_limiter.py)) — sustains throughput inside free-tier quotas across parallel runs
- **Coarse-to-fine LLM filtering** — keyword pre-filter rules out obvious misses cheaply before invoking the LLM, cutting token spend substantially vs naive per-JD scoring
- **Maintained operational docs:** [`REQUIREMENTS.md`](REQUIREMENTS.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BUGS.md`](BUGS.md), [`CHANGELOG.md`](CHANGELOG.md)
- **Excel as the state store** — deliberate choice: durable, auditable by humans, zero infra dependency, portable across machines

---

## What This Project Demonstrates

For an **AI-TPM hiring manager**, PathFinder is evidence of:

| Competency | Where to Look |
|---|---|
| **Multi-agent system design** | `agents/` — 4 agents with clean separation and typed contracts |
| **LLM orchestration** | `match_agent.py` (2-stage match), `resume_optimizer.py` (rewrite + re-score loop) |
| **Evaluation thinking** | 4-dimension weighted scoring, before/after match-lift measurement, `.claude/agents/eval-engineer.md` |
| **Cost awareness at scale** | Key pooling, rate limiting, coarse-to-fine filtering, `.claude/agents/cost.md` |
| **Structured output discipline** | Pydantic schemas on every LLM call, strict validation |
| **AI-augmented program management** | `.claude/agents/` (11 review subagents I designed), `.claude/skills/` (8 skills I authored), `docs/sdlc/` (full phase gates) |
| **TPM-grade process discipline** | BRD, risk registers, test execution reports, launch gates — for a personal project |

---

## Resume One-liner

> *Designed and shipped a 4-agent LLM pipeline for end-to-end AI-TPM job discovery, matching, and resume tailoring. Architected an 11-agent Claude subagent review team and used Claude Code as implementation partner across a full SDLC (BRD → Design → Implementation → Testing → Launch), with Pydantic-typed inter-agent contracts and 485+ unit tests.*

---

## Links

- **Repository:** https://github.com/michaellam26/pathfinder
- **Architecture deep-dive:** [ARCHITECTURE.md](ARCHITECTURE.md)
- **Requirements tracking:** [REQUIREMENTS.md](REQUIREMENTS.md)
- **SDLC artifacts:** [docs/sdlc/PRJ-001-pathfinder-v1/](docs/sdlc/PRJ-001-pathfinder-v1/)
- **Change history:** [CHANGELOG.md](CHANGELOG.md)
