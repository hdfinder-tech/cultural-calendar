"""Marian Goodman listing parser tests (pure, offline)."""

import datetime as dt

from cultural_calendar import legacy as L
from cultural_calendar.core.config import Source

_SRC = Source(id="marian_goodman", name="Marian Goodman (NY)", category="art", type="html",
              url="https://www.mariangoodman.com/exhibitions/")


def _card(href, location, artist, subtitle, date):
    return (
        f'class="area"> <a href="{href}"> <div class="prelude ani-in">'
        f'<span class="location">{location}</span></div>'
        f'<h2 class="ani-in"><span class="heading_title">{artist}</span></h2>'
        f'<div class="subheading ani-in">{subtitle}</div> </a>'
        f'<div class="content ani-in"></div><div class="bottom ani-in"> {date} </div>'
    )


def test_parser_ny_future_only(monkeypatch):
    monkeypatch.setattr(L, "today", lambda: dt.date(2026, 6, 19))
    monkeypatch.setattr(L, "end_date", lambda: dt.date(2027, 12, 31))
    page = "<html>" + "".join([
        _card("/exhibitions/630-matt-saunders/", "New York", "Matt Saunders", "On an Overgrown Path", "25 June - 7 August 2026"),
        _card("/exhibitions/626-julie-mehretu/", "New York", "Julie Mehretu", "Our Days", "14 April - 6 June 2026"),  # already closed
        _card("/exhibitions/627-ettore-spalletti/", "Paris", "Ettore Spalletti", "X", "3 July - 26 September 2026"),  # not NY
    ]) + "</html>"
    items = L.parse_marian_goodman(_SRC, page)
    titles = [i["title"] for i in items]
    assert titles == ["Matt Saunders: On an Overgrown Path"]
    it = items[0]
    assert it["category"] == "art" and it["venue_or_platform"] == "Marian Goodman" and it["city"] == "New York"
    assert it["date_start"] == "2026-06-25" and it["date_end"] == "2026-08-07"
    assert it["source_url"].endswith("/exhibitions/630-matt-saunders/")
