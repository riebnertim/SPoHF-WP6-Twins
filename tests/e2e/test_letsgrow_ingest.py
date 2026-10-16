"""E2E: LetsGrow rows land in red's readings, keyed by source.

The unit tests prove the SQL *says* `ON CONFLICT (source, device_name,
sensor_tag, time)`; only a real database proves red has a unique index that
matches it, so that a re-synced window updates in place instead of erroring.
"""

from datetime import UTC, datetime

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tests.e2e.conftest import RED_TSDB_DSN, remove_cagg_refresh_policy
from wp6_data.api.models import LetsGrowReading
from wp6_data.db.queries import upsert_readings
from wp6_data.red import letsgrow
from wp6_data.red.tsdb import ensure_schema_red

pytestmark = pytest.mark.e2e

ROW = LetsGrowReading.model_validate({
    "module_id": 39887,
    "module_name": "Climate B4",
    "col_id": 1467902,
    "description": "Temperature greenhouse - °C - 5 min",
    "timestamp": "2026-10-15T14:00:00",
    "value": 21.5,
})


async def _drop_red_schema(conn) -> None:
    """Same teardown as test_red_tsdb_schema: cagg before the hypertable."""
    await remove_cagg_refresh_policy(conn)
    async with conn.cursor() as cur:
        await cur.execute("DROP MATERIALIZED VIEW IF EXISTS sensors_daily_summary")
        await cur.execute("DROP TABLE IF EXISTS sync_metadata CASCADE")
        await cur.execute("DROP TABLE IF EXISTS sync_run_history CASCADE")
        await cur.execute("DROP TABLE IF EXISTS readings CASCADE")
        await cur.execute("DROP TABLE IF EXISTS manual_uploads CASCADE")
    await conn.commit()


@pytest.fixture()
async def red_pool(red_tsdb_conn):
    await _drop_red_schema(red_tsdb_conn)
    pool = AsyncConnectionPool(RED_TSDB_DSN, min_size=1, max_size=2, open=False)
    await pool.open()
    await ensure_schema_red(pool)
    yield pool
    await pool.close()
    await _drop_red_schema(red_tsdb_conn)


async def _rows(pool) -> list[dict]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT time, device_name, sensor_tag, value, source FROM readings ORDER BY source"
        )
        return list(await cur.fetchall())


async def test_letsgrow_row_is_stored_in_utc_under_its_source(red_pool):
    async with red_pool.connection() as conn:
        _, created = await upsert_readings(
            conn, [letsgrow.to_params(ROW)], source=letsgrow.SOURCE
        )
        await conn.commit()

    assert created == 1
    [row] = await _rows(red_pool)
    assert row["source"] == "letsgrow"
    assert row["device_name"] == "letsgrow-climate-b4"
    assert row["sensor_tag"] == "lg_temp_greenhouse"
    assert row["value"] == 21.5
    # 14:00 Amsterdam summer time.
    assert row["time"] == datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


async def test_resync_updates_in_place(red_pool):
    """The client re-emits rows when it bisects a window; that must not duplicate."""
    async with red_pool.connection() as conn:
        await upsert_readings(conn, [letsgrow.to_params(ROW)], source=letsgrow.SOURCE)
        newer = ROW.model_copy(update={"value": 22.0})
        upserted, created = await upsert_readings(
            conn, [letsgrow.to_params(newer)], source=letsgrow.SOURCE
        )
        await conn.commit()

    assert (upserted, created) == (1, 0)
    [row] = await _rows(red_pool)
    assert row["value"] == 22.0


async def test_same_key_under_another_source_is_a_separate_reading(red_pool):
    """Red's dedup key includes source, so LetsGrow cannot overwrite a Sijia row."""
    params = letsgrow.to_params(ROW)
    async with red_pool.connection() as conn:
        await upsert_readings(conn, [params], source=letsgrow.SOURCE)
        _, created = await upsert_readings(conn, [params], source="sijia")
        await conn.commit()

    assert created == 1
    assert [r["source"] for r in await _rows(red_pool)] == ["letsgrow", "sijia"]
