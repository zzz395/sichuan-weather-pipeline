"""PNG integrity and Matplotlib figure-semantics regressions."""

from pathlib import Path

import pytest

from sichuan_weather import cli
from sichuan_weather.validation import validate_dataset

from tests.helpers import csv_rows, make_manifest, make_record, write_dataset
from tests.oracle import SAMPLE_MANIFEST, SAMPLE_RECORDS


def _sample_panels(result: object) -> list[tuple[object, str, list[object]]]:
    from sichuan_weather.visualization import _manifest_snapshots

    snapshots = _manifest_snapshots(result.manifest)
    locations = tuple(sorted(result.manifest.locations))
    groups = {
        (snapshot.snapshot_id, location): []
        for snapshot in snapshots
        for location in locations
    }
    for record in result.canonical_records:
        groups[(record.snapshot_id, record.location)].append(record)
    for records in groups.values():
        records.sort(key=lambda record: record.valid_date)
    return [
        (snapshot, location, groups[(snapshot.snapshot_id, location)])
        for snapshot in snapshots
        for location in locations
    ]


def test_sample_pngs_fully_decode_at_frozen_dimensions_and_dpi(tmp_path: Path) -> None:
    from PIL import Image

    from sichuan_weather.visualization import render_figures

    result = validate_dataset(SAMPLE_RECORDS, SAMPLE_MANIFEST)
    assert result.is_valid and result.manifest is not None
    paths = render_figures(result.manifest, result.canonical_records, tmp_path)

    assert [path.name for path in paths] == [
        "temperature-ranges.png",
        "weather-frequency.png",
    ]
    for path in paths:
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.size == (1800, 1200)
            assert image.info["Software"] == "sichuan-weather-pipeline"
            assert image.info["dpi"][0] == pytest.approx(150, abs=0.05)
            assert image.info["dpi"][1] == pytest.approx(150, abs=0.05)
            image.verify()
        with Image.open(path) as image:
            image.load()
            assert image.getbbox() is not None


def test_sample_temperature_panels_match_canonical_intervals_and_midpoints() -> None:
    import matplotlib.dates as mdates
    from matplotlib.collections import LineCollection, PathCollection
    import matplotlib.pyplot as plt

    from sichuan_weather.visualization import _new_figure, _render_temperature_figure

    result = validate_dataset(SAMPLE_RECORDS, SAMPLE_MANIFEST)
    assert result.is_valid and result.manifest is not None
    panels = _sample_panels(result)
    figure = _new_figure(plt, len(panels))
    try:
        _render_temperature_figure(figure, panels, mdates=mdates)

        assert [axis.get_title().splitlines()[0] for axis in figure._weather_pipeline_axes] == [
            "sample-a | Chengdu",
            "sample-a | Mianyang",
            "sample-b | Chengdu",
            "sample-b | Mianyang",
        ]
        assert [axis.get_title().splitlines()[2] for axis in figure._weather_pipeline_axes] == [
            "n_temperature=3",
            "n_temperature=3",
            "n_temperature=2",
            "n_temperature=2",
        ]
        assert [text.get_text() for text in figure.texts] == [
            "Forecast temperature ranges",
            "Synthetic sample — demonstration only",
        ]

        expected_intervals = [
            [(-2.0, 4.0), (0.0, 6.0), (-4.0, -4.0)],
            [(-1.0, 5.0), (2.0, 8.0), (1.5, 6.5)],
            [(1.0, 7.0), (-3.0, 1.0)],
            [(2.0, 8.0), (3.0, 9.0)],
        ]
        expected_midpoints = [
            [1.0, 3.0, -4.0],
            [2.0, 5.0, 4.0],
            [4.0, -1.0],
            [5.0, 6.0],
        ]
        for axis, intervals, midpoints in zip(
            figure._weather_pipeline_axes,
            expected_intervals,
            expected_midpoints,
            strict=True,
        ):
            interval_collection = next(
                collection
                for collection in axis.collections
                if isinstance(collection, LineCollection)
            )
            assert [
                (float(segment[0][1]), float(segment[1][1]))
                for segment in interval_collection.get_segments()
            ] == intervals
            point_collections = [
                collection
                for collection in axis.collections
                if isinstance(collection, PathCollection)
            ]
            assert len(point_collections) == 3
            assert [
                float(offset[1]) for offset in point_collections[-1].get_offsets()
            ] == midpoints
    finally:
        plt.close(figure)


