"""Tests for the federated RedSensorProvider.

The provider routes per-sensor between MySQL (legacy LoRaWAN tables) and
the red TimescaleDB (manual measurements + the letsgrow sync) based on
the `source` field on each `sensor_defaults` entry in metadata.yaml.

Tests use the real red metadata registry so routing reflects production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from wp6_data.red import deps as red_deps
from wp6_data.red.provider import RedSensorProvider
from wp6_data.shared.metadata import MetadataRegistry

RED_YAML = Path(__file__).parent.parent / "src" / "wp6_data" / "red" / "metadata.yaml"


@pytest.fixture()
def red_metadata() -> MetadataRegistry:
    return MetadataRegistry(RED_YAML)


@pytest.fixture()
def mock_mysql(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the global MySQL connection with an AsyncMock."""
    db = AsyncMock()
    # Wire leg defaults to empty so explorer enumerations don't choke on a mock;
    # tests that exercise the wire override this.
    db.get_wire_device_summary.return_value = {}
    db.get_active_wire_ids.return_value = []
    monkeypatch.setattr(red_deps, "db", db)
    return db


@pytest.fixture()
def mock_tsdb_fetch_data(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the TSDB fetch_data helper with an AsyncMock."""
    from wp6_data.red import tsdb

    fn = AsyncMock(
        return_value=pd.DataFrame(columns=["device", "sensor", "time", "value"]),
    )
    monkeypatch.setattr(tsdb, "fetch_data_tsdb", fn)
    return fn


@pytest.fixture(autouse=True)
def clear_caches() -> None:
    """Clear the provider's coverage, sensor_summary and wire-map caches.

    All have multi-minute TTLs that would otherwise carry state across tests.
    """
    from wp6_data.red.provider import _coverage_cache
    from wp6_data.red.wires import invalidate_wire_cache
    from wp6_data.shared import sensor_summary

    _coverage_cache.clear()
    sensor_summary.invalidate()
    invalidate_wire_cache()


@pytest.fixture()
def mock_tsdb_coverage(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the TSDB daily-coverage table helper with an AsyncMock."""
    from wp6_data.red import tsdb

    fn = AsyncMock(return_value=[])
    monkeypatch.setattr(tsdb, "fetch_daily_coverage_from_cagg", fn)
    return fn


@pytest.fixture()
def mock_tsdb_cagg(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the TSDB per-(device,sensor) cagg helper with an AsyncMock.

    Returns cagg-shaped rows: device, sensor, readings, earliest, latest.
    """
    from wp6_data.red import tsdb

    fn = AsyncMock(return_value=[])
    monkeypatch.setattr(tsdb, "fetch_sensors_from_cagg", fn)
    return fn


def test_init_builds_tsdb_routing_set_from_sensor_defaults_source_field(
    red_metadata: MetadataRegistry,
) -> None:
    """Sensors with non-empty `source` route to TSDB; others to MySQL."""
    provider = RedSensorProvider(metadata=red_metadata)

    # Sijia fruit-chemistry sensors have source: "sijia" → TSDB
    assert "chlorophyll" in provider.tsdb_sensors
    assert "vitamin_c_fresh" in provider.tsdb_sensors

    # Legacy MySQL sensors have no source → MySQL (default)
    assert "par" not in provider.tsdb_sensors
    assert "temp" not in provider.tsdb_sensors
    assert "co2" not in provider.tsdb_sensors


async def test_fetch_data_for_mysql_sensor_only_hits_mysql(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_fetch_data: AsyncMock,
) -> None:
    """A request for a MySQL-routed sensor must not hit TSDB."""
    mock_mysql.get_readings_for_comparison.return_value = pd.DataFrame([
        {
            "device": "s2100-01-par",
            "sensor": "par",
            "time": pd.Timestamp("2025-01-01T12:00:00", tz=UTC),
            "value": 100.0,
        },
    ])
    provider = RedSensorProvider(metadata=red_metadata)

    df = await provider.fetch_data(
        sensor_tags=["par"],
        device_names=["s2100-01-par"],
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 2, tzinfo=UTC),
    )

    mock_mysql.get_readings_for_comparison.assert_awaited_once()
    mock_tsdb_fetch_data.assert_not_awaited()
    assert list(df.columns) == ["device", "sensor", "time", "value"]
    assert len(df) == 1


async def test_fetch_data_for_tsdb_sensor_only_hits_tsdb(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_fetch_data: AsyncMock,
) -> None:
    """A request for a TSDB-routed sensor (e.g. chlorophyll) must not hit MySQL."""
    mock_tsdb_fetch_data.return_value = pd.DataFrame([
        {
            "device": "neurath-B-2034-strabelina",
            "sensor": "chlorophyll",
            "time": pd.Timestamp("2025-09-01T00:00:00", tz=UTC),
            "value": 12.3,
        },
    ])
    provider = RedSensorProvider(metadata=red_metadata)

    df = await provider.fetch_data(
        sensor_tags=["chlorophyll"],
        device_names=["neurath-B-2034-strabelina"],
    )

    mock_tsdb_fetch_data.assert_awaited_once()
    mock_mysql.get_readings_for_comparison.assert_not_awaited()
    mock_mysql.get_readings_by_measurement.assert_not_awaited()
    assert list(df.columns) == ["device", "sensor", "time", "value"]
    assert len(df) == 1
    assert df.iloc[0]["sensor"] == "chlorophyll"


async def test_fetch_data_mixed_sources_returns_merged_dataframe(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_fetch_data: AsyncMock,
) -> None:
    """Mixed-source request (e.g. PAR + chlorophyll) returns one merged frame."""
    mock_mysql.get_readings_by_measurement.return_value = pd.DataFrame([
        {
            "device": "s2100-01-par",
            "sensor": "par",
            "time": pd.Timestamp("2025-08-01T12:00:00", tz=UTC),
            "value": 90.0,
        },
    ])
    mock_tsdb_fetch_data.return_value = pd.DataFrame([
        {
            "device": "neurath-B-2034-strabelina",
            "sensor": "chlorophyll",
            "time": pd.Timestamp("2025-09-01T00:00:00", tz=UTC),
            "value": 12.3,
        },
    ])
    provider = RedSensorProvider(metadata=red_metadata)

    df = await provider.fetch_data(sensor_tags=["par", "chlorophyll"])

    mock_mysql.get_readings_by_measurement.assert_awaited_once()
    mock_tsdb_fetch_data.assert_awaited_once()
    assert list(df.columns) == ["device", "sensor", "time", "value"]
    assert set(df["sensor"]) == {"par", "chlorophyll"}
    # Merged frame is sorted by time
    assert list(df["time"]) == sorted(df["time"])


async def test_fetch_available_sensors_includes_tsdb_sensors_from_metadata(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_cagg: AsyncMock,
) -> None:
    """TSDB-side sensors are enumerated from metadata.yaml.

    Each device with source X yields one entry per sensor_default with the
    same source. Existing MySQL devices/sensors continue to come from the
    legacy MySQL flatten path.
    """
    mock_mysql.get_all_devices.return_value = {
        "s2100-01-par": {
            "tables": ["s2100"], "measurements": ["par"], "readings": 100,
        },
    }
    provider = RedSensorProvider(metadata=red_metadata)

    sensors = await provider.fetch_available_sensors()
    pairs = {(s["device"], s["sensor"]) for s in sensors}

    # MySQL device pairs survive
    assert ("s2100-01-par", "par") in pairs

    # Each Sijia device is paired with every Sijia sensor_default
    sijia_sensors = {
        tag for tag, m in red_metadata.sensor_defaults.items() if m.source == "sijia"
    }
    for device in ("neurath-B-2034-strabelina", "neurath-B-2012-shivious"):
        for sensor in sijia_sensors:
            assert (device, sensor) in pairs


async def test_fetch_available_sensors_uses_cagg_counts_for_tsdb_pairs(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_cagg: AsyncMock,
) -> None:
    """TSDB pair reading-counts come from the cagg (was hardcoded 0 previously)."""
    mock_mysql.get_all_devices.return_value = {}
    mock_tsdb_cagg.return_value = [
        {"device": "neurath-B-2034-strabelina", "sensor": "chlorophyll",
         "readings": 42, "earliest": None, "latest": None},
    ]
    provider = RedSensorProvider(metadata=red_metadata)

    sensors = await provider.fetch_available_sensors()
    by_pair = {(s["device"], s["sensor"]): s["readings"] for s in sensors}

    assert by_pair[("neurath-B-2034-strabelina", "chlorophyll")] == 42
    # An undeclared-in-cagg pair still appears with 0
    assert by_pair[("neurath-B-2012-shivious", "chlorophyll")] == 0


async def test_fetch_device_data_includes_tsdb_devices_from_metadata(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_cagg: AsyncMock,
) -> None:
    """fetch_device_data merges MySQL devices with TSDB-declared devices."""
    mock_mysql.get_all_devices.return_value = {
        "s2100-01-par": {
            "tables": ["s2100"], "measurements": ["par"], "readings": 100,
        },
    }
    provider = RedSensorProvider(metadata=red_metadata)

    devices = await provider.fetch_device_data()

    assert "s2100-01-par" in devices
    assert "par" in devices["s2100-01-par"]["sensors"]

    # Sijia devices appear with all Sijia sensors declared in metadata
    sijia_sensors = {
        tag for tag, m in red_metadata.sensor_defaults.items() if m.source == "sijia"
    }
    assert "neurath-B-2034-strabelina" in devices
    assert set(devices["neurath-B-2034-strabelina"]["sensors"]) == sijia_sensors


async def test_fetch_device_data_populates_tsdb_reading_counts_from_cagg(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_cagg: AsyncMock,
) -> None:
    """TSDB-backed devices' totals are summed across cagg per-sensor rows."""
    mock_mysql.get_all_devices.return_value = {}
    mock_tsdb_cagg.return_value = [
        {"device": "neurath-B-2034-strabelina", "sensor": "chlorophyll",
         "readings": 30, "earliest": None, "latest": None},
        {"device": "neurath-B-2034-strabelina", "sensor": "vitamin_c_fresh",
         "readings": 12, "earliest": None, "latest": None},
        {"device": "neurath-B-2012-shivious", "sensor": "chlorophyll",
         "readings": 17, "earliest": None, "latest": None},
    ]
    provider = RedSensorProvider(metadata=red_metadata)

    devices = await provider.fetch_device_data()

    assert devices["neurath-B-2034-strabelina"]["readings"] == 42
    assert devices["neurath-B-2012-shivious"]["readings"] == 17


async def test_fetch_device_data_zero_count_for_tsdb_device_with_no_rows(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_cagg: AsyncMock,
) -> None:
    """A TSDB device with no readings yet still appears, with count 0."""
    mock_mysql.get_all_devices.return_value = {}
    mock_tsdb_cagg.return_value = []  # no rows in cagg at all
    provider = RedSensorProvider(metadata=red_metadata)

    devices = await provider.fetch_device_data()

    # Devices declared in metadata still appear (structural enumeration)
    assert "neurath-B-2034-strabelina" in devices
    assert devices["neurath-B-2034-strabelina"]["readings"] == 0


async def test_invalidate_caches_drops_both_cagg_and_coverage(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_cagg: AsyncMock,
) -> None:
    """invalidate_caches() forces a re-fetch from both cache layers.

    Regression: the Sijia post-apply bookkeeping must call this so the
    same-process dashboard request after an upload sees fresh data instead
    of the empty cagg result cached at startup. Without invalidation, the
    in-process TTL caches (5 min cagg, 1 hr coverage) hold stale data.
    """
    from wp6_data.red.provider import invalidate_caches

    mock_mysql.get_all_devices.return_value = {}
    mock_tsdb_cagg.return_value = []
    provider = RedSensorProvider(metadata=red_metadata)

    # Warm the cagg cache with the empty result.
    await provider.fetch_device_data()
    assert mock_tsdb_cagg.await_count == 1

    # A second call hits the cache (no new fetch).
    await provider.fetch_device_data()
    assert mock_tsdb_cagg.await_count == 1

    # Simulate a write happening in this process; invalidate the caches.
    mock_tsdb_cagg.return_value = [
        {"device": "neurath-B-2034-strabelina", "sensor": "chlorophyll",
         "readings": 42, "earliest": None, "latest": None},
    ]
    invalidate_caches()

    devices = await provider.fetch_device_data()
    assert mock_tsdb_cagg.await_count == 2  # cache was dropped
    assert devices["neurath-B-2034-strabelina"]["readings"] == 42


async def test_wire_devices_come_from_the_upstream_wire_map(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
) -> None:
    """The installed wires form the third routing leg: radiation above, then
    four sensors per height. A wire the map no longer lists is not a device,
    whatever wire_sensors still holds for it."""
    mock_mysql.get_active_wire_ids.return_value = ["WS_01_02", "WS_01_03"]
    provider = RedSensorProvider(metadata=red_metadata)

    wire_devices = await provider.wire_devices()

    assert set(wire_devices["WS_01_03-h3"]) == {"par", "temp", "hum", "co2"}
    assert wire_devices["WS_01_03-h0"] == ["rad"]
    assert not any(d.startswith("WS_01_01") for d in wire_devices)


async def test_fetch_device_data_includes_wire_devices(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_cagg: AsyncMock,
) -> None:
    """Wire devices appear in the explorer with their declared sensors + counts."""
    mock_mysql.get_all_devices.return_value = {}
    mock_mysql.get_active_wire_ids.return_value = ["WS_01_01"]
    mock_mysql.get_wire_device_summary.return_value = {
        "WS_01_01-h1": {"readings": 1234, "last_seen": None},
    }
    provider = RedSensorProvider(metadata=red_metadata)

    devices = await provider.fetch_device_data()

    assert "WS_01_01-h1" in devices
    assert set(devices["WS_01_01-h1"]["sensors"]) == {"par", "temp", "hum", "co2"}
    assert devices["WS_01_01-h1"]["readings"] == 1234


async def test_fetch_data_routes_wire_device_to_wire_table(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_fetch_data: AsyncMock,
) -> None:
    """A wire device request hits the wire reader, not the legacy MySQL leg."""
    mock_mysql.get_active_wire_ids.return_value = ["WS_01_01"]
    mock_mysql.get_wire_sensor_readings.return_value = pd.DataFrame([
        {"device": "WS_01_01-h3", "height": 3, "measurement": "par",
         "time": pd.Timestamp("2026-05-26T12:00:00", tz=UTC), "value": 7.0},
        {"device": "WS_01_01-h3", "height": 3, "measurement": "temp",
         "time": pd.Timestamp("2026-05-26T12:00:00", tz=UTC), "value": 26.0},
    ])
    provider = RedSensorProvider(metadata=red_metadata)

    df = await provider.fetch_data(
        sensor_tags=["par"], device_names=["WS_01_01-h3"],
    )

    mock_mysql.get_wire_sensor_readings.assert_awaited_once()
    mock_mysql.get_readings_for_comparison.assert_not_awaited()
    mock_tsdb_fetch_data.assert_not_awaited()
    assert list(df.columns) == ["device", "sensor", "time", "value"]
    # Only the requested sensor survives; height is folded into the device id.
    assert set(df["sensor"]) == {"par"}
    assert df.iloc[0]["device"] == "WS_01_01-h3"


async def test_fetch_daily_coverage_merges_mysql_and_tsdb(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_coverage: AsyncMock,
) -> None:
    """Coverage must include rows from both backends.

    Coverage is the one method that legitimately queries the data store
    (metadata has no per-day info). The merge is structural.
    """
    from datetime import date

    # Stub MySQL cursor path — fetch_daily_coverage iterates SENSOR_TABLES.
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[("s2100-01-par", date(2025, 1, 1))])
    cursor_ctx = AsyncMock()
    cursor_ctx.__aenter__ = AsyncMock(return_value=cursor)
    cursor_ctx.__aexit__ = AsyncMock(return_value=False)
    conn = AsyncMock()
    conn.cursor = lambda: cursor_ctx
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = lambda: conn_ctx
    mock_mysql.pool = pool

    mock_tsdb_coverage.return_value = [
        {"device": "neurath-B-2034-strabelina",
         "sensor": "chlorophyll", "day": date(2025, 9, 1)},
    ]

    provider = RedSensorProvider(metadata=red_metadata)
    coverage = await provider.fetch_daily_coverage()

    pairs = {(c["device"], c["sensor"], c["day"]) for c in coverage}
    assert ("s2100-01-par", "par", date(2025, 1, 1)) in pairs
    assert (
        "neurath-B-2034-strabelina", "chlorophyll", date(2025, 9, 1),
    ) in pairs
    mock_tsdb_coverage.assert_awaited_once()


async def test_fetch_daily_coverage_tags_manual_by_source(
    red_metadata: MetadataRegistry,
    mock_mysql: AsyncMock,
    mock_tsdb_coverage: AsyncMock,
) -> None:
    """Each record is tagged `manual`: MySQL (LoRaWAN) rows are never manual;
    TSDB rows are manual iff their `source` is a registered manual source.

    This is what lets `shared.routes.status` split red's coverage grid into
    Sensor vs Manual sections (matching blue) instead of lumping Sijia in
    with the automated sensors.
    """
    from datetime import date

    from wp6_data.red.sijia.source import SIJIA

    # MySQL leg: one live LoRaWAN sensor row.
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[("s2100-01-par", date(2025, 1, 1))])
    cursor_ctx = AsyncMock()
    cursor_ctx.__aenter__ = AsyncMock(return_value=cursor)
    cursor_ctx.__aexit__ = AsyncMock(return_value=False)
    conn = AsyncMock()
    conn.cursor = lambda: cursor_ctx
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = lambda: conn_ctx
    mock_mysql.pool = pool

    # TSDB leg: a manual (Sijia) row and a hypothetical automated row that
    # also carries a source (e.g. letsgrow) — only the former is manual.
    mock_tsdb_coverage.return_value = [
        {"device": "neurath-B-2034-strabelina", "sensor": "chlorophyll",
         "day": date(2025, 9, 1), "source": SIJIA.categorical_value},
        {"device": "s2100-01-par", "sensor": "par",
         "day": date(2025, 2, 1), "source": "letsgrow"},
    ]

    provider = RedSensorProvider(metadata=red_metadata)
    coverage = await provider.fetch_daily_coverage()

    manual = {(c["device"], c["sensor"]) for c in coverage if c["manual"]}
    automated = {(c["device"], c["sensor"]) for c in coverage if not c["manual"]}

    assert ("neurath-B-2034-strabelina", "chlorophyll") in manual
    assert ("s2100-01-par", "par") in automated  # MySQL + non-manual TSDB source
    assert all("source" not in c for c in coverage)  # source consumed into `manual`
