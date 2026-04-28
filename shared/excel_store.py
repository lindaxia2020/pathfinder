"""
Shared Excel persistence layer — used by all three agents.
All functions load → modify → save on each call (safe for single-process use).
job_agent wraps writes in an asyncio.Lock to prevent concurrent corruption.
"""
import os
import json
from datetime import datetime
import openpyxl
from openpyxl import load_workbook

# ── Path ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCEL_PATH   = os.path.join(PROJECT_ROOT, "pathfinder_dashboard.xlsx")

# ── Sheet headers ─────────────────────────────────────────────────────────────
COMPANY_HEADERS          = ["Company Name", "AI Domain", "Business Focus", "Career URL", "Updated At", "TPM Jobs", "AI TPM Jobs", "No TPM Count", "Auto Archived"]
WITHOUT_TPM_HEADERS      = ["Company Name", "AI Domain", "Business Focus", "Career URL", "Updated At", "TPM Jobs", "AI TPM Jobs"]
JD_HEADERS      = ["JD URL", "Job Title", "Company", "Location", "Salary", "Requirements",
                   "Additional Qualifications", "Responsibilities", "Is AI TPM", "Updated At", "MD Hash",
                   "Data Quality"]
MATCH_HEADERS   = ["Resume ID", "JD URL", "Score", "Strengths", "Gaps", "Reason", "Updated At", "Resume Hash", "Stage"]
TAILORED_HEADERS = ["Resume ID", "JD URL", "Job Title", "Company", "Original Score",
                    "Tailored Score", "Score Delta", "Tailored Resume Path",
                    "Optimization Summary", "Updated At", "Resume Hash"]

# BUG-52: pre-computed 1-based column indices for JD_Tracker lookups
_JD_COL = {h: i + 1 for i, h in enumerate(JD_HEADERS)}


