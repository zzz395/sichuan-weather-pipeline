"""Deterministic static figures for canonical weather forecast records.

Matplotlib is deliberately imported only inside :func:`render_figures`.  This
keeps package and CLI imports free from backend selection, font-cache access,
and other plotting-library initialization.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any


_SYNTHETIC_NOTICE = "Synthetic sample — demonstration only"
_FIGURE_SIZE = (12.0, 8.0)
_DPI = 150
_PANEL_COLUMNS = 2
_MISSING = object()


def render_figures(
    manifest: object,
    records: Iterable[object],
    figures_dir: str | Path,
) -> tuple[Path, Path]:
    """Render the two required PNG figures and return their paths.

    ``manifest`` and each item in ``records`` may be either mappings or
    attribute-based domain objects.  The expected values are the validated
    manifest fields and canonical record fields from the pipeline contract.
    Plotting is intentionally a terminal concern: Decimal values are converted
    to floats here only for Matplotlib coordinates.
    """

    # Backend selection must happen before pyplot is imported.  Keeping every
    # Matplotlib import in this call path preserves import-side-effect safety.
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    rendering_defaults = {
        key: value
        for key, value in matplotlib.rcParamsDefault.items()
        if key != "backend"
    }
    rendering_defaults.update(
        {
            "font.family": ["DejaVu Sans"],
            "font.sans-serif": ["DejaVu Sans"],
            "text.usetex": False,
        }
    )
    with matplotlib.rc_context(rc=rendering_defaults):
        return _render_figures(
            manifest,
            records,
            figures_dir,
            plt=plt,
            mdates=mdates,
            max_n_locator=MaxNLocator,
        )


def _render_figures(
    manifest: object,
    records: Iterable[object],
    figures_dir: str | Path,
    *,
    plt: Any,
    mdates: Any,
    max_n_locator: Any,
) -> tuple[Path, Path]:
    snapshots = _manifest_snapshots(manifest)
    locations = tuple(sorted(str(value) for value in _value(manifest, "locations")))
    if not snapshots:
        raise ValueError("manifest must contain at least one snapshot")
    if not locations:
        raise ValueError("manifest must contain at least one location")

    groups: dict[tuple[str, str], list[object]] = {
        (snapshot.snapshot_id, location): []
        for snapshot in snapshots
        for location in locations
    }
    for record in records:
        key = (str(_value(record, "snapshot_id")), str(_value(record, "location")))
        if key not in groups:
            raise ValueError(
                "canonical record references a snapshot/location outside the manifest: "
                f"{key!r}"
            )
        groups[key].append(record)

    for grouped_records in groups.values():
        grouped_records.sort(key=lambda item: _as_date(_value(item, "valid_date")))

    destination = Path(figures_dir)
    destination.mkdir(parents=True, exist_ok=True)
    temperature_path = destination / "temperature-ranges.png"
    weather_path = destination / "weather-frequency.png"

    panels = [
        (snapshot, location, groups[(snapshot.snapshot_id, location)])
        for snapshot in snapshots
        for location in locations
    ]

    temperature_figure = _new_figure(plt, len(panels))
    try:
        _render_temperature_figure(
            temperature_figure,
            panels,
            mdates=mdates,
        )
        temperature_figure.savefig(
            temperature_path,
            dpi=_DPI,
            format="png",
            facecolor="white",
            metadata={"Software": "sichuan-weather-pipeline"},
        )
    finally:
        plt.close(temperature_figure)

    weather_figure = _new_figure(plt, len(panels))
    try:
        _render_weather_figure(
            weather_figure,
            panels,
            max_n_locator=max_n_locator,
        )
        weather_figure.savefig(
            weather_path,
            dpi=_DPI,
            format="png",
            facecolor="white",
            metadata={"Software": "sichuan-weather-pipeline"},
        )
    finally:
        plt.close(weather_figure)

    return temperature_path, weather_path


class _SnapshotView:
    """Small internal view that avoids coupling plots to a manifest class."""

    __slots__ = ("snapshot_id", "snapshot_date", "valid_start", "valid_end")

    def __init__(
        self,
        snapshot_id: str,
        snapshot_date: date,
        valid_start: date,
        valid_end: date,
    ) -> None:
        self.snapshot_id = snapshot_id
        self.snapshot_date = snapshot_date
        self.valid_start = valid_start
        self.valid_end = valid_end


def _manifest_snapshots(manifest: object) -> tuple[_SnapshotView, ...]:
    raw_snapshots = _value(manifest, "snapshots")
    if isinstance(raw_snapshots, Mapping):
        items: Iterable[tuple[object | None, object]] = raw_snapshots.items()
    else:
        items = ((None, snapshot) for snapshot in raw_snapshots)

    snapshots: list[_SnapshotView] = []
    for mapping_key, snapshot in items:
        raw_id = _value(snapshot, "snapshot_id", default=mapping_key)
        if raw_id is None:
            raise ValueError("manifest snapshot is missing snapshot_id")
        snapshots.append(
            _SnapshotView(
                snapshot_id=str(raw_id),
                snapshot_date=_as_date(_value(snapshot, "snapshot_date")),
                valid_start=_as_date(_value(snapshot, "valid_start")),
                valid_end=_as_date(_value(snapshot, "valid_end")),
            )
        )

    return tuple(
        sorted(
            snapshots,
            key=lambda item: (item.snapshot_date, item.snapshot_id),
        )
    )


def _new_figure(plt: Any, panel_count: int) -> Any:
    rows = (panel_count + _PANEL_COLUMNS - 1) // _PANEL_COLUMNS
    figure, axes = plt.subplots(
        rows,
        _PANEL_COLUMNS,
        figsize=_FIGURE_SIZE,
        dpi=_DPI,
        squeeze=False,
    )
    flat_axes = list(axes.flat)
    for axis in flat_axes[panel_count:]:
        axis.set_visible(False)
    # Store the active axes in a private Python attribute rather than relying
    # on Matplotlib's axes list, which also includes hidden padding panels.
    figure._weather_pipeline_axes = flat_axes[:panel_count]
    return figure


def _render_temperature_figure(
    figure: Any,
    panels: list[tuple[_SnapshotView, str, list[object]]],
    *,
    mdates: Any,
) -> None:
    figure.suptitle(
        "Forecast temperature ranges",
        x=0.5,
        y=0.975,
        fontsize=16,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.935,
        _SYNTHETIC_NOTICE,
        ha="center",
        va="center",
        fontsize=12,
        fontweight="bold",
        color="#9B1C1C",
    )

    for axis, (snapshot, location, group) in zip(
        figure._weather_pipeline_axes,
        panels,
        strict=True,
    ):
        temperatures: list[tuple[date, Decimal, Decimal, Decimal]] = []
        for record in group:
            lower = _value(record, "temp_min_c")
            upper = _value(record, "temp_max_c")
            if lower is None or upper is None:
                continue
            midpoint = _value(record, "temp_midpoint_c", default=None)
            lower_decimal = _as_decimal(lower)
            upper_decimal = _as_decimal(upper)
            midpoint_decimal = (
                _as_decimal(midpoint)
                if midpoint is not None
                else (lower_decimal + upper_decimal) / Decimal(2)
            )
            temperatures.append(
                (
                    _as_date(_value(record, "valid_date")),
                    lower_decimal,
                    upper_decimal,
                    midpoint_decimal,
                )
            )

        axis.set_title(
            _panel_title(
                snapshot,
                location,
                f"n_temperature={len(temperatures)}",
            ),
            fontsize=10,
        )
        _configure_date_axis(axis, snapshot, group, mdates)
        axis.set_xlabel("Valid date")
        axis.set_ylabel("Forecast temperature (deg C)")
        axis.grid(axis="y", color="#D8DEE9", linewidth=0.7, alpha=0.8)

        if not temperatures:
            axis.text(
                0.5,
                0.5,
                "No temperature data",
                transform=axis.transAxes,
                ha="center",
                va="center",
                fontsize=12,
                fontweight="bold",
                color="#5B6573",
            )
            continue

        valid_dates = [item[0] for item in temperatures]
        lows = [float(item[1]) for item in temperatures]
        highs = [float(item[2]) for item in temperatures]
        midpoints = [float(item[3]) for item in temperatures]

        axis.vlines(
            valid_dates,
            lows,
            highs,
            color="#3572A5",
            linewidth=4,
            alpha=0.85,
            label="Forecast interval",
            zorder=2,
        )
        axis.scatter(
            valid_dates,
            lows,
            marker="_",
            s=150,
            linewidths=2,
            color="#173F5F",
            label="Lower / upper bound",
            zorder=3,
        )
        axis.scatter(
            valid_dates,
            highs,
            marker="_",
            s=150,
            linewidths=2,
            color="#173F5F",
            zorder=3,
        )
        axis.scatter(
            valid_dates,
            midpoints,
            marker="o",
            s=32,
            color="#D1495B",
            edgecolors="white",
            linewidths=0.6,
            label="Forecast midpoint",
            zorder=4,
        )
        axis.legend(loc="best", fontsize=7, frameon=False)

    figure.tight_layout(rect=(0.025, 0.025, 0.975, 0.90), h_pad=2.0, w_pad=1.5)


def _render_weather_figure(
    figure: Any,
    panels: list[tuple[_SnapshotView, str, list[object]]],
    *,
    max_n_locator: Any,
) -> None:
    figure.suptitle(
        "Forecast weather frequency",
        x=0.5,
        y=0.975,
        fontsize=16,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.935,
        _SYNTHETIC_NOTICE,
        ha="center",
        va="center",
        fontsize=12,
        fontweight="bold",
        color="#9B1C1C",
    )

    for axis, (snapshot, location, group) in zip(
        figure._weather_pipeline_axes,
        panels,
        strict=True,
    ):
        counts = Counter(
            str(weather)
            for record in group
            if (weather := _value(record, "weather")) is not None
        )
        n_weather = sum(counts.values())
        missing_weather = len(group) - n_weather
        axis.set_title(
            _panel_title(
                snapshot,
                location,
                f"n_weather={n_weather} | missing_weather={missing_weather}",
            ),
            fontsize=10,
        )
        axis.set_xlabel("Weather category")
        axis.set_ylabel("Record count")
        axis.yaxis.set_major_locator(max_n_locator(integer=True))
        axis.grid(axis="y", color="#D8DEE9", linewidth=0.7, alpha=0.8)
        axis.set_axisbelow(True)

        if not counts:
            axis.text(
                0.5,
                0.5,
                "No weather data",
                transform=axis.transAxes,
                ha="center",
                va="center",
                fontsize=12,
                fontweight="bold",
                color="#5B6573",
            )
            axis.set_xticks([])
            continue

        categories = sorted(counts)
        values = [counts[category] for category in categories]
        positions = list(range(len(categories)))
        bars = axis.bar(
            positions,
            values,
            width=0.68,
            color="#4C956C",
            edgecolor="#2C6E49",
            linewidth=0.8,
            zorder=2,
        )
        axis.set_xticks(positions, [_unicode_escape_label(value) for value in categories])
        axis.tick_params(axis="x", labelrotation=20)
        axis.set_ylim(0, max(values) + max(1.0, max(values) * 0.22))
        axis.bar_label(bars, labels=[str(value) for value in values], padding=3, fontsize=8)

    figure.tight_layout(rect=(0.025, 0.025, 0.975, 0.90), h_pad=2.0, w_pad=1.5)


def _configure_date_axis(
    axis: Any,
    snapshot: _SnapshotView,
    group: list[object],
    mdates: Any,
) -> None:
    window_start = datetime.combine(snapshot.valid_start, time.min)
    window_end = datetime.combine(snapshot.valid_end, time.min)
    left_limit = (
        window_start
        if snapshot.valid_start == date.min
        else window_start - timedelta(hours=12)
    )
    right_limit = (
        datetime.combine(date.max, time.max)
        if snapshot.valid_end == date.max
        else window_end + timedelta(hours=12)
    )
    axis.set_xlim(
        left_limit,
        right_limit,
    )
    candidate_dates = {
        snapshot.valid_start,
        snapshot.valid_end,
        *(_as_date(_value(record, "valid_date")) for record in group),
    }
    ticks = _limited_date_ticks(sorted(candidate_dates), limit=8)
    axis.set_xticks(ticks)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    axis.tick_params(axis="x", labelrotation=25)


def _limited_date_ticks(values: list[date], *, limit: int) -> list[date]:
    if len(values) <= limit:
        return values
    indexes = {
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [values[index] for index in sorted(indexes)]


def _panel_title(snapshot: _SnapshotView, location: str, metric: str) -> str:
    return (
        f"{_unicode_escape_label(snapshot.snapshot_id)} | "
        f"{_unicode_escape_label(location)}\n"
        f"Snapshot {snapshot.snapshot_date.isoformat()} | "
        f"Window {snapshot.valid_start.isoformat()} to "
        f"{snapshot.valid_end.isoformat()}\n"
        f"{metric}"
    )


def _unicode_escape_label(value: object) -> str:
    """Return an ASCII-only, reversible display form for arbitrary labels."""

    text = str(value)
    escaped: list[str] = []
    for character in text:
        code_point = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif 0x20 <= code_point <= 0x7E:
            escaped.append(character)
        elif code_point <= 0xFFFF:
            escaped.append(f"\\u{code_point:04x}")
        else:
            escaped.append(f"\\U{code_point:08x}")
    return "".join(escaped)


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"expected date or ISO date string, got {type(value).__name__}")


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _value(subject: object, name: str, *, default: object = _MISSING) -> Any:
    if isinstance(subject, Mapping):
        if name in subject:
            return subject[name]
    elif hasattr(subject, name):
        return getattr(subject, name)
    if default is not _MISSING:
        return default
    raise TypeError(f"{type(subject).__name__} does not provide required field {name!r}")


__all__ = ["render_figures"]