def test_sample_weather_panels_match_categories_counts_and_missingness() -> None:
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    from sichuan_weather.visualization import _new_figure, _render_weather_figure

    result = validate_dataset(SAMPLE_RECORDS, SAMPLE_MANIFEST)
    assert result.is_valid and result.manifest is not None
    panels = _sample_panels(result)
    figure = _new_figure(plt, len(panels))
    try:
        _render_weather_figure(figure, panels, max_n_locator=MaxNLocator)

        assert [axis.get_title().splitlines()[2] for axis in figure._weather_pipeline_axes] == [
            "n_weather=4 | missing_weather=0",
            "n_weather=3 | missing_weather=1",
            "n_weather=2 | missing_weather=0",
            "n_weather=2 | missing_weather=0",
        ]
        assert [text.get_text() for text in figure.texts] == [
            "Forecast weather frequency",
            "Synthetic sample — demonstration only",
        ]
        expected = [
            (["Cloudy", "Snow", "Sunny"], [2.0, 1.0, 1.0]),
            (["Cloudy", "Rain", "Sunny"], [1.0, 1.0, 1.0]),
            (["Cloudy", "Sunny"], [1.0, 1.0]),
            (["Rain"], [2.0]),
        ]
        for axis, (categories, heights) in zip(
            figure._weather_pipeline_axes,
            expected,
            strict=True,
        ):
            assert [label.get_text() for label in axis.get_xticklabels()] == categories
            assert [float(bar.get_height()) for bar in axis.patches] == heights
    finally:
        plt.close(figure)


def test_equal_snapshot_dates_remain_distinct_in_rendered_panels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import matplotlib.dates as mdates
    from matplotlib.collections import LineCollection, PathCollection
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    from sichuan_weather.visualization import _render_figures

    manifest = make_manifest()
    manifest["snapshots"] = [
        {
            "snapshot_id": "snapshot-b",
            "snapshot_date": "2026-12-30",
            "valid_start": "2026-12-30",
            "valid_end": "2026-12-30",
        },
        {
            "snapshot_id": "snapshot-a",
            "snapshot_date": "2026-12-30",
            "valid_start": "2026-12-30",
            "valid_end": "2026-12-30",
        },
    ]
    snapshot_b = make_record("2026-12-30", "0~2℃", "Rain")
    snapshot_b["snapshot_id"] = "snapshot-b"
    snapshot_a = make_record("2026-12-30", "10~14℃", "Sunny")
    snapshot_a["snapshot_id"] = "snapshot-a"
    input_path, manifest_path = write_dataset(
        tmp_path / "input",
        [snapshot_b, snapshot_a],
        manifest=manifest,
    )
    result = validate_dataset(input_path, manifest_path)
    assert result.is_valid and result.manifest is not None

    retained_figures: list[object] = []
    original_close = plt.close
    monkeypatch.setattr(plt, "close", retained_figures.append)
    try:
        _render_figures(
            result.manifest,
            result.canonical_records,
            tmp_path / "figures",
            plt=plt,
            mdates=mdates,
            max_n_locator=MaxNLocator,
        )

        assert len(retained_figures) == 2
        temperature_figure, weather_figure = retained_figures
        temperature_axes = temperature_figure._weather_pipeline_axes
        weather_axes = weather_figure._weather_pipeline_axes
        expected_context = [
            [
                "snapshot-a | Chengdu",
                "Snapshot 2026-12-30 | Window 2026-12-30 to 2026-12-30",
            ],
            [
                "snapshot-b | Chengdu",
                "Snapshot 2026-12-30 | Window 2026-12-30 to 2026-12-30",
            ],
        ]
        assert [axis.get_title().splitlines()[:2] for axis in temperature_axes] == (
            expected_context
        )
        assert [axis.get_title().splitlines()[:2] for axis in weather_axes] == (
            expected_context
        )
        assert [axis.get_title().splitlines()[2] for axis in temperature_axes] == [
            "n_temperature=1",
            "n_temperature=1",
        ]
        assert [axis.get_title().splitlines()[2] for axis in weather_axes] == [
            "n_weather=1 | missing_weather=0",
            "n_weather=1 | missing_weather=0",
        ]

        assert [
            [
                float(offset[1])
                for offset in [
                    collection
                    for collection in axis.collections
                    if isinstance(collection, PathCollection)
                ][-1].get_offsets()
            ]
            for axis in temperature_axes
        ] == [[12.0], [1.0]]
        assert [
            [
                (float(segment[0][1]), float(segment[1][1]))
                for segment in next(
                    collection
                    for collection in axis.collections
                    if isinstance(collection, LineCollection)
                ).get_segments()
            ]
            for axis in temperature_axes
        ] == [[(10.0, 14.0)], [(0.0, 2.0)]]
        assert [
            [label.get_text() for label in axis.get_xticklabels()]
            for axis in weather_axes
        ] == [["Sunny"], ["Rain"]]
        assert [
            [float(bar.get_height()) for bar in axis.patches]
            for axis in weather_axes
        ] == [[1.0], [1.0]]
    finally:
        for figure in retained_figures:
            original_close(figure)