# ── Init ──────────────────────────────────────────────────────────────────────
def get_or_create_excel(xlsx_path: str = EXCEL_PATH) -> str:
    if os.path.exists(xlsx_path):
        try:
            wb = load_workbook(xlsx_path)
        except Exception as e:
            import shutil, logging as _log
            bak = xlsx_path + ".bak"
            try:
                shutil.move(xlsx_path, bak)
            except Exception as move_err:
                raise RuntimeError(
                    f"[Excel] Corrupted file at {xlsx_path} and cannot back up to {bak}: {move_err}"
                ) from e
            _log.warning(f"[Excel] Corrupted file moved to {bak}: {e}. Recreating.")
            return get_or_create_excel(xlsx_path)
        try:
            changed = False
            # Migrate: rename old "AI_" prefixed sheet tabs to new names
            _SHEET_RENAMES = [("AI_Company_List", "Company_List"),
                              ("AI_Company_Without_TPM", "Company_Without_TPM")]
            for old_name, new_name in _SHEET_RENAMES:
                if old_name in wb.sheetnames and new_name not in wb.sheetnames:
                    wb[old_name].title = new_name
                    changed = True
                    import logging as _log
                    _log.info(f"[Excel] Migrated sheet: '{old_name}' → '{new_name}'.")
            for name, headers in [("Company_List",         COMPANY_HEADERS),
                                   ("Company_Without_TPM",  WITHOUT_TPM_HEADERS),
                                   ("JD_Tracker",              JD_HEADERS),
                                   ("Match_Results",           MATCH_HEADERS),
                                   ("Tailored_Match_Results",  TAILORED_HEADERS)]:
                if name not in wb.sheetnames:
                    wb.create_sheet(name).append(headers)
                    changed = True
            if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
                del wb["Sheet"]
                changed = True
            # Migrate JD_Tracker: insert "Job Title" as col 2 if missing
            if "JD_Tracker" in wb.sheetnames:
                import logging as _log
                ws_jd = wb["JD_Tracker"]
                header_row = [ws_jd.cell(1, c).value for c in range(1, ws_jd.max_column + 1)]
                if "Job Title" not in header_row:
                    ws_jd.insert_cols(2)
                    ws_jd.cell(1, 2).value = "Job Title"
                    header_row.insert(1, "Job Title")
                    changed = True
                    _log.info("[Excel] Migrated JD_Tracker: inserted 'Job Title' column.")
                # Rename "Tech Stack" → "Requirements"
                if "Tech Stack" in header_row and "Requirements" not in header_row:
                    col_idx = header_row.index("Tech Stack") + 1
                    ws_jd.cell(1, col_idx).value = "Requirements"
                    header_row[col_idx - 1] = "Requirements"
                    changed = True
                    _log.info("[Excel] Migrated JD_Tracker: renamed 'Tech Stack' to 'Requirements'.")
                # Insert "Additional Qualifications" after "Requirements" if missing
                if "Additional Qualifications" not in header_row and "Requirements" in header_row:
                    req_col = header_row.index("Requirements") + 1
                    ws_jd.insert_cols(req_col + 1)
                    ws_jd.cell(1, req_col + 1).value = "Additional Qualifications"
                    changed = True
                    _log.info("[Excel] Migrated JD_Tracker: inserted 'Additional Qualifications' column.")
                # REQ-060: Add "Data Quality" column if missing
                # Re-read headers after possible prior migrations
                header_row = [ws_jd.cell(1, c).value for c in range(1, ws_jd.max_column + 1)]
                if "Data Quality" not in header_row:
                    next_col = ws_jd.max_column + 1
                    ws_jd.cell(1, next_col).value = "Data Quality"
                    changed = True
                    _log.info("[Excel] Migrated JD_Tracker: added 'Data Quality' column.")
            # Migrate Company_List: add "TPM Jobs" and "AI TPM Jobs" columns if missing
            if "Company_List" in wb.sheetnames:
                ws_co = wb["Company_List"]
                co_headers = [ws_co.cell(1, c).value for c in range(1, ws_co.max_column + 1)]
                if "TPM Jobs" not in co_headers:
                    next_col = ws_co.max_column + 1
                    ws_co.cell(1, next_col).value = "TPM Jobs"
                    ws_co.cell(1, next_col + 1).value = "AI TPM Jobs"
                    changed = True
                    import logging as _log
                    _log.info("[Excel] Migrated Company_List: added 'TPM Jobs' and 'AI TPM Jobs' columns.")
                # REQ-063: add "No TPM Count" and "Auto Archived" columns if missing
                co_headers = [ws_co.cell(1, c).value for c in range(1, ws_co.max_column + 1)]
                if "No TPM Count" not in co_headers:
                    next_col = ws_co.max_column + 1
                    ws_co.cell(1, next_col).value = "No TPM Count"
                    ws_co.cell(1, next_col + 1).value = "Auto Archived"
                    changed = True
                    import logging as _log
                    _log.info("[Excel] Migrated Company_List: added 'No TPM Count' and 'Auto Archived' columns.")
            # BUG-55: Migrate Company_Without_TPM: add "TPM Jobs" and "AI TPM Jobs" if missing
            if "Company_Without_TPM" in wb.sheetnames:
                ws_wt = wb["Company_Without_TPM"]
                wt_headers = [ws_wt.cell(1, c).value for c in range(1, ws_wt.max_column + 1)]
                if "TPM Jobs" not in wt_headers:
                    next_col = ws_wt.max_column + 1
                    ws_wt.cell(1, next_col).value = "TPM Jobs"
                    ws_wt.cell(1, next_col + 1).value = "AI TPM Jobs"
                    changed = True
                    import logging as _log
                    _log.info("[Excel] Migrated Company_Without_TPM: added 'TPM Jobs' and 'AI TPM Jobs' columns.")
            # Migrate Match_Results: add "Resume Hash" (col 8) and "Stage" (col 9) if missing
            if "Match_Results" in wb.sheetnames:
                ws_mr = wb["Match_Results"]
                mr_headers = [ws_mr.cell(1, c).value for c in range(1, ws_mr.max_column + 1)]
                if "Resume Hash" not in mr_headers:
                    next_col = ws_mr.max_column + 1
                    ws_mr.cell(1, next_col).value = "Resume Hash"
                    ws_mr.cell(1, next_col + 1).value = "Stage"
                    for r in range(2, ws_mr.max_row + 1):
                        if ws_mr.cell(r, 1).value:
                            ws_mr.cell(r, next_col).value = ""
                            ws_mr.cell(r, next_col + 1).value = "fine"
                    changed = True
                    import logging as _log
                    _log.info("[Excel] Migrated Match_Results: added 'Resume Hash' and 'Stage' columns.")
            if changed:
                wb.save(xlsx_path)
        finally:
            wb.close()
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Company_List"
        ws.append(COMPANY_HEADERS)
        wb.create_sheet("Company_Without_TPM").append(WITHOUT_TPM_HEADERS)
        wb.create_sheet("JD_Tracker").append(JD_HEADERS)
        wb.create_sheet("Match_Results").append(MATCH_HEADERS)
        wb.create_sheet("Tailored_Match_Results").append(TAILORED_HEADERS)
        wb.save(xlsx_path)
        wb.close()
    return xlsx_path


