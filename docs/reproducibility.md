# Reproducibility

This document describes the supported targets, dependency controls,
deterministic outputs, contract coverage, and offline acceptance methodology.

## Current public status

| Target or gate | Status |
|---|---|
| Windows x64 / CPython 3.12 local offline acceptance | PASS |
| Windows x64 / CPython 3.13 local offline acceptance | PASS |
| Linux validation | PENDING |
| Hosted GitHub Actions | NOT RUN |
| Git-mirror fresh-clone acceptance | PENDING |
| Publication acceptance | PENDING |

These results are limited to the stated Windows targets. They do not establish
cross-platform or publication acceptance. `.w3/` is an ignored local
acceptance workspace; its contents are neither public documentation nor
distribution inputs.

## Supported targets

- `windows-x64-py312`: CPython 3.12, x86-64
- `windows-x64-py313`: CPython 3.13, x86-64
- `linux-x64-py312`: CPython 3.12, x86-64
- `linux-x64-py313`: CPython 3.13, x86-64

The project metadata requires Python `>=3.12,<3.14`. Target-specific hashed
runtime and development locks are stored under `requirements/`. Normal CI
validation is configured to refuse a target when either required lock is absent
rather than resolve dependencies implicitly. Committing a lock does not itself
establish validation for that target.

## Contract coverage

The test suite covers these observable areas:

| Area | Covered behavior |
|---|---|
| Manifest and JSONL structure | Strict UTF-8/JSON, exact fields, types, declarations, snapshot windows, and source-description constraints |
| Date parsing | Full dates, cross-year month/day resolution, ambiguity, leap days, valid windows, and weekday checks |
| Temperature parsing | Missing markers, signed and decimal ranges, equal endpoints, negative zero, malformed values, and inverted ranges |
| Record grouping | Logical keys, exact duplicates, conflicts, source-line retention, and deterministic ordering |
| Missing data | Missing temperature/weather denominators, all-missing groups, and explicit missing months |
| Analysis and serialization | Expanded summary groups, stable schemas, decimal precision, UTF-8/LF output, and deterministic ordering |
| CLI and delivery | Exit-code classes, paths with spaces, validate-only behavior, new/empty output requirements, and atomic failure handling |
| Figures | Panel coverage, labels, missing-data states, category counts, and visible synthetic disclosure |
| Distribution | Exact safe sdist/wheel inventories, metadata, console entry point, installed import, and in-memory source compilation |
| Repeated execution | Seven-artifact inventory and byte identity across independent processes |
| Offline tooling | Candidate copying, wheelhouse closure, target checks, negative probes, and network-isolation refusal behavior |

The detailed field and quality-rule summary is in
[Public data-contract summary](data-contract.md).

## Dependency lock strategy

`requirements/baseline.in` constrains shared compatibility versions. Each
target lock is generated on its matching interpreter: runtime first, then the
development lock with the runtime lock as a constraint. Accepted lock files:

- pin every dependency and include SHA-256 hashes;
- contain no direct URLs, VCS references, editable installs, local paths, or
  package indexes;
- are regenerated from the locked resolver environment; and
- must be byte-identical across the two resolver passes.

The development lock covers test, build, packaging, and reproducibility tools;
the runtime lock covers only runtime installation.

## Wheelhouse strategy

Wheelhouse preparation is the online phase. It downloads exactly one
compatible, lock-authorized wheel for each active development dependency and
writes a deterministic manifest. Verification resolves the target requirements
using local files only and rejects missing, added, incompatible, unhashed, or
unauthorized material.

The repository does not bundle dependency wheels. A user planning a
disconnected install must prepare and verify the target wheelhouse before
disconnecting.

## Deterministic output

For a fixed manifest, JSONL input, Python target, and accepted dependency set,
the pipeline produces a fixed seven-file artifact inventory:

```text
cleaned.csv
quality.json
summary.csv
monthly_summary.csv
weather_frequency.csv
figures/temperature-ranges.png
figures/weather-frequency.png
```

Determinism is enforced through NFC normalization, exact decimal arithmetic,
explicit sort keys, fixed CSV/JSON schemas and line endings, fixed decimal
precision, stable issue ordering, and fixed visualization settings. Independent
processes use separate plotting caches and different hash seeds, then compare
every artifact byte-for-byte. The artifact checker independently validates
schemas, counts, ordering, hashes, PNG signatures, dimensions, and synthetic
notices.

## Local test procedure

In an isolated environment installed from the matching development lock and
the current project, run:

```console
python -m pip check
python -m pytest --import-mode=importlib -p no:cacheprovider -ra tests
```

The full run must complete with no failures, skips, xfails, or xpasses. Import
checks confirm the package comes from the active environment rather than source
path injection.

## Offline acceptance methodology

Offline acceptance is distinct from setting pip to `--no-index`. For each
target, the method is:

1. Prepare and verify a target-specific wheelhouse while online.
2. Freeze an exact allowlisted source candidate and verify its file hashes.
3. Disconnect the machine and prove the required connectivity probes are
   unreachable before beginning.
4. In new isolated environments, install locked dependencies only from the
   wheelhouse, build the sdist and wheel, install each distribution, and run
   import and console checks outside the source tree.
5. Run the complete test suite, the public synthetic sample, the artifact
   checker, packaging checks, and negative wheelhouse/hash probes.
6. Run the sample independently again and compare all seven artifact files
   byte-for-byte.
7. Recheck network unreachability, inspect both figures for content and the
   synthetic notice, and report PASS only if every required phase succeeds.

The acceptance command does not change network state. Any reachable network,
missing material, target mismatch, or nonzero required phase prevents PASS; a
failed run is not resumed after online repair.

## Hosted workflow boundary

The workflow defines Windows and Ubuntu jobs for Python 3.12 and 3.13. It is
configured to use committed target locks, build a wheelhouse online, and then
perform restricted local-only installs and validation. Configuration is not an
execution result: the hosted workflow has not run, and Linux validation remains
pending.
