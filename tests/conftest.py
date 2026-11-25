import pytest

from trade_hub import db, fx


@pytest.fixture(autouse=True)
def _fx_offline(monkeypatch):
    """대시보드 환율: 테스트는 네트워크 없이 1 USD = 1,400원."""
    monkeypatch.setattr(fx, "ONLINE", False)
    monkeypatch.setattr(fx, "_cost_fallback", lambda: 1400.0)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """테스트마다 새 공용 DB (실제 data/trade.db는 건드리지 않음)."""
    monkeypatch.setenv("TRADE_DB", str(tmp_path / "trade.db"))
    c = db.connect()
    yield c
    c.close()
