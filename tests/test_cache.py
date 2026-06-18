"""Integrity-rule tests: cache precedence, fetch validation, horizon override.

These lock the invariants from the b9001b3 review — especially that a clean live fetch
overrides a stale cached row (and never silently zeroes a venue)."""

import datetime as dt
import json

from cultural_calendar import legacy as L
from cultural_calendar.core import config
from cultural_calendar.core.config import Source


def test_merge_by_title_base_wins():
    base = [{"title": "Show X", "date_start": "LIVE"}]
    extra = [{"title": "show x", "date_start": "CACHE"}, {"title": "Other", "date_start": "C"}]
    out = L.merge_by_title(base, extra)
    x = [i for i in out if i["title"].lower() == "show x"]
    assert len(x) == 1 and x[0]["date_start"] == "LIVE"   # base wins on a title collision
    assert any(i["title"] == "Other" for i in out)        # extra-only rows are still added


def test_fetch_valid_page_rejects_challenge(monkeypatch):
    monkeypatch.setattr(L, "fetch_text", lambda u, **k: "<div>" + "x" * 20000 + ' class="teaser ">')
    assert L.fetch_valid_page("u", must_contain=('class="teaser ',)) is not None
    monkeypatch.setattr(L, "fetch_text", lambda u, **k: "<html>Attention Required! | Cloudflare</html>")
    assert L.fetch_valid_page("u") is None                       # challenge page -> None
    monkeypatch.setattr(L, "fetch_text", lambda u, **k: "too small")
    assert L.fetch_valid_page("u") is None                       # truncated shell -> None
    monkeypatch.setattr(L, "fetch_text", lambda u, **k: "x" * 20000)
    assert L.fetch_valid_page("u", must_contain=("MISSING",)) is None  # missing boilerplate -> None


def test_end_date_env_override(monkeypatch):
    monkeypatch.setenv("CALENDAR_END_DATE", "2027-06-30")
    assert config.end_date() == dt.date(2027, 6, 30)
    monkeypatch.delenv("CALENDAR_END_DATE", raising=False)
    # default is the rolling window, never below the 2026-12-31 floor
    assert config.end_date() >= dt.date(2026, 12, 31)


def _item(title, start, label, ext):
    return {"title": title, "category": "art", "date_start": start, "date_label": label,
            "date_precision": "exact", "venue_or_platform": "V", "city": "London",
            "source_url": "u", "external_id": ext, "importance_score": 1}


def test_import_with_cache_live_overrides_cache(tmp_path, monkeypatch):
    """A clean live fetch must correct a stale cached row (same title), and a cache-only row
    must survive (cache fills live-missing rows)."""
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 16))
    monkeypatch.setattr(L, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(L, "save_raw", lambda source, text: None)
    monkeypatch.setattr(L, "fetch_valid_page", lambda url, must_contain=(): "VALID PAGE")
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"capturedAt": "2026-06-16", "items": [
        _item("Show X", "2026-09-01", "STALE", "x"),
        _item("Cache Only", "2026-10-01", "c", "co"),
    ]}))

    def parser(source, text):
        return [_item("Show X", "2026-10-15", "FRESH", "x")]  # corrected date for same title

    conn = L.connect()
    src = Source(id="t", name="T", category="art", type="html", url="u")
    L.import_with_cache(conn, src, cache_path, parser)
    rows = {r["title"]: r for r in conn.execute("select title, date_start, date_label from items where source_id='t'")}
    assert rows["Show X"]["date_start"] == "2026-10-15"   # live won, not the cached 2026-09-01
    assert rows["Show X"]["date_label"] == "FRESH"
    assert "Cache Only" in rows                            # cache filled the live-missing row


def test_age_cache_retires_after_two_misses():
    cache = [{"title": "Gone", "date_start": "2026-09-01"}, {"title": "Stay", "date_start": "2026-10-01"}]
    live = [{"title": "Stay", "date_start": "2026-10-02"}]   # Gone no longer listed
    out1 = L.age_cache(live, cache)
    gone = [i for i in out1 if i["title"] == "Gone"]
    assert gone and gone[0]["_misses"] == 1                  # 1st miss: kept, aging
    assert any(i["title"] == "Stay" and i["_misses"] == 0 for i in out1)  # live resets
    out2 = L.age_cache(live, out1)                           # feed aged cache back: 2nd miss
    assert not any(i["title"] == "Gone" for i in out2)       # retired after two misses
    assert any(i["title"] == "Stay" for i in out2)


def test_armory_fieldwise_keeps_category_refreshes_date():
    cache = [{"title": "Carlo Vistoli", "category": "music", "date_start": "2026-09-10",
              "date_label": "old", "source_url": "old"}]
    live = [{"title": "Carlo Vistoli", "date_start": "2026-09-12", "date_label": "new",
             "source_url": "new"},
            {"title": "New Show", "date_start": "2026-10-01"}]
    out = {i["title"]: i for i in L.armory_fieldwise_merge(cache, live)}
    assert out["Carlo Vistoli"]["category"] == "music"        # curated category preserved
    assert out["Carlo Vistoli"]["date_start"] == "2026-09-12" # date refreshed from live
    assert out["Carlo Vistoli"]["date_label"] == "new"
    assert "New Show" in out                                  # new live show added


def _raise(*a, **k):
    raise RuntimeError("curl fetch returned HTTP 403")


def test_fixture_backed_source_survives_fetch_error(tmp_path, monkeypatch):
    """A raised fetch failure (e.g. curl rejecting a 403) for a fixture-backed source must serve
    the committed fixture, never zero — the regression that briefly lost the Met Opera season."""
    monkeypatch.setattr(L, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(L, "fetch_text", _raise)
    monkeypatch.setattr(L, "enrich_detail_pages", lambda *a, **k: None)
    conn = L.connect()
    src = next(s for s in L.load_sources() if s.id == "met_opera_2026_27")
    n = L.import_html_source(conn, src)
    assert n == len(L.load_capture_fixture(L.MET_OPERA_CAPTURE)) and n > 0  # fixture served, not empty
    status = conn.execute(
        "select status from source_runs where source_id='met_opera_2026_27' order by id desc limit 1"
    ).fetchone()[0]
    assert status == "stale"
