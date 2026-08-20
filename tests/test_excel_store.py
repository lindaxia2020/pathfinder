"""
Tests for shared/excel_store.py

Coverage:
  - get_or_create_excel: creates file with correct sheets/headers
  - count_company_rows / get_company_rows: reads rows
  - upsert_companies: insert + update
  - get_company_names_without_tpm: reads second sheet
  - update_company_career_url: updates URL column
  - get_jd_urls / get_jd_url_meta: JD read helpers
  - batch_update_jd_timestamps: timestamp update
  - get_jd_rows_for_match: filter by is_ai_tpm
  - upsert_jd_record / batch_upsert_jd_records: JD write
  - get_incomplete_jd_rows: detects missing fields
  - count_tpm_jobs_by_company: job count aggregation
  - update_company_job_counts: write job counts
  - get_match_pairs / upsert_match_record / batch_upsert_match_records
"""
import os
import sys
import json
import tempfile
import unittest
import unittest.mock
from datetime import datetime, date, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from shared.excel_store import (
    get_or_create_excel, count_company_rows, get_company_rows,
    get_company_rows_with_row_num,
    upsert_companies, get_company_names_without_tpm, update_company_career_url,
    get_jd_urls, get_jd_url_meta, batch_update_jd_timestamps,
    get_jd_rows_for_match, upsert_jd_record, batch_upsert_jd_records,
    get_incomplete_jd_rows, count_tpm_jobs_by_company, update_company_job_counts,
    get_match_pairs, upsert_match_record, batch_upsert_match_records,
    get_scored_matches, get_tailored_match_pairs, batch_upsert_tailored_records,
    get_archived_companies, update_archive_status, unarchive_company,
    get_company_archive_info, count_valid_tpm_jobs_by_company,
    classify_location, sort_jd_tracker_by_tier, sort_company_list_by_track,
    get_triaged_jd_urls, TRIAGE_SHEETS, canonical_jd_url,
    get_incomplete_company_rows, update_company_business_focus,
    load_workbook_readonly,
    COMPANY_HEADERS, JD_HEADERS, MATCH_HEADERS, TAILORED_HEADERS, _JD_COL,
)
import openpyxl


def _tmp_xlsx() -> str:
    """Return path to a temp xlsx that doesn't exist yet."""
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    os.remove(path)  # get_or_create_excel will create it
    return path


