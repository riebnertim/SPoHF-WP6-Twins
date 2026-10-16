"""Red sensor data provider — federated over MySQL and red TimescaleDB.

Routes per-sensor based on the `source` field on `sensor_defaults` entries
in the red MetadataRegistry. Sensors with no source go to MySQL (the
legacy LoRaWAN tables); sensors with a source go to TimescaleDB (manual
uploads + the letsgrow sync). The routing map is built once at init.
Federation is transparent — public protocol surface is unchanged.

TSDB-side counts/last-seen come from the `sensors_daily_summary` continuous
aggregate (see `wp6_data.db.schema`). Status sync rows come from
`sync_metadata`. Daily coverage rows for TSDB devices come from the
`daily_coverage` table; the MySQL leg still scans `SENSOR_TABLES` directly.

Red-specific methods (get_par_readings, get_weather_station_readings)
stay on MySQLConnection and are accessed directly by DLI routes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd
from cachetools import TTLCache

from wp6_data.shared.aggregation import bucket_and_aggregate
from wp6_data.shared.sensor_summary import get_sensor_summary
from wp6_data.shared.time import display_tz

# Coverage only changes once per day — cache for 1 hour
_coverage_cache: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=4, ttl=3600)

# Cache key for the per-sensor cagg readout (shared TTLCache, 5min)
_CAGG_CACHE_KEY = "red:tsdb-cagg"

if TYPE_CHECKING:
    from wp6_data.red.db import MySQLConnection
    from wp6_data.shared.metadata import MetadataRegistry


async def _fetch_cagg_summary() -> list[dict[str, Any]]:
    """Cached read of the cagg, per (device, sensor)."""
    from wp6_data.red.tsdb import fetch_sensors_from_cagg

    return await get_sensor_summary(_CAGG_CACHE_KEY, fetch_sensors_from_cagg)


def invalidate_caches() -> None:
    """Drop in-process caches that point at red TSDB.

    Call this from write paths in the same process (e.g. Sijia ingest) so the
    next dashboard request reflects the freshly-written rows. Cross-process
    writers don't need this — their caches are naturally cold on next read.
    """
    from wp6_data.shared.sensor_summary import invalidate

    invalidate(_CAGG_CACHE_KEY)
    _coverage_cache.clear()


class RedSensorProvider:
    """Federated SensorDataProvider over MySQL and red TimescaleDB.

    Lazily references deps.db so the provider can be created before
    the MySQL connection is established at startup.
    """

    def __init__(self, metadata: MetadataRegistry) -> None:
        self._metadata = metadata
        self.tsdb_sensors: frozenset[str] = frozenset(
            tag for tag, meta in metadata.sensor_defaults.items() if meta.source
        )

    async def wire_devices(self) -> dict[str, list[str]]:
        """Third routing leg: the installed wires' per-height devices, served
        from the wide wire_sensors table (see ADR 0001). Device id -> sensors."""
        from wp6_data.red.wires import wire_devices

        return await wire_devices(self.db)

    @property
    def db(self) -> MySQLConnection:
        """Direct access to the underlying MySQL connection for DLI routes."""
        from wp6_data.red import deps

        if deps.db is None:
            raise RuntimeError("Database not connected")
        return deps.db

    async def fetch_sync_metrics(self) -> list[dict[str, Any]]:
        from wp6_data.red.tsdb import fetch_sync_metrics_tsdb

        return await fetch_sync_metrics_tsdb()

    async def fetch_daily_coverage(self) -> list[dict[str, Any]]:
        """Derive daily coverage from MySQL sensor tables + TSDB coverage table."""
        key = "red:coverage"
        if key in _coverage_cache:
            return _coverage_cache[key]
        result = await self._fetch_daily_coverage()
        _coverage_cache[key] = result
        return result

    async def _fetch_daily_coverage(self) -> list[dict[str, Any]]:
        """Distinct (device, sensor, day) records — MySQL + wire + TSDB merged.

        Each record carries a ``manual`` flag so `shared.routes.status` can
        split the coverage grid. The MySQL and wire legs are live sensors —
        always automated. The TSDB leg is manual iff its ``source`` is a
        registered manual-upload source (see `red.manual_sources`).
        """
        from wp6_data.red.db import (
            SENSOR_TABLES,
            WIRE_DEVICE_HEIGHTS,
            WIRE_SENSORS_TABLE,
            wire_device_id,
        )
        from wp6_data.red.manual_sources import MANUAL_SOURCE_VALUES
        from wp6_data.red.tsdb import fetch_daily_coverage_from_cagg

        records: list[dict[str, Any]] = []
        # Resolved before taking a pool connection: on a cache miss it needs one.
        wire_devices = await self.wire_devices()
        async with self.db.pool.acquire() as conn, conn.cursor() as cursor:
            for table, measurements in SENSOR_TABLES.items():
                try:
                    await cursor.execute(
                        f"SELECT DISTINCT device_id, DATE(received_at) AS day "
                        f"FROM {table}",
                    )
                    rows = await cursor.fetchall()
                except Exception:
                    continue
                for device_id, day in rows:
                    for sensor in measurements:
                        records.append({
                            "device": device_id,
                            "sensor": sensor,
                            "day": day,
                            "manual": False,
                        })

            # Wire leg: one virtual device per height of each installed wire.
            # Wire is a live (automated) feed, like the MySQL leg.
            if wire_devices:
                try:
                    await cursor.execute(
                        f"SELECT DISTINCT device_id, DATE(received_at) AS day "
                        f"FROM {WIRE_SENSORS_TABLE}",
                    )
                    wire_rows = await cursor.fetchall()
                except Exception:
                    wire_rows = []
                for physical_id, day in wire_rows:
                    for height in WIRE_DEVICE_HEIGHTS:
                        device_id = wire_device_id(physical_id, height)
                        for sensor in wire_devices.get(device_id, []):
                            records.append({
                                "device": device_id,
                                "sensor": sensor,
                                "day": day,
                                "manual": False,
                            })

        for rec in await fetch_daily_coverage_from_cagg():
            rec["manual"] = rec.pop("source", None) in MANUAL_SOURCE_VALUES
            records.append(rec)
        return records

    def _split_by_route(
        self, sensor_tags: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        """Partition sensor_tags into (mysql_tags, tsdb_tags)."""
        if not sensor_tags:
            return [], []
        mysql_tags = [t for t in sensor_tags if t not in self.tsdb_sensors]
        tsdb_tags = [t for t in sensor_tags if t in self.tsdb_sensors]
        return mysql_tags, tsdb_tags

    async def _fetch_mysql(
        self,
        sensor_tags: list[str],
        device_names: list[str] | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> pd.DataFrame:
        """MySQL leg of fetch_data — preserves the existing shape contract."""
        device = device_names[0] if device_names else None
        sensor = sensor_tags[0] if sensor_tags else None
        if device and sensor:
            return await self.db.get_readings_for_comparison(
                device, sensor, start=start, end=end, limit=limit,
            )
        if sensor:
            return await self.db.get_readings_by_measurement(
                sensor, start=start, end=end, limit_per_table=limit,
            )
        return pd.DataFrame(columns=["device", "sensor", "time", "value"])

    async def _fetch_wire(
        self,
        sensor_tags: list[str] | None,
        device_names: list[str],
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> pd.DataFrame:
        """Wire leg of fetch_data — serve wire devices from the wide table.

        Reshapes the long wire frame (device, height, measurement, time, value)
        into the shared (device, sensor, time, value) contract; height is already
        baked into the virtual device id, so it's dropped here.
        """
        df = await self.db.get_wire_sensor_readings(start=start, end=end, limit=limit)
        if df.empty:
            return pd.DataFrame(columns=["device", "sensor", "time", "value"])
        out = df.rename(columns={"measurement": "sensor"})
        out = out[out["device"].isin(device_names)]
        if sensor_tags:
            out = out[out["sensor"].isin(sensor_tags)]
        return out[["device", "sensor", "time", "value"]].reset_index(drop=True)

    async def fetch_data(
        self,
        sensor_tags: list[str] | None = None,
        device_names: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500_000,
        *,
        bucket: timedelta | None = None,
        agg: str | None = None,
    ) -> pd.DataFrame:
        """Fetch readings, dispatching per-device/sensor across three legs.

        Wire devices (typed ``wire`` in metadata) are served from the wide
        wire_sensors table; remaining sensors route per-sensor between the legacy
        MySQL tables and TSDB. The TSDB leg buckets server-side; the MySQL and
        wire legs fetch raw and are folded into the same bucketed contract by the
        shared pandas fallback. Each series routes to exactly one leg, so legs
        never produce overlapping buckets.
        """
        from wp6_data.red.tsdb import fetch_data_tsdb

        bucketed = bucket is not None and agg is not None
        frames: list[pd.DataFrame] = []

        # Partition the requested devices into wire vs the rest.
        wire_devices = await self.wire_devices() if device_names else {}
        wire_names = [d for d in (device_names or []) if d in wire_devices]
        other_names = (
            [d for d in device_names if d not in wire_devices]
            if device_names is not None
            else None
        )

        if wire_names:
            wire_df = await self._fetch_wire(sensor_tags, wire_names, start, end, limit)
            if bucketed:
                wire_df = bucket_and_aggregate(wire_df, bucket, agg, display_tz())
            frames.append(wire_df)

        # Run the legacy/TSDB legs for non-wire devices, or for an unfiltered
        # (measurement-wide) query. A device filter naming only wire devices
        # skips them entirely.
        if device_names is None or other_names:
            mysql_tags, tsdb_tags = self._split_by_route(sensor_tags)
            if mysql_tags:
                mysql_df = await self._fetch_mysql(
                    mysql_tags, other_names, start, end, limit,
                )
                if bucketed:
                    mysql_df = bucket_and_aggregate(
                        mysql_df, bucket, agg, display_tz(),
                    )
                frames.append(mysql_df)
            if tsdb_tags:
                frames.append(await fetch_data_tsdb(
                    sensor_tags=tsdb_tags,
                    device_names=other_names,
                    start=start, end=end, limit=limit,
                    bucket=bucket, agg=agg,
                ))

        if not frames:
            return pd.DataFrame(columns=["device", "sensor", "time", "value"])
        if len(frames) == 1:
            return frames[0]
        merged = pd.concat(frames, ignore_index=True)
        if not merged.empty:
            merged = merged.sort_values("time").reset_index(drop=True)
        return merged

    def _tsdb_devices_by_source(self) -> dict[str, list[str]]:
        """Group TSDB-routed devices by their `source` value.

        Maps source → [device_id, ...]. Only devices with a non-empty
        `source` are TSDB-backed; the rest are MySQL.
        """
        grouped: dict[str, list[str]] = {}
        for device_id, dev in self._metadata.devices.items():
            if dev.source:
                grouped.setdefault(dev.source, []).append(device_id)
        return grouped

    def _enumerate_tsdb_sensors(self) -> list[tuple[str, str]]:
        """List (device, sensor) pairs declared in metadata for TSDB sources.

        A device that lists its own ``sensors`` carries exactly those (a
        LetsGrow module measures its own handful of the source's tags); one
        that lists none carries every tag of its source (each Sijia plant is
        measured for every fruit-chemistry tag).
        """
        sensors_by_source: dict[str, list[str]] = {}
        for tag, meta in self._metadata.sensor_defaults.items():
            if meta.source:
                sensors_by_source.setdefault(meta.source, []).append(tag)

        pairs: list[tuple[str, str]] = []
        for source, devices in self._tsdb_devices_by_source().items():
            for device in devices:
                declared = self._metadata.devices[device].sensors
                for sensor in declared or sensors_by_source.get(source, []):
                    pairs.append((device, sensor))
        return pairs

    async def fetch_available_sensors(self) -> list[dict[str, Any]]:
        """Flat [{device, sensor, readings}, ...] across both backends.

        - MySQL: derived from get_all_devices() (existing behaviour).
        - TSDB: cagg-backed counts per (device, sensor); rows are emitted
          for every metadata-declared pair, with a count of 0 when the cagg
          has no data yet so the sensor still shows up on the home page.
        """
        all_devices = await self.db.get_all_devices()
        result: list[dict[str, Any]] = []
        for device_id, info in sorted(all_devices.items()):
            for measurement in sorted(info["measurements"]):
                result.append({
                    "device": device_id,
                    "sensor": measurement,
                    "readings": info["readings"],
                })

        cagg_rows = await _fetch_cagg_summary()
        cagg_lookup: dict[tuple[str, str], int] = {
            (r["device"], r["sensor"]): int(r["readings"] or 0)
            for r in cagg_rows
        }
        for device, sensor in self._enumerate_tsdb_sensors():
            result.append({
                "device": device,
                "sensor": sensor,
                "readings": cagg_lookup.get((device, sensor), 0),
            })

        wire_summary = await self.db.get_wire_device_summary()
        for device_id, sensors in (await self.wire_devices()).items():
            readings = wire_summary.get(device_id, {}).get("readings", 0)
            for sensor in sensors:
                result.append({
                    "device": device_id,
                    "sensor": sensor,
                    "readings": readings,
                })
        return result

    async def fetch_device_data(self) -> dict[str, dict]:
        """Device overview for the home page (MySQL + TSDB merged).

        TSDB devices' reading totals and last-seen come from the
        sensors_daily_summary cagg via _fetch_cagg_summary (5-min TTL cache).
        """
        all_devices = await self.db.get_all_devices()
        cagg_rows = await _fetch_cagg_summary()

        tsdb_per_device: dict[str, dict[str, Any]] = {}
        for row in cagg_rows:
            entry = tsdb_per_device.setdefault(
                row["device"], {"readings": 0, "last_seen": None},
            )
            entry["readings"] += int(row["readings"] or 0)
            latest = row.get("latest")
            if latest and (entry["last_seen"] is None or latest > entry["last_seen"]):
                entry["last_seen"] = latest

        device_data: dict[str, dict] = {}
        for device_id, info in all_devices.items():
            device_data[device_id] = {
                "sensors": info["measurements"],
                "readings": info["readings"],
                "last_seen": info.get("last_seen"),
            }

        for device, sensor in self._enumerate_tsdb_sensors():
            tsdb_info = tsdb_per_device.get(device, {})
            entry = device_data.setdefault(device, {
                "sensors": [],
                "readings": tsdb_info.get("readings", 0),
                "last_seen": tsdb_info.get("last_seen"),
            })
            if sensor not in entry["sensors"]:
                entry["sensors"].append(sensor)

        wire_summary = await self.db.get_wire_device_summary()
        for device_id, sensors in (await self.wire_devices()).items():
            info = wire_summary.get(device_id, {})
            device_data[device_id] = {
                "sensors": list(sensors),
                "readings": info.get("readings", 0),
                "last_seen": info.get("last_seen"),
            }
        return device_data

    async def fetch_manual_metadata(self) -> dict[str, Any]:
        """Last-upload per source and last-measure per manual sensor key."""
        from wp6_data.red.tsdb import fetch_manual_summary_tsdb

        return await fetch_manual_summary_tsdb()
