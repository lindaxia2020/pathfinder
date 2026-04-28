"""
Match Agent — runs after every resume update (most frequent).

Responsibilities:
  1. Load resume text from PROFILE_DIR (default: ./profile/)
  2. Read all AI-TPM JDs from JD_Tracker sheet
  3. Two-stage evaluation:
       Stage 1 — Batch coarse scoring (10 JDs per Gemini call), write results
       Stage 2 — Fine evaluation of top 20% coarse-scored JDs (1 JD per call)
  4. Invalidate stale scores when resume changes (MD5 hash detection)
  5. Write results to Match_Results sheet

Run:
  python agents/match_agent.py

Profile dir can be overridden:
  PROFILE_DIR=/path/to/folder python agents/match_agent.py
"""
import os
import sys
import re
import json
import math
import hashlib
import asyncio
import time
import logging
from dotenv import load_dotenv
from google import genai
from google.genai import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.excel_store import (
    EXCEL_PATH, PROJECT_ROOT, get_or_create_excel,
    get_jd_rows_for_match, get_match_pairs, upsert_match_record,
    batch_upsert_match_records, MATCH_HEADERS,
)
from shared.prompts import COARSE_SYSTEM_PROMPT, FINE_SYSTEM_PROMPT
from shared.schemas import CoarseItem, BatchCoarseResult, MatchResult

# ── JD Markdown cache ──────────────────────────────────────────────────────────
JD_CACHE_DIR = os.path.join(PROJECT_ROOT, "jd_cache")

def _load_jd_markdown(url: str) -> str | None:
    """Load JD Markdown from cache. Prefer _structured.md, fallback to raw .md."""
    md5_hex = hashlib.md5(url.encode()).hexdigest()
    structured = os.path.join(JD_CACHE_DIR, f"{md5_hex}_structured.md")
    if os.path.exists(structured):
        with open(structured, encoding="utf-8") as f:
            return f.read()
    raw = os.path.join(JD_CACHE_DIR, f"{md5_hex}.md")
    if os.path.exists(raw):
        with open(raw, encoding="utf-8") as f:
            return f.read()
    return None

from shared.gemini_pool import _GeminiKeyPoolBase
from shared.rate_limiter import _RateLimiter
from shared.config import MODEL

# BUG-41: single shared limiter across Stage 1 and Stage 2
_GEMINI_LIMITER = _RateLimiter(rpm=13)


# BUG-31: use _GeminiKeyPoolBase directly with genai_mod parameter
_GeminiKeyPool = _GeminiKeyPoolBase  # alias for backward compat (tests)

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

_KEYWORD_THRESHOLD = 4

# ── Keyword pre-filter (AI-specific terms only) ───────────────────────────────
_AI_TECH_TERMS = frozenset({
    "llm", "gpt", "genai", "transformer", "pytorch", "tensorflow",
    "cuda", "gpu", "inference", "training", "mlops", "neural",
    "foundation", "agent", "diffusion", "multimodal", "embedding", "rlhf",
    "finetuning", "huggingface", "langchain", "openai", "anthropic", "gemini",
    "retrieval", "rag", "vector", "tokenizer", "attention",
})


def _quick_keyword_score(resume_text: str, jd_json: str) -> int:
    """Count shared AI-specific terms between resume and JD.
    Returns 999 on parse error (don't filter). Threshold: < _KEYWORD_THRESHOLD → skip Gemini."""
    try:
        d = json.loads(jd_json)
        jd_text = " ".join([
            d.get("job_title", ""),
            " ".join(d.get("requirements", [])),
            " ".join(d.get("additional_qualifications", [])),
            " ".join(d.get("key_responsibilities", [])),
            " ".join(d.get("core_ai_tech_stack", [])),
        ]).lower()
    except Exception:
        return 999
    resume_words = set(re.findall(r'\b\w+\b', resume_text.lower()))
    jd_words     = set(re.findall(r'\b\w+\b', jd_text))
    return len(_AI_TECH_TERMS & resume_words & jd_words)


PROFILE_DIR = os.getenv("PROFILE_DIR", os.path.join(PROJECT_ROOT, "profile"))


