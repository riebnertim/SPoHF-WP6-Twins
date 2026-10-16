"""Tests for the LetsGrow feed: relay rows -> red readings.

Covers the mapping (`red.letsgrow`), the declarations that make the rows
visible on the red dashboard (`red/metadata.yaml`), and the sync plumbing that
routes `letsgrow-data` to red's database rather than blue's.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wp6_data.api.models import LetsGrowReading, LetsGrowResponse
from wp6_data.db.queries import upsert_readings
from wp6_data.red import letsgrow
from wp6_data.red.provider import RedSensorProvider
from wp6_data.shared.metadata import MetadataRegistry
from wp6_data.sync.feeds import LETSGROW, YOOKR, feed_for
from wp6_data.sync.orchestrator import SyncOrchestrator

RED_YAML = Path(__file__).parent.parent / "src" / "wp6_data" / "red" / "metadata.yaml"

# A row exactly as the relay serves it (2026-10-16).
RELAY_ROW = {
    "module_id": 39887,
    "module_name": "Climate B4",
    "col_id": 1467902,
    "description": "Temperature greenhouse - °C - 5 min",
    "timestamp": "2026-10-16T00:00:00",
    "value": 17.2,
    "offset": 0,
    "synced_at": "2026-10-16T08:30:04.156869",
}


def _reading(**overrides) -> LetsGrowReading:
    return LetsGrowReading.model_validate({**RELAY_ROW, **overrides})


@pytest.fixture()
def red_metadata() -> MetadataRegistry:
    return MetadataRegistry(RED_YAML)


# --- mapping ---


class TestMapping:
    def test_relay_page_parses(self):
        page = LetsGrowResponse.model_validate({"results": [RELAY_ROW], "count": 1})
        assert page.results[0].col_id == 1467902
        assert page.results[0].value == 17.2

    def test_device_is_slugged_module_name(self):
        assert letsgrow.device_name("Climate B4") == "letsgrow-climate-b4"
        assert letsgrow.device_name("Weather station") == "letsgrow-weather-station"

    def test_declared_column_maps_to_its_tag(self):
        assert letsgrow.sensor_tag(1467902) == "lg_temp_greenhouse"

    def test_undeclared_column_is_kept_under_its_id(self):
        """A new LetsGrow column must be stored, not dropped."""
        assert letsgrow.sensor_tag(999) == "lg_col_999"
        params = letsgrow.to_params(_reading(col_id=999))
        assert params["sensor_tag"] == "lg_col_999"

    def test_summer_local_time_is_two_hours_ahead_of_utc(self):
        ts = letsgrow.measured_at(_reading(timestamp="2026-07-01T14:00:00"))
        assert ts == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    def test_winter_local_time_is_one_hour_ahead_of_utc(self):
        ts = letsgrow.measured_at(_reading(timestamp="2026-01-15T14:00:00"))
        assert ts == datetime(2026, 1, 15, 13, 0, tzinfo=UTC)

    def test_to_params_matches_upsert_shape(self):
        params = letsgrow.to_params(_reading())
        assert params == {
            "device_name": "letsgrow-climate-b4",
            "sensor_tag": "lg_temp_greenhouse",
            "value": "17.2",
            # Local midnight in October (CEST) is 22:00 UTC the day before.
            "datetime_measure": "2026-10-15T22:00:00+00:00",
        }

    def test_missing_value_becomes_empty_raw_value(self):
        assert letsgrow.to_params(_reading(value=None))["value"] == ""


# --- metadata declarations ---


class TestMetadata:
    def test_every_mapped_tag_is_declared_as_letsgrow(self, red_metadata):
        for tag in set(letsgrow.COLUMNS.values()):
            assert red_metadata.sensor_default(tag).source == letsgrow.SOURCE, tag

    def test_tags_never_collide_with_legacy_mysql_tags(self, red_metadata):
        """Red routes by tag: a LetsGrow tag equal to a MySQL tag would hijack it."""
        unsourced = {t for t, m in red_metadata.sensor_defaults.items() if not m.source}
        assert not unsourced & set(letsgrow.COLUMNS.values())

    def test_letsgrow_is_automated_not_manual(self, red_metadata):
        assert not red_metadata.is_manual_source(letsgrow.SOURCE)
        assert red_metadata.is_manual_source("sijia")

    def test_device_sensors_are_declared_tags(self, red_metadata):
        tags = set(letsgrow.COLUMNS.values())
        devices = {
            d: m for d, m in red_metadata.devices.items() if m.source == letsgrow.SOURCE
        }
        assert devices
        for device, meta in devices.items():
            assert set(meta.sensors) <= tags, device

    def test_provider_lists_only_each_modules_own_sensors(self, red_metadata):
        pairs = set(RedSensorProvider(red_metadata)._enumerate_tsdb_sensors())
        assert ("letsgrow-climate-b4", "lg_vpd") in pairs
        assert ("letsgrow-weather-station", "lg_vpd") not in pairs
        # Sijia plants still carry every Sijia tag.
        assert ("neurath-B-2034-strabelina", "brix") in pairs

    def test_provider_routes_letsgrow_tags_to_tsdb(self, red_metadata):
        provider = RedSensorProvider(red_metadata)
        assert "lg_temp_greenhouse" in provider.tsdb_sensors
        assert "temp" not in provider.tsdb_sensors


# --- persistence ---


def _conn():
    conn = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(True,))
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=cursor)
    ctx.__aexit__ = AsyncMock(return_value=False)
    conn.cursor = MagicMock(return_value=ctx)
    return conn, cursor


class TestUpsertWithSource:
    @pytest.mark.asyncio()
    async def test_source_is_written_and_part_of_the_conflict_key(self):
        conn, cursor = _conn()
        await upsert_readings(conn, [letsgrow.to_params(_reading())], source="letsgrow")

        query, params = cursor.execute.await_args.args
        assert "source" in query.split("VALUES")[0]
        assert "ON CONFLICT (source, device_name, sensor_tag, time)" in query
        assert params["source"] == "letsgrow"

    @pytest.mark.asyncio()
    async def test_without_source_blue_query_is_unchanged(self):
        conn, cursor = _conn()
        await upsert_readings(conn, [letsgrow.to_params(_reading())])

        query, params = cursor.execute.await_args.args
        assert "ON CONFLICT (device_name, sensor_tag, time)" in query
        assert "source" not in params


# --- sync routing ---


class TestFeedRouting:
    def test_letsgrow_feed_belongs_to_red(self):
        assert feed_for("letsgrow-data") is LETSGROW
        assert LETSGROW.twin == "red"
        assert LETSGROW.source == "letsgrow"

    def test_unknown_endpoint_falls_back_to_blue_yookr(self):
        assert feed_for("yookr-data") is YOOKR
        assert feed_for("something-new") is YOOKR

    def test_letsgrow_reads_red_dsn_not_blue(self, monkeypatch, mock_settings):
        monkeypatch.setenv("WP6_RED_TSDB_URL", "postgresql://red/wp6_red")
        assert LETSGROW.dsn(mock_settings) == "postgresql://red/wp6_red"


def _orch(mock_settings):
    orch = SyncOrchestrator.__new__(SyncOrchestrator)
    orch.settings = mock_settings
    orch.client = MagicMock()
    return orch


class TestRunPerTwin:
    @pytest.mark.asyncio()
    async def test_each_twin_gets_its_own_pool_and_schema(self, mock_settings):
        mock_settings.endpoint_list = ["yookr-data", "letsgrow-data"]
        blue = replace(YOOKR, ensure_schema=AsyncMock(), dsn=lambda _s: "blue-dsn")
        red = replace(LETSGROW, ensure_schema=AsyncMock(), dsn=lambda _s: "red-dsn")
        orch = _orch(mock_settings)

        with (
            patch(
                "wp6_data.sync.orchestrator.feed_for",
                side_effect=lambda ep: red if ep == "letsgrow-data" else blue,
            ),
            patch.object(orch, "_sync_endpoint", new_callable=AsyncMock, return_value=3),
            patch(
                "wp6_data.sync.orchestrator.init_pool", new_callable=AsyncMock
            ) as init_pool,
            patch("wp6_data.sync.orchestrator.refresh_sensor_summary_recent", new=AsyncMock()),
            patch("wp6_data.sync.orchestrator.close_pool", new=AsyncMock()) as close_pool,
        ):
            stats = await orch.run()

        assert [c.args[0] for c in init_pool.await_args_list] == ["blue-dsn", "red-dsn"]
        blue.ensure_schema.assert_awaited_once()
        red.ensure_schema.assert_awaited_once()
        assert close_pool.await_count == 2
        assert stats["endpoints"] == {"yookr-data": 3, "letsgrow-data": 3}

    @pytest.mark.asyncio()
    async def test_unreachable_red_db_does_not_stop_blue(self, mock_settings):
        mock_settings.endpoint_list = ["letsgrow-data", "yookr-data"]
        blue = replace(YOOKR, ensure_schema=AsyncMock(), dsn=lambda _s: "blue-dsn")
        red = replace(LETSGROW, ensure_schema=AsyncMock(), dsn=lambda _s: "red-dsn")
        orch = _orch(mock_settings)

        async def init_pool(dsn):
            if dsn == "red-dsn":
                raise ConnectionError("red down")
            return MagicMock()

        with (
            patch(
                "wp6_data.sync.orchestrator.feed_for",
                side_effect=lambda ep: red if ep == "letsgrow-data" else blue,
            ),
            patch.object(orch, "_sync_endpoint", new_callable=AsyncMock, return_value=5),
            patch("wp6_data.sync.orchestrator.init_pool", side_effect=init_pool),
            patch("wp6_data.sync.orchestrator.refresh_sensor_summary_recent", new=AsyncMock()),
            patch("wp6_data.sync.orchestrator.close_pool", new=AsyncMock()),
        ):
            stats = await orch.run()

        assert stats["endpoints"] == {"yookr-data": 5}
        assert stats["errors"] == ["letsgrow-data: red down"]


class TestSyncEndpointLetsGrow:
    @pytest.mark.asyncio()
    async def test_rows_are_mapped_stamped_and_watermarked_in_utc(self, mock_settings):
        mock_settings.sync_mode = "incremental"
        orch = _orch(mock_settings)

        conn = AsyncMock()
        pool = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.connection = MagicMock(return_value=ctx)

        seen_kwargs = {}

        async def one_row(*args, **kwargs):
            seen_kwargs.update(kwargs)
            yield _reading()

        orch.client.fetch_window = one_row
        window = (datetime(2026, 10, 15, tzinfo=UTC), datetime(2026, 10, 16, tzinfo=UTC))

        with (
            patch("wp6_data.sync.orchestrator.SyncStateManager") as MockState,
            patch("wp6_data.sync.orchestrator.get_pool", return_value=pool),
            patch(
                "wp6_data.sync.orchestrator.upsert_readings",
                new_callable=AsyncMock,
                return_value=(1, 1),
            ) as upsert,
            patch("wp6_data.sync.orchestrator._generate_windows", return_value=iter([window])),
        ):
            state = AsyncMock()
            MockState.return_value = state
            count = await orch._sync_endpoint("letsgrow-data")

        assert count == 1
        assert seen_kwargs["response_model"] is LetsGrowResponse
        batch = upsert.await_args.args[1]
        assert batch[0]["device_name"] == "letsgrow-climate-b4"
        assert upsert.await_args.kwargs["source"] == "letsgrow"
        state.update_timestamp.assert_awaited_once_with(
            datetime(2026, 10, 15, 22, 0, tzinfo=UTC), 1
        )

    @pytest.mark.asyncio()
    async def test_mid_window_flushes_carry_the_source(self, mock_settings):
        """A window bigger than one batch flushes early; those flushes are red's too."""
        mock_settings.sync_mode = "incremental"
        orch = _orch(mock_settings)

        conn = AsyncMock()
        pool = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.connection = MagicMock(return_value=ctx)

        async def three_rows(*args, **kwargs):
            for minute in (0, 5, 10):
                yield _reading(timestamp=f"2026-10-15T12:{minute:02d}:00")

        orch.client.fetch_window = three_rows
        window = (datetime(2026, 10, 15, tzinfo=UTC), datetime(2026, 10, 16, tzinfo=UTC))

        with (
            patch("wp6_data.sync.orchestrator.BATCH_SIZE", 2),
            patch("wp6_data.sync.orchestrator.SyncStateManager", return_value=AsyncMock()),
            patch("wp6_data.sync.orchestrator.get_pool", return_value=pool),
            patch(
                "wp6_data.sync.orchestrator.upsert_readings",
                new_callable=AsyncMock,
                side_effect=lambda _conn, batch, source: (len(batch), len(batch)),
            ) as upsert,
            patch("wp6_data.sync.orchestrator._generate_windows", return_value=iter([window])),
        ):
            count = await orch._sync_endpoint("letsgrow-data")

        assert count == 3
        assert [len(c.args[1]) for c in upsert.await_args_list] == [2, 1]
        assert {c.kwargs["source"] for c in upsert.await_args_list} == {"letsgrow"}


