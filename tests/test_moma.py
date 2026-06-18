"""MoMA fixture-backed source + flag-gated live scraper.

Invariants from the editorial spec:
- The committed fixture is the source of truth; a 403 / JS shell / unverified parse never
  replaces it with an empty or wrong result.
- The live parser is opt-in (MOMA_LIVE) and may override the fixture only when the fetched
  document proves it's the real index (both section headings + an in-section exhibition link).
- The live parser reads only the 'Upcoming exhibitions' section (before 'Installations and
  projects'); current/ongoing shows outside it never leak in.
"""

import datetime as dt
import json
from pathlib import Path

from cultural_calendar import legacy as L

FIXTURES = Path(__file__).parent / "fixtures"
MOMA_HTML = (FIXTURES / "moma_index.html").read_text()
SHELL_403 = "<html><head><title>Access Denied</title></head><body>nope</body></html>"


def _src():
    return next(s for s in L.load_sources() if s.id == "moma_exhibitions")


def _legacy_fixture(path: Path):
    """A small legacy {exhibitionLinks} fixture with two in-horizon shows (distinct from the
    live HTML titles, so we can tell which source supplied the rows)."""
    path.write_text(json.dumps({"exhibitionLinks": [
        {"text": "Fixture Show One Jul 1, 2026–Jan 2, 2027",
         "href": "https://www.moma.org/calendar/exhibitions/9001"},
        {"text": "Fixture Show Two Aug 1, 2026–Summer 2027",
         "href": "https://www.moma.org/calendar/exhibitions/9002"},
    ]}))


def test_moma_document_valid_gate():
    assert L.moma_document_valid(MOMA_HTML) is True            # both headings + in-section link
    assert L.moma_document_valid(SHELL_403) is False           # 403 shell: no headings
    assert L.moma_document_valid("") is False
    # Upcoming heading present but boundary missing → can't trust the section bounds.
    assert L.moma_document_valid(
        "<h2>Upcoming exhibitions</h2><a href='/calendar/exhibitions/1'>x</a>") is False


def test_parse_moma_exhibitions_is_section_scoped(monkeypatch):
    monkeypatch.setenv("CALENDAR_END_DATE", "2027-12-31")
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 18))
    items = L.parse_moma_exhibitions(_src(), MOMA_HTML)
    titles = [i["title"] for i in items]
    assert len(items) == 9                                     # exactly the nine upcoming
    assert any(t.startswith("Pierre Huyghe") for t in titles)
    assert any(t.startswith("Mondrian Boogie Woogie") for t in titles)
    assert not any("Marcel Duchamp" in t for t in titles)      # current section, above boundary
    assert not any("Art Lab" in t for t in titles)             # installations, below boundary
    # A 403 shell has no section → parser returns nothing (caller serves the fixture).
    assert L.parse_moma_exhibitions(_src(), SHELL_403) == []


def _run_import(monkeypatch, tmp_path, *, flag, fetch):
    monkeypatch.setenv("CALENDAR_END_DATE", "2027-12-31")
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 18))
    monkeypatch.setattr(L, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(L, "save_raw", lambda *a, **k: None)
    monkeypatch.setattr(L, "enrich_detail_pages", lambda *a, **k: None)
    cache = tmp_path / "moma.json"
    _legacy_fixture(cache)
    monkeypatch.setattr(L, "MOMA_CAPTURE_LINKS", cache)
    if flag:
        monkeypatch.setenv("MOMA_LIVE", "1")
    else:
        monkeypatch.delenv("MOMA_LIVE", raising=False)
    monkeypatch.setattr(L, "fetch_text", fetch)
    conn = L.connect()
    n = L.import_html_source(conn, _src())
    titles = {r[0] for r in conn.execute(
        "select title from items where source_id='moma_exhibitions'")}
    status = conn.execute(
        "select status from source_runs where source_id='moma_exhibitions' order by id desc limit 1"
    ).fetchone()[0]
    return n, titles, status, cache


def test_flag_off_serves_fixture_ignoring_live(monkeypatch, tmp_path):
    """A clean, valid live document is ignored when the flag is off — fixture stays canonical."""
    n, titles, status, cache = _run_import(
        monkeypatch, tmp_path, flag=False, fetch=lambda *a, **k: MOMA_HTML)
    assert n == 2 and "Fixture Show One" in titles             # fixture served
    assert not any("Pierre Huyghe" in t for t in titles)       # live NOT used
    assert status == "stale"
    assert "exhibitionLinks" in json.loads(cache.read_text())  # fixture file untouched


def test_403_leaves_fixture_untouched(monkeypatch, tmp_path):
    """A 403 (curl rejecting non-2xx → RuntimeError) serves the fixture, never empty."""
    def boom(*a, **k):
        raise RuntimeError("curl fetch returned HTTP 403")
    n, titles, status, cache = _run_import(monkeypatch, tmp_path, flag=True, fetch=boom)
    assert n == 2 and "Fixture Show One" in titles
    assert status == "stale"
    assert "exhibitionLinks" in json.loads(cache.read_text())  # not overwritten


def test_flag_on_unverified_shell_leaves_fixture(monkeypatch, tmp_path):
    """Flag on but a 200 JS-shell (no section headings) fails the gate → fixture kept."""
    n, titles, status, cache = _run_import(
        monkeypatch, tmp_path, flag=True, fetch=lambda *a, **k: SHELL_403)
    assert n == 2 and "Fixture Show One" in titles
    assert not any("Pierre Huyghe" in t for t in titles)
    assert "exhibitionLinks" in json.loads(cache.read_text())  # not overwritten


def test_flag_on_valid_document_refreshes_fixture(monkeypatch, tmp_path):
    """Flag on + a verified real index → live parse wins and refreshes the committed fixture."""
    n, titles, status, cache = _run_import(
        monkeypatch, tmp_path, flag=True, fetch=lambda *a, **k: MOMA_HTML)
    assert n == 9 and any("Pierre Huyghe" in t for t in titles)
    assert not any("Fixture Show One" in t for t in titles)    # live replaced the fixture rows
    assert status == "ok"
    refreshed = json.loads(cache.read_text())
    assert "items" in refreshed and len(refreshed["items"]) == 9  # fixture refreshed, structured
