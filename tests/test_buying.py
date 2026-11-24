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
    monkeypatch.setattr(buying, "AUTO_NAME_KO", False)                 # 상품명 한국어 풀이: 실제 번역 호출 안 함


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
    assert "상품명 불일치: 25건" in html


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
    assert 'form class="buysearch"' in html and '<option value="watch">시계</option>' in html and " selected" not in html
    assert '<option value="">카테고리 선택</option>' in html and 'name="category" required' in html   # 미리 고르지 않음
    assert 'value="food"' not in html
    assert 'name="query" required maxlength="100" autocomplete="off" placeholder' in html   # 새로 고침하면 빈 칸 (value 없음)
    assert html.index('data-lang="en"') < html.index('data-lang="ja"') < html.index('data-lang="zh"')
    assert html.index('value="rakuten"') < html.index('value="ebay"') < html.index('value="china"')
    assert 'value="aliexpress"' not in html and "타오바오·1688(중국어" not in html     # 중국 항목 하나로 합침
    assert '<option value="china" data-lang="zh">중국(타오바오·1688·알리 · 중국어 · 링크만)</option>' in html
    assert "알리·이베이는 buyagent에 API 키" not in html and "야후는 buyagent에 앱 ID, 이베이는 API 키 필요" in html
    assert 'title="영어로 번역 (이베이용)"' in html and 'title="중국어로 번역 (중국(타오바오·1688·알리) 링크용)"' in html
    assert '<option value="ebay" data-lang="en">이베이(미국·영어로 검색)</option>' in html


def test_search_zero_results_show_reason_and_error(ba):
    (ba / "reports").mkdir()
    f = ba / "reports" / "search.json"
    f.write_text(json.dumps({"ok": True, "run_at": "2026-11-19T10:00:00", "found": 30,
        "query": {"keyword": "watch", "category": "watch", "market": "ebay"},
        "markets": [], "excluded": {"차단:전자제품": 12}, "items": []}, ensure_ascii=False), encoding="utf-8")
    html = buying.page()
    assert '이베이 "watch"' in html and "걸러낸 결과 — 차단:전자제품: 12건" in html
    f.write_text(json.dumps({"ok": False, "error": "키 없음 → .env 에 EBAY_APP_ID", "run_at": "2026-11-19T10:00:00",
        "query": {"keyword": "watch", "category": "watch", "market": "ebay"}, "markets": [], "found": 0, "items": []},
        ensure_ascii=False), encoding="utf-8")
    html = buying.page()
    assert '<p class="bad">키 없음 → .env 에 EBAY_APP_ID</p>' in html and "판매처 없음" not in html



def test_search_shows_keyword_original_from_buyagent(ba, conn):
    (ba / "reports").mkdir()
    (ba / "reports" / "search.json").write_text(json.dumps({"ok": True, "run_at": "2026-11-18T12:00:00",
        "query": {"keyword": "セイコー 腕時計", "keyword_original": "세이코 손목시계", "category": "watch"},
        "markets": [], "found": 0, "items": []}, ensure_ascii=False), encoding="utf-8")
    assert "(← 세이코 손목시계)" in buying.page()


REAL_WEB = buying.buyagent_dir.__globals__["db"].PKG_DIR.parent.parent / "buyagent" / "buyagent" / "web.py"


def _search_json(ba, **query):
    (ba / "reports").mkdir(exist_ok=True)
    q = {"keyword": "セイコー SBTQ041", "keyword_original": "세이코 SBTQ041", "category": "watch", "market": "ebay",
         "must_include": ["SBTQ041"], "exclude": ["ベルト", "バンド"], **query}
    (ba / "reports" / "search.json").write_text(json.dumps({"ok": True, "run_at": "2026-11-19T10:00:00", "query": q,
        "markets": [], "found": 3, "items": []}, ensure_ascii=False), encoding="utf-8")