class TestFullModeRefresh:
    @pytest.mark.asyncio()
    async def test_full_sync_refreshes_each_twins_whole_history(self, mock_settings):
        mock_settings.sync_mode = "full"
        mock_settings.endpoint_list = ["letsgrow-data"]
        red = replace(LETSGROW, ensure_schema=AsyncMock(), dsn=lambda _s: "red-dsn")
        orch = _orch(mock_settings)
        pool = MagicMock()

        with (
            patch("wp6_data.sync.orchestrator.feed_for", return_value=red),
            patch.object(orch, "_sync_endpoint", new_callable=AsyncMock, return_value=1),
            patch("wp6_data.sync.orchestrator.init_pool", new=AsyncMock(return_value=pool)),
            patch("wp6_data.sync.orchestrator.refresh_sensor_summary", new=AsyncMock()) as full,
            patch(
                "wp6_data.sync.orchestrator.refresh_sensor_summary_recent", new=AsyncMock()
            ) as recent,
            patch("wp6_data.sync.orchestrator.close_pool", new=AsyncMock()),
        ):
            await orch.run()

        full.assert_awaited_once_with(pool)
        recent.assert_not_awaited()


# --- status page ---


class TestFreshnessBudget:
    @pytest.mark.asyncio()
    async def test_letsgrow_is_judged_against_daily_delivery(self):
        """Without a budget the status page could never flag a stalled LetsGrow feed."""
        from wp6_data.red.tsdb import fetch_sync_metrics_tsdb

        rows = [{"endpoint": "letsgrow-data"}, {"endpoint": "sijia"}, {"endpoint": "red-export"}]
        with (
            patch("wp6_data.db.pool.get_pool"),
            patch(
                "wp6_data.db.queries.fetch_sync_metrics_rows",
                new=AsyncMock(return_value=rows),
            ),
        ):
            got = {r["endpoint"]: r for r in await fetch_sync_metrics_tsdb()}

        assert got["letsgrow-data"]["source_type"] == "synced"
        assert got["letsgrow-data"]["freshness_budget"] == letsgrow.FRESHNESS_BUDGET_HOURS
        assert got["sijia"]["source_type"] == "manual"
        assert got["sijia"]["freshness_budget"] is None
        assert got["red-export"]["freshness_budget"] is None
