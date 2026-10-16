"""Which relay endpoint feeds which twin, and how its rows become readings.

The relay serves several endpoints through one API, but each has its own row
shape and belongs to one twin's database: `yookr-data` is blue's soil and
microclimate sensors, `letsgrow-data` is red's greenhouse climate computer. A
:class:`Feed` carries everything the orchestrator needs to know that differs
between them; paging, windowing, dedup early-stop and run bookkeeping are
shared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from wp6_data.api.client import SpoHFClient
from wp6_data.api.models import (
    ApiResponse,
    LetsGrowReading,
    LetsGrowResponse,
    Page,
    SensorReading,
)
from wp6_data.blue.tsdb import ensure_schema_blue
from wp6_data.config import RedSettings, Settings
from wp6_data.red import letsgrow
from wp6_data.red.tsdb import ensure_schema_red


@dataclass(frozen=True)
class Feed[RowT: BaseModel]:
    """One relay endpoint's route into a twin's TimescaleDB.

    `RowT` ties the page shape to the functions that read its rows, so a feed
    cannot pair LetsGrow pages with yookr's mapping.
    """

    twin: str
    response_model: type[Page[RowT]]
    dsn: Callable[[Settings], str]
    ensure_schema: Callable[[AsyncConnectionPool], Awaitable[None]]
    to_params: Callable[[RowT], dict[str, Any]]
    # The measurement time, tz-aware, for the sync watermark.
    measured_at: Callable[[RowT], datetime]
    # `readings.source` to stamp; None leaves blue's column at its default.
    source: str | None = None

    async def rows(
        self,
        client: SpoHFClient,
        endpoint: str,
        timestamp_from: datetime,
        timestamp_until: datetime,
    ) -> AsyncIterator[tuple[dict[str, Any], datetime]]:
        """Fetch one window and yield each row as (upsert params, measured at).

        Kept on the feed so the row type never leaves it: the orchestrator
        handles feeds of different row types and only ever sees the params.
        """
        async for row in client.fetch_window(
            endpoint, timestamp_from, timestamp_until,
            response_model=self.response_model,
        ):
            yield self.to_params(row), self.measured_at(row)


# Every registered feed. A union rather than `Feed[Any]`, so each member keeps
# its row type.
AnyFeed = Feed[SensorReading] | Feed[LetsGrowReading]


def _yookr_to_params(reading: SensorReading) -> dict[str, Any]:
    """The relay's own `reading.project` is ignored — it arrives as 'unknown'
    and the column no longer exists. Automated rows take `source`'s default.
    """
    return {
        "device_name": reading.device_name,
        "sensor_tag": reading.sensor_tag,
        "value": reading.value,
        "datetime_measure": reading.datetime_measure.isoformat(),
    }


def _yookr_measured_at(reading: SensorReading) -> datetime:
    ts = reading.datetime_measure
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


# Blue's yookr shape. Also the fallback for any endpoint not registered below,
# which is how every endpoint was treated before red had a feed.
YOOKR: Feed[SensorReading] = Feed(
    twin="blue",
    response_model=ApiResponse,
    dsn=lambda settings: settings.tsdb_url,
    ensure_schema=ensure_schema_blue,
    to_params=_yookr_to_params,
    measured_at=_yookr_measured_at,
)

LETSGROW: Feed[LetsGrowReading] = Feed(
    twin="red",
    response_model=LetsGrowResponse,
    # Red's own database setting, never WP6_TSDB_URL: a misconfigured job must
    # not be able to write greenhouse rows into blue.
    dsn=lambda _settings: RedSettings().tsdb_url,
    ensure_schema=ensure_schema_red,
    to_params=letsgrow.to_params,
    measured_at=letsgrow.measured_at,
    source=letsgrow.SOURCE,
)

FEEDS: dict[str, AnyFeed] = {
    "yookr-data": YOOKR,
    letsgrow.ENDPOINT: LETSGROW,
}


def feed_for(endpoint: str) -> AnyFeed:
    """The feed for an endpoint; unregistered endpoints are yookr-shaped blue."""
    return FEEDS.get(endpoint, YOOKR)

