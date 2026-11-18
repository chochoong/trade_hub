import json
import sqlite3
from datetime import date

import pytest

from trade_hub import buying, dashboard, naver

PRODUCTS = """id,name,category,keyword,jan,must_include,exclude,trusted_shops,sources,shipping_profile,bundle_size,my_price_coupang,my_price_naver,active,memo,hub_product_id
ai-glasses,AI안경 <스마트>,smart_wearable,AI smart glasses,,,,,manual,,,0,0,Y,2번 공급처 발굴에서 가져옴 20261117_120713,23
watch,세이코 시계,watch,セイコー,,,,,,,,0,0,N,시계 예시,
"""
PRICES = """id,price,currency,origin,shipping,in_stock,shop,url,checked,memo
ai-glasses,46.6,USD,CN,,Y,NAMOFOTO,https://www.aliexpress.us/item/1.html,2026-11-17,소매가
ai-glasses,34.5,USD,CN,3.2,N,Evil,javascript:alert(1),2026-11-01,오래된 가격
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(naver, "keys", lambda: None)


@pytest.fixture
def ba(tmp_path, monkeypatch):
    base = tmp_path / "buyagent"
    (base / "catalog").mkdir(parents=True)
    (base / "catalog" / "products.csv").write_text(PRODUCTS, encoding="utf-8-sig")
    (base / "catalog" / "manual_prices.csv").write_text(PRICES, encoding="utf-8-sig")
    monkeypatch.setenv("BUYAGENT_DIR", str(base))
    return base


def test_page_without_results_shows_catalog(ba):
    html = buying.page()
    assert "아직 실행 결과가 없습니다" in html
    assert '<a href="#p23"' in html and "AI안경 &lt;스마트&gt;" in html          # hub_product_id → 품목 카드 링크
    assert "2번에서 가져옴" in html and "꺼짐" in html
    assert 'href="https://www.aliexpress.us/item/1.html"' in html
    assert 'href="javascript' not in html                                       # 위험한 주소는 링크로 걸지 않음
    assert ">미확인</span>" in html                                               # 빈 배송비 = 미확인
    assert "품절" in html


def test_stale_manual_price_flagged(ba):
    data = buying.load()
    html = buying._prices_section(data, today=date(2026, 11, 18))
    assert html.count(">다시 확인</span>") == 1                                  # 11-01 확인분만 (17일 전)


def test_latest_json_results(ba):
    (ba / "reports").mkdir()
    (ba / "reports" / "latest.json").write_text(json.dumps({
        "schema": 1, "run_at": "2026-11-18T09:00:00", "ok": True, "error": None, "demo": False,
        "markets": ["coupang", "naver"],
        "items": [
            {"kind": "감시", "id": "ai-glasses", "hub_product_id": 23, "name": "AI안경", "category": "smart_wearable",
             "status": "ok", "blocked_reason": None,
             "best_offer": {"source": "manual", "shop": "NAMOFOTO", "url": "https://x.example/1", "price": 46.6,
                            "shipping": None, "shipping_known": False, "currency": "USD", "in_stock": True},
             "cost_krw": 71000, "quotes": [{"market": "coupang", "recommended_price": 89000, "margin": 12000}],
             "warnings": ["카메라 탑재 → 문구 주의"], "events": [], "notice": "구매대행 고지"},
            {"kind": "감시", "id": "watch", "name": "시계", "status": "blocked", "blocked_reason": "가품 위험",
             "best_offer": None, "cost_krw": None, "quotes": [], "warnings": [], "events": []}]}), encoding="utf-8")
    html = buying.page()
    assert "2026-11-18 09:00 실행" in html and "89,000원" in html and "마진 12,000원" in html
    assert "배송비 미확인" in html and "카메라 탑재" in html
    assert ">차단</span>" in html and "가품 위험" in html
    nav = buying.nav_item()
    assert 'href="#buy"' in nav and "성공 · 11/18" in nav and ">1<small> 감시" in nav   # 활성 1개


def test_broken_latest_json_does_not_break_page(ba):
    (ba / "reports").mkdir()
    (ba / "reports" / "latest.json").write_text("{깨진", encoding="utf-8")
    assert "latest.json을 읽지 못했습니다" in buying.page()


def test_history_read_only(ba):
    conn = sqlite3.connect(ba / "history.db")
    conn.execute("CREATE TABLE history (ts, pid, in_stock, total, currency, source, shop, url, cost_krw)")
    conn.execute("INSERT INTO history VALUES ('2026-11-18 09:00', 'ai-glasses', 1, 46.6, 'USD', 'manual', 'NAMOFOTO', '', 70000)")
    conn.commit()
    conn.close()
    html = buying.page()
    assert "전체 1건" in html and "70,000원" in html


def test_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("BUYAGENT_DIR", str(tmp_path / "없음"))
    assert "buyagent 폴더를 찾지 못했습니다" in buying.page()
    assert "폴더 없음" in buying.nav_item()


def test_dashboard_has_buying_tab_next_to_sourcing(ba, conn, tmp_path):
    html = dashboard.build(tmp_path / "d.html", conn, refs=False).read_text(encoding="utf-8")
    nav = html[html.index('<nav class="pipe-nav">'):html.index("</nav>")]
    assert nav.index('href="#a2"') < nav.index('href="#buy"') < nav.index('href="#a3"')   # 2 공급처 바로 옆
    assert 'id="p-buy"' in html and "구매대행 (buyagent)" in html


def test_search_results(ba):
    html = buying.page()
    assert "아직 직접 검색한 결과가 없습니다" in html
    (ba / "reports").mkdir()
    (ba / "reports" / "search.json").write_text(json.dumps({
        "ok": True, "error": None, "run_at": "2026-11-18T11:00:00",
        "query": {"keyword": "セイコー SBTQ041", "category": "watch", "must_include": ["SBTQ041"], "exclude": []},
        "markets": ["coupang"], "found": 90, "excluded": {"상품명 불일치": 25},
        "items": [{"kind": "검색", "id": "search", "hub_product_id": None, "name": "セイコー SBTQ041 <正規品>",
                   "category": "watch", "status": "ok", "blocked_reason": None,
                   "best_offer": {"source": "rakuten_web", "shop": "スリーエス", "url": "https://item.rakuten.co.jp/a/",
                                  "price": 14630, "shipping": 0, "shipping_known": True, "currency": "JPY",
                                  "in_stock": True},
                   "cost_krw": 140565, "quotes": [{"market": "coupang", "recommended_price": 182900, "margin": 20405,
                                                   "my_price": None, "my_margin": None}],
                   "warnings": ["한정·예약 상품"], "events": [], "notice": None}],
    }, ensure_ascii=False), encoding="utf-8")
    html = buying.page()
    assert "セイコー SBTQ041 &lt;正規品&gt;" in html
    assert "140,565원" in html and "182,900원" in html
    assert 'href="https://item.rakuten.co.jp/a/"' in html
    assert "상품명 불일치 25건" in html


def test_search_json_broken(ba):
    (ba / "reports").mkdir()
    (ba / "reports" / "search.json").write_text("{", encoding="utf-8")
    assert "search.json을 읽지 못했습니다" in buying.page()


def test_search_form_only_in_server_mode(ba):
    (ba / "config").mkdir()
    (ba / "config" / "categories.toml").write_text(
        '[categories.watch]\nname = "시계"\nallowed = true\n[categories.food]\nname = "식품"\nallowed = false\n',
        encoding="utf-8")
    assert 'form class="buysearch"' not in buying.page()
    dashboard.SERVER["on"] = True
    try:
        html = buying.page()
    finally:
        dashboard.SERVER["on"] = False
    assert 'form class="buysearch"' in html and '<option value="watch" selected>시계</option>' in html
    assert 'value="food"' not in html
    assert 'name="query" required maxlength="100" autocomplete="off" placeholder' in html   # 새로 고침하면 빈 칸 (value 없음)


def test_search_shows_korean_original(ba, conn):
    from trade_hub import translate
    translate.to_japanese("세이코 시계", call=lambda q: "セイコー 腕時計")
    (ba / "reports").mkdir()
    (ba / "reports" / "search.json").write_text(json.dumps({"ok": True, "run_at": "2026-11-18T12:00:00",
        "query": {"keyword": "セイコー 腕時計", "category": "watch"}, "markets": [], "found": 0, "items": []},
        ensure_ascii=False), encoding="utf-8")
    assert "(← 세이코 시계)" in buying.page()


def test_search_shows_keyword_original_from_buyagent(ba, conn):
    (ba / "reports").mkdir()
    (ba / "reports" / "search.json").write_text(json.dumps({"ok": True, "run_at": "2026-11-18T12:00:00",
        "query": {"keyword": "セイコー 腕時計", "keyword_original": "세이코 손목시계", "category": "watch"},
        "markets": [], "found": 0, "items": []}, ensure_ascii=False), encoding="utf-8")
    assert "(← 세이코 손목시계)" in buying.page()
