# Public data-contract summary

This document summarizes the public data contract implemented and tested by
the current pipeline. The implementation and tests define actual behavior; this
summary does not introduce a separate contract or add requirements.

## Files and encoding

The pipeline consumes one UTF-8 JSON manifest and one UTF-8 JSON Lines file.
Byte-order marks, malformed UTF-8, invalid JSON, duplicate JSON object keys,
non-standard numeric constants, unpaired Unicode surrogates, and undeclared
fields are rejected. Blank JSONL lines are ignored; every nonblank physical
line must contain exactly one JSON object.

## Manifest fields

The manifest is an object with exactly these fields:

| Field | Implemented requirement |
|---|---|
| `schema_version` | JSON integer exactly `1`; booleans are not integers |
| `dataset_kind` | String exactly `synthetic` |
| `source_description` | Non-empty string identifying synthetic demonstration or test data without a real-source claim |
| `locations` | Non-empty array of strings; each value is trimmed, NFC-normalized, non-empty, and unique after normalization |
| `snapshots` | Non-empty array of snapshot objects with unique normalized identifiers |

Each snapshot object has exactly four fields:

| Field | Implemented requirement |
|---|---|
| `snapshot_id` | String; trimmed value is non-empty printable ASCII with no whitespace and is unique in the manifest |
| `snapshot_date` | Valid `YYYY-MM-DD` calendar date |
| `valid_start` | Valid `YYYY-MM-DD` calendar date |
| `valid_end` | Valid `YYYY-MM-DD` calendar date |

Dates must satisfy `snapshot_date <= valid_start <= valid_end`. A snapshot is a
declared forecast issue and its closed valid-date window; distinct snapshot IDs
remain distinct even when they share a snapshot date.

## JSONL record fields

Every record object has exactly these fields:

| Field | Implemented requirement and normalization |
|---|---|
| `location` | String; trimmed and NFC-normalized, non-empty, and declared in `locations` |
| `snapshot_id` | String; normalized as above and declared in `snapshots` |
| `forecast_date_raw` | String in supported date syntax that resolves to exactly one date in the snapshot window |
| `temperature_raw` | String or `null`; a supported Celsius range or an implemented missing marker |
| `weather_raw` | String or `null`; trimmed and NFC-normalized, with empty text treated as missing |

Supported forecast-date text is `MM-DD` or `YYYY-MM-DD`, optionally followed by
a line break and a Chinese weekday label. A full date must lie within the
snapshot's closed valid window. An `MM-DD` value is resolved only when exactly
one candidate year in that window produces the date. Invalid, out-of-window,
ambiguous, impossible, or weekday-mismatched dates are rejected.

Temperature text contains two signed decimal endpoints separated by `~` or
`～`, with optional Celsius units and surrounding whitespace. Endpoints use one
to three integer digits and at most one fractional digit. Parsing uses
`Decimal`; the lower endpoint may equal but must not exceed the upper endpoint.
The midpoint is the exact arithmetic midpoint. `null`, empty/whitespace text,
`—`, and case-insensitive `N/A` are missing-temperature markers.

## Canonical records, duplicates, and conflicts

The logical key is:

```text
(snapshot_id, location, valid_date)
```

Canonical business values are `(temp_min_c, temp_max_c, weather)`. When every
record in one key group has equal normalized business values, one record is
retained, all source line numbers are preserved in sorted order, and extra rows
are reported by Q09. Format differences in equivalent raw values therefore do
not create a conflict.

If any canonical business value differs, including `null` versus non-`null`,
Q10 reports the differing fields and the entire logical-key group is excluded.
No value is selected, combined, or imputed from a conflicting group.

Canonical output ordering is by `snapshot_date`, `snapshot_id`, `location`,
then `valid_date`, using normalized Unicode code-point ordering for text.

## Missing-value and aggregation semantics

Missing temperature produces null canonical temperature fields; missing
weather produces a null canonical weather field. Neither becomes zero and
neither is imputed. A record may remain eligible when either value is missing.

Temperature metrics use only records with non-missing temperature intervals;
`n_temperature` is that denominator. Weather counts and proportions use only
non-missing weather values; `n_weather` is that denominator. When a non-empty
overall or monthly group has no usable values for one measure, its metrics are
null and Q13 records the condition.

Overall summaries include every manifest-defined snapshot/location pair.
Monthly summaries include every calendar-month intersection with each
snapshot's valid window, including empty groups. Q12 and
`missing_month_groups` identify manifest-defined monthly groups with no
canonical records.

## Quality rules Q01-Q13

| Rule | Severity | Implemented meaning |
|---|---|---|
| Q01 | ERROR | Encoding/JSON structural failure, duplicate key, extra field, BOM, or invalid Unicode/constant |
| Q02 | ERROR | Missing field, wrong type, invalid empty value, or invalid snapshot identifier shape |
| Q03 | ERROR | Manifest value/reference constraint failure, duplicate normalized declaration, unsupported data claim, or invalid snapshot window |
| Q04 | ERROR | Unsupported, invalid, ambiguous, out-of-window, or weekday-mismatched forecast date |
| Q05 | WARNING | Missing temperature |
| Q06 | ERROR | Unsupported or invalid temperature range syntax/value |
| Q07 | ERROR | Temperature minimum exceeds maximum |
| Q08 | WARNING | Missing weather |
| Q09 | INFO | Exact normalized duplicates deterministically merged |
| Q10 | ERROR | Conflicting normalized values within one logical-key group; whole group excluded |
| Q11 | ERROR | Input contains no records |
| Q12 | WARNING | Manifest-defined monthly group contains no canonical records |
| Q13 | WARNING | Non-empty overall or monthly group has no non-missing temperature or weather values |

A result is valid only when scanning completed and no `ERROR` issue exists.
Warnings and informational issues remain visible without making an otherwise
complete result invalid.

## Command and artifact behavior

`validate` writes `quality.json`-shaped content to standard output and writes no
files. `run` writes only `quality.json` for invalid data; for valid data it
writes the seven artifacts listed in the README. Output is assembled privately
and delivered atomically to a new or empty directory.