class TestGetOrCreateExcel(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_creates_file(self):
        get_or_create_excel(self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_creates_required_sheets(self):
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        for sheet in ["Company_List", "Company_Without_TPM", "JD_Tracker", "Match_Results"]:
            self.assertIn(sheet, wb.sheetnames, f"Missing sheet: {sheet}")

    def test_returns_path(self):
        returned = get_or_create_excel(self.path)
        self.assertEqual(returned, self.path)

    def test_idempotent_second_call(self):
        """Calling twice should not error and return same path."""
        get_or_create_excel(self.path)
        returned = get_or_create_excel(self.path)
        self.assertEqual(returned, self.path)

    def test_company_list_headers(self):
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        for h in ["Company Name", "Track", "Career URL"]:
            self.assertIn(h, headers)

    def test_match_results_headers(self):
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Match_Results"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        for h in ["Resume ID", "JD URL", "Score", "Resume Hash", "Stage"]:
            self.assertIn(h, headers)

    def test_creates_triage_sheets(self):
        """BUG-65: fresh workbooks include the user triage tabs with JD headers."""
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        for sheet in TRIAGE_SHEETS:
            self.assertIn(sheet, wb.sheetnames, f"Missing triage sheet: {sheet}")
            ws = wb[sheet]
            headers = [ws.cell(1, c).value for c in range(1, len(JD_HEADERS) + 1)]
            self.assertEqual(headers, JD_HEADERS)
        wb.close()

    def test_migration_adds_missing_triage_sheets(self):
        """BUG-65: existing workbooks gain missing triage tabs on next open."""
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        for sheet in TRIAGE_SHEETS:
            del wb[sheet]
        wb.save(self.path)
        wb.close()
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        for sheet in TRIAGE_SHEETS:
            self.assertIn(sheet, wb.sheetnames)
        wb.close()

    def test_existing_triage_rows_untouched(self):
        """BUG-65: user data in triage tabs survives get_or_create_excel."""
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        wb["Skipped JD"].append(["https://x.co/jd/1", "TPM", "XCo"])
        wb.save(self.path)
        wb.close()
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        self.assertEqual(wb["Skipped JD"].cell(2, 1).value, "https://x.co/jd/1")
        wb.close()

    def test_bug06_move_fails_raises_runtimeerror_not_recursion(self):
        """BUG-06 regression: if shutil.move fails during corrupt-file recovery,
        get_or_create_excel must raise RuntimeError instead of infinite recursion."""
        import tempfile
        from unittest.mock import patch

        fd, corrupt_path = tempfile.mkstemp(suffix=".xlsx")
        os.write(fd, b"CORRUPT NOT A ZIP")
        os.close(fd)
        try:
            with patch("shutil.move", side_effect=OSError("disk full")):
                with self.assertRaises(RuntimeError):
                    get_or_create_excel(corrupt_path)
        finally:
            if os.path.exists(corrupt_path):
                os.remove(corrupt_path)

    def test_bug06_move_succeeds_recreates_fresh_file(self):
        """BUG-06: when shutil.move succeeds, recursive call must create a fresh
        valid xlsx (not loop again)."""
        import tempfile

        fd, corrupt_path = tempfile.mkstemp(suffix=".xlsx")
        os.write(fd, b"CORRUPT NOT A ZIP")
        os.close(fd)
        bak = corrupt_path + ".bak"
        try:
            result = get_or_create_excel(corrupt_path)
            self.assertEqual(result, corrupt_path)
            self.assertTrue(os.path.exists(corrupt_path))
            wb = openpyxl.load_workbook(corrupt_path)
            self.assertIn("Company_List", wb.sheetnames)
        finally:
            if os.path.exists(corrupt_path):
                os.remove(corrupt_path)
            if os.path.exists(bak):
                os.remove(bak)


class TestCountAndGetCompanyRows(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_empty_returns_zero(self):
        self.assertEqual(count_company_rows(self.path), 0)

    def test_count_after_insert(self):
        upsert_companies(self.path, [
            {"company_name": "TestCo", "ai_domain": "AI Startups",
             "business_focus": "Focus", "career_url": "https://testco.com/jobs"},
        ])
        self.assertEqual(count_company_rows(self.path), 1)

    def test_get_rows_returns_list(self):
        upsert_companies(self.path, [
            {"company_name": "Alpha", "ai_domain": "AI Startups",
             "business_focus": "B", "career_url": "https://alpha.com/careers"},
        ])
        rows = get_company_rows(self.path)
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Alpha")

    def test_empty_get_rows(self):
        rows = get_company_rows(self.path)
        self.assertEqual(rows, [])


class TestUpsertCompanies(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_insert_two_companies(self):
        upsert_companies(self.path, [
            {"company_name": "A", "ai_domain": "AI Startups",
             "business_focus": "Focus A", "career_url": "https://a.com/jobs"},
            {"company_name": "B", "ai_domain": "Large Model Labs",
             "business_focus": "Focus B", "career_url": "https://b.ai/careers"},
        ])
        self.assertEqual(count_company_rows(self.path), 2)

    def test_update_existing_company(self):
        upsert_companies(self.path, [
            {"company_name": "Corp", "track": "AI-native",
             "business_focus": "Old", "career_url": "https://old.com/jobs"},
        ])
        upsert_companies(self.path, [
            {"company_name": "Corp", "track": "Mid-large Tech",
             "business_focus": "New focus", "career_url": "https://new.com/careers"},
        ])
        # Should still be 1 row (updated, not duplicated)
        self.assertEqual(count_company_rows(self.path), 1)
        rows = get_company_rows(self.path)
        self.assertEqual(rows[0][1], "Mid-large Tech")

    def test_missing_career_url_defaults_na(self):
        upsert_companies(self.path, [
            {"company_name": "NoCareers", "ai_domain": "AI Startups",
             "business_focus": "Focus"},
        ])
        rows = get_company_rows(self.path)
        self.assertEqual(rows[0][3], "N/A")


class TestP0_5UpsertCompaniesPreservesCounts(unittest.TestCase):
    """P0-5: upsert_companies must NOT reset cols 6-9 (TPM Jobs / AI TPM Jobs /
    No TPM Count / Auto Archived) on existing rows. Those columns are managed
    by update_company_job_counts and the auto-archival pipeline; resetting
    them on upsert silently destroys data when the pipeline crashes between
    company discovery and JD scraping."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _seed_company_with_counts(self, name: str, tpm: int, ai_tpm: int,
                                  no_tpm: int, auto_archived: str):
        upsert_companies(self.path, [{
            "company_name": name, "ai_domain": "NLP",
            "business_focus": "X", "career_url": "https://example.com/jobs"
        }])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        # Find the row by name
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, 1).value == name:
                ws.cell(r, 6).value = tpm
                ws.cell(r, 7).value = ai_tpm
                ws.cell(r, 8).value = no_tpm
                ws.cell(r, 9).value = auto_archived
                break
        wb.save(self.path)
        wb.close()

    def _read_row(self, name: str):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        try:
            for r in range(2, ws.max_row + 1):
                if ws.cell(r, 1).value == name:
                    return [ws.cell(r, c).value for c in range(1, 10)]
            return None
        finally:
            wb.close()

    def test_existing_row_preserves_tpm_counts(self):
        self._seed_company_with_counts("Acme", tpm=5, ai_tpm=2,
                                       no_tpm=1, auto_archived="Yes")
        # Re-upsert with NEW career_url
        upsert_companies(self.path, [{
            "company_name": "Acme", "ai_domain": "NLP",
            "business_focus": "X", "career_url": "https://acme.io/careers"
        }])
        row = self._read_row("Acme")
        # REQ-153: a filled Career URL is user-verified and never overwritten
        self.assertEqual(row[3], "https://example.com/jobs",
                         "filled career_url must be preserved (REQ-153)")
        self.assertEqual(row[5], 5, "TPM Jobs must be preserved")
        self.assertEqual(row[6], 2, "AI TPM Jobs must be preserved")
        self.assertEqual(row[7], 1, "No TPM Count must be preserved")
        self.assertEqual(row[8], "Yes", "Auto Archived must be preserved")

    def test_new_row_initializes_counts_to_zero(self):
        upsert_companies(self.path, [{
            "company_name": "NewCo", "ai_domain": "NLP",
            "business_focus": "X", "career_url": "https://newco.com"
        }])
        row = self._read_row("NewCo")
        self.assertEqual(row[5], 0)
        self.assertEqual(row[6], 0)
        self.assertEqual(row[7], 0)
        self.assertEqual(row[8], "No")

    def test_existing_row_updates_cols_1_to_5(self):
        self._seed_company_with_counts("MutCo", tpm=9, ai_tpm=3,
                                       no_tpm=0, auto_archived="No")
        upsert_companies(self.path, [{
            "company_name": "MutCo", "track": "Robotics",
            "business_focus": "Updated focus",
            "career_url": "https://mutco.io/jobs"
        }])
        row = self._read_row("MutCo")
        self.assertEqual(row[1], "Robotics")
        self.assertEqual(row[2], "Updated focus")
        # REQ-153: col 4 is the exception — filled Career URL never overwritten
        self.assertEqual(row[3], "https://example.com/jobs")
        # Updated At (col 5, index 4) should be a non-empty string
        self.assertIsNotNone(row[4])
        self.assertNotEqual(row[4], "")
        # Counts preserved
        self.assertEqual(row[5], 9)
        self.assertEqual(row[6], 3)


class TestGetCompanyNamesWithoutTPM(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_empty_when_no_data(self):
        names = get_company_names_without_tpm(self.path)
        self.assertIsInstance(names, set)
        self.assertEqual(len(names), 0)

    def test_reads_without_tpm_sheet(self):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_Without_TPM"]
        ws.append(["CompanyX", "AI Startups", "Focus", "https://x.com", "2026-01-01"])
        wb.save(self.path)
        names = get_company_names_without_tpm(self.path)
        self.assertIn("CompanyX", names)


class TestUpdateCompanyCareerUrl(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)
        upsert_companies(self.path, [
            {"company_name": "UrlCo", "ai_domain": "AI Startups",
             "business_focus": "F", "career_url": "https://old.com/jobs"},
        ])

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_updates_url(self):
        # First data row is Excel row 2 (row 1 = header)
        update_company_career_url(self.path, 2, "https://new.com/careers")
        rows = get_company_rows(self.path)
        self.assertEqual(rows[0][3], "https://new.com/careers")


class TestBug05CareerUrlRowAlignment(unittest.TestCase):
    """Regression test for BUG-05: update_company_career_url must write to the
    correct row even when empty rows exist between data rows in the sheet."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _insert_with_gap(self):
        """Insert CompanyA at row 2, leave row 3 empty, insert CompanyB at row 4."""
        import openpyxl
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        # row 2: CompanyA
        ws.cell(2, 1, "CompanyA"); ws.cell(2, 4, "https://a.com/jobs")
        # row 3: intentionally empty (simulates real-world sparse sheets)
        # row 4: CompanyB
        ws.cell(4, 1, "CompanyB"); ws.cell(4, 4, "https://b.com/jobs")
        wb.save(self.path)

    def test_get_company_rows_with_row_num_returns_actual_excel_rows(self):
        """get_company_rows_with_row_num() must return actual Excel row numbers,
        not list indices — critical for correct write-back when gaps exist."""
        self._insert_with_gap()
        indexed = get_company_rows_with_row_num(self.path)
        # Should return two entries with actual row numbers 2 and 4
        self.assertEqual(len(indexed), 2)
        excel_rows = [r for r, _ in indexed]
        self.assertIn(2, excel_rows, "CompanyA should be at Excel row 2")
        self.assertIn(4, excel_rows, "CompanyB should be at Excel row 4")

    def test_update_second_company_with_gap_does_not_corrupt_first(self):
        """BUG-05: when an empty row separates two companies, updating the
        second company must write to Excel row 4, not row 3."""
        self._insert_with_gap()
        indexed = get_company_rows_with_row_num(self.path)
        # Find CompanyB's actual Excel row
        company_b_entry = next((r, row) for r, row in indexed if row[0] == "CompanyB")
        excel_row_b, _ = company_b_entry
        self.assertEqual(excel_row_b, 4, "CompanyB must be at Excel row 4")

        update_company_career_url(self.path, excel_row_b, "https://b.com/careers-new")

        rows = get_company_rows(self.path)
        names = [r[0] for r in rows]
        urls  = {r[0]: r[3] for r in rows}

        # CompanyA must be untouched
        self.assertEqual(urls.get("CompanyA"), "https://a.com/jobs",
                         "CompanyA URL must NOT be modified (BUG-05 regression)")
        # CompanyB must have the new URL
        self.assertEqual(urls.get("CompanyB"), "https://b.com/careers-new",
                         "CompanyB URL must be updated correctly")


class TestJDHelpers(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _insert_jd(self, url, company="TestCorp", job_domain="AI",
                   location="Remote", tech="pytorch", resp="Lead AI",
                   data_quality=""):
        jd_json = json.dumps({
            "job_title": "TPM",
            "company": company,
            "location": location,
            "salary_range": "$200k",
            "requirements": [tech],
            "additional_qualifications": ["Nice to have"],
            "key_responsibilities": [resp],
            "job_domain": job_domain,
            "data_quality": data_quality,
        })
        upsert_jd_record(self.path, url, jd_json, "abc123")

    def test_get_jd_urls_empty(self):
        urls = get_jd_urls(self.path)
        self.assertEqual(urls, [])

    def test_get_jd_urls_after_insert(self):
        self._insert_jd("https://jobs.lever.co/testcorp/123")
        urls = get_jd_urls(self.path)
        self.assertIn("https://jobs.lever.co/testcorp/123", urls)

    def test_get_jd_urls_excludes_na_company(self):
        # Manually insert a row with N/A company
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        ws.append(["https://example.com/job1", "Bad JD", "N/A",
                   "", "", "", "", "False", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""])
        wb.save(self.path)
        urls = get_jd_urls(self.path)
        self.assertNotIn("https://example.com/job1", urls)

    def test_get_jd_url_meta_has_hash(self):
        self._insert_jd("https://greenhouse.io/testco/job/1")
        meta = get_jd_url_meta(self.path)
        self.assertIn("https://greenhouse.io/testco/job/1", meta)
        self.assertIn("hash", meta["https://greenhouse.io/testco/job/1"])

    def test_get_jd_url_meta_age_days(self):
        self._insert_jd("https://greenhouse.io/testco/job/2")
        meta = get_jd_url_meta(self.path)
        age = meta["https://greenhouse.io/testco/job/2"]["age_days"]
        self.assertLess(age, 1.0)  # just inserted, should be < 1 day

    def test_get_jd_rows_for_match_all_valid_rows(self):
        """REQ-004-21: every written row is already domain-qualified — the
        selector returns all valid rows across tracks, carrying job_domain."""
        self._insert_jd("https://lever.co/x/ai", job_domain="AI")
        self._insert_jd("https://lever.co/x/space", job_domain="Space")
        rows = get_jd_rows_for_match(self.path)
        by_url = {r["url"]: r for r in rows}
        self.assertIn("https://lever.co/x/ai", by_url)
        self.assertIn("https://lever.co/x/space", by_url)
        self.assertEqual(by_url["https://lever.co/x/space"]["job_domain"], "Space")
        jd = json.loads(by_url["https://lever.co/x/space"]["jd_json"])
        self.assertEqual(jd["job_domain"], "Space")

    def test_get_jd_rows_for_match_excludes_failed_quality(self):
        self._insert_jd("https://lever.co/x/ok", job_domain="AI")
        self._insert_jd("https://lever.co/x/bad", job_domain="AI",
                        data_quality="failed")
        urls = [r["url"] for r in get_jd_rows_for_match(self.path)]
        self.assertIn("https://lever.co/x/ok", urls)
        self.assertNotIn("https://lever.co/x/bad", urls)

    def test_get_jd_rows_for_match_invalid_domain_falls_back_to_ai(self):
        """Blank/invalid Job Domain must never drop the row — AI fallback + warning."""
        self._insert_jd("https://lever.co/x/blank", job_domain="")
        rows = get_jd_rows_for_match(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_domain"], "AI")

    def test_upsert_jd_record_update_existing(self):
        url = "https://jobs.lever.co/testco/xyz"
        self._insert_jd(url, location="NYC")
        jd_json = json.dumps({
            "job_title": "Sr TPM", "company": "TestCorp",
            "location": "San Francisco", "salary_range": "$250k",
            "core_ai_tech_stack": ["tensorflow"],
            "key_responsibilities": ["Lead ML"],
            "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, url, jd_json, "newhash")
        # Should not duplicate
        urls = get_jd_urls(self.path)
        self.assertEqual(urls.count(url), 1)

    def test_upsert_jd_record_bad_json(self):
        url = "https://example.com/bad"
        upsert_jd_record(self.path, url, "NOT JSON", "hash")
        # Should not raise; URL should be in tracker with JSON ERROR
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        found = False
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, 1).value == url:
                found = True
                self.assertEqual(ws.cell(r, 2).value, "JSON ERROR")
        self.assertTrue(found)

    def test_batch_upsert_jd_records(self):
        records = [
            ("https://gh.io/a/1", json.dumps({
                "job_title": "TPM", "company": "CompA", "location": "Remote",
                "salary_range": "N/A", "core_ai_tech_stack": ["llm"],
                "key_responsibilities": ["ship"], "is_ai_tpm": True,
            }), "hash1"),
            ("https://gh.io/a/2", json.dumps({
                "job_title": "PM", "company": "CompB", "location": "NYC",
                "salary_range": "N/A", "core_ai_tech_stack": [],
                "key_responsibilities": [], "is_ai_tpm": False,
            }), "hash2"),
        ]
        n = batch_upsert_jd_records(self.path, records)
        self.assertEqual(n, 2)
        urls = get_jd_urls(self.path)
        self.assertIn("https://gh.io/a/1", urls)


class TestGetIncompleteJdRows(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_complete_jd_not_returned(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "FullCo", "location": "Remote",
            "salary_range": "$200k", "requirements": ["pytorch"],
            "additional_qualifications": ["Nice to have"],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, "https://jd.com/1", jd_json, "h")
        incomplete = get_incomplete_jd_rows(self.path)
        urls = [r["url"] for r in incomplete]
        self.assertNotIn("https://jd.com/1", urls)

    def test_incomplete_rows_carry_date_fields(self):
        """BUG-67: retry records include the row's Posted Date + Date Flag so
        the retry rewrite can preserve them."""
        jd_json = json.dumps({
            "job_title": "TPM", "company": "DateCo", "location": "N/A",
            "requirements": [], "key_responsibilities": [],
            "posted_date": "2026-07-01",
        })
        upsert_jd_record(self.path, "https://jd.com/dated", jd_json, "h")
        incomplete = get_incomplete_jd_rows(self.path)
        rec = next(r for r in incomplete if r["url"] == "https://jd.com/dated")
        self.assertEqual(rec["posted_date"], "2026-07-01")
        self.assertIn("date_flag", rec)

    def test_missing_location_returned(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "NoCo", "location": "N/A",
            "salary_range": "N/A", "core_ai_tech_stack": ["pytorch"],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, "https://jd.com/2", jd_json, "h")
        incomplete = get_incomplete_jd_rows(self.path)
        urls = [r["url"] for r in incomplete]
        self.assertIn("https://jd.com/2", urls)


class TestCountTpmJobsByCompany(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_counts_jobs_correctly(self):
        for i, domain in enumerate(["AI", "Fintech", ""]):
            jd_json = json.dumps({
                "job_title": "TPM", "company": "BigCo", "location": "Remote",
                "salary_range": "N/A", "core_ai_tech_stack": [],
                "key_responsibilities": [], "job_domain": domain,
            })
            upsert_jd_record(self.path, f"https://bco.com/job/{i}", jd_json, f"h{i}")
        counts = count_tpm_jobs_by_company(self.path)
        self.assertIn("BigCo", counts)
        self.assertEqual(counts["BigCo"]["tpm"], 3)
        # Only rows with a valid 5-track Job Domain count as qualified
        self.assertEqual(counts["BigCo"]["qualified"], 2)

    def test_empty_tracker_returns_empty_dict(self):
        counts = count_tpm_jobs_by_company(self.path)
        self.assertEqual(counts, {})


class TestUpdateCompanyJobCounts(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)
        upsert_companies(self.path, [
            {"company_name": "BigCo", "ai_domain": "Big Tech (AI Investment)",
             "business_focus": "F", "career_url": "https://bigco.com/jobs"},
        ])

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_writes_tpm_counts(self):
        update_company_job_counts(self.path, {
            "BigCo": {"tpm": 5, "qualified": 3}
        })
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        tpm_col = headers.get("TPM Jobs")
        qual_col = headers.get("Qualified Jobs")
        self.assertIsNotNone(tpm_col)
        self.assertEqual(ws.cell(2, tpm_col).value, 5)
        self.assertEqual(ws.cell(2, qual_col).value, 3)


class TestBatchUpdateJdTimestamps(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_updates_known_urls(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "TimeCo", "location": "Remote",
            "salary_range": "N/A", "core_ai_tech_stack": [],
            "key_responsibilities": [], "is_ai_tpm": True,
        })
        url = "https://timeco.com/job/1"
        upsert_jd_record(self.path, url, jd_json, "h")
        n = batch_update_jd_timestamps(self.path, [url])
        self.assertEqual(n, 1)

    def test_empty_list_returns_zero(self):
        n = batch_update_jd_timestamps(self.path, [])
        self.assertEqual(n, 0)


class TestMatchHelpers(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _match_json(self, score=75):
        return json.dumps({
            "compatibility_score": score,
            "key_strengths": ["Strong LLM background"],
            "critical_gaps": ["No GenAI prod deployment"],
            "recommendation_reason": "Good fit overall.",
        })

    def test_get_match_pairs_empty(self):
        pairs = get_match_pairs(self.path)
        self.assertEqual(pairs, {})

    def test_upsert_match_record_insert(self):
        upsert_match_record(
            self.path, "resume_v1", "https://jobs.lever.co/x/1",
            self._match_json(80), "abc", "fine"
        )
        pairs = get_match_pairs(self.path)
        key = ("resume_v1", "https://jobs.lever.co/x/1")
        self.assertIn(key, pairs)
        self.assertEqual(pairs[key]["score"], 80)

    def test_upsert_match_record_update(self):
        url = "https://jobs.lever.co/x/2"
        upsert_match_record(self.path, "r1", url, self._match_json(50), "h", "coarse")
        upsert_match_record(self.path, "r1", url, self._match_json(85), "h2", "fine")
        pairs = get_match_pairs(self.path)
        self.assertEqual(pairs[("r1", url)]["score"], 85)
        self.assertEqual(pairs[("r1", url)]["stage"], "fine")

    def test_get_match_pairs_stage_and_hash(self):
        upsert_match_record(
            self.path, "rv2", "https://gh.io/c/1",
            self._match_json(60), "resumehash", "coarse"
        )
        pairs = get_match_pairs(self.path)
        info = pairs[("rv2", "https://gh.io/c/1")]
        self.assertEqual(info["stage"], "coarse")
        self.assertEqual(info["hash"], "resumehash")

    def test_batch_upsert_match_records(self):
        records = [
            ("r1", "https://url1.com", self._match_json(70), "h1", "coarse"),
            ("r1", "https://url2.com", self._match_json(90), "h1", "fine"),
        ]
        n = batch_upsert_match_records(self.path, records)
        self.assertEqual(n, 2)
        pairs = get_match_pairs(self.path)
        self.assertIn(("r1", "https://url1.com"), pairs)
        self.assertIn(("r1", "https://url2.com"), pairs)

    def test_batch_upsert_empty_returns_zero(self):
        n = batch_upsert_match_records(self.path, [])
        self.assertEqual(n, 0)

    def test_upsert_match_bad_json(self):
        upsert_match_record(self.path, "rx", "https://bad.com/jd", "NOT JSON", "", "fine")
        pairs = get_match_pairs(self.path)
        self.assertIn(("rx", "https://bad.com/jd"), pairs)
        self.assertEqual(pairs[("rx", "https://bad.com/jd")]["score"], 0)


class TestPRJ002GetScoredMatchesPerDim(unittest.TestCase):
    """PRJ-002 PR 4 — get_scored_matches returns per-dim columns
    (None when column missing or value blank)."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _match_json(self, score=70):
        return json.dumps({
            "compatibility_score": score,
            "key_strengths": [], "critical_gaps": [],
            "recommendation_reason": "ok",
        })

    def test_reads_per_dim_columns_when_present(self):
        # Write a fine-stage row WITH all 3 dims (PR 3+ shape).
        batch_upsert_match_records(self.path, [{
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "match_json": self._match_json(80),
            "resume_hash": "h", "stage": "fine",
            "ats_coverage_percent": 65.5,
            "ats_missing": ["Rust"],
            "recruiter_score": 72,
            "hm_score": 80,
        }])
        scored = get_scored_matches(self.path, stage="fine")
        self.assertEqual(len(scored), 1)
        m = scored[0]
        self.assertEqual(m["score"], 80)
        self.assertEqual(m["ats_coverage_percent"], 65.5)
        self.assertEqual(m["recruiter_score"], 72)
        self.assertEqual(m["hm_score"], 80)

    def test_returns_none_for_blank_per_dim_cells(self):
        # Legacy row (pre-PR 3): only Score column, new dim cols blank.
        upsert_match_record(self.path, "r1", "https://a.com/1",
                            self._match_json(75), "h", "fine")
        scored = get_scored_matches(self.path, stage="fine")
        m = scored[0]
        self.assertEqual(m["score"], 75)
        self.assertIsNone(m["ats_coverage_percent"])
        self.assertIsNone(m["recruiter_score"])
        self.assertIsNone(m["hm_score"])


class TestPRJ002TailoredPerDimWrites(unittest.TestCase):
    """PRJ-002 PR 4 — batch_upsert_tailored_records writes per-dim columns."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _read_row(self):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, 1).value:
                row = {h: ws.cell(r, c + 1).value for c, h in enumerate(headers)}
                wb.close()
                return row
        wb.close()
        return None

    def test_writes_all_per_dim_columns(self):
        rec = {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "job_title": "TPM", "company": "Acme",
            "original_score": 42, "tailored_score": 45, "score_delta": 3,
            "tailored_resume_path": "/p", "optimization_summary": "s",
            "resume_hash": "h",
            # PR 4 per-dim:
            "original_ats": 40.0, "tailored_ats": 92.0, "ats_delta": 52.0,
            "original_recruiter": 58, "tailored_recruiter": 64, "recruiter_delta": 6,
            "original_hm": 42, "tailored_hm": 45, "hm_delta": 3,
        }
        batch_upsert_tailored_records(self.path, [rec])
        row = self._read_row()
        # Per-dim columns
        self.assertEqual(row["Original ATS"], 40.0)
        self.assertEqual(row["Tailored ATS"], 92.0)
        self.assertEqual(row["ATS Delta"], 52.0)
        self.assertEqual(row["Original Recruiter"], 58)
        self.assertEqual(row["Tailored Recruiter"], 64)
        self.assertEqual(row["Recruiter Delta"], 6)
        self.assertEqual(row["Original HM"], 42)
        self.assertEqual(row["Tailored HM"], 45)
        self.assertEqual(row["HM Delta"], 3)
        # Legacy mirroring
        self.assertEqual(row["Original Score"], 42)
        self.assertEqual(row["Tailored Score"], 45)
        self.assertEqual(row["Score Delta"], 3)

    def test_none_per_dim_writes_none(self):
        rec = {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "original_score": 50, "tailored_score": 50, "score_delta": 0,
            "resume_hash": "h",
            "original_ats": None, "tailored_ats": None, "ats_delta": None,
            "original_recruiter": None, "tailored_recruiter": 60, "recruiter_delta": None,
        }
        batch_upsert_tailored_records(self.path, [rec])
        row = self._read_row()
        self.assertIsNone(row["Original ATS"])
        self.assertIsNone(row["Tailored ATS"])
        self.assertIsNone(row["ATS Delta"])
        self.assertIsNone(row["Original Recruiter"])
        self.assertEqual(row["Tailored Recruiter"], 60)
        self.assertIsNone(row["Recruiter Delta"])


class TestPRJ002RegressionFromHMDelta(unittest.TestCase):
    """PRJ-002 PR 4 / REQ-108 — regression rule prefers hm_delta over score_delta.

    Critical correctness property: ATS / Recruiter dropping while HM holds
    must NOT mark a regression. The user wants to keep using the tailored
    resume in that case (better keywords, same fit)."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _read_regression(self):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        col = headers.index("Regression") + 1
        result = ws.cell(2, col).value
        wb.close()
        return result

    def _base_rec(self):
        return {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "job_title": "TPM", "company": "Acme",
            "tailored_resume_path": "/p", "optimization_summary": "s",
            "resume_hash": "h",
        }

    def test_explicit_regression_wins(self):
        rec = self._base_rec()
        rec.update({"score_delta": 10, "hm_delta": 10, "regression": True})
        batch_upsert_tailored_records(self.path, [rec])
        self.assertTrue(self._read_regression())

    def test_hm_delta_negative_marks_regression(self):
        rec = self._base_rec()
        rec.update({"score_delta": -3, "hm_delta": -3})
        batch_upsert_tailored_records(self.path, [rec])
        self.assertTrue(self._read_regression())

    def test_hm_delta_positive_no_regression(self):
        rec = self._base_rec()
        rec.update({"score_delta": 5, "hm_delta": 5})
        batch_upsert_tailored_records(self.path, [rec])
        self.assertFalse(self._read_regression())

    def test_ats_drop_with_hm_hold_no_regression(self):
        """ATS drops a lot, HM holds → NOT a regression (REQ-108)."""
        rec = self._base_rec()
        rec.update({
            "score_delta": 0,        # legacy mirrors HM = 0 (no change)
            "hm_delta": 0,
            "ats_delta": -30.0,      # big ATS drop — should NOT regress
            "recruiter_delta": -5,   # Recruiter drop — should NOT regress
        })
        batch_upsert_tailored_records(self.path, [rec])
        self.assertFalse(self._read_regression())

    def test_legacy_fallback_to_score_delta(self):
        """When hm_delta absent (pre-PR 4 caller), fall back to score_delta."""
        rec = self._base_rec()
        rec.update({"score_delta": -5})
        batch_upsert_tailored_records(self.path, [rec])
        self.assertTrue(self._read_regression())

    def test_legacy_fallback_zero_no_regression(self):
        rec = self._base_rec()
        rec.update({"score_delta": 0})
        batch_upsert_tailored_records(self.path, [rec])
        self.assertFalse(self._read_regression())


class TestPRJ002BatchUpsertDictFormat(unittest.TestCase):
    """PRJ-002 PR 3 — dict-format records write 3-dim columns; tuple format
    still works (back-compat); 'key absent' on dicts preserves prior values."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _read_row(self, key=("r1", "https://a.com/1")):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Match_Results"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, 1).value == key[0] and ws.cell(r, 2).value == key[1]:
                row = {h: ws.cell(r, c + 1).value for c, h in enumerate(headers)}
                wb.close()
                return row
        wb.close()
        return None

    def _match_json(self, score=70, strengths=None, gaps=None, reason="ok"):
        return json.dumps({
            "compatibility_score": score,
            "key_strengths": strengths or [],
            "critical_gaps": gaps or [],
            "recommendation_reason": reason,
        })

    def test_dict_record_writes_all_three_dim_columns(self):
        rec = {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "match_json": self._match_json(70),
            "resume_hash": "h", "stage": "coarse",
            "ats_coverage_percent": 78.5,
            "ats_missing": ["Rust", "Erlang"],
            "recruiter_score": 70,
            "hm_score": None,
        }
        batch_upsert_match_records(self.path, [rec])
        row = self._read_row()
        self.assertEqual(row["ATS Coverage %"], 78.5)
        self.assertEqual(row["Recruiter Score"], 70)
        self.assertIsNone(row["HM Score"])
        self.assertEqual(row["ATS Missing"], "Rust, Erlang")

    def test_ats_missing_caps_at_top_5(self):
        rec = {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "match_json": self._match_json(50),
            "resume_hash": "h", "stage": "coarse",
            "ats_missing": ["a", "b", "c", "d", "e", "f", "g"],
        }
        batch_upsert_match_records(self.path, [rec])
        row = self._read_row()
        self.assertEqual(row["ATS Missing"], "a, b, c, d, e")

    def test_ats_missing_empty_list_writes_blank(self):
        rec = {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "match_json": self._match_json(80),
            "resume_hash": "h", "stage": "coarse",
            "ats_missing": [],
        }
        batch_upsert_match_records(self.path, [rec])
        row = self._read_row()
        self.assertIsNone(row["ATS Missing"])

    def test_stage2_omits_ats_recruiter_preserves_prior(self):
        # Stage 1: write ATS=78, Recruiter=70.
        batch_upsert_match_records(self.path, [{
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "match_json": self._match_json(70),
            "resume_hash": "h", "stage": "coarse",
            "ats_coverage_percent": 78.5,
            "ats_missing": ["Rust"],
            "recruiter_score": 70,
            "hm_score": None,
        }])
        # Stage 2: only HM dim, omits ATS / Recruiter keys entirely.
        batch_upsert_match_records(self.path, [{
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "match_json": self._match_json(45, ["s"], ["g"], "deep"),
            "resume_hash": "h", "stage": "fine",
            "hm_score": 45,
        }])
        row = self._read_row()
        # Legacy Score column reflects the latest (HM=45).
        self.assertEqual(row["Score"], 45)
        self.assertEqual(row["Stage"], "fine")
        # Per-dim columns: ATS / Recruiter preserved from Stage 1; HM updated.
        self.assertEqual(row["ATS Coverage %"], 78.5)
        self.assertEqual(row["Recruiter Score"], 70)
        self.assertEqual(row["HM Score"], 45)
        self.assertEqual(row["ATS Missing"], "Rust")

    def test_tuple_format_still_works_back_compat(self):
        # Legacy 5-tuple — must NOT break, must NOT touch new columns.
        records = [("r1", "https://a.com/1", self._match_json(80), "h", "fine")]
        batch_upsert_match_records(self.path, records)
        row = self._read_row()
        self.assertEqual(row["Score"], 80)
        self.assertEqual(row["Stage"], "fine")
        self.assertIsNone(row["ATS Coverage %"])
        self.assertIsNone(row["Recruiter Score"])
        self.assertIsNone(row["HM Score"])

    def test_mixed_tuple_and_dict_in_one_batch(self):
        records = [
            ("r1", "https://a.com/1", self._match_json(70), "h", "coarse"),
            {
                "resume_id": "r1", "jd_url": "https://a.com/2",
                "match_json": self._match_json(85),
                "resume_hash": "h", "stage": "fine",
                "hm_score": 85,
                "ats_coverage_percent": 60.0,
            },
        ]
        n = batch_upsert_match_records(self.path, records)
        self.assertEqual(n, 2)
        row1 = self._read_row(("r1", "https://a.com/1"))
        row2 = self._read_row(("r1", "https://a.com/2"))
        self.assertIsNone(row1["HM Score"])
        self.assertEqual(row2["HM Score"], 85)
        self.assertEqual(row2["ATS Coverage %"], 60.0)

    def test_ats_percent_none_writes_none(self):
        # Legacy JD with no ats_keywords yields percent=None — must round-trip.
        rec = {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "match_json": self._match_json(70),
            "resume_hash": "h", "stage": "coarse",
            "ats_coverage_percent": None,
            "ats_missing": [],
            "recruiter_score": 70,
        }
        batch_upsert_match_records(self.path, [rec])
        row = self._read_row()
        self.assertIsNone(row["ATS Coverage %"])
        self.assertEqual(row["Recruiter Score"], 70)


class TestBug12WorkbookClose(unittest.TestCase):
    """BUG-12 regression: every load_workbook() call must be followed by wb.close()."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _assert_close_called(self, fn, *args):
        """Wrap load_workbook to spy on close(); fail if close() is never called."""
        import openpyxl as _openpyxl
        closed = []
        original_load = _openpyxl.load_workbook

        def spy_load(*a, **kw):
            wb = original_load(*a, **kw)
            orig_close = wb.close
            def tracked_close():
                closed.append(True)
                orig_close()
            wb.close = tracked_close
            return wb

        with unittest.mock.patch("shared.excel_store.load_workbook", side_effect=spy_load):
            fn(*args)

        self.assertTrue(
            len(closed) > 0,
            f"{fn.__name__} did not call wb.close() — file handle leak (BUG-12)",
        )

    def test_count_company_rows_closes_workbook(self):
        self._assert_close_called(count_company_rows, self.path)

    def test_get_company_rows_closes_workbook(self):
        self._assert_close_called(get_company_rows, self.path)

    def test_get_company_rows_with_row_num_closes_workbook(self):
        self._assert_close_called(get_company_rows_with_row_num, self.path)

    def test_get_company_names_without_tpm_closes_workbook(self):
        self._assert_close_called(get_company_names_without_tpm, self.path)

    def test_get_jd_urls_closes_workbook(self):
        self._assert_close_called(get_jd_urls, self.path)

    def test_get_jd_url_meta_closes_workbook(self):
        self._assert_close_called(get_jd_url_meta, self.path)

    def test_get_jd_rows_for_match_closes_workbook(self):
        self._assert_close_called(get_jd_rows_for_match, self.path)

    def test_get_incomplete_jd_rows_closes_workbook(self):
        self._assert_close_called(get_incomplete_jd_rows, self.path)

    def test_count_tpm_jobs_by_company_closes_workbook(self):
        self._assert_close_called(count_tpm_jobs_by_company, self.path)

    def test_get_match_pairs_closes_workbook(self):
        self._assert_close_called(get_match_pairs, self.path)

    def test_upsert_companies_closes_workbook(self):
        self._assert_close_called(
            upsert_companies, self.path,
            [{"company_name": "CloseTestCo", "ai_domain": "AI", "business_focus": "F"}],
        )

    def test_update_company_career_url_closes_workbook(self):
        upsert_companies(self.path, [
            {"company_name": "CloseCo", "ai_domain": "AI",
             "business_focus": "F", "career_url": "https://old.com"},
        ])
        self._assert_close_called(update_company_career_url, self.path, 2, "https://new.com")

    def test_upsert_jd_record_closes_workbook(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "CloseCo", "location": "Remote",
            "salary_range": "N/A", "core_ai_tech_stack": [], "key_responsibilities": [],
            "is_ai_tpm": True,
        })
        self._assert_close_called(
            upsert_jd_record, self.path, "https://close.test/1", jd_json, "h"
        )

    def test_batch_upsert_jd_records_closes_workbook(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "CloseCo", "location": "Remote",
            "salary_range": "N/A", "core_ai_tech_stack": [], "key_responsibilities": [],
            "is_ai_tpm": True,
        })
        self._assert_close_called(
            batch_upsert_jd_records, self.path,
            [("https://close.test/2", jd_json, "h")],
        )

    def test_upsert_match_record_closes_workbook_on_early_return(self):
        """Ensure close() is called even when upsert_match_record takes the early-return path."""
        match_json = json.dumps({
            "compatibility_score": 80, "key_strengths": [], "critical_gaps": [],
            "recommendation_reason": "ok",
        })
        # Insert once so the update (early-return) path is exercised on second call
        upsert_match_record(self.path, "r_close", "https://close.test/mr", match_json, "h", "fine")
        self._assert_close_called(
            upsert_match_record, self.path, "r_close", "https://close.test/mr",
            match_json, "h2", "fine",
        )

    def test_batch_upsert_match_records_closes_workbook(self):
        match_json = json.dumps({
            "compatibility_score": 75, "key_strengths": [], "critical_gaps": [],
            "recommendation_reason": "ok",
        })
        self._assert_close_called(
            batch_upsert_match_records, self.path,
            [("r_close2", "https://close.test/3", match_json, "h", "fine")],
        )

    def test_update_company_job_counts_closes_workbook(self):
        self._assert_close_called(
            update_company_job_counts, self.path, {"CloseCo": {"tpm": 1, "ai_tpm": 0}}
        )

    def test_batch_update_jd_timestamps_closes_workbook(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "CloseCo", "location": "Remote",
            "salary_range": "N/A", "core_ai_tech_stack": [], "key_responsibilities": [],
            "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, "https://close.test/ts", jd_json, "h")
        self._assert_close_called(
            batch_update_jd_timestamps, self.path, ["https://close.test/ts"]
        )


class TestTailoredMatchResultsSheet(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_sheet_created(self):
        wb = openpyxl.load_workbook(self.path)
        self.assertIn("Tailored_Match_Results", wb.sheetnames)

    def test_headers_correct(self):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        for h in TAILORED_HEADERS:
            self.assertIn(h, headers)

    def test_migration_adds_sheet_to_existing_file(self):
        """If an older Excel has no Tailored_Match_Results sheet, migration adds it."""
        wb = openpyxl.load_workbook(self.path)
        if "Tailored_Match_Results" in wb.sheetnames:
            del wb["Tailored_Match_Results"]
        wb.save(self.path)
        wb.close()
        get_or_create_excel(self.path)
        wb2 = openpyxl.load_workbook(self.path)
        self.assertIn("Tailored_Match_Results", wb2.sheetnames)


class TestGetScoredMatches(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _match_json(self, score=75):
        return json.dumps({
            "compatibility_score": score,
            "key_strengths": ["Strong LLM background"],
            "critical_gaps": ["No GenAI prod deployment"],
            "recommendation_reason": "Good fit overall.",
        })

    def test_empty_returns_empty(self):
        results = get_scored_matches(self.path)
        self.assertEqual(results, [])

    def test_default_returns_only_fine_stage(self):
        """P0-6: by default get_scored_matches returns only Stage 2 'fine' rows.
        Stage 1 'coarse' heuristic scores are excluded so they never enter
        the tailored-resume score-delta computation."""
        upsert_match_record(self.path, "r1", "https://a.com/1", self._match_json(80), "h1", "fine")
        upsert_match_record(self.path, "r1", "https://a.com/2", self._match_json(0), "h1", "coarse")
        upsert_match_record(self.path, "r1", "https://a.com/3", self._match_json(45), "h1", "coarse")
        results = get_scored_matches(self.path)
        urls = [r["jd_url"] for r in results]
        self.assertEqual(urls, ["https://a.com/1"])
        self.assertEqual(results[0]["stage"], "fine")

    def test_stage_none_returns_all_stages(self):
        """P0-6: stage=None disables the filter (used by tests / migrations)."""
        upsert_match_record(self.path, "r1", "https://a.com/1", self._match_json(80), "h1", "fine")
        upsert_match_record(self.path, "r1", "https://a.com/2", self._match_json(45), "h1", "coarse")
        results = get_scored_matches(self.path, stage=None)
        urls = sorted(r["jd_url"] for r in results)
        self.assertEqual(urls, ["https://a.com/1", "https://a.com/2"])

    def test_explicit_stage_coarse_returns_only_coarse(self):
        upsert_match_record(self.path, "r1", "https://a.com/1", self._match_json(80), "h1", "fine")
        upsert_match_record(self.path, "r1", "https://a.com/2", self._match_json(45), "h1", "coarse")
        results = get_scored_matches(self.path, stage="coarse")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["jd_url"], "https://a.com/2")

    def test_returns_correct_fields(self):
        upsert_match_record(self.path, "r1", "https://a.com/1", self._match_json(70), "hash1", "fine")
        results = get_scored_matches(self.path)
        self.assertEqual(len(results), 1)
        rec = results[0]
        self.assertEqual(rec["resume_id"], "r1")
        self.assertEqual(rec["jd_url"], "https://a.com/1")
        self.assertEqual(rec["score"], 70)
        self.assertEqual(rec["stage"], "fine")
        self.assertEqual(rec["resume_hash"], "hash1")


class TestGetTailoredMatchPairs(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_empty_returns_empty(self):
        pairs = get_tailored_match_pairs(self.path)
        self.assertEqual(pairs, {})

    def test_returns_inserted_records(self):
        batch_upsert_tailored_records(self.path, [{
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "job_title": "TPM", "company": "TestCo",
            "original_score": 60, "tailored_score": 80, "score_delta": 20,
            "tailored_resume_path": "tailored_resumes/r1/abc.md",
            "optimization_summary": "Improved keywords",
            "resume_hash": "hash1",
        }])
        pairs = get_tailored_match_pairs(self.path)
        key = ("r1", "https://a.com/1")
        self.assertIn(key, pairs)
        self.assertEqual(pairs[key]["tailored_score"], 80)
        self.assertEqual(pairs[key]["resume_hash"], "hash1")

    def test_missing_sheet_returns_empty(self):
        """If sheet doesn't exist (e.g. old file), return empty dict."""
        wb = openpyxl.load_workbook(self.path)
        if "Tailored_Match_Results" in wb.sheetnames:
            del wb["Tailored_Match_Results"]
        wb.save(self.path)
        wb.close()
        pairs = get_tailored_match_pairs(self.path)
        self.assertEqual(pairs, {})


class TestLastWrittenHash(unittest.TestCase):
    """Tailored_Match_Results stores sha256 of last-written .md so the
    optimizer can detect user hand-edits between runs."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_column_exists_after_migration(self):
        self.assertIn("Last Written Hash", TAILORED_HEADERS)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        wb.close()
        self.assertIn("Last Written Hash", headers)

    def test_round_trip_persists_hash(self):
        """Write a record with last_written_hash → read back via get_tailored_match_pairs."""
        batch_upsert_tailored_records(self.path, [{
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "job_title": "TPM", "company": "TestCo",
            "original_score": 60, "tailored_score": 80, "score_delta": 20,
            "tailored_resume_path": "tailored_resumes/r1/abc.md",
            "optimization_summary": "x", "resume_hash": "h1",
            "last_written_hash": "a" * 64,
        }])
        pairs = get_tailored_match_pairs(self.path)
        key = ("r1", "https://a.com/1")
        self.assertIn(key, pairs)
        self.assertEqual(pairs[key]["last_written_hash"], "a" * 64)

    def test_missing_key_leaves_cell_untouched_on_update(self):
        """Re-upserting without last_written_hash must NOT clobber the existing hash.

        This matters for non-write code paths (not currently used, but the
        contract should match per-dim columns: 'key absent → preserve')."""
        batch_upsert_tailored_records(self.path, [{
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "job_title": "TPM", "company": "TestCo",
            "original_score": 60, "tailored_score": 80, "score_delta": 20,
            "tailored_resume_path": "/p", "optimization_summary": "x",
            "resume_hash": "h1",
            "last_written_hash": "b" * 64,
        }])
        # Second upsert WITHOUT the key
        batch_upsert_tailored_records(self.path, [{
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "job_title": "TPM", "company": "TestCo",
            "original_score": 60, "tailored_score": 85, "score_delta": 25,
            "tailored_resume_path": "/p", "optimization_summary": "y",
            "resume_hash": "h1",
            # last_written_hash absent
        }])
        pairs = get_tailored_match_pairs(self.path)
        self.assertEqual(pairs[("r1", "https://a.com/1")]["last_written_hash"], "b" * 64,
                         "Hash must be preserved when key is absent from upsert dict")

    def test_legacy_row_returns_empty_string(self):
        """Rows written before this migration have blank cell → empty string."""
        # Manually insert a row without populating the new column.
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        # Insert a stub row matching the legacy (pre-M1) column layout.
        # Column 11 is Resume Hash; new column "Last Written Hash" is later
        # and stays blank.
        ws.append(["r2", "https://b.com/2", "PM", "Co", 50, 60, 10,
                   "/p", "s", "2026-05-05", "h2"])
        wb.save(self.path)
        wb.close()
        pairs = get_tailored_match_pairs(self.path)
        self.assertEqual(pairs[("r2", "https://b.com/2")]["last_written_hash"], "")


class TestBatchUpsertTailoredRecords(unittest.TestCase):

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _rec(self, url="https://a.com/1", original=60, tailored=80):
        return {
            "resume_id": "r1", "jd_url": url,
            "job_title": "TPM", "company": "TestCo",
            "original_score": original, "tailored_score": tailored,
            "score_delta": tailored - original,
            "tailored_resume_path": f"tailored_resumes/r1/abc.md",
            "optimization_summary": "Improved keywords",
            "resume_hash": "hash1",
        }

    def test_insert_records(self):
        n = batch_upsert_tailored_records(self.path, [
            self._rec("https://a.com/1"),
            self._rec("https://a.com/2", 50, 70),
        ])
        self.assertEqual(n, 2)
        pairs = get_tailored_match_pairs(self.path)
        self.assertEqual(len(pairs), 2)

    def test_update_existing_record(self):
        batch_upsert_tailored_records(self.path, [self._rec("https://a.com/1", 60, 80)])
        batch_upsert_tailored_records(self.path, [self._rec("https://a.com/1", 60, 90)])
        pairs = get_tailored_match_pairs(self.path)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[("r1", "https://a.com/1")]["tailored_score"], 90)

    def test_empty_returns_zero(self):
        n = batch_upsert_tailored_records(self.path, [])
        self.assertEqual(n, 0)

    def test_writes_all_columns(self):
        batch_upsert_tailored_records(self.path, [self._rec()])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        self.assertEqual(ws.cell(2, 1).value, "r1")  # Resume ID
        self.assertEqual(ws.cell(2, 3).value, "TPM")  # Job Title
        self.assertEqual(ws.cell(2, 4).value, "TestCo")  # Company
        self.assertEqual(ws.cell(2, 5).value, 60)  # Original Score
        self.assertEqual(ws.cell(2, 6).value, 80)  # Tailored Score
        self.assertEqual(ws.cell(2, 7).value, 20)  # Score Delta
        self.assertIn("tailored_resumes", str(ws.cell(2, 8).value))  # Path
        self.assertEqual(ws.cell(2, 11).value, "hash1")  # Resume Hash
        self.assertEqual(ws.cell(2, 12).value, False)  # Regression (delta=20 → False)

    def test_regression_explicit_true(self):
        """When caller passes regression=True, it round-trips."""
        rec = self._rec("https://a.com/1", 80, 75)  # delta=-5
        rec["regression"] = True
        batch_upsert_tailored_records(self.path, [rec])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        self.assertEqual(ws.cell(2, 12).value, True)

    def test_regression_inferred_from_negative_delta(self):
        """If caller omits regression, it's inferred from score_delta < 0."""
        rec = self._rec("https://a.com/1", 80, 75)  # delta=-5
        rec.pop("regression", None)
        batch_upsert_tailored_records(self.path, [rec])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        self.assertEqual(ws.cell(2, 12).value, True)

    def test_regression_false_for_positive_delta(self):
        """No regression when tailored >= original."""
        rec = self._rec("https://a.com/1", 60, 75)  # delta=+15
        rec.pop("regression", None)
        batch_upsert_tailored_records(self.path, [rec])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        self.assertEqual(ws.cell(2, 12).value, False)

    def test_regression_false_for_zero_delta(self):
        """Tailored == base is not a regression."""
        rec = self._rec("https://a.com/1", 70, 70)  # delta=0
        rec.pop("regression", None)
        batch_upsert_tailored_records(self.path, [rec])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Tailored_Match_Results"]
        self.assertEqual(ws.cell(2, 12).value, False)


class TestTailoredHeadersHasRegression(unittest.TestCase):
    """TAILORED_HEADERS must include the Regression column at column 12."""

    def test_regression_in_headers(self):
        self.assertIn("Regression", TAILORED_HEADERS)

    def test_regression_position_stable(self):
        """Regression must stay at column 12 (index 11) so existing readers
        with hardcoded column references keep working. PR 2 appends NEW
        per-dim columns AFTER Regression — Regression itself doesn't move.
        """
        self.assertEqual(TAILORED_HEADERS.index("Regression"), 11)


class TestTailoredMigration(unittest.TestCase):
    """Existing files without Regression column should auto-migrate on open."""

    # Pre-P0-12 schema: 11 columns, no Regression, no per-dim columns.
    # Hardcoded so the test is stable across future schema additions.
    _PRE_P0_12_HEADERS = [
        "Resume ID", "JD URL", "Job Title", "Company", "Original Score",
        "Tailored Score", "Score Delta", "Tailored Resume Path",
        "Optimization Summary", "Updated At", "Resume Hash",
    ]

    def test_migration_adds_regression_column(self):
        path = _tmp_xlsx()
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Tailored_Match_Results"
            ws.append(self._PRE_P0_12_HEADERS)
            ws.append(["r1", "https://a.com/1", "TPM", "Co", 80, 70, -10,
                       "p", "s", "2026-01-01", "h"])
            ws.append(["r1", "https://a.com/2", "TPM", "Co", 60, 80, 20,
                       "p", "s", "2026-01-01", "h"])
            for required in ("Company_List", "Company_Without_TPM", "JD_Tracker",
                             "Match_Results"):
                wb.create_sheet(required)
            wb.save(path)
            wb.close()

            get_or_create_excel(path)

            wb = openpyxl.load_workbook(path)
            ws = wb["Tailored_Match_Results"]
            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
            self.assertIn("Regression", headers)
            reg_col = headers.index("Regression") + 1
            self.assertEqual(ws.cell(2, reg_col).value, True)   # delta=-10
            self.assertEqual(ws.cell(3, reg_col).value, False)  # delta=+20
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestPRJ002Phase4ATSKeywordsColumn(unittest.TestCase):
    """PRJ-002 Phase 4 fix — ats_keywords must round-trip through JD_Tracker.

    Pre-fix: job_agent extracted ats_keywords via Gemini, but the upsert path
    silently dropped the field (JD_HEADERS had no column for it), and
    get_jd_rows_for_match reconstructed jd_json without it. Result: every
    call to _extract_ats_keywords returned [], so ATS Coverage % was always
    None. The flagship dimension didn't actually work.
    """

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _jd_payload(self, ats_keywords):
        return json.dumps({
            "job_title": "AI TPM", "company": "Acme",
            "location": "Remote", "salary_range": "",
            "requirements": ["LLM experience"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead roadmap"],
            "is_ai_tpm": True,
            "ats_keywords": ats_keywords,
        })

    def test_jd_headers_has_ats_keywords(self):
        self.assertIn("ATS Keywords", JD_HEADERS)

    def test_upsert_jd_record_persists_ats_keywords(self):
        upsert_jd_record(self.path, "https://a.com/1",
                         self._jd_payload(["PyTorch", "Kubernetes", "LLM"]),
                         "h1")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        col = headers.index("ATS Keywords") + 1
        cell = ws.cell(2, col).value or ""
        for kw in ("PyTorch", "Kubernetes", "LLM"):
            self.assertIn(kw, cell)
        wb.close()

    def test_batch_upsert_jd_records_persists_ats_keywords(self):
        records = [
            ("https://a.com/1", self._jd_payload(["PyTorch"]), "h1"),
            ("https://a.com/2", self._jd_payload(["Kubernetes", "MLOps"]), "h2"),
        ]
        batch_upsert_jd_records(self.path, records)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        col = headers.index("ATS Keywords") + 1
        self.assertIn("PyTorch", ws.cell(2, col).value or "")
        self.assertIn("Kubernetes", ws.cell(3, col).value or "")
        self.assertIn("MLOps", ws.cell(3, col).value or "")
        wb.close()

    def test_empty_ats_keywords_writes_none_sentinel(self):
        # Same pattern as requirements / additional_qualifications.
        upsert_jd_record(self.path, "https://a.com/1",
                         self._jd_payload([]), "h1")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        col = headers.index("ATS Keywords") + 1
        self.assertEqual(ws.cell(2, col).value, "None")
        wb.close()

    def test_get_jd_rows_for_match_round_trips_ats_keywords(self):
        """End-to-end: write JD with keywords → read back → keywords survive."""
        upsert_jd_record(self.path, "https://a.com/1",
                         self._jd_payload(["PyTorch", "Kubernetes", "LLM"]),
                         "h1")
        rows = get_jd_rows_for_match(self.path)
        self.assertEqual(len(rows), 1)
        parsed = json.loads(rows[0]["jd_json"])
        self.assertEqual(parsed["ats_keywords"], ["PyTorch", "Kubernetes", "LLM"])

    def test_match_agent_extract_ats_keywords_now_works_end_to_end(self):
        """Regression test for the Phase 4 ship-blocker: this assertion would
        have failed pre-fix because get_jd_rows_for_match dropped the field."""
        from agents.match_agent import _extract_ats_keywords
        upsert_jd_record(self.path, "https://a.com/1",
                         self._jd_payload(["PyTorch", "Kubernetes"]),
                         "h1")
        rows = get_jd_rows_for_match(self.path)
        self.assertEqual(_extract_ats_keywords(rows[0]),
                         ["PyTorch", "Kubernetes"])


class TestPRJ004JDTrackerMigration(unittest.TestCase):
    """PRJ-004 REQ-004-14 — legacy JD_Tracker (with 'Is AI TPM' header) is
    replaced wholesale, but ONLY when it holds no data rows (the user wipes
    JD rows pre-launch; intake C3). With data present the migration must
    refuse loudly rather than silently apply over legacy rows."""

    _LEGACY_HEADERS = [
        "JD URL", "Job Title", "Company", "Location", "Salary",
        "Requirements", "Additional Qualifications", "Responsibilities",
        "Is AI TPM", "Updated At", "MD Hash", "Data Quality",
    ]

    def _make_legacy_workbook(self, path, with_data: bool):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "JD_Tracker"
        ws.append(self._LEGACY_HEADERS)
        if with_data:
            ws.append(["https://a.com/1", "TPM", "Acme", "Remote", "",
                       "• Req 1", "None", "• Resp 1",
                       "True", "2026-01-01", "h", ""])
        for required in ("Company_List", "Company_Without_TPM",
                         "Match_Results", "Tailored_Match_Results"):
            wb.create_sheet(required)
        wb.save(path)
        wb.close()

    def test_empty_legacy_sheet_gets_new_headers(self):
        path = _tmp_xlsx()
        try:
            self._make_legacy_workbook(path, with_data=False)
            get_or_create_excel(path)
            wb = openpyxl.load_workbook(path)
            ws = wb["JD_Tracker"]
            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
            wb.close()
            self.assertEqual(headers[:len(JD_HEADERS)], JD_HEADERS)
            self.assertNotIn("Is AI TPM", headers)
            for h in ("Job Domain", "Posted Date", "Freshness Tier", "Min YoE",
                      "YoE Flag", "Work-Auth Status", "Date Flag", "Sort Tier",
                      "ATS Keywords", "Data Quality"):
                self.assertIn(h, headers)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_legacy_sheet_with_data_raises(self):
        path = _tmp_xlsx()
        try:
            self._make_legacy_workbook(path, with_data=True)
            with self.assertRaises(RuntimeError):
                get_or_create_excel(path)
            # And nothing was deleted: the legacy row is still there.
            wb = openpyxl.load_workbook(path)
            ws = wb["JD_Tracker"]
            self.assertEqual(ws.cell(2, 1).value, "https://a.com/1")
            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
            self.assertIn("Is AI TPM", headers)
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestPRJ002Headers(unittest.TestCase):
    """PRJ-002 PR 2 — 3-dimension scoring columns must be in headers."""

    def test_match_headers_has_ats_coverage(self):
        self.assertIn("ATS Coverage %", MATCH_HEADERS)

    def test_match_headers_has_recruiter_score(self):
        self.assertIn("Recruiter Score", MATCH_HEADERS)

    def test_match_headers_has_hm_score(self):
        self.assertIn("HM Score", MATCH_HEADERS)

    def test_match_headers_has_ats_missing(self):
        self.assertIn("ATS Missing", MATCH_HEADERS)

    def test_match_legacy_columns_unchanged(self):
        # Old column indices must stay valid for back-compat readers.
        self.assertEqual(MATCH_HEADERS.index("Score"), 2)
        self.assertEqual(MATCH_HEADERS.index("Resume Hash"), 7)
        self.assertEqual(MATCH_HEADERS.index("Stage"), 8)

    def test_tailored_headers_has_per_dim_columns(self):
        for col in ("Original ATS", "Tailored ATS", "ATS Delta",
                    "Original Recruiter", "Tailored Recruiter", "Recruiter Delta",
                    "Original HM", "Tailored HM", "HM Delta"):
            self.assertIn(col, TAILORED_HEADERS, f"Missing column: {col}")

    def test_tailored_legacy_columns_unchanged(self):
        # Old column indices must stay valid.
        self.assertEqual(TAILORED_HEADERS.index("Original Score"), 4)
        self.assertEqual(TAILORED_HEADERS.index("Tailored Score"), 5)
        self.assertEqual(TAILORED_HEADERS.index("Score Delta"), 6)
        self.assertEqual(TAILORED_HEADERS.index("Resume Hash"), 10)
        self.assertEqual(TAILORED_HEADERS.index("Regression"), 11)


class TestPRJ002MatchResultsMigration(unittest.TestCase):
    """PRJ-002 PR 2 — Match_Results gains 4 columns; old files auto-migrate."""

    # Pre-PRJ-002 Match_Results schema: 9 columns (post-P0 stage column).
    _PRE_PRJ002_HEADERS = [
        "Resume ID", "JD URL", "Score", "Strengths", "Gaps", "Reason",
        "Updated At", "Resume Hash", "Stage",
    ]

    def test_migration_adds_three_dim_columns(self):
        path = _tmp_xlsx()
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Match_Results"
            ws.append(self._PRE_PRJ002_HEADERS)
            ws.append(["r1", "https://a.com/1", 75, "s1", "g1", "ok",
                       "2026-01-01", "h", "fine"])
            for required in ("Company_List", "Company_Without_TPM", "JD_Tracker",
                             "Tailored_Match_Results"):
                wb.create_sheet(required)
            wb.save(path)
            wb.close()

            get_or_create_excel(path)

            wb = openpyxl.load_workbook(path)
            ws = wb["Match_Results"]
            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
            for new_col in ("ATS Coverage %", "Recruiter Score", "HM Score", "ATS Missing"):
                self.assertIn(new_col, headers)
            # Existing row's legacy columns untouched.
            self.assertEqual(ws.cell(2, 3).value, 75)
            self.assertEqual(ws.cell(2, 9).value, "fine")
            # New columns blank for the existing row.
            for new_col in ("ATS Coverage %", "Recruiter Score", "HM Score", "ATS Missing"):
                col = headers.index(new_col) + 1
                self.assertIsNone(ws.cell(2, col).value)
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_migration_idempotent(self):
        """Running get_or_create_excel twice should not duplicate new columns."""
        path = _tmp_xlsx()
        try:
            get_or_create_excel(path)
            get_or_create_excel(path)
            wb = openpyxl.load_workbook(path)
            ws = wb["Match_Results"]
            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
            self.assertEqual(headers.count("ATS Coverage %"), 1)
            self.assertEqual(headers.count("HM Score"), 1)
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestPRJ002TailoredMigration(unittest.TestCase):
    """PRJ-002 PR 2 — Tailored_Match_Results gains 9 per-dim columns."""

    # Post-P0-12, pre-PRJ-002: 12 columns.
    _PRE_PRJ002_HEADERS = [
        "Resume ID", "JD URL", "Job Title", "Company", "Original Score",
        "Tailored Score", "Score Delta", "Tailored Resume Path",
        "Optimization Summary", "Updated At", "Resume Hash", "Regression",
    ]

    def test_migration_adds_nine_per_dim_columns(self):
        path = _tmp_xlsx()
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Tailored_Match_Results"
            ws.append(self._PRE_PRJ002_HEADERS)
            ws.append(["r1", "https://a.com/1", "TPM", "Co", 60, 80, 20,
                       "p", "s", "2026-01-01", "h", False])
            for required in ("Company_List", "Company_Without_TPM", "JD_Tracker",
                             "Match_Results"):
                wb.create_sheet(required)
            wb.save(path)
            wb.close()

            get_or_create_excel(path)

            wb = openpyxl.load_workbook(path)
            ws = wb["Tailored_Match_Results"]
            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
            for new_col in ("Original ATS", "Tailored ATS", "ATS Delta",
                            "Original Recruiter", "Tailored Recruiter", "Recruiter Delta",
                            "Original HM", "Tailored HM", "HM Delta"):
                self.assertIn(new_col, headers)
            # Legacy columns and values untouched.
            self.assertEqual(ws.cell(2, 5).value, 60)   # Original Score
            self.assertEqual(ws.cell(2, 6).value, 80)   # Tailored Score
            self.assertEqual(ws.cell(2, 7).value, 20)   # Score Delta
            self.assertEqual(ws.cell(2, 12).value, False)  # Regression
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_full_migration_pre_p0_12_to_latest(self):
        """End-to-end: pre-P0-12 schema (no Regression, no dim cols) → latest.

        This verifies that BOTH the P0-12 Regression migration and the
        PRJ-002 dim-column migration run successfully on a single
        outdated file.
        """
        path = _tmp_xlsx()
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Tailored_Match_Results"
            ws.append([
                "Resume ID", "JD URL", "Job Title", "Company", "Original Score",
                "Tailored Score", "Score Delta", "Tailored Resume Path",
                "Optimization Summary", "Updated At", "Resume Hash",
            ])
            ws.append(["r1", "https://a.com/1", "TPM", "Co", 80, 70, -10,
                       "p", "s", "2026-01-01", "h"])
            for required in ("Company_List", "Company_Without_TPM", "JD_Tracker",
                             "Match_Results"):
                wb.create_sheet(required)
            wb.save(path)
            wb.close()

            get_or_create_excel(path)

            wb = openpyxl.load_workbook(path)
            ws = wb["Tailored_Match_Results"]
            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
            # All target columns now present
            for col in ("Regression", "Original ATS", "HM Delta"):
                self.assertIn(col, headers)
            # Regression backfilled from delta < 0
            reg_col = headers.index("Regression") + 1
            self.assertTrue(ws.cell(2, reg_col).value)
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestTailoredWorkbookClose(unittest.TestCase):
    """Ensure all tailored functions properly close workbook handles."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _assert_close_called(self, fn, *args):
        import openpyxl as _openpyxl
        closed = []
        original_load = _openpyxl.load_workbook

        def spy_load(*a, **kw):
            wb = original_load(*a, **kw)
            orig_close = wb.close
            def tracked_close():
                closed.append(True)
                orig_close()
            wb.close = tracked_close
            return wb

        with unittest.mock.patch("shared.excel_store.load_workbook", side_effect=spy_load):
            fn(*args)

        self.assertTrue(
            len(closed) > 0,
            f"{fn.__name__} did not call wb.close() — file handle leak",
        )

    def test_get_scored_matches_closes(self):
        self._assert_close_called(get_scored_matches, self.path)

    def test_get_tailored_match_pairs_closes(self):
        self._assert_close_called(get_tailored_match_pairs, self.path)

    def test_batch_upsert_tailored_records_closes(self):
        rec = {
            "resume_id": "r1", "jd_url": "https://a.com/1",
            "job_title": "TPM", "company": "Co",
            "original_score": 60, "tailored_score": 80, "score_delta": 20,
            "tailored_resume_path": "p", "optimization_summary": "s",
            "resume_hash": "h",
        }
        self._assert_close_called(batch_upsert_tailored_records, self.path, [rec])


class TestDataQualityColumn(unittest.TestCase):
    """REQ-060: Data Quality column in JD_Tracker sheet."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_jd_headers_include_data_quality(self):
        self.assertIn("Data Quality", JD_HEADERS)

    def test_new_excel_has_data_quality_header(self):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        self.assertIn("Data Quality", headers)
        wb.close()

    def test_upsert_jd_record_writes_data_quality(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "QCo", "location": "Remote",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
            "data_quality": "complete",
        })
        upsert_jd_record(self.path, "https://qco.com/1", jd_json, "h")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        dq_col = headers.index("Data Quality") + 1
        self.assertEqual(ws.cell(2, dq_col).value, "complete")
        wb.close()

    def test_upsert_jd_record_writes_partial_quality(self):
        jd_json = json.dumps({
            "job_title": "TPM", "company": "QCo", "location": "",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
            "data_quality": "partial",
        })
        upsert_jd_record(self.path, "https://qco.com/2", jd_json, "h")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        dq_col = headers.index("Data Quality") + 1
        self.assertEqual(ws.cell(2, dq_col).value, "partial")
        wb.close()

    def test_batch_upsert_jd_records_writes_data_quality(self):
        records = [
            ("https://qco.com/3", json.dumps({
                "job_title": "TPM", "company": "QCo", "location": "NYC",
                "salary_range": "N/A", "requirements": ["ML"],
                "additional_qualifications": [],
                "key_responsibilities": ["Ship"], "is_ai_tpm": True,
                "data_quality": "complete",
            }), "h1"),
            ("https://qco.com/4", json.dumps({
                "job_title": "PM", "company": "QCo", "location": "",
                "salary_range": "N/A", "requirements": [],
                "additional_qualifications": [],
                "key_responsibilities": [], "is_ai_tpm": False,
                "data_quality": "failed",
            }), "h2"),
        ]
        batch_upsert_jd_records(self.path, records)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        dq_col = headers.index("Data Quality") + 1
        self.assertEqual(ws.cell(2, dq_col).value, "complete")
        self.assertEqual(ws.cell(3, dq_col).value, "failed")
        wb.close()

    def test_upsert_bad_json_writes_failed_quality(self):
        upsert_jd_record(self.path, "https://bad.com/1", "NOT JSON", "h")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        dq_col = headers.index("Data Quality") + 1
        self.assertEqual(ws.cell(2, dq_col).value, "failed")
        wb.close()

    def test_batch_upsert_bad_json_writes_failed_quality(self):
        records = [("https://bad.com/2", "NOT JSON", "h")]
        batch_upsert_jd_records(self.path, records)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        dq_col = headers.index("Data Quality") + 1
        self.assertEqual(ws.cell(2, dq_col).value, "failed")
        wb.close()

    def test_legacy_migration_includes_data_quality_column(self):
        """PRJ-004: the wholesale legacy-header rewrite carries Data Quality —
        an empty legacy sheet (pre-Data-Quality era, with 'Is AI TPM') ends up
        with the full new header set."""
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        # Simulate an old-era header row (no Data Quality, legacy Is AI TPM).
        for c in range(ws.max_column, 0, -1):
            ws.delete_cols(c)
        for i, h in enumerate(["JD URL", "Job Title", "Company", "Location",
                               "Salary", "Requirements",
                               "Additional Qualifications", "Responsibilities",
                               "Is AI TPM", "Updated At", "MD Hash"], 1):
            ws.cell(1, i).value = h
        wb.save(self.path)
        wb.close()
        # Run migration
        get_or_create_excel(self.path)
        wb2 = openpyxl.load_workbook(self.path)
        ws2 = wb2["JD_Tracker"]
        headers2 = [ws2.cell(1, c).value for c in range(1, ws2.max_column + 1)]
        self.assertIn("Data Quality", headers2)
        self.assertNotIn("Is AI TPM", headers2)
        wb2.close()

    def test_data_quality_empty_when_not_provided(self):
        """If jd_json has no data_quality key, column should be empty/None."""
        jd_json = json.dumps({
            "job_title": "TPM", "company": "OldCo", "location": "Remote",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, "https://oldco.com/1", jd_json, "h")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        dq_col = headers.index("Data Quality") + 1
        # openpyxl returns None for empty cells; "" is written but read back as None
        self.assertIn(ws.cell(2, dq_col).value, (None, ""))
        wb.close()


class TestAutoArchive(unittest.TestCase):
    """REQ-063: Auto-archive companies with no TPM jobs."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)
        # Seed two companies
        upsert_companies(self.path, [
            {"company_name": "ArchiveCo", "ai_domain": "LLM",
             "business_focus": "AI", "career_url": "https://archive.co/careers"},
            {"company_name": "ActiveCo", "ai_domain": "Vision",
             "business_focus": "AI", "career_url": "https://active.co/careers"},
        ])

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_company_headers_include_archive_columns(self):
        self.assertIn("No TPM Count", COMPANY_HEADERS)
        self.assertIn("Auto Archived", COMPANY_HEADERS)

    def test_new_excel_has_archive_headers(self):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        self.assertIn("No TPM Count", headers)
        self.assertIn("Auto Archived", headers)
        wb.close()

    def test_get_archived_companies_empty_initially(self):
        result = get_archived_companies(self.path)
        self.assertEqual(result, set())

    def test_get_archived_companies_returns_archived(self):
        update_archive_status(self.path, "ArchiveCo", 3, "yes")
        result = get_archived_companies(self.path)
        self.assertEqual(result, {"ArchiveCo"})

    def test_get_archived_companies_excludes_non_archived(self):
        update_archive_status(self.path, "ArchiveCo", 3, "yes")
        update_archive_status(self.path, "ActiveCo", 1, "no")
        result = get_archived_companies(self.path)
        self.assertIn("ArchiveCo", result)
        self.assertNotIn("ActiveCo", result)

    def test_update_archive_status_writes_count_and_flag(self):
        update_archive_status(self.path, "ArchiveCo", 2, "no")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        cnt_col = headers["No TPM Count"]
        arch_col = headers["Auto Archived"]
        for r in range(2, ws.max_row + 1):
            if str(ws.cell(r, 1).value).strip() == "ArchiveCo":
                self.assertEqual(ws.cell(r, cnt_col).value, 2)
                self.assertEqual(ws.cell(r, arch_col).value, "no")
                break
        else:
            self.fail("ArchiveCo not found in sheet")
        wb.close()

    def test_update_archive_status_sets_yes(self):
        update_archive_status(self.path, "ArchiveCo", 3, "yes")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        arch_col = headers["Auto Archived"]
        for r in range(2, ws.max_row + 1):
            if str(ws.cell(r, 1).value).strip() == "ArchiveCo":
                self.assertEqual(ws.cell(r, arch_col).value, "yes")
                break
        wb.close()

    def test_unarchive_company_resets(self):
        update_archive_status(self.path, "ArchiveCo", 3, "yes")
        self.assertIn("ArchiveCo", get_archived_companies(self.path))
        unarchive_company(self.path, "ArchiveCo")
        self.assertNotIn("ArchiveCo", get_archived_companies(self.path))
        info = get_company_archive_info(self.path)
        self.assertEqual(info["ArchiveCo"]["no_tpm_count"], 0)
        self.assertEqual(info["ArchiveCo"]["archived"], "no")

    def test_get_company_archive_info_returns_all(self):
        update_archive_status(self.path, "ArchiveCo", 3, "yes")
        update_archive_status(self.path, "ActiveCo", 1, "no")
        info = get_company_archive_info(self.path)
        self.assertEqual(info["ArchiveCo"]["no_tpm_count"], 3)
        self.assertEqual(info["ArchiveCo"]["archived"], "yes")
        self.assertEqual(info["ActiveCo"]["no_tpm_count"], 1)
        self.assertEqual(info["ActiveCo"]["archived"], "no")

    def test_migration_adds_archive_columns(self):
        """Old Excel without archive columns gets them via migration."""
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        # Remove archive columns to simulate old file
        for col_name in ("No TPM Count", "Auto Archived"):
            if col_name in headers:
                idx = headers.index(col_name) + 1
                ws.delete_cols(idx)
                headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        wb.save(self.path)
        wb.close()
        # Run migration
        get_or_create_excel(self.path)
        wb2 = openpyxl.load_workbook(self.path)
        ws2 = wb2["Company_List"]
        headers2 = [ws2.cell(1, c).value for c in range(1, ws2.max_column + 1)]
        self.assertIn("No TPM Count", headers2)
        self.assertIn("Auto Archived", headers2)
        wb2.close()

    def test_update_nonexistent_company_no_error(self):
        """Updating a company that doesn't exist should not raise."""
        update_archive_status(self.path, "GhostCo", 5, "yes")
        # GhostCo not in sheet, so nothing should change
        result = get_archived_companies(self.path)
        self.assertNotIn("GhostCo", result)


class TestCountValidTpmJobsByCompany(unittest.TestCase):
    """REQ-063: count_valid_tpm_jobs_by_company excludes data_quality='failed'."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_empty_returns_empty(self):
        result = count_valid_tpm_jobs_by_company(self.path)
        self.assertEqual(result, {})

    def test_counts_non_failed_records(self):
        jd_ok = json.dumps({
            "job_title": "TPM", "company": "TestCo", "location": "Remote",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
            "data_quality": "complete",
        })
        upsert_jd_record(self.path, "https://test.co/1", jd_ok, "h1")
        result = count_valid_tpm_jobs_by_company(self.path)
        self.assertEqual(result.get("TestCo"), 1)

    def test_excludes_failed_records(self):
        jd_ok = json.dumps({
            "job_title": "TPM", "company": "TestCo", "location": "Remote",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
            "data_quality": "complete",
        })
        jd_fail = json.dumps({
            "job_title": "TPM", "company": "TestCo", "location": "",
            "salary_range": "N/A", "requirements": [],
            "additional_qualifications": [],
            "key_responsibilities": [], "is_ai_tpm": False,
            "data_quality": "failed",
        })
        upsert_jd_record(self.path, "https://test.co/1", jd_ok, "h1")
        upsert_jd_record(self.path, "https://test.co/2", jd_fail, "h2")
        result = count_valid_tpm_jobs_by_company(self.path)
        self.assertEqual(result.get("TestCo"), 1)

    def test_company_only_failed_not_counted(self):
        jd_fail = json.dumps({
            "job_title": "TPM", "company": "FailCo", "location": "",
            "salary_range": "N/A", "requirements": [],
            "additional_qualifications": [],
            "key_responsibilities": [], "is_ai_tpm": False,
            "data_quality": "failed",
        })
        upsert_jd_record(self.path, "https://fail.co/1", jd_fail, "h1")
        result = count_valid_tpm_jobs_by_company(self.path)
        self.assertNotIn("FailCo", result)

    def test_partial_quality_counted(self):
        jd_partial = json.dumps({
            "job_title": "TPM", "company": "PartCo", "location": "",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": [],
            "key_responsibilities": ["Lead"], "is_ai_tpm": True,
            "data_quality": "partial",
        })
        upsert_jd_record(self.path, "https://part.co/1", jd_partial, "h1")
        result = count_valid_tpm_jobs_by_company(self.path)
        self.assertEqual(result.get("PartCo"), 1)


# ─────────────────────────────────────────────────────────────────────────────
class TestBug33NullListFields(unittest.TestCase):
    """BUG-33: d.get('requirements', []) returns None when Gemini returns null,
    causing TypeError in '\\n'.join(). Must handle null gracefully."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_requirements_null_no_crash(self):
        """When requirements is explicitly null, upsert must not raise TypeError."""
        jd_json = json.dumps({
            "job_title": "TPM", "company": "NullCo", "location": "SF, CA",
            "salary_range": "N/A", "requirements": None,
            "additional_qualifications": ["Nice to have"],
            "key_responsibilities": ["Lead projects"], "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, "https://null.co/1", jd_json, "h1")
        ws = openpyxl.load_workbook(self.path)["JD_Tracker"]
        self.assertEqual(ws.cell(2, 6).value, "None")  # requirements → "None"
        ws.parent.close()

    def test_additional_qualifications_null_no_crash(self):
        """When additional_qualifications is null, upsert must not raise TypeError."""
        jd_json = json.dumps({
            "job_title": "TPM", "company": "NullCo2", "location": "NYC",
            "salary_range": "N/A", "requirements": ["Python"],
            "additional_qualifications": None,
            "key_responsibilities": ["Ship"], "is_ai_tpm": False,
        })
        upsert_jd_record(self.path, "https://null.co/2", jd_json, "h2")
        ws = openpyxl.load_workbook(self.path)["JD_Tracker"]
        self.assertEqual(ws.cell(2, 7).value, "None")  # addq → "None"
        ws.parent.close()

    def test_key_responsibilities_null_no_crash(self):
        """When key_responsibilities is null, upsert must not raise TypeError."""
        jd_json = json.dumps({
            "job_title": "TPM", "company": "NullCo3", "location": "LA",
            "salary_range": "N/A", "requirements": ["Go"],
            "additional_qualifications": ["K8s"],
            "key_responsibilities": None, "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, "https://null.co/3", jd_json, "h3")
        ws = openpyxl.load_workbook(self.path)["JD_Tracker"]
        self.assertEqual(ws.cell(2, 8).value, "None")  # resp → "None"
        ws.parent.close()

    def test_all_three_null_no_crash(self):
        """When all three list fields are null, upsert must not raise TypeError."""
        jd_json = json.dumps({
            "job_title": "TPM", "company": "AllNull", "location": "Remote",
            "salary_range": "N/A", "requirements": None,
            "additional_qualifications": None,
            "key_responsibilities": None, "is_ai_tpm": False,
        })
        upsert_jd_record(self.path, "https://null.co/all", jd_json, "hall")
        ws = openpyxl.load_workbook(self.path)["JD_Tracker"]
        self.assertEqual(ws.cell(2, 6).value, "None")
        self.assertEqual(ws.cell(2, 7).value, "None")
        self.assertEqual(ws.cell(2, 8).value, "None")
        ws.parent.close()

    def test_missing_keys_still_work(self):
        """When keys are completely absent (not null), should still work."""
        jd_json = json.dumps({
            "job_title": "TPM", "company": "MissCo", "location": "Austin",
            "salary_range": "N/A", "is_ai_tpm": True,
        })
        upsert_jd_record(self.path, "https://miss.co/1", jd_json, "hm")
        ws = openpyxl.load_workbook(self.path)["JD_Tracker"]
        self.assertEqual(ws.cell(2, 6).value, "None")
        self.assertEqual(ws.cell(2, 7).value, "None")
        self.assertEqual(ws.cell(2, 8).value, "None")
        ws.parent.close()


# ─────────────────────────────────────────────────────────────────────────────
class TestBug40OperatorPrecedence(unittest.TestCase):
    """BUG-40: Operator precedence in get_match_pairs/get_scored_matches must be correct."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_parenthesized_correctly_in_source(self):
        """Verify the expressions use explicit parentheses for correct precedence."""
        import inspect
        from shared.excel_store import get_match_pairs, get_scored_matches
        for fn in (get_match_pairs, get_scored_matches):
            source = inspect.getsource(fn)
            # Should NOT have the ambiguous pattern without parens
            self.assertNotIn('.value or "" if ws.max_column', source,
                             f"{fn.__name__} has ambiguous operator precedence")
            self.assertNotIn(".value or \"fine\" if ws.max_column", source,
                             f"{fn.__name__} has ambiguous operator precedence")

    def test_match_pairs_hash_and_stage_with_values(self):
        """Verify hash and stage are correctly read when values exist."""
        upsert_match_record(self.path, "r1", "https://j.co/1",
                            '{"score":85}', "hashABC", stage="coarse")
        pairs = get_match_pairs(self.path)
        key = ("r1", "https://j.co/1")
        self.assertIn(key, pairs)
        self.assertEqual(pairs[key]["hash"], "hashABC")
        self.assertEqual(pairs[key]["stage"], "coarse")


# ─────────────────────────────────────────────────────────────────────────────
class TestBug45UpsertCompaniesInitAllColumns(unittest.TestCase):
    """BUG-45: upsert_companies must initialize all 9 COMPANY_HEADERS columns."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_new_company_has_all_columns(self):
        upsert_companies(self.path, [{"company_name": "TestCo", "ai_domain": "NLP"}])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        # Row 2 should have 9 columns
        vals = [ws.cell(2, c).value for c in range(1, 10)]
        wb.close()
        self.assertEqual(vals[0], "TestCo")  # Company Name
        self.assertEqual(vals[5], 0)  # TPM Jobs
        self.assertEqual(vals[6], 0)  # AI TPM Jobs
        self.assertEqual(vals[7], 0)  # No TPM Count
        self.assertEqual(vals[8], "No")  # Auto Archived


class TestBug46DocstringColumnNumber(unittest.TestCase):
    """BUG-46: batch_update_jd_timestamps docstring must not reference wrong column number."""

    def test_docstring_no_wrong_column(self):
        # After BUG-52 fix, docstring no longer references hardcoded column numbers at all
        self.assertNotIn("col 9", batch_update_jd_timestamps.__doc__)
        self.assertIn("Updated At", batch_update_jd_timestamps.__doc__)


class TestBug55WithoutTpmHeadersMigration(unittest.TestCase):
    """BUG-55 (updated for PRJ-004): WITHOUT_TPM_HEADERS must include TPM Jobs
    and Qualified Jobs; legacy 'AI Domain'/'AI TPM Jobs' headers migrate in place."""

    def setUp(self):
        self.path = _tmp_xlsx()

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_headers_include_tpm_columns(self):
        from shared.excel_store import WITHOUT_TPM_HEADERS
        self.assertIn("TPM Jobs", WITHOUT_TPM_HEADERS)
        self.assertIn("Qualified Jobs", WITHOUT_TPM_HEADERS)

    def test_new_excel_has_correct_without_tpm_headers(self):
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_Without_TPM"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        wb.close()
        self.assertIn("TPM Jobs", headers)
        self.assertIn("Qualified Jobs", headers)

    def test_migration_adds_columns_to_existing(self):
        # Create old-style file with only 5 columns (pre-PRJ-004 header names)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Company_Without_TPM"
        ws.append(["Company Name", "AI Domain", "Business Focus", "Career URL", "Updated At"])
        wb.save(self.path)
        wb.close()
        # Run migration
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_Without_TPM"]
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        wb.close()
        self.assertIn("TPM Jobs", headers)
        self.assertIn("Qualified Jobs", headers)
        # PRJ-004: legacy 'AI Domain' header renamed in place
        self.assertIn("Track", headers)
        self.assertNotIn("AI Domain", headers)


class TestBug52JdTrackerDynamicColumns(unittest.TestCase):
    """BUG-52: JD_Tracker read functions must use _JD_COL for column lookup, not hardcoded numbers."""

    def test_jd_col_mapping_matches_headers(self):
        """_JD_COL must map every JD_HEADERS entry to correct 1-based index."""
        for i, h in enumerate(JD_HEADERS):
            self.assertEqual(_JD_COL[h], i + 1, f"_JD_COL['{h}'] should be {i + 1}")

    def test_no_hardcoded_column_numbers_in_jd_functions(self):
        """JD_Tracker read functions must not contain hardcoded ws.cell(r, <number>) patterns."""
        import inspect
        funcs = [get_jd_urls, get_jd_url_meta, batch_update_jd_timestamps,
                 get_jd_rows_for_match, get_incomplete_jd_rows,
                 count_tpm_jobs_by_company, count_valid_tpm_jobs_by_company]
        import re
        pattern = re.compile(r'ws\.cell\(r,\s*\d+\)')
        for fn in funcs:
            source = inspect.getsource(fn)
            matches = pattern.findall(source)
            self.assertEqual(matches, [],
                             f"{fn.__name__} still has hardcoded column numbers: {matches}")

    def test_get_jd_urls_reads_correct_columns(self):
        """End-to-end: get_jd_urls reads URL and Company from correct columns."""
        path = _tmp_xlsx()
        try:
            get_or_create_excel(path)
            wb = openpyxl.load_workbook(path)
            ws = wb["JD_Tracker"]
            # Write a valid row
            row = [None] * len(JD_HEADERS)
            row[JD_HEADERS.index("JD URL")] = "https://example.com/job/1"
            row[JD_HEADERS.index("Company")] = "TestCorp"
            ws.append(row)
            wb.save(path)
            wb.close()
            urls = get_jd_urls(path)
            self.assertIn("https://example.com/job/1", urls)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_count_tpm_jobs_reads_correct_columns(self):
        """End-to-end: count_tpm_jobs_by_company reads Company and Job Domain correctly."""
        path = _tmp_xlsx()
        try:
            get_or_create_excel(path)
            wb = openpyxl.load_workbook(path)
            ws = wb["JD_Tracker"]
            row = [None] * len(JD_HEADERS)
            row[JD_HEADERS.index("JD URL")] = "https://example.com/job/1"
            row[JD_HEADERS.index("Company")] = "AcmeCorp"
            row[JD_HEADERS.index("Job Domain")] = "Robotics"
            ws.append(row)
            row2 = [None] * len(JD_HEADERS)
            row2[JD_HEADERS.index("JD URL")] = "https://example.com/job/2"
            row2[JD_HEADERS.index("Company")] = "AcmeCorp"
            row2[JD_HEADERS.index("Job Domain")] = ""
            ws.append(row2)
            wb.save(path)
            wb.close()
            counts = count_tpm_jobs_by_company(path)
            self.assertEqual(counts["AcmeCorp"]["tpm"], 2)
            self.assertEqual(counts["AcmeCorp"]["qualified"], 1)
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestClassifyLocation(unittest.TestCase):
    """Unit tests for classify_location — pure function, no I/O."""

    def test_empty_or_missing(self):
        self.assertEqual(classify_location(""), "Other")
        self.assertEqual(classify_location(None), "Other")
        self.assertEqual(classify_location("N/A"), "Other")
        self.assertEqual(classify_location("None"), "Other")

    def test_seattle_single(self):
        self.assertEqual(classify_location("Seattle, WA"), "Greater Seattle")
        self.assertEqual(classify_location("Bellevue, WA"), "Greater Seattle")
        self.assertEqual(classify_location("Redmond, WA - US"), "Greater Seattle")
        self.assertEqual(classify_location("kirkland, wa"), "Greater Seattle")  # case-insensitive

    def test_seattle_multi_segment(self):
        self.assertEqual(
            classify_location("San Francisco, CA; Seattle, WA"),
            "Greater Seattle",
        )
        self.assertEqual(
            classify_location("Bellevue, WA; Sunnyvale, CA"),
            "Greater Seattle",
        )
        self.assertEqual(
            classify_location("San Francisco, CA - US; Bellevue, WA - US; Sunnyvale, CA - US"),
            "Greater Seattle",
        )

    def test_seattle_priority_over_remote(self):
        # Seattle wins when both present.
        self.assertEqual(
            classify_location("Seattle, WA; Remote"),
            "Greater Seattle",
        )

    def test_remote_us_variants(self):
        # Bare "Remote" (US-default) and explicit US qualifiers all qualify.
        self.assertEqual(classify_location("Remote"), "Remote")
        self.assertEqual(classify_location("Remote, US"), "Remote")
        self.assertEqual(classify_location("Remote, USA"), "Remote")
        self.assertEqual(classify_location("Remote, United States"), "Remote")
        self.assertEqual(classify_location("Remote, U.S."), "Remote")
        self.assertEqual(classify_location("Remote, U.S.A."), "Remote")
        self.assertEqual(classify_location("San Francisco, CA; New York, NY; Remote"), "Remote")

    def test_remote_non_us_excluded(self):
        # Explicit non-US country qualifiers do NOT qualify as Remote.
        self.assertEqual(classify_location("Remote, Canada"), "Other")
        self.assertEqual(classify_location("Remote, UK"), "Other")
        self.assertEqual(classify_location("Remote, Germany"), "Other")

    def test_washington_state_forms(self):
        # Whitelisted Washington-state forms qualify as Greater Seattle.
        self.assertEqual(classify_location("Washington, US"), "Greater Seattle")
        self.assertEqual(classify_location("Washington, USA"), "Greater Seattle")
        self.assertEqual(classify_location("Washington, United States"), "Greater Seattle")
        self.assertEqual(classify_location("Washington State"), "Greater Seattle")
        self.assertEqual(classify_location("washington, u.s."), "Greater Seattle")  # case-insensitive

    def test_washington_dc_still_other(self):
        # D.C. variants must remain Other — guards against accidental capture.
        self.assertEqual(classify_location("Washington, D.C."), "Other")
        self.assertEqual(classify_location("Washington, DC"), "Other")
        self.assertEqual(classify_location("Washington, District of Columbia"), "Other")
        # Bare "Washington" is intentionally ambiguous → Other (whitelist-only).
        self.assertEqual(classify_location("Washington"), "Other")

    def test_other(self):
        self.assertEqual(classify_location("San Francisco, CA"), "Other")
        self.assertEqual(classify_location("Mountain View, California"), "Other")
        self.assertEqual(classify_location("United Kingdom"), "Other")

    def test_kent_oh_not_seattle(self):
        # Guards against false positive: city name 'Kent' without WA must not match.
        self.assertEqual(classify_location("Kent, OH"), "Other")

    def test_sample_real_locations(self):
        # Sampled from production JD_Tracker data.
        self.assertEqual(
            classify_location("Livingston, NJ; New York, NY; Sunnyvale, CA; Bellevue, WA"),
            "Greater Seattle",
        )
        self.assertEqual(
            classify_location(
                "Huntsville, AL; Oklahoma City, OK; Mechanicsburg, PA; Ogden, UT; "
                "San Antonio, TX; Fort Meade, MD; Columbus, OH; Remote"
            ),
            "Remote",
        )


class TestSortJdTrackerByTier(unittest.TestCase):
    """PRJ-004 REQ-004-15: sort_jd_tracker_by_tier recomputes Freshness Tier
    from Posted Date, writes the combined 1-6 Sort Tier (9 = unknown/aged/
    other-region), sorts rows, applies fills, preserves row count, and is
    idempotent."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    @staticmethod
    def _days_ago(n: int) -> str:
        return (datetime.now().date() - timedelta(days=n)).strftime("%Y-%m-%d")

    def _add_jd(self, url: str, location: str, posted_days_ago: int | None,
                updated_at: str = "2026-05-01 10:00:00", company: str = "Acme"):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        row = [None] * len(JD_HEADERS)
        row[JD_HEADERS.index("JD URL")]     = url
        row[JD_HEADERS.index("Company")]    = company
        row[JD_HEADERS.index("Location")]   = location
        row[JD_HEADERS.index("Updated At")] = updated_at
        if posted_days_ago is not None:
            row[JD_HEADERS.index("Posted Date")] = self._days_ago(posted_days_ago)
        ws.append(row)
        wb.save(self.path)
        wb.close()

    def _ordered(self):
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        url_col  = JD_HEADERS.index("JD URL") + 1
        tier_col = JD_HEADERS.index("Sort Tier") + 1
        out = [(ws.cell(r, url_col).value, ws.cell(r, tier_col).value)
               for r in range(2, ws.max_row + 1) if ws.cell(r, url_col).value]
        wb.close()
        return out

    def test_sort_order_combined_tiers(self):
        # One row per tier cell, inserted out of order on purpose.
        self._add_jd("u_t4", "Austin, TX",         posted_days_ago=5)     # T2 x CA/TX   -> 4
        self._add_jd("u_t1", "Seattle, WA",        posted_days_ago=1)     # T1 x Sea/Rem -> 1
        self._add_jd("u_t6", "San Francisco, CA",  posted_days_ago=10)    # T3 x CA/TX   -> 6
        self._add_jd("u_t3", "Remote",             posted_days_ago=5)     # T2 x Sea/Rem -> 3
        self._add_jd("u_t9", "Bellevue, WA",       posted_days_ago=None)  # unknown date -> 9
        self._add_jd("u_t2", "Irvine, CA",         posted_days_ago=1)     # T1 x CA/TX   -> 2
        self._add_jd("u_t5", "Redmond, WA",        posted_days_ago=10)    # T3 x Sea/Rem -> 5

        n = sort_jd_tracker_by_tier(self.path)
        self.assertEqual(n, 7)
        self.assertEqual(self._ordered(), [
            ("u_t1", 1), ("u_t2", 2), ("u_t3", 3), ("u_t4", 4),
            ("u_t5", 5), ("u_t6", 6), ("u_t9", 9),
        ])

    def test_freshness_recomputed_from_posted_date(self):
        """A stale scrape-time Freshness Tier is overwritten at sort time."""
        self._add_jd("u1", "Seattle, WA", posted_days_ago=10)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        ws.cell(2, JD_HEADERS.index("Freshness Tier") + 1).value = 1  # stale
        wb.save(self.path)
        wb.close()

        sort_jd_tracker_by_tier(self.path)

        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        self.assertEqual(ws.cell(2, JD_HEADERS.index("Freshness Tier") + 1).value, 3)
        self.assertEqual(ws.cell(2, JD_HEADERS.index("Sort Tier") + 1).value, 5)
        wb.close()

    def test_aged_row_sinks_but_is_never_deleted(self):
        """REQ-004-16/G7: a grandfathered row older than 14 days gets tier 9
        and sinks — it is never removed by the sorter."""
        self._add_jd("u_aged",  "Seattle, WA", posted_days_ago=30)
        self._add_jd("u_fresh", "Seattle, WA", posted_days_ago=1)
        n = sort_jd_tracker_by_tier(self.path)
        self.assertEqual(n, 2)
        self.assertEqual(self._ordered(), [("u_fresh", 1), ("u_aged", 9)])

    def test_fill_colors_applied(self):
        self._add_jd("u1", "Seattle, WA",       posted_days_ago=1)     # tier 1 green
        self._add_jd("u2", "San Francisco, CA", posted_days_ago=5)     # tier 4 yellow
        self._add_jd("u3", "Bellevue, WA",      posted_days_ago=None)  # tier 9 no fill

        sort_jd_tracker_by_tier(self.path)

        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        self.assertIn("C6EFCE", str(ws.cell(2, 1).fill.start_color.rgb or ""))
        self.assertIn("FFEB9C", str(ws.cell(3, 1).fill.start_color.rgb or ""))
        row4_fill_type = ws.cell(4, 1).fill.fill_type
        self.assertIn(row4_fill_type, (None, "none"))
        wb.close()

    def test_idempotent(self):
        self._add_jd("u1", "Seattle, WA",       posted_days_ago=1)
        self._add_jd("u2", "Remote",            posted_days_ago=5)
        self._add_jd("u3", "San Francisco, CA", posted_days_ago=10)

        sort_jd_tracker_by_tier(self.path)
        sort_jd_tracker_by_tier(self.path)  # second call must not change order

        self.assertEqual([u for u, _ in self._ordered()], ["u1", "u2", "u3"])

    def test_empty_sheet(self):
        # No data rows: sort returns 0 and does not crash.
        n = sort_jd_tracker_by_tier(self.path)
        self.assertEqual(n, 0)

    def test_posted_date_desc_within_tier(self):
        self._add_jd("u_older", "Seattle, WA",  posted_days_ago=2,
                     updated_at="2026-05-09 10:00:00")
        self._add_jd("u_newer", "Bellevue, WA", posted_days_ago=1,
                     updated_at="2026-05-01 10:00:00")

        sort_jd_tracker_by_tier(self.path)
        # Both tier 1; newer Posted Date first even though Updated At is older.
        self.assertEqual([u for u, _ in self._ordered()], ["u_newer", "u_older"])


class TestComputeFreshnessTier(unittest.TestCase):
    """Pure-function boundary tests (design section 7: 2/3, 7/8, 14/15 pinned)."""

    TODAY = date(2026, 7, 7)

    def _tier(self, days_ago: int):
        from shared.excel_store import compute_freshness_tier
        posted = (self.TODAY - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        return compute_freshness_tier(posted, today=self.TODAY)

    def test_band_boundaries(self):
        self.assertEqual(self._tier(0), 1)
        self.assertEqual(self._tier(2), 1)
        self.assertEqual(self._tier(3), 2)
        self.assertEqual(self._tier(7), 2)
        self.assertEqual(self._tier(8), 3)
        self.assertEqual(self._tier(14), 3)   # day-14 keeps: last tierable day
        self.assertIsNone(self._tier(15))     # pinned boundary: >=15 untierable

    def test_blank_and_unparsable(self):
        from shared.excel_store import compute_freshness_tier
        self.assertIsNone(compute_freshness_tier("", today=self.TODAY))
        self.assertIsNone(compute_freshness_tier("Posted 3 Days Ago", today=self.TODAY))

    def test_future_date_treated_as_today(self):
        self.assertEqual(self._tier(-1), 1)


class TestClassifyRegion(unittest.TestCase):
    """Pure-function matrix for classify_region (REQ-004-12)."""

    def _r(self, loc):
        from shared.excel_store import classify_region
        return classify_region(loc)

    def test_wa_state(self):
        self.assertEqual(self._r("Seattle, WA"), "WA")
        self.assertEqual(self._r("Bellevue, WA"), "WA")
        # 2026-08-19 widening: whole WA state, not just Seattle metro
        self.assertEqual(self._r("Spokane, WA"), "WA")
        self.assertEqual(self._r("Vancouver, WA"), "WA")
        self.assertEqual(self._r("Richland, WA, USA"), "WA")
        self.assertEqual(self._r("Washington State"), "WA")
        self.assertEqual(self._r("Washington, USA"), "WA")

    def test_wa_no_false_positives(self):
        # Bare "Washington" stays ambiguous (D.C. collision) → Other
        self.assertEqual(self._r("Washington"), "Other")
        self.assertEqual(self._r("Washington, DC"), "Other")
        self.assertEqual(self._r("Washington, D.C."), "Other")
        # ", wa" substring must be a state token, not part of a word
        self.assertEqual(self._r("Cardiff, Wales"), "Other")

    def test_remote(self):
        self.assertEqual(self._r("Remote"), "Remote")
        self.assertEqual(self._r("Remote, USA"), "Remote")
        self.assertEqual(self._r("Remote, Canada"), "Other")

    def test_california_incl_socal(self):
        self.assertEqual(self._r("San Francisco, CA"), "CA")
        self.assertEqual(self._r("El Segundo, CA"), "CA")
        self.assertEqual(self._r("Hawthorne, California"), "CA")

    def test_texas(self):
        self.assertEqual(self._r("Austin, TX"), "TX")
        self.assertEqual(self._r("Houston, Texas"), "TX")

    def test_florida(self):
        # REQ-162: FL recognized unconditionally; Space-only keep is enforced
        # by job_agent._geo_out_of_scope, not here.
        self.assertEqual(self._r("Cape Canaveral, FL"), "FL")
        self.assertEqual(self._r("Melbourne, FL"), "FL")
        self.assertEqual(self._r("Titusville, Florida"), "FL")
        # bare "melbourne" (no FL qualifier) is ambiguous (Australia) → Other
        self.assertEqual(self._r("Melbourne"), "Other")

    def test_other_us_dropped_regions(self):
        self.assertEqual(self._r("New York, NY"), "Other")
        self.assertEqual(self._r("Boston, MA"), "Other")

    def test_unknown(self):
        self.assertEqual(self._r(""), "Unknown")
        self.assertEqual(self._r(None), "Unknown")
        self.assertEqual(self._r("N/A"), "Unknown")

    def test_multi_segment_best_region_wins(self):
        self.assertEqual(self._r("San Francisco, CA; Seattle, WA"), "WA")
        self.assertEqual(self._r("Austin, TX; Remote"), "Remote")
        self.assertEqual(self._r("New York, NY; Austin, TX"), "TX")


class TestComputeSortTier(unittest.TestCase):
    """Full 6-cell matrix + tier-9 cases for compute_sort_tier (REQ-004-15)."""

    def _t(self, fresh, region):
        from shared.excel_store import compute_sort_tier
        return compute_sort_tier(fresh, region)

    def test_matrix(self):
        self.assertEqual(self._t(1, "WA"), 1)
        self.assertEqual(self._t(1, "Remote"), 1)
        self.assertEqual(self._t(1, "CA"), 2)
        self.assertEqual(self._t(1, "TX"), 2)
        self.assertEqual(self._t(2, "WA"), 3)
        self.assertEqual(self._t(2, "CA"), 4)
        self.assertEqual(self._t(3, "Remote"), 5)
        self.assertEqual(self._t(3, "TX"), 6)
        self.assertEqual(self._t(1, "FL"), 2)   # REQ-162: FL groups with CA/TX
        self.assertEqual(self._t(3, "FL"), 6)

    def test_tier_nine_cases(self):
        self.assertEqual(self._t(None, "WA"), 9)        # unknown/aged date
        self.assertEqual(self._t(1, "Other"), 9)        # non-target region
        self.assertEqual(self._t(1, "Unknown"), 9)
        self.assertEqual(self._t(None, "Other"), 9)



class TestFreshnessTierXorDateFlag(unittest.TestCase):
    """Intake §9.4 seed, literal form (Phase 4 QA P2): every written row has a
    Freshness Tier XOR a Date Flag — no row has neither."""

    def test_written_rows_have_tier_xor_flag(self):
        import tempfile, json
        fd, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        os.remove(path)
        try:
            get_or_create_excel(path)
            base = {"job_title": "TPM", "company": "Co", "location": "Seattle, WA",
                    "salary_range": "", "requirements": ["x"],
                    "additional_qualifications": [], "key_responsibilities": ["y"],
                    "job_domain": "AI"}
            recent = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
            rows = [
                ("https://a.co/dated",   {**base, "posted_date": recent}),
                ("https://a.co/unknown", {**base,
                                          "date_flag": "manual review — unknown posted date"}),
            ]
            batch_upsert_jd_records(path, [(u, json.dumps(d), "h") for u, d in rows])
            wb = openpyxl.load_workbook(path)
            ws = wb["JD_Tracker"]
            c_fresh = JD_HEADERS.index("Freshness Tier") + 1
            c_flag  = JD_HEADERS.index("Date Flag") + 1
            for r in range(2, ws.max_row + 1):
                has_tier = ws.cell(r, c_fresh).value in (1, 2, 3)
                has_flag = bool(ws.cell(r, c_flag).value)
                self.assertTrue(has_tier != has_flag,
                                f"row {r}: tier={ws.cell(r, c_fresh).value!r} "
                                f"flag={ws.cell(r, c_flag).value!r} — must be XOR")
            wb.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


# ─────────────────────────────────────────────────────────────────────────────
class TestGetIncompleteCompanyRows(unittest.TestCase):
    """BUG-69: detect Company_List rows with blank/N-A Business Focus."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)
        upsert_companies(self.path, [
            {"company_name": "FullCo", "ai_domain": "AI-native",
             "business_focus": "Builds AI tools.", "career_url": "https://f.co"},
            {"company_name": "BlankCo", "ai_domain": "AI-native",
             "business_focus": "", "career_url": "https://b.co"},
            {"company_name": "NaCo", "ai_domain": "Fintech",
             "business_focus": "N/A", "career_url": "https://n.co"},
        ])

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_blank_and_na_rows_returned_with_row_numbers(self):
        rows = get_incomplete_company_rows(self.path)
        by_name = {r["name"]: r for r in rows}
        self.assertNotIn("FullCo", by_name)
        self.assertIn("BlankCo", by_name)
        self.assertIn("NaCo", by_name)
        # excel_row aligns with the sheet (row 2 = FullCo, 3 = BlankCo, 4 = NaCo)
        self.assertEqual(by_name["BlankCo"]["excel_row"], 3)
        self.assertEqual(by_name["NaCo"]["excel_row"], 4)
        self.assertEqual(by_name["BlankCo"]["career_url"], "https://b.co")

    def test_all_complete_returns_empty(self):
        update_company_business_focus(self.path, 3, "Fills a gap.")
        update_company_business_focus(self.path, 4, "Also filled.")
        self.assertEqual(get_incomplete_company_rows(self.path), [])


class TestUpdateCompanyBusinessFocus(unittest.TestCase):
    """BUG-69: write Business Focus in place + refresh Updated At."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)
        upsert_companies(self.path, [
            {"company_name": "FocusCo", "ai_domain": "AI-native",
             "business_focus": "", "career_url": "https://f.co"},
        ])

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_updates_focus_and_timestamp(self):
        update_company_business_focus(self.path, 2, "Builds robots for ports.")
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        self.assertEqual(ws.cell(2, 3).value, "Builds robots for ports.")
        self.assertTrue(ws.cell(2, 5).value)  # Updated At refreshed
        wb.close()
        self.assertEqual(get_incomplete_company_rows(self.path), [])


# ─────────────────────────────────────────────────────────────────────────────
class TestGetTriagedJdUrls(unittest.TestCase):
    """BUG-65: URLs in user triage tabs are collected for permanent exclusion."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _append(self, sheet: str, url):
        wb = openpyxl.load_workbook(self.path)
        wb[sheet].append([url])
        wb.save(self.path)
        wb.close()

    def test_union_across_both_tabs(self):
        self._append("JD_ToApply", "https://a.co/jd/1")
        self._append("Skipped JD", "https://b.co/jd/2")
        self.assertEqual(get_triaged_jd_urls(self.path),
                         {"a.co/jd/1", "b.co/jd/2"})

    def test_empty_tabs_return_empty_set(self):
        self.assertEqual(get_triaged_jd_urls(self.path), set())

    def test_missing_tabs_tolerated(self):
        wb = openpyxl.load_workbook(self.path)
        for sheet in TRIAGE_SHEETS:
            del wb[sheet]
        wb.save(self.path)
        wb.close()
        self.assertEqual(get_triaged_jd_urls(self.path), set())

    def test_blank_and_whitespace_urls_skipped(self):
        self._append("JD_ToApply", "  ")
        self._append("Skipped JD", None)
        self._append("Skipped JD", "https://c.co/jd/3")
        self.assertEqual(get_triaged_jd_urls(self.path), {"c.co/jd/3"})

    def test_url_whitespace_stripped(self):
        self._append("JD_ToApply", "  https://d.co/jd/4 ")
        self.assertEqual(get_triaged_jd_urls(self.path), {"d.co/jd/4"})

    def test_returns_canonical_urls(self):
        """BUG-71: triaged set holds canonical forms — a tracking-param variant
        of a triaged URL must hit the set after canonicalization."""
        self._append("Skipped JD",
                     "https://www.linkedin.com/jobs/view/pm-at-uber-4407580818"
                     "?pagenum=0&position=40&refid=abc&trackingid=xyz")
        triaged = get_triaged_jd_urls(self.path)
        rediscovered = ("https://www.linkedin.com/jobs/view/pm-at-uber-4407580818"
                        "?pagenum=2&position=2&refid=def&trackingid=uvw")
        self.assertIn(canonical_jd_url(rediscovered), triaged)


# ─────────────────────────────────────────────────────────────────────────────
class TestCanonicalJdUrl(unittest.TestCase):
    """BUG-71: canonical_jd_url collapses cosmetic URL variants of the same
    posting while keeping genuinely different postings distinct. Cases mirror
    real duplicates observed in the 2026-07 triage tabs."""

    def test_linkedin_tracking_params_collapse(self):
        a = ("https://www.linkedin.com/jobs/view/program-manager-at-uber-4407580818"
             "?pagenum=0&position=40&refid=Yl34&trackingid=0QcF")
        b = ("https://www.linkedin.com/jobs/view/program-manager-at-uber-4407580818"
             "?pagenum=2&position=2&refid=ef8p&trackingid=r5EE")
        self.assertEqual(canonical_jd_url(a), canonical_jd_url(b))
        self.assertEqual(canonical_jd_url(a), "linkedin.com/jobs/view/4407580818")

    def test_linkedin_different_jobs_stay_distinct(self):
        a = "https://www.linkedin.com/jobs/view/x-at-uber-4407580818"
        b = "https://www.linkedin.com/jobs/view/x-at-uber-4407580819"
        self.assertNotEqual(canonical_jd_url(a), canonical_jd_url(b))

    def test_tesla_apply_and_slug_forms_collapse(self):
        a = "https://www.tesla.com/careers/search/job/apply/275085"
        b = ("https://www.tesla.com/careers/search/job/"
             "technical-program-manager-integration-platforms-275085")
        self.assertEqual(canonical_jd_url(a), canonical_jd_url(b))
        self.assertEqual(canonical_jd_url(a), "tesla.com/careers/search/job/275085")

    def test_tesla_different_ids_stay_distinct(self):
        a = "https://www.tesla.com/careers/search/job/apply/275085"
        b = "https://www.tesla.com/careers/search/job/apply/254809"
        self.assertNotEqual(canonical_jd_url(a), canonical_jd_url(b))

    def test_gh_jid_is_preserved_as_job_key(self):
        """Embedded Greenhouse boards: the query IS the job identity."""
        a = "https://www.pinterestcareers.com/jobs/?gh_jid=7494686"
        b = "https://www.pinterestcareers.com/jobs/?gh_jid=7770927"
        self.assertNotEqual(canonical_jd_url(a), canonical_jd_url(b))
        self.assertIn("gh_jid=7494686", canonical_jd_url(a))

    def test_gh_src_tracking_param_dropped(self):
        a = "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123&gh_src=abc123"
        b = "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123"
        self.assertEqual(canonical_jd_url(a), canonical_jd_url(b))

    def test_greenhouse_host_variants_collapse(self):
        a = "https://boards.greenhouse.io/acme/jobs/123"
        b = "https://job-boards.greenhouse.io/acme/jobs/123"
        self.assertEqual(canonical_jd_url(a), canonical_jd_url(b))

    def test_scheme_www_case_trailing_slash_normalized(self):
        a = "https://WWW.Amazon.jobs/en/jobs/10457530/senior-tpm/"
        b = "http://amazon.jobs/en/jobs/10457530/senior-tpm"
        self.assertEqual(canonical_jd_url(a), canonical_jd_url(b))

    def test_utm_params_dropped_and_order_insensitive(self):
        a = "https://x.co/jobs/9?b=2&a=1&utm_source=li&utm_campaign=q3"
        b = "https://x.co/jobs/9?a=1&b=2"
        self.assertEqual(canonical_jd_url(a), canonical_jd_url(b))

    def test_blank_and_none_safe(self):
        self.assertEqual(canonical_jd_url(""), "")
        self.assertEqual(canonical_jd_url(None), "")

    def test_coreweave_bare_param_kept_deterministically(self):
        a = "https://coreweave.com/careers/job?4690181006&board=coreweave&gh_jid=4690181006"
        b = "https://coreweave.com/careers/job?4694176006&board=coreweave&gh_jid=4694176006"
        self.assertNotEqual(canonical_jd_url(a), canonical_jd_url(b))
        self.assertEqual(canonical_jd_url(a), canonical_jd_url(a))


class TestBatchUpsertCanonicalMatch(unittest.TestCase):
    """BUG-71: a posting rediscovered under a URL variant updates its existing
    JD_Tracker row instead of appending a duplicate."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _jd_json(self, title):
        return json.dumps({"job_title": title, "company": "Uber",
                           "location": "Seattle, WA"})

    def test_variant_url_updates_not_appends(self):
        base = ("https://www.linkedin.com/jobs/view/pm-at-uber-4407580818"
                "?pagenum=0&refid=a")
        variant = ("https://www.linkedin.com/jobs/view/pm-at-uber-4407580818"
                   "?pagenum=2&refid=b")
        batch_upsert_jd_records(self.path, [(base, self._jd_json("PM v1"), "h1")])
        batch_upsert_jd_records(self.path, [(variant, self._jd_json("PM v2"), "h2")])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        rows = [ws.cell(r, 2).value for r in range(2, ws.max_row + 1)
                if ws.cell(r, 1).value]
        wb.close()
        self.assertEqual(rows, ["PM v2"])

    def test_distinct_jobs_still_append(self):
        a = "https://www.tesla.com/careers/search/job/apply/275085"
        b = "https://www.tesla.com/careers/search/job/apply/254809"
        batch_upsert_jd_records(self.path, [(a, self._jd_json("TPM A"), "h1"),
                                            (b, self._jd_json("TPM B"), "h2")])
        wb = openpyxl.load_workbook(self.path)
        ws = wb["JD_Tracker"]
        n = sum(1 for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value)
        wb.close()
        self.assertEqual(n, 2)


class TestSortCompanyListByTrack(unittest.TestCase):
    """sort_company_list_by_track orders rows by canonical TRACK_ORDER then
    name; blank/custom Track values sink to the bottom; all 9 columns travel
    with their row; count-preserving and idempotent."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        ws = wb["Company_List"]
        # Deliberately out of order; counters (cols 6–9) distinct per row.
        ws.append(["Zeta", "Fintech", "f", "https://z.co", "2026-01-01", 5, 3, 1, "No"])
        ws.append(["CustomCo", "SomethingElse", "c", "https://c.co", "2026-01-01", 9, 9, 9, "No"])
        ws.append(["Beta", "AI-native", "b", "https://b.co", "2026-01-01", 2, 1, 0, "No"])
        ws.append(["BlankCo", None, "n", "https://n.co", "2026-01-01", 7, 7, 7, "Yes"])
        ws.append(["alpha", "AI-native", "a", "https://a.co", "2026-01-01", 1, 0, 0, "No"])
        ws.append(["Delta", "Defense", "d", "https://d.co", "2026-01-01", 4, 2, 0, "No"])
        wb.save(self.path)
        wb.close()

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _rows(self):
        wb = openpyxl.load_workbook(self.path, read_only=True)
        ws = wb["Company_List"]
        rows = [list(r) for r in ws.iter_rows(min_row=2, values_only=True)
                if any(v not in (None, "") for v in r)]
        wb.close()
        return rows

    def test_sorts_by_track_order_then_name(self):
        n = sort_company_list_by_track(self.path)
        self.assertEqual(n, 6)
        names = [r[0] for r in self._rows()]
        # AI-native (alpha, Beta — case-insensitive), Fintech, Defense,
        # then unknown tracks last (BlankCo, CustomCo by name).
        self.assertEqual(names, ["alpha", "Beta", "Zeta", "Delta",
                                 "BlankCo", "CustomCo"])

    def test_all_columns_travel_with_row(self):
        sort_company_list_by_track(self.path)
        rows = {r[0]: r for r in self._rows()}
        self.assertEqual(rows["Zeta"][5:9], [5, 3, 1, "No"])
        self.assertEqual(rows["BlankCo"][5:9], [7, 7, 7, "Yes"])
        self.assertEqual(rows["BlankCo"][3], "https://n.co")

    def test_count_preserved_and_idempotent(self):
        n1 = sort_company_list_by_track(self.path)
        first = self._rows()
        n2 = sort_company_list_by_track(self.path)
        self.assertEqual((n1, n2), (6, 6))
        self.assertEqual(first, self._rows())
        self.assertEqual(count_company_rows(self.path), 6)


class TestLoadWorkbookReadonlySnapshot(unittest.TestCase):
    """BUG-73: read-only loads snapshot the file into memory, so another
    process's wb.save() (truncate + rewrite in place) can never crash a
    reader mid-stream with EOFError."""

    def setUp(self):
        self.path = _tmp_xlsx()
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_reader_survives_file_truncation_mid_read(self):
        wb = load_workbook_readonly(self.path)
        try:
            # Simulate a concurrent wb.save(): truncate the file in place
            # while the read-only workbook is still open.
            with open(self.path, "wb") as fh:
                fh.write(b"")
            ws = wb["Company_List"]
            rows = list(ws.iter_rows(values_only=True))
            self.assertEqual(rows[0][0], "Company Name")
        finally:
            wb.close()

    def test_corrupt_file_raises_after_retries(self):
        import zipfile
        with open(self.path, "wb") as fh:
            fh.write(b"not a zip file")
        with unittest.mock.patch("shared.excel_store.time.sleep") as slept:
            with self.assertRaises(zipfile.BadZipFile):
                load_workbook_readonly(self.path)
        self.assertEqual(slept.call_count, 2)  # 3 attempts, 2 backoffs

    def test_transient_corruption_recovers_on_retry(self):
        """A writer caught mid-save yields a bad zip once; the retry sees
        the completed file and succeeds."""
        with open(self.path, "rb") as fh:
            good = fh.read()
        with open(self.path, "wb") as fh:
            fh.write(good[: len(good) // 2])  # half-written save

        def _heal(_):
            with open(self.path, "wb") as fh:
                fh.write(good)

        with unittest.mock.patch("shared.excel_store.time.sleep",
                                 side_effect=_heal):
            wb = load_workbook_readonly(self.path)
        self.assertIn("Company_List", wb.sheetnames)
        wb.close()


# ═════════════════════════════════════════════════════════════════════════════
# BUG-74~77 — Company_Audit transient review tab
# ═════════════════════════════════════════════════════════════════════════════
class TestReplaceAuditSheet(unittest.TestCase):

    def setUp(self):
        from shared.excel_store import get_or_create_excel
        fd, self.path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        os.remove(self.path)
        get_or_create_excel(self.path)
        wb = openpyxl.load_workbook(self.path)
        wb["Company_List"].append(
            ["SomeCo", "AI-native", "focus", "https://jobs.lever.co/someco",
             None, 0, 0, 0, "No"])
        wb.save(self.path)
        wb.close()

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _row(self, name="SomeCo"):
        return [name, "AI-native", "", "no", "focus", "", "no", "", "track:yes focus:yes",
                "", "2026-08-19 00:00:00"]

    def test_creates_sheet_with_headers(self):
        from shared.excel_store import replace_audit_sheet, AUDIT_SHEET, AUDIT_HEADERS
        n = replace_audit_sheet(self.path, [self._row()])
        self.assertEqual(n, 1)
        wb = openpyxl.load_workbook(self.path, read_only=True)
        self.assertIn(AUDIT_SHEET, wb.sheetnames)
        rows = list(wb[AUDIT_SHEET].iter_rows(values_only=True))
        wb.close()
        self.assertEqual(list(rows[0]), AUDIT_HEADERS)
        self.assertEqual(rows[1][0], "SomeCo")

    def test_replace_is_idempotent_not_append(self):
        from shared.excel_store import replace_audit_sheet, AUDIT_SHEET
        replace_audit_sheet(self.path, [self._row("A"), self._row("B")])
        replace_audit_sheet(self.path, [self._row("C")])
        wb = openpyxl.load_workbook(self.path, read_only=True)
        rows = list(wb[AUDIT_SHEET].iter_rows(values_only=True))
        wb.close()
        self.assertEqual(len(rows), 2)  # header + 1
        self.assertEqual(rows[1][0], "C")

    def test_other_tabs_untouched(self):
        from shared.excel_store import replace_audit_sheet, get_company_rows
        wb = openpyxl.load_workbook(self.path, read_only=True)
        sheets_before = set(wb.sheetnames)
        wb.close()
        company_before = get_company_rows(self.path)
        replace_audit_sheet(self.path, [self._row()])
        wb = openpyxl.load_workbook(self.path, read_only=True)
        sheets_after = set(wb.sheetnames)
        wb.close()
        self.assertEqual(sheets_after - sheets_before, {"Company_Audit"})
        self.assertEqual(get_company_rows(self.path), company_before)

    def test_not_auto_created_by_get_or_create(self):
        # Transient tab: get_or_create_excel must NOT create it.
        wb = openpyxl.load_workbook(self.path, read_only=True)
        self.assertNotIn("Company_Audit", wb.sheetnames)
        wb.close()


class TestUpsertNeverOverwritesFilledCareerUrl(unittest.TestCase):
    """REQ-153 storage-layer invariant: a filled Career URL is user-verified
    data — upsert_companies may fill a blank cell but never replace one."""

    def setUp(self):
        from shared.excel_store import get_or_create_excel
        fd, self.path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        os.remove(self.path)
        get_or_create_excel(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _url(self, name):
        wb = openpyxl.load_workbook(self.path, read_only=True)
        urls = {r[0]: r[3] for r in wb["Company_List"].iter_rows(min_row=2, values_only=True) if r[0]}
        wb.close()
        return urls.get(name)

    def test_filled_url_survives_re_upsert(self):
        from shared.excel_store import upsert_companies
        upsert_companies(self.path, [{"company_name": "VerifiedCo", "track": "AI-native",
                                      "business_focus": "f", "career_url": "https://user-verified.example.com"}])
        upsert_companies(self.path, [{"company_name": "VerifiedCo", "track": "Robotics",
                                      "business_focus": "new", "career_url": "https://discovered-wrong.example.com"}])
        self.assertEqual(self._url("VerifiedCo"), "https://user-verified.example.com")

    def test_blank_and_na_urls_still_fillable(self):
        from shared.excel_store import upsert_companies
        upsert_companies(self.path, [{"company_name": "BlankCo", "track": "Space",
                                      "business_focus": "f", "career_url": ""},
                                     {"company_name": "NaCo", "track": "Space",
                                      "business_focus": "f", "career_url": "N/A"}])
        upsert_companies(self.path, [{"company_name": "BlankCo", "track": "Space",
                                      "business_focus": "f", "career_url": "https://found.example.com"},
                                     {"company_name": "NaCo", "track": "Space",
                                      "business_focus": "f", "career_url": "https://found2.example.com"}])
        self.assertEqual(self._url("BlankCo"), "https://found.example.com")
        self.assertEqual(self._url("NaCo"), "https://found2.example.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
