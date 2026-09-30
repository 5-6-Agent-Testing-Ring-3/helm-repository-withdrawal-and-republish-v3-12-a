from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def chart_dir() -> Path:
    return Path(__file__).parents[1] / "charts" / "ledger-api"


@pytest.fixture(autouse=True)
def prohibit_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit tests must not contact live services")

    monkeypatch.setattr("socket.create_connection", blocked)
