"""Shared paths, constants, and the Source dataclass for the Cultural Calendar package."""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# The calendar is New York editorial; anchor "today" to Eastern so a run near midnight UTC
# (or a runner in another tz) doesn't misclassify what counts as a future opening.
_CALENDAR_TZ = ZoneInfo("America/New_York")

# Project root = parent of the cultural_calendar/ package, so data/, sources.json, and the
# *_capture/ fixtures resolve the same way they did for the original toy_calendar.py.
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DETAIL_DIR = DATA_DIR / "details"
DB_PATH = DATA_DIR / "calendar.db"
SOURCES_PATH = ROOT / "sources.json"
HTML_PATH = DATA_DIR / "toy-calendar.html"
MOMA_CAPTURE_LINKS = ROOT / "moma_capture" / "moma-exhibition-links.json"
MET_CAPTURE = ROOT / "met_capture" / "met-exhibitions.json"
MET_OPERA_CAPTURE = ROOT / "met_opera_capture" / "met-opera-season.json"
ARMORY_CAPTURE = ROOT / "armory_capture" / "armory-events.json"
NPG_CAPTURE = ROOT / "npg_capture" / "npg-exhibitions.json"
SERPENTINE_CAPTURE = ROOT / "serpentine_capture" / "serpentine-exhibitions.json"
# Last-good caches for the live scrapers (never publish empty on a blocked/invalid fetch).
VA_CACHE = ROOT / "va_capture" / "va-exhibitions.json"
TATE_MODERN_CACHE = ROOT / "tate_capture" / "tate-modern-exhibitions.json"
TATE_BRITAIN_CACHE = ROOT / "tate_britain_capture" / "tate-britain-exhibitions.json"
FLV_CACHE = ROOT / "flv_capture" / "flv-exhibitions.json"
GRAND_PALAIS_CAPTURE = ROOT / "grand_palais_capture" / "grand-palais-exhibitions.json"
POMPIDOU_CAPTURE = ROOT / "pompidou_capture" / "pompidou-exhibitions.json"
MAM_CAPTURE = ROOT / "mam_capture" / "mam-paris-exhibitions.json"
FRICK_CAPTURE = ROOT / "frick_capture" / "frick-exhibitions.json"
OCULA_CAPTURE = ROOT / "ocula_capture" / "ocula-ny.json"
MARIAN_GOODMAN_CACHE = ROOT / "marian_goodman_capture" / "marian-goodman-exhibitions.json"

MONTH_PATTERN = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December"
MONTH_RE = rf"(?:{MONTH_PATTERN})"
MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


@dataclass
class Source:
    id: str
    name: str
    category: str
    type: str
    url: str
    enabled: bool = True
    requires_env: str | None = None


def today() -> dt.date:
    return dt.datetime.now(_CALENDAR_TZ).date()


HORIZON_MONTHS = 18  # rolling editorial window


def end_date() -> dt.date:
    """Forward horizon for "is this upcoming." Pin with CALENDAR_END_DATE=YYYY-MM-DD; otherwise a
    rolling ~18-month window (end of that month), floored at 2026-12-31 so it never drops below the
    project's original through-2026 commitment. Rolling keeps the calendar useful past 2026 and
    lets announced 2027 seasons through."""
    override = os.environ.get("CALENDAR_END_DATE")
    if override:
        try:
            return dt.date.fromisoformat(override)
        except ValueError:
            pass
    t = today()
    total = t.month - 1 + HORIZON_MONTHS
    year, month = t.year + total // 12, total % 12 + 1
    nxt = dt.date(year + month // 12, month % 12 + 1, 1)  # first of the following month
    return max(nxt - dt.timedelta(days=1), dt.date(2026, 12, 31))


def load_sources() -> list[Source]:
    raw = json.loads(SOURCES_PATH.read_text())
    # Tolerate enrichment keys (tactic/config/expected_rows) added for the redesign by
    # passing only the fields Source declares.
    fields = {"id", "name", "category", "type", "url", "enabled", "requires_env"}
    return [Source(**{k: v for k, v in item.items() if k in fields}) for item in raw if item.get("enabled", True)]