# ── Company helpers ───────────────────────────────────────────────────────────
def count_company_rows(xlsx_path: str = EXCEL_PATH) -> int:
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["Company_List"]
        return sum(1 for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value)
    finally:
        wb.close()


def get_company_rows(xlsx_path: str = EXCEL_PATH) -> list:
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws   = wb["Company_List"]
        rows = []
        for r in range(2, ws.max_row + 1):
            row = [ws.cell(r, c).value or "" for c in range(1, ws.max_column + 1)]
            if any(row):
                rows.append(row)
        return rows
    finally:
        wb.close()


def get_company_rows_with_row_num(xlsx_path: str = EXCEL_PATH) -> list[tuple[int, list]]:
    """Return [(excel_row_number, row_data), ...] skipping empty rows.
    excel_row_number is the actual 1-based row index in the sheet (2 = first data row).
    Use this when you need to write back to a specific row via update_company_career_url.
    """
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws   = wb["Company_List"]
        rows = []
        for r in range(2, ws.max_row + 1):
            row = [ws.cell(r, c).value or "" for c in range(1, ws.max_column + 1)]
            if any(row):
                rows.append((r, row))
        return rows
    finally:
        wb.close()


def upsert_companies(xlsx_path: str, companies_data: list):
    """Upsert companies into Company_List.

    For NEW companies: write all 9 columns with TPM counts initialized to
    [0, 0, 0, "No"].
    For EXISTING companies: only update cols 1–5 (Name, AIDomain, Focus,
    Career URL, Updated At). Cols 6–9 (TPM Jobs, AI TPM Jobs, No TPM Count,
    Auto Archived) are preserved — they are managed exclusively by
    update_company_job_counts and the auto-archival pipeline.
    """
    wb = load_workbook(xlsx_path)
    try:
        ws  = wb["Company_List"]
        idx  = {ws.cell(r, 1).value: r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value}
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for c in companies_data:
            name = c.get("company_name", "N/A")
            cols_1_to_5 = [name, c.get("ai_domain", "N/A"), c.get("business_focus", "N/A"),
                           c.get("career_url", "N/A"), now]
            if name in idx:
                for col, val in enumerate(cols_1_to_5, 1):
                    ws.cell(idx[name], col, val)
            else:
                ws.append(cols_1_to_5 + [0, 0, 0, "No"])
                idx[name] = ws.max_row
        wb.save(xlsx_path)
    finally:
        wb.close()


def get_company_names_without_tpm(xlsx_path: str = EXCEL_PATH) -> set:
    """Return a set of company names from the Company_Without_TPM sheet."""
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        if "Company_Without_TPM" not in wb.sheetnames:
            return set()
        ws = wb["Company_Without_TPM"]
        names = set()
        for r in range(2, ws.max_row + 1):
            val = ws.cell(r, 1).value
            if val:
                names.add(str(val).strip())
        return names
    finally:
        wb.close()


def update_company_career_url(xlsx_path: str, excel_row: int, new_url: str):
    """excel_row is the actual 1-based Excel row number (2 = first data row after header)."""
    wb = load_workbook(xlsx_path)
    try:
        ws = wb["Company_List"]
        ws.cell(excel_row, 4, new_url)
        wb.save(xlsx_path)
    finally:
        wb.close()


# ── Archive helpers (REQ-063) ────────────────────────────────────────────────
def get_archived_companies(xlsx_path: str = EXCEL_PATH) -> set:
    """Return set of company names where Auto Archived == 'yes'."""
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["Company_List"]
        headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        arch_col = headers.get("Auto Archived")
        if not arch_col:
            return set()
        names = set()
        for r in range(2, ws.max_row + 1):
            name = ws.cell(r, 1).value
            archived = str(ws.cell(r, arch_col).value or "").strip().lower()
            if name and archived == "yes":
                names.add(str(name).strip())
        return names
    finally:
        wb.close()