_KEY_POOL: "_GeminiKeyPool | None" = None  # initialised in main()


# ── Pydantic schemas: see shared/schemas.py ───────────────────────────────────


# ── Resume loader ─────────────────────────────────────────────────────────────
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


# ── Stage 1: batch coarse scoring ────────────────────────────────────────────
def _format_jd_for_coarse(jd: dict) -> str:
    """Format JD for coarse scoring using Requirements, Additional Qualifications,
    and Responsibilities fields from structured Excel data."""
    try:
        d = json.loads(jd["jd_json"])
        parts = [
            f"Job Title: {d.get('job_title', 'N/A')}",
            f"Company: {d.get('company', 'N/A')}",
            f"Location: {d.get('location', 'N/A')}",
        ]
        if d.get("requirements"):
            parts.append("Requirements:\n" + "\n".join(f"- {r}" for r in d["requirements"]))
        if d.get("additional_qualifications"):
            parts.append("Additional Qualifications:\n" + "\n".join(f"- {q}" for q in d["additional_qualifications"]))
        if d.get("key_responsibilities"):
            parts.append("Responsibilities:\n" + "\n".join(f"- {r}" for r in d["key_responsibilities"]))
        return "\n\n".join(parts)
    except Exception:
        return jd["jd_json"]


def batch_coarse_score(resume_text: str, jds_batch: list[dict]) -> list[int]:
    """Send resume + up to 10 JDs in one Gemini call; return list of int scores."""
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking batch_coarse_score()")
    numbered = "\n\n".join(
        f"[JD {i}]\n{_format_jd_for_coarse(jd)}" for i, jd in enumerate(jds_batch)
    )
    cfg = types.GenerateContentConfig(
        system_instruction=COARSE_SYSTEM_PROMPT,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=BatchCoarseResult,
    )
    try:
        resp = _KEY_POOL.generate_content(
            model=MODEL,
            contents=(
                "--- CANDIDATE PROFILE ---\n"
                f"<scraped_content>\n{resume_text}\n</scraped_content>\n\n"
                "--- JDs TO SCORE ---\n"
                f"<scraped_content>\n{numbered}\n</scraped_content>\n\n"
                "Return BatchCoarseResult JSON with one CoarseItem per JD."
            ),
            config=cfg,
        )
        result = BatchCoarseResult.model_validate_json(resp.text)
        scores = [1] * len(jds_batch)
        for item in result.items:
            if 0 <= item.index < len(jds_batch):
                scores[item.index] = max(1, item.score)
        return scores
    except Exception as e:
        logging.error(f"Batch coarse score failed: {e}")
        return [1] * len(jds_batch)


