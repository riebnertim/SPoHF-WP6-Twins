"""Tests for wp6_data.sync.orchestrator — patch SyncStateManager + upsert_readings + mock client."""

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from wp6_data.sync.feeds import YOOKR
from wp6_data.sync.orchestrator import (
    CONSECUTIVE_DUPE_WINDOW_THRESHOLD,
    INCREMENTAL_LOOKBACK_DAYS,
    SyncOrchestrator,
    _generate_windows,
    ensure_utc,
)

from .conftest import make_reading

# --- Pure helpers (no mocks) ---


class TestEnsureUtc:
    def test_naive_gets_utc(self):
        dt = datetime(2024, 1, 1, 12, 0)
        result = ensure_utc(dt)
        assert result.tzinfo is UTC

    def test_aware_unchanged(self):
        dt = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        result = ensure_utc(dt)
        assert result is dt


_FULL_START = datetime(2024, 1, 1, tzinfo=UTC)


class TestGenerateWindows:
    def test_full_mode_starts_from_full_start(self):
        windows = list(_generate_windows("full", 1, full_start=_FULL_START))
        # Last window (generated last, furthest back) should start at full_start
        assert windows[-1][0] == _FULL_START

    def test_incremental_mode_limited_lookback(self):
        windows = list(_generate_windows("incremental", 1, full_start=_FULL_START))
        assert INCREMENTAL_LOOKBACK_DAYS - 1 <= len(windows) <= INCREMENTAL_LOOKBACK_DAYS + 1

    def test_incremental_mode_ignores_full_start(self):
        windows = list(_generate_windows("incremental", 1, full_start=_FULL_START))
        assert windows[-1][0] > _FULL_START

    def test_windows_go_backwards(self):
        windows = list(_generate_windows("incremental", 1, full_start=_FULL_START))
        # First window should be most recent
        assert windows[0][1] > windows[-1][1]

    def test_windows_are_contiguous(self):
        windows = list(_generate_windows("incremental", 1, full_start=_FULL_START))
        for i in range(len(windows) - 1):
            assert windows[i][0] == windows[i + 1][1]

    def test_multi_day_windows(self):
        windows = list(_generate_windows("incremental", 7, full_start=_FULL_START))
        for start, end in windows:
            assert (end - start).days <= 7

    def test_full_mode_bounded_window(self):
        """Both bounds set → a full sync scoped to an arbitrary historical window."""
        start = datetime(2025, 11, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, tzinfo=UTC)
        windows = list(_generate_windows("full", 1, full_start=start, end=end))

        assert windows[0][1] == end
        assert windows[-1][0] == start
        assert len(windows) == (end - start).days
        # contiguous, no gaps or overlaps across the whole span
        for i in range(len(windows) - 1):
            assert windows[i][0] == windows[i + 1][1]


class TestUpdateSensorStats:
    def test_first_reading(self):
        stats = defaultdict(lambda: {"count": 0, "min": None, "max": None})
        SyncOrchestrator._update_sensor_stats(stats, "temp", "2024-01-01T00:00:00")
        assert stats["temp"]["count"] == 1
        assert stats["temp"]["min"] == "2024-01-01T00:00:00"
        assert stats["temp"]["max"] == "2024-01-01T00:00:00"

    def test_updates_min_max(self):
        stats = defaultdict(lambda: {"count": 0, "min": None, "max": None})
        SyncOrchestrator._update_sensor_stats(stats, "temp", "2024-06-01T00:00:00")
        SyncOrchestrator._update_sensor_stats(stats, "temp", "2024-01-01T00:00:00")
        SyncOrchestrator._update_sensor_stats(stats, "temp", "2024-12-01T00:00:00")
        assert stats["temp"]["count"] == 3
        assert stats["temp"]["min"] == "2024-01-01T00:00:00"
        assert stats["temp"]["max"] == "2024-12-01T00:00:00"

    def test_separate_tags(self):
        stats = defaultdict(lambda: {"count": 0, "min": None, "max": None})
        SyncOrchestrator._update_sensor_stats(stats, "temp", "2024-01-01T00:00:00")
        SyncOrchestrator._update_sensor_stats(stats, "humidity", "2024-02-01T00:00:00")
        assert stats["temp"]["count"] == 1
        assert stats["humidity"]["count"] == 1


