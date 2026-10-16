"""Reusable HTML components: cards, tables, tabs, date filters."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wp6_data.shared.metadata import MetadataRegistry


def utc_day_bounds(target_date: date) -> tuple[datetime, datetime]:
    """Return inclusive UTC start/end datetimes for a calendar date."""
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=UTC)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end


def default_date_range() -> tuple[date, date]:
    """Return default date range: last 7 days."""
    end = date.today()
    start = end - timedelta(days=7)
    return start, end


def resolve_date_range(
    start: date | None = None,
    end: date | None = None,
) -> tuple[date, date, datetime, datetime]:
    """Apply defaults and convert date params to a (date, date, datetime, datetime) tuple."""
    default_start, default_end = default_date_range()
    start = start or default_start
    end = end or default_end
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)
    return start, end, start_dt, end_dt


# Where the "All" preset reaches back to when a page does not say. A page that
# knows when its own data begins should pass ``all_start`` instead — "All"
# landing on a date with no readings behind it is a range the page cannot fill.
DEFAULT_ALL_START = date(2024, 1, 1)


def render_date_filter(
    start: date,
    end: date,
    extra_params: dict[str, str] | None = None,
    *,
    all_start: date | None = None,
) -> str:
    """Render an HTML date-range filter with quick-select presets and custom inputs.

    ``all_start`` is where the "All" preset begins; pass the first day the page
    actually has data for, so "All" means all of *this page's* data rather than
    an arbitrary epoch.
    """
    today = date.today()
    all_start = all_start or DEFAULT_ALL_START
    presets = [
        ("1d", 1),
        ("7d", 7),
        ("30d", 30),
        ("90d", 90),
        ("1y", 365),
        ("All", None),
    ]
    # Determine which preset is active
    active = None
    for label, days in presets:
        if days is None:
            if start == all_start and end == today:
                active = label
        elif start == today - timedelta(days=days) and end == today:
            active = label

    buttons = []
    for label, days in presets:
        cls = "contrast" if label == active else "outline"
        js_days = "null" if days is None else str(days)
        buttons.append(
            f'<button type="button" class="{cls}" onclick="setRange({js_days})">'
            f"{label}</button>"
        )

    hidden_fields = ""
    if extra_params:
        hidden_fields = "".join(
            f'<input type="hidden" name="{k}" value="{v}">' for k, v in extra_params.items()
        )

    return f"""
    <article class="date-filter">
    <form id="dateFilter" method="get">
        {hidden_fields}
        <div class="date-presets">
            {''.join(buttons)}
        </div>
        <div class="date-inputs">
            <input type="date" id="df-start" name="start"
                   value="{start.isoformat()}" title="From"
                   onchange="document.getElementById('dateFilter').submit()">
            <span class="date-sep">&ndash;</span>
            <input type="date" id="df-end" name="end"
                   value="{end.isoformat()}" title="To"
                   onchange="document.getElementById('dateFilter').submit()">
        </div>
    </form>
    </article>
    <script>
    function setRange(days) {{
        var end = new Date();
        var start = days === null
            ? new Date('{all_start.isoformat()}')
            : new Date(end.getTime() - days * 86400000);
        document.getElementById('df-start').value = start.toISOString().slice(0, 10);
        document.getElementById('df-end').value = end.toISOString().slice(0, 10);
        document.getElementById('dateFilter').submit();
    }}
    </script>
    """


def render_card(
    title: str, body: str, *, description: str = "", card_class: str = "",
) -> str:
    """Render content wrapped in a styled article card."""
    desc = f"<p>{description}</p>" if description else ""
    cls = f' class="{card_class}"' if card_class else ""
    return f"<article{cls}><h2>{title}</h2>{desc}{body}</article>"


def render_hub_card(
    title: str,
    description: str = "",
    *,
    href: str = "",
    label: str = "Open",
    body: str = "",
    card_class: str = "",
    disabled: bool = False,
) -> str:
    """Render a hub navigation card: heading, description, optional body, action button.

    Used by overview/hub pages that link out to detailed views (DLI dashboard,
    Plant Monitor, Multi Height). Pass ``disabled=True`` for a non-clickable button
    (e.g. an admin-gated action); ``body`` injects extra HTML before the button.
    """
    desc = f"<p>{description}</p>" if description else ""
    if disabled:
        action = f'<span class="btn-disabled">{label}</span>'
    else:
        action = f'<a href="{href}" class="btn">{label}</a>'
    cls = f' class="{card_class}"' if card_class else ""
    return f"<article{cls}><h3>{title}</h3>{desc}{body}{action}</article>"


def pill_row(base_path, param, choices, active, preserve, label):
    """A labelled segmented toggle that swaps ``param`` (preserving other params).

    Reuses the shared ``.group-toggle`` / ``.group-btn`` styling from the chart
    page; each segment is a navigation link. ``choices`` is a list of
    ``(value, text)``; ``preserve`` a dict of other query params (falsy dropped);
    ``label`` is the row caption (e.g. "Device").
    """
    qs = "".join(f"&amp;{key}={val}" for key, val in preserve.items() if val)
    segments = []
    for value, text in choices:
        cls = "group-btn active" if value == active else "group-btn"
        segments.append(
            f'<a class="{cls}" style="text-decoration:none;" '
            f'href="{base_path}?{param}={value}{qs}">{text}</a>'
        )
    return (
        '<div style="display:flex;align-items:center;gap:0.75rem;'
        'margin-bottom:0.5rem;flex-wrap:wrap;">'
        f'<span style="font-weight:600;min-width:6rem;">{label}:</span>'
        '<div class="group-toggle" style="width:fit-content;">'
        + "".join(segments)
        + "</div></div>"
    )


def render_hub_grid(cards: list[str]) -> str:
    """Wrap hub navigation cards in a responsive auto-fit grid."""
    return f'<div class="hub-grid">{"".join(cards)}</div>'


def render_stat_tile(
    value: str, label: str, sublabel: str = "", *, value_class: str = "",
) -> str:
    """Render one stat tile: a big value with a caption (and optional sub-caption).

    ``value_class`` adds a modifier class on the value (e.g. ``"warning"``).
    """
    cls = f' class="stat-value {value_class}"' if value_class else ' class="stat-value"'
    sub = f"<br><small>{sublabel}</small>" if sublabel else ""
    return f"<article><div{cls}>{value}</div><small>{label}</small>{sub}</article>"


def render_stat_grid(
    tiles: list[str | tuple[str, ...]], *, cols: int | str | None = None,
) -> str:
    """Render a grid of stat tiles (the ``.stats-grid`` pattern).

    Each tile is either a ``(value, label)`` / ``(value, label, sublabel)``
    tuple, or an already-rendered tile string (see :func:`render_stat_tile`)
    for tiles needing e.g. a value class. ``cols`` selects a ``cols-N`` /
    ``cols-auto`` layout modifier.
    """
    rendered = "".join(
        tile if isinstance(tile, str) else render_stat_tile(*tile) for tile in tiles
    )
    cls = f"stats-grid cols-{cols}" if cols is not None else "stats-grid"
    return f'<div class="{cls}">{rendered}</div>'


def render_table(
    headers: list[str], rows: list[list[str]], *,
    sortable: bool = True, group_col: int | None = None,
) -> str:
    """Render an HTML table from headers and row data (cells may contain HTML).

    When *group_col* is set, the table is marked with ``data-group-col`` so that
    the front-end can visually merge consecutive identical values in that column
    while it is the active sort.
    """
    thead = "<tr>" + "".join(
        f'<th style="cursor:pointer" onclick="sortTable(this)">{h}</th>'
        if sortable else f"<th>{h}</th>"
        for h in headers
    ) + "</tr>"
    tbody = "".join(
        "<tr>" + "".join(
            f'<td data-sort="{cell[1]}">{cell[0]}</td>'
            if isinstance(cell, tuple)
            else f"<td>{cell}</td>"
            for cell in row
        ) + "</tr>"
        for row in rows
    )
    table_attrs = (
        f' data-group-col="{group_col}"' if group_col is not None else ""
    )
    return (
        f"<table{table_attrs}><thead>{thead}</thead>"
        f"<tbody>{tbody}</tbody></table>"
    )


EXPLORE_TAB_IDS = ("devices", "sensors", "manual")
EXPLORE_TAB_LABELS = {
    "devices": "Devices",
    "sensors": "Sensors",
    "manual": "Manual measurements",
}


def _format_timestamp_cell(dt: Any) -> tuple[str, str]:
    """Render a datetime as an HTML cell tuple (display, sort_key).

    Display is a relative phrase ("3 hours ago"); sort_key is the ISO string
    so client-side sort orders by absolute time. ``None`` renders as a dash.
    """
    if dt is None:
        return ("—", "")

    from datetime import datetime

    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = now - dt
        seconds = delta.total_seconds()
        if seconds < 60:
            phrase = "just now"
        elif seconds < 3600:
            phrase = f"{int(seconds // 60)} min ago"
        elif seconds < 86400:
            phrase = f"{int(seconds // 3600)} h ago"
        elif seconds < 7 * 86400:
            phrase = f"{int(seconds // 86400)} d ago"
        else:
            phrase = dt.strftime("%Y-%m-%d")
        # Tooltip carries the absolute UTC timestamp for precision.
        title = dt.strftime("%Y-%m-%d %H:%M UTC")
        html = f'<span title="{title}">{phrase}</span>'
        return (html, dt.isoformat())
    return (str(dt), str(dt))


def build_explore_tabs(
    metadata: MetadataRegistry,
    devices: dict[str, dict],
    available_exports: dict[str, str],
    manual_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the three explore tables (devices, sensors, manual measurements).

    Sensors are split by ``sensor_default(key).source``: empty source = auto-
    logged, non-empty = manual upload unless the twin declares that source
    automated (``MetadataRegistry.is_manual_source``). Devices are similarly
    filtered by ``device(id).source`` so that manual-measurement plants do not
    appear in the Devices tab.

    Args:
        metadata: The twin's metadata registry.
        devices: ``{device_id: {"sensors": [str], "readings": int,
                              "last_seen": datetime | None}}``.
        available_exports: Export availability dict for download links.
        manual_metadata: ``{"uploads": {source: datetime},
                           "measurements": {sensor_key: datetime}}``
            from the provider's ``fetch_manual_metadata``.

    Returns:
        ``{"devices": html, "sensors": html, "manual": html}``
    """
    manual_metadata = manual_metadata or {"uploads": {}, "measurements": {}}
    manual_uploads = manual_metadata.get("uploads", {})
    manual_measurements = manual_metadata.get("measurements", {})
    from wp6_data.shared.export import render_download_link

    # --- Aggregate per-sensor: total readings + set of devices ---
    sensor_readings: dict[str, int] = {}
    sensor_devices: dict[str, set[str]] = {}
    for device_id, info in devices.items():
        for s in info["sensors"]:
            sensor_readings[s] = sensor_readings.get(s, 0) + info["readings"]
            sensor_devices.setdefault(s, set()).add(device_id)

    auto_entries: list[dict[str, str]] = []
    manual_entries: list[dict[str, str]] = []
    for key in sorted(sensor_readings, key=lambda k: -sensor_readings[k]):
        sm = metadata.sensor_default(key)
        entry = {
            "key": key,
            "url": chart_url([
                f"{d}:{key}" for d in sorted(sensor_devices.get(key, set()))
            ]),
            "type": sm.type,
            "alias": sm.alias,
            "unit": sm.unit,
            "Devices": ", ".join(sorted(sensor_devices.get(key, set()))),
            "Readings": f"{sensor_readings[key]:,}",
            "Source": sm.source,
        }
        if metadata.is_manual_source(sm.source):
            entry["Last upload"] = _format_timestamp_cell(
                manual_uploads.get(sm.source),
            )
            entry["Last measure"] = _format_timestamp_cell(
                manual_measurements.get(key),
            )
            manual_entries.append(entry)
        else:
            auto_entries.append(entry)

    sensors_tab = render_sensor_type_table(
        auto_entries, extra_headers=["Devices", "Readings"],
    )
    if manual_entries:
        manual_tab = render_manual_measurement_table(
            manual_entries,
            extra_headers=["Readings", "Last upload", "Last measure"],
        )
    else:
        manual_tab = (
            "<p>No manual measurements have been uploaded yet.</p>"
        )

    # --- Devices tab: only auto-logged hardware ---
    auto_devices = {
        device_id: info
        for device_id, info in devices.items()
        if not metadata.is_manual_source(metadata.device(device_id).source)
    }

    position_series: dict[str, list[str]] = {}
    dtype_series: dict[str, list[str]] = {}
    for device_id, info in auto_devices.items():
        dm = metadata.device(device_id)
        series = [f"{device_id}:{s}" for s in sorted(info["sensors"])]
        if dm.position:
            position_series.setdefault(dm.position, []).extend(series)
        if dm.type:
            dtype_series.setdefault(dm.type, []).extend(series)

    device_entries = []
    for device_id, info in sorted(auto_devices.items()):
        dm = metadata.device(device_id)
        dev_series = [f"{device_id}:{s}" for s in sorted(info["sensors"])]
        sensor_links = ", ".join(
            f'<a href="{chart_url([f"{device_id}:{s}"])}">'
            f"{metadata.sensor_default(s).alias or s}</a>"
            for s in sorted(info["sensors"])
        )
        pos_html = (
            f'<a href="{chart_url(position_series[dm.position])}">'
            f"{dm.position}</a>"
            if dm.position
            else ""
        )
        type_html = (
            f'<strong><a href="{chart_url(dtype_series[dm.type])}">'
            f"{dm.type}</a></strong>"
            if dm.type
            else ""
        )
        # Manual sources (devices whose readings come from a manually-uploaded
        # file rather than an automated sensor) get a small badge so users can
        # tell measurement plants apart from sensor stations at a glance.
        source_badge = (
            f' <small><mark>manual: {dm.source}</mark></small>'
            if metadata.is_manual_source(dm.source) else ""
        )
        device_entries.append({
            "name": (
                f'<strong><a href="{chart_url(dev_series)}">{device_id}</a>'
                f"</strong>{source_badge}"
            ),
            "position": pos_html,
            "type": type_html,
            "type_sort": dm.type,
            "sensors": sensor_links,
            "readings": f'{info["readings"]:,}' if info["readings"] else "",
            "Last seen": _format_timestamp_cell(info.get("last_seen")),
            "Download": render_download_link(device_id, available_exports),
        })
    devices_tab = render_device_table(
        device_entries, extra_columns=["Last seen", "Download"],
    )

    return {
        "devices": devices_tab,
        "sensors": sensors_tab,
        "manual": manual_tab,
    }