def test_search_shows_add_watch_or_already_watching(ba):
    _search_json(ba)
    assert "감시에 추가" not in buying.page()                       # 정적 화면에는 버튼 없음
    dashboard.SERVER["on"] = True
    try:
        html = buying.page()
        assert 'data-buywatch="add" data-name="세이코 SBTQ041"' in html and 'name="must" value="SBTQ041"' in html
        assert 'data-buywatch="toggle" data-id="ai-glasses">끄기' in html and 'data-id="watch">켜기' in html
        _search_json(ba, keyword="セイコー")                       # products.csv 의 watch 줄과 같은 검색어 (꺼진 상태)
        html = buying.page()
    finally:
        dashboard.SERVER["on"] = False
    assert 'data-buywatch="add"' not in html and "이미 감시 중 — ID <b>watch</b> (꺼짐" in html


def _copy_buyagent_code(ba):
    import shutil
    shutil.copy(REAL_WEB, ba / "web.py")
    shutil.copytree(REAL_WEB.parent / "core", ba / "core", ignore=shutil.ignore_patterns("__pycache__"))


@pytest.mark.skipif(not REAL_WEB.is_file(), reason="buyagent web.py 없음")
def test_watch_call_uses_buyagent_add_and_toggle(ba):
    _copy_buyagent_code(ba)
    _search_json(ba)
    s = json.loads((ba / "reports" / "search.json").read_text(encoding="utf-8"))
    code, out = buying.watch_call("add", ba, search=s, name="세이코 SBTQ041", must="SBTQ041|SBTQ")
    assert code == 200, out
    row = buying.load()["products"][-1]
    assert (row["id"], row["name"], row["keyword"], row["category"], row["sources"], row["active"]) ==         (out["id"], "세이코 SBTQ041", "セイコー SBTQ041", "watch", "ebay", "Y")
    assert row["must_include"] == "SBTQ041|SBTQ" and row["exclude"] == "ベルト|バンド"
    assert buying.watch_call("add", ba, search=s, name="", must=None)[0] == 409             # 같은 검색어 → 이미 감시 중
    assert buying.watch_call("toggle", ba, id=out["id"]) == (200, {"id": out["id"], "active": "N"})
    assert buying.watch_call("toggle", ba, id="없음")[0] == 404

def test_suggest_must_same_rule_as_buyagent():
    assert buying.suggest_must({"must_include": ["A1", ""], "keyword": "x SBTR"}) == "A1"
    assert buying.suggest_must({"keyword": "セイコー SBTR シリーズ SBTQ041 ab1 12"}) == "SBTR|SBTQ041"
    assert buying.suggest_must({"keyword": "セイコー 腕時計"}) == ""


@pytest.mark.skipif(not REAL_WEB.is_file(), reason="buyagent web.py 없음")
def test_suggest_must_matches_real_buyagent(ba):
    _copy_buyagent_code(ba)
    for q in ({"keyword": "セイコー SBTR シリーズ SBTQ041"}, {"must_include": ["X9"], "keyword": "a"}, {"keyword": "abc DEF"}):
        assert buying.suggest_must(q) == buying.watch_call("suggest", ba, query=q)[1]["must"]

def test_cost_cell_shows_forwarding_and_local_shipping():
    it = {"cost_krw": 140565, "forwarding_krw": 15000, "shipping_profile": "medium",
          "best_offer": {"shipping_known": True, "shipping": 0, "currency": "JPY"}}
    assert buying.cost_cell(it) == '140,565원<div class="small muted">배대지 15,000원 포함 (medium) · 현지배송 무료</div>'
    it.update(forwarding_krw=0, best_offer={"shipping_known": False})
    assert "배대지 없음 (직배송) · 현지배송 미확인" in buying.cost_cell(it)
    it.update(best_offer={"shipping_known": True, "shipping": 550, "currency": "JPY"})
    assert "현지배송 550 JPY 포함" in buying.cost_cell(it)
    assert buying.cost_cell({"cost_krw": None}) is not None and "배대지" not in buying.cost_cell({"cost_krw": 5})


