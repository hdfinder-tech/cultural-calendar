"""Source registry: maps each source id to its tactic, importer, and health range.

This replaces the original ``if source.id == ... elif ...`` dispatch in run_imports.
Sources not given a dedicated importer fall through to ``import_html_source`` (the HTML
catch-all that internally handles the museum framework, the capture fixtures, broadway
hydration, and the per-site HTML parsers).
"""

from __future__ import annotations

from . import legacy
from .core.config import Source
from .sources.base import SourcePlugin

# Sources with a dedicated importer. Everything else -> html catch-all (import_html_source).
_DEDICATED: dict[str, tuple[str, object, bool]] = {
    # id: (tactic, importer, needs_aperture)
    "tvmaze_full_schedule": ("json_api", legacy.import_tvmaze, True),
    "tmdb_movies": ("json_api", legacy.import_tmdb, False),
    "carnegie_hall": ("json_api", legacy.import_carnegie, False),
    "ibdb": ("html", legacy.import_ibdb, False),
    "nyphil_concerts": ("json_api", legacy.import_nyphil_api, False),
    "gagosian": ("embedded_json", legacy.import_gallery_nextdata, False),
    "guggenheim": ("embedded_json", legacy.import_guggenheim, False),
    "lacma": ("html", legacy.import_lacma, False),
    "pac_nyc": ("html", legacy.import_pac, False),
    "the_shed": ("html", legacy.import_the_shed, False),
}

# Tactic label for the html-catch-all sources (for docs/health/tests clarity).
_HTML_TACTIC = {
    "new_museum": "embedded_json",
    "moma_exhibitions": "capture",
    "frick": "capture",
    "armory": "capture",
    "tate_modern": "capture",
}

# Expected row ranges (health / silent-drift alarm). Wide enough to tolerate normal churn,
# tight enough to catch a source that breaks to 0 or balloons.
EXPECTED_ROWS: dict[str, tuple[int, int]] = {
    "tmdb_movies": (15, 52), "tvmaze_full_schedule": (40, 200),
    "carnegie_hall": (20, 220), "metacritic_albums": (40, 260),
    "broadway_org": (1, 30), "playbill_broadway": (0, 30), "playbill_offbroadway": (3, 60),
    "bam_programs": (0, 12), "met_exhibitions": (1, 25), "moma_exhibitions": (0, 30),
    "whitney": (0, 30), "brooklyn_museum": (0, 25), "moca_la": (0, 20), "lacma": (0, 25),
    "pace_gallery": (0, 20), "gagosian": (0, 20), "guggenheim": (0, 20), "frick": (0, 20),
    "new_museum": (0, 20), "met_opera_2026_27": (5, 40), "nycb_seasons": (5, 50),
    "nyphil_concerts": (5, 120), "aoty_upcoming": (0, 60), "ibdb": (0, 25),
    "pac_nyc": (1, 25), "the_shed": (1, 25), "armory": (0, 30), "tate_modern": (0, 20),
}


def plugin_for(source: Source) -> SourcePlugin:
    expected = EXPECTED_ROWS.get(source.id)
    if source.id in _DEDICATED:
        tactic, importer, needs_aperture = _DEDICATED[source.id]
        return SourcePlugin(source.id, tactic, importer, needs_aperture, expected)
    tactic = _HTML_TACTIC.get(source.id, "html")
    return SourcePlugin(source.id, tactic, legacy.import_html_source, False, expected)