class TestReadingToParams:
    def test_converts_reading(self):
        reading = make_reading()
        params = YOOKR.to_params(reading)
        assert params["device_name"] == "Test Device"
        assert params["sensor_tag"] == "temperature"
        assert params["value"] == "21.5"
        assert "datetime_measure" in params
        # We deliberately drop the relay's ingestion timestamp — not persisted, not used.
        assert "api_timestamp" not in params
        # Only the four columns the INSERT binds; `sensor_id` and the retired
        # `project` are not carried into the params.
        assert "sensor_id" not in params
        assert "project" not in params


# --- _flush_batch ---


class TestFlushBatch:
    @pytest.mark.asyncio()
    async def test_empty_batch(self, mock_settings):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch.settings = mock_settings
        conn = AsyncMock()
        upserted, created = await orch._flush_batch(conn, [])
        assert (upserted, created) == (0, 0)

    @pytest.mark.asyncio()
    async def test_delegates_to_upsert_readings(self, mock_settings):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch.settings = mock_settings
        conn = AsyncMock()
        batch = [
            {
                "device_name": "dev1",
                "sensor_tag": "temp",
                "datetime_measure": "2024-06-15T12:00:00",
            }
        ]

        with patch(
            "wp6_data.sync.orchestrator.upsert_readings",
            new_callable=AsyncMock,
            return_value=(3, 2),
        ) as mock_upsert:
            upserted, created = await orch._flush_batch(conn, batch)

        # Coverage is derived from the cagg now — the sync only writes readings.
        assert (upserted, created) == (3, 2)
        mock_upsert.assert_awaited_once_with(conn, batch, source=None)


# --- _sync_endpoint ---


def _make_orchestrator(mock_settings):
    """Create an orchestrator with mocked internals."""
    orch = SyncOrchestrator.__new__(SyncOrchestrator)
    orch.settings = mock_settings
    orch.client = MagicMock()
    return orch


# Blue's feed with schema bootstrap stubbed, for run() tests that never touch a DB.
_BLUE_FEED = replace(YOOKR, ensure_schema=AsyncMock())


def _mock_pool_ctx():
    """Create a mock pool with connection context manager."""
    conn = AsyncMock()
    pool = MagicMock()
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.connection = MagicMock(return_value=conn_ctx)
    return pool, conn