def update_archive_status(xlsx_path: str, company_name: str,
                          no_tpm_count: int, archived: str) -> None:
    """Update No TPM Count and Auto Archived columns for a company."""
    wb = load_workbook(xlsx_path)
    try:
        ws = wb["Company_List"]
        headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        cnt_col  = headers.get("No TPM Count")
        arch_col = headers.get("Auto Archived")
        if not cnt_col or not arch_col:
            return
        for r in range(2, ws.max_row + 1):
            name = str(ws.cell(r, 1).value or "").strip()
            if name == company_name:
                ws.cell(r, cnt_col).value  = no_tpm_count
                ws.cell(r, arch_col).value = archived
                break
        wb.save(xlsx_path)
    finally:
        wb.close()


def unarchive_company(xlsx_path: str, company_name: str) -> None:
    """Manually restore an archived company: reset counter and archived flag."""
    update_archive_status(xlsx_path, company_name, 0, "no")


def get_company_archive_info(xlsx_path: str = EXCEL_PATH) -> dict:
    """Return {company_name: {"no_tpm_count": int, "archived": str}} for all companies."""
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["Company_List"]
        headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        cnt_col  = headers.get("No TPM Count")
        arch_col = headers.get("Auto Archived")
        result = {}
        if not cnt_col or not arch_col:
            return result
        for r in range(2, ws.max_row + 1):
            name = str(ws.cell(r, 1).value or "").strip()
            if not name:
                continue
            raw_cnt = ws.cell(r, cnt_col).value
            cnt = int(raw_cnt) if isinstance(raw_cnt, (int, float)) and raw_cnt else 0
            arch = str(ws.cell(r, arch_col).value or "").strip().lower()
            result[name] = {"no_tpm_count": cnt, "archived": arch}
        return result
    finally:
        wb.close()


# ── JD helpers ────────────────────────────────────────────────────────────────
def get_jd_urls(xlsx_path: str = EXCEL_PATH) -> list:
    """Return URLs that have been successfully extracted (company != N/A/empty).
    Incomplete records (failed Gemini extraction) are excluded so they get retried."""
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["JD_Tracker"]
        c_url = _JD_COL["JD URL"]
        c_company = _JD_COL["Company"]
        result = []
        for r in range(2, ws.max_row + 1):
            url     = ws.cell(r, c_url).value
            company = str(ws.cell(r, c_company).value or "").strip()
            if url and company not in ("", "N/A", "JSON ERROR"):
                result.append(url)
        return result
    finally:
        wb.close()


