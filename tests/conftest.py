"""Shared pytest factories; every factory call returns independent mutable data."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.helpers import make_manifest, make_record


@pytest.fixture
def manifest_factory() -> Callable[[], dict[str, object]]:
    return make_manifest


@pytest.fixture
def record_factory() -> Callable[[str, str | None, str | None], dict[str, object]]:
    return make_record
