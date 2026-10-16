"""LetsGrow — the greenhouse climate computer's data, relayed by SPoHF.

LetsGrow is Neurath's own process-control system: it logs the climate, water,
lighting and weather-station values the grower steers on, including actuators
(vents, pipes, screens) that red's own sensors cannot see. Red never talks to
LetsGrow; it reads the relay's `letsgrow-data` endpoint, exactly as blue reads
yookr through `yookr-data`, and stores the rows in red's `readings` under
``source = 'letsgrow'``.

A relay row names a LetsGrow *module* (a screen, e.g. "Climate B4") and a
*column* inside it (`col_id`, e.g. 1467902 "Temperature greenhouse - °C").
Both become red's (device, sensor) pair:

* device = ``letsgrow-`` + the module name, slugged (`letsgrow-climate-b4`).
* sensor = the ``lg_`` tag that :data:`COLUMNS` declares for the `col_id`. The
  prefix is load-bearing: red routes a query to TimescaleDB *by sensor tag*
  (`RedSensorProvider.tsdb_sensors`), so a LetsGrow tag must never equal a
  legacy MySQL tag such as ``temp`` or ``par``.

A column missing from :data:`COLUMNS` is still stored — as ``lg_col_<col_id>``
— and logged, so a new LetsGrow column is never silently dropped; declaring it
here and in `metadata.yaml` is what makes it visible on the dashboard.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from wp6_data.api.models import LetsGrowReading

logger = structlog.get_logger()

ENDPOINT = "letsgrow-data"

# `readings.source` for every LetsGrow row.
SOURCE = "letsgrow"

# LetsGrow exports the greenhouse's wall clock, not UTC. Fixed, not
# `display_timezone`: this is where the timestamps were *recorded*, which a
# viewer's display preference must not change.
#
# DST: on the autumn fall-back night 02:00-03:00 happens twice, and the naive
# timestamps cannot say which. Both resolve to the first occurrence (fold=0),
# so the repeated hour upserts onto one set of rows — one hour a year is lost.
GREENHOUSE_TZ = ZoneInfo("Europe/Amsterdam")

# LetsGrow delivers once a day: the relay holds data up to local midnight,
# synced the next morning (~08:30). So a healthy feed is up to ~33 h behind,
# and the 2 h/6 h budget yookr uses would flag it permanently stale.
FRESHNESS_BUDGET_HOURS: tuple[float, float] = (36.0, 60.0)

# col_id -> sensor tag. One tag per *quantity*: the same quantity measured by
# two modules (greenhouse temperature in Climate and Plant balance, radiation
# sum in three) shares a tag, and the device tells them apart. Descriptions as
# the relay reports them (probed 2026-10-16), unit included.
COLUMNS: dict[int, str] = {
    # Climate B4
    1467547: "lg_abs_hum_greenhouse",   # Absolute humidity content greenhouse air - g/m³
    1467551: "lg_vpd",                  # Greenhouse: VPD - kPa
    1467902: "lg_temp_greenhouse",      # Temperature greenhouse - °C
    1467906: "lg_rh_greenhouse",        # RH greenhouse - %
    1467910: "lg_hum_deficit_greenhouse",  # Humidity deficit greenhouse - g/m³
    1467918: "lg_par",                  # Plant: PAR - µmol/m²/s
    1467922: "lg_par_sum",              # PAR sum - mol/m²
    1467924: "lg_co2",                  # CO2 greenhouse - ppm
    1467928: "lg_co2_kg",               # CO2 - kg
    1467930: "lg_heating_setpoint",     # Heating temp: setpoint - °C
    1467934: "lg_pipe_lower",           # Lower circuit - °C
    1467938: "lg_pipe_lower_calc",      # Lower circuit: calc - °C
    1467942: "lg_pipe_growth",          # Growth circuit - °C
    1467946: "lg_pipe_growth_calc",     # Growth circuit: calc - °C
    1467950: "lg_vent_lee",             # Vent lee - %
    1467954: "lg_vent_lee_calc",        # Lee side vent position: calc - %
    1467958: "lg_vent_wind",            # Vent wind - %
    1467962: "lg_vent_wind_calc",       # Wind side vent position: calc - %
    1467966: "lg_vent_setpoint",        # Ventilationtemp: setpoint - °C
    1467970: "lg_vent_temp_lee_calc",   # Lee side ventilation temp: calc - °C
    1467974: "lg_vent_temp_wind_calc",  # Wind side ventilation temp: calc - °C
    1467978: "lg_energy_curtain",       # Energy curtain - %
    1467982: "lg_fans",                 # Fans: status (1=off, 2=on)
    3947425: "lg_blackout_curtain",     # Black-out curtain - %
    # Grow light B4
    1467602: "lg_assim_light_on",       # Assimilation lighting: on
    1467604: "lg_assim_light_pct",      # Assimilation lights part - %
    1621012: "lg_led1_on",              # LED lighting 1: on
    # Plant balance B4
    1468140: "lg_temp_greenhouse",      # Temperature greenhouse - °C
    1468144: "lg_rad_sum",              # Radiation sum - J/cm²
    # Water B4
    1467600: "lg_water_uptake",         # Water uptake - l/m²
    1468076: "lg_drain_pct",            # Drain - %
    1468078: "lg_drain_ec",             # Drain - EC
    1468080: "lg_drain_ph",             # Drain - pH
    1468082: "lg_irrigation_starts",    # Crop section: number of starts total
    1468084: "lg_substrate_weight",     # Substrate weight - kg
    1468086: "lg_rad_sum",              # Radiation sum - J/cm²
    1468088: "lg_irrigation",           # Irrigation - l/m²
    1468090: "lg_irrigation_ec",        # Irrigation - EC
    1468092: "lg_irrigation_ph",        # Irrigation - pH
    1468094: "lg_drain",                # Drain - l/m²
    # Weather station
    1467360: "lg_abs_hum_outside",      # Absolute humidity content outside air - g/m³
    1468096: "lg_temp_outside",         # Outside temperature - °C
    1468100: "lg_rh_outside",           # Outside RH - %
    1468108: "lg_rad",                  # Radiation - W/m²
    1468110: "lg_rad_sum",              # Radiation sum - J/cm²
    1468112: "lg_wind_speed",           # Wind speed - m/s
    1468116: "lg_wind_dir",             # Wind direction registration
    1468120: "lg_rain",                 # Rain (status 1=rain, 0=dry)
    3167090: "lg_hum_deficit_outside",  # Humidity deficit outside - g/m³
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# An undeclared column reports every 5 minutes; say so once per run, not per row.
_warned_undeclared: set[int] = set()


def device_name(module_name: str) -> str:
    """Red device id for a LetsGrow module: ``"Climate B4"`` -> ``letsgrow-climate-b4``."""
    return "letsgrow-" + _SLUG_RE.sub("-", module_name.lower()).strip("-")


def sensor_tag(col_id: int) -> str:
    """Red sensor tag for a LetsGrow column; undeclared columns keep their id."""
    return COLUMNS.get(col_id, f"lg_col_{col_id}")


def measured_at(reading: LetsGrowReading) -> datetime:
    """The reading's measurement time in UTC, from LetsGrow's local wall clock."""
    ts = reading.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=GREENHOUSE_TZ)
    return ts.astimezone(UTC)


def to_params(reading: LetsGrowReading) -> dict[str, Any]:
    """Map one relay row onto the `upsert_readings` parameter shape."""
    if reading.col_id not in COLUMNS and reading.col_id not in _warned_undeclared:
        _warned_undeclared.add(reading.col_id)
        logger.warning(
            "letsgrow_column_undeclared",
            col_id=reading.col_id,
            module=reading.module_name,
            description=reading.description,
            hint="stored as lg_col_<id>; declare it in red/letsgrow.py + metadata.yaml",
        )
    return {
        "device_name": device_name(reading.module_name),
        "sensor_tag": sensor_tag(reading.col_id),
        "value": "" if reading.value is None else str(reading.value),
        "datetime_measure": measured_at(reading).isoformat(),
    }
