"""Pydantic models for SPoHF API responses."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SensorReading(BaseModel):
    """Single sensor reading from the API.

    Each row represents one measurement value for one sensor tag.
    Multiple tags (soilMoisture, temperature, etc.) come as separate rows.
    """

    sensor_id: str  # UUID of the device
    device_name: str = "unknown"  # Optional - some records don't have it
    sensor_tag: str  # Measurement type: solarRadiation, soilMoisture, etc.
    value: str  # Coerced to string
    metadata: dict[str, Any] | None = None
    datetime_measure: datetime  # When measurement was taken
    # Fields we deliberately don't model, because Pydantic drops unknown keys and
    # we never persist or act on them:
    #   - `project`: the relay's pipeline tag. Blue dropped `readings.project` when
    #     yookr-direct was retired; automated rows now take `source`'s default.
    #   - the ingestion-time field (`timestamp`, renamed to `synced_at` ~2026-07-07):
    #     a datalake internal. Freshness is derived from datetime_measure instead.

    @field_validator("value", mode="before")
    @classmethod
    def coerce_value_to_string(cls, v: Any) -> str:
        """Coerce value to string - API sometimes returns float."""
        return str(v) if v is not None else ""

    @property
    def value_float(self) -> float | None:
        """Parse value to float, returning None if not parseable."""
        try:
            return float(self.value)
        except (ValueError, TypeError):
            return None


class Page[RowT: BaseModel](BaseModel):
    """One page of any relay endpoint. Every endpoint pages the same way; only
    the row shape differs, which is what `RowT` carries through the client."""

    results: list[RowT]
    count: int = Field(description="Number of results in this page")


class ApiResponse(Page[SensorReading]):
    """Paginated API response wrapper (`yookr-data`)."""


class LetsGrowReading(BaseModel):
    """Single LetsGrow reading from the relay (`letsgrow-data`).

    One row is one value of one LetsGrow *column* (`col_id`) inside one *module*
    (a LetsGrow screen such as "Climate B4"). Its shape is unrelated to
    yookr's: there is no device/tag pair, only the module and a free-text
    `description` that carries the unit ("Temperature greenhouse - °C - 5 min").
    `col_id` is the stable identity of the quantity; the description is a label.
    """

    module_id: int
    module_name: str
    col_id: int
    description: str = ""
    # Greenhouse wall-clock time, naive: LetsGrow exports local time, not UTC
    # (outdoor radiation peaks at ~13:15 here in October). Localised on ingest.
    timestamp: datetime
    value: float | None = None
    # Not modelled: `offset` (always 0) and `synced_at` (the relay's ingestion
    # time — freshness is derived from the measurement time instead).


class LetsGrowResponse(Page[LetsGrowReading]):
    """Paginated `letsgrow-data` response wrapper."""
