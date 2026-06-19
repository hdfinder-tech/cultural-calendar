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


def test_jsonld_event_extraction():
    # JALC detail pages: a WP @graph with the real Event node mixed among page-metadata nodes.
    page = ('<script type="application/ld+json">'
            '{"@context":"x","@graph":[{"@type":"WebPage"},'
            '{"@type":"Event","name":"Big Band Holidays &amp; More",'
            '"startDate":"2026-12-15T19:00:00+0000","endDate":"2026-12-20"}]}</script>')
    name, sd = L._jsonld_event(page)
    assert name == "Big Band Holidays & More" and sd == "2026-12-15"
    assert L._jsonld_event("<html>no structured data</html>") == (None, None)


_ABT_SRC = Source(id="abt", name="American Ballet Theatre", category="dance", type="html", url="x")


def test_abt_groups_by_ballet_and_cleans_titles(monkeypatch):
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 1))
    monkeypatch.setattr(L, "end_date", lambda: dt.date(2027, 12, 31))

    def fake_fetch(url, *a, **k):
        if "/events/swan-lake/" in url:
            return '<meta property="og:title" content="Swan Lake | American Ballet Theatre (ABT) - Metropolitan Opera House">'
        if "/events/onegin/" in url:
            return '<meta property="og:title" content="Onegin - Met - American Ballet Theatre">'
        return ""  # supplemental season pages: empty
    monkeypatch.setattr(L, "fetch_text", fake_fetch)
    page = (
        "Metropolitan Opera House"
        '<a href="/event_dates/swan-lake-2026-06-19-730pm/">x</a>'
        '<a href="/event_dates/swan-lake-2026-07-18-200pm/">x</a>'   # range -> opening is earliest
        '<a href="/event_dates/onegin-2026-06-23-730pm/">x</a>'
        '<a href="/event_dates/giselle-2025-01-01-730pm/">past</a>'  # opening past -> dropped
    )
    items = sorted(L.parse_abt(_ABT_SRC, page), key=lambda i: i["date_start"])
    assert [i["title"] for i in items] == ["Swan Lake", "Onegin"]   # dash/pipe junk stripped; past dropped
    swan = items[0]
    assert swan["date_start"] == "2026-06-19" and "–" in swan["date_label"]
    assert swan["venue_or_platform"] == "Metropolitan Opera House" and swan["category"] == "ballet"


_SFTC_SRC = Source(id="summer_city", name="Summer for the City", category="music", type="html", url="x")


def _sftc_card(date, slug, title, subseries, discipline, venue_html):
    return (
        f'<h4 class="event-date"> {date} </h4>'
        '<h2 class="presenting-organization"><a href="/series/summer-for-the-city">Summer for the City</a><br />'
        f'<a href="/series/summer-for-the-city/s/{subseries.replace(" ", "%20")}">{subseries}</a></h2>'
        f'<h2 class="event-title"><a href="/series/summer-for-the-city/{slug}">{title}</a></h2>'
        f'<h2 class="presenting-organization more-constituent-info"><img class="show-card-loction-icon" src="x">{venue_html}</h2>'
        '<div class="vs-show-short-description">desc</div>'
        f'<div class="show-icons-item-text">{discipline}</div>'
    )


def test_summer_city_marquee_filter_and_fields(monkeypatch):
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 1))
    monkeypatch.setattr(L, "end_date", lambda: dt.date(2027, 12, 31))
    page = (
        "UPCOMING SHOWS"
        # Festival Orchestra is tagged CLASSICAL (not MUSIC) — must still be kept, as music
        + _sftc_card("Saturday, June 20 at 7:30 pm", "fest-orch-1", "Festival Orchestra: Mozart",
                     "Festival Orchestra of Lincoln Center", "CLASSICAL", '<a href="/venue/x">Alice Tully Hall</a>')
        # Silent Disco is not a performance subseries -> dropped
        + _sftc_card("Sunday, June 21 at 12:00 pm", "silent-disco-9", "Silent Disco",
                     "Silent Disco", "DISCO", "The Dance Floor")
    )
    items = L.parse_summer_city(_SFTC_SRC, page)
    assert [i["title"] for i in items] == ["Festival Orchestra: Mozart"]
    it = items[0]
    assert it["category"] == "music" and it["venue_or_platform"] == "Alice Tully Hall"
    assert it["date_start"] == "2026-06-20"
    assert "Summer for the City" in it["description"]
    assert "Festival Orchestra of Lincoln Center" in it["description"]