def render_explore_tabs(
    tabs: dict[str, str],
    active: str = "devices",
    extra_tabs: dict[str, tuple[str, str]] | None = None,
) -> str:
    """Wrap the explore tables in a tab switcher.

    Args:
        tabs: ``{tab_id: html}`` content for each tab.
        active: Initially-selected tab id; falls back to "devices" if invalid.
        extra_tabs: ``{tab_id: (label, html)}`` for twin-specific extra tabs
            rendered after the built-in three.
    """
    import re

    extra_tabs = extra_tabs or {}
    extra_tabs = {
        tid: tab
        for tid, tab in extra_tabs.items()
        if re.fullmatch(r"[a-z0-9_-]+", tid)
    }
    all_ids = list(EXPLORE_TAB_IDS) + [tid for tid in extra_tabs if tid not in EXPLORE_TAB_IDS]
    all_labels = {**EXPLORE_TAB_LABELS, **{tid: lbl for tid, (lbl, _) in extra_tabs.items()}}
    all_content = {**{tid: tabs.get(tid, "") for tid in EXPLORE_TAB_IDS},
                   **{tid: html for tid, (_, html) in extra_tabs.items()}}

    if active not in all_ids:
        active = "devices"

    buttons = "".join(
        f'<button type="button" role="tab" data-tab="{tid}"'
        f' aria-selected="{"true" if tid == active else "false"}">'
        f"{all_labels[tid]}</button>"
        for tid in all_ids
    )
    panels = "".join(
        f'<div role="tabpanel" data-tab-panel="{tid}"'
        f'{"" if tid == active else " hidden"}>'
        f'{all_content.get(tid, "")}</div>'
        for tid in all_ids
    )

    return (
        '<div class="explore-tabs">'
        f'<div class="tab-buttons" role="tablist">{buttons}</div>'
        f"{panels}"
        "</div>"
    )


