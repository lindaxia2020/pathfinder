# Lessons Learned

## 2026-07-07 — Session handoff means deliverable only, not the next step
- **What happened**: User asked for a new branch + a clear requirements record so a fresh session could run the SDLC cycle. I additionally started running `/sdlc-init` (creating the PRJ-004 skeleton). User rejected the write and restated the instruction.
- **Lesson**: When work is being handed off to a future session, deliver exactly the handoff artifact requested. Do not pre-run steps that belong to the next session's workflow — the receiving session owns its own process (e.g., `/sdlc-init` assigns its own project ID and skeleton).
- **Prevention**: Before adding a step the user didn't name, ask whether it belongs to this session or the next one. Default: it belongs to the next one.

## 2026-07-07 — Pipeline exit codes mask test failures
**What happened**: `pytest ... | tail -2 && git commit` committed T11 with a
red suite — the pipe made the command's exit status `tail`'s (0), not pytest's.
**Rule**: never chain `git commit` behind a piped pytest. Run pytest bare (or
with `set -o pipefail`) and commit as a separate command after seeing the tally.

## 2026-08-20 — Push real ATS location strings through the gate they feed
**What happened**: `tests/test_job_agent.py` already used Amazon's real
`normalized_location` format ("Seattle, Washington") as a fixture, but only
asserted the adapter passed it through — nobody ran `classify_region` on it.
The WA rule only knew ", WA", so 177 Amazon Washington TPM postings per run
were silently geo-dropped from launch until the user noticed "Seattle big
tech feels thin". Same pattern: the Workday CXS `total` quirk (page 0 only)
capped every Workday company at 40 and looked like a plausible result count.
**Rule**: when an adapter test carries a live-format fixture (location,
date, URL), add one assertion that the downstream deterministic gate
(`classify_region`, `compute_freshness_tier`, `_tpm_filter`) accepts it.
And when a paginated fetch returns a round number (40, 100) for a large
company, treat it as a cap until proven otherwise.


## 2026-08-22 — A live adapter check must walk the whole result set, not page 1
**What happened**: the new Microsoft `pcsx/search` adapter parsed page 1
perfectly in unit tests and live, but the first full live run returned 30 of
528 postings: the API is burst-rate-limited, the generic HTTP retry's 0.5 s
back-off was too short, and the loop treated the surviving 429 as "done".
A round, suspiciously small total looked like a plausible result again
(same shape as the Workday 40-cap on 2026-08-20).
**Rule**: for every new list adapter, run one full live walk and compare the
fetched count against the API's own total (`count` / `total` / `hits`)
before calling it done; when they differ, look for rate limiting first.
Make 429 handling explicit per adapter (pace + same-page retry), and keep
the verification script output in the task notes.

## 2026-08-22 — Broad `except` around a parser hides its bugs; assert on a live sample
**What happened**: `_parse_jsonld_jobposting` crashed on a perfectly valid
schema.org `addressCountry: {"@type": "Country", "name": "US"}` (TypeError in
`", ".join`). Every caller wraps the parse in `except Exception`, so the path
silently fell through to Firecrawl/browser and nobody noticed the JSON-LD
route was dead for those pages.
**Rule**: when a parser sits behind a broad `except`, keep one test that
feeds it a verbatim live sample and asserts the parsed fields (not just
"no exception"), and log at WARNING (not DEBUG) when a structured path that
"should" work returns nothing.
