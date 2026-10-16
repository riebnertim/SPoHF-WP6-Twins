"""Registry of red's manual-upload sources.

The one list of red :class:`ManualSource` descriptors fed by the manual
upload path (currently only Sijia). "Manual" data — for the status-page
coverage split — is defined as readings whose ``source`` matches one of
these, as distinct from automated feeds that also carry a metadata
``source`` (the LoRaWAN tables via MySQL, and the letsgrow sync).

Add a descriptor here when red gains another manual source.
"""

from wp6_data.red.sijia.source import SIJIA
from wp6_data.shared.manual_ingest.source import ManualSource

MANUAL_SOURCES: tuple[ManualSource, ...] = (SIJIA,)

# The `readings.source` / `daily_coverage.source` values for the above —
# what coverage rows are matched against to tag them manual.
MANUAL_SOURCE_VALUES: frozenset[str] = frozenset(
    s.categorical_value for s in MANUAL_SOURCES
)