def test_all_missing_panels_have_explicit_no_data_semantics(tmp_path: Path) -> None:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    from sichuan_weather.visualization import (
        _new_figure,
        _render_temperature_figure,
        _render_weather_figure,
    )

    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", None, None)],
        manifest=make_manifest(),
    )
    result = validate_dataset(input_path, manifest_path)
    assert result.is_valid and result.manifest is not None
    panels = _sample_panels(result)

    temperature_figure = _new_figure(plt, 1)
    weather_figure = _new_figure(plt, 1)
    try:
        _render_temperature_figure(temperature_figure, panels, mdates=mdates)
        _render_weather_figure(
            weather_figure,
            panels,
            max_n_locator=MaxNLocator,
        )
        temperature_axis = temperature_figure._weather_pipeline_axes[0]
        weather_axis = weather_figure._weather_pipeline_axes[0]
        assert temperature_axis.get_title().splitlines()[2] == "n_temperature=0"
        assert weather_axis.get_title().splitlines()[2] == (
            "n_weather=0 | missing_weather=1"
        )
        assert "No temperature data" in {
            text.get_text() for text in temperature_axis.texts
        }
        assert "No weather data" in {text.get_text() for text in weather_axis.texts}
        assert len(temperature_axis.collections) == 0
        assert len(weather_axis.patches) == 0
        assert weather_axis.get_xticks().tolist() == []
    finally:
        plt.close(temperature_figure)
        plt.close(weather_figure)


def test_minimum_and_maximum_iso_dates_render(tmp_path: Path) -> None:
    manifest = make_manifest()
    manifest["snapshots"] = [
        {
            "snapshot_id": "first-day",
            "snapshot_date": "0001-01-01",
            "valid_start": "0001-01-01",
            "valid_end": "0001-01-01",
        },
        {
            "snapshot_id": "last-day",
            "snapshot_date": "9999-12-31",
            "valid_start": "9999-12-31",
            "valid_end": "9999-12-31",
        },
    ]
    first = make_record("0001-01-01", "0~1℃")
    first["snapshot_id"] = "first-day"
    last = make_record("9999-12-31", "1~2℃")
    last["snapshot_id"] = "last-day"
    input_path, manifest_path = write_dataset(
        tmp_path / "input",
        [first, last],
        manifest=manifest,
    )

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(tmp_path / "output"),
        ]
    )

    assert exit_code == 0
    monthly_rows = csv_rows(tmp_path / "output" / "monthly_summary.csv")
    assert [row["year_month"] for row in monthly_rows] == ["0001-01", "9999-12"]


def test_figure_panel_titles_separate_context_and_metrics() -> None:
    from sichuan_weather.visualization import _manifest_snapshots, _panel_title

    result = validate_dataset(SAMPLE_RECORDS, SAMPLE_MANIFEST)
    snapshot = _manifest_snapshots(result.manifest)[0]
    lines = _panel_title(
        snapshot,
        "Mianyang",
        "n_weather=3 | missing_weather=1",
    ).splitlines()

    assert len(lines) == 3
    assert lines[2] == "n_weather=3 | missing_weather=1"
