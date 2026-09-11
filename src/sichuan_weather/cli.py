"""Command-line interface for the weather pipeline."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from .pipeline import (
    ArtifactDeliveryError,
    PathContractError,
    deliver_run,
    require_available_output,
    require_source_file,
)
from .serialization import quality_document, quality_json_text
from .validation import validate_dataset


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser without performing pipeline work."""
    parser = argparse.ArgumentParser(
        prog="weather-pipeline",
        description=(
            "Validate and process weather-forecast snapshot data in an "
            "offline-capable pipeline."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate input records and report data quality.",
    )
    validate_parser.add_argument(
        "--input",
        required=True,
        metavar="RECORDS_JSONL",
        help="Path to the JSONL forecast records.",
    )
    validate_parser.add_argument(
        "--manifest",
        required=True,
        metavar="MANIFEST_JSON",
        help="Path to the dataset manifest.",
    )
    validate_parser.set_defaults(handler=_handle_validate)

    run_parser = subparsers.add_parser(
        "run",
        help="Run the validated pipeline and write output artifacts.",
    )
    run_parser.add_argument(
        "--input",
        required=True,
        metavar="RECORDS_JSONL",
        help="Path to the JSONL forecast records.",
    )
    run_parser.add_argument(
        "--manifest",
        required=True,
        metavar="MANIFEST_JSON",
        help="Path to the dataset manifest.",
    )
    run_parser.add_argument(
        "--output",
        required=True,
        metavar="DIRECTORY",
        help="Path to a new or empty output directory.",
    )
    run_parser.set_defaults(handler=_handle_run)

    return parser


def _validated_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    input_path = Path(args.input)
    manifest_path = Path(args.manifest)
    require_source_file(input_path, "input")
    require_source_file(manifest_path, "manifest")
    return input_path, manifest_path


def _handle_validate(args: argparse.Namespace) -> int:
    input_path, manifest_path = _validated_paths(args)
    result = validate_dataset(input_path, manifest_path)
    document = quality_document(
        result,
        analysis_generated=False,
        output_records=0,
    )
    sys.stdout.write(quality_json_text(document))
    return 0 if result.is_valid else 1


def _handle_run(args: argparse.Namespace) -> int:
    input_path, manifest_path = _validated_paths(args)
    output_path = Path(args.output)
    require_available_output(output_path)
    result = validate_dataset(input_path, manifest_path)
    deliver_run(result, output_path)
    return 0 if result.is_valid else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, dispatch a command, and preserve exit-code classes."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.handler(args)
    except PathContractError as error:
        print(f"weather-pipeline: path error: {error}", file=sys.stderr)
        return 2
    except ArtifactDeliveryError as error:
        print(f"weather-pipeline: runtime error: {error}", file=sys.stderr)
        if error.temporary_path is not None:
            print(
                "weather-pipeline: temporary artifacts could not be removed: "
                f"{error.temporary_path}",
                file=sys.stderr,
            )
        return 3
    except OSError as error:
        print(
            "weather-pipeline: runtime I/O error: "
            f"{type(error).__name__}",
            file=sys.stderr,
        )
        return 3
    except Exception as error:
        print(
            "weather-pipeline: unexpected runtime error: "
            f"{type(error).__name__}",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
