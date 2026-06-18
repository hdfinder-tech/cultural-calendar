"""Registry / dispatch tests: every source resolves to a valid plugin + health range."""

from cultural_calendar.core.config import load_sources
from cultural_calendar.registry import plugin_for, EXPECTED_ROWS
from cultural_calendar.sources.base import TACTICS


def test_every_enabled_source_has_plugin_and_tactic():
    for source in load_sources():
        plugin = plugin_for(source)
        assert plugin.id == source.id
        assert plugin.tactic in TACTICS, f"{source.id} has bad tactic {plugin.tactic}"
        assert callable(plugin.importer)


def test_sources_json_type_matches_registry_tactic():
    """sources.json `type` is informational, but must not contradict the real dispatch tactic
    (api↔json_api; html↔html/capture/embedded_json) — it's a handoff artifact."""
    allowed = {"api": {"json_api"}, "html": {"html", "capture", "embedded_json"}}
    for source in load_sources():
        tactic = plugin_for(source).tactic
        assert tactic in allowed.get(source.type, set()), \
            f"{source.id}: type={source.type} contradicts tactic={tactic}"


def test_expected_rows_cover_every_enabled_source():
    ids = {s.id for s in load_sources()}
    missing = ids - set(EXPECTED_ROWS)
    assert not missing, f"missing expected_rows ranges for {missing}"


def test_health_flags_out_of_range():
    for source in load_sources():
        plugin = plugin_for(source)
        if plugin.expected_rows and plugin.expected_rows[0] > 0:
            assert plugin.health(0) is not None  # a source breaking to 0 must warn
            return
