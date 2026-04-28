"""
Resume Optimizer Agent — the 4th agent in the pipeline.

Responsibilities:
  1. Load all scored matches (score >= 0) from Match_Results
  2. For each match, tailor the resume to the specific JD using Gemini
  3. Re-score the tailored resume using the same fine-evaluation prompt
  4. Save tailored resumes to tailored_resumes/{resume_id}/{url_md5}.md
  5. Write results to Tailored_Match_Results sheet

Run:
  python agents/resume_optimizer.py
"""
import os
import sys
import json
import hashlib
import asyncio
import logging
import math
from dotenv import load_dotenv
from google import genai
from google.genai import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.excel_store import (
    EXCEL_PATH, PROJECT_ROOT, TAILORED_HEADERS, get_or_create_excel,
    get_scored_matches, get_tailored_match_pairs, batch_upsert_tailored_records,
    get_jd_rows_for_match,
)
from shared.gemini_pool import _GeminiKeyPoolBase
from shared.rate_limiter import _RateLimiter
from shared.config import MODEL
from shared.exceptions import GeminiTransientError, GeminiStructuralError
from shared.prompts import (
    FINE_SYSTEM_PROMPT, BATCH_FINE_SYSTEM_PROMPT,
    TAILOR_SYSTEM_PROMPT, BATCH_TAILOR_SYSTEM_PROMPT,
)
from shared.schemas import (
    MatchResult, TailoredResume,
    BatchTailoredItem, BatchTailoredResult,
    BatchMatchItem, BatchMatchResult,
)

# BUG-41: single shared limiter for all Gemini calls
_GEMINI_LIMITER = _RateLimiter(rpm=13)

# ── JD cache ──────────────────────────────────────────────────────────────────
JD_CACHE_DIR = os.path.join(PROJECT_ROOT, "jd_cache")
TAILORED_DIR = os.path.join(PROJECT_ROOT, "tailored_resumes")
PROFILE_DIR  = os.getenv("PROFILE_DIR", os.path.join(PROJECT_ROOT, "profile"))


# BUG-31: use _GeminiKeyPoolBase directly with genai_mod parameter
_GeminiKeyPool = _GeminiKeyPoolBase  # alias for backward compat (tests)

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

_KEY_POOL: "_GeminiKeyPool | None" = None


# ── Pydantic schemas: see shared/schemas.py ───────────────────────────────────
# ── Prompts: see shared/prompts.py ────────────────────────────────────────────

BATCH_TAILOR_SIZE = 2   # JDs per batch for tailor (output is large — full Markdown resume each)
BATCH_RESCORE_SIZE = 5  # pairs per batch for re-score (output is small — scores + short text)


# ── Helpers ──────────────────────────────────────────────────────────────────
def load_resume(folder: str) -> tuple:
    """Returns (resume_text, resume_id). resume_id = filename without extension."""
    if not os.path.exists(folder):
        logging.error(f"Profile folder not found: {folder}")
        return "", ""
    files = [f for f in os.listdir(folder)
             if not f.startswith('.') and f.lower().endswith(('.md', '.txt'))]
    if not files:
        logging.error(f"No .md/.txt files in {folder}")
        return "", ""
    fname = files[0]
    with open(os.path.join(folder, fname), encoding="utf-8") as fh:
        text = fh.read()
    resume_id = os.path.splitext(fname)[0]
    logging.info(f"Loaded resume: {fname}  ({len(text)} chars)")
    return text, resume_id


def _load_jd_markdown(url: str) -> str | None:
    """Load JD Markdown from cache. Prefer _structured.md, fallback to raw .md."""
    md5 = hashlib.md5(url.encode()).hexdigest()
    structured = os.path.join(JD_CACHE_DIR, f"{md5}_structured.md")
    if os.path.exists(structured):
        with open(structured, encoding="utf-8") as f:
            return f.read()
    raw = os.path.join(JD_CACHE_DIR, f"{md5}.md")
    if os.path.exists(raw):
        with open(raw, encoding="utf-8") as f:
            return f.read()
    return None


