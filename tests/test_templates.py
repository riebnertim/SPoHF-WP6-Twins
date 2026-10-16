"""Tests for wp6_data.shared.templates."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from wp6_data.shared.metadata import (
    DeviceMetadata,
    MetadataRegistry,
    SensorMetadata,
    TwinMetadata,
)
from wp6_data.shared.templates import (
    DEFAULT_ALL_START,
    build_explore_tabs,
    configure_dashboard,
    default_date_range,
    render_date_filter,
    render_explore_tabs,
    render_page,
    render_stat_grid,
    render_stat_tile,
    render_unified_chart_page,
    resolve_date_range,
)
from wp6_data.shared.templates import config as templates_config


@pytest.fixture(autouse=True)
def _configured_dashboard():
    """Give page renderers a dashboard identity, restored after each test."""
    saved = (
        templates_config._dashboard_id,
        templates_config._dashboard_title,
        templates_config._twin_theme_css,
        templates_config._data_sources,
    )
    configure_dashboard("test")
    yield
    (
        templates_config._dashboard_id,
        templates_config._dashboard_title,
        templates_config._twin_theme_css,
        templates_config._data_sources,
    ) = saved


def _registry(
    sensor_defaults: dict[str, SensorMetadata] | None = None,
    devices: dict[str, DeviceMetadata] | None = None,
) -> MetadataRegistry:
    """Build an in-memory MetadataRegistry for tests (bypasses YAML)."""
    reg = MetadataRegistry.__new__(MetadataRegistry)
    reg._meta = TwinMetadata(
        sensor_defaults=sensor_defaults or {},
        devices=devices or {},
    )
    return reg


class TestResolveDateRange:
    def test_applies_defaults_when_none(self):
        start, end, start_dt, end_dt = resolve_date_range(None, None)
        assert start == end - timedelta(days=7)
        assert start_dt.hour == 0 and start_dt.minute == 0
        assert end_dt.hour == 23 and end_dt.minute == 59 and end_dt.second == 59

    def test_uses_provided_dates(self):
        s, e = date(2026, 6, 1), date(2026, 6, 10)
        start, end, start_dt, end_dt = resolve_date_range(s, e)
        assert start == s
        assert end == e
        assert start_dt.year == 2026 and start_dt.month == 6 and start_dt.day == 1
        assert end_dt.day == 10

    def test_partial_override_start_only(self):
        s = date(2026, 1, 1)
        start, end, _, _ = resolve_date_range(s, None)
        assert start == s
        assert end == date.today()

    def test_partial_override_end_only(self):
        e = date(2026, 12, 31)
        start, end, _, _ = resolve_date_range(None, e)
        assert end == e
        assert start == date.today() - timedelta(days=7)

    def test_datetimes_are_utc(self):
        from datetime import UTC

        _, _, start_dt, end_dt = resolve_date_range(date(2026, 3, 1), date(2026, 3, 2))
        assert start_dt.tzinfo is UTC
        assert end_dt.tzinfo is UTC


class TestDefaultDateRange:
    @patch("wp6_data.shared.templates.components.date")
    def test_returns_7_day_window(self, mock_date):
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        start, end = default_date_range()
        assert end == date(2026, 3, 15)
        assert start == date(2026, 3, 8)

    def test_start_before_end(self):
        start, end = default_date_range()
        assert start < end
        assert (end - start).days == 7


class TestRenderDateFilter:
    def test_contains_form_element(self):
        html = render_date_filter(date(2026, 1, 1), date(2026, 1, 8))
        assert '<form id="dateFilter"' in html
        assert 'method="get"' in html

    def test_contains_all_preset_buttons(self):
        html = render_date_filter(date(2026, 1, 1), date(2026, 1, 8))
        for label in ("1d", "7d", "30d", "90d", "1y", "All"):
            assert f">{label}</button>" in html

    def test_date_inputs_have_correct_values(self):
        start = date(2026, 2, 1)
        end = date(2026, 2, 10)
        html = render_date_filter(start, end)
        assert 'value="2026-02-01"' in html
        assert 'value="2026-02-10"' in html

    @patch("wp6_data.shared.templates.components.date")
    def test_active_preset_7d(self, mock_date):
        today = date(2026, 3, 15)
        mock_date.today.return_value = today
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        start = today - timedelta(days=7)
        html = render_date_filter(start, today)
        # 7d button should have active style (contrast class)
        assert 'class="contrast" onclick="setRange(7)">7d</button>' in html

    @patch("wp6_data.shared.templates.components.date")
    def test_active_preset_all(self, mock_date):
        today = date(2026, 3, 15)
        mock_date.today.return_value = today
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        html = render_date_filter(DEFAULT_ALL_START, today)
        assert 'class="contrast" onclick="setRange(null)">All</button>' in html

    @patch("wp6_data.shared.templates.components.date")
    def test_all_start_moves_where_all_reaches_back_to(self, mock_date):
        """A page that knows when its data begins says so, and "All" honours it."""
        today = date(2026, 3, 15)
        mock_date.today.return_value = today
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        first_reading = date(2025, 11, 4)

        html = render_date_filter(first_reading, today, all_start=first_reading)

        # The preset button lands on the data, not on the shared epoch.
        assert f"new Date('{first_reading.isoformat()}')" in html
        assert DEFAULT_ALL_START.isoformat() not in html
        # ...and the page loaded on that range is recognised as "All".
        assert 'class="contrast" onclick="setRange(null)">All</button>' in html

    @patch("wp6_data.shared.templates.components.date")
    def test_all_start_defaults_to_the_shared_epoch(self, mock_date):
        today = date(2026, 3, 15)
        mock_date.today.return_value = today
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        html = render_date_filter(date(2026, 1, 1), today)
        assert f"new Date('{DEFAULT_ALL_START.isoformat()}')" in html

    @patch("wp6_data.shared.templates.components.date")
    def test_no_active_preset_for_custom_range(self, mock_date):
        today = date(2026, 3, 15)
        mock_date.today.return_value = today
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        html = render_date_filter(date(2026, 2, 1), date(2026, 2, 15))
        # No button should have the active (contrast) class
        assert 'class="contrast"' not in html

    def test_extra_params_rendered_as_hidden_fields(self):
        html = render_date_filter(
            date(2026, 1, 1), date(2026, 1, 8), extra_params={"sensor": "temp", "device": "d1"}
        )
        assert '<input type="hidden" name="sensor" value="temp">' in html
        assert '<input type="hidden" name="device" value="d1">' in html

    def test_setrange_script_present(self):
        html = render_date_filter(date(2026, 1, 1), date(2026, 1, 8))
        assert "function setRange(days)" in html


class TestRenderStatGrid:
    def test_tuple_tile_with_sublabel_and_cols(self):
        html = render_stat_grid([("12.3", "Avg DLI", "mol/m²/day")], cols=4)
        assert '<div class="stats-grid cols-4">' in html
        assert '<div class="stat-value">12.3</div>' in html
        assert "<small>Avg DLI</small>" in html
        assert "<br><small>mol/m²/day</small>" in html

    def test_prerendered_tile_passthrough_with_value_class(self):
        tile = render_stat_tile("7", "Occlusion days", value_class="warning")
        html = render_stat_grid([("1.0", "Median"), tile])
        assert '<div class="stats-grid">' in html
        assert tile in html
        assert '<div class="stat-value warning">7</div>' in html


class TestRenderPage:
    def test_minimal_page_structure(self):
        html = render_page("Test", "<p>Hello</p>", show_logo=False, show_footer=False)
        assert "<!DOCTYPE html>" in html
        assert "<title>Test</title>" in html
        assert "<p>Hello</p>" in html

    def test_nav_bar_rendered(self):
        html = render_page("T", "content")
        assert "dashboard-nav" in html
        assert "theme-toggle" in html

    def test_dashboard_identity_is_the_configured_one(self):
        html = render_page("T", "content")
        assert 'data-dashboard="test"' in html

    def test_render_without_configuration_fails_loud(self):
        templates_config._dashboard_id = None
        with pytest.raises(RuntimeError, match="configure_dashboard"):
            render_page("T", "content")

    def test_footer_shown_by_default(self):
        html = render_page("T", "content")
        assert "<footer>" in html

    def test_footer_hidden(self):
        html = render_page("T", "content", show_footer=False)
        assert "<footer>" not in html

    def test_back_link_hidden_by_default(self):
        html = render_page("T", "content")
        assert 'class="back"' not in html

    def test_back_link_shown(self):
        html = render_page("T", "content", show_back_link=True)
        assert 'class="back"' in html
        assert 'href="/"' in html

    def test_custom_back_url(self):
        html = render_page("T", "content", show_back_link=True, back_url="/home")
        assert 'href="/home"' in html

    def test_extra_css_included(self):
        html = render_page("T", "content", extra_css=".custom { color: red; }")
        assert ".custom { color: red; }" in html

    def test_pico_css_always_included(self):
        html = render_page("T", "content")
        assert "pico.classless.min.css" in html


class TestRenderUnifiedChartPage:
    def test_contains_chart_layout(self):
        html = render_unified_chart_page("Test", date(2026, 1, 1), date(2026, 1, 8))
        assert "chart-layout" in html
        assert "sensor-panel" in html
        assert "chart-area" in html

    def test_contains_date_filter(self):
        html = render_unified_chart_page("Test", date(2026, 1, 1), date(2026, 1, 8))
        assert "dateFilter" in html
        assert 'value="2026-01-01"' in html

    def test_contains_plotly_script(self):
        html = render_unified_chart_page("Test", date(2026, 1, 1), date(2026, 1, 8))
        assert "plotly" in html.lower()

    def test_contains_grouping_toggle(self):
        html = render_unified_chart_page("Test", date(2026, 1, 1), date(2026, 1, 8))
        assert "By type" in html
        assert "By device" in html

    def test_contains_clear_button(self):
        html = render_unified_chart_page("Test", date(2026, 1, 1), date(2026, 1, 8))
        assert "clear-all" in html

    def test_contains_panel_toggle(self):
        html = render_unified_chart_page("Test", date(2026, 1, 1), date(2026, 1, 8))
        assert "panel-toggle" in html
        assert "Hide controls" in html


class TestBuildExploreTabs:
    def test_returns_three_tabs(self):
        tabs = build_explore_tabs(_registry(), {}, {})
        assert set(tabs) == {"devices", "sensors", "manual"}

    def test_splits_sensors_by_source(self):
        registry = _registry(
            sensor_defaults={
                "par": SensorMetadata(type="radiation", unit="μmol", alias="PAR"),
                "chlorophyll": SensorMetadata(
                    type="chemistry", unit="mg/L",
                    alias="Chl", source="sijia",
                ),
            },
            devices={
                "s2100-01-par": DeviceMetadata(position="B4"),
                "neurath-B-2034-strabelina": DeviceMetadata(source="sijia"),
            },
        )
        device_data = {
            "s2100-01-par": {"sensors": ["par"], "readings": 100},
            "neurath-B-2034-strabelina": {
                "sensors": ["chlorophyll"], "readings": 50,
            },
        }
        tabs = build_explore_tabs(registry, device_data, {})

        # Auto sensor only on Sensors tab
        assert "PAR" in tabs["sensors"]
        assert "Chl" not in tabs["sensors"]
        # Manual sensor only on Manual tab
        assert "Chl" in tabs["manual"]
        assert "PAR" not in tabs["manual"]

    def test_automated_source_is_shown_as_automated(self):
        """A sync-ingested source (red's LetsGrow) routes by `source` but is not manual."""
        registry = _registry(
            sensor_defaults={
                "lg_vpd": SensorMetadata(type="vpd", alias="LG VPD", source="letsgrow"),
            },
            devices={"letsgrow-climate-b4": DeviceMetadata(source="letsgrow")},
        )
        registry._meta.automated_sources = ["letsgrow"]
        device_data = {"letsgrow-climate-b4": {"sensors": ["lg_vpd"], "readings": 7}}

        tabs = build_explore_tabs(registry, device_data, {})

        assert "LG VPD" in tabs["sensors"]
        assert "LG VPD" not in tabs["manual"]
        assert "letsgrow-climate-b4" in tabs["devices"]

    def test_devices_tab_excludes_manual_source_devices(self):
        registry = _registry(
            sensor_defaults={
                "par": SensorMetadata(type="radiation"),
                "chlorophyll": SensorMetadata(type="chemistry", source="sijia"),
            },
            devices={
                "s2100-01-par": DeviceMetadata(position="B4"),
                "neurath-B-2034-strabelina": DeviceMetadata(source="sijia"),
            },
        )
        device_data = {
            "s2100-01-par": {"sensors": ["par"], "readings": 100},
            "neurath-B-2034-strabelina": {
                "sensors": ["chlorophyll"], "readings": 50,
            },
        }
        tabs = build_explore_tabs(registry, device_data, {})

        assert "s2100-01-par" in tabs["devices"]
        assert "neurath-B-2034-strabelina" not in tabs["devices"]

    def test_manual_tab_empty_state_when_no_manual_sensors(self):
        registry = _registry(
            sensor_defaults={"par": SensorMetadata(type="radiation")},
            devices={"s2100-01-par": DeviceMetadata()},
        )
        device_data = {"s2100-01-par": {"sensors": ["par"], "readings": 1}}
        tabs = build_explore_tabs(registry, device_data, {})
        assert "No manual measurements" in tabs["manual"]


class TestManualMeasurementTab:
    def test_renders_with_source_grouping_columns(self):
        registry = _registry(
            sensor_defaults={
                "chlorophyll": SensorMetadata(
                    type="fruit chemistry", unit="mg/L",
                    alias="Chlorophyll", source="sijia",
                ),
            },
            devices={
                "neurath-B-2034-strabelina": DeviceMetadata(source="sijia"),
            },
        )
        device_data = {
            "neurath-B-2034-strabelina": {
                "sensors": ["chlorophyll"], "readings": 12, "last_seen": None,
            },
        }
        tabs = build_explore_tabs(registry, device_data, {})
        manual_html = tabs["manual"]
        # Headers include Source first, plus Type/Measurement/Unit/Readings
        # /Last upload/Last measure
        for header in (
            "Source", "Type", "Measurement", "Unit", "Readings",
            "Last upload", "Last measure",
        ):
            assert f">{header}</th>" in manual_html, header
        # Group column attribute is present (Source is col 0)
        assert 'data-group-col="0"' in manual_html
        # Source label sijia appears (no link, plain bold text)
        assert "<strong>sijia</strong>" in manual_html

    def test_empty_manual_keeps_descriptive_message(self):
        registry = _registry(
            sensor_defaults={"par": SensorMetadata(type="radiation")},
            devices={"s2100-01-par": DeviceMetadata()},
        )
        device_data = {"s2100-01-par": {
            "sensors": ["par"], "readings": 1, "last_seen": None,
        }}
        tabs = build_explore_tabs(registry, device_data, {})
        assert "No manual measurements" in tabs["manual"]
        # Empty state is a paragraph, not a table
        assert "<table" not in tabs["manual"]


class TestRenderExploreTabs:
    def test_renders_all_three_tab_buttons(self):
        html = render_explore_tabs(
            {"devices": "<p>D</p>", "sensors": "<p>S</p>", "manual": "<p>M</p>"},
        )
        assert 'data-tab="devices"' in html
        assert 'data-tab="sensors"' in html
        assert 'data-tab="manual"' in html
        assert "Manual measurements" in html

    def test_default_active_tab_is_devices(self):
        html = render_explore_tabs({"devices": "", "sensors": "", "manual": ""})
        assert 'data-tab="devices" aria-selected="true"' in html
        assert 'data-tab="sensors" aria-selected="false"' in html

    def test_query_param_drives_active_tab(self):
        html = render_explore_tabs(
            {"devices": "", "sensors": "", "manual": ""}, active="manual",
        )
        assert 'data-tab="manual" aria-selected="true"' in html
        assert 'data-tab="devices" aria-selected="false"' in html

    def test_invalid_active_falls_back_to_devices(self):
        html = render_explore_tabs(
            {"devices": "", "sensors": "", "manual": ""}, active="nonsense",
        )
        assert 'data-tab="devices" aria-selected="true"' in html

    def test_inactive_panels_are_hidden(self):
        html = render_explore_tabs(
            {"devices": "<p>D</p>", "sensors": "<p>S</p>", "manual": "<p>M</p>"},
            active="sensors",
        )
        assert 'data-tab-panel="devices" hidden' in html
        assert 'data-tab-panel="sensors">' in html
        assert 'data-tab-panel="manual" hidden' in html