def test_paused_and_shipping_rate_warnings(ba):
    (ba / "config").mkdir()
    (ba / "config" / "settings.toml").write_text(
        '[origins.JP]\nname = "일본"\n[origins.US]\nname = "미국"\n'
        '[shipping_checked]\nmax_days = 30\nJP = ""\nUS = "2026-01-01"\nCN = "2099-01-01"\n', encoding="utf-8")
    html = buying.page()
    assert "일본 배대지 요금 확인 기록 없음" in html and "미국 배대지 요금 확인한 지 30일 넘음 (마지막 확인 2026-01-01)" in html
    assert "중국" not in html.split("배대지 요금")[0][-200:] and "CN 배대지" not in html
    assert "감시 멈춤 중" not in html
    (ba / "data").mkdir()
    (ba / "data" / "watch_paused.json").write_text('{"since": "2026-11-19 15:00"}', encoding="utf-8")
    (ba / "reports").mkdir()
    (ba / "reports" / "latest.json").write_text(json.dumps({"ok": True, "run_at": "2026-11-01T10:00:00", "markets": [],
                                                           "items": []}), encoding="utf-8")
    html = buying.page()
    assert "⏸ 감시 전체 멈춤 중 (2026-11-19 15:00부터)" in html and "갱신되지 않는 것은 정상" in html
    assert "마지막 감시 2026-11-01 10:00" in html and "data-buywatch" not in html            # 정적 화면엔 버튼 없음
    assert "감시 멈춤 중" in buying.nav_item()


def test_watch_line_server_mode_uses_buyagent_status(ba, monkeypatch):
    calls = []

    def fake_call(action, base=None, **kw):
        calls.append(action)
        return 200, {"paused_since": None, "schedule": {"registered": True, "next": "2026-11-19 오후 4:00:00"}}
    monkeypatch.setattr(buying, "watch_call", fake_call)
    buying.forget_status()
    (ba / "reports").mkdir()
    (ba / "reports" / "latest.json").write_text(json.dumps({"ok": True, "demo": True, "run_at": "2026-11-19T15:00:00",
                                                           "markets": [], "items": []}), encoding="utf-8")
    dashboard.SERVER["on"] = True
    try:
        html = buying.page()
        buying.page()                                                           # 30초 안에는 다시 묻지 않음
    finally:
        dashboard.SERVER["on"] = False
        buying.forget_status()
    assert calls == ["status"]
    assert "● 감시 중" in html and "자동 실행: <b>매시간</b> (다음 2026-11-19 오후 4:00:00)" in html
    assert "마지막 감시 2026-11-19 15:00 (예시 데이터)" in html
    assert 'data-buywatch="pause" data-paused="1">⏸ 감시 전체 멈춤' in html
    assert 'data-buywatch="schedule" data-on="0">자동 실행 끄기' in html


@pytest.mark.skipif(not REAL_WEB.is_file(), reason="buyagent web.py 없음")
def test_pause_uses_real_buyagent(ba):
    _copy_buyagent_code(ba)
    assert buying.watch_call("pause", ba, paused=True) == (200, {"paused": True})
    assert buying.paused_since(ba) and buying.watch_call("status", ba)[1]["paused_since"] == buying.paused_since(ba)
    assert buying.watch_call("pause", ba, paused=False) == (200, {"paused": False})
    assert buying.paused_since(ba) is None


