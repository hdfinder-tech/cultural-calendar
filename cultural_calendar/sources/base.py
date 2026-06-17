"""Source plugin model and the fetch-tactic taxonomy.

Every source maps to exactly one of four tactics. The actual fetch/parse logic currently
lives in `cultural_calendar.legacy` (migrated 1:1 from the original monolith) and is
referenced by each plugin's `importer`; the plugin layer makes the tactic explicit and
turns the old scattered ``if source.id == ...`` dispatch into a single registry lookup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from ..core.config import Source

# The four fetch tactics (see handover Refresh Runbook):
#   json_api      - configured request to a JSON endpoint (TMDb, TVMaze, Carnegie/Algolia, NY Phil)
#   html          - fetch HTML (browser headers + curl fallback), parse, optional detail hydrate.
#                   Live scrapers can be cache-backed (import_with_cache): a blocked/invalid fetch
#                   serves the last-good committed cache and records "stale", never empty.
#   embedded_json - fetch HTML, extract an embedded JSON blob (Gagosian/Guggenheim/New Museum)
#   capture       - read a committed fixture (MoMA, Frick; self-refreshing Met museum/Met Opera)
TACTICS = {"json_api", "html", "embedded_json", "capture"}


@dataclass
class SourcePlugin:
    id: str
    tactic: str
    importer: Callable[..., int]          # importer(conn, source) -> count
    needs_aperture: bool = False          # TVMaze takes the TV aperture
    expected_rows: tuple[int, int] | None = None  # health range; (lo, hi) inclusive

    def run(self, conn: sqlite3.Connection, source: Source, aperture: str = "wide") -> int:
        if self.needs_aperture:
            return self.importer(conn, source, aperture)
        return self.importer(conn, source)

    def health(self, count: int) -> str | None:
        """Return a warning string if the row count falls outside the expected range."""
        if self.expected_rows is None:
            return None
        lo, hi = self.expected_rows
        if count < lo or count > hi:
            return f"{self.id}: {count} rows outside expected {lo}-{hi} (possible scraper drift)"
        return None
