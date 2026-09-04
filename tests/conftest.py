"""Shared test configuration.

anyio's pytest plugin ships with anyio (a FastAPI dependency), so async tests
need no extra package. Restrict it to asyncio -- there is no trio here.
"""

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