def test_china_option_links_only_no_api_search():
    js = dashboard.SERVER_JS
    china = js.index('if (body.market === "china") {')
    branch = js[china:js.index("startJob(\"/api/buysearch\"", china)]
    assert 'buyTranslate(f, "zh")' in branch and "buyLinks(f, q)" in branch and "return;" in branch   # 중국어로만 번역, API 전에 끝
    for site in ("https://s.taobao.com/search?q=", "https://s.1688.com/selloffer/offer_search.htm",
                 "https://www.aliexpress.com/w/wholesale-"):
        assert site in js
    assert [k for k, _ in buying.SITE_OPTIONS] == ["rakuten", "yahoo", "ebay", "china"]
    assert "aliexpress" in buying.BUY_MARKETS and "yahoo" in buying.BUY_MARKETS   # 서버 허용 값 (알리는 화면에서만 안 부름)


def test_yahoo_option_only_when_buyagent_accepts_market(tmp_path):
    main = tmp_path / "main.py"
    main.write_text('SEARCH_MARKETS = {\n    "rakuten": {"lang": "ja"},\n    "ebay": {"lang": "en"},\n}\n', encoding="utf-8")
    assert [k for k, _ in buying.site_options(tmp_path)] == ["rakuten", "ebay", "china"]
    main.write_text('SEARCH_MARKETS = {\n    "rakuten": {"lang": "ja"},\n    "yahoo": {"lang": "ja", "sources": ["yahoo"]},\n}\n',
                    encoding="utf-8")
    assert [k for k, _ in buying.site_options(tmp_path)] == ["rakuten", "yahoo", "ebay", "china"]
    assert [k for k, _ in buying.site_options(tmp_path / "none")] == ["rakuten", "ebay", "china"]
    assert buying.SITE_LANG["yahoo"] == "ja"


@pytest.mark.skipif(not REAL_WEB.is_file(), reason="buyagent web.py 없음")
def test_naver_low_link_value_and_save_use_real_buyagent(ba):
    _copy_buyagent_code(ba)
    (ba / "reports").mkdir(exist_ok=True)
    (ba / "reports" / "latest.json").write_text(json.dumps({"ok": True, "run_at": "2026-11-20T10:00:00", "markets": ["coupang", "naver"],
        "items": [{"id": "ai-glasses", "name": "AI안경", "status": "ok", "quotes": [{"market": "naver", "recommended_price": 171600, "margin": 1}]},
                  {"id": "watch", "name": "세이코 시계", "status": "ok", "quotes": []},
                  {"id": "gone", "name": "목록에 없는 상품 (괄호)", "status": "ok", "quotes": []}]}), encoding="utf-8")
    _search_json(ba)
    sj = json.loads((ba / "reports" / "search.json").read_text(encoding="utf-8"))
    sj.update(markets=["coupang", "naver"], items=[{"name": "SEIKO SBTQ041", "status": "ok", "best_offer": {"price": 1, "currency": "USD"},
                                                    "quotes": [{"market": "naver", "recommended_price": 50000, "margin": 1}]}])
    (ba / "reports" / "search.json").write_text(json.dumps(sj, ensure_ascii=False), encoding="utf-8")
    code, out = buying.watch_call("naver_low", ba, id="ai-glasses", price="165,000", memo="스토어A 배송비 포함")
    assert code == 200 and out["saved"]["price"] == "165000", out
    assert buying.watch_call("naver_low", ba, id="없음", price="1", memo="")[0] == 404
    assert "숫자" in buying.watch_call("naver_low", ba, id="watch", price="abc", memo="")[1]["error"]
    dashboard.SERVER["on"] = True
    try:
        html = buying.page()
    finally:
        dashboard.SERVER["on"] = False
    secs = {x.split("<", 1)[0]: x for x in html.split("<h2>")[1:]}   # 섹션 제목 → 내용
    latest = secs["최근 실행 결과"]
    assert 'href="https://search.shopping.naver.com/search/all?query=AI%EC%95%88%EA%B2%BD%20%3C%EC%8A%A4%EB%A7%88%ED%8A%B8%3E"' in latest
    assert 'rel="noopener noreferrer">네이버 최저가 보기 ↗' in latest
    assert 'title="메모: 스토어A 배송비 포함">네이버 최저가 165,000원 · ' in latest and "권장가가 4% 높음" in latest
    assert "네이버 최저가 미입력" in latest and latest.count("최저가 적기") == 2      # 목록에 없는 'gone'은 링크만
    assert 'data-buywatch="naverlow" data-id="ai-glasses" data-price="165000"' in latest
    search = secs["직접 검색"]
    assert "query=SBTQ041\"" in search and "네이버 최저가 보기" in search and "최저가 적기" not in search
    old = (ba / "catalog" / "naver_prices.csv").read_text(encoding="utf-8-sig").replace(date.today().isoformat(), "2026-01-01")
    (ba / "catalog" / "naver_prices.csv").write_text(old, encoding="utf-8-sig")
    assert "다시 확인 필요 (" in buying.page()
    assert buying.watch_call("naver_low", ba, id="ai-glasses", price="", memo="")[0] == 200     # 빈 값 = 지우기
    assert "165,000원" not in buying.page()


