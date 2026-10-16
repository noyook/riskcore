"""Shared fixtures for the verification suite.

Discipline (verification area rule 3): no test reads a wall clock — every
timestamp is injected. The fake clock returns a fixed instant; tests that need
"later" advance an explicit offset.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eventlog import SqliteStore

from .factories import AS_OF


class FakeClock:
    def __init__(self, now: datetime = AS_OF) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        from datetime import timedelta

        self.now = self.now + timedelta(**delta)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    s = SqliteStore(tmp_path / "factlog.db")
    yield s
    s.close()


@pytest.fixture
def app_store(store: SqliteStore):
    """FastAPI TestClient bound to a fresh store + fake clock."""
    from fastapi.testclient import TestClient

    from eventlog.app import create_app

    client = TestClient(create_app(store, FakeClock()))
    return client, store
