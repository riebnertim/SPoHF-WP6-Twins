"""Device and sensor metadata registry.

Loads manually-enriched metadata from a per-twin YAML file and provides
lookup + API enrichment helpers. Complements the dynamic sensor lists
from the database (cached by sensor_summary.py) with static descriptive
information that rarely changes.

YAML structure::

    sensor_defaults:          # shared unit/alias/type for measurement keys
      par:
        type: radiation
        unit: "μmol/m²/s"
        alias: PAR

    devices:                  # each device lists its sensors inline
      s2100-01-par:
        description: "PAR above lamp level"
        position: B4
        sensors:
          par:                # inherits from sensor_defaults, can override
            intention: "Measures PAR above grow lamp"
"""

from __future__ import annotations

from collections import defaultdict
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

from wp6_data.shared.aggregation import CHART_AGG_FUNCS

_GLOB_CHARS = frozenset("*?[")


def _is_glob(key: str) -> bool:
    """True if a device key is a wildcard pattern (vs. a literal device name)."""
    return any(c in _GLOB_CHARS for c in key)


class SensorMetadata(BaseModel):
    """Metadata for a sensor / measurement type."""

    type: str = ""
    unit: str = ""
    alias: str = ""
    intention: str = ""
    source: str = ""  # routing key; "" = MySQL default for red, datalake for blue
    # A non-empty source is a manual upload unless the twin lists it under
    # `automated_sources` (see `MetadataRegistry.is_manual_source`).
    # How several readings of this measure combine into one figure for a period.
    # "avg" is right for anything measured *per item* — a concentration, a
    # per-berry weight, a score — which is every measure either twin takes
    # today. Declare "sum" for a measure that is a *total* over the period, such
    # as a yield picked across several harvests: averaging those would report a
    # fraction of the real figure. Validated against CHART_AGG_FUNCS on load.
    agg: str = "avg"

    @field_validator("agg")
    @classmethod
    def _known_agg(cls, value: str) -> str:
        if value not in CHART_AGG_FUNCS:
            raise ValueError(
                f"unknown agg {value!r}; expected one of {sorted(CHART_AGG_FUNCS)}"
            )
        return value


class DeviceMetadata(BaseModel):
    """Metadata for a physical device and its location."""

    description: str = ""
    position: str = ""
    latitude: float | None = None
    longitude: float | None = None
    type: str = ""
    source: str = ""  # UI-labelling hint; canonical routing is sensor-level
    sensors: dict[str, SensorMetadata] = {}


class TwinMetadata(BaseModel):
    """Complete metadata for one digital twin, loaded from YAML."""

    sensor_defaults: dict[str, SensorMetadata] = {}
    devices: dict[str, DeviceMetadata] = {}
    # Sources whose readings a sync job ingests rather than a person uploads.
    # They route like any sourced sensor but are shown as automated.
    automated_sources: list[str] = []


class MetadataRegistry:
    """Loads and serves device/sensor metadata from a YAML file."""

    def __init__(self, yaml_path: Path) -> None:
        if yaml_path.exists():
            with yaml_path.open(encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self._meta = TwinMetadata(**raw)
        else:
            self._meta = TwinMetadata()
        # Device keys containing a glob metacharacter are wildcard patterns
        # (e.g. "Org1 / plant *"), letting a family of data-driven devices
        # inherit one entry. Pre-sorted longest-first so the most specific
        # pattern wins; exact keys always take precedence (see `_resolve`).
        self._device_patterns: list[str] = sorted(
            (k for k in self._meta.devices if _is_glob(k)),
            key=lambda k: (-len(k), k),
        )

    def _resolve(self, device_key: str) -> DeviceMetadata | None:
        """Resolve a device to its metadata: exact match, else most-specific
        wildcard pattern, else ``None``."""
        exact = self._meta.devices.get(device_key)
        if exact is not None:
            return exact
        for pattern in self._device_patterns:
            if fnmatchcase(device_key, pattern):
                return self._meta.devices[pattern]
        return None

    def device(self, device_key: str) -> DeviceMetadata:
        """Return metadata for a device, or empty defaults if not enriched."""
        return self._resolve(device_key) or DeviceMetadata()

    def sensor_default(self, sensor_key: str) -> SensorMetadata:
        """Return the global default metadata for a measurement key."""
        return self._meta.sensor_defaults.get(sensor_key, SensorMetadata())

    @property
    def sensor_defaults(self) -> dict[str, SensorMetadata]:
        """Read-only view of all sensor_defaults entries."""
        return self._meta.sensor_defaults

    @property
    def devices(self) -> dict[str, DeviceMetadata]:
        """Read-only view of all device entries."""
        return self._meta.devices

    def is_manual_source(self, source: str) -> bool:
        """True if readings tagged ``source`` come from a manual upload.

        ``""`` is the twin's default automated feed; any other source is manual
        unless declared under ``automated_sources``.
        """
        return bool(source) and source not in self._meta.automated_sources

    def sensor(
        self, sensor_key: str, device_key: str | None = None,
    ) -> SensorMetadata:
        """Return merged sensor metadata (device-specific over defaults).

        Fields set on the device-level sensor override the defaults.
        """
        defaults = self.sensor_default(sensor_key)
        if device_key is None:
            return defaults

        dev = self._resolve(device_key)
        if dev is None:
            return defaults

        override = dev.sensors.get(sensor_key)
        if override is None:
            return defaults

        # Merge: override wins for non-default fields
        merged = defaults.model_dump()
        for field, value in override.model_dump(exclude_defaults=True).items():
            merged[field] = value
        return SensorMetadata(**merged)

    def sensor_types(self) -> dict[str, list[str]]:
        """Return mapping of sensor type → list of sensor keys.

        Built from sensor_defaults. Useful for home page grouping.
        """
        types: dict[str, list[str]] = defaultdict(list)
        for key, meta in self._meta.sensor_defaults.items():
            if meta.type:
                types[meta.type].append(key)
        return dict(types)

    def enrich_sensor_list(
        self, flat_sensors: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Group a flat sensor list by device and attach metadata.

        Input:  [{"device": "d1", "sensor": "s1"}, ...]
        Output: nested by device with metadata attached.
        """
        grouped: dict[str, list[str]] = defaultdict(list)
        device_order: list[str] = []
        for entry in flat_sensors:
            dev = entry["device"]
            if dev not in grouped:
                device_order.append(dev)
            grouped[dev].append(entry["sensor"])

        result: list[dict[str, Any]] = []
        for dev in device_order:
            dev_obj = self.device(dev)
            device_meta = dev_obj.model_dump(
                exclude_defaults=True, exclude={"sensors"},
            )
            sensors = []
            for sensor_key in grouped[dev]:
                merged = self.sensor(sensor_key, dev)
                sensor_meta = merged.model_dump(exclude_defaults=True)
                entry: dict[str, Any] = {"sensor": sensor_key}
                if sensor_meta:
                    entry["meta"] = sensor_meta
                sensors.append(entry)

            device_entry: dict[str, Any] = {"device": dev, "sensors": sensors}
            if device_meta:
                device_entry["meta"] = device_meta
            result.append(device_entry)

        return result
