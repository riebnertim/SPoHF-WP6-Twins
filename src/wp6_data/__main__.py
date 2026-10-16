"""Entry point for wp6-data sync job.

Runs the SPoHF datalake sync: `yookr-data` into blue (the single automated
source for blue since `yookr-direct` was retired) and `letsgrow-data` into red.
Which endpoints run is `WP6_ENDPOINTS`; each goes to its own twin's database.

Usage:
    uv run python -m wp6_data
"""

import sys
from typing import Any

import structlog

from wp6_data.config import Settings
from wp6_data.shared.compat import run_async
from wp6_data.shared.observability import setup_observability
from wp6_data.sync import SyncOrchestrator

# `--spohf` used to select between two syncs. Only one remains, so it is a no-op
# kept so existing CronJob/Job specs keep working. Anything else is a mistake we
# would rather surface than silently reinterpret as "sync the datalake".
_ACCEPTED_ARGS = frozenset({"--spohf"})


def _run_spohf_sync(settings: Settings, logger: Any) -> int:
    """Run the SPoHF API → TimescaleDB sync."""
    logger.info(
        "spohf_sync_starting",
        api_base_url=settings.api_base_url,
        endpoints=settings.endpoint_list,
        sync_mode=settings.sync_mode,
    )
    orchestrator = SyncOrchestrator(settings)
    stats = run_async(orchestrator.run())

    if stats["errors"]:
        logger.warning("spohf_sync_completed_with_errors", errors=stats["errors"])
        return 1

    logger.info(
        "spohf_sync_success",
        total_records=stats["total_records"],
        duration_seconds=round(stats["duration_seconds"], 2),
    )
    return 0


def main() -> int:
    """Main entry point for sync job."""
    settings = Settings()
    setup_observability("wp6-sync", settings)
    logger = structlog.get_logger()

    unknown = set(sys.argv[1:]) - _ACCEPTED_ARGS
    if unknown:
        logger.error("sync_job_failed", reason=f"unrecognised arguments: {sorted(unknown)}")
        return 1

    logger.info(
        "sync_job_starting",
        tsdb_url=settings.tsdb_url.split("@")[-1],  # hide credentials
        sync_mode=settings.sync_mode,
    )

    if not settings.api_token:
        logger.error("spohf_sync_failed", reason="WP6_API_TOKEN is required for SPoHF sync")
        return 1

    try:
        return _run_spohf_sync(settings, logger)
    except Exception as e:
        logger.exception("sync_job_failed", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
