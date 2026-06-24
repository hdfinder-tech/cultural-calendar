"""TVmaze relevance + scoring.

Regression: TVmaze renamed Apple's streaming service from 'Apple TV+' to 'Apple TV', which
dropped every Apple premiere at the country gate (a global streamer not on the allowlist is
treated as non-US). Both names must pass and score as a major platform.
"""

from cultural_calendar import legacy as L


def _premiere(channel_name, *, country=None, season=1, number=1, language="English"):
    show = {
        "name": "Some Show",
        "id": 1,
        "network": None,
        "webChannel": {"name": channel_name, "country": country},
        "language": language,
        "type": "Scripted",
        "premiered": "2026-08-05",
        "genres": ["Drama"],
    }
    episode = {"season": season, "number": number, "airdate": "2026-08-05", "name": "Premiere"}
    return episode, show


def test_apple_tv_rename_is_relevant():
    # The current TVmaze name and the legacy name both survive the country/relevance gate.
    for name in ("Apple TV", "Apple TV+"):
        ep, show = _premiere(name)
        assert L.is_relevant_tv_episode(ep, show, "wide") is True, name


def test_apple_tv_scored_as_major_platform():
    apple_now = L.tv_importance_score(*_premiere("Apple TV"))
    apple_old = L.tv_importance_score(*_premiere("Apple TV+"))
    assert apple_now == apple_old        # the rename must not change the score
    assert apple_now >= 50               # a marquee streamer premiere, not the default tier


def test_unlisted_global_streamer_is_dropped():
    # A global webChannel that isn't a prestige streamer (e.g. YouTube) is still filtered out.
    ep, show = _premiere("YouTube")
    assert L.is_relevant_tv_episode(ep, show, "wide") is False


def test_non_premiere_episode_is_dropped():
    # A mid-season episode (not a start signal) is dropped even on a marquee streamer.
    ep, show = _premiere("Apple TV", season=2, number=5)
    assert L.is_relevant_tv_episode(ep, show, "wide") is False
