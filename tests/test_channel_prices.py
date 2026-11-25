"""스키마 v19 product_suppliers.channel_prices_json (4번 마켓별 판매가) → 2번 공급처 표 쿠팡·스마트스토어 권장가."""
import json
import sqlite3
from datetime import date

from trade_hub import dashboard, db

CP = {"coupang": {"target_krw": 11900, "walk_krw": 9800}, "smartstore": {"target_krw": 11200, "walk_krw": 9300},
      "qty": 100, "landed_krw": 5230, "updated_at": "2026-11-25T10:00:00"}


class FakeNaver:
    @staticmethod
    def search_url(kw):
        return "https://search.shopping.naver.com/search/all?query=" + kw

    @staticmethod
    def link_keyword(row):
        return row.get("naver_keyword") or row.get("name")

    @staticmethod
    def status(row, limit=30):
        return {"price": row["price"], "checked": "2026-11-24", "days": 1, "stale": False, "memo": ""} if row else None


def test_supplier_table_channel_prices(conn, monkeypatch):
    with conn:
        pid = db.upsert_product(conn, "벽선반", stage="sourcing")
        conn.execute("UPDATE products SET est_retail_krw = 13980, keywords_ko_json = ? WHERE id = ?",
                     (json.dumps(["무타공 벽선반"], ensure_ascii=False), pid))
        other = db.upsert_product(conn, "키캡", stage="sourcing")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=2.0, channel_prices_json=CP)
        db.link_supplier(conn, other, sid, unit_price_usd=1.0)
    assert json.loads(conn.execute("SELECT channel_prices_json FROM product_suppliers WHERE id = ?", (ps,)).fetchone()[0])["qty"] == 100
    monkeypatch.setattr(dashboard, "naver_context", lambda: {
        "nl": FakeNaver, "by_hub": {pid: {"id": "shelf", "name": "벽선반", "naver_keyword": "무타공 선반"}},
        "lows": {"shelf": {"price": 10000}}, "days": 30})
    html, _ = dashboard.products_in_progress(conn)
    assert "<th>쿠팡 권장가</th><th>스마트스토어 권장가</th>" in html
    assert ">11,900원</b>" in html and ">11,200원</b>" in html and "원가 5,230원 · 100개 기준" in html
    assert "최소 마진 판매가 9,800원" in html and "시장 가격 아님" in html
    assert "1번 판매가보다 " in html and "15% 낮음" in html                        # 11,900 vs 13,980
    assert "query=무타공 선반" in html and "네이버 최저가 10,000원" in html and "권장가가 12% 높음" in html
    assert "query=키캡" in html                                                   # buyagent에 없는 품목: 품목 검색어로 링크만
    assert '<span class="muted" title="4번 원가 계산 전">–</span>' in html


def test_v18_db_gets_channel_prices_column(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setenv("TRADE_DB", str(path))
    db.connect().close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE product_suppliers DROP COLUMN channel_prices_json")
    raw.execute("PRAGMA user_version = 18")
    raw.commit(); raw.close()
    c = db.connect()
    try:
        assert "channel_prices_json" in [r[1] for r in c.execute("PRAGMA table_info(product_suppliers)")]
    finally:
        c.close()
