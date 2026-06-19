"""JSON-LD ExhibitionEvent date extraction (the reliable museum date source)."""

import datetime as dt

from cultural_calendar import legacy as L


def _page(start, end):
    return (
        '<html><head><script type="application/ld+json">'
        f'{{"@type":"ExhibitionEvent","name":"X","startDate":"{start}","endDate":"{end}"}}'
        '</script></head><body>© 2026 Museum</body></html>'
    )


def test_jsonld_future_exhibition():
    s, e = L.extract_jsonld_dates(_page("2026-10-14T00:00:00-04:00", "2027-05-01T00:00:00-04:00"))
    assert s == dt.date(2026, 10, 14) and e == dt.date(2027, 5, 1)


def test_jsonld_past_start_returned_so_caller_can_drop():
    # A permanent/old work (e.g. Day's End, Artport) has a past start; the extractor surfaces it
    # and hydrate_museum_dates drops it rather than scanning prose for a misleading date.
    s, _ = L.extract_jsonld_dates(_page("2021-04-29T00:00:00-04:00", "2051-12-31T00:00:00-05:00"))
    assert s == dt.date(2021, 4, 29)


def test_jsonld_absent():
    assert L.extract_jsonld_dates("<html><body>no structured data, © 2026</body></html>") == (None, None)
