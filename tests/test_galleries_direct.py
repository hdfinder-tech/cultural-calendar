"""Direct gallery parsers: shared date parser + Lisson + Tanya Bonakdar (offline)."""

import datetime as dt

from cultural_calendar import legacy as L
from cultural_calendar.core.config import Source

_SRC = Source(id="x", name="x", category="art", type="html", url="x")


def test_show_dates_us_uk_and_dayless():
    assert L.parse_show_dates("April 30 - July 31, 2026")[:2] == ("2026-04-30", "2026-07-31")
    assert L.parse_show_dates("1 May – 25 July 2026")[:2] == ("2026-05-01", "2026-07-25")
    s, e, label, prec = L.parse_show_dates("September – October 2026")  # day-less start
    assert s == "2026-09-01" and prec == "month"
    assert L.parse_show_dates("no date here") is None


def test_lisson_ny_future(monkeypatch):
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 19))
    monkeypatch.setattr(L, "end_date", lambda: dt.date(2027, 12, 31))
    page = (
        '<a class="link-discreet" href="/exhibitions/sugimoto"> Hiroshi Sugimoto <br/> September – October 2026 <br/> New York </a>'
        '<a class="link-discreet" href="/exhibitions/ken-price"> Ken Price <br/> 1 May – 25 July 2026 <br/> London </a>'
        '<a class="link-discreet" href="/exhibitions/akashi"> Kelly Akashi: <br/> Heirloom <br/> New York </a>'
    )
    items = L.parse_lisson(_SRC, page)
    titles = [i["title"] for i in items]
    assert titles == ["Hiroshi Sugimoto"]            # NY + future; London excluded; dateless excluded
    assert items[0]["venue_or_platform"] == "Lisson Gallery" and items[0]["date_start"] == "2026-09-01"


def test_tanya_bonakdar_ny_future(monkeypatch):
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 19))
    monkeypatch.setattr(L, "end_date", lambda: dt.date(2027, 12, 31))
    page = (
        '<li><a href="/exhibitions/999-jane-doe-mirage-tanya-bonakdar-gallery-new-york/">'
        '<span class="date">September 9 - October 14, 2026</span></a></li>'
        '<li><a href="/exhibitions/998-past-show-tanya-bonakdar-gallery-new-york/">'
        '<span class="date">January 8 - February 12, 2026</span></a></li>'   # past
        '<li><a href="/exhibitions/997-la-artist-tanya-bonakdar-gallery-los-angeles/">'
        '<span class="date">September 9 - October 14, 2026</span></a></li>'  # LA, not NY
    )
    items = L.parse_tanya_bonakdar(_SRC, page)
    assert [i["title"] for i in items] == ["Jane Doe Mirage"]
    assert items[0]["date_start"] == "2026-09-09" and items[0]["venue_or_platform"] == "Tanya Bonakdar"
