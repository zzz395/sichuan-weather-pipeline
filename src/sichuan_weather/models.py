"""Domain models for validated weather-forecast snapshot data."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Literal


Severity = Literal["ERROR", "WARNING", "INFO"]
LogicalKey = tuple[str, str, date]


@dataclass(frozen=True, slots=True)
class Issue:
    """A deterministic validation or data-quality issue."""

    rule_id: str
    severity: Severity
    source_lines: tuple[int, ...]
    field: str | None
    logical_key: LogicalKey | None
    message: str

    def sort_key(self) -> tuple[object, ...]:
        """Return the frozen ordering key used in ``quality.json``."""
        first_line = self.source_lines[0] if self.source_lines else -1
        logical_key = (
            ("", "", "")
            if self.logical_key is None
            else (
                self.logical_key[0],
                self.logical_key[1],
                self.logical_key[2].isoformat(),
            )
        )
        return (
            0 if not self.source_lines else 1,
            first_line,
            self.rule_id,
            self.field or "",
            *logical_key,
            self.message,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize this issue using stable key and value shapes."""
        result: dict[str, object] = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "source_lines": list(self.source_lines),
        }
        if self.field is not None:
            result["field"] = self.field
        if self.logical_key is not None:
            result["logical_key"] = {
                "snapshot_id": self.logical_key[0],
                "location": self.logical_key[1],
                "valid_date": self.logical_key[2].isoformat(),
            }
        result["message"] = self.message
        return result


@dataclass(frozen=True, slots=True)
class SnapshotDefinition:
    """One explicitly declared forecast snapshot and its valid window."""

    snapshot_id: str
    snapshot_date: date
    valid_start: date
    valid_end: date


@dataclass(frozen=True, slots=True)
class Manifest:
    """Normalized dataset manifest."""

    schema_version: int
    dataset_kind: str
    source_description: str
    locations: tuple[str, ...]
    snapshots: tuple[SnapshotDefinition, ...]

    @property
    def snapshots_by_id(self) -> dict[str, SnapshotDefinition]:
        """Return snapshot definitions keyed by their explicit identifier."""
        return {snapshot.snapshot_id: snapshot for snapshot in self.snapshots}


@dataclass(frozen=True, slots=True)
class TemperatureRange:
    """A normalized Celsius forecast interval and its exact midpoint."""

    temp_min_c: Decimal
    temp_max_c: Decimal
    temp_midpoint_c: Decimal


@dataclass(frozen=True, slots=True)
class CanonicalRecord:
    """One canonical post-validation, post-deduplication record."""

    location: str
    snapshot_id: str
    snapshot_date: date
    valid_date: date
    temp_min_c: Decimal | None
    temp_max_c: Decimal | None
    temp_midpoint_c: Decimal | None
    weather: str | None
    source_lines: tuple[int, ...]

    @property
    def logical_key(self) -> LogicalKey:
        return (self.snapshot_id, self.location, self.valid_date)

    @property
    def business_values(
        self,
    ) -> tuple[Decimal | None, Decimal | None, str | None]:
        return (self.temp_min_c, self.temp_max_c, self.weather)


@dataclass(frozen=True, slots=True)
class MissingMonthGroup:
    """A manifest-expanded month that contains no canonical records."""

    snapshot_id: str
    location: str
    year_month: str
    period_start: date
    period_end: date


@dataclass(frozen=True, slots=True)
class ValidationCounts:
    """Quality counters; ``None`` denotes a genuinely undetermined value."""

    input_records: int | None
    individually_valid_records: int | None
    invalid_records: int | None
    conflict_keys: int | None
    conflict_rows: int | None
    duplicate_rows_removed: int | None
    eligible_unique_records: int | None
    output_records: int
    missing_temperature: int | None
    missing_weather: int | None
    missing_month_groups: int | None

    def with_output_records(self, output_records: int) -> ValidationCounts:
        """Return a copy suitable for validate-only or run quality output."""
        if output_records < 0:
            raise ValueError("output_records must not be negative")
        return replace(self, output_records=output_records)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Complete result of reading, validating, and canonicalizing a dataset."""

    manifest: Manifest | None
    canonical_records: tuple[CanonicalRecord, ...]
    issues: tuple[Issue, ...]
    input_sha256: str
    manifest_sha256: str
    validation_complete: bool
    counts: ValidationCounts
    missing_month_groups: tuple[MissingMonthGroup, ...]

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "ERROR" for issue in self.issues)

    @property
    def is_valid(self) -> bool:
        return self.validation_complete and not self.has_errors

    @property
    def status(self) -> str:
        return "valid" if self.is_valid else "invalid"

    @property
    def input_records(self) -> int | None:
        return self.counts.input_records

    @property
    def individually_valid_records(self) -> int | None:
        return self.counts.individually_valid_records

    @property
    def invalid_records(self) -> int | None:
        return self.counts.invalid_records

    @property
    def conflict_keys(self) -> int | None:
        return self.counts.conflict_keys

    @property
    def conflict_rows(self) -> int | None:
        return self.counts.conflict_rows

    @property
    def duplicate_rows_removed(self) -> int | None:
        return self.counts.duplicate_rows_removed

    @property
    def eligible_unique_records(self) -> int | None:
        return self.counts.eligible_unique_records

    @property
    def output_records(self) -> int:
        return self.counts.output_records

    @property
    def missing_temperature(self) -> int | None:
        return self.counts.missing_temperature

    @property
    def missing_weather(self) -> int | None:
        return self.counts.missing_weather