def _save_tailored_resume(resume_id: str, url: str, content: str) -> str:
    """Save tailored resume to tailored_resumes/{resume_id}/{url_md5}.md. Returns path."""
    subdir = os.path.join(TAILORED_DIR, resume_id)
    os.makedirs(subdir, exist_ok=True)
    md5 = hashlib.md5(url.encode()).hexdigest()
    path = os.path.join(subdir, f"{md5}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ── Gemini calls ─────────────────────────────────────────────────────────────
def tailor_resume(resume_text: str, jd_content: str) -> str | None:
    """Call Gemini to tailor resume for a specific JD. Returns JSON string.

    P0-4: returns None on structural error so the caller can drop the
    record. Transient errors propagate as GeminiTransientError.
    """
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking tailor_resume()")
    cfg = types.GenerateContentConfig(
        system_instruction=TAILOR_SYSTEM_PROMPT,
        temperature=0.3,
        response_mime_type="application/json",
        response_schema=TailoredResume,
    )
    try:
        resp = _KEY_POOL.generate_content(
            model=MODEL,
            contents=(
                "--- ORIGINAL RESUME ---\n"
                f"<scraped_content>\n{resume_text}\n</scraped_content>\n\n"
                "--- TARGET JD ---\n"
                f"<scraped_content>\n{jd_content}\n</scraped_content>\n\n"
                "Tailor the resume for this specific job. Return TailoredResume JSON."
            ),
            config=cfg,
        )
        return resp.text
    except GeminiTransientError:
        raise
    except GeminiStructuralError as e:
        logging.error(f"Tailor resume structural failure (record dropped): {e}")
        return None


def batch_tailor_resume(resume_text: str, jd_contents: list[str]) -> list[dict]:
    """Batch-tailor resume for multiple JDs in one Gemini call. Returns list[dict] aligned to input."""
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking batch_tailor_resume()")
    numbered = "\n\n".join(
        f"[JD {i}]\n{jd}" for i, jd in enumerate(jd_contents)
    )
    cfg = types.GenerateContentConfig(
        system_instruction=BATCH_TAILOR_SYSTEM_PROMPT,
        temperature=0.3,
        response_mime_type="application/json",
        response_schema=BatchTailoredResult,
    )
    try:
        resp = _KEY_POOL.generate_content(
            model=MODEL,
            contents=(
                "--- ORIGINAL RESUME ---\n"
                f"<scraped_content>\n{resume_text}\n</scraped_content>\n\n"
                "--- TARGET JDs ---\n"
                f"<scraped_content>\n{numbered}\n</scraped_content>\n\n"
                "Tailor the resume for each JD. Return BatchTailoredResult JSON."
            ),
            config=cfg,
        )
        result = BatchTailoredResult.model_validate_json(resp.text)
        out = [{} for _ in range(len(jd_contents))]
        for item in result.items:
            if 0 <= item.index < len(jd_contents):
                out[item.index] = {
                    "tailored_resume_markdown": item.tailored_resume_markdown,
                    "optimization_summary": item.optimization_summary,
                }
        return out
    except GeminiTransientError:
        raise
    except GeminiStructuralError as e:
        logging.error(f"Batch tailor structural failure (skipping {len(jd_contents)} jobs): {e}")
        return [{} for _ in range(len(jd_contents))]
    except Exception as e:
        logging.error(f"Batch tailor response parse failed (skipping {len(jd_contents)} jobs): {e}")
        return [{} for _ in range(len(jd_contents))]


def batch_re_score(pairs: list[dict]) -> list[dict]:
    """Batch re-score multiple (tailored_resume, jd_content) pairs. Returns list[dict] aligned to input."""
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking batch_re_score()")
    numbered = "\n\n".join(
        f"[PAIR {i}]\n"
        "--- CANDIDATE PROFILE ---\n"
        f"<scraped_content>\n{p['tailored_resume']}\n</scraped_content>\n\n"
        "--- TARGET JD ---\n"
        f"<scraped_content>\n{p['jd_content']}\n</scraped_content>"
        for i, p in enumerate(pairs)
    )
    cfg = types.GenerateContentConfig(
        system_instruction=BATCH_FINE_SYSTEM_PROMPT,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=BatchMatchResult,
    )
    try:
        resp = _KEY_POOL.generate_content(
            model=MODEL,
            contents=(f"{numbered}\n\nProvide BatchMatchResult JSON with one item per pair."),
            config=cfg,
        )
        result = BatchMatchResult.model_validate_json(resp.text)
        # P0-4: missing items stay as {} (sentinel for "no real score") so the
        # caller can route them to the per-item fallback path. Previous code
        # used {"compatibility_score": 0} which silently wrote a fake 0.
        out: list[dict] = [{} for _ in range(len(pairs))]
        for item in result.items:
            if 0 <= item.index < len(pairs):
                out[item.index] = {
                    "compatibility_score": item.compatibility_score,
                    "key_strengths": item.key_strengths,
                    "critical_gaps": item.critical_gaps,
                    "recommendation_reason": item.recommendation_reason,
                }
        return out
    except GeminiTransientError:
        raise
    except GeminiStructuralError as e:
        logging.error(f"Batch re-score structural failure (skipping {len(pairs)} pairs): {e}")
        return [{} for _ in range(len(pairs))]
    except Exception as e:
        logging.error(f"Batch re-score response parse failed (skipping {len(pairs)} pairs): {e}")
        return [{} for _ in range(len(pairs))]


def re_score(tailored_resume: str, jd_content: str) -> str | None:
    """Re-score the tailored resume using the same fine-evaluation prompt.

    P0-4: returns None on structural error so the caller can drop the
    record. Transient errors propagate as GeminiTransientError.
    """
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking re_score()")
    cfg = types.GenerateContentConfig(
        system_instruction=FINE_SYSTEM_PROMPT,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=MatchResult,
    )
    try:
        resp = _KEY_POOL.generate_content(
            model=MODEL,
            contents=(
                "--- CANDIDATE PROFILE ---\n"
                f"<scraped_content>\n{tailored_resume}\n</scraped_content>\n\n"
                "--- TARGET JD ---\n"
                f"<scraped_content>\n{jd_content}\n</scraped_content>\n\n"
                "Provide MatchResult JSON."
            ),
            config=cfg,
        )
        return resp.text
    except GeminiTransientError:
        raise
    except GeminiStructuralError as e:
        logging.error(f"Re-score structural failure (record dropped): {e}")
        return None


# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    global _KEY_POOL
    gemini_keys = [k for k in [
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
    ] if k]
    if not gemini_keys:
        print("Missing GEMINI_API_KEY in .env")
        return
    _KEY_POOL = _GeminiKeyPoolBase(gemini_keys, genai_mod=genai)
    logging.info(f"[KeyPool] Loaded {len(gemini_keys)} Gemini API key(s).")

    print("\n" + "=" * 60)
    print("RESUME OPTIMIZER AGENT")
    print("=" * 60 + "\n")

    # 1. Load resume
    resume_text, resume_id = load_resume(PROFILE_DIR)
    if not resume_text:
        print(f"No resume found in {PROFILE_DIR}")
        return

    resume_hash = hashlib.md5(resume_text.encode("utf-8")).hexdigest()
    print(f"Resume: {resume_id}  (MD5: {resume_hash[:8]}...)")

    # 2. Load scored matches + existing tailored pairs
    # P0-6: stage="fine" is the default but pass explicitly so the intent is
    # visible: only Stage 2 Gemini fine-eval scores feed score-delta math.
    xlsx_path = get_or_create_excel()
    scored = get_scored_matches(xlsx_path, stage="fine")
    if not scored:
        print("No scored matches found. Run match_agent.py first.")
        return

    existing_tailored = get_tailored_match_pairs(xlsx_path)

    # 3. Build JD lookup for title/company
    jd_rows = get_jd_rows_for_match(xlsx_path)
    jd_meta = {}
    for jd in jd_rows:
        try:
            d = json.loads(jd["jd_json"])
            jd_meta[jd["url"]] = {
                "job_title": d.get("job_title", ""),
                "company": d.get("company", ""),
                "jd_json": jd["jd_json"],
            }
        except Exception:
            pass

    # 4. Filter: skip already-optimized pairs with same resume hash
    to_process = []
    for match in scored:
        key = (match["resume_id"], match["jd_url"])
        if key in existing_tailored:
            if existing_tailored[key]["resume_hash"] == resume_hash:
                continue  # already optimized, resume unchanged
        to_process.append(match)

    print(f"Scored matches (score >= 0): {len(scored)}")
    print(f"Already optimized:          {len(scored) - len(to_process)}")
    print(f"To process:                 {len(to_process)}\n")

    if not to_process:
        print("All matches already optimized. Nothing to do.")
        _print_summary(xlsx_path, resume_id)
        return

    # 5. Prepare job items (pre-load JD content)
    limiter = _GEMINI_LIMITER
    total   = len(to_process)

    job_items = []
    for match in to_process:
        url = match["jd_url"]
        meta = jd_meta.get(url, {})
        jd_content = _load_jd_markdown(url) or meta.get("jd_json", "")
        if not jd_content:
            logging.warning(f"No JD content for {url}, skipping.")
            continue
        job_items.append({
            "match": match,
            "url": url,
            "meta": meta,
            "jd_content": jd_content,
        })

    # ── Phase 1: Batch Tailor ──────────────────────────────────────
    n_tailor_batches = math.ceil(len(job_items) / BATCH_TAILOR_SIZE)
    print(f"[Phase 1] Batch tailoring {len(job_items)} jobs in {n_tailor_batches} batches "
          f"(batch_size={BATCH_TAILOR_SIZE})...\n")

    tailor_results: dict[str, dict] = {}   # url -> {"tailored_md", "opt_summary"}
    tailor_fallback: list[dict] = []

    for b in range(n_tailor_batches):
        batch = job_items[b * BATCH_TAILOR_SIZE : (b + 1) * BATCH_TAILOR_SIZE]
        jd_contents = [item["jd_content"] for item in batch]

        labels = ", ".join(
            f"{item['meta'].get('company', '?')}" for item in batch
        )
        print(f"  [Tailor {b+1}/{n_tailor_batches}] {labels}")

        await limiter.acquire()
        batch_results = await asyncio.to_thread(
            batch_tailor_resume, resume_text, jd_contents
        )

        for item, result in zip(batch, batch_results):
            if result and result.get("tailored_resume_markdown"):
                url = item["url"]
                tailor_results[url] = {
                    "tailored_md": result["tailored_resume_markdown"],
                    "opt_summary": result.get("optimization_summary", ""),
                }
                _save_tailored_resume(resume_id, url, result["tailored_resume_markdown"])
            else:
                tailor_fallback.append(item)

    # ── Phase 1.5: Fallback — retry failed items individually ──────
    if tailor_fallback:
        print(f"\n  [Fallback] Retrying {len(tailor_fallback)} failed tailor items individually...")
        for item in tailor_fallback:
            await limiter.acquire()
            tailor_json = await asyncio.to_thread(
                tailor_resume, resume_text, item["jd_content"]
            )
            # P0-4: None = structural error; drop the record (do not write empty MD).
            if tailor_json is None:
                logging.error(f"  Fallback tailor also failed for {item['url']} (structural)")
                continue
            try:
                data = json.loads(tailor_json)
                md = data.get("tailored_resume_markdown", "")
                if md:
                    url = item["url"]
                    tailor_results[url] = {
                        "tailored_md": md,
                        "opt_summary": data.get("optimization_summary", ""),
                    }
                    _save_tailored_resume(resume_id, url, md)
            except Exception as e:
                logging.error(f"  Fallback tailor parse failed for {item['url']}: {e}")

    print(f"\n[Phase 1 done] {len(tailor_results)}/{len(job_items)} resumes tailored.")

    # ── Phase 2: Batch Re-score ────────────────────────────────────
    rescore_items = [item for item in job_items if item["url"] in tailor_results]
    n_rescore_batches = math.ceil(len(rescore_items) / BATCH_RESCORE_SIZE) if rescore_items else 0
    print(f"\n[Phase 2] Batch re-scoring {len(rescore_items)} items in {n_rescore_batches} batches "
          f"(batch_size={BATCH_RESCORE_SIZE})...\n")

    score_results: dict[str, dict] = {}   # url -> score data dict
    score_fallback: list[dict] = []

    for b in range(n_rescore_batches):
        batch = rescore_items[b * BATCH_RESCORE_SIZE : (b + 1) * BATCH_RESCORE_SIZE]
        pairs = [
            {
                "tailored_resume": tailor_results[item["url"]]["tailored_md"],
                "jd_content": item["jd_content"],
            }
            for item in batch
        ]

        labels = ", ".join(
            f"{item['meta'].get('company', '?')}" for item in batch
        )
        print(f"  [Re-score {b+1}/{n_rescore_batches}] {labels}")

        await limiter.acquire()
        batch_scores = await asyncio.to_thread(batch_re_score, pairs)

        for item, score_data in zip(batch, batch_scores):
            if score_data.get("compatibility_score", 0) > 0:
                score_results[item["url"]] = score_data
            else:
                score_fallback.append(item)

    # ── Phase 2.5: Fallback — retry failed re-scores individually ──
    if score_fallback:
        print(f"\n  [Fallback] Retrying {len(score_fallback)} failed re-score items individually...")
        for item in score_fallback:
            url = item["url"]
            await limiter.acquire()
            score_json = await asyncio.to_thread(
                re_score, tailor_results[url]["tailored_md"], item["jd_content"]
            )
            # P0-4: None = structural error; drop the record (do not write fake score=0).
            if score_json is None:
                logging.error(f"  Fallback re-score failed for {url} (structural); dropping record.")
                continue
            try:
                score_results[url] = json.loads(score_json)
            except Exception as e:
                logging.error(f"  Fallback re-score parse failed for {url}: {e}; dropping record.")

    # ── Phase 3: Assemble results + write Excel ────────────────────
    results: list[dict] = []
    for item in job_items:
        url = item["url"]
        if url not in tailor_results or url not in score_results:
            continue
        meta = item["meta"]
        match = item["match"]
        score_data = score_results[url]
        tailored_score = score_data.get("compatibility_score", 0)
        original_score = match["score"]
        delta = tailored_score - original_score

        job_title = meta.get("job_title", "Unknown")
        company   = meta.get("company", "Unknown")
        print(f"  {company} — {job_title}: {original_score} → {tailored_score} ({delta:+d})")

        md5 = hashlib.md5(url.encode()).hexdigest()
        path = os.path.join(TAILORED_DIR, resume_id, f"{md5}.md")

        results.append({
            "resume_id": resume_id,
            "jd_url": url,
            "job_title": job_title,
            "company": company,
            "original_score": original_score,
            "tailored_score": tailored_score,
            "score_delta": delta,
            "tailored_resume_path": path,
            "optimization_summary": tailor_results[url]["opt_summary"],
            "resume_hash": resume_hash,
        })

    if results:
        batch_upsert_tailored_records(xlsx_path, results)
        logging.info(f"Wrote {len(results)} tailored records.")

    api_calls = n_tailor_batches + n_rescore_batches + len(tailor_fallback) + len(score_fallback)
    old_calls = total * 2
    print(f"\nResume Optimizer complete. {len(results)}/{total} jobs processed.")
    print(f"API calls: {api_calls} (was {old_calls} without batching, saved {old_calls - api_calls})")
    _print_summary(xlsx_path, resume_id)


def _print_summary(xlsx_path: str, resume_id: str):
    """Print tailored match summary sorted by score delta."""
    pairs = get_tailored_match_pairs(xlsx_path)
    if not pairs:
        return

    from openpyxl import load_workbook
    # BUG-50: dynamic column lookup instead of hardcoded indices
    col_rid      = TAILORED_HEADERS.index("Resume ID") + 1
    col_company  = TAILORED_HEADERS.index("Company") + 1
    col_title    = TAILORED_HEADERS.index("Job Title") + 1
    col_original = TAILORED_HEADERS.index("Original Score") + 1
    col_tailored = TAILORED_HEADERS.index("Tailored Score") + 1
    col_delta    = TAILORED_HEADERS.index("Score Delta") + 1
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["Tailored_Match_Results"]
        rows = []
        for r in range(2, ws.max_row + 1):
            rid = ws.cell(r, col_rid).value
            if rid != resume_id:
                continue
            company   = ws.cell(r, col_company).value or ""
            job_title = ws.cell(r, col_title).value or ""
            original  = ws.cell(r, col_original).value or 0
            tailored  = ws.cell(r, col_tailored).value or 0
            delta     = ws.cell(r, col_delta).value or 0
            rows.append((delta, company, job_title, original, tailored))
        rows.sort(reverse=True)
        if not rows:
            return
        print(f"\n{'='*70}")
        print(f"{'Company':<20} {'Job Title':<20} {'Orig':>5} {'Tail':>5} {'Delta':>6}")
        print(f"{'-'*70}")
        total_delta = 0
        for delta, company, title, orig, tail in rows[:20]:
            c = (company[:18] + "..") if len(company) > 20 else company
            t = (title[:18] + "..") if len(title) > 20 else title
            print(f"{c:<20} {t:<20} {orig:>5} {tail:>5} {delta:>+6}")
            total_delta += delta
        if rows:
            avg = total_delta / len(rows)
            print(f"{'-'*70}")
            print(f"Average delta: {avg:+.1f}  ({len(rows)} jobs)")
    finally:
        wb.close()


if __name__ == "__main__":
    asyncio.run(main())