class TestSyncEndpoint:
    @pytest.mark.asyncio()
    async def test_empty_sync_returns_zero(self, mock_settings):
        mock_settings.sync_mode = "incremental"
        orch = _make_orchestrator(mock_settings)
        pool, conn = _mock_pool_ctx()

        async def empty_iter(*args, **kwargs):
            return
            yield  # noqa: E111 — make it an async generator

        orch.client.fetch_window = empty_iter

        with (
            patch("wp6_data.sync.orchestrator.SyncStateManager") as MockState,
            patch("wp6_data.sync.orchestrator.get_pool", return_value=pool),
            patch(
                "wp6_data.sync.orchestrator.upsert_readings",
                new_callable=AsyncMock,
                return_value=(0, 0),
            ),
        ):
            state_inst = AsyncMock()
            MockState.return_value = state_inst
            count = await orch._sync_endpoint("yookr-data")

        assert count == 0
        state_inst.record_run_result.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_empty_sync_skips_timestamp_update(self, mock_settings):
        mock_settings.sync_mode = "incremental"
        orch = _make_orchestrator(mock_settings)
        pool, conn = _mock_pool_ctx()

        async def empty_iter(*args, **kwargs):
            return
            yield

        orch.client.fetch_window = empty_iter

        with (
            patch("wp6_data.sync.orchestrator.SyncStateManager") as MockState,
            patch("wp6_data.sync.orchestrator.get_pool", return_value=pool),
            patch(
                "wp6_data.sync.orchestrator.upsert_readings",
                new_callable=AsyncMock,
                return_value=(0, 0),
            ),
        ):
            state_inst = AsyncMock()
            MockState.return_value = state_inst
            await orch._sync_endpoint("yookr-data")

        state_inst.update_timestamp.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_api_error_records_failure(self, mock_settings):
        mock_settings.sync_mode = "incremental"
        orch = _make_orchestrator(mock_settings)
        pool, conn = _mock_pool_ctx()

        async def error_iter(*args, **kwargs):
            raise httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(500, text="oops"),
            )
            yield  # noqa: E111

        orch.client.fetch_window = error_iter

        with (
            patch("wp6_data.sync.orchestrator.SyncStateManager") as MockState,
            patch("wp6_data.sync.orchestrator.get_pool", return_value=pool),
        ):
            state_inst = AsyncMock()
            MockState.return_value = state_inst

            with pytest.raises(httpx.HTTPStatusError):
                await orch._sync_endpoint("yookr-data")

        state_inst.record_run_result.assert_awaited_once()
        call_kwargs = state_inst.record_run_result.call_args.kwargs
        assert call_kwargs["success"] is False

    @pytest.mark.asyncio()
    async def test_incremental_early_stop_on_consecutive_dupes(self, mock_settings):
        """When all records in consecutive windows are duplicates, stop early."""
        mock_settings.sync_mode = "incremental"
        orch = _make_orchestrator(mock_settings)
        pool, conn = _mock_pool_ctx()

        reading = make_reading()

        async def one_reading_iter(*args, **kwargs):
            yield reading

        orch.client.fetch_window = one_reading_iter

        with (
            patch("wp6_data.sync.orchestrator.SyncStateManager") as MockState,
            patch("wp6_data.sync.orchestrator.get_pool", return_value=pool),
            patch(
                "wp6_data.sync.orchestrator.upsert_readings",
                new_callable=AsyncMock,
                return_value=(1, 0),  # upserted=1, created=0 → all dupes
            ),
            patch(
                "wp6_data.sync.orchestrator._generate_windows"
            ) as mock_gen,
        ):
            state_inst = AsyncMock()
            MockState.return_value = state_inst

            # Generate more windows than the threshold
            now = datetime.now(UTC)
            mock_gen.return_value = iter([
                (now - timedelta(days=i + 1), now - timedelta(days=i))
                for i in range(10)
            ])
            count = await orch._sync_endpoint("yookr-data")

        # Should have stopped after CONSECUTIVE_DUPE_WINDOW_THRESHOLD windows
        # Each window upserts 1 record, so total = threshold * 1
        assert count == CONSECUTIVE_DUPE_WINDOW_THRESHOLD


# --- run() ---