# ── Stage 2: fine match evaluation ───────────────────────────────────────────
def evaluate_match(resume_text: str, jd_json: str) -> str:
    if _KEY_POOL is None:
        raise RuntimeError("_KEY_POOL not initialized — call main() first or set _KEY_POOL before invoking evaluate_match()")
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
                f"<scraped_content>\n{resume_text}\n</scraped_content>\n\n"
                "--- TARGET JD ---\n"
                f"<scraped_content>\n{jd_json}\n</scraped_content>\n\n"
                "Provide MatchResult JSON."
            ),
            config=cfg,
        )
        return resp.text
    except Exception as e:
        logging.error(f"Fine match eval failed: {e}")
        return json.dumps({
            "compatibility_score": 1,
            "key_strengths": [],
            "critical_gaps": ["Evaluation failed — default minimum score."],
            "recommendation_reason": "Fine evaluation error; assigned minimum score.",
        })


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global _KEY_POOL
    gemini_keys = [k for k in [
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
    ] if k]
    if not gemini_keys:
        print("❌ Missing GEMINI_API_KEY in .env")
        return
    _KEY_POOL = _GeminiKeyPoolBase(gemini_keys, genai_mod=genai)
    logging.info(f"[KeyPool] Loaded {len(gemini_keys)} Gemini API key(s).")

    print("\n" + "="*60)
    print("MATCH AGENT")
    print("="*60 + "\n")

    resume_text, resume_id = load_resume(PROFILE_DIR)
    if not resume_text:
        print(f"⚠️  No resume found in {PROFILE_DIR}\n"
              f"   Create {PROFILE_DIR}/ and add a .md or .txt resume file.")
        return

    resume_hash = hashlib.md5(resume_text.encode("utf-8")).hexdigest()
    print(f"📄 Resume: {resume_id}  (MD5: {resume_hash[:8]}...)")

    xlsx_path = get_or_create_excel()
    jds       = get_jd_rows_for_match(xlsx_path)
    if not jds:
        print("⚠️  No AI-TPM JDs in tracker. Run job_agent.py first.")
        return

    # ── Stage 1: pre-filter + batch coarse scoring ────────────────────────────
    existing_pairs = get_match_pairs(xlsx_path)

    # Detect stale pairs (resume changed since last score)
    stale_keys = {
        k for k, v in existing_pairs.items()
        if k[0] == resume_id and v["hash"] and v["hash"] != resume_hash
    }
    if stale_keys:
        logging.info(f"[Stage1] {len(stale_keys)} stale pair(s) detected — resume changed, will re-score.")

    already_fine   = {k for k, v in existing_pairs.items()
                      if k[0] == resume_id and v["stage"] == "fine" and k not in stale_keys}
    already_coarse = {k for k, v in existing_pairs.items()
                      if k[0] == resume_id and v["stage"] == "coarse" and k not in stale_keys}

    pre_pass, pre_fail = [], []
    for jd in jds:
        key = (resume_id, jd["url"])
        if key in already_fine or key in already_coarse:
            continue
        qs = _quick_keyword_score(resume_text, jd["jd_json"])
        (pre_pass if qs >= _KEYWORD_THRESHOLD else pre_fail).append((jd, qs))

    print(f"📋 JDs in tracker (AI-TPM): {len(jds)}")
    print(f"✅ Already fine-scored:      {len(already_fine)}")
    print(f"~  Already coarse-scored:   {len(already_coarse)}")
    print(f"🚫 Pre-filtered (keyword):   {len(pre_fail)}")
    print(f"🔍 Needs coarse scoring:     {len(pre_pass)}\n")

    # Write pre-filter failures as score=0
    if pre_fail:
        pf_records = [
            (resume_id, jd["url"], json.dumps({
                "compatibility_score": 0,
                "key_strengths": [],
                "critical_gaps": ["Too few shared AI/tech terms with resume."],
                "recommendation_reason": f"Pre-filter: keyword_overlap={qs} < {_KEYWORD_THRESHOLD}.",
            }), resume_hash, "coarse")
            for jd, qs in pre_fail
        ]
        batch_upsert_match_records(xlsx_path, pf_records)
        logging.info(f"[Stage1] Wrote {len(pf_records)} pre-filter records.")

    # Batch coarse scoring in groups of 10
    coarse_scores: dict[str, int] = {}
    if pre_pass:
        limiter    = _GEMINI_LIMITER
        pass_jds   = [jd for jd, _ in pre_pass]
        n_batches  = math.ceil(len(pass_jds) / 10)

        for b in range(n_batches):
            batch = pass_jds[b * 10 : (b + 1) * 10]
            print(f"[Coarse batch {b+1}/{n_batches}] Scoring {len(batch)} JDs...")
            await limiter.acquire()
            scores = await asyncio.to_thread(batch_coarse_score, resume_text, batch)
            for jd, score in zip(batch, scores):
                coarse_scores[jd["url"]] = score
            logging.info(f"[Stage1] Batch {b+1} scores: {scores}")

        coarse_records = [
            (resume_id, jd["url"], json.dumps({
                "compatibility_score": coarse_scores.get(jd["url"], 1),
                "key_strengths": [],
                "critical_gaps": [],
                "recommendation_reason": "Coarse screening score — pending fine evaluation.",
            }), resume_hash, "coarse")
            for jd in pass_jds
        ]
        batch_upsert_match_records(xlsx_path, coarse_records)
        logging.info(f"[Stage1] Wrote {len(coarse_records)} coarse records.")

    # ── Stage 2: fine evaluation of top 20% coarse-scored JDs ─────────────────
    all_pairs = get_match_pairs(xlsx_path)
    scored_for_resume = {
        k: v for k, v in all_pairs.items()
        if k[0] == resume_id and v["score"] > 0
    }

    if not scored_for_resume:
        print("No scored JDs found; skipping fine evaluation.")
        _print_top_results(xlsx_path, resume_id)
        return

    all_scores_sorted = sorted(
        [v["score"] for v in scored_for_resume.values()], reverse=True
    )
    top_n        = max(1, math.ceil(len(all_scores_sorted) * 0.20))
    cutoff_score = all_scores_sorted[top_n - 1]

    to_fine = [
        k for k, v in scored_for_resume.items()
        if v["score"] >= cutoff_score and v["stage"] == "coarse"
    ]

    print(f"\n[Stage2] Total scored JDs:      {len(all_scores_sorted)}")
    print(f"[Stage2] Top-20% cutoff score:  {cutoff_score}  ({top_n} JDs)")
    print(f"[Stage2] Fine evaluations queued: {len(to_fine)}\n")

    if to_fine:
        jd_lookup  = {jd["url"]: jd for jd in jds}
        limiter    = _GEMINI_LIMITER
        sem        = asyncio.Semaphore(3)
        total_fine = len(to_fine)
        fine_records: list[tuple] = []

        async def fine_one(key: tuple, i: int) -> None:
            async with sem:
                url = key[1]
                jd  = jd_lookup.get(url)
                if not jd:
                    return
                print(f"[Fine {i}/{total_fine}] Evaluating: {url}")
                await limiter.acquire()
                jd_content  = _load_jd_markdown(url) or jd["jd_json"]
                result_json = await asyncio.to_thread(
                    evaluate_match, resume_text, jd_content
                )
                try:
                    parsed = json.loads(result_json)
                    score = max(1, parsed.get("compatibility_score", 1))
                    if parsed.get("compatibility_score", 1) < 1:
                        parsed["compatibility_score"] = score
                        result_json = json.dumps(parsed)
                except Exception:
                    score = 1
                fine_records.append((resume_id, url, result_json, resume_hash, "fine"))
                print(f"    Score: {score}/100")

        await asyncio.gather(*[fine_one(k, i) for i, k in enumerate(to_fine, 1)])
        batch_upsert_match_records(xlsx_path, fine_records)
        logging.info(f"[Stage2] Wrote {len(fine_records)} fine records.")

    print(f"\n🎉 Match Agent complete. Results in {xlsx_path}")
    _print_top_results(xlsx_path, resume_id)


