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