def chart_url(series: list[str]) -> str:
    """Build a /chart?s=... URL from a list of device:sensor keys."""
    from urllib.parse import urlencode

    if not series:
        return "/chart"
    return f"/chart?{urlencode({'s': ','.join(series)})}"


def render_sensor_type_table(
    sensors: list[dict[str, str]],
    *,
    extra_headers: list[str] | None = None,
) -> str:
    """Render a sensor table grouped by sensor type.

    Each dict in *sensors* should have keys:
        - ``key``: sensor/measurement key (used as fallback label)
        - ``url``: link target for the sensor name
        - ``type``: sensor type for grouping (rows without type are skipped)
        - ``alias``: display name (falls back to key)
        - ``unit``: unit of measurement
    Plus any extra keys matching *extra_headers*.
    """
    from collections import defaultdict as _defaultdict

    type_groups: dict[str, list[dict[str, str]]] = _defaultdict(list)
    for s in sensors:
        if s.get("type"):
            type_groups[s["type"]].append(s)

    headers = ["Type", "Sensor", "Unit"]
    if extra_headers:
        headers.extend(extra_headers)

    rows: list[list] = []
    for sensor_type in sorted(type_groups):
        for s in type_groups[sensor_type]:
            type_html = (
                f'<strong><a href="/type/{sensor_type}">'
                f"{sensor_type}</a></strong>"
            )
            type_cell = (type_html, sensor_type)
            row: list = [
                type_cell,
                f'<a href="{s["url"]}">{s.get("alias") or s["key"]}</a>',
                s.get("unit", ""),
            ]
            if extra_headers:
                for h in extra_headers:
                    row.append(s.get(h, ""))
            rows.append(row)

    return render_table(headers, rows, group_col=0)


