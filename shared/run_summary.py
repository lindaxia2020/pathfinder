"""Per-agent structured run summary written to run_logs/{agent}-{run_id}.json.

P0-7 — until now an agent run that processed 100 JDs and silently failed on
80 of them looked indistinguishable from a clean success in stdout. This
module gives every main() a small dataclass to count attempts / successes /
failures (split into transient vs structural after P0-4) and serialize it
on completion. The orchestrator can grep run_logs/ for unhealthy runs
without re-running.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RunSummary:
    """Run metrics for one main() invocation.

    Counters are bumped by the agent at write-points; transient_errors and
    structural_errors mirror the P0-4 exception taxonomy so the user can
    tell quota blips apart from response-quality issues.
    """
    agent: str                          # "company" | "job" | "match" | "optimizer"
    run_id: str = field(default_factory=_new_run_id)
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    attempted: int = 0                  # records considered for processing
    succeeded: int = 0                  # records written successfully
    failed: int = 0                     # records that failed any reason (sum of transient + structural)
    skipped: int = 0                    # records intentionally skipped (already done, prefilter, etc.)
    transient_errors: int = 0           # GeminiTransientError occurrences
    structural_errors: int = 0          # GeminiStructuralError occurrences
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def mark_finished(self) -> None:
        self.finished_at = _now_iso()

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "transient_errors": self.transient_errors,
            "structural_errors": self.structural_errors,
            "notes": list(self.notes),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def write(self, log_dir: str = "run_logs") -> str:
        """Write the summary to log_dir/{agent}-{run_id}.json. Creates dir if needed.
        Returns the absolute file path."""
        if not self.finished_at:
            self.mark_finished()
        os.makedirs(log_dir, exist_ok=True)
        fname = f"{self.agent}-{self.run_id}.json"
        path = os.path.abspath(os.path.join(log_dir, fname))
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.to_json())
        except Exception as e:
            # Logging is fine but never let summary writing crash the run.
            logging.error(f"[RunSummary] Failed to write {path}: {e}")
        return path