def get_jd_url_meta(xlsx_path: str = EXCEL_PATH) -> dict:
    """Returns {url: {"hash": str, "age_days": float, "title": str}} for valid, complete JD rows.
    Skips rows where company is N/A, empty, or JSON ERROR, OR where location/tech/resp are
    missing/None/N/A (incomplete records). Incomplete records must be re-processed."""
    wb  = load_workbook(xlsx_path, read_only=True)
    try:
        ws  = wb["JD_Tracker"]
        c_url  = _JD_COL["JD URL"]
        c_title = _JD_COL["Job Title"]
        c_company = _JD_COL["Company"]
        c_location = _JD_COL["Location"]
        c_req  = _JD_COL["Requirements"]
        c_resp = _JD_COL["Responsibilities"]
        c_updated = _JD_COL["Updated At"]
        c_hash = _JD_COL["MD Hash"]
        now = datetime.now()
        result = {}
        for r in range(2, ws.max_row + 1):
            url      = ws.cell(r, c_url).value
            title    = str(ws.cell(r, c_title).value or "").strip()
            company  = str(ws.cell(r, c_company).value or "").strip()
            location = str(ws.cell(r, c_location).value or "").strip()
            req      = str(ws.cell(r, c_req).value or "").strip()
            resp     = str(ws.cell(r, c_resp).value or "").strip()
            updated  = ws.cell(r, c_updated).value
            md_hash  = str(ws.cell(r, c_hash).value or "").strip()
            if not url or company in ("", "N/A", "JSON ERROR"):
                continue
            # Also skip records with missing key fields — treat as incomplete so main loop retries them
            if (location.lower() in _JD_MISSING
                    or req.lower() in _JD_MISSING
                    or resp.lower() in _JD_MISSING):
                continue
            if isinstance(updated, datetime):
                age_days = (now - updated).total_seconds() / 86400
            elif isinstance(updated, str) and updated:
                try:
                    age_days = (now - datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
                except Exception:
                    age_days = 999.0
            else:
                age_days = 999.0
            result[url] = {"hash": md_hash, "age_days": age_days, "title": title}
        return result
    finally:
        wb.close()


def batch_update_jd_timestamps(xlsx_path: str, urls: list) -> int:
    """Update only Updated At column for given URLs. Returns count updated."""
    if not urls:
        return 0
    url_set = set(urls)
    wb  = load_workbook(xlsx_path)
    try:
        ws  = wb["JD_Tracker"]
        c_url = _JD_COL["JD URL"]
        c_updated = _JD_COL["Updated At"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count = 0
        for r in range(2, ws.max_row + 1):
            url = ws.cell(r, c_url).value
            if url in url_set:
                ws.cell(r, c_updated).value = now
                count += 1
        wb.save(xlsx_path)
        return count
    finally:
        wb.close()


def get_jd_rows_for_match(xlsx_path: str = EXCEL_PATH) -> list:
    """
    Returns list of dicts for every JD where is_ai_tpm == 'True'.
    Reconstructs a match-ready JSON string from individual columns.
    """
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws   = wb["JD_Tracker"]
        c_url   = _JD_COL["JD URL"]
        c_title = _JD_COL["Job Title"]
        c_company = _JD_COL["Company"]
        c_loc   = _JD_COL["Location"]
        c_sal   = _JD_COL["Salary"]
        c_req   = _JD_COL["Requirements"]
        c_addq  = _JD_COL["Additional Qualifications"]
        c_resp  = _JD_COL["Responsibilities"]
        c_tpm   = _JD_COL["Is AI TPM"]
        rows = []
        for r in range(2, ws.max_row + 1):
            url        = ws.cell(r, c_url).value
            job_title  = ws.cell(r, c_title).value or ""
            company    = ws.cell(r, c_company).value or ""
            location   = ws.cell(r, c_loc).value or ""
            salary     = ws.cell(r, c_sal).value or ""
            req_raw    = ws.cell(r, c_req).value or ""
            addq_raw   = ws.cell(r, c_addq).value or ""
            resp_raw   = ws.cell(r, c_resp).value or ""
            is_tpm     = str(ws.cell(r, c_tpm).value or "").strip()
            if not url or is_tpm != "True":
                continue
            req_list  = [s.lstrip("• ").strip() for s in req_raw.split("\n") if s.strip()]
            addq_list = [s.lstrip("• ").strip() for s in addq_raw.split("\n") if s.strip()]
            resp_list = [s.lstrip("• ").strip() for s in resp_raw.split("\n") if s.strip()]
            jd_json   = json.dumps({
                "job_title": job_title, "company": company, "location": location,
                "salary_range": salary, "requirements": req_list,
                "additional_qualifications": addq_list,
                "key_responsibilities": resp_list, "is_ai_tpm": True,
            })
            rows.append({"url": url, "jd_json": jd_json})
        return rows
    finally:
        wb.close()


def upsert_jd_record(xlsx_path: str, jd_url: str, jd_json: str, markdown_hash: str):
    wb = load_workbook(xlsx_path)
    try:
        ws   = wb["JD_Tracker"]
        idx  = {ws.cell(r, 1).value: r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value}
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            d     = json.loads(jd_json)
            req   = "\n".join(f"• {x}" for x in (d.get("requirements") or [])) or "None"
            addq  = "\n".join(f"• {x}" for x in (d.get("additional_qualifications") or [])) or "None"
            resp  = "\n".join(f"• {x}" for x in (d.get("key_responsibilities") or [])) or "None"
            row_data = [jd_url, d.get("job_title","N/A"), d.get("company","N/A"),
                        d.get("location","N/A"), d.get("salary_range","N/A"),
                        req, addq, resp,
                        str(d.get("is_ai_tpm", False)), now, markdown_hash,
                        d.get("data_quality", "")]
        except json.JSONDecodeError:
            row_data = [jd_url, "JSON ERROR", "JSON ERROR", "JSON ERROR", "JSON ERROR",
                        "JSON ERROR", "JSON ERROR", "JSON ERROR", "JSON ERROR", now, markdown_hash,
                        "failed"]
        if jd_url in idx:
            for col, val in enumerate(row_data, 1):
                ws.cell(idx[jd_url], col, val)
        else:
            ws.append(row_data)
        wb.save(xlsx_path)
    finally:
        wb.close()


def batch_upsert_jd_records(xlsx_path: str, records: list) -> int:
    """
    Write multiple JD records in a single load→modify→save cycle.
    records: list of (jd_url, jd_json, markdown_hash) tuples.
    Returns the number of records written.
    """
    if not records:
        return 0
    wb  = load_workbook(xlsx_path)
    try:
        ws  = wb["JD_Tracker"]
        idx = {ws.cell(r, 1).value: r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value}
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for jd_url, jd_json, markdown_hash in records:
            try:
                d     = json.loads(jd_json)
                req   = "\n".join(f"• {x}" for x in d.get("requirements", [])) or "None"
                addq  = "\n".join(f"• {x}" for x in d.get("additional_qualifications", [])) or "None"
                resp  = "\n".join(f"• {x}" for x in d.get("key_responsibilities", [])) or "None"
                row_data = [jd_url, d.get("job_title","N/A"), d.get("company","N/A"),
                            d.get("location","N/A"), d.get("salary_range","N/A"),
                            req, addq, resp,
                            str(d.get("is_ai_tpm", False)), now, markdown_hash,
                            d.get("data_quality", "")]
            except json.JSONDecodeError:
                row_data = [jd_url, "JSON ERROR", "JSON ERROR", "JSON ERROR", "JSON ERROR",
                            "JSON ERROR", "JSON ERROR", "JSON ERROR", "JSON ERROR", now, markdown_hash,
                            "failed"]
            if jd_url in idx:
                for col, val in enumerate(row_data, 1):
                    ws.cell(idx[jd_url], col, val)
            else:
                ws.append(row_data)
                idx[jd_url] = ws.max_row
        wb.save(xlsx_path)
        return len(records)
    finally:
        wb.close()


# ── JD completeness helpers ───────────────────────────────────────────────────
_JD_MISSING = {"", "n/a", "none", "json error", "not specified", "not available"}

def get_incomplete_jd_rows(xlsx_path: str = EXCEL_PATH) -> list:
    """
    Return list of dicts for JD records that are missing key fields
    (location, tech_stack, or responsibilities are empty/N/A/None/JSON ERROR).
    These records should be retried regardless of whether their URL is known.
    """
    wb  = load_workbook(xlsx_path, read_only=True)
    try:
        ws  = wb["JD_Tracker"]
        c_url  = _JD_COL["JD URL"]
        c_title = _JD_COL["Job Title"]
        c_company = _JD_COL["Company"]
        c_loc  = _JD_COL["Location"]
        c_req  = _JD_COL["Requirements"]
        c_resp = _JD_COL["Responsibilities"]
        out = []
        for r in range(2, ws.max_row + 1):
            url      = ws.cell(r, c_url).value
            if not url:
                continue
            title    = str(ws.cell(r, c_title).value or "").strip()
            company  = str(ws.cell(r, c_company).value or "").strip()
            location = str(ws.cell(r, c_loc).value or "").strip()
            req      = str(ws.cell(r, c_req).value or "").strip()
            resp     = str(ws.cell(r, c_resp).value or "").strip()
            if (location.lower() in _JD_MISSING
                    or req.lower() in _JD_MISSING
                    or resp.lower() in _JD_MISSING):
                out.append({"url": url, "title": title, "company": company})
        return out
    finally:
        wb.close()


def count_tpm_jobs_by_company(xlsx_path: str = EXCEL_PATH) -> dict:
    """
    Return {company_name: {"tpm": int, "ai_tpm": int}} counting all valid JD rows.
    """
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws     = wb["JD_Tracker"]
        c_url = _JD_COL["JD URL"]
        c_company = _JD_COL["Company"]
        c_tpm = _JD_COL["Is AI TPM"]
        counts = {}
        for r in range(2, ws.max_row + 1):
            url     = ws.cell(r, c_url).value
            company = str(ws.cell(r, c_company).value or "").strip()
            is_ai   = str(ws.cell(r, c_tpm).value or "").strip()
            if not url or company.lower() in ("", "n/a", "json error"):
                continue
            if company not in counts:
                counts[company] = {"tpm": 0, "ai_tpm": 0}
            counts[company]["tpm"] += 1
            if is_ai == "True":
                counts[company]["ai_tpm"] += 1
        return counts
    finally:
        wb.close()


def count_valid_tpm_jobs_by_company(xlsx_path: str = EXCEL_PATH) -> dict:
    """Return {company_name: int} counting JD rows where data_quality != 'failed'.

    Used by REQ-063 archive logic: only non-failed records count toward
    determining whether a company has TPM jobs.
    """
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["JD_Tracker"]
        c_url = _JD_COL["JD URL"]
        c_company = _JD_COL["Company"]
        c_dq = _JD_COL["Data Quality"]
        counts = {}
        for r in range(2, ws.max_row + 1):
            url     = ws.cell(r, c_url).value
            company = str(ws.cell(r, c_company).value or "").strip()
            if not url or company.lower() in ("", "n/a", "json error"):
                continue
            if c_dq:
                dq = str(ws.cell(r, c_dq).value or "").strip().lower()
                if dq == "failed":
                    continue
            counts[company] = counts.get(company, 0) + 1
        return counts
    finally:
        wb.close()


def update_company_job_counts(xlsx_path: str, counts: dict) -> None:
    """
    Write TPM Jobs / AI TPM Jobs columns in Company_List.
    counts: {company_name: {"tpm": int, "ai_tpm": int}}
    """
    wb = load_workbook(xlsx_path)
    try:
        ws_co   = wb["Company_List"]
        headers = {ws_co.cell(1, c).value: c for c in range(1, ws_co.max_column + 1)}
        tpm_col    = headers.get("TPM Jobs")
        ai_tpm_col = headers.get("AI TPM Jobs")
        if not tpm_col or not ai_tpm_col:
            return
        for r in range(2, ws_co.max_row + 1):
            name = str(ws_co.cell(r, 1).value or "").strip()
            if name in counts:
                ws_co.cell(r, tpm_col).value    = counts[name]["tpm"]
                ws_co.cell(r, ai_tpm_col).value = counts[name]["ai_tpm"]
        wb.save(xlsx_path)
    finally:
        wb.close()


# ── Match helpers ─────────────────────────────────────────────────────────────
def get_match_pairs(xlsx_path: str = EXCEL_PATH) -> dict:
    """Returns {(resume_id, jd_url): {"score": int, "hash": str, "stage": str}}."""
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws    = wb["Match_Results"]
        pairs = {}
        for r in range(2, ws.max_row + 1):
            rid = ws.cell(r, 1).value
            url = ws.cell(r, 2).value
            if rid and url:
                score  = ws.cell(r, 3).value or 0
                rhash  = (ws.cell(r, 8).value or "") if ws.max_column >= 8 else ""
                stage  = (ws.cell(r, 9).value or "fine") if ws.max_column >= 9 else "fine"
                pairs[(str(rid), str(url))] = {
                    "score": int(score) if isinstance(score, (int, float)) else 0,
                    "hash":  str(rhash),
                    "stage": str(stage),
                }
        return pairs
    finally:
        wb.close()


def upsert_match_record(xlsx_path: str, resume_id: str, jd_url: str, match_json: str,
                        resume_hash: str = "", stage: str = "fine"):
    wb  = load_workbook(xlsx_path)
    try:
        ws  = wb["Match_Results"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            d         = json.loads(match_json)
            strengths = "\n".join(f"• {x}" for x in d.get("key_strengths", [])) or "None"
            gaps      = "\n".join(f"• {x}" for x in d.get("critical_gaps", [])) or "None"
            row_data  = [resume_id, jd_url, d.get("compatibility_score", 0),
                         strengths, gaps, d.get("recommendation_reason","N/A"), now,
                         resume_hash, stage]
        except json.JSONDecodeError:
            row_data = [resume_id, jd_url, 0, "JSON ERROR", "JSON ERROR", "JSON ERROR", now,
                        resume_hash, stage]
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, 1).value == resume_id and ws.cell(r, 2).value == jd_url:
                for col, val in enumerate(row_data, 1):
                    ws.cell(r, col, val)
                wb.save(xlsx_path)
                return
        ws.append(row_data)
        wb.save(xlsx_path)
    finally:
        wb.close()


def batch_upsert_match_records(xlsx_path: str, records: list) -> int:
    """
    Write multiple match records in a single load→modify→save cycle.
    records: list of (resume_id, jd_url, match_json, resume_hash, stage) tuples.
    Returns the number of records written.
    """
    if not records:
        return 0
    wb  = load_workbook(xlsx_path)
    try:
        ws  = wb["Match_Results"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        idx = {}
        for r in range(2, ws.max_row + 1):
            rid = ws.cell(r, 1).value
            url = ws.cell(r, 2).value
            if rid and url:
                idx[(str(rid), str(url))] = r
        for resume_id, jd_url, match_json, resume_hash, stage in records:
            try:
                d         = json.loads(match_json)
                strengths = "\n".join(f"• {x}" for x in d.get("key_strengths", [])) or "None"
                gaps      = "\n".join(f"• {x}" for x in d.get("critical_gaps", [])) or "None"
                row_data  = [resume_id, jd_url, d.get("compatibility_score", 0),
                             strengths, gaps, d.get("recommendation_reason","N/A"), now,
                             resume_hash, stage]
            except json.JSONDecodeError:
                row_data = [resume_id, jd_url, 0, "JSON ERROR", "JSON ERROR", "JSON ERROR", now,
                            resume_hash, stage]
            key = (str(resume_id), str(jd_url))
            if key in idx:
                for col, val in enumerate(row_data, 1):
                    ws.cell(idx[key], col, val)
            else:
                ws.append(row_data)
                idx[key] = ws.max_row
        wb.save(xlsx_path)
        return len(records)
    finally:
        wb.close()


# ── Tailored match helpers ───────────────────────────────────────────────────
def get_scored_matches(xlsx_path: str = EXCEL_PATH) -> list:
    """Return all match records with score >= 0.
    Returns list of dicts: {resume_id, jd_url, score, stage, resume_hash}."""
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb["Match_Results"]
        results = []
        for r in range(2, ws.max_row + 1):
            rid   = ws.cell(r, 1).value
            url   = ws.cell(r, 2).value
            score = ws.cell(r, 3).value or 0
            rhash = (ws.cell(r, 8).value or "") if ws.max_column >= 8 else ""
            stage = (ws.cell(r, 9).value or "fine") if ws.max_column >= 9 else "fine"
            if rid and url and isinstance(score, (int, float)) and int(score) >= 0:
                results.append({
                    "resume_id": str(rid),
                    "jd_url": str(url),
                    "score": int(score),
                    "stage": str(stage),
                    "resume_hash": str(rhash),
                })
        return results
    finally:
        wb.close()


def get_tailored_match_pairs(xlsx_path: str = EXCEL_PATH) -> dict:
    """Returns {(resume_id, jd_url): {"tailored_score": int, "resume_hash": str}}."""
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        if "Tailored_Match_Results" not in wb.sheetnames:
            return {}
        ws    = wb["Tailored_Match_Results"]
        pairs = {}
        for r in range(2, ws.max_row + 1):
            rid = ws.cell(r, 1).value
            url = ws.cell(r, 2).value
            if rid and url:
                t_score = ws.cell(r, 6).value or 0
                rhash   = ws.cell(r, 11).value or ""
                pairs[(str(rid), str(url))] = {
                    "tailored_score": int(t_score) if isinstance(t_score, (int, float)) else 0,
                    "resume_hash": str(rhash),
                }
        return pairs
    finally:
        wb.close()


def batch_upsert_tailored_records(xlsx_path: str, records: list) -> int:
    """
    Write multiple tailored match records in a single load→modify→save cycle.
    records: list of dicts with keys:
        resume_id, jd_url, job_title, company, original_score,
        tailored_score, score_delta, tailored_resume_path,
        optimization_summary, resume_hash
    Returns the number of records written.
    """
    if not records:
        return 0
    wb = load_workbook(xlsx_path)
    try:
        ws  = wb["Tailored_Match_Results"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        idx = {}
        for r in range(2, ws.max_row + 1):
            rid = ws.cell(r, 1).value
            url = ws.cell(r, 2).value
            if rid and url:
                idx[(str(rid), str(url))] = r
        for rec in records:
            row_data = [
                rec["resume_id"], rec["jd_url"], rec.get("job_title", ""),
                rec.get("company", ""), rec.get("original_score", 0),
                rec.get("tailored_score", 0), rec.get("score_delta", 0),
                rec.get("tailored_resume_path", ""),
                rec.get("optimization_summary", ""), now,
                rec.get("resume_hash", ""),
            ]
            key = (str(rec["resume_id"]), str(rec["jd_url"]))
            if key in idx:
                for col, val in enumerate(row_data, 1):
                    ws.cell(idx[key], col, val)
            else:
                ws.append(row_data)
                idx[key] = ws.max_row
        wb.save(xlsx_path)
        return len(records)
    finally:
        wb.close()