def _print_top_results(xlsx_path: str, resume_id: str):
    """Print top-5 matches to console. ★ = fine evaluated, ~ = coarse only."""
    from openpyxl import load_workbook
    # BUG-51: dynamic column lookup instead of hardcoded indices
    col_rid   = MATCH_HEADERS.index("Resume ID") + 1
    col_url   = MATCH_HEADERS.index("JD URL") + 1
    col_score = MATCH_HEADERS.index("Score") + 1
    col_stage = MATCH_HEADERS.index("Stage") + 1
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["Match_Results"]
        rows = []
        for r in range(2, ws.max_row + 1):
            rid = ws.cell(r, col_rid).value
            if rid != resume_id:
                continue
            url   = ws.cell(r, col_url).value or ""
            score = ws.cell(r, col_score).value or 0
            stage = ws.cell(r, col_stage).value or "fine"
            rows.append((score, url, stage))
        rows.sort(reverse=True)
        if not rows:
            return
        print("\n🏆 Top matches:")
        for rank, (score, url, stage) in enumerate(rows[:5], 1):
            indicator = "★" if stage == "fine" else "~"
            print(f"  {rank}. {indicator} [{score}/100] {url}")
        print("  (★ = fine-evaluated  ~ = coarse-only)")
    finally:
        wb.close()


if __name__ == "__main__":
    asyncio.run(main())
