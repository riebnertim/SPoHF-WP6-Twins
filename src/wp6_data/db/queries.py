"""SQL queries for TimescaleDB operations."""

from datetime import datetime
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from wp6_data.db.schema import SYNC_HISTORY_RETENTION

logger = structlog.get_logger()


async def upsert_readings(
    conn: AsyncConnection,
    readings: list[dict[str, Any]],
    *,
    source: str | None = None,
) -> tuple[int, int]:
    """Upsert a batch of readings into TimescaleDB.

    Args:
        conn: psycopg async connection
        readings: List of dicts with keys:
            device_name, sensor_tag, value, datetime_measure
        source: The `readings.source` to stamp. ``None`` (blue's datalake rows)
            leaves the column at its default and dedups on blue's
            ``(device_name, sensor_tag, time)``; a value dedups on red's
            ``(source, device_name, sensor_tag, time)``, which is the only
            unique index red's `readings` has.

    Returns:
        Tuple of (total upserted, newly created)
    """
    if not readings:
        return 0, 0

    if source is None:
        columns = "time, device_name, sensor_tag, value, raw_value"
        source_value = ""
        # Matches blue's idx_readings_dedup_v3.
        conflict = "device_name, sensor_tag, time"
    else:
        columns = "time, device_name, sensor_tag, value, raw_value, source"
        source_value = ", %(source)s"
        # Matches red's idx_readings_dedup.
        conflict = "source, device_name, sensor_tag, time"

    query = (
        f"INSERT INTO readings ({columns})"
        " VALUES ("
        "  %(datetime_measure)s, %(device_name)s, %(sensor_tag)s,"
        r"  CASE WHEN %(value)s ~ '^-?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?$'"
        "       THEN %(value)s::double precision"
        "       ELSE NULL"
        "  END,"
        f"  %(value)s{source_value}"
        " )"
        # Re-syncing a window (the client bisects and re-emits) must update in
        # place, never duplicate.
        f" ON CONFLICT ({conflict})"
        " DO UPDATE SET value = EXCLUDED.value,"
        "              raw_value = EXCLUDED.raw_value,"
        "              synced_at = NOW()"
        # `xmax = 0` iff this row was freshly inserted; on a conflict-update xmax
        # holds the updating xid. The command tag can't tell them apart — ON
        # CONFLICT DO UPDATE reports "INSERT 0 1" either way — so the sync's
        # dupe-window early-stop depends on this distinction.
        " RETURNING (xmax = 0) AS inserted"
    )

    async with conn.cursor() as cur:
        created = 0
        for r in readings:
            await cur.execute(query, r if source is None else {**r, "source": source})
            row = await cur.fetchone()
            if row and row[0]:
                created += 1

    return len(readings), created


# Enriched per-endpoint sync metrics: current state from sync_metadata joined to
# rolling aggregates from sync_run_history (7-day SLA, the last 12 record counts
# for a sparkline, the last 20 outcomes for "X of last Y"). Both twins use it; the
# status page renders it. `freshness_budget` is attached per-twin by the caller.
SYNC_METRICS_QUERY = """
    WITH hist AS (
        SELECT endpoint,
            count(*) FILTER (WHERE run_at > NOW() - INTERVAL '7 days')             AS runs_7d,
            count(*) FILTER (WHERE run_at > NOW() - INTERVAL '7 days' AND success) AS ok_7d,
            (array_agg(records ORDER BY run_at DESC))[1:12] AS recent_records,
            (array_agg(success ORDER BY run_at DESC))[1:20] AS recent_success
        FROM sync_run_history
        GROUP BY endpoint
    )
    SELECT m.endpoint,
           m.last_run_at,
           m.last_run_success,
           m.last_run_duration_sec AS duration_seconds,
           m.last_run_records AS records,
           m.last_error AS error,
           m.last_api_status AS api_status,
           m.last_api_error_detail AS api_error_detail,
           m.total_runs,
           m.total_failures,
           m.consecutive_failures,
           m.failing_since,
           m.last_timestamp AS last_data_timestamp,
           h.runs_7d,
           h.ok_7d,
           h.recent_records,
           h.recent_success
    FROM sync_metadata m
    LEFT JOIN hist h USING (endpoint)
    ORDER BY m.endpoint
"""


