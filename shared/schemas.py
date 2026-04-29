"""Shared Pydantic output schemas for Gemini structured responses.

Single source of truth — match_agent and resume_optimizer must reference
the same MatchResult class so the original score and tailored re-score
use identical schemas.
"""
from pydantic import BaseModel, Field


class CoarseItem(BaseModel):
    index: int = Field(description="0-based index of the JD in the batch.")
    score: int = Field(description="0-100 coarse fit score.")


class BatchCoarseResult(BaseModel):
    items: list[CoarseItem] = Field(description="One entry per JD in the batch.")


class MatchResult(BaseModel):
    compatibility_score:   int       = Field(description="0–100 fit score.")
    key_strengths:         list[str]
    critical_gaps:         list[str]
    recommendation_reason: str       = Field(description="Specific, weighted analysis per criteria.")


class TailoredResume(BaseModel):
    tailored_resume_markdown: str = Field(description="Complete tailored resume in Markdown.")
    optimization_summary: str = Field(description="3-5 key changes made and which JD requirements they target.")


class BatchTailoredItem(BaseModel):
    index: int = Field(description="0-based index of the JD in the batch.")
    tailored_resume_markdown: str = Field(description="Complete tailored resume in Markdown.")
    optimization_summary: str = Field(description="3-5 key changes made and which JD requirements they target.")


class BatchTailoredResult(BaseModel):
    items: list[BatchTailoredItem] = Field(description="One entry per JD in the batch.")
