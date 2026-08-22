# CHANGELOG

## 2026-08-22 (open follow-ups cleared — BUG-83~85, REQ-168~172)

The five follow-ups left after the 08-20/21 job-agent result review, worked
as one batch (live probes of public endpoints first, repro tests before each
fix). Full suite 1,149 → 1,165+ passed / 1 skipped.

### Google labeling (BUG-83)
- `_fetch_google_jobs` honors the Career URL's `company=` params (repeatable):
  forwarded to the request (server-side filter — DeepMind 4 / YouTube 7 /
  Google 280 vs 291 unfiltered, verified live) and re-checked against the
  payload's company field (index 7); `**Company:**` in the prefetched JD text
  follows the payload. The "Google DeepMind" row no longer absorbs Google-wide
  postings; the "Google" row (`company=DeepMind&company=YouTube&company=Google`)
  picks up the rest.

### Microsoft discovery adapter (REQ-169)
- `careers.microsoft.com` runs on Eightfold (`apply.careers.microsoft.com`);
  new `_fetch_microsoft_jobs` walks the public
  `GET /api/pcsx/search?domain=microsoft.com&query="technical program manager"&location=United States&sort_by=timestamp&start=N&num=10`
  (quoted phrase → ~49 rows / 5 pages with the same 37 TPM-titled postings
  as the 527-row fuzzy walk, verified by id set; `sort_by=timestamp` because
  relevance order repeats/skips rows across pages; ids de-duplicated;
  10/page hard cap, `data.count` total, `postedTs` epoch-s, `positionUrl`
  → `https://apply.careers.microsoft.com/careers/job/{id}`; "Country, State,
  City" locations reordered to "City, State, Country", placeholder "Multiple
  Locations" → Unknown/keep). Burst rate limit: 2 s pacing + 15 s back-off and
  same-page retry on 429. `ATS_PLATFORMS["microsoft"]` → `json_api` with
  `domains=["careers.microsoft.com"]` (ATS-trusted path, no Path-B delay).
- `_scrape_microsoft_jd`: Eightfold job pages embed a JobPosting JSON-LD
  (description + `datePosted`) → plain GET tried first, Firecrawl
  `only_main_content=True` remains the fallback.
- `_detect_ats` hardened (BUG-84) and `_parse_jsonld_jobposting` accepts
  nested `addressCountry`/`addressRegion` objects (BUG-85) — both found
  while wiring the adapter.

### Freshness first-seen window per track (REQ-170)
- `shared/config.py`: `FIRST_SEEN_MAX_AGE_DAYS = 14` (Mid-large Tech / blank /
  custom) and `FIRST_SEEN_MAX_AGE_DAYS_VERTICAL = 45` (AI-native / Robotics /
  Fintech / Space / Defense). `_apply_prescrape_freshness_gate(..., track)`
  skips a first-seen posting only when its list-API date is older than the
  track's window; regex-shaped but invalid dates ("2026-13-45") are now
  unknown/keep (were aged-skip). Kept rows older than 14 days have no
  freshness tier → sort tier 9 (bottom, visible). Motivating case: Cowboy
  Space "TPM, Avionics" (07-17) was 36 days old — 30 would still have lost it.
  User-confirmed 2026-08-22 (one constant to change if ever needed).
- Workday "N Locations" fallback: `_workday_location_from_path` renders
  `---` slugs as "California, San Francisco" (was "California, , , San,
  Francisco"); comma-dropped slugs keep the legacy rendering (state token
  preserved for geo).

### Salesforce / "Technical Program Management" titles (REQ-171)
- Salesforce's Workday search coverage was fine (422 "Program Manager" / 263
  "Technical Program Manager" / 1,530 total, probed live); the postings are
  titled "… Technical Program Management …" and `TPM_KW`'s "manager"
  substrings never matched "management". `TPM_KW` + "technical program
  management" on all tracks → Salesforce 0 → 3 TPM candidates live.

### Ashby prefetched JD (REQ-172)
- `_fetch_ashby_jobs` carries `descriptionPlain` (fallback: tag-stripped
  `descriptionHtml`) as `_prefetched_md` (+`_platform: "Ashby"`) when ≥200
  chars; `_route_scraper` serves it with zero browser/Firecrawl calls (Cowboy
  Space: 71/71 prefetched; previously 3 Crawl4AI passes per run).

## 2026-08-21 (auto-archive feature removed — REQ-167)

User decision: a company with no TPM openings today may publish one
tomorrow, so the job agent must never skip a company. The REQ-063
auto-archive mechanism is removed end-to-end:
- `job_agent`: no `Auto Archived` skip list at run start; post-run archive
  phase deleted.
- `shared/excel_store.py`: `get_archived_companies` / `update_archive_status`
  / `unarchive_company` / `get_company_archive_info` /
  `count_valid_tpm_jobs_by_company` deleted; `COMPANY_HEADERS` back to 7
  columns; the Company_List migration now **removes** legacy `No TPM Count`
  / `Auto Archived` columns from existing workbooks (applied to
  `pathfinder_dashboard.xlsx` on 2026-08-21; backup
  `pathfinder_dashboard.backup-20260821.xlsx`).
- `shared/config.py`: `AUTO_ARCHIVE_THRESHOLD` deleted.
- Tests: REQ-063 classes replaced by `TestAutoArchiveRemoved` (headers,
  helper absence, migration strips legacy columns, job_agent has no archive
  references).

## 2026-08-21 (write-time geo robustness BUG-82 + YoE policy REQ-166)

Round 2 of the job-agent result review: discovery was fixed on 08-20, but
two downstream write-time behaviours still hid rows the user expected.

### Geo: leading state codes, remote variants, list-location fallback (BUG-82)

- `classify_region` normalizes separators (`-`, `–`, `/`, `|`, parentheses →
  `, `) before classifying, so Workday/Blue Origin `"WA - Landmark (Ride
  East), United States of America"` → WA, `"CA - Remote, …"` → CA,
  `"FL - The Factory at Rocket Park, …"` → FL. Leading state codes map
  directly (`_LEADING_STATE_RE`).
- Remote rule is token-based: bare "Remote" or remote + any US qualifier
  (`US - Remote`, `Remote (United States)`, `Remote, US, East Coast`) →
  Remote; remote + other tokens falls through to the state rules
  (`Remote, California` → CA) and otherwise ends Other (`Remote, Canada`).
- **Write-time gate**: `_gate_and_finalize` now also receives the list
  API's structured location (`list_location`, threaded from
  `process_company` → `_process_scraped_jd`). A row is dropped only when
  both the LLM-extracted location and the list location are out of scope —
  the LLM's formatting of a location the ATS already placed in scope can no
  longer drop the row. Retry path (no list location) keeps its strictness.

### YoE gate: skip only stated min ≥11 (REQ-166, amends REQ-004-08)

The lower cut (min ≤3 → skip) is removed: a stated "2-5 years" TPM at an
early-stage company (Cowboy Space, Greater Seattle Area) is in scope. Upper
cut tightened from ≥12 to ≥11. Unstated-YoE handling (keep + flag /
senior-title auto-qualify) unchanged.

Tests: +3 (region variants, list-location rescue, YoE boundary table
rewritten).

## 2026-08-20 (job-agent recall fixes: WA geo forms + Workday slug/pagination)

User review of a manual job_agent run — "Seattle big tech under-counted,
Cowboy Space TPM missing" — root-caused to three silent-drop bugs
(BUG-79~81). No filter-policy change; these restore recall the existing
policy already intended.

### Geo: full-state-name WA forms now classify "WA" (BUG-79)

