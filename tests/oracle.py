"""Independent frozen constants used by the contract tests."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MANIFEST = PROJECT_ROOT / "data" / "sample" / "manifest.json"
SAMPLE_RECORDS = PROJECT_ROOT / "data" / "sample" / "records.jsonl"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SAMPLE_RECORDS_SHA256 = (
    "71e6fff3bd7d349a3415b93f1a7657df04767ecce5bb43e92abdb1552edb55bc"
)
SAMPLE_MANIFEST_SHA256 = (
    "30cc3661478e986cad1408fdbe1323f1dfc6d9e4049531919abfb0c6df78dcad"
)