def render_manual_measurement_table(
    measurements: list[dict[str, Any]],
    *,
    extra_headers: list[str] | None = None,
) -> str:
    """Render a manual-measurement table grouped by source (column 0).

    Each dict in *measurements* should have keys:
        - ``key``: measurement key
        - ``url``: link for the measurement name
        - ``type``: sensor type label (e.g. "fruit chemistry")
        - ``alias``: display name for the measurement
        - ``unit``: unit of measurement
        - ``Source``: source string (e.g. "sijia")
    Plus any extra keys matching *extra_headers*.
    """
    from collections import defaultdict as _defaultdict

    source_groups: dict[str, list[dict[str, Any]]] = _defaultdict(list)
    for m in measurements:
        source_groups[m.get("Source", "")].append(m)

    headers = ["Source", "Type", "Measurement", "Unit"]
    if extra_headers:
        headers.extend(extra_headers)

    rows: list[list] = []
    for source in sorted(source_groups):
        for m in source_groups[source]:
            source_cell = (f"<strong>{source}</strong>", source)
            sensor_type = m.get("type", "")
            type_cell = (
                (
                    f'<a href="/type/{sensor_type}">{sensor_type}</a>',
                    sensor_type,
                )
                if sensor_type
                else ("", "")
            )
            row: list = [
                source_cell,
                type_cell,
                f'<a href="{m["url"]}">{m.get("alias") or m["key"]}</a>',
                m.get("unit", ""),
            ]
            if extra_headers:
                for h in extra_headers:
                    row.append(m.get(h, ""))
            rows.append(row)

    return render_table(headers, rows, group_col=0)


