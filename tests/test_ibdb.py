"""IBDB forward-Broadway parser tests (pure, no network)."""

from cultural_calendar import legacy as L

LISTING = """
<h2>Productions Current & Upcoming</h2>
<a href="/broadway-production/inter-alia-546489">Inter Alia</a>
<a href="/broadway-production/galileo-545785">Galileo</a>
<a href="/broadway-production/inter-alia-546489">Inter Alia</a>
<h2>Opening Nights in History — Today</h2>
<a href="/broadway-production/ziegfeld-follies-of-1909-6662">Ziegfeld Follies</a>
"""


def test_listing_scopes_to_current_and_upcoming():
    slugs = L.parse_ibdb_listing(LISTING)
    # de-duplicated, in order, and the historical "Opening Nights" block is excluded
    assert slugs == ["inter-alia-546489", "galileo-545785"]
    assert not any("ziegfeld" in s for s in slugs)


def _detail(opening_value):
    return (
        '<title>Inter Alia – Broadway Play – Original | IBDB</title>'
        f'<div class="xt-lable">Opening Date</div><div class="xt-main-title">{opening_value}</div>'
    )


def test_opening_date_firm():
    assert L.ibdb_opening_date(_detail("Dec 01, 2026")) == ("2026-12-01", "exact", "Dec 1, 2026")


def test_opening_date_vague_month():
    assert L.ibdb_opening_date(_detail("Sep 2026")) == ("2026-09-01", "month", "Sep 2026")


def test_opening_date_tbd_is_none():
    assert L.ibdb_opening_date(_detail("TBD")) is None
    assert L.ibdb_opening_date(_detail("2027")) is None


def test_title_and_type():
    d = _detail("Dec 01, 2026")
    assert L.ibdb_title(d) == "Inter Alia"
    assert L.ibdb_show_type(d) == "Play"
