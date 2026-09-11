"""Strict manifest and JSONL validation for forecast snapshot data."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any

from .models import (
    CanonicalRecord,
    Issue,
    Manifest,
    MissingMonthGroup,
    SnapshotDefinition,
    TemperatureRange,
    ValidationCounts,
    ValidationResult,
)


_UTF8_BOM = b"\xef\xbb\xbf"
_INVALID_JSON = object()
_OUT_OF_RANGE_JSON_NUMBER = object()
_RAW_FIELDS = (
    "location",
    "snapshot_id",
    "forecast_date_raw",
    "temperature_raw",
    "weather_raw",
)
_MANIFEST_FIELDS = (
    "schema_version",
    "dataset_kind",
    "source_description",
    "locations",
    "snapshots",
)
_SNAPSHOT_FIELDS = (
    "snapshot_id",
    "snapshot_date",
    "valid_start",
    "valid_end",
)
_WEEKDAYS = {
    "星期一": 0,
    "星期二": 1,
    "星期三": 2,
    "星期四": 3,
    "星期五": 4,
    "星期六": 5,
    "星期日": 6,
    "星期天": 6,
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
    "周天": 6,
}
_DATE_RE = re.compile(
    r"(?P<value>(?:[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{2}-[0-9]{2}))"
    r"(?:(?:\r\n|\n)(?P<weekday>星期[一二三四五六日天]|周[一二三四五六日天]))?\Z"
)
_NUMBER = r"[+-]?[0-9]{1,3}(?:\.[0-9])?"
_UNIT = r"(?:℃|°C|C)"
_TEMPERATURE_RE = re.compile(
    rf"\s*(?P<lower>{_NUMBER})\s*(?:{_UNIT})?\s*"
    rf"[~～]\s*(?P<upper>{_NUMBER})\s*(?:{_UNIT})?\s*\Z"
)


class ContractValueError(ValueError):
    """A scalar value violates a stable quality rule."""

    def __init__(self, rule_id: str, message: str) -> None:
        super().__init__(message)
        self.rule_id = rule_id


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON key: {key}")
        self.key = key


class _NonStandardConstantError(ValueError):
    pass


class _InvalidUnicodeStringError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _RawScan:
    entries: tuple[tuple[int, object | None], ...]
    issues: tuple[Issue, ...]
    input_records: int | None
    decode_complete: bool


@dataclass(frozen=True, slots=True)
class _ManifestContext:
    locations: tuple[str, ...]
    locations_complete: bool
    snapshots: tuple[SnapshotDefinition, ...]
    declared_snapshot_ids: frozenset[str]
    snapshot_ids_complete: bool
    complete: bool

    @property
    def snapshots_by_id(self) -> dict[str, SnapshotDefinition]:
        return {snapshot.snapshot_id: snapshot for snapshot in self.snapshots}


def normalize_location(value: str) -> str:
    """Normalize a location without guessing aliases or changing case."""
    return unicodedata.normalize("NFC", value).strip()


def normalize_weather(value: str) -> str:
    """Normalize weather text while preserving category meaning."""
    return unicodedata.normalize("NFC", value).strip()


def normalize_snapshot_id(value: str) -> str:
    """Trim and validate an explicit printable ASCII identifier."""
    normalized = value.strip()
    if not normalized:
        raise ContractValueError("Q02", "snapshot identifier is empty")
    if not normalized.isascii() or any(
        character.isspace() or not character.isprintable()
        for character in normalized
    ):
        raise ContractValueError(
            "Q02", "snapshot identifier must contain printable ASCII without whitespace"
        )
    return normalized


def parse_forecast_date(raw: str, snapshot: SnapshotDefinition) -> date:
    """Resolve one supported forecast date inside a snapshot window."""
    normalized = raw.strip()
    match = _DATE_RE.fullmatch(normalized)
    if match is None:
        raise ContractValueError("Q04", "forecast date has unsupported syntax")

    date_text = match.group("value")
    weekday_text = match.group("weekday")
    if len(date_text) == 10:
        try:
            resolved = date.fromisoformat(date_text)
        except ValueError as error:
            raise ContractValueError("Q04", "forecast date is invalid") from error
        if not snapshot.valid_start <= resolved <= snapshot.valid_end:
            raise ContractValueError(
                "Q04", "forecast date is outside the snapshot valid window"
            )
    else:
        month = int(date_text[:2])
        day = int(date_text[3:])
        candidates: list[date] = []
        for year in range(snapshot.valid_start.year, snapshot.valid_end.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if snapshot.valid_start <= candidate <= snapshot.valid_end:
                candidates.append(candidate)
        if not candidates:
            raise ContractValueError(
                "Q04", "month-day does not occur in the snapshot valid window"
            )
        if len(candidates) > 1:
            raise ContractValueError(
                "Q04", "month-day resolves to more than one year in the valid window"
            )
        resolved = candidates[0]

    if weekday_text is not None and resolved.weekday() != _WEEKDAYS[weekday_text]:
        raise ContractValueError(
            "Q04", "weekday suffix does not match the resolved forecast date"
        )
    return resolved


def _validate_forecast_syntax_without_snapshot(raw: str) -> None:
    """Validate only date facts that do not depend on manifest context."""
    normalized = raw.strip()
    match = _DATE_RE.fullmatch(normalized)
    if match is None:
        raise ContractValueError("Q04", "forecast date has unsupported syntax")

    date_text = match.group("value")
    weekday_text = match.group("weekday")
    try:
        if len(date_text) == 10:
            resolved = date.fromisoformat(date_text)
        else:
            resolved = date(2000, int(date_text[:2]), int(date_text[3:]))
    except ValueError as error:
        raise ContractValueError("Q04", "forecast date is invalid") from error

    if (
        len(date_text) == 10
        and weekday_text is not None
        and resolved.weekday() != _WEEKDAYS[weekday_text]
    ):
        raise ContractValueError(
            "Q04", "weekday suffix does not match the forecast date"
        )


def parse_temperature(raw: str | None) -> TemperatureRange | None:
    """Parse an exact Celsius forecast range using Decimal arithmetic."""
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped or stripped == "—" or stripped.casefold() == "n/a":
        return None

    match = _TEMPERATURE_RE.fullmatch(stripped)
    if match is None:
        raise ContractValueError("Q06", "temperature range has unsupported syntax")

    try:
        lower = Decimal(match.group("lower"))
        upper = Decimal(match.group("upper"))
    except Exception as error:
        raise ContractValueError("Q06", "temperature endpoint is invalid") from error
    if not lower.is_finite() or not upper.is_finite():
        raise ContractValueError("Q06", "temperature endpoints must be finite")
    if lower == 0:
        lower = Decimal(0)
    if upper == 0:
        upper = Decimal(0)
    if lower > upper:
        raise ContractValueError(
            "Q07", "temperature minimum is greater than the maximum"
        )
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        midpoint = (lower + upper) / Decimal(2)
    if midpoint == 0:
        midpoint = Decimal(0)
    return TemperatureRange(lower, upper, midpoint)


def validate_dataset(input_path: Path, manifest_path: Path) -> ValidationResult:
    """Read, validate, normalize, and deduplicate a complete dataset."""
    input_bytes = input_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    manifest, manifest_context, manifest_issues = _parse_manifest(manifest_bytes)
    raw_scan = _scan_jsonl(input_bytes)
    data_issues = list(raw_scan.issues)

    if manifest_context is None:
        for line_number, value in raw_scan.entries:
            if value is _INVALID_JSON:
                continue
            _, independent_issues = _validate_record(value, line_number, None)
            data_issues.extend(independent_issues)
        issues = _ordered_issues(manifest_issues, data_issues)
        counts = ValidationCounts(
            input_records=raw_scan.input_records,
            individually_valid_records=None,
            invalid_records=None,
            conflict_keys=None,
            conflict_rows=None,
            duplicate_rows_removed=None,
            eligible_unique_records=None,
            output_records=0,
            missing_temperature=None,
            missing_weather=None,
            missing_month_groups=None,
        )
        return ValidationResult(
            manifest=manifest,
            canonical_records=(),
            issues=issues,
            input_sha256=input_sha256,
            manifest_sha256=manifest_sha256,
            validation_complete=False,
            counts=counts,
            missing_month_groups=(),
        )

    candidates: list[CanonicalRecord] = []
    invalid_records = 0
    for line_number, value in raw_scan.entries:
        if value is _INVALID_JSON:
            invalid_records += 1
            continue
        record, record_issues = _validate_record(
            value,
            line_number,
            manifest_context,
        )
        data_issues.extend(record_issues)
        if record is None:
            invalid_records += 1
        else:
            candidates.append(record)

    canonical, group_issues, conflict_keys, conflict_rows, duplicates = (
        _resolve_groups(candidates)
    )
    data_issues.extend(group_issues)

    if not manifest_context.complete:
        counts = ValidationCounts(
            input_records=raw_scan.input_records,
            individually_valid_records=None,
            invalid_records=None,
            conflict_keys=None,
            conflict_rows=None,
            duplicate_rows_removed=None,
            eligible_unique_records=None,
            output_records=0,
            missing_temperature=None,
            missing_weather=None,
            missing_month_groups=None,
        )
        return ValidationResult(
            manifest=manifest,
            canonical_records=(),
            issues=_ordered_issues(manifest_issues, data_issues),
            input_sha256=input_sha256,
            manifest_sha256=manifest_sha256,
            validation_complete=False,
            counts=counts,
            missing_month_groups=(),
        )

    missing_groups, quality_issues = _group_quality(manifest_context, canonical)
    data_issues.extend(quality_issues)

    input_records = raw_scan.input_records
    if input_records is None:
        raise AssertionError("decoded JSONL must have a determinate record count")
    individually_valid = len(candidates)
    if input_records != individually_valid + invalid_records:
        raise AssertionError("record validation counts are inconsistent")
    eligible = individually_valid - conflict_rows - duplicates
    if eligible != len(canonical):
        raise AssertionError("deduplication counts are inconsistent")

    counts = ValidationCounts(
        input_records=input_records,
        individually_valid_records=individually_valid,
        invalid_records=invalid_records,
        conflict_keys=conflict_keys,
        conflict_rows=conflict_rows,
        duplicate_rows_removed=duplicates,
        eligible_unique_records=eligible,
        output_records=0,
        missing_temperature=sum(
            record.temp_min_c is None and record.temp_max_c is None
            for record in canonical
        ),
        missing_weather=sum(record.weather is None for record in canonical),
        missing_month_groups=len(missing_groups),
    )
    return ValidationResult(
        manifest=manifest,
        canonical_records=canonical,
        issues=_ordered_issues(manifest_issues, data_issues),
        input_sha256=input_sha256,
        manifest_sha256=manifest_sha256,
        validation_complete=raw_scan.decode_complete,
        counts=counts,
        missing_month_groups=missing_groups,
    )


def _parse_manifest(
    data: bytes,
) -> tuple[Manifest | None, _ManifestContext | None, list[Issue]]:
    issues: list[Issue] = []
    content = data
    if content.startswith(_UTF8_BOM):
        issues.append(_manifest_issue("Q01", None, "manifest contains a UTF-8 BOM"))
        content = content[len(_UTF8_BOM) :]
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        issues.append(_manifest_issue("Q01", None, "manifest is not valid UTF-8"))
        return None, None, issues
    try:
        value = _strict_json_loads(text)
    except _DuplicateKeyError as error:
        issues.append(
            _manifest_issue("Q01", error.key, "manifest contains a duplicate JSON key")
        )
        return None, None, issues
    except _InvalidUnicodeStringError:
        issues.append(
            _manifest_issue("Q01", None, "manifest contains an invalid Unicode string")
        )
        return None, None, issues
    except (_NonStandardConstantError, json.JSONDecodeError, ValueError, RecursionError):
        issues.append(_manifest_issue("Q01", None, "manifest JSON syntax is invalid"))
        return None, None, issues

    manifest, context = _validate_manifest_object(value, issues)
    return manifest, context, issues


def _validate_manifest_object(
    value: object,
    issues: list[Issue],
) -> tuple[Manifest | None, _ManifestContext | None]:
    if not isinstance(value, dict):
        issues.append(_manifest_issue("Q02", None, "manifest must be a JSON object"))
        return None, None

    keys = set(value)
    for field in sorted(keys - set(_MANIFEST_FIELDS)):
        issues.append(_manifest_issue("Q01", field, "manifest has an extra field"))
    for field in _MANIFEST_FIELDS:
        if field not in keys:
            issues.append(_manifest_issue("Q02", field, "manifest is missing a required field"))

    schema_version: int | None = None
    if "schema_version" in value:
        raw_schema_version = value["schema_version"]
        if type(raw_schema_version) is not int:
            issues.append(_manifest_issue("Q02", "schema_version", "schema_version must be an integer"))
        elif raw_schema_version != 1:
            issues.append(_manifest_issue("Q03", "schema_version", "schema_version must equal 1"))
        else:
            schema_version = raw_schema_version

    dataset_kind: str | None = None
    if "dataset_kind" in value:
        raw_dataset_kind = value["dataset_kind"]
        if not isinstance(raw_dataset_kind, str):
            issues.append(_manifest_issue("Q02", "dataset_kind", "dataset_kind must be a string"))
        elif raw_dataset_kind != "synthetic":
            issues.append(_manifest_issue("Q03", "dataset_kind", "dataset_kind must equal synthetic"))
        else:
            dataset_kind = raw_dataset_kind

    normalized_description = ""
    if "source_description" in value:
        source_description = value["source_description"]
        if not isinstance(source_description, str):
            issues.append(_manifest_issue("Q02", "source_description", "source_description must be a string"))
        else:
            normalized_description = source_description.strip()
            if not normalized_description:
                issues.append(_manifest_issue("Q02", "source_description", "source_description must not be empty"))
            elif not _is_synthetic_description(normalized_description):
                issues.append(
                    _manifest_issue(
                        "Q03",
                        "source_description",
                        "source_description must identify synthetic demonstration or test data without source claims",
                    )
                )

    normalized_locations: list[str] = []
    locations_complete = False
    if "locations" in value:
        locations_value = value["locations"]
        if not isinstance(locations_value, list):
            issues.append(_manifest_issue("Q02", "locations", "locations must be a JSON array"))
        else:
            locations_complete = True
            if not locations_value:
                issues.append(_manifest_issue("Q03", "locations", "locations must not be empty"))
            seen_locations: set[str] = set()
            for index, raw_location in enumerate(locations_value):
                field = f"locations[{index}]"
                if not isinstance(raw_location, str):
                    issues.append(_manifest_issue("Q02", field, "location must be a string"))
                    locations_complete = False
                    continue
                location = normalize_location(raw_location)
                if not location:
                    issues.append(_manifest_issue("Q02", field, "normalized location must not be empty"))
                    locations_complete = False
                    continue
                if location in seen_locations:
                    issues.append(_manifest_issue("Q03", field, "normalized manifest location is duplicated"))
                    continue
                seen_locations.add(location)
                normalized_locations.append(location)

    snapshots: list[SnapshotDefinition] = []
    declared_snapshot_ids: set[str] = set()
    snapshot_ids_complete = False
    snapshot_definitions_complete = False
    if "snapshots" in value:
        snapshots_value = value["snapshots"]
        if not isinstance(snapshots_value, list):
            issues.append(_manifest_issue("Q02", "snapshots", "snapshots must be a JSON array"))
        else:
            snapshot_ids_complete = True
            if not snapshots_value:
                issues.append(_manifest_issue("Q03", "snapshots", "snapshots must not be empty"))
            id_counts: dict[str, int] = defaultdict(int)
            validated: list[tuple[str | None, SnapshotDefinition | None]] = []
            for index, raw_snapshot in enumerate(snapshots_value):
                normalized_id: str | None = None
                if isinstance(raw_snapshot, dict):
                    raw_id = raw_snapshot.get("snapshot_id")
                    if isinstance(raw_id, str):
                        try:
                            normalized_id = normalize_snapshot_id(raw_id)
                        except ContractValueError:
                            pass
                if normalized_id is None:
                    snapshot_ids_complete = False
                else:
                    declared_snapshot_ids.add(normalized_id)
                    id_counts[normalized_id] += 1
                    if id_counts[normalized_id] > 1:
                        issues.append(
                            _manifest_issue(
                                "Q03",
                                f"snapshots[{index}].snapshot_id",
                                "snapshot identifier is duplicated",
                            )
                        )
                snapshot, _ = _validate_snapshot(raw_snapshot, index, issues)
                validated.append((normalized_id, snapshot))

            duplicate_ids = {
                snapshot_id
                for snapshot_id, count in id_counts.items()
                if count > 1
            }
            snapshots = [
                snapshot
                for normalized_id, snapshot in validated
                if snapshot is not None and normalized_id not in duplicate_ids
            ]
            snapshot_definitions_complete = (
                snapshot_ids_complete and len(snapshots) == len(snapshots_value)
            )

    context = _ManifestContext(
        locations=tuple(normalized_locations),
        locations_complete=locations_complete,
        snapshots=tuple(snapshots),
        declared_snapshot_ids=frozenset(declared_snapshot_ids),
        snapshot_ids_complete=snapshot_ids_complete,
        complete=locations_complete and snapshot_definitions_complete,
    )
    manifest = None
    if (
        schema_version is not None
        and dataset_kind is not None
        and context.complete
    ):
        manifest = Manifest(
            schema_version=schema_version,
            dataset_kind=dataset_kind,
            source_description=normalized_description,
            locations=context.locations,
            snapshots=context.snapshots,
        )
    return manifest, context


def _validate_snapshot(
    value: object,
    index: int,
    issues: list[Issue],
) -> tuple[SnapshotDefinition | None, bool]:
    prefix = f"snapshots[{index}]"
    if not isinstance(value, dict):
        issues.append(_manifest_issue("Q02", prefix, "snapshot must be a JSON object"))
        return None, True
    fatal = False
    keys = set(value)
    for name in sorted(keys - set(_SNAPSHOT_FIELDS)):
        issues.append(_manifest_issue("Q01", f"{prefix}.{name}", "snapshot has an extra field"))
    for name in _SNAPSHOT_FIELDS:
        if name not in keys:
            issues.append(_manifest_issue("Q02", f"{prefix}.{name}", "snapshot is missing a required field"))
            fatal = True
    snapshot_id = ""
    if "snapshot_id" in value:
        raw_id = value["snapshot_id"]
        if not isinstance(raw_id, str):
            issues.append(_manifest_issue("Q02", f"{prefix}.snapshot_id", "snapshot_id must be a string"))
            fatal = True
        else:
            try:
                snapshot_id = normalize_snapshot_id(raw_id)
            except ContractValueError as error:
                issues.append(_manifest_issue(error.rule_id, f"{prefix}.snapshot_id", str(error)))
                fatal = True

    dates: dict[str, date] = {}
    for name in ("snapshot_date", "valid_start", "valid_end"):
        if name not in value:
            continue
        raw_date = value[name]
        if not isinstance(raw_date, str):
            issues.append(_manifest_issue("Q02", f"{prefix}.{name}", f"{name} must be a string"))
            fatal = True
            continue
        try:
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_date) is None:
                raise ValueError
            dates[name] = date.fromisoformat(raw_date)
        except ValueError:
            issues.append(_manifest_issue("Q03", f"{prefix}.{name}", f"{name} must be a valid ISO date"))
            fatal = True

    if len(dates) == 3 and not (
        dates["snapshot_date"] <= dates["valid_start"] <= dates["valid_end"]
    ):
        issues.append(_manifest_issue("Q03", prefix, "snapshot date window ordering is invalid"))
        fatal = True
    if fatal:
        return None, True
    return (
        SnapshotDefinition(
            snapshot_id=snapshot_id,
            snapshot_date=dates["snapshot_date"],
            valid_start=dates["valid_start"],
            valid_end=dates["valid_end"],
        ),
        False,
    )


def _scan_jsonl(data: bytes) -> _RawScan:
    issues: list[Issue] = []
    content = data
    if content.startswith(_UTF8_BOM):
        issues.append(_data_issue("Q01", "ERROR", (1,), None, None, "input contains a UTF-8 BOM"))
        content = content[len(_UTF8_BOM) :]
    entries: list[tuple[int, object | None]] = []
    input_records = 0
    decode_complete = True
    for line_number, raw_line in enumerate(content.split(b"\n"), start=1):
        try:
            physical_line = raw_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            if raw_line.strip():
                input_records += 1
                entries.append((line_number, _INVALID_JSON))
                issues.append(
                    _data_issue(
                        "Q01",
                        "ERROR",
                        (line_number,),
                        None,
                        None,
                        "record line is not valid UTF-8",
                    )
                )
            decode_complete = False
            continue
        if not physical_line.strip():
            continue
        input_records += 1
        try:
            value = _strict_json_loads(physical_line)
        except _DuplicateKeyError as error:
            issues.append(
                _data_issue(
                    "Q01",
                    "ERROR",
                    (line_number,),
                    error.key,
                    None,
                    "record contains a duplicate JSON key",
                )
            )
            entries.append((line_number, _INVALID_JSON))
            continue
        except _InvalidUnicodeStringError:
            issues.append(
                _data_issue(
                    "Q01",
                    "ERROR",
                    (line_number,),
                    None,
                    None,
                    "record contains an invalid Unicode string",
                )
            )
            entries.append((line_number, _INVALID_JSON))
            continue
        except (_NonStandardConstantError, json.JSONDecodeError, ValueError, RecursionError):
            issues.append(
                _data_issue(
                    "Q01",
                    "ERROR",
                    (line_number,),
                    None,
                    None,
                    "record JSON syntax is invalid",
                )
            )
            entries.append((line_number, _INVALID_JSON))
            continue
        entries.append((line_number, value))

    if input_records == 0:
        issues.append(
            _data_issue("Q11", "ERROR", (), None, None, "input contains no records")
        )
    return _RawScan(
        tuple(entries),
        tuple(issues),
        input_records,
        decode_complete,
    )


def _validate_record(
    value: object,
    line_number: int,
    manifest: _ManifestContext | None,
) -> tuple[CanonicalRecord | None, list[Issue]]:
    issues: list[Issue] = []
    lines = (line_number,)
    if not isinstance(value, dict):
        issues.append(_data_issue("Q02", "ERROR", lines, None, None, "record must be a JSON object"))
        return None, issues

    keys = set(value)
    for field in sorted(keys - set(_RAW_FIELDS)):
        issues.append(_data_issue("Q01", "ERROR", lines, field, None, "record has an extra field"))
    for field in _RAW_FIELDS:
        if field not in keys:
            issues.append(_data_issue("Q02", "ERROR", lines, field, None, "record is missing a required field"))

    location: str | None = None
    location_declared = False
    if "location" in value:
        raw_location = value["location"]
        if not isinstance(raw_location, str):
            issues.append(_data_issue("Q02", "ERROR", lines, "location", None, "location must be a string"))
        else:
            location = normalize_location(raw_location)
            if not location:
                issues.append(_data_issue("Q02", "ERROR", lines, "location", None, "normalized location must not be empty"))
            elif manifest is not None:
                location_declared = location in manifest.locations
                if not location_declared and manifest.locations_complete:
                    issues.append(_data_issue("Q03", "ERROR", lines, "location", None, "location is not declared in the manifest"))

    snapshot_id: str | None = None
    snapshot: SnapshotDefinition | None = None
    if "snapshot_id" in value:
        raw_snapshot_id = value["snapshot_id"]
        if not isinstance(raw_snapshot_id, str):
            issues.append(_data_issue("Q02", "ERROR", lines, "snapshot_id", None, "snapshot_id must be a string"))
        else:
            try:
                snapshot_id = normalize_snapshot_id(raw_snapshot_id)
            except ContractValueError as error:
                issues.append(_data_issue(error.rule_id, "ERROR", lines, "snapshot_id", None, str(error)))
            else:
                if manifest is not None:
                    snapshot = manifest.snapshots_by_id.get(snapshot_id)
                    if (
                        snapshot is None
                        and manifest.snapshot_ids_complete
                        and snapshot_id not in manifest.declared_snapshot_ids
                    ):
                        issues.append(_data_issue("Q03", "ERROR", lines, "snapshot_id", None, "snapshot_id is not declared in the manifest"))

    valid_date: date | None = None
    if "forecast_date_raw" in value:
        raw_date = value["forecast_date_raw"]
        if not isinstance(raw_date, str):
            issues.append(_data_issue("Q02", "ERROR", lines, "forecast_date_raw", None, "forecast_date_raw must be a string"))
        elif snapshot is not None:
            try:
                valid_date = parse_forecast_date(raw_date, snapshot)
            except ContractValueError as error:
                issues.append(_data_issue(error.rule_id, "ERROR", lines, "forecast_date_raw", None, str(error)))
        elif snapshot is None:
            try:
                _validate_forecast_syntax_without_snapshot(raw_date)
            except ContractValueError as error:
                issues.append(_data_issue(error.rule_id, "ERROR", lines, "forecast_date_raw", None, str(error)))

    temperature: TemperatureRange | None = None
    if "temperature_raw" in value:
        raw_temperature = value["temperature_raw"]
        if raw_temperature is not None and not isinstance(raw_temperature, str):
            issues.append(_data_issue("Q02", "ERROR", lines, "temperature_raw", None, "temperature_raw must be a string or null"))
        else:
            try:
                temperature = parse_temperature(raw_temperature)
            except ContractValueError as error:
                issues.append(_data_issue(error.rule_id, "ERROR", lines, "temperature_raw", None, str(error)))
            else:
                if temperature is None:
                    issues.append(_data_issue("Q05", "WARNING", lines, "temperature_raw", None, "temperature is missing"))

    weather: str | None = None
    if "weather_raw" in value:
        raw_weather = value["weather_raw"]
        if raw_weather is not None and not isinstance(raw_weather, str):
            issues.append(_data_issue("Q02", "ERROR", lines, "weather_raw", None, "weather_raw must be a string or null"))
        else:
            weather = None if raw_weather is None else normalize_weather(raw_weather)
            if weather == "":
                weather = None
            if weather is None:
                issues.append(_data_issue("Q08", "WARNING", lines, "weather_raw", None, "weather is missing"))

    if (
        manifest is not None
        and location is not None
        and location_declared
        and snapshot_id is not None
        and snapshot is not None
        and valid_date is not None
    ):
        logical_key = (snapshot_id, location, valid_date)
        issues = [
            replace(issue, logical_key=logical_key)
            if issue.logical_key is None
            else issue
            for issue in issues
        ]

    if any(issue.severity == "ERROR" for issue in issues):
        return None, issues
    if manifest is None:
        return None, issues
    if (
        location is None
        or not location_declared
        or snapshot_id is None
        or snapshot is None
        or valid_date is None
    ):
        return None, issues
    return (
        CanonicalRecord(
            location=location,
            snapshot_id=snapshot_id,
            snapshot_date=snapshot.snapshot_date,
            valid_date=valid_date,
            temp_min_c=None if temperature is None else temperature.temp_min_c,
            temp_max_c=None if temperature is None else temperature.temp_max_c,
            temp_midpoint_c=None if temperature is None else temperature.temp_midpoint_c,
            weather=weather,
            source_lines=lines,
        ),
        issues,
    )


def _resolve_groups(
    records: list[CanonicalRecord],
) -> tuple[tuple[CanonicalRecord, ...], list[Issue], int, int, int]:
    grouped: dict[tuple[str, str, date], list[CanonicalRecord]] = defaultdict(list)
    for record in records:
        grouped[record.logical_key].append(record)

    canonical: list[CanonicalRecord] = []
    issues: list[Issue] = []
    conflict_keys = 0
    conflict_rows = 0
    duplicate_rows_removed = 0
    for logical_key, group in grouped.items():
        source_lines = tuple(sorted({line for record in group for line in record.source_lines}))
        business_values = {record.business_values for record in group}
        if len(business_values) > 1:
            differing_fields = tuple(
                field_name
                for index, field_name in enumerate(
                    ("temp_min_c", "temp_max_c", "weather")
                )
                if len({values[index] for values in business_values}) > 1
            )
            conflict_keys += 1
            conflict_rows += len(group)
            issues.append(
                _data_issue(
                    "Q10",
                    "ERROR",
                    source_lines,
                    None,
                    logical_key,
                    "logical-key group contains conflicting normalized values; "
                    f"differing fields: {', '.join(differing_fields)}",
                )
            )
            continue
        retained = min(group, key=lambda record: record.source_lines)
        canonical.append(replace(retained, source_lines=source_lines))
        if len(group) > 1:
            duplicate_rows_removed += len(group) - 1
            issues.append(
                _data_issue(
                    "Q09",
                    "INFO",
                    source_lines,
                    None,
                    logical_key,
                    "exact duplicate records were deterministically merged",
                )
            )

    canonical.sort(
        key=lambda record: (
            record.snapshot_date,
            record.snapshot_id,
            record.location,
            record.valid_date,
        )
    )
    return tuple(canonical), issues, conflict_keys, conflict_rows, duplicate_rows_removed


def _group_quality(
    manifest: Manifest | _ManifestContext,
    records: tuple[CanonicalRecord, ...],
) -> tuple[tuple[MissingMonthGroup, ...], list[Issue]]:
    issues: list[Issue] = []
    missing: list[MissingMonthGroup] = []
    overall: dict[tuple[str, str], list[CanonicalRecord]] = defaultdict(list)
    monthly: dict[tuple[str, str, str], list[CanonicalRecord]] = defaultdict(list)
    for record in records:
        overall[(record.snapshot_id, record.location)].append(record)
        monthly[
            (record.snapshot_id, record.location, record.valid_date.strftime("%Y-%m"))
        ].append(record)

    for snapshot in sorted(
        manifest.snapshots,
        key=lambda item: (item.snapshot_date, item.snapshot_id),
    ):
        for location in sorted(manifest.locations):
            overall_group = overall.get((snapshot.snapshot_id, location), [])
            _append_all_missing_issues(
                issues,
                overall_group,
                f"overall group {snapshot.snapshot_id}/{location}",
            )
            for year_month, period_start, period_end in _iter_month_windows(snapshot):
                group = monthly.get((snapshot.snapshot_id, location, year_month), [])
                if not group:
                    missing.append(
                        MissingMonthGroup(
                            snapshot_id=snapshot.snapshot_id,
                            location=location,
                            year_month=year_month,
                            period_start=period_start,
                            period_end=period_end,
                        )
                    )
                    issues.append(
                        _data_issue(
                            "Q12",
                            "WARNING",
                            (),
                            "year_month",
                            None,
                            f"manifest-defined group {snapshot.snapshot_id}/{location}/{year_month} has no records",
                        )
                    )
                else:
                    _append_all_missing_issues(
                        issues,
                        group,
                        f"monthly group {snapshot.snapshot_id}/{location}/{year_month}",
                    )
    return tuple(missing), issues


def _append_all_missing_issues(
    issues: list[Issue],
    records: list[CanonicalRecord],
    group_name: str,
) -> None:
    if not records:
        return
    source_lines = tuple(sorted({line for record in records for line in record.source_lines}))
    if all(record.temp_min_c is None for record in records):
        issues.append(
            _data_issue(
                "Q13",
                "WARNING",
                source_lines,
                "temperature",
                None,
                f"{group_name} has no non-missing temperature values",
            )
        )
    if all(record.weather is None for record in records):
        issues.append(
            _data_issue(
                "Q13",
                "WARNING",
                source_lines,
                "weather",
                None,
                f"{group_name} has no non-missing weather values",
            )
        )


def _iter_month_windows(
    snapshot: SnapshotDefinition,
) -> list[tuple[str, date, date]]:
    windows: list[tuple[str, date, date]] = []
    cursor = date(snapshot.valid_start.year, snapshot.valid_start.month, 1)
    while cursor <= snapshot.valid_end:
        if cursor == date(9999, 12, 1):
            next_month = None
            month_end = date.max
        elif cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
            month_end = next_month - timedelta(days=1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
            month_end = next_month - timedelta(days=1)
        windows.append(
            (
                f"{cursor.year:04d}-{cursor.month:02d}",
                max(cursor, snapshot.valid_start),
                min(month_end, snapshot.valid_end),
            )
        )
        if next_month is None:
            break
        cursor = next_month
    return windows


def _strict_json_loads(text: str) -> object:
    value = json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_parse_json_decimal,
        parse_int=_parse_json_integer,
    )
    _ensure_unicode_scalars(value)
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _ensure_unicode_scalars(key)
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise _NonStandardConstantError(value)


def _parse_json_integer(value: str) -> object:
    try:
        return int(value)
    except ValueError:
        return _parse_json_decimal(value)


def _parse_json_decimal(value: str) -> Decimal | object:
    try:
        return Decimal(value)
    except DecimalException:
        return _OUT_OF_RANGE_JSON_NUMBER


def _ensure_unicode_scalars(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise _InvalidUnicodeStringError("unpaired Unicode surrogate")
        return
    if isinstance(value, list):
        for item in value:
            _ensure_unicode_scalars(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _ensure_unicode_scalars(key)
            _ensure_unicode_scalars(item)


def _is_synthetic_description(value: str) -> bool:
    lowered = value.casefold()
    has_purpose_marker = (
        re.search(r"\b(?:demonstration|demo|testing|test)\b", lowered)
        is not None
        or any(marker in lowered for marker in ("演示", "测试"))
    )
    negated_purpose = re.search(
        r"\b(?:not|never)\s+(?:(?:intended|meant)\s+)?"
        r"(?:(?:for|as)\s+)?(?:demonstration|demo|testing|test)\b|"
        r"\bno\s+(?:demonstration|demo|testing|test)\b|"
        r"(?:不用于|并非用于|不是用于|非用于).{0,4}(?:测试|演示)",
        lowered,
    )
    identifies_purpose = has_purpose_marker and negated_purpose is None
    has_synthetic_marker = (
        re.search(r"\bsynthetic\b", lowered) is not None
        or any(marker in lowered for marker in ("合成", "模拟"))
    )
    negated_synthetic = re.search(
        r"\b(?:not|never)\s+"
        r"(?:(?:actually|really|truly|fully|entirely)\s+)?"
        r"(?:(?:considered|described|presented|intended)\s+(?:as\s+)?)?"
        r"(?:a\s+|the\s+)?synthetic\b|"
        r"\bno\s+(?:synthetic\b|"
        r"(?:claim|evidence|indication)\b[^.!?;:\n]{0,60}\bsynthetic\b)|"
        r"(?:不是|并非|不属于|非)(?:真正的?)?(?:合成|模拟)",
        lowered,
    )
    positive_synthetic = has_synthetic_marker and negated_synthetic is None
    safe_denial = re.compile(
        r"\b(?:not|no|never)\s+"
        r"(?:(?:based|derived)\s+(?:on|from)\s+|using\s+)?"
        r"(?:(?:real|actual|historical|live)\s+)*(?:weather\s+)?"
        r"(?:data|observations?|records?|collection|measurements?)\b|"
        r"\b(?:does|do)\s+not\s+"
        r"(?:contain|use|represent|describe|claim|collect|derive\s+from)\s+"
        r"(?:(?:real|actual|historical|live)\s+)*(?:weather\s+)?"
        r"(?:data|observations?|records?|collection|measurements?)\b|"
        r"(?:并非|不是|不含|不使用|没有使用)(?:任何)?"
        r"(?:真实|实际|历史|实时){1,3}(?:天气|气象)?"
        r"(?:观测|实测|数据|记录|采集|收集)"
    )
    claims_only = safe_denial.sub("", lowered)
    prohibited_claim = re.search(
        r"\bhistorical\b(?:\W+\w+){0,3}\W+"
        r"(?:observations?|measurements?|collection)\b|"
        r"\bhistorical\s+(?:weather\s+)?data\b|"
        r"\b(?:observed|collected)\s+(?:weather\s+)?"
        r"(?:data|observations?|measurements?|records?)\b|"
        r"\b(?:actual|real|production|live)\b(?:\W+\w+){0,3}\W+"
        r"(?:weather|data|measurements?|records?|collection|sensors?|telemetry|stations?)\b|"
        r"\b(?:copied|gathered|obtained|sourced|downloaded|scraped|derived|"
        r"fetched|imported|replayed)"
        r"\s+from\s+(?:\w+\s+){0,4}"
        r"(?:production|live|operational|official|government|real|actual|"
        r"historical|sensors?|telemetry|stations?|archives?|api)\b|"
        r"(?:真实|实际|历史|实时).{0,12}"
        r"(?:天气|气象|观测|实测|数据|采集|收集|气象站|测站)|"
        r"(?:来自|来源于|采集自|收集自).{0,12}"
        r"(?:真实|实际|历史|实时|气象站|测站)",
        claims_only,
    )
    return positive_synthetic and identifies_purpose and prohibited_claim is None


def _manifest_issue(rule_id: str, field: str | None, message: str) -> Issue:
    return Issue(rule_id, "ERROR", (), field, None, message)


def _data_issue(
    rule_id: str,
    severity: str,
    source_lines: tuple[int, ...],
    field: str | None,
    logical_key: tuple[str, str, date] | None,
    message: str,
) -> Issue:
    return Issue(rule_id, severity, source_lines, field, logical_key, message)  # type: ignore[arg-type]


def _ordered_issues(
    manifest_issues: list[Issue],
    data_issues: list[Issue],
) -> tuple[Issue, ...]:
    return tuple(
        sorted(manifest_issues, key=Issue.sort_key)
        + sorted(data_issues, key=Issue.sort_key)
    )


__all__ = [
    "ContractValueError",
    "normalize_location",
    "normalize_snapshot_id",
    "normalize_weather",
    "parse_forecast_date",
    "parse_temperature",
    "validate_dataset",
]