def render_device_table(
    devices: list[dict[str, str]],
    *,
    extra_columns: list[str] | None = None,
) -> str:
    """Render a device table grouped by device type (column 0).

    Devices without a type land in a single un-typed bucket sorted last.

    Each dict in *devices* should have keys:
        - ``name``: device name/id (displayed as-is, may contain HTML links)
        - ``position``: from device metadata
        - ``type``: device type label
        - ``type_sort``: raw type string for sort/grouping (defaults to ``type``)
        - ``sensors``: comma-separated sensor aliases
        - ``readings``: formatted reading count
    Plus any extra keys matching *extra_columns* headers.
    """
    headers = ["Type", "Device", "Position", "Sensors", "Readings"]
    if extra_columns:
        headers.extend(extra_columns)

    # Group by type_sort (raw type) so the merge-cell JS can identify groups.
    grouped: dict[str, list[dict[str, str]]] = {}
    for d in devices:
        grouped.setdefault(d.get("type_sort", ""), []).append(d)

    # Empty-type bucket sorts last by using a high-codepoint sentinel.
    def _sort_key(t: str) -> tuple[int, str]:
        return (1, "") if t == "" else (0, t)

    rows: list[list] = []
    for type_key in sorted(grouped, key=_sort_key):
        for d in grouped[type_key]:
            row: list = [
                (d.get("type", ""), type_key),
                d.get("name", ""),
                d.get("position", ""),
                d.get("sensors", ""),
                d.get("readings", ""),
            ]
            if extra_columns:
                for col in extra_columns:
                    row.append(d.get(col, ""))
            rows.append(row)

    return render_table(headers, rows, group_col=0)

