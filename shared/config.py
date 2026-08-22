"""Shared configuration constants."""
import os

MODEL = "gemini-3.5-flash-lite"

# PRJ-004 6-track taxonomy, in canonical display/sort order. Single source of
# truth shared by company_agent (quotas/schemas) and excel_store (Company_List
# sort) — defined here to avoid a circular import between those two modules.
TRACK_ORDER = ("AI-native", "Mid-large Tech", "Robotics", "Fintech", "Space", "Defense")

# Computed independently to avoid importing from shared.excel_store (which itself
# imports config). Resolves to <repo root>/jd_cache.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JD_CACHE_DIR = os.path.join(_PROJECT_ROOT, "jd_cache")

# 2026-08-22 (REQ-170) freshness first-seen window: a posting seen for the
# FIRST time is skipped pre-scrape when its list-API date is older than this
# many days (rows already tracked are never touched). Mid-large Tech keeps the
# PRJ-004 14-day window (= the freshness-tier ceiling, REQ-004-10); vertical
# tracks (AI-native / Robotics / Fintech / Space / Defense) get 45 — small and
# early-stage orgs post rarely and keep roles open for months, and a paused
# schedule / sporadic manual runs would otherwise lose those postings for good
# (Cowboy Space "TPM, Avionics", posted 07-17, was 36 days old when this
# landed — 30 would still have lost it). Kept rows older than 14 days carry no
# freshness tier (sort tier 9 — visible at the bottom, never dropped).
FIRST_SEEN_MAX_AGE_DAYS = 14
FIRST_SEEN_MAX_AGE_DAYS_VERTICAL = 45