def test_naver_low_absent_in_old_buyagent(ba):
    assert buying.load()["naver_low"] is None and "네이버 최저가" not in buying.page()


@pytest.mark.skipif(not (REAL_WEB.parent / "core" / "name_ko.py").is_file(), reason="buyagent core/name_ko.py 없음")
def test_name_ko_shown_and_background_fill_uses_real_buyagent(ba, monkeypatch):
    import time
    _copy_buyagent_code(ba)
    (ba / "reports").mkdir(exist_ok=True)
    (ba / "reports" / "latest.json").write_text(json.dumps({"ok": True, "markets": [], "items": [
        {"id": "watch", "name": "セイコー 腕時計 <SBTQ041>", "status": "ok"}]}, ensure_ascii=False), encoding="utf-8")
    _search_json(ba)
    sj = json.loads((ba / "reports" / "search.json").read_text(encoding="utf-8"))
    sj["items"] = [{"name": "SEIKO Watch SBTQ041", "status": "ok", "best_offer": {"price": 1, "currency": "USD"}}]
    (ba / "reports" / "search.json").write_text(json.dumps(sj, ensure_ascii=False), encoding="utf-8")
    (ba / "data").mkdir()
    (ba / "data" / "name_ko.json").write_text(json.dumps({"セイコー 腕時計 <SBTQ041>": "세이코 손목시계 <SBTQ041>",
                                                          "SEIKO Watch SBTQ041": "세이코 시계 SBTQ041"}, ensure_ascii=False), encoding="utf-8")
    html = buying.page()
    assert 'セイコー 腕時計 &lt;SBTQ041&gt;</a>' in html or 'セイコー 腕時計 &lt;SBTQ041&gt;' in html
    assert '<span class="muted">(세이코 손목시계 &lt;SBTQ041&gt;)</span>' in html                 # 최근 감시 결과 (이스케이프)
    assert '<span class="muted">(세이코 시계 SBTQ041)</span>' in html                             # 직접 검색 결과
    assert "(AI" not in html                                                                     # 풀이 없는 이름은 괄호 없음
    # 뒤에서 채우기: 키 없음 → 시작하지 않고 잠시 다시 안 봄 / 키 있음 → missing만 보내고 한 번 보낸 이름은 다시 안 보냄
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    buying._NAME_JOB.update(running=False, tried=set(), no_key_until=0.0, last=None)
    assert buying.start_name_fill(ba, ["Seiko Watch"])
    for _ in range(100):
        if not buying._NAME_JOB["running"]:
            break
        time.sleep(0.1)
    assert buying._NAME_JOB["last"] == {"ready": False} and buying._NAME_JOB["no_key_until"] > time.time()
    assert not buying.start_name_fill(ba, ["Seiko Watch"])                                       # 키 없음 → 60초 동안 안 봄
    buying._NAME_JOB.update(no_key_until=0.0, tried={"Seiko Watch"})
    assert not buying.start_name_fill(ba, ["Seiko Watch"])                                       # 이미 보낸 이름