async def fetch_sync_metrics_rows(pool: Any) -> list[dict[str, Any]]:
    """Enriched sync-metric rows for every endpoint (twin-agnostic).

    Callers attach a per-endpoint ``freshness_budget`` before rendering.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SYNC_METRICS_QUERY)
        return list(await cur.fetchall())


async def record_sync_run(
    conn: AsyncConnection,
    endpoint: str,
    *,
    success: bool,
    duration_sec: float,
    records: int,
    last_timestamp: datetime | None = None,
    error: str | None = None,
) -> None:
    """Upsert `sync_metadata` and append a `sync_run_history` row for a job run.

    Increments `total_runs` always, `total_failures` only on failure. On success
    the transient error and the failure streak are cleared (the lifetime
    `total_failures` is kept, and the run history holds the archive), so a
    recovered job stops showing a stale error.

    Endpoint is a free-form string — treat it as a generic job identifier
    (e.g. ``"sijia"``, ``"red-export"``, ``"yookr-data"``).
    """
    params = {
        "endpoint": endpoint,
        "last_ts": last_timestamp,
        "success": success,
        "duration": duration_sec,
        "records": records,
        "failure_inc": 0 if success else 1,
        "error": error,
    }
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO sync_metadata (
                endpoint, last_timestamp, last_run_at, last_run_success,
                last_run_duration_sec, last_run_records,
                total_runs, total_failures, consecutive_failures, failing_since, last_error
            )
            VALUES (
                %(endpoint)s, %(last_ts)s, NOW(), %(success)s,
                %(duration)s, %(records)s,
                1, %(failure_inc)s, %(failure_inc)s,
                CASE WHEN %(success)s THEN NULL ELSE NOW() END, %(error)s
            )
            ON CONFLICT (endpoint) DO UPDATE SET
                last_timestamp = COALESCE(EXCLUDED.last_timestamp, sync_metadata.last_timestamp),
                last_run_at = NOW(),
                last_run_success = %(success)s,
                last_run_duration_sec = %(duration)s,
                last_run_records = %(records)s,
                total_runs = COALESCE(sync_metadata.total_runs, 0) + 1,
                total_failures = COALESCE(sync_metadata.total_failures, 0) + %(failure_inc)s,
                consecutive_failures = CASE WHEN %(success)s THEN 0
                    ELSE COALESCE(sync_metadata.consecutive_failures, 0) + 1 END,
                failing_since = CASE WHEN %(success)s THEN NULL
                    ELSE COALESCE(sync_metadata.failing_since, NOW()) END,
                last_error = CASE WHEN %(success)s THEN NULL ELSE %(error)s END
            """,
            params,
        )
        await cur.execute(
            """
            INSERT INTO sync_run_history (endpoint, success, records, duration_sec)
            VALUES (%(endpoint)s, %(success)s, %(records)s, %(duration)s)
            """,
            params,
        )
        await cur.execute(
            "DELETE FROM sync_run_history "
            "WHERE endpoint = %(endpoint)s "
            f"  AND run_at < NOW() - INTERVAL '{SYNC_HISTORY_RETENTION}'",
            {"endpoint": endpoint},
        )


async def fetch_manual_summary(pool: Any) -> dict[str, Any]:
    """Twin-agnostic manual-upload freshness for the home/status pages.

    Returns ``{"uploads": {slug: last_uploaded_at},
              "measurements": {sensor_tag: last_measure_time}}``.

    Groups ``manual_uploads`` by its ``source`` slug and finds the latest
    reading time for rows that carry an ``upload_id`` (i.e. came from a
    manual upload). Depends only on ``manual_uploads`` + ``readings.upload_id``,
    so it is the same query for any twin.
    """
    async with pool.connection() as conn, conn.cursor(
        row_factory=dict_row,
    ) as cur:
        await cur.execute(
            "SELECT source, MAX(uploaded_at) AS last_upload "
            "FROM manual_uploads GROUP BY source",
        )
        upload_rows = await cur.fetchall()
        await cur.execute(
            "SELECT r.sensor_tag, MAX(r.time) AS last_measure "
            "FROM readings r WHERE r.upload_id IS NOT NULL "
            "GROUP BY r.sensor_tag",
        )
        measure_rows = await cur.fetchall()

    return {
        "uploads": {r["source"]: r["last_upload"] for r in upload_rows},
        "measurements": {
            r["sensor_tag"]: r["last_measure"] for r in measure_rows
        },
    }


async def refresh_sensor_summary(pool: Any) -> None:
    """Refresh the sensors_daily_summary continuous aggregate over its whole
    history. Heavy on large datasets — reserve for mode=full sync or manual
    rebuild. The try/finally resets autocommit before the connection returns
    to the pool, since psycopg-pool does not reset client-side autocommit
    state and other callers (e.g. Sijia's transactional apply) rely on the
    pool default of manual-commit mode.
    """
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            await conn.execute(
                "CALL refresh_continuous_aggregate("
                "'sensors_daily_summary', NULL, NULL)"
            )
        finally:
            await conn.set_autocommit(False)
    logger.info("sensors_daily_summary_refreshed", scope="whole_history")


async def refresh_sensor_summary_recent(pool: Any) -> None:
    """Refresh the sensors_daily_summary cagg over the last 2 days of buckets.

    Used at the tail of incremental sync so dashboards see freshly-written
    data immediately. 2 days covers the sync's 1-day window plus a margin
    for clock skew / TZ edges, and the work is bounded (~2 daily buckets)
    regardless of dataset size. The background refresh policy still handles
    the broader 7-day window on its own schedule.
    """
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            await conn.execute(
                "CALL refresh_continuous_aggregate("
                "'sensors_daily_summary',"
                " NOW() - INTERVAL '2 days', NULL)"
            )
        finally:
            await conn.set_autocommit(False)
    logger.info("sensors_daily_summary_refreshed", scope="last_2d")
