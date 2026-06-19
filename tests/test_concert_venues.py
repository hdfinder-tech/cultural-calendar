"""Bargemusic Tribe-API parser test (offline)."""

import datetime as dt
import json

from cultural_calendar import legacy as L
from cultural_calendar.core.config import Source

_SRC = Source(id="bargemusic", name="Bargemusic", category="music", type="html", url="x")


def test_bargemusic_future_only(monkeypatch):
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 19))
    monkeypatch.setattr(L, "end_date", lambda: dt.date(2027, 12, 31))
    payload = json.dumps({"events": [
        {"id": 1, "title": "Mozart Quintet in B-flat", "start_date": "2026-06-20 14:00:00",
         "end_date": "2026-06-20 16:00:00", "url": "https://www.bargemusic.org/concert/x"},
        {"id": 2, "title": "Past Show", "start_date": "2026-01-05 20:00:00",
         "url": "https://www.bargemusic.org/concert/y"},  # already past -> dropped
    ]})
    items = L.parse_bargemusic(_SRC, payload)
    assert [i["title"] for i in items] == ["Mozart Quintet in B-flat"]
    it = items[0]
    assert it["category"] == "music" and it["venue_or_platform"] == "Bargemusic"
    assert it["date_start"] == "2026-06-20" and it["city"] == "New York"
    # Bargemusic routes into the Concerts lane
    assert "bargemusic" in L.CONCERT_MUSIC_SOURCES


_MERKIN_SRC = Source(id="merkin", name="Merkin Hall", category="music", type="html", url="x")


def _merkin_event(slug, presenter, title, datestr):
    return (
        f'<div class="event"><a class="image" href="https://www.kaufmanmusiccenter.org/mch/event/{slug}"></a>'
        f'<div class="description"><div class="kmc-presents">{presenter}</div>'
        f'<h2 class="h3"><a href="https://www.kaufmanmusiccenter.org/mch/event/{slug}">{title}</a></h2>'
        f'<p class="datetime">Friday | {datestr} | 8 pm</p></div></div>'
    )


def test_merkin_keeps_kaufman_presented_only(monkeypatch):
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 1))
    monkeypatch.setattr(L, "end_date", lambda: dt.date(2027, 12, 31))
    monkeypatch.setattr(L, "fetch_text", lambda *a, **k: "")  # no further pagination pages
    page = "<div id='events'>" + "".join([
        _merkin_event("ecstatic", "KAUFMAN MUSIC CENTER PRESENTS", "Ecstatic Music: X", "October 9, 2026"),
        _merkin_event("nyfos", "Kaufman Music Center & NYFOS co-present", "Song of America", "November 1, 2026"),
        _merkin_event("rental", "Scott Siegel Presents", "Broadway by the Seasons", "October 10, 2026"),
        _merkin_event("comp", "Musical Life Foundation presents", "Competition Winners", "October 11, 2026"),
    ]) + "</div>"
    titles = [i["title"] for i in L.parse_merkin(_MERKIN_SRC, page)]
    assert titles == ["Ecstatic Music: X", "Song of America"]   # co-presentation kept; rentals dropped
