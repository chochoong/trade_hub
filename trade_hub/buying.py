"""
대시보드 '구매대행' 화면 — buyagent(구매대행 모니터) 폴더를 읽기만 해서 보여 준다.

읽는 것 (모두 읽기 전용, 없으면 빈 칸으로 안내):
  catalog/products.csv       감시 상품 (hub_product_id 열이 있으면 trade_hub 품목 카드로 링크)
  catalog/manual_prices.csv  타오바오·알리 등 직접 입력한 가격 (빈 배송비 = 미확인, 7일 지난 가격 = 다시 확인)
  reports/latest.json        최근 실행 결과 (buyagent가 실행마다 덮어씀, 형식은 buyagent 요청문의 schema 1)
  reports/search.json        직접 검색 결과 (python main.py --search "검색어" --category 카테고리)
  history.db                 가격 이력

폴더 위치: 환경변수 BUYAGENT_DIR, 없으면 trade_hub 옆의 ../buyagent/buyagent
"""

import csv
import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from trade_hub import db

STALE_DAYS = 7          # buyagent 수동 가격 '다시 확인' 기준과 같게
FROM_SOURCING = "2번 공급처 발굴에서 가져옴"
STATUS = {"ok": ("판매 가능", "ok"), "blocked": ("차단", "bad"), "no_offer": ("판매처 없음", "warn"),
          "error": ("오류", "bad")}


def buyagent_dir():
    return Path(os.environ.get("BUYAGENT_DIR") or db.PKG_DIR.parent.parent / "buyagent" / "buyagent")


def categories(base=None):
    """buyagent config/categories.toml 에서 판매 가능한 카테고리 [(키, 이름)] (직접 검색 고르기용)."""
    import tomllib
    try:
        with open(Path(base or buyagent_dir()) / "config" / "categories.toml", "rb") as f:
            cats = tomllib.load(f).get("categories", {})
    except (OSError, ValueError):
        return []
    return [(k, v.get("name") or k) for k, v in cats.items() if not k.startswith("_") and v.get("allowed")]


