"""Tests for wp6_data.api.client — mock httpx.AsyncClient."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from wp6_data.api.client import MIN_WINDOW, SpoHFClient, parse_api_timestamp
from wp6_data.api.models import ApiResponse

from .conftest import make_api_response, make_reading

# --- parse_api_timestamp ---


class TestParseApiTimestamp:
    def test_z_suffix(self):
        dt = parse_api_timestamp("2024-06-15T12:00:00Z")
        assert dt == datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

    def test_offset_suffix(self):
        dt = parse_api_timestamp("2024-06-15T12:00:00+00:00")
        assert dt == datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


# --- SpoHFClient init ---


class TestClientInit:
    def test_trailing_slash_stripped(self):
        client = SpoHFClient("https://api.example.com/", "token")
        assert client.base_url == "https://api.example.com"

    def test_page_size_default(self):
        client = SpoHFClient("https://api.example.com", "token")
        assert client.page_size == 1000

    def test_custom_page_size(self):
        client = SpoHFClient("https://api.example.com", "token", page_size=500)
        assert client.page_size == 500

    def test_auth_header_set(self):
        client = SpoHFClient("https://api.example.com", "my-token")
        assert client._headers["Authorization"] == "Bearer my-token"


# --- _fetch_page ---


def _mock_response(json_data, status=200):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    resp.text = ""
    return resp


class TestFetchPage:
    @pytest.mark.asyncio()
    async def test_success_path(self):
        client = SpoHFClient("https://api.example.com", "token", page_size=10)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        reading_data = {
            "sensor_id": "d1",
            "sensor_tag": "temp",
            "value": "21.5",
            "datetime_measure": "2024-06-15T12:00:00+00:00",
            "timestamp": "2024-06-15T12:00:01+00:00",
        }
        mock_http.get.return_value = _mock_response(
            {"results": [reading_data], "count": 1}
        )

        ts_from = datetime(2024, 6, 1, tzinfo=UTC)
        ts_until = datetime(2024, 6, 2, tzinfo=UTC)
        # Bypass tenacity retry for unit test
        result = await client._fetch_page.__wrapped__(
            client, mock_http, "yookr-data", ts_from, ts_until, 0, ApiResponse
        )
        assert len(result.results) == 1
        assert result.count == 1

    @pytest.mark.asyncio()
    async def test_correct_params_passed(self):
        client = SpoHFClient("https://api.example.com", "token", page_size=50)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get.return_value = _mock_response({"results": [], "count": 0})

        ts_from = datetime(2024, 6, 1, tzinfo=UTC)
        ts_until = datetime(2024, 6, 2, tzinfo=UTC)
        await client._fetch_page.__wrapped__(
            client, mock_http, "ep1", ts_from, ts_until, 100, ApiResponse
        )

        call_kwargs = mock_http.get.call_args
        assert call_kwargs.kwargs["params"]["timestamp_from"] == ts_from.isoformat()
        assert call_kwargs.kwargs["params"]["timestamp_until"] == ts_until.isoformat()
        assert call_kwargs.kwargs["params"]["size"] == "50"
        assert call_kwargs.kwargs["params"]["from"] == "100"
        assert "api.example.com/api/v1/data/ep1" in call_kwargs.args[0]


# --- fetch_window ---


class TestFetchWindow:
    @pytest.mark.asyncio()
    async def test_single_page(self):
        client = SpoHFClient("https://api.example.com", "token", page_size=10)
        reading = make_reading()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [reading.model_dump(mode="json")],
            "count": 1,
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("wp6_data.api.client.httpx.AsyncClient") as MockClient:
            mock_http = AsyncMock()
            mock_http.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = []
            ts_from = datetime(2024, 1, 1, tzinfo=UTC)
            ts_until = datetime(2024, 1, 2, tzinfo=UTC)
            async for r in client.fetch_window("ep1", ts_from, ts_until):
                results.append(r)

        assert len(results) == 1

    @pytest.mark.asyncio()
    async def test_empty_results(self):
        client = SpoHFClient("https://api.example.com", "token", page_size=10)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": [], "count": 0}
        mock_resp.raise_for_status = MagicMock()

        with patch("wp6_data.api.client.httpx.AsyncClient") as MockClient:
            mock_http = AsyncMock()
            mock_http.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = []
            ts_from = datetime(2024, 1, 1, tzinfo=UTC)
            ts_until = datetime(2024, 1, 2, tzinfo=UTC)
            async for r in client.fetch_window("ep1", ts_from, ts_until):
                results.append(r)

        assert len(results) == 0

    @pytest.mark.asyncio()
    async def test_multi_page_pagination(self):
        client = SpoHFClient("https://api.example.com", "token", page_size=1)

        r1 = make_reading(sensor_tag="a")
        r2 = make_reading(sensor_tag="b")

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "results": [r1.model_dump(mode="json")],
            "count": 1,  # count == page_size => more pages
        }
        page1.raise_for_status = MagicMock()

        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "results": [r2.model_dump(mode="json")],
            "count": 0,  # count < page_size => last page
        }
        page2.raise_for_status = MagicMock()

        with patch("wp6_data.api.client.httpx.AsyncClient") as MockClient:
            mock_http = AsyncMock()
            mock_http.get.side_effect = [page1, page2]
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = []
            ts_from = datetime(2024, 1, 1, tzinfo=UTC)
            ts_until = datetime(2024, 1, 2, tzinfo=UTC)
            async for r in client.fetch_window("ep1", ts_from, ts_until):
                results.append(r)

        assert len(results) == 2


# --- fetch_window: result-cap bisection ---


def _capped_backend(readings, page_size, cap):
    """Fake the relay: offset paging over a time-filtered slice, capped like ES.

    Mirrors `backoffice.spohf.com`'s Elasticsearch `max_result_window`: a request
    for `from + size > cap` is never issued by the client, so anything past `cap`
    in a single window is unreachable by offset alone.
    """

    async def _fetch_page(
        _self, client, endpoint, ts_from, ts_until, offset, response_model=None
    ):
        assert offset + page_size <= cap, "client must never request past the cap"
        rows = sorted(
            (r for r in readings if ts_from <= r.datetime_measure < ts_until),
            key=lambda r: r.datetime_measure,
        )
        page = rows[offset : offset + page_size]
        return make_api_response(page)

    return _fetch_page


class TestFetchWindowResultCap:
    """A window holding more than the cap must be split, never silently truncated."""

    @staticmethod
    def _spread(n, start, span):
        return [
            make_reading(
                sensor_tag=f"s{i}",
                datetime_measure=start + (span / n) * i,
            )
            for i in range(n)
        ]

    @pytest.mark.asyncio()
    async def test_window_over_cap_is_split_and_loses_nothing(self):
        cap, page_size = 4, 2
        start = datetime(2024, 1, 1, tzinfo=UTC)
        span = timedelta(hours=8)
        readings = self._spread(6, start, span)  # 6 > cap

        client = SpoHFClient("https://api.example.com", "token", page_size=page_size)

        with (
            patch("wp6_data.api.client.MAX_RESULT_WINDOW", cap),
            patch("wp6_data.api.client.httpx.AsyncClient"),
            patch.object(
                SpoHFClient,
                "_fetch_page",
                _capped_backend(readings, page_size, cap),
            ),
        ):
            got = [r async for r in client.fetch_window("ep1", start, start + span)]

        # Every reading surfaces, even those past the cap in the un-split window.
        assert {r.sensor_tag for r in got} == {r.sensor_tag for r in readings}

    @pytest.mark.asyncio()
    async def test_split_re_emits_rows_from_the_truncated_attempt(self):
        """Documents the contract: callers must be idempotent."""
        cap, page_size = 4, 2
        start = datetime(2024, 1, 1, tzinfo=UTC)
        span = timedelta(hours=8)
        readings = self._spread(6, start, span)

        client = SpoHFClient("https://api.example.com", "token", page_size=page_size)

        with (
            patch("wp6_data.api.client.MAX_RESULT_WINDOW", cap),
            patch("wp6_data.api.client.httpx.AsyncClient"),
            patch.object(
                SpoHFClient,
                "_fetch_page",
                _capped_backend(readings, page_size, cap),
            ),
        ):
            got = [r async for r in client.fetch_window("ep1", start, start + span)]

        assert len(got) > len(readings)

    @pytest.mark.asyncio()
    async def test_window_under_cap_is_not_split(self):
        cap, page_size = 4, 2
        start = datetime(2024, 1, 1, tzinfo=UTC)
        span = timedelta(hours=8)
        readings = self._spread(3, start, span)  # 3 < cap

        client = SpoHFClient("https://api.example.com", "token", page_size=page_size)

        with (
            patch("wp6_data.api.client.MAX_RESULT_WINDOW", cap),
            patch("wp6_data.api.client.httpx.AsyncClient"),
            patch.object(
                SpoHFClient,
                "_fetch_page",
                _capped_backend(readings, page_size, cap),
            ),
        ):
            got = [r async for r in client.fetch_window("ep1", start, start + span)]

        assert len(got) == len(readings)

    @pytest.mark.asyncio()
    async def test_unsplittable_window_terminates(self):
        """A single instant over the cap cannot be split — bail out, don't recurse forever."""
        cap, page_size = 4, 2
        start = datetime(2024, 1, 1, tzinfo=UTC)
        instant = [make_reading(sensor_tag=f"s{i}") for i in range(6)]
        for r in instant:
            r.datetime_measure = start

        client = SpoHFClient("https://api.example.com", "token", page_size=page_size)

        with (
            patch("wp6_data.api.client.MAX_RESULT_WINDOW", cap),
            patch("wp6_data.api.client.httpx.AsyncClient"),
            patch.object(
                SpoHFClient,
                "_fetch_page",
                _capped_backend(instant, page_size, cap),
            ),
        ):
            got = [r async for r in client.fetch_window("ep1", start, start + MIN_WINDOW)]

        assert len(got) == cap  # what offset paging can reach; the rest is unreachable
