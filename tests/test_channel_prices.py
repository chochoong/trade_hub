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
    assert "<th>단가</th><th>스마트스토어 권장가</th><th>연락처</th>" in html and "쿠팡 권장가" not in html
    assert ">11,900원</b>" not in html and ">11,200원</b>" in html and "원가 5,230원 · 100개 기준" in html
    assert "최소 마진 판매가 9,300원" in html and "시장 가격 아님" in html
    assert "1번 판매가보다 " in html and "20% 낮음" in html                        # 11,200 vs 13,980
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


def test_channel_dash_reasons():
    row = lambda cp: {"channel_prices_json": json.dumps(cp) if cp is not None else None}
    p = {"id": 1, "est_retail_krw": None, "name": "x", "keywords_ko_json": None}
    assert "4번 원가 계산 전" in dashboard.channel_cells(row(None), p, {})[0]
    c = dashboard.channel_cells(row({"smartstore": {"target_krw": None, "walk_krw": None}, "qty": 5}), p, {})
    assert len(c) == 1 and "100% 이상" in c[0]                               # 쿠팡 칸은 뺌


def test_best_qty_block():
    row = {"qty": 200, "landed_krw": 8135, "unit_profit_krw": 4839, "total_profit_krw": 967778, "cash_krw": 1786680, "margin_pct": 25.5}
    big = {"qty": 500, "landed_krw": 7000, "unit_profit_krw": 5000, "total_profit_krw": 2500000, "cash_krw": 3900000, "margin_pct": 30.0}
    cp = {"best_qty": row, "best_none_reason": None, "min_target_qty": 120, "target_margin_pct": 25, "price_krw": 20900,
          "max_cash_krw": 2000000, "by_qty": [row, big]}
    h = dashboard.qty_block(cp)
    assert "<b>이익 최대 200개</b> · 개당 +4,839원 · 총 +967,778원 (자금 179만원)" in h
    assert "목표 마진 25%는 120개부터" in h and "수량별 이익 (2)" in h and "<td><b>200</b></td>" in h
    assert '<tr class="muted"><td>500</td>' in h and "판매가 20,900원(1번 예상 판매가, 부가세 포함) 기준 · 이익·원가는 부가세 제외 · 자금엔 수입 부가세 포함" in h
    nop = dashboard.qty_block({**cp, "best_qty": None, "best_none_reason": "no_profit", "min_target_qty": None})
    assert '<span class="bad">어떤 수량도 손해</span>' in nop and "목표 마진" not in nop
    over = dashboard.qty_block({**cp, "best_qty": None, "best_none_reason": "over_cash"})
    assert "자금 한도 200만원 안에서 살 수 있는 수량 없음" in over and "어떤 수량도 손해" not in over
    assert dashboard.qty_block({"smartstore": {"target_krw": 1}, "qty": 50}) == ""             # 판매가 없음 → 키 없음
