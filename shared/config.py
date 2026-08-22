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