`classify_region` knew WA only via the `", WA"` token / exact whitelist, while
CA/TX/FL had full-name + city hints. Amazon ("Seattle, Washington, USA" —
177 of 295 TPM-titled postings), Microsoft ("Redmond, Washington, United
States") and Ashby/Workday ("Greater Seattle Area") all classified "Other"
and were dropped by both geo gates. Now:
- `"<city>, Washington[, country]"` → WA (leading comma = state-qualified;
  bare "Washington" still Other);
- unambiguous WA city hints (greater seattle / seattle / bellevue / redmond /
  kirkland / bothell / sammamish / issaquah / tacoma / spokane), vetoed when
  another state/province is named (Redmond, OR; Vancouver, BC);
- every WA name/hint rule is guarded by `_DC_RE` (dc / d.c. / district of
  columbia) so D.C. forms — including "Capitol Hill, Washington, DC" — stay
  Other.
- Adjacent fix: CA/TX/FL state tokens are word-bounded regexes — the old
  `", ca"` substring matched `", canada"` ("Toronto, Canada" → CA).

### Workday: locale slug + pagination (BUG-80/81)

- `/en-US/<site>` career URLs (13 of 28 Workday rows — Salesforce, Zillow,
  Expedia, Walmart, Blue Origin, …) used "en-US" as the site slug → CXS 404 →
  0 postings. The locale segment is now skipped.
- The CXS API reports `total` on page 0 only (0 afterwards), so
  `offset >= total` stopped every fetch at 40 postings. `total` is now
  captured once from the first positive value; REQ-004-13 uncapped
  pagination is live again (NVIDIA 40 → 725 postings; Blue Origin 0 → 295).

Tests: +4 (region matrix extended; Workday locale + total-on-page-0).

## 2026-08-19 (filter widening: titles + WA geo)

### Title filter: Technical Project Manager everywhere; plain Program Manager on 4 vertical tracks

Recall widening at the cheap top of the funnel (write-time gates — domain /
YoE / work-auth — keep final precision):
- **`TPM_KW`** now also matches "Technical Project Manager" (+ mgr/tech
  variants) on every track — many companies use it for the same senior role.
- **New `PM_TITLE_OK_TRACKS`** = {AI-native, Robotics, Space, Defense}:
  `_tpm_filter` additionally accepts plain "Program Manager" titles there
  (small orgs, loose titling — most PM roles are technical). Fintech and
  Mid-large Tech keep the strict TPM rule (large non-technical PM
  populations). `track` is now threaded `process_company → discover_jobs →
  _discover_via_api → _tpm_filter`.
- **`llm_filter_jobs`** (non-ATS path) rules updated to mirror the same
  three-way split (PM-OK vertical / Fintech / mid-large).
- **Server-side queries**: Workday `searchText` and Firecrawl map search
  broadened "Technical Program Manager" → "Program Manager" (they pre-narrow
  before any client filter). Amazon/Google queries deliberately kept —
  Mid-large Tech, and broader queries risk the pagination caps.

### Geo: Seattle metro → all of Washington state

`classify_region` region "Seattle" renamed **"WA"** and now matches any
`", WA"` state token (word-boundary regex; Spokane/Vancouver/Olympia etc.)
plus the explicit Washington-state forms. Bare "Washington" still → Other
(D.C. collision). Keep-set is now **WA/CA/TX/US-Remote**; sort-tier grouping
unchanged (WA+Remote > CA/TX). Legacy `classify_location` (no production
callers) untouched.

### Company discovery aligned to the same geo scope

`company_agent`'s two geo touchpoints (both soft biases, no deterministic
filter) updated to match the job-side keep-set WA/CA/TX/US-Remote:
- **GEOGRAPHY prompt clause** (discovery extraction): "Greater Seattle" →
  "Washington state (Seattle metro or anywhere in WA)", and companies hiring
  TPMs into **US-remote roles** now qualify explicitly.