def _csv(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def load(base=None):
    """buyagent 폴더에서 화면에 필요한 것만 읽는다. 폴더가 없으면 {'exists': False}."""
    base = Path(base or buyagent_dir())
    out = {"dir": base, "exists": (base / "catalog").is_dir(), "products": [], "prices": [], "latest": None,
           "latest_error": None, "search": None, "search_error": None, "history": [], "history_count": 0}
    if not out["exists"]:
        return out
    out["products"] = _csv(base / "catalog" / "products.csv")
    out["prices"] = _csv(base / "catalog" / "manual_prices.csv")
    latest = base / "reports" / "latest.json"
    if latest.exists():
        try:
            out["latest"] = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out["latest_error"] = f"{type(e).__name__}: {e}"
    search = base / "reports" / "search.json"
    if search.exists():
        try:
            out["search"] = json.loads(search.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out["search_error"] = f"{type(e).__name__}: {e}"
    hist = base / "history.db"
    if hist.exists():
        try:
            conn = sqlite3.connect(f"file:{hist.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                out["history_count"] = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
                out["history"] = conn.execute("SELECT * FROM history ORDER BY ts DESC LIMIT 20").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return out


def _hub_id(row):
    v = (row.get("hub_product_id") or "").strip()
    return int(v) if v.isdigit() else None


def _product_link(name, hub_id):
    from trade_hub.dashboard import e
    return (f'<a href="#p{hub_id}" title="trade_hub 품목 #{hub_id} 카드로">{e(name)}</a> <span class="muted small">#{hub_id}</span>'
            if hub_id else e(name))


def _age_days(checked, today=None):
    try:
        return ((today or date.today()) - date.fromisoformat((checked or "").strip()[:10])).days
    except ValueError:
        return None


def nav_item(data=None):
    """파이프라인 메뉴 칸 (2 공급처 옆)."""
    from trade_hub.dashboard import e
    data = data or load()
    active = sum(1 for p in data["products"] if (p.get("active") or "Y").strip().upper() not in ("N", "0", "FALSE"))
    latest = data["latest"] or {}
    if not data["exists"]:
        state, label = "off", "폴더 없음"
    elif latest:
        state = "ok" if latest.get("ok") else "bad"
        label = ("성공" if latest.get("ok") else "실패") + f' · {(latest.get("run_at") or "")[5:10].replace("-", "/")}'
    else:
        state, label = "off", "실행 결과 없음"
    return (f'<a class="step {state}" href="#buy" title="구매대행 (buyagent)"><span class="no">2-R</span><b>구매대행</b>'
            f'<span class="small"><i class="dot"></i>{e(label)}</span>'
            f'<span class="metric">{active if data["exists"] else "–"}<small> 감시</small></span></a>')


def _latest_section(data):
    from trade_hub.dashboard import clip, e, num, section, table, web_link
    latest = data["latest"]
    if data["latest_error"]:
        return section("최근 실행 결과", f'<p class="bad">reports/latest.json을 읽지 못했습니다: {e(data["latest_error"])}</p>')
    if not latest:
        return section("최근 실행 결과", '<p class="empty">아직 실행 결과가 없습니다 — buyagent 폴더에서 '
                                       '<code>python main.py</code>를 실행하면 최저가·원가·권장가가 여기에 나옵니다.</p>')
    markets = latest.get("markets") or []
    rows = []
    for it in latest.get("items") or []:
        label, cls = STATUS.get(it.get("status"), (it.get("status") or "–", "muted"))
        why = it.get("blocked_reason")
        o = it.get("best_offer") or {}
        price = ""
        if o:
            ship = ('<span class="moq unk" title="현지 배송비를 아직 확인하지 않음">배송비 미확인</span>'
                    if o.get("shipping_known") is False else e(f'배송 {o.get("shipping")}') if o.get("shipping") else "")
            price = (f'{web_link(o.get("url"), o.get("shop") or o.get("source") or "판매처") or e(o.get("shop") or "")}'
                     f'<div>{e(o.get("price"))} {e(o.get("currency"))} {ship}</div>')
        quotes = {q.get("market"): q for q in it.get("quotes") or []}
        cells = [f'{q.get("recommended_price"):,}원<div class="small muted">마진 {q.get("margin"):,}원</div>'
                 if (q := quotes.get(m)) and isinstance(q.get("recommended_price"), (int, float))
                 and isinstance(q.get("margin"), (int, float)) else '<span class="muted">–</span>' for m in markets]
        notes = (it.get("warnings") or []) + (it.get("events") or [])
        rows.append([_product_link(it.get("name") or it.get("id"), it.get("hub_product_id")),
                     f'<span class="{cls}">{e(label)}</span>' + (f'<div class="small">{clip(why, 50)}</div>' if why else ""),
                     price or '<span class="muted">–</span>',
                     num(it.get("cost_krw"), "{:,}원"), *cells,
                     clip(" / ".join(notes), 60)])
    sub = f'{e((latest.get("run_at") or "")[:16].replace("T", " "))} 실행' + (" · 예시 데이터(--demo)" if latest.get("demo") else "")
    if not latest.get("ok"):
        sub += f' · <span class="bad">실패: {e(latest.get("error") or "")}</span>'
    return section("최근 실행 결과", table(["상품", "판정", "최저가 판매처", "개당 원가", *[f"{m} 권장가" for m in markets], "경고·알림"],
                                         rows, "tight") if rows else '<p class="empty">결과 상품 없음</p>', sub)


LANG_BUTTONS = [   # (언어, 버튼 이름, 설명) — 서버 LANG_MARKET 과 맞춤
    ("en", "영어", "영어로 번역 → 알리익스프레스 자동 검색"),
    ("ja", "일본어", "일본어로 번역 → 라쿠텐 자동 검색"),
    ("zh", "중국어", "중국어로 번역 → 타오바오·1688 검색 링크"),
]


def _search_section(data):
    """buyagent --search 결과: 등록하지 않은 상품을 한 번 검색한 최저가 후보 (개당 원가 낮은 순)"""
    from trade_hub.dashboard import clip, e, num, section, table, web_link
    from trade_hub.dashboard import SERVER
    if SERVER["on"]:
        last = (data.get("search") or {}).get("query") or {}
        opts = "".join(f'<option value="{e(k)}"{" selected" if k == (last.get("category") or "watch") else ""}>{e(n)}</option>'
                       for k, n in categories(data["dir"]))
        langs = "".join(
            f'<button type="button" class="buylang{" on" if k == "ja" else ""}" data-lang="{k}" title="{e(t)}">{e(n)}</button>'
            for k, n, t in LANG_BUTTONS)
        how = (f'<form class="buysearch" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0">'
               '<input type="hidden" name="lang" value="ja">'
               '<input name="query" required maxlength="100" autocomplete="off" '
               'placeholder="검색어 (예: 세이코 SBTQ041) → 언어 버튼을 누르면 번역" style="flex:1;min-width:220px">'
               f'<span class="buylangs" style="display:inline-flex;gap:2px">{langs}</span>'
               f'<select name="category" title="관세율·마진 기준·차단 규칙이 카테고리마다 다릅니다">{opts}</select>'
               f'<button type="submit">🔎 검색</button></form>'
               '<div class="buylinks small"></div>'
               '<p class="small muted">언어 버튼을 누르면 한글 검색어를 그 언어로 바꿔 검색창에 넣습니다 (고쳐 써도 됩니다 · 1원 미만). '
               '<b>일본어</b> = 라쿠텐 자동 검색 (30초~1분) · <b>영어</b> = 알리익스프레스 자동 검색 (buyagent에 API 키 필요) · '
               '<b>중국어</b> = 타오바오·1688 검색 링크 (자동 검색 없음, 직접 확인). '
               '모델명(예: SBTQ041)을 넣으면 그 단어가 들어간 상품만 남겨 부속품을 걸러 냅니다. '
               '감시 목록·가격 이력·알림에는 영향 없습니다.</p>')
    else:
        how = ('<p class="small muted">검색창은 <code>trade_hub serve</code>로 열면 보입니다. 또는 buyagent 폴더에서 '
               '<code>python main.py --search "セイコー SBTQ041" --category watch</code></p>')
    if data.get("search_error"):
        return section("직접 검색", f'<p class="bad">reports/search.json을 읽지 못했습니다: {e(data["search_error"])}</p>')
    s = data.get("search")
    if not s:
        return section("직접 검색", how + '<p class="empty">아직 직접 검색한 결과가 없습니다.</p>')
    q = s.get("query") or {}
    markets = s.get("markets") or []
    rows = []
    for i, it in enumerate(s.get("items") or [], 1):
        if it.get("status") == "blocked":
            rows.append([str(i), e(it.get("name")), f'<span class="bad">차단</span><div class="small">{clip(it.get("blocked_reason"), 60)}</div>',
                         "", *["" for _ in markets], ""])
            continue
        o = it.get("best_offer") or {}
        ship = ('<span class="moq unk" title="현지 배송비를 아직 확인하지 않음">배송비 미확인</span>'
                if o.get("shipping_known") is False else "")
        shop = web_link(o.get("url"), o.get("shop") or "판매처") or e(o.get("shop") or "")
        quotes = {x.get("market"): x for x in it.get("quotes") or []}
        cells = [f'{x.get("recommended_price"):,}원<div class="small muted">마진 {x.get("margin"):,}원</div>'
                 if (x := quotes.get(m)) and isinstance(x.get("recommended_price"), (int, float))
                 and isinstance(x.get("margin"), (int, float)) else '<span class="muted">–</span>' for m in markets]
        rows.append([str(i), f'{shop}<div class="small">{clip(it.get("name"), 70)}</div>',
                     f'{num(o.get("price"), "{:,.0f}" if o.get("currency") in ("JPY", "KRW") else "{:,.2f}")} '
                     f'{e(o.get("currency"))} {ship}',
                     num(it.get("cost_krw"), "{:,}원"), *cells, clip(" / ".join(it.get("warnings") or []), 60)])
    ko = q.get("keyword_original")                       # 한글로 검색했으면 buyagent가 번역 → 원래 검색어도 보여 줌
    site = {"rakuten": "라쿠텐", "aliexpress": "알리익스프레스"}.get(q.get("market") or "rakuten", q.get("market"))
    sub = (f'{e(site)} "{e(q.get("keyword") or "")}"' + (f' (← {e(ko)})' if ko else "") + ' · 카테고리'
           f' {e(q.get("category") or "")} · '
           f'{e((s.get("run_at") or "")[:16].replace("T", " "))} 검색 · 쇼핑몰 결과 {e(s.get("found", 0))}건')
    if not s.get("ok"):
        sub += f' · <span class="bad">실패: {e(s.get("error") or "")}</span>'
    excl = s.get("excluded") or {}
    note = (f'<p class="small muted">걸러낸 결과: {e(", ".join(f"{k} {v}건" for k, v in excl.items()))}</p>' if excl else "")
    body = (table(["#", "판매처·상품명", "현지가", "개당 원가", *[f"{m} 권장가" for m in markets], "경고"], rows, "tight")
            if rows else '<p class="empty">조건에 맞는 재고 있는 판매처 없음</p>')
    return section("직접 검색", how + body + note, sub)


def _products_section(data):
    from trade_hub.dashboard import clip, e, section, table
    rows = []
    for p in data["products"]:
        on = (p.get("active") or "Y").strip().upper() not in ("N", "0", "FALSE")
        memo = p.get("memo") or ""
        tag = '<span class="pick">2번에서 가져옴</span> ' if FROM_SOURCING in memo else ""
        rows.append([e(p.get("id")), _product_link(p.get("name") or p.get("id"), _hub_id(p)), e(p.get("category")),
                     e(p.get("sources") or "기본"), "활성" if on else '<span class="muted">꺼짐</span>',
                     tag + clip(memo, 50)])
    body = table(["ID", "상품", "카테고리", "판매처 소스", "상태", "메모"], rows, "tight") if rows else '<p class="empty">없음</p>'
    return section("감시 상품", body, f'{len(rows)}개 · catalog/products.csv')


def _prices_section(data, today=None):
    from trade_hub.dashboard import clip, e, section, table, web_link
    rows = []
    for r in data["prices"]:
        age = _age_days(r.get("checked"), today)
        checked = e(r.get("checked") or "")
        if age is None:
            checked = '<span class="moq unk">날짜 없음</span>'
        elif age > STALE_DAYS:
            checked += f' <span class="moq over" title="{age}일 전 확인">다시 확인</span>'
        ship = (r.get("shipping") or "").strip()
        ship_html = '<span class="moq unk" title="빈 칸 = 아직 확인 안 함">미확인</span>' if not ship else e(ship)
        stock = "있음" if (r.get("in_stock") or "Y").strip().upper() not in ("N", "0", "FALSE") else '<span class="bad">품절</span>'
        rows.append([e(r.get("id")), web_link(r.get("url"), r.get("shop") or "판매처") or e(r.get("shop") or ""),
                     f'{e(r.get("price"))} {e(r.get("currency"))}', ship_html, stock, checked, clip(r.get("memo"), 50)])
    body = (table(["상품 ID", "판매처", "가격", "현지 배송비", "재고", "확인 날짜", "메모"], rows, "tight")
            if rows else '<p class="empty">없음</p>')
    return section("수동 가격", body, f'{len(rows)}줄 · catalog/manual_prices.csv · {STALE_DAYS}일 지나면 다시 확인')


def _history_section(data):
    from trade_hub.dashboard import e, num, section, table
    if not data["history_count"]:
        return section("가격 이력", '<p class="empty">아직 기록 없음 (buyagent를 실행하면 쌓입니다)</p>')
    rows = [[e(h["ts"]), e(h["pid"]), "있음" if h["in_stock"] else "품절", f'{e(h["total"])} {e(h["currency"])}',
             e(h["shop"]), num(h["cost_krw"], "{:,.0f}원")] for h in data["history"]]
    return section("가격 이력", table(["시각", "상품", "재고", "가격", "판매처", "원가"], rows, "tight"),
                   f'전체 {data["history_count"]}건 중 최근 {len(rows)}건')


def page(data=None):
    from trade_hub.dashboard import e
    data = data or load()
    roles = ["해외 쇼핑몰에서 팔아도 되는 정품만 골라 최저가 찾기 (판매 가능·차단·경고 판단)",
             "원가(관부가세·배대지·수수료)와 쿠팡·스마트스토어 권장가 계산, 가격·재고 변동 알림"]
    if not data["exists"]:
        state = f'<span class="muted">buyagent 폴더를 찾지 못했습니다: {e(data["dir"])} (환경변수 BUYAGENT_DIR로 지정)</span>'
    else:
        state = f'<span class="muted">{e(data["dir"])} · 읽기 전용 (목록·가격 수정은 buyagent의 CSV에서)</span>'
    intro = (f'<section class="intro"><div class="intro-no">2-R</div><div><h2>구매대행 (buyagent)</h2>'
             f'<ul>{"".join(f"<li>{e(x)}</li>" for x in roles)}</ul><p class="small">{state}</p></div></section>')
    if not data["exists"]:
        return intro
    return intro + _latest_section(data) + _search_section(data) + _products_section(data) + _prices_section(data) + _history_section(data)
