"""SPoHF API client with pagination and retry logic."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import httpx
import structlog
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wp6_data.api.models import ApiResponse, Page, SensorReading

# Each relay endpoint has its own row shape (yookr's device/tag rows vs
# LetsGrow's module/column rows); the paging, retry and bisection below are
# identical for all of them. `response_model` picks the shape, and its row type
# is what the iterators yield.

logger = structlog.get_logger()

# The relay is Elasticsearch-backed and refuses `from + size > max_result_window`.
# Offset paging therefore cannot reach past this many records in one time window;
# a window holding more is truncated, not paginated. `fetch_window` bisects instead.
MAX_RESULT_WINDOW = 10_000

# Stop bisecting here. A window this narrow that still overflows means a single
# instant holds >MAX_RESULT_WINDOW records, which no time-split can separate.
MIN_WINDOW = timedelta(seconds=1)


def parse_api_timestamp(ts_str: str) -> datetime:
    """Parse API timestamp string to datetime."""
    # Handle both Z suffix and +00:00
    ts_str = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(ts_str)


class SpoHFClient:
    """Async client for SPoHF sensor data API."""

    def __init__(self, base_url: str, token: str, page_size: int = 1000):
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    )
    async def _fetch_page[RowT: BaseModel](
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        timestamp_from: datetime,
        timestamp_until: datetime,
        offset: int,
        response_model: type[Page[RowT]],
    ) -> Page[RowT]:
        """Fetch a single page with retry logic."""
        url = f"{self.base_url}/api/v1/data/{endpoint}"
        params = {
            "timestamp_from": timestamp_from.isoformat(),
            "timestamp_until": timestamp_until.isoformat(),
            "size": str(int(self.page_size)),
            "from": str(int(offset)),
        }

        logger.info("fetching_page", endpoint=endpoint, offset=offset)

        response = await client.get(
            url,
            params=params,
            headers=self._headers,
            timeout=30.0,
        )
        response.raise_for_status()

        data = response.json()
        return response_model.model_validate(data)

    async def fetch_window[RowT: BaseModel = SensorReading](
        self,
        endpoint: str,
        timestamp_from: datetime,
        timestamp_until: datetime,
        *,
        response_model: type[Page[RowT]] = ApiResponse,
    ) -> AsyncIterator[RowT]:
        """Yield every record in one time window, bisecting past the result cap.

        Args:
            endpoint: API endpoint (e.g., "yookr-data")
            timestamp_from: Start of window (inclusive)
            timestamp_until: End of window (exclusive)
            response_model: The endpoint's page shape (default: yookr's).

        Yields:
            The page's rows: `SensorReading` by default, `LetsGrowReading` for
            `LetsGrowResponse`. A window
            exceeding MAX_RESULT_WINDOW is split in half and re-fetched, so
            records already yielded from the truncated attempt are emitted
            again. Callers must be idempotent (``upsert_readings`` is) —
            duplicates are the price of never silently dropping the tail.
        """
        async with httpx.AsyncClient() as client:
            total = 0
            async for reading in self._fetch_range(
                client, endpoint, timestamp_from, timestamp_until, response_model
            ):
                total += 1
                yield reading

            logger.info(
                "fetch_window_complete",
                endpoint=endpoint,
                window_from=timestamp_from.isoformat(),
                window_until=timestamp_until.isoformat(),
                records=total,
            )

    async def _fetch_range[RowT: BaseModel](
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        timestamp_from: datetime,
        timestamp_until: datetime,
        response_model: type[Page[RowT]],
    ) -> AsyncIterator[RowT]:
        """Page one range; on hitting the result cap, bisect and recurse."""
        offset = 0

        while True:
            try:
                response = await self._fetch_page(
                    client, endpoint, timestamp_from, timestamp_until, offset,
                    response_model,
                )
            except httpx.HTTPStatusError as e:
                logger.error(
                    "api_error",
                    endpoint=endpoint,
                    status=e.response.status_code,
                    detail=e.response.text[:200],
                )
                raise

            if not response.results:
                return

            for reading in response.results:
                yield reading

            # A short page is the only honest end-of-range signal.
            if response.count < self.page_size:
                return

            offset += self.page_size

            # The next page would need `from + size` past the relay's cap, which it
            # refuses. Everything beyond here is unreachable by offset — split the
            # range so each half fits, rather than mistaking the cap for "no more".
            if offset + self.page_size > MAX_RESULT_WINDOW:
                span = timestamp_until - timestamp_from
                if span <= MIN_WINDOW:
                    logger.error(
                        "window_unsplittable",
                        endpoint=endpoint,
                        window_from=timestamp_from.isoformat(),
                        window_until=timestamp_until.isoformat(),
                        cap=MAX_RESULT_WINDOW,
                        hint="a single instant exceeds the result cap; records are lost",
                    )
                    return

                midpoint = timestamp_from + span / 2
                logger.warning(
                    "window_truncated_splitting",
                    endpoint=endpoint,
                    window_from=timestamp_from.isoformat(),
                    window_until=timestamp_until.isoformat(),
                    cap=MAX_RESULT_WINDOW,
                )
                for lo, hi in (
                    (timestamp_from, midpoint),
                    (midpoint, timestamp_until),
                ):
                    async for reading in self._fetch_range(
                        client, endpoint, lo, hi, response_model
                    ):
                        yield reading
                return