- **`TAVILY_QUERIES`**: rebuilt as `_TAVILY_QUERY_BASES` (track content only)
  + one uniform `_QUERY_GEO_TAIL` ("Washington state California Texas
  remote") appended to every query by construction — no per-track geo
  differentiation, exact match with the job-side keep-set. City anchors
  (El Segundo, Seattle, Austin…) removed; example-company names carry the
  search signal.
- **Space-track regional enhancement** (`_SPACE_REGION_QUERIES`, +4 queries →
  18 total): dedicated geo-targeted queries for the space industry's metro
  clusters — Greater Seattle, SoCal (El Segundo/Hawthorne/Long Beach/
  Torrance), Texas (Austin/Houston), and Florida (Cape Canaveral/Melbourne/
  Space Coast). Outside the uniform-tail construction by design.

### Florida as a Space-track-only target region (REQ-162)

FL upgraded from discovery bait to a real target region, restricted to the
Space track:
- **`classify_region`** gains region "FL" (`", fl"` / "florida" /
  unambiguous Space-Coast city hints; bare "melbourne" excluded — Australia
  collision). Precedence WA > Remote > CA > TX > FL; sort tier groups FL
  with CA/TX.
- **New `job_agent._geo_out_of_scope(location, track)`** — single geo gate
  shared by `_tpm_filter` and `_gate_and_finalize`: "FL" keeps only when
  Track == "Space"; every other track treats FL like "Other". Both stages
  covered, no track-agnostic leak.
- **GEOGRAPHY extraction clause** (company discovery): Space-track companies
  additionally qualify via Florida (Space Coast) hiring.

Tests: +5 classes/cases (track-title matrix, WA-state matrix incl.
false-positive guards, LLM rule split) — 1,145 passing.

## 2026-08-19 (anti-oscillation + urls-only apply)

### `--urls-only` + audit decision ledger

Second live audit re-proposed reversals of track changes the user accepted
the same day (NVIDIA, Tesla, Physical Intelligence flipped back; Aerospace
Corp got a third answer) — Gemini oscillates on borderline companies even at
temperature 0. Fixes:
- **`--apply-audit --urls-only`**: apply only Y-approved URL suggestions,
  skip every Track/Focus proposal. Used for this round (3 URLs applied:
  Applied Digital, Zep AI, 6sense).
- **Anti-oscillation ledger** (`logs/company_audit_ledger.json`): every
  Track/Focus value applied via `--apply-audit` is recorded (focus as a
  sha256 fingerprint); `--audit` suppresses re-proposals against a
  user-confirmed value (noted in the row's Notes + a "Re-proposals
  suppressed" counter). Suppression fires only while the cell still equals
  the confirmed value — hand-editing a cell re-opens it, and deleting the
  ledger file re-opens everything. Ledger seeded one-off with all 371
  current Track/Focus values (the user-blessed 2026-08-19 state).
Tests: +6 (`TestAuditAntiOscillation`, urls-only/ledger cases) — 474 in the
touched files.

## 2026-08-19 (URL correction loop)

### `--suggest-urls` + approved-URL apply — the ongoing correction workflow

Agreed workflow for Career URL quality from here on: pipeline runs never
replace URLs (REQ-153) → periodic `--audit` → flagged rows get machine
suggestions → the user approves per row → `--apply-audit` writes only
approved cells. New pieces:
- **`--suggest-urls`** (with `--audit`): rows whose flags mark the URL as
  wrong (`POSTING_URL`/`AGGREGATOR`/`OWNERSHIP_MISMATCH`/`HTTP_FAIL`/`BLANK`;
  informational `NON_ATS` only via `--suggest-non-ats`) get a
  `_suggest_career_url` lookup — cheapest-first: deterministic posting-root
  strip → KNOWN_CAREER_URLS → ATS slug probe with ownership evidence (free
  HTTP) → Tavily ATS/general search → identity-gated homepage scrape. Every
  candidate must differ from the current URL and pass the BUG-74/75/77
  gates. Results land in three new audit columns: **Suggested URL**,
  **URL Evidence** (e.g. "lever board 'goodco' — org name 'GoodCo', 7 live
  jobs"), and an empty **Approve URL?**. `--suggest-limit N` caps lookups
  (Tavily budget: ≤2 searches/row, slug probe costs none).
- **`--apply-audit` extension**: a suggestion is written to Company_List
  ONLY when the user typed Y/yes in "Approve URL?" — the sole sanctioned
  Career URL write outside blank-row backfill; unapproved suggestions are
  never applied. `get_audit_proposals` returns the new fields.
Tests: +12 (`TestSuggestCareerUrl`, `TestRunCompanyAuditSuggestUrls`,
approval cases in `TestApplyAudit`) — 462 passing in the touched files.

**BUG-78** (found by the first live `--suggest-urls` run, which suggested
Automattic's board for Zep AI, Chime's for Arista, Advance Auto Parts' /
QVC's Workday for Akamai / Meta, an instahyre listing for Pure Storage, and
a `/details/`-form Workday posting for F5): the Tavily result path — the
ORIGINAL entry point of the wrong-URL bugs — had been left ungated by
BUG-75. New `_tavily_result_matches_company` on every Tavily result: ATS
boards → ownership check, Workday → subdomain equality, other hosts →
company name must appear in the hostname or the domain core must lead the
company name (never the reverse). Also: Workday `/details/` posting form
added to the BUG-74 patterns; instahyre.com blocklisted. Applies to
discovery and suggestions alike. Tests: +8 — 469 passing.

## 2026-08-19 (later)

### `--apply-audit` — user-approved audit proposals applied

After reviewing `Company_Audit`, the user hand-corrected several Career URLs
and deleted 9 companies in Company_List (their edits are authoritative and
untouched), then approved the Track/Focus proposals. New
`company_agent --apply-audit` (+ `apply_audit()`, `get_audit_proposals()` in
excel_store): applies ONLY Track (`update_company_track`) and Business Focus
(`update_company_business_focus`), matched by Company Name — never excel_row
(sorts/deletions shift rows); Names + Career URLs never written; invalid
buckets and since-deleted companies skipped; canonical Track sort last.
Applied live: 27 track changes + 21 focus rewrites, 2 skipped (deleted
companies); PayPal→Fintech proposal reverted to Mid-large Tech at apply time
(deterministic legacy-incumbent taxonomy rule beats the model). Backup:
`pathfinder_dashboard.backup-20260819.xlsx`. Verified post-apply: Name+URL
set byte-identical to the user's hand-edited state; job-count columns
preserved. Tests: +4 (`TestApplyAudit`), 448 targeted / suite green.

Storage-layer hardening (same day): `upsert_companies` now preserves a
FILLED Career URL on existing-row upserts (only blank/N-A cells accept a
value) — previously discovery dedup was the only thing standing between a
re-discovered name and a clobbered user-verified URL; the invariant now
lives at the write layer where no upstream regression can bypass it
(REQ-153). Two legacy tests asserting the old overwrite behavior updated;
+2 new (`TestUpsertNeverOverwritesFilledCareerUrl`).

## 2026-08-19

### Company-agent career-URL/track quality (BUG-74~77) + report-only `--audit`

Trigger: the user manually verified all 380 Company_List rows and found three
recurring company_agent defects — job-posting links stored as company career
URLs, ATS board URLs "created" from slug guesses that belong to other
companies, and inaccurate Track/Business Focus values.

**Hard constraint honored throughout**: the 380 user-verified Company Name +
Career URL values are never modified by any code path.

**Fixes** (`agents/company_agent.py`):
- **BUG-74** — `_is_job_posting_url` + `_posting_url_to_board_root`: per-ATS
  posting-URL shapes (greenhouse `/jobs/<id>`+`gh_jid=`, lever/ashby UUID,
  workable `/j/<id>`, workday `/job/`, generic numeric ids) rejected by
  `_is_likely_career_url`; Tavily posting results are upgraded to their board
  root; `validate_and_upgrade_ats_url` step 2 strips postings instead of
  freezing them.
- **BUG-75** — board OWNERSHIP verification: `_org_name_matches_company`
  (equality-only compact matching; the Workday subdomain matcher now
  delegates to it) + per-platform org-name fetch (greenhouse/workable JSON
  `name`, lever/ashby board `<title>`); gate wired into `_find_ats_url` and
  `validate_and_upgrade_ats_url`. `_slug_candidates` no longer emits bare
  single words from multi-word names. Homepage guesses require the page to
  identify as the company (`_homepage_belongs_to_company`).
- **Phase 1.5 write guard** — `run_phase_1_5` now ONLY backfills blank
  Career URLs; filled rows are never probed/overwritten (pre-fix it silently
  rewrote every filled non-ATS URL each run).
- **BUG-76** — discovery confidence gate: `CompanyInfo.confident` +
  do-not-guess instruction; unconfident extractions blank Track/Focus for the
  confident-gated repair paths to refill.
- **BUG-77** — `_AGGREGATOR_HOSTS` hostname-suffix blocklist (builtin,
  ycombinator, iitjobs, consider, wellfound, indeed, glassdoor, linkedin, …)
  rejected at discovery; `jobs.gem.com` deliberately allowed (real ATS).
- **`--audit` (report-only)** — batched Gemini re-evaluation of Track/
  Business Focus for every row (20/batch ≈ 19 calls for 380 rows, zero
  Tavily) + URL health flags into the transient `Company_Audit` tab
  (`shared/excel_store.py: replace_audit_sheet`, create-or-replace). Never
  writes Company_List. `--audit-limit N`, `--audit-skip-http` for dry runs.
  Applying user-approved changes is a follow-up (`--apply-audit`, keyed by
  Company Name; not built yet).

**Tests**: 1104 passed / 1 skipped (+~55 new: posting detection/root
stripping, ownership matching/fail-open semantics, slug tightening, homepage
identity gate, phase-1.5 never-rewrites guard, discovery confident gate,
audit report-only invariants, `replace_audit_sheet`).

## 2026-07-16

### Cross-process run lock + crash-proof workbook reads (BUG-73)

A manual `job_agent` run collided with the still-running scheduled pipeline
(6h long due to API retries): both scraped the same 387 JDs (duplicate
Gemini/Firecrawl spend) and the manual run crashed with `EOFError` when the
pipeline's match agent rewrote `pathfinder_dashboard.xlsx` mid-read.

**Fixes**:
- **Run lock (`shared/run_lock.py`)**: every agent entry point takes an
  exclusive `flock` on `logs/pathfinder.lock` for the whole run. A second
  agent fails fast with a message naming the holder (agent/pid/start time);
  `PATHFINDER_LOCK_WAIT=1` queues instead — exported by
  `run_pipeline_scheduled.sh` so pipeline phases never abort on contention.
  The kernel drops the flock on any process exit, so crashes can't leave a
  stale lock.
- **Snapshot reads (`shared/excel_store.py:load_workbook_readonly`)**: all
  read-only workbook loads (17 in excel_store + 2 agent-local) now read the
  file bytes into memory first, so a concurrent writer can never crash a
  reader mid-stream; a half-written file fails the zip open immediately and
  is retried (3 attempts).
- **`get_company_archive_info`** rewritten to a single `iter_rows` pass —
  per-cell access on a read-only sheet re-parses the sheet XML per call
  (O(rows²)). The other read helpers share the pattern (correct but slow);
  noted in BUGS.md as a follow-up.

**Tests**: +9 (lock contention/fail-fast/wait-mode/release, snapshot
survival under truncation, retry-on-half-written-file, pipeline env export).
Full suite 1042 passed / 1 skipped.

## 2026-07-13

### Job agent: canonical-URL dedup (BUG-71) + intern filter (BUG-72)

User review of the first post-launch JD batches found duplicate postings
surviving triage and internship roles in JD_Tracker.

**Fixes**:
- **Canonical JD URLs (`shared/excel_store.py:canonical_jd_url`)**: all JD
  dedup layers (BUG-65 triage exclusion, fresh/stale sets, BUG-04 in-run
  guard, upsert row index) now compare canonical URL forms instead of exact
  strings, so the same posting rediscovered under tracking-param or
  host/path variants (LinkedIn, Tesla, Greenhouse hosts) never re-scrapes or
  duplicates. `get_triaged_jd_urls` now returns canonical URLs — callers
  must canonicalize before membership tests. 3 pre-existing duplicate rows
  removed from `Skipped JD` (backup: `pathfinder_dashboard.backup-20260713.xlsx`).
- **Intern filter (`agents/job_agent.py`)**: intern / co-op / new-grad
  titles are dropped pre-scrape in `_tpm_filter` and at write time in
  `_gate_and_finalize` (Gate 1.5) — they previously slipped through the YoE
  gate because student JDs state no minimum YoE.

## 2026-07-10

### Company agent: discover-to-500 loop, blank-Track enrichment, Track sort

One company_agent run now tries to fill the whole 500-company universe, and
every enrichable gap in Company_List self-heals. Plan:
`.claude/plans/tingly-riding-meerkat.md`.

**Features**:
- **Discovery loop (`run_discovery_loop`)**: replaces the single
  50-company batch — discovery repeats in `BATCH_SIZE=50` batches until all
  track buckets hit quota (`MAX_TOTAL=500`), a batch yields nothing new
  (Tavily quota exhausted / query-pool convergence — retried next run), or
  the 10-iteration runaway cap fires. Exclusion list re-read every iteration;
  run summary accumulates attempted/succeeded/failed and notes the stop
  reason.
- **Blank-Track enrichment (`run_enrich_missing_tracks`)**: blank/N-A `Track`
  cells (typical of manual name-only inserts) are classified every run via
  the batched Gemini classifier shared with `--migrate-tracks`
  (`_classify_tracks_batch`). Unconfident rows stay blank for retry; custom
  values (incl. `UNMIGRATED — manual review`) are never touched; defense
  legacy primes deterministically forced to Mid-large Tech. With the existing
  Career-URL backfill (Phase 1.5) and Business Focus re-enrich (BUG-69), a
  manual row with just a name now fully self-heals: URL → focus → track.
- **Company_List sorted by Track (`sort_company_list_by_track`)**: final
  step of every run — canonical `TRACK_ORDER` (now in `shared/config.py`,
  shared source of truth), name-alphabetical within a track, unknown/custom
  tracks sink to the bottom. Count-preserving in-place rewrite (the
  `sort_jd_tracker_by_tier` pattern); all 9 columns travel with their row.

**Notes**:
- The 2026-07-09 "attempted 50 / succeeded 0" run was Tavily plan-limit
  exhaustion, not a code defect — discovery stops cleanly on quota and
  resumes next run once the Tavily quota resets.

### Tavily key pool (BUG-70)

- **`shared/tavily_pool.py`** (new, mirror of the BUG-68 Firecrawl pool):
  `TAVILY_API_KEY` + optional `TAVILY_API_KEY_2`, rotation on quota errors —
  now **including Tavily's real plan-limit text "exceeds your plan's set
  usage limit"**, which contains none of 402/429/quota and therefore evaded
  every existing quota-abort check on the 2026-07-09 run. Once ALL keys are
  exhausted: one loud console warning, then instant `TavilyQuotaExhausted`
  whose message contains "429"+"quota" so all existing call-site checks
  recognize it unchanged (search has no free fallback — callers abort via
  exception, unlike Firecrawl's None-return).
- Wired everywhere a `TavilyClient` was built: company_agent `main()` builds
  ONE pool and threads it through discovery loop → Phase 1.5 → focus
  re-enrich (exhaustion state carries across steps); job_agent posting-date
  backfill client replaced. The pool is duck-type compatible (`.search`), so
  downstream signatures are unchanged.

**Tests**: +13 loop/enrich/sort (above) +14 tavily_pool (rotation incl.
real usage-limit text, one-warning exhaustion, call-site-check-compatible
message, non-quota re-raise, caching, env pickup).

## 2026-07-09

### Excel-review follow-up batch (BUG-62, BUG-65~69 + Google adapter T16)

Post-launch fixes from the user's manual review of the dashboard. Plan:
`.claude/plans/lovely-tinkering-bumblebee.md`; details in BUGS.md.

**Features**:
- **User triage tabs (BUG-65)**: `JD_ToApply` and `Skipped JD` tabs are now
  first-class — URLs the user moves there are permanently excluded from
  re-scraping/re-insertion (`get_triaged_jd_urls` + exclusion in
  `process_company` and the retry phase). Tabs auto-created on new/migrated
  workbooks; existing user rows never touched. Do not rename the tabs.
- **Google Careers discovery adapter (T16 / REQ-004-18, was deferred)**:
  `_fetch_google_jobs` paginates the server-rendered careers search page
  (`AF_initDataCallback` payload) — titles, locations, posted dates AND
  prefetched JD text with zero Firecrawl/browser calls. Documented deviation:
  design.md's `careers.google.com/api/v3/search/` endpoint is dead (404).
  Live check 2026-07-09: 291 postings → 106 TPM candidates.
- **Firecrawl key pool (BUG-68)**: new `shared/firecrawl_pool.py` —
  `FIRECRAWL_API_KEY` + optional `FIRECRAWL_API_KEY_2`, rotation on
  402/429, one loud console warning when all keys are exhausted, instant
  fallback to free scrapers afterwards (no wasted retry delays).
- **Business Focus self-heal (BUG-69)**: blank/N-A `Business Focus` cells are
  re-enriched every company_agent run (Tavily context + one batched Gemini
  call), mirroring the existing blank-Career-URL backfill.

**Fixes**:
- **Geo filter (BUG-66)**: extraction prompt now captures ALL locations
  verbatim (was US-only, which blinded the gate); the write-time gate block
  is factored into `_gate_and_finalize` and runs on EVERY path — custom
  scrapers (Microsoft/Tesla/Google/Workday/prefetched) and the incomplete-row
  retry included. **User-visible tightening**: retry/custom paths now also
  drop non-WA/CA/TX US rows per REQ-004-12. Dead `_is_us*`/pycountry code
  removed (`classify_region` is the live filter; pycountry no longer used).
- **Posted dates (BUG-67)**: shared JSON-LD parser now reads `datePosted`;
  date chain = ATS list-meta → scrape-time JSON-LD → one plain-GET JSON-LD →
  Tavily backfill → keep+flag. `retry_one` no longer wipes existing Posted
  Dates (the Zebra/Workday blank-date cause). Workday "Just Posted" parsed.
- **BUG-62**: empty-company extractions now write a sheet-visible
  JSON-ERROR audit row instead of silently retrying forever.

**Tests**: 945 → 995+ (new: firecrawl_pool suite, triage exclusion, geo-gate
matrix, JSON-LD parser, date fallback chain, Google adapter fixtures,
Business Focus re-enrich; removed: dead `_is_us*` tests).

## 2026-07-07

### PRJ-004: Multi-Track Expansion (breaking schema change)

Expanded the pipeline from AI-only/200-company scope to six tracks at 500
companies (AI-native 150 / Mid-large Tech 150 / Robotics 50 / Fintech 50 /
Space 50 / Defense 50), with freshness, YoE-seniority, and work-authorization
filtering. Full SDLC cycle: `docs/sdlc/PRJ-004-multi-track-expansion/`.

**Breaking (Excel schema)**:
- `JD_Tracker`: `Is AI TPM` → `Job Domain` (AI/Robotics/Fintech/Space/Defense);
  `Location Tier` → `Sort Tier` (combined 1–6 freshness×region index, 9 = sink);
  new `Posted Date`, `Freshness Tier`, `Min YoE`, `YoE Flag`, `Work-Auth
  Status`, `Date Flag` columns. Legacy header sets are replaced wholesale
  behind an assert-empty guard (RuntimeError if legacy data rows exist —
  wipe is a user-owned pre-launch step).
- `Company_List`/`Company_Without_TPM`: `AI Domain` → `Track`, `AI TPM Jobs`
  → `Qualified Jobs` (in-place header rename; values re-bucketed via the new
  one-time `python agents/company_agent.py --migrate-tracks` CLI).

**Features**:
- company_agent: 6-bucket taxonomy with per-bucket quotas + grandfathering,
  early-company rule (~2000+), defense legacy-prime hard exclusion with
  Palantir allowlist, hires-in-region geography, `--migrate-tracks`.
- job_agent: 5-track domain classifier with big-tech sub-org mapping anchors
  (cloud→AI, payments→Fintech, Kuiper/Leo→Space, Azure Government→Defense);
  YoE window gate (skip stated min ≤3 or ≥12; unstated → keep + flag);
  global work-authorization screen (citizenship/clearance → skip, audited);
  posting-date extraction across all ATS adapters + ≤14-day write-time
  freshness gate (never retroactive) + optional Tavily LinkedIn-scoped date
  backfill; geo tightened to Seattle/CA/TX/US-Remote; Workday pagination
  (removes the 20-job cap); Firecrawl map uncapped; new Amazon.jobs adapter
  with prefetched JD text (zero crawler fallback).
- match layer: 5 per-track Recruiter/HM prompt pairs + tailor emphasis
  clauses; routing by Job Domain in match_agent and resume_optimizer with
  per-track byte-identity preserved (REQ-052); per-track Gemini context
  caches; batches never mix tracks.
- ops: launchd wrapper writes `logs/LAST_RUN_FAILED`/`LAST_RUN_OK` markers;
  all agents log Gemini token usage into RunSummary (trial-run cost carrier).

**Tests**: 859 → 945 (+86, incl. Phase 4 QA follow-ups), 0 regressions.
**Backlog**: BUG-62, BUG-63. Google Careers adapter (P2 stretch) deferred.


## 2026-06-10

### Docs: accuracy pass across README / PROJECT_OVERVIEW / ARCHITECTURE / REQUIREMENTS / CHANGELOG

Five-doc review against the codebase fixed every factual error found:
script name `run_daily_pipeline.sh` → `run_pipeline_scheduled.sh` (4 docs);
Workday-via-Tavily fallback re-attributed from Job Agent to Company Agent
(README); "~4K lines of Python" → "~6K" (README); `GEMINI_API_KEY_2` and the
macOS `brew install pango` prerequisite added to setup docs; "read-only"
subagent claims reworded to "no Edit/Write tools (analysis-only by design)"
(4 docs — `Bash` is in `allowed-tools`, so read-only was not tool-enforced);
`agent-reviewer.md` pinned `model: opus` to match its documented tier;
`api-debugger.md` stale `-preview` model name updated; ARCHITECTURE
JD_Tracker schema gained the missing `ATS Keywords` column, diagram
"Fine eval (Top 20)" → 3-dim labels (UNION 60%), stale "two-stage" comment
fixed, `scripts/` tree completed; REQUIREMENTS fixed REQ-006/007 (5 search
categories at 25/20/25/20/10), REQ-056 (`BATCH_RESCORE_SIZE` never existed —
rescore is `RESCORE_CONCURRENCY=3`), REQ-122 (MD5, not SHA), REQ-101 line
count, and the "8 Custom Agents" → 11 count; CHANGELOG corrected four wrong
P0-9~12 commit hashes and backfilled missing entries (P0-1~8, P1 batch 1,
2026-04-21 public release prep). Docs + agent-definition frontmatter only;
no runtime code changes.

### Docs: reframe the AI SDLC team as a four-group org under the Architect / Product Owner

The previous "11-agent Claude subagent review team" framing was inaccurate on
three counts: it labeled PM/TPM as reviewers (they author requirements and
milestones), routed the org chart through Claude Code as a middle layer
(the four groups report to the user directly), and called all subagents
"strictly read-only" while SDLC artifacts are authored by them. README,
PROJECT_OVERVIEW, CLAUDE.md, and ARCHITECTURE now describe **four groups under
the user (Architect / Product Owner)** — Planning (PM + TPM: requirements &
milestones), Implementation (Claude Code Opus: sole code write access, runtime
dispatch harness), Quality (6 agents: test plans & reviews), Evaluation
(3 agents: eval & cost reports) — plus an explicit delivery path (architecture
→ requirements → milestones → implementation → testing → eval → launch) and a
write-permission model note (groups author deliverables; only Claude Code
writes code). Docs-only change; no code or agent-definition changes.

## 2026-05-20

### Manual-entry override: backfill Career URL for hand-inserted Company_List rows

`company_agent.run_phase_1_5` now detects `Company_List` rows that were inserted
manually (`Company Name` + `AI Domain` present, `Career URL` blank) and runs the
full `find_career_url` discovery pipeline on them — Tavily ATS-targeted search →
Tavily general search → Greenhouse / Lever / Ashby / Workable slug probes →
Workday-via-Tavily fallback → homepage crawl — then writes the result back.
Tavily is a hard requirement for this path; rows that resist discovery are
reported and left blank for the next run to retry. `AI Domain` accepts any of
the 6 whitelisted buckets *or* a custom string (job_agent treats unknown
buckets as `ai_native`). Lets the user drop in a target company list without
also hunting down their ATS URLs by hand.

### Gemini model name → GA (`gemini-3.1-flash-lite`)

`shared/config.py:MODEL` updated from `gemini-3.1-flash-lite-preview` to
`gemini-3.1-flash-lite` (the GA name). Google deprecates the `-preview` alias
on 2026-05-25; this is a pure rename — same model, same weights, same prompts,
same costs, scores comparable across the rename. No call-site changes.

## 2026-05-05

### Discovery coverage: Workday-via-Tavily fallback (with strict subdomain guard)

Follow-up to the Workable + URL-unwrap work below. Many big-tech and infra companies (Adobe, Cisco, Qualcomm, Salesforce, Dell, Broadcom, Cadence, DataRobot, Zendesk, PayPal, Equinix, etc.) use Workday with **unguessable subdomains** (`adobe.wd5`, `cisco.wd5`, `qualcomm.wd12`, `paypal.wd1`, …), so slug-probing can never find them. They were stuck at custom career pages or LinkedIn URLs.

- **`_find_workday_url(company_name, tavily_client)`** in `agents/company_agent.py`: queries Tavily with `"<company>" careers site:myworkdayjobs.com` and returns the first hit on `myworkdayjobs.com` whose subdomain matches the company name.
- **`_workday_subdomain_matches_company(url, company_name)`** — critical false-positive guard. Tavily's site filter happily returns URLs from *other* companies that happen to mention the search term in their JD pages: AMD → `argonne.wd1` (Argonne Lab JDs reference AMD silicon), Oracle → `pwc.wd3` (PwC Oracle-consulting roles), Apple → `applebank.wd5`, Western Digital → `westernunion.wd5`, Clay → `claycountybcc.wd1`. Initial fuzzy `c_compact in sub_compact` matching let the substring cases through; tightened to **exact compact-form equality** (each slug candidate must equal the subdomain after stripping non-alphanumerics — no prefix or substring rules).
- Wired as **step 4** in `validate_and_upgrade_ats_url` (after slug-probe misses); only fires when a `tavily_client` is supplied. `run_phase_1_5` now auto-instantiates a Tavily client from `TAVILY_API_KEY` if none is passed.

**Phase-1.5 re-run results** (against `pathfinder_dashboard.xlsx`, 75 non-ATS candidate rows): **26 upgrades, 0 false positives**.
- Workday recoveries (11): Adobe, Cisco, Qualcomm, Salesforce, Dell Technologies, Broadcom, Cadence Design Systems, DataRobot, Zendesk, PayPal, Equinix — all → their real `*.wdN.myworkdayjobs.com` boards.
- Plus 15 Ashby + unwrap wins from the prior iteration (Sierra, Cursor, Weaviate, Modal Labs, Baseten, Replit, Runway, Suno, Gamma, Applied Materials, ElevenLabs, Mercor, Nutanix, EliseAI, Physical Intelligence).

**Tests**: +18 added (`TestWorkdaySubdomainMatch` 9 cases including the 3 real false-positive guards; `TestFindWorkdayUrl` 8 cases; `TestValidateAndUpgradeWorkdayFallback` 4 cases). Full suite 854/854 green.

---

### Discovery coverage: Workable ATS + LinkedIn / VC-portfolio URL unwrapping

Audit of the 146-company `Company_List` sheet showed two coverage gaps that were *not* unsupported scrapers but unsupported *URL shapes*: 8 companies stuck at `linkedin.com/jobs/<slug>-jobs` and 4 stuck behind VC-portfolio job-listing wrappers (a16z / battery / gaingels / 01a). Plus Hugging Face routed through the Firecrawl crawl path despite Workable having a clean public API.

- **A′ — Workable as first-class ATS**: added `workable` entry to `ATS_PLATFORMS` (`agents/job_agent.py`) and a dedicated `_fetch_workable_jobs` (mirrors `_fetch_ashby_jobs` — Workable's widget API uses multi-field `city/state/country/locations[]/telecommuting` rather than a single `location` dict). Endpoint: `https://apply.workable.com/api/v1/widget/accounts/{slug}` (the older `/api/v3/accounts/...` path 404s). Added Workable to `ATS_VALIDATORS` in `agents/company_agent.py` so slug-probing also covers it during phase 1.5.
- **A″ — wrapper URL unwrapper**: new `_unwrap_career_url(url)` helper in `agents/company_agent.py` extracts the underlying company slug from `linkedin.com/jobs/<slug>-jobs`, `linkedin.com/company/<slug>/jobs`, and `jobs.<vc>.com/jobs/<slug>` for the four known VC-portfolio hosts. Wired into `validate_and_upgrade_ats_url` between the hard-coded-override step and the already-ATS short-circuit; on a wrapper match, the extracted slug feeds `_check_ats_slug` against all validators. If the underlying company is on a public ATS, `current_url` is rewritten to the real board URL.
- **Ashby in `ATS_VALIDATORS`** (pre-existing gap, surfaced during A″ verification): `company_agent.py`'s slug-probe validators previously only covered Greenhouse + Lever, so any company on Ashby (a heavily-used AI-startup ATS) silently fell through to homepage scraping. Added Ashby to `ATS_VALIDATORS` — fixes both the new A″ unwrap path AND the existing `find_career_url` / `validate_and_upgrade_ats_url` step-3 slug-probe. Added regression test `test_validators_cover_all_json_api_platforms`.

**Phase-1.5 re-run results** (against `pathfinder_dashboard.xlsx`, 75 non-ATS candidate rows): **16 upgrades**.
- A″ unwrap wins (4): Modal Labs, Baseten, Replit, EliseAI — all → Ashby.
- Ashby-validator wins (12): Snowflake, Sierra, Cursor, Weaviate, Runway, Suno, Gamma, Applied Materials, ElevenLabs, Mercor, Nutanix, Physical Intelligence — all were stuck on custom career URLs, now routed to their Ashby boards.

**Tests**: +36 added by this work (17 Workable in `tests/test_job_agent.py`: `TestWorkableAtsConfig` / `TestFormatWorkableLocation` / `TestFetchWorkableSlugExtraction` / `TestFetchWorkableApiParsing` / `TestDiscoverJobsWorkableRouting`; 19 in `tests/test_company_agent.py`: `TestUnwrapCareerUrl` / `TestValidateAndUpgradeUnwraps` + `test_validators_cover_all_json_api_platforms`). Full suite 833/833 green. All mocked — no live network calls.

### Self-audit fixes: TPM scraping coverage + classifier strictness

Self-audit (two parallel `Explore` subagents) of the AI-TPM job pipeline surfaced three concrete defects, all fixed in `agents/job_agent.py`:

- **Workable + Workday fallback search params** (`ATS_SEARCH_PARAM`): primary JSON-API path was already TPM-filtered, but the fallback `_crawl_ats_board` crawler had no search keyword for these two platforms, so when the API returned empty the crawler would scrape the entire job board un-filtered. Added `workable: query=…` and `workday: q=…` (the standard public search-URL syntax for each platform). Defense-in-depth — affects ~20 companies (Hugging Face, Intel, HPE, NVIDIA, Pinecone, etc.) when their JSON API hiccups.
- **Compliance/SOX/audit/governance/GRC title block** (`_TITLE_BLOCK_KW`): Big-Tech title prefilter was admitting roles like "TPM, SOX Compliance" and "TPM, GRC Audit" as candidates. The downstream LLM classifier also accepted some as `is_ai_tpm=True` because the prompt's domain-exclusion list omitted compliance/audit. Added 5 keywords to the title prefilter so these never reach Gemini.
- **Big Tech `is_ai_tpm` prompt**: appended explicit ACCEPT/REJECT few-shot examples (ML Infrastructure / GPU Cluster / Foundation Models vs. SOX / Marketing-Tech / Trust & Safety / Federal-clearance) and added the same compliance keywords to the natural-language exclusion list. Reduces false positives on JDs whose titles slipped past the title prefilter.

**Tests**: 802 → 805 (+3). New `TestAiTitlePrefilter` class regression-tests the compliance keyword block; `test_search_params_exist_for_main_ats` extended to require Workable + Workday entries.

### Self-audit P1 follow-up: silent-failure observability + retries + Microsoft JD

Round-2 fixes for the P1 items the self-audit flagged but the first batch deferred. All in `agents/job_agent.py`.

- **HTTP retry helper** (`_http_request_with_retry`): new module-level function with exponential backoff (0.5/1.0/2.0s) on 429 + 5xx + transient network exceptions. All 4 ATS API fetchers (`_fetch_ats_jobs` for Greenhouse/Lever, `_fetch_ashby_jobs`, `_fetch_workable_jobs`, `_fetch_workday_jobs`) routed through the helper — used to be `requests.get` once, return `[]` on any non-200, no retry, no URL in the error log. Now: 3 attempts, sleep between attempts, every retry/failure logs URL + status + attempt number. The helper dispatches to `requests.get` / `requests.post` (rather than `requests.request`) so the 50+ existing test patches that mock those names continue to work unchanged. Backoff base is the module-level constant `_RETRY_BASE_SLEEP_SECS` (default 0.5s, set to 0 in tests).
- **Error-context logging** at the 4 remaining swallow sites: `_crawl_page` / `scrape_jd` / `llm_filter_jobs` / `extract_jd` previously logged just the exception message. Now log the URL (or company + JD char count + classification class for the LLM calls) and the exception type. "API down" no longer looks like "zero jobs" in the run log.
- **Microsoft careers JD scraper** (`_scrape_microsoft_jd`): mirrors `_scrape_google_jd`. Routes URLs at `jobs.careers.microsoft.com` and `careers.microsoft.com` through Firecrawl with `only_main_content=True`, falls back to plain HTTP + JSON-LD if Firecrawl is unavailable, falls back further to the generic browser scraper if both miss. Closes the audit's "40-50% of cached MS JDs were nav-chrome" finding. Wired via the existing `ATS_PLATFORMS` table + `_JD_FN_REGISTRY` so no router code changed.

**Confidence/reasoning fields on `JobDetails`** (audit P2): deliberately deferred. Adding it would touch `excel_store.py` (column migration) + `match_agent.py` + `resume_optimizer.py` (downstream consumers must read it); >3 files violates the project's task-size rule. Will land as its own PR.

**Tests**: 805 → 832 (+27, includes prior parallel session's 805→820 baseline shift). New `TestHttpRetryHelper` (7) + `TestMicrosoftJdScraper` (5) classes in `tests/test_job_agent.py`. The retry helper is exercised against 200/4xx/429/5xx/exception scenarios; the Microsoft scraper is asserted to route via `_match_ats` and to call Firecrawl with `only_main_content=True`. Existing ATS fetcher tests still patch `requests.get`/`requests.post` directly; module-level `_RETRY_BASE_SLEEP_SECS = 0` keeps the suite snappy despite the new retries.

### PRJ-003: PDF Resume I/O + Claude Opus Tailor Evaluation

Three additions, all opt-in / passive:

- **PDF → MD resume input** (no LLM): drop a `.pdf` into `profile/` and the agents auto-convert via `pdfplumber`. Conversion is deterministic, layout-aware (detects section headers by font size + all-caps, preserves bullet structure), and cached at `profile/.cache/{stem}.{md5}.md` — re-runs are zero-cost. Picker priority is `.md > .txt > .pdf` so a hand-edited `.md` always wins. `resume_id` is preserved across formats so existing Excel keys remain stable. Implemented in `shared/resume_io.py`; `agents/match_agent.py` and `agents/resume_optimizer.py` now import the shared loader (local copies removed).
- **MD → PDF tailored output (ATS-safe)**: every tailored resume written to `tailored_resumes/{resume_id}/{md5}.md` now also gets a sibling `{md5}.pdf` rendered via WeasyPrint. The CSS template (`templates/resume.css`) enforces ATS-safe rules: single column, standard fonts (Helvetica / Arial / Times / Calibri / Georgia stack), no images / headers / footers / multi-column, real Unicode bullets, selectable text. Font family + body size are captured from the input PDF (when present) and injected as CSS variables so output mimics source typography while staying ATS-safe; body size clamps to [9pt, 12pt]. PDF generation failures log a warning but never block the pipeline — the `.md` remains source of truth for Excel records.
- **Claude Opus 4.7 tailor evaluation memo** (`docs/sdlc/PRJ-003-pdf-io-opus-eval/eval.md`): cost / latency / quality analysis vs current Gemini 3.1 Flash Lite for the `tailor_resume` step. **Verdict**: not as a full swap — Opus is ~170× more expensive per call and ~2–3× slower; recommend opt-in fallback that fires only on regressions and user-flagged priority JDs.

**System dependency**: WeasyPrint needs Pango/Cairo. On macOS: `brew install pango` (one-time).

**Tests**: 736 → 749 (+13 in `tests/test_resume_io.py`), all passing.

**Reference docs**: `docs/sdlc/PRJ-003-pdf-io-opus-eval/` (status + eval memo).

### Tailored-resume user-edit protection

`resume_optimizer._save_tailored_resume` now refuses to overwrite a tailored
resume that the user hand-edited between runs. Mechanism: each successful
write records a `sha256` of the `.md` content in a new
`Tailored_Match_Results.Last Written Hash` column; before any subsequent
write, the on-disk hash is compared against that record — mismatch ⇒ skip
both the `.md` write and the sibling `.pdf` render, and exclude the pair from
that run's Excel update so the row stays aligned with the on-disk
(user-edited) file. Legacy rows (empty `Last Written Hash`) are treated as
"no prior write on file" and write normally on first run (no false-positive
tamper detection). New `--force-rewrite` CLI flag bypasses the check.
Motivation: daily scheduled re-runs (see launchd entry below) would
otherwise silently clobber polish the user applied by hand.

**Tests**: +13 in `tests/test_resume_optimizer.py` (`TestUserEditProtection`)
and `tests/test_excel_store.py` (`TestLastWrittenHash`).

### JD_Tracker auto-sort by location tier

After each `job_agent` run, `JD_Tracker` is now sorted into three location
tiers — **Greater Seattle** → **Remote (US)** → **Other** — with a new
`Location Tier` column and row highlighting per tier. Tier classifier
recognises Seattle / Bellevue / Redmond / Kirkland / Greater Seattle
metro variants; Remote includes "Remote (US)", "United States — Remote",
etc. The sort is stable within-tier (preserves prior recency order). Lets
the user scan the highest-priority openings first without spreadsheet
filters.

### launchd-based daily pipeline runner

Added `scripts/run_pipeline_scheduled.sh` + sample `com.pathfinder.daily.plist`
that schedules the full 4-agent pipeline (company → job → match →
optimizer) via macOS `launchd`. Logs land in `logs/` (gitignored).
`.gitignore` updated to also exclude `worktrees/` (Claude Code agent
isolation artifacts). Replaces ad-hoc terminal runs for users who want a
fire-and-forget daily refresh.

## 2026-04-28

### Major: 3-Dimension Scoring (PRJ-002)

The resume-fit scoring pipeline is restructured from a single LLM-derived "fit score" into three parallel dimensions that each map to one real-world hiring filter:

- **ATS Coverage** (deterministic keyword match, no LLM) — proxy for Applicant Tracking System / recruiter-keyword-search pass-through. Implemented in `shared/ats_matcher.py` (~120 lines, no new pip deps) with case-insensitive matching, lightweight plural stem (`-ies` / `-sses` / `-xes` / `-ches` / `-shes` / `-s`), and a hand-curated synonym table (~18 entries: GenAI≈Generative AI, K8s≈Kubernetes, LLM≈Large Language Model, etc.) in `shared/ats_synonyms.py`.
- **Recruiter Score** (Gemini, was COARSE) — quick recruiter-style 1-100 scan; renamed for clarity.
- **HM Score** (Gemini, was FINE) — hiring-manager 4-criteria deep evaluation (AI/ML Tech Depth 30% / TPM Function 30% / Domain 20% / Growth 20%); renamed for clarity.

**New behavior**:
- `agents/job_agent.py`: `JobDetails` extracts `ats_keywords: list[str]` (8-15 noun phrases) at JD ingest time.
- `agents/match_agent.py`: ATS coverage runs deterministically before Stage 1 for every pending JD; Recruiter and HM dimensions retain existing call shapes. Coarse / fine records switch to dict format with optional per-dim fields.
- `agents/resume_optimizer.py`: rescores all 3 dimensions after tailoring (1 deterministic ATS + 1 Recruiter Gemini + 1 HM Gemini per JD). Surfaces per-dimension delta so users can see ATS keyword gains separately from semantic strength changes.
- **Regression flag now means `HM Delta < 0` only** (was: legacy single-score delta < 0). ATS / Recruiter drops are informational — a tailor that shifts emphasis away from a recruiter keyword while preserving HM fit is fine and should NOT push the user to keep the base resume.

**Excel schema changes** (auto-migrated, backward compatible):
- `Match_Results`: +4 columns (ATS Coverage %, Recruiter Score, HM Score, ATS Missing). All legacy column indices preserved.
- `Tailored_Match_Results`: +9 columns (per-dim Original / Tailored / Delta × {ATS, Recruiter, HM}). Legacy `Original Score` / `Tailored Score` / `Score Delta` mirror the HM dimension for back-compat.
- Old rows: blank values for new cols. Re-run match_agent / optimizer to populate.
- `batch_upsert_match_records` and `batch_upsert_tailored_records` accept dict records with optional per-dim keys; key absent → preserve cell. Tuple-format records still supported for back-compat.

**Prompt rename pivot** (pure rename, content byte-identical):
- `COARSE_SYSTEM_PROMPT` → `RECRUITER_SYSTEM_PROMPT` (back-compat alias retained)
- `FINE_SYSTEM_PROMPT` → `HM_SYSTEM_PROMPT` (back-compat alias retained)

**Cost impact**: Resume Optimizer adds 1 Recruiter Gemini call per tailored JD (was: 1 tailor + 1 HM rescore; now: 1 tailor + 1 Recruiter + 1 HM). At 13 RPM shared rate limit, 10 tailored JDs takes ~45s extra. ATS dimension is deterministic — zero API cost.

**Reference docs**: `docs/sdlc/PRJ-002-3d-scoring/` (BRD + tech design + status).

**Tests**: 619 → 718 (+99 across 5 sequential PRs), all passing.

### P0 Code Review Follow-ups

- **P0-9** (`ef64907`): Stage 2 fine candidate selection switched from "top 20%" to UNION of (score >= `MATCH_FINE_SCORE_THRESHOLD`, top `MATCH_FINE_TOP_PERCENT%`). Protects against both flat-high distributions (where top-N% would discard genuine fits) and flat-low ones (where the absolute threshold would select nothing).
- **P0-10** (`6ffbe1b`): Optimizer rescore call shape unified with match_agent's fine eval. Dropped batch re-score (5 pairs/call) in favor of per-JD calls with `RESCORE_CONCURRENCY=3`. Eliminates ~3-5pt batch-context anchoring inflation in Score Delta. Removed unused `BATCH_FINE_SYSTEM_PROMPT` / `BatchMatchItem` / `BatchMatchResult`.
- **P0-11** (`fe600dd`): Gemini transient errors (5xx / UNAVAILABLE / timeout) now retry on the same key with bounded exponential backoff (2s / 4s / 8s + jitter) before raising. Quota / 429 still rotates keys (key-specific). Aligns with Gemini API guidance for retryable server-side errors.
- **P0-12** (`a3679ac`): `Tailored_Match_Results` gains a persisted `Regression` boolean column. Previously this signal was only printed at run time and lost between runs; users had to re-derive from Score Delta < 0.

### P0 Code Review Round 1 (P0-1 ~ P0-8) + P1 batch 1

*(Entries backfilled 2026-06-10 from git history — these shipped 2026-04-28 alongside PRJ-002 but were not recorded at the time.)*

- **P0-1** (`764fb49`): Enable Gemini Context Caching for the resume + system prompt, cutting repeated-token cost on multi-JD scoring runs.
- **P0-2** (`700a55d`): Extract `_FINE_SYSTEM_PROMPT` and `MatchResult` from `match_agent` into `shared/` — single source of truth for the optimizer's rescore path.
- **P0-3** (`5094644`): Wrap scraped JD/resume content in `<scraped_content>` delimiters as a prompt-injection boundary for LLM calls.
- **P0-4** (`b471393`): Classify Gemini exceptions (transient vs. structural); remove fake-score fallbacks so failures surface instead of writing fabricated scores.
- **P0-5** (`72a2993`): `upsert_companies` preserves TPM counts on existing rows instead of resetting them.
- **P0-6** (`1521e35`): `get_scored_matches` filters by `stage='fine'` by default, so downstream consumers no longer see coarse-only rows.
- **P0-7** (`591682f`): Add `RunSummary` dataclass and structured per-run logs (`shared/run_summary.py`).
- **P0-8** (`b4e7ef5`): Remove redundant `_quick_keyword_score` pre-filter from match_agent.
- **P1 batch 1** (`5e69291` code, `0bd67ac` skills): `JD_CACHE_DIR` moved to `shared/config.py`; `usage_metadata` capture in `gemini_pool.py`; schema clamp; `/test-one` mapping expanded to 15 entries; `/check-env` scanner generalized.

## 2026-04-21

### Public release preparation

*(Entry backfilled 2026-06-10 from git history.)*

Repo prepared for public visibility (`debc5dd`): MIT `LICENSE` added; `PROJECT_OVERVIEW.md` created (curated technical overview for recruiters/hiring managers, 112 lines); README rewritten for a public audience; `.gitignore` additions.

## 2026-03-17

### Rename: Simplified Excel Tab Names (Removed Redundant "AI_" Prefix)

- `AI_Company_List` → `Company_List`, `AI_Company_Without_TPM` → `Company_Without_TPM`
- **Migration compatibility**: `get_or_create_excel()` added sheet rename migration logic; existing Excel files automatically rename old tabs
- **Code updates**: Global replacement across `shared/excel_store.py`, `agents/company_agent.py`, `tests/test_excel_store.py`
- **Documentation updates**: REQUIREMENTS.md, ARCHITECTURE.md, brd.md, observability.md, schema-validator.md

### New: Observability Agent + Cost Agent

- **`.claude/agents/observability.md`** — Added Observability Agent (sonnet, Quality layer)
  - Mode A: Run Report — Global overview of metrics across 4 Agents (company count, JD count, score distribution, optimization effectiveness)
  - Mode B: Quality Drift Detection — Score distribution shift, JD extraction degradation, AI TPM classification drift, Score Delta trends
  - Mode C: Anomaly Detection — All-zero scores, score clustering, orphan records, Excel corruption, Schema mismatches
- **`.claude/agents/cost.md`** — Added Cost Agent (sonnet, Quality layer)
  - Mode A: Token Usage Estimation — Gemini/Tavily/Firecrawl call counts and token consumption estimates per Agent
  - Mode B: API Quota Check — Gemini RPD/RPM/TPM, Tavily monthly search count, Firecrawl credits quota status
  - Mode C: Optimization Recommendations — Prompt length optimization, batch size tuning, pre-filter threshold analysis, cache hit rate evaluation
- **Documentation updates** — CLAUDE.md (9 → 11 agents), ARCHITECTURE.md (layered diagram + agents table + file structure)

## 2026-03-16

### Bug Fixes: Full Code Audit (BUG-28~55)

- **BUG-28 (P1)**: Fixed retry phase executing outside `AsyncWebCrawler` context, causing crawler to become inactive
- **BUG-29 (P2)**: `_GeminiKeyPool` empty key list protection (via inheriting `_GeminiKeyPoolBase`)
- **BUG-30 (P2)**: Added missing `wb.close()` to `_print_top_results()`
- **BUG-31 (P3)**: Unified duplicate `_GeminiKeyPool` subclasses across 4 agents to `_GeminiKeyPoolBase` alias
- **BUG-32 (P0)**: `_scrape_tesla_jd` imported incorrect `Firecrawl` class name; corrected to `FirecrawlApp`
- **BUG-33 (P0)**: `d.get("requirements", [])` still returns `None` when Gemini returns `null`; changed to `(d.get(...) or [])`
- **BUG-34 (P1)**: `gemini_pool` created a new Client on each call; changed to per-key cached `_clients` dictionary
- **BUG-35 (P1)**: `rotate()` incremented one-way without wrapping; changed to `% len` round-robin
- **BUG-36 (P1)**: `_idx`/`rotate()` had no lock protection; added `threading.Lock`
- **BUG-37 (P1)**: `_firecrawl_map` synchronously blocked the event loop; call site changed to `asyncio.to_thread`
- **BUG-38 (P1)**: Semaphore+RateLimiter stacking caused actual RPM to exceed limits; moved Gemini calls outside Semaphore block
- **BUG-39 (P2)**: `_load_jd_markdown` did not read `_structured.md`; changed to prioritize structured version
- **BUG-40 (P2)**: Python operator precedence error (`or "" if cond else ""`); added parentheses at 4 locations
- **BUG-41 (P2)**: Stage 1/2 had independent `_RateLimiter` instances; changed to module-level shared `_GEMINI_LIMITER`
- **BUG-42 (P2)**: `asyncio.gather(return_exceptions=True)` silently swallowed 429 errors; added quota warning
- **BUG-43 (P2)**: `_scrape_google_jd` was missing `formats=["markdown"]`
- **BUG-44 (P2)**: Tavily quota exhaustion was not handled separately; added 402/429/quota detection and warning
- **BUG-45 (P2)**: `upsert_companies` only wrote 5 columns; expanded to 9 columns covering all COMPANY_HEADERS
- **BUG-46 (P2)**: `batch_update_jd_timestamps` docstring had inconsistent column number
- **BUG-47 (P2)**: `data_quality` field was not included in the `JobDetails` Pydantic model
- **BUG-48 (P2)**: `[{}] * N` created shared references; changed to list comprehension
- **BUG-49 (P2)**: `discover_ai_companies` directly modified the caller's set; changed to local copy
- **BUG-50 (P3)**: `_print_summary` hardcoded column numbers changed to `TAILORED_HEADERS.index()` dynamic lookup
- **BUG-51 (P3)**: `_print_top_results` hardcoded column numbers changed to `MATCH_HEADERS.index()` dynamic lookup
- **BUG-52 (P3)**: 7 JD_Tracker functions with hardcoded column numbers changed to `_JD_COL` mapping
- **BUG-53 (P3)**: `_fmt_addr` was repeatedly defined inside the loop; moved before the loop
- **BUG-54 (P3)**: Added `_KEY_POOL` None guards across 4 agents (9 functions)
- **BUG-55 (P2)**: `WITHOUT_TPM_HEADERS` expanded from 5 columns to 7 columns + migration logic

### New: Job Agent Enhancements (REQ-058~063)

- **REQ-058 (P1)**: Ashby upgraded to API_ATS — Uses public Job Posting API for structured JSON retrieval
- **REQ-059 (P1)**: Soft 404 hardening + JD positive signal validation — Expanded keyword set + positive feature word threshold detection
- **REQ-060 (P2)**: JD field completeness grading — `_assess_jd_quality()` + `Data Quality` column
- **REQ-061 (P2)**: Workday URL format expansion — Support for formats without `wd` prefix
- **REQ-062 (P3)**: ATS declarative routing table refactor — `ATS_PLATFORMS` dict + `_match_ats()` router
- **REQ-063 (P3)**: Auto-archive companies with no TPM positions — `No TPM Count`/`Auto Archived` columns + 5 management functions

### Improvements: Gemini Pool Refactor

- **`shared/gemini_pool.py`** — Unified to `_GeminiKeyPoolBase` base class
  - `genai_mod` parameter enables unified `generate_content` method
  - `_clients` dictionary caches Client instances per key
  - `rotate()` changed to round-robin (`% len`)
  - `threading.Lock` protects `_idx`/`rotate()`/`_get_client()`
  - Subclass definitions in 4 agents changed to `_GeminiKeyPool = _GeminiKeyPoolBase` alias

### New: SDLC Workflow Framework (TPM Agent + Coordination Layer)

- **`.claude/agents/tpm.md`** — Added TPM Agent (opus): Coordinates full SDLC workflow
  - 7 operation modes: kickoff / review-brd / review-design / coordinate / status / launch / fast-track
  - Document-driven communication, `status.md` as single source of truth
  - Escalation mechanism: L1 Info → L2 Decision → L3 Business [ESCALATE] → L4 Blocker [BLOCKED]
- **`.claude/agents/product-manager.md`** — PM Agent added 2 new modes
  - Mode F: BRD Writing (research feasibility, generate structured BRD draft)
  - Mode G: Testing Sign-off (evaluate test results against BRD success criteria, output sign-off)
  - description updated to "Research, feasibility analysis, BRD writing, testing sign-off"
- **`docs/sdlc/`** — SDLC project documentation directory
  - `index.md` project index table
  - Each project in its own directory `PRJ-xxx-<name>/` (status.md, brd.md, tech-design.md, reviews/)
- **`.claude/skills/`** — 3 new SDLC Skills
  - `/sdlc-init` — Initialize project (assign ID, create directory and templates)
  - `/sdlc-status` — View project status (single project details or global overview)
  - `/sdlc-review` — Trigger stage-specific reviews (brd/design/testing/launch)
- **SDLC 5-phase workflow** — BRD → Tech Design → Implementation → Testing → Launch
  - Roles: User(Business Owner) + PM + TPM + Engineer Lead(Claude Code) + QA Team(6 agents)
- **Architecture layer updates** — Planning layer + Coordination layer(new) + Quality layer + Operations layer
- **Documentation updates** — ARCHITECTURE.md v1.3, CLAUDE.md (8 agents + 8 skills), CHANGELOG.md

### New: Development Tooling Layer (Custom Agents + Skills)

- **`.claude/agents/`** — Added 7 Custom Agents (development assistance, read-only analysis)
  - `product-manager` (sonnet) — Requirements analysis, progress tracking, impact assessment, decision support
  - `agent-reviewer` (opus) — Code quality review, Prompt design, cross-Agent consistency
  - `schema-validator` (sonnet) — Excel schema and data contract validation
  - `test-analyzer` (sonnet) — Test failure analysis, coverage gap identification
  - `api-debugger` (sonnet) — Gemini/Tavily/Firecrawl/ATS API diagnostics
  - `doc-sync` (sonnet) — Code-to-documentation drift detection
  - `bug-tracker` (sonnet) — BUGS.md status verification, new bug scanning, regression test recommendations
- **`.claude/skills/`** — 5 operational execution Skills (pipeline, run-agent, test-all, test-one, check-env)
- **Architecture layers**: Planning layer(PM agent) → Quality layer(6 analysis agents) → Operations layer(5 skills)
- **Documentation updates** — ARCHITECTURE.md v1.2, CLAUDE.md, CHANGELOG.md

### Improvements: Score Floor + Coarse Prompt + Company Dedup

- **agents/match_agent.py** — Changed Gemini scoring minimum to 1
  - JDs scored by Gemini get a minimum score of 1; pre-filter rejected JDs remain at 0
  - `batch_coarse_score` default and fallback both changed to 1, return value `max(1, score)`
  - `evaluate_match` returns complete JSON (score=1) on exception, no longer returns empty `{}`
  - `main()` coarse_scores default changed to 1, fine eval parsed score clamped to 1
  - `resume_optimizer.get_scored_matches()` filters `score >= 0` to retrieve all match records for optimization

- **agents/match_agent.py** — Improved Coarse Scoring Prompt
  - Extracted inline prompt to `_COARSE_SYSTEM_PROMPT` module-level constant
  - Added 3 calibration anchor sections (1-30 weak match / 31-60 moderate / 61-100 strong match)
  - Listed 3 key scoring factors (AI/ML relevance, TPM function match, seniority match)

- **agents/company_agent.py** — Improved company name deduplication
  - Added `_normalize_company_name()`: lowercase + strip + remove common suffixes (Inc/Corp/LLC/Ltd/Technologies/Labs/AI/Platform/Systems/Computing)
  - Added `_is_duplicate_company()`: normalized matching + bidirectional startswith check (minimum >= 4 characters)
  - Replaced original exact-match dedup filter

- **tests/** — Added 21 test cases
  - `test_match_agent.py`: +2 score clamp tests, +3 prompt content tests, modified 3 existing assertions
  - `test_company_agent.py`: +8 normalization tests, +8 duplicate detection tests

### New Feature: Resume Optimizer Agent
- **agents/resume_optimizer.py** — 4th Agent, tailors and rewrites resume for each matched JD
  - Loads all match records with score >= 0, calls Gemini to generate tailored resume for each JD
  - Tailored resume only reorganizes/rewrites existing content, never fabricates experience (strict ATS optimization rules)
  - Re-scores using the same `_FINE_SYSTEM_PROMPT` as Match Agent, ensuring fair before/after comparison
  - Tailored resumes saved to `tailored_resumes/{resume_id}/{url_md5}.md`
  - Supports incremental updates: skips already-optimized pairs with unchanged resume_hash
  - Concurrency control: `asyncio.Semaphore(3)` + `_RateLimiter(rpm=13)`
- **shared/excel_store.py** — Added `Tailored_Match_Results` worksheet support
  - Added `TAILORED_HEADERS` constant (11 columns)
  - Added `get_scored_matches()` — Reads all match records with score >= 0
  - Added `get_tailored_match_pairs()` — Reads already-optimized records (for incremental skip)
  - Added `batch_upsert_tailored_records()` — Batch writes tailored match results
  - `get_or_create_excel()` auto-creates/migrates `Tailored_Match_Results` sheet
- **tests/** — Added `test_resume_optimizer.py` + extended `test_excel_store.py`
- **Documentation updates** — REQUIREMENTS.md (REQ-049~057), ARCHITECTURE.md (v1.1), CLAUDE.md

## 2026-03-14

### Bug Fixes
- **BUG-27 (P1)**: Fixed incomplete JD records not being reprocessed by the main loop
  - `get_jd_url_meta` originally only excluded rows where company was empty/"N/A"/"JSON ERROR"; incomplete records with valid company but missing location/tech/resp still entered `fresh_set`, main loop skipped them, relying only on retry phase
  - Fix: `get_jd_url_meta` now also reads location/tech/resp; if values are in `_JD_MISSING`, they are skipped and not placed into `fresh_set`, ensuring the main loop rediscovers and processes these URLs
  - Also fixed `retry_one` validation: if extracted location/tech/resp are all empty, old records are not overwritten (prevents infinite retry loop writing "None")
  - 122 tests all passed

## 2026-03-12

### Bug Fixes
- **BUG-05 (P1)**: Fixed Career URL write row misalignment in `run_phase_1_5`
  - Added `get_company_rows_with_row_num()` returning `(excel_row, row_data)` tuples with actual Excel row numbers
  - `update_company_career_url` parameter changed from 0-based list index to actual Excel row number (`excel_row`), eliminating implicit `+2` offset convention
  - `run_phase_1_5` now uses `get_company_rows_with_row_num()`, ensuring Career URL is not written to wrong company row when empty rows exist
  - Added 2 regression tests (`TestBug05CareerUrlRowAlignment`), 162 tests all passed

- **BUG-09 (P1)**: Converted network-dependent tests in `tests/test_company_agent.py` to use mocks
  - `TestValidateCareerUrl`, `TestCheckAtsSlug`, `TestFindAtsUrl` test classes originally made real HTTP requests
  - Fix: Replaced with `unittest.mock.patch` for `requests.get` and `_check_ats_slug`, eliminating network dependency and real API quota consumption
  - Also fixed `test_openai_careers` (OpenAI returning 403 caused false failure); behavior is now deterministic with mocks
  - Also mocked `time.sleep` to eliminate artificial delay in `_find_ats_url`; test execution time reduced from ~6.8s to ~0.34s
  - 164 tests all passed

- **BUG-12 (P2)**: Fixed workbook handle leaks across entire `shared/excel_store.py`
  - Added `try/finally: wb.close()` to all 19 functions, including both paths in `get_or_create_excel`
  - Added `TestBug12WorkbookClose` (18 test cases), 61 related tests all passed

- **BUG-18 (P2)**: Missing `jd_cache/` directory in `.gitignore`
  - Added `jd_cache/` to `.gitignore` to prevent JD cache files from being committed to VCS

- **BUG-19 (P2)**: Missing core dependencies in `CLAUDE.md` Key Libraries table
  - Added `pycountry`, `openpyxl`, `tavily-python` with usage descriptions

- **BUG-21 (P3)**: Duplicate `hashlib` import in `match_agent.py` (cleaned up alongside BUG-07)
- **BUG-25 (P3)**: Dead code variable `already_ats` in `company_agent.py:528-531` cleaned up

- **BUG-04 (P1)**: `process_company()` URL concurrent deduplication (documented)
- **BUG-07**: `match_agent.py` duplicate `hashlib` import (documented)
- **BUG-08**: Cache round-trip test validity fix (documented)
- **BUG-10**: `_GeminiKeyPool` empty key list IndexError (documented)
- **BUG-11**: CLAUDE.md Python version documentation correction (documented)

### Bug Fixes (later session, same day)
- **BUG-24 (P3)**: Misleading module docstring in `tests/test_company_agent.py`
  - Original docstring stated "real HTTP check", "live ATS API probe", but BUG-09 had already mocked all HTTP calls
  - Updated module docstring to accurately describe that all HTTP calls are mocked, and added integration test instructions (`INTEGRATION_TEST=1`) and `@unittest.skipUnless` usage guidance

- **BUG-26 (P3)**: `tests/test_job_agent.py` pycountry mock contained only 1 state
  - Original mock only had California, causing IN/OR/DE/ME/OK tests to "falsely pass" (these state codes were not in the mock at all)
  - Replaced with complete mock of all 50 states + DC; BUG-03 regression tests in `TestIsUsSegment` now genuinely validate the fix logic

- **BUG-20 (P2)**: Added missing CHANGELOG historical records
  - Added CHANGELOG entries for BUG-12/18/19/21/25; documentation now accurately reflects historical fix status
