"""TMDb coverage: the per-month horizon pass must surface far-out films that the global
popularity pass crowds out (the Dec-2026 prestige titles the calendar was missing)."""

import datetime as dt
import json

from cultural_calendar import legacy as L


def test_month_starts_inclusive():
    months = L._month_starts(dt.date(2026, 6, 18), dt.date(2026, 12, 31))
    assert months == [dt.date(2026, m, 1) for m in range(6, 13)]  # Jun 1 .. Dec 1, inclusive


def test_import_tmdb_per_month_pass_surfaces_far_horizon(tmp_path, monkeypatch):
    monkeypatch.setenv("CALENDAR_END_DATE", "2026-12-31")
    monkeypatch.setenv("TMDB_API_KEY", "tok")
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 18))
    monkeypatch.setattr(L, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(L, "save_raw", lambda source, text: None)
    monkeypatch.setattr(L, "tmdb_principals", lambda *a, **k: [])

    def fake_fetch(url, params=None, headers=None):
        gte, lte = params["release_date.gte"], params["release_date.lte"]
        results = []
        if params["page"] == 1:
            if gte == "2026-06-18" and lte == "2026-12-31":
                # Global popularity pass: only the buzzy near-term film is in range.
                results = [{"id": 1, "title": "Near Movie", "release_date": "2026-06-25",
                            "popularity": 99, "overview": "x"}]
            elif gte.startswith("2026-12"):
                # December bucket: a low-popularity prestige title the global pass never reached.
                results = [{"id": 2, "title": "Werwulf", "release_date": "2026-12-25",
                            "popularity": 3, "overview": "x"}]
        return json.dumps({"results": results, "total_pages": 1})

    monkeypatch.setattr(L, "fetch_text", fake_fetch)
    src = next(s for s in L.load_sources() if s.id == "tmdb_movies")
    conn = L.connect()
    count = L.import_tmdb(conn, src)
    titles = {r[0] for r in conn.execute("select title from items where source_id='tmdb_movies'")}
    assert "Near Movie" in titles                 # global pass still works
    assert "Werwulf" in titles                    # per-month pass recovered the far-horizon film
    assert count == 2                              # deduped by external_id across passes