class TestRun:
    @pytest.mark.asyncio()
    async def test_connect_disconnect_lifecycle(self, mock_settings):
        orch = _make_orchestrator(mock_settings)

        mock_pool = AsyncMock()

        # Make _sync_endpoint a coroutine that returns 5
        with (
            patch.object(orch, "_sync_endpoint", new_callable=AsyncMock, return_value=5),
            patch(
                "wp6_data.sync.orchestrator.init_pool",
                new_callable=AsyncMock,
                return_value=mock_pool,
            ) as mock_init,
            patch("wp6_data.sync.orchestrator.feed_for", return_value=_BLUE_FEED),
            patch(
                "wp6_data.sync.orchestrator.refresh_sensor_summary",
                new_callable=AsyncMock,
            ),
            patch(
                "wp6_data.sync.orchestrator.refresh_sensor_summary_recent",
                new_callable=AsyncMock,
            ),
            patch(
                "wp6_data.sync.orchestrator.close_pool",
                new_callable=AsyncMock,
            ) as mock_close,
        ):
            stats = await orch.run()

        mock_init.assert_awaited_once()
        mock_close.assert_awaited_once()
        assert stats["total_records"] == 5

    @pytest.mark.asyncio()
    async def test_per_endpoint_error_isolation(self, mock_settings):
        mock_settings.endpoint_list = ["ep1", "ep2"]
        orch = _make_orchestrator(mock_settings)

        call_count = 0

        async def side_effect(endpoint):
            nonlocal call_count
            call_count += 1
            if endpoint == "ep1":
                raise RuntimeError("ep1 failed")
            return 10

        with (
            patch.object(orch, "_sync_endpoint", side_effect=side_effect),
            patch(
                "wp6_data.sync.orchestrator.init_pool",
                new_callable=AsyncMock,
                return_value=AsyncMock(),
            ),
            patch("wp6_data.sync.orchestrator.feed_for", return_value=_BLUE_FEED),
            patch(
                "wp6_data.sync.orchestrator.refresh_sensor_summary",
                new_callable=AsyncMock,
            ),
            patch(
                "wp6_data.sync.orchestrator.refresh_sensor_summary_recent",
                new_callable=AsyncMock,
            ),
            patch(
                "wp6_data.sync.orchestrator.close_pool",
                new_callable=AsyncMock,
            ),
        ):
            stats = await orch.run()

        assert call_count == 2
        assert len(stats["errors"]) == 1
        assert "ep1" in stats["errors"][0]
        assert stats["endpoints"]["ep2"] == 10
        assert stats["total_records"] == 10

    @pytest.mark.asyncio()
    async def test_stats_aggregation(self, mock_settings):
        mock_settings.endpoint_list = ["ep1", "ep2"]
        orch = _make_orchestrator(mock_settings)

        async def side_effect(endpoint):
            return 10 if endpoint == "ep1" else 20

        with (
            patch.object(orch, "_sync_endpoint", side_effect=side_effect),
            patch(
                "wp6_data.sync.orchestrator.init_pool",
                new_callable=AsyncMock,
                return_value=AsyncMock(),
            ),
            patch("wp6_data.sync.orchestrator.feed_for", return_value=_BLUE_FEED),
            patch(
                "wp6_data.sync.orchestrator.refresh_sensor_summary",
                new_callable=AsyncMock,
            ),
            patch(
                "wp6_data.sync.orchestrator.refresh_sensor_summary_recent",
                new_callable=AsyncMock,
            ),
            patch(
                "wp6_data.sync.orchestrator.close_pool",
                new_callable=AsyncMock,
            ),
        ):
            stats = await orch.run()

        assert stats["total_records"] == 30
        assert stats["endpoints"] == {"ep1": 10, "ep2": 20}
        assert "duration_seconds" in stats


class TestSyncEndpointWindowBounds:
    """`sync_end` must scope a full sync without ever rewinding an incremental one."""

    @staticmethod
    async def _capture_window_kwargs(orch):
        pool, _conn = _mock_pool_ctx()

        async def no_readings(*args, **kwargs):
            return
            yield  # pragma: no cover — makes this an async generator

        orch.client.fetch_window = no_readings

        with (
            patch("wp6_data.sync.orchestrator.SyncStateManager") as MockState,
            patch("wp6_data.sync.orchestrator.get_pool", return_value=pool),
            patch("wp6_data.sync.orchestrator._generate_windows") as mock_gen,
        ):
            MockState.return_value = AsyncMock()
            mock_gen.return_value = iter([])
            await orch._sync_endpoint("yookr-data")

        return mock_gen.call_args.kwargs

    @pytest.mark.asyncio()
    async def test_full_mode_honours_sync_end(self, mock_settings):
        mock_settings.sync_mode = "full"
        mock_settings.sync_start = date(2024, 11, 1)
        mock_settings.sync_end = date(2025, 12, 1)

        kwargs = await self._capture_window_kwargs(_make_orchestrator(mock_settings))

        assert kwargs["full_start"] == datetime(2024, 11, 1, tzinfo=UTC)
        assert kwargs["end"] == datetime(2025, 12, 1, tzinfo=UTC)

    @pytest.mark.asyncio()
    async def test_incremental_mode_ignores_sync_end(self, mock_settings):
        """Otherwise the watermark would stop advancing and the twin would go stale."""
        mock_settings.sync_mode = "incremental"
        mock_settings.sync_end = date(2025, 12, 1)

        kwargs = await self._capture_window_kwargs(_make_orchestrator(mock_settings))

        assert kwargs["end"] is None
