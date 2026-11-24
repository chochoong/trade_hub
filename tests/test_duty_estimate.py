"""스키마 v17 compliance_checks.duty_estimate_json — 3번이 HS 후보로 조회한 관세율 참고값 (관세사 확인값 아님)."""
import json

import pytest

from trade_hub import dashboard, db

EST = {"general_pct": 8.0, "fta_pct": 0.0, "fta_name": "한-중 FTA", "origin": "CN", "complete": True,
       "checked_on": "2026-11-20", "basis": "관세청 관세율표 · HS 미확정 (후보 중 가장 높은 세율)", "candidates": []}


@pytest.fixture(autouse=True)
def no_naver(monkeypatch):
    from trade_hub import naver
    monkeypatch.setattr(naver, "keys", lambda: None)


def test_duty_estimate_saved_and_shown_but_confirmed_rate_wins(conn):
    assert "duty_estimate" in db.COMPLIANCE_LIST_FIELDS and db.SCHEMA_VERSION >= 17
    with conn:
        a = db.upsert_product(conn, "벽선반", stage="sourcing")
        b = db.upsert_product(conn, "키캡", stage="sourcing")
        c = db.upsert_product(conn, "케이스", stage="sourcing")
        cid = db.save_compliance_check(conn, a, None, "conditional", "2026-11-20", duty_estimate=EST)
        db.save_compliance_check(conn, b, None, "go", "2026-11-20", duty_estimate=None)          # 못 찾음 → NULL
        db.save_compliance_check(conn, c, None, "go", "2026-11-20", duty_estimate={**EST, "general_pct": 6.5,
                                                                                    "fta_pct": None, "complete": False})
    row = conn.execute("SELECT duty_estimate_json FROM compliance_checks WHERE id = ?", (cid,)).fetchone()
    assert json.loads(row[0])["general_pct"] == 8.0
    assert conn.execute("SELECT duty_rate FROM products WHERE id = ?", (a,)).fetchone()[0] is None   # 확정 칸은 그대로
    html = dashboard.render(conn)
    assert "참고 8% (FTA 0%)</span>" in html and "관세사 확인값 아님" in html and "한-중 FTA" in html
    assert "참고 6.5%?</span>" in html and "후보 일부만 조회됨" in html                              # FTA 없음·일부만
    with conn:
        conn.execute("UPDATE products SET duty_rate = 0.065 WHERE id = ?", (a,))                    # 관세사 확인값이 이김
    html = dashboard.render(conn)
    assert "참고 8%" not in html and '6.5% <span class="small muted">확정</span>' in html


def test_v16_db_gets_duty_estimate_column(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    monkeypatch.setenv("TRADE_DB", str(path))
    db.connect().close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE compliance_checks DROP COLUMN duty_estimate_json")
    raw.execute("PRAGMA user_version = 16")
    raw.commit(); raw.close()
    c = db.connect()
    try:
        assert "duty_estimate_json" in [r[1] for r in c.execute("PRAGMA table_info(compliance_checks)")]
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        c.close()
