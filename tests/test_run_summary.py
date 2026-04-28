"""
P0-7: RunSummary dataclass unit tests.

Covers init defaults, counter mutation, JSON serialization, and the
write() helper that drops {agent}-{run_id}.json into run_logs/.
"""
import sys
import os
import json
import shutil
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from shared.run_summary import RunSummary


class TestRunSummaryDefaults(unittest.TestCase):

    def test_run_id_is_unique_per_instance(self):
        a = RunSummary(agent="match")
        b = RunSummary(agent="match")
        self.assertNotEqual(a.run_id, b.run_id)
        self.assertTrue(len(a.run_id) >= 8)

    def test_started_at_is_iso_string(self):
        s = RunSummary(agent="match")
        # ISO 8601: contains 'T'
        self.assertIn("T", s.started_at)

    def test_counters_default_to_zero(self):
        s = RunSummary(agent="match")
        self.assertEqual(s.attempted, 0)
        self.assertEqual(s.succeeded, 0)
        self.assertEqual(s.failed, 0)
        self.assertEqual(s.skipped, 0)
        self.assertEqual(s.transient_errors, 0)
        self.assertEqual(s.structural_errors, 0)
        self.assertEqual(s.notes, [])

    def test_finished_at_starts_none(self):
        s = RunSummary(agent="match")
        self.assertIsNone(s.finished_at)


class TestRunSummaryMutation(unittest.TestCase):

    def test_counters_increment_independently(self):
        s = RunSummary(agent="match")
        s.attempted += 5
        s.succeeded += 3
        s.failed += 1
        s.transient_errors += 1
        self.assertEqual(s.attempted, 5)
        self.assertEqual(s.succeeded, 3)
        self.assertEqual(s.failed, 1)
        self.assertEqual(s.transient_errors, 1)
        # untouched counters stay at 0
        self.assertEqual(s.structural_errors, 0)
        self.assertEqual(s.skipped, 0)

    def test_note_appends(self):
        s = RunSummary(agent="match")
        s.note("first")
        s.note("second")
        self.assertEqual(s.notes, ["first", "second"])

    def test_mark_finished_sets_iso_string(self):
        s = RunSummary(agent="match")
        self.assertIsNone(s.finished_at)
        s.mark_finished()
        self.assertIsNotNone(s.finished_at)
        self.assertIn("T", s.finished_at)


class TestRunSummarySerialization(unittest.TestCase):

    def test_to_dict_includes_all_fields(self):
        s = RunSummary(agent="match")
        s.attempted = 10
        s.succeeded = 7
        s.note("hello")
        d = s.to_dict()
        for key in ("agent", "run_id", "started_at", "finished_at",
                    "attempted", "succeeded", "failed", "skipped",
                    "transient_errors", "structural_errors", "notes"):
            self.assertIn(key, d)
        self.assertEqual(d["attempted"], 10)
        self.assertEqual(d["succeeded"], 7)
        self.assertEqual(d["notes"], ["hello"])

    def test_to_json_round_trips(self):
        s = RunSummary(agent="match")
        s.attempted = 4
        s.note("test")
        parsed = json.loads(s.to_json())
        self.assertEqual(parsed["agent"], "match")
        self.assertEqual(parsed["attempted"], 4)
        self.assertEqual(parsed["notes"], ["test"])


class TestRunSummaryWrite(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pf-runlogs-")

    def tearDown(self):
        if os.path.isdir(self.tmpdir):
            shutil.rmtree(self.tmpdir)

    def test_write_creates_file_with_agent_and_run_id_in_name(self):
        s = RunSummary(agent="match")
        s.attempted = 3
        path = s.write(log_dir=self.tmpdir)
        self.assertTrue(os.path.isfile(path))
        self.assertIn("match-", os.path.basename(path))
        self.assertIn(s.run_id, os.path.basename(path))

    def test_write_serializes_correct_json(self):
        s = RunSummary(agent="optimizer")
        s.attempted = 5
        s.succeeded = 3
        s.failed = 2
        s.structural_errors = 2
        path = s.write(log_dir=self.tmpdir)
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["agent"], "optimizer")
        self.assertEqual(payload["attempted"], 5)
        self.assertEqual(payload["succeeded"], 3)
        self.assertEqual(payload["failed"], 2)
        self.assertEqual(payload["structural_errors"], 2)

    def test_write_creates_log_dir_if_missing(self):
        nested = os.path.join(self.tmpdir, "nested", "run_logs")
        s = RunSummary(agent="match")
        path = s.write(log_dir=nested)
        self.assertTrue(os.path.isdir(nested))
        self.assertTrue(os.path.isfile(path))

    def test_write_finalizes_finished_at(self):
        s = RunSummary(agent="match")
        self.assertIsNone(s.finished_at)
        s.write(log_dir=self.tmpdir)
        self.assertIsNotNone(s.finished_at)


if __name__ == "__main__":
    unittest.main()
