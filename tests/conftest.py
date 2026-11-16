import pytest

from trade_hub import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """테스트마다 새 공용 DB (실제 data/trade.db는 건드리지 않음)."""
    monkeypatch.setenv("TRADE_DB", str(tmp_path / "trade.db"))
    c = db.connect()
    yield c
    c.close()
