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
import re
import tomllib
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
           "latest_error": None, "search": None, "search_error": None, "history": [], "history_count": 0,
           "paused_since": None, "ship_warnings": []}
    if not out["exists"]:
        return out
    out["paused_since"] = paused_since(base)
    out["ship_warnings"] = ship_rate_warnings(base)
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


# buyagent web.py 함수를 buyagent 폴더에서 따로 띄운 파이썬으로 실행 (web.py가 buyagent의 core 패키지를 쓰므로
# trade_hub 서버 안으로 불러오지 않음). 입력은 stdin JSON, 결과는 마지막 줄 RESULT_JSON=[코드, 내용]
WATCH_SCRIPT = """
import json, sys
from pathlib import Path
import web
a = json.loads(sys.stdin.read())
base = Path.cwd()
if a["action"] == "add":
    r = web.add_watch(base, a["search"], a.get("name") or "", a.get("must"))
elif a["action"] == "toggle":
    r = web.toggle_watch(base, a["id"])
elif a["action"] == "pause":        # 감시 전체 멈춤/다시 시작 → core.pause.set_paused
    r = web.pause_request({"paused": a["paused"]}, base)
elif a["action"] == "schedule":     # 매시간 자동 실행 등록/해제 (작업 스케줄러)
    r = web.schedule_request({"on": a["on"]}, base)
elif a["action"] == "status":
    r = (200, {"paused_since": web.paused_since(base), "schedule": web.schedule_status()})
else:
    r = (200, {"must": web.suggest_must(a["query"])})
print("RESULT_JSON=" + json.dumps(list(r), ensure_ascii=False))
"""
WATCH_TIMEOUT = 30       # 초
_STATUS = {}             # 감시 상태 캐시 {"at": 시각, "base": 폴더, "value": dict} — 화면을 그릴 때마다 schtasks를 부르지 않게
STATUS_TTL = 30          # 초
_WATCH_LOCK = __import__("threading").Lock()   # 동시 클릭도 한 번에 하나씩 (products.csv 를 각자 읽고 쓰면 한 줄이 사라질 수 있음)


def watch_call(action, base=None, **payload):
    """buyagent web.py add_watch·toggle_watch(·suggest_must) → (HTTP 코드, dict).
    products.csv 열 순서·id 규칙·중복 거절·원자적 쓰기가 buyagent 자체 화면과 똑같음."""
    import subprocess
    import sys
    base = Path(base or buyagent_dir())
    if not (base / "web.py").is_file():
        raise RuntimeError(f"buyagent web.py가 없습니다: {base / 'web.py'}")
    try:
        with _WATCH_LOCK:
            r = subprocess.run([sys.executable, "-c", WATCH_SCRIPT], cwd=base, input=json.dumps({"action": action, **payload}),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=WATCH_TIMEOUT,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"buyagent 응답 없음 ({WATCH_TIMEOUT}초)")
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT_JSON="):
            code, out = json.loads(line.split("=", 1)[1])
            return int(code), out
    tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["출력 없음"]
    raise RuntimeError(f"buyagent web.py 실행 실패: {tail[0][:200]}")


def watch_status(base=None, fresh=False):
    """buyagent 감시 상태 {"paused_since": str|None, "schedule": {"registered": bool, "next": str}} — buyagent
    web.paused_since·schedule_status 그대로. 실패하면 None (화면은 멈춤 파일만 직접 읽어 보여 줌)."""
    import time
    base = Path(base or buyagent_dir())
    if not fresh and _STATUS.get("base") == str(base) and time.time() - _STATUS.get("at", 0) < STATUS_TTL:
        return _STATUS["value"]
    try:
        code, out = watch_call("status", base)
    except RuntimeError:
        return None
    if code != 200:
        return None
    _STATUS.update(at=time.time(), base=str(base), value=out)
    return out


def forget_status():
    _STATUS.clear()


def is_on(row):
    return (row.get("active") or "Y").strip().upper() not in ("N", "0", "FALSE")


def watching(products, keyword):
    """같은 검색어(keyword)로 이미 감시 목록에 있는 줄 (buyagent add_watch 중복 기준과 같음)."""
    keyword = (keyword or "").strip()
    return next((p for p in products if keyword and (p.get("keyword") or "").strip() == keyword), None)


def paused_since(base):
    """감시 전체 멈춤이면 멈춘 시각, 아니면 None — buyagent core/pause.py 와 같은 파일·규칙 (data/watch_paused.json).
    멈춘 동안 buyagent는 latest.json 을 갱신하지 않으므로 run_at 이 오래돼도 오류가 아님."""
    try:
        d = json.loads((Path(base) / "data" / "watch_paused.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return str(d.get("since") or "?") if isinstance(d, dict) else "?"


def ship_rate_warnings(base, today=None):
    """배대지 요금표 확인이 필요한 나라 → 문장 (buyagent core/shipping_rates.stale_messages 와 같은 기준). 읽기만 함."""
    try:
        with open(Path(base) / "config" / "settings.toml", "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, ValueError):
        return []
    sec = cfg.get("shipping_checked") or {}
    try:
        max_days = int(sec.get("max_days", 30))
    except (TypeError, ValueError):
        max_days = 30
    today = today or date.today()
    out = []
    for origin, v in sec.items():
        if origin == "max_days":
            continue
        name = (cfg.get("origins") or {}).get(origin, {}).get("name", origin)
        try:
            d = v if isinstance(v, date) else date.fromisoformat(str(v).strip()) if str(v).strip() else None
        except ValueError:
            d = None
        if d is None:
            out.append(f"{name} 배대지 요금 확인 기록 없음 (지금 값은 추정값)")
        elif (today - d).days > max_days:
            out.append(f"{name} 배대지 요금 확인한 지 {max_days}일 넘음 (마지막 확인 {d})")
    return out


def suggest_must(q):
    """감시 추가 때 '꼭 들어갈 단어' 기본값 — buyagent web.py suggest_must 와 같은 규칙
    (검색 때 쓴 필수 단어, 없으면 검색어 속 영문 대문자·숫자 코드, 예: SBTR|SBTQ041)"""
    must = [w for w in q.get("must_include") or [] if w]
    if not must:
        must = [w for w in (q.get("keyword") or "").split()
                if len(w) >= 3 and re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", w) and any(c.isalpha() for c in w)]
    return "|".join(must)


def cost_cell(it):
    """개당 원가 + 그 안에 들어간 배대지 비용·현지 배송 (buyagent web.py cost_cell 과 같은 문구)"""
    from trade_hub.dashboard import e, num
    cost = it.get("cost_krw")
    if not isinstance(cost, (int, float)):
        return num(cost, "{:,}원")
    fwd, prof, parts = it.get("forwarding_krw"), it.get("shipping_profile"), []
    if isinstance(fwd, (int, float)):
        parts.append(f"배대지 {fwd:,.0f}원 포함" + (f" ({prof})" if prof else "") if fwd else "배대지 없음 (직배송)")
    o = it.get("best_offer") or {}
    if o:
        if not o.get("shipping_known"):
            parts.append("현지배송 미확인")
        elif not o.get("shipping"):
            parts.append("현지배송 무료")
        else:
            parts.append(f"현지배송 {o.get('shipping')} {o.get('currency') or ''} 포함".replace("  ", " "))
    return f"{cost:,.0f}원" + (f'<div class="small muted">{e(" · ".join(parts))}</div>' if parts else "")


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
    elif data.get("paused_since"):
        state, label = "off", "감시 멈춤 중"
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
                     cost_cell(it), *cells,
                     clip(" / ".join(notes), 60)])
    sub = f'{e((latest.get("run_at") or "")[:16].replace("T", " "))} 실행' + (" · 예시 데이터(--demo)" if latest.get("demo") else "")
    if not latest.get("ok"):
        sub += f' · <span class="bad">실패: {e(latest.get("error") or "")}</span>'
    if data.get("paused_since"):
        sub += f' · <b>감시 멈춤 중</b> ({e(data["paused_since"])}부터 — 그동안 결과가 갱신되지 않는 것은 정상)'
    return section("최근 실행 결과", table(["상품", "판정", "최저가 판매처", "개당 원가", *[f"{m} 권장가" for m in markets], "경고·알림"],
                                         rows, "tight") if rows else '<p class="empty">결과 상품 없음</p>', sub)


LANG_BUTTONS = [   # (언어, 버튼 이름, 설명) — 번역만 함. 검색할 사이트는 사이트 메뉴가 정함
    ("en", "영어", "영어로 번역 (이베이용)"),
    ("ja", "일본어", "일본어로 번역 (라쿠텐용)"),
    ("zh", "중국어", "중국어로 번역 (중국(타오바오·1688·알리) 링크용)"),
]
# /api/buysearch 허용 값 = buyagent main.py SEARCH_MARKETS(--market)와 같은 값.
# aliexpress(알리 제휴 API·영어 검색)는 서버에 남겨 두지만 화면 메뉴에서는 뺌 — 알리 키를 받으면 다시 넣을지 정함 (buyagent 요청 2026-11-20)
BUY_MARKETS = {"rakuten": "라쿠텐(일본·일본어로 검색)", "aliexpress": "알리익스프레스(중국·영어로 검색)",
               "ebay": "이베이(미국·영어로 검색)"}
# 사이트 메뉴: 라쿠텐 / 이베이 / 중국. china = 자동 검색 없이 중국어 검색 링크만 (타오바오·1688·알리, 화면 JS buyLinks)
SITE_OPTIONS = [("rakuten", BUY_MARKETS["rakuten"]), ("ebay", BUY_MARKETS["ebay"]),
                ("china", "중국(타오바오·1688·알리 · 중국어 · 링크만)")]
SITE_LANG = {"rakuten": "ja", "aliexpress": "en", "ebay": "en", "china": "zh"}   # 언어 버튼 ↔ 사이트 맞춤 (화면 JS)
SITE_SHORT = {"rakuten": "라쿠텐", "aliexpress": "알리익스프레스", "ebay": "이베이"}


def _search_section(data):
    """buyagent --search 결과: 등록하지 않은 상품을 한 번 검색한 최저가 후보 (개당 원가 낮은 순)"""
    from trade_hub.dashboard import clip, e, num, section, table, web_link
    from trade_hub.dashboard import SERVER
    if SERVER["on"]:
        # 카테고리는 미리 고르지 않음: 배송비·최소 마진이 카테고리마다 달라 잘못 고르면 권장가가 크게 틀림
        opts = '<option value="">카테고리 선택</option>' + "".join(
            f'<option value="{e(k)}">{e(n)}</option>' for k, n in categories(data["dir"]))
        sites = "".join(f'<option value="{k}" data-lang="{SITE_LANG[k]}">{e(n)}</option>' for k, n in SITE_OPTIONS)
        langs = "".join(
            f'<button type="button" class="buylang" data-lang="{k}" title="{e(t)}">{e(n)}</button>'
            for k, n, t in LANG_BUTTONS)
        how = (f'<form class="buysearch" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0">'
               '<input name="query" required maxlength="100" autocomplete="off" '
               'placeholder="검색어 (예: 세이코 SBTQ041)" style="flex:1;min-width:220px">'
               f'<span class="buylangs" style="display:inline-flex;gap:2px">{langs}</span>'
               f'<select name="market" title="검색할 사이트 — 한글 검색어는 사이트 언어로 자동 번역">{sites}</select>'
               f'<select name="category" required title="배송비·최소 마진·관세·차단 규칙이 카테고리마다 다릅니다">{opts}</select>'
               f'<button type="submit">🔎 검색</button></form>'
               '<div class="buylinks small"></div>'
               '<p class="small muted"><b>사이트</b>를 고르고 검색하면 한글 검색어는 그 사이트 언어로 자동 번역됩니다 '
               '(라쿠텐 30초~1분 · 이베이는 buyagent에 API 키 필요). 언어 버튼은 번역 결과를 미리 보고 고치는 용도입니다 (1원 미만). '
               '<b>중국</b>은 자동 검색 없이 중국어로 바꾼 검색 링크(타오바오·1688·알리)만 보여 줍니다. '
               '<b>카테고리</b>는 꼭 맞게 고르세요 — 배송비·최소 마진이 달라 권장가가 크게 바뀝니다. '
               '모델명(예: SBTQ041)을 넣으면 그 단어가 들어간 상품만 남깁니다. 감시 목록·가격 이력·알림에는 영향 없습니다.</p>')
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
                     cost_cell(it), *cells, clip(" / ".join(it.get("warnings") or []), 60)])
    ko = q.get("keyword_original")                       # 한글로 검색했으면 buyagent가 번역 → 원래 검색어도 보여 줌
    site = SITE_SHORT.get(q.get("market") or "rakuten", q.get("market"))
    sub = (f'{e(site)} "{e(q.get("keyword") or "")}"' + (f' (← {e(ko)})' if ko else "") + ' · 카테고리'
           f' {e(q.get("category") or "")} · '
           f'{e((s.get("run_at") or "")[:16].replace("T", " "))} 검색 · 쇼핑몰 결과 {e(s.get("found", 0))}건')
    if not s.get("ok"):
        sub += f' · <span class="bad">실패: {e(s.get("error") or "")}</span>'
    watch = ""
    if SERVER["on"] and s.get("ok") and q.get("keyword") and q.get("category"):
        same = watching(data["products"], q.get("keyword"))
        if same:
            watch = (f'<p class="small">✔ 이미 감시 중 — ID <b>{e(same.get("id"))}</b>'
                     f'{"" if is_on(same) else " (꺼짐 · 아래 감시 상품에서 켜기)"}</p>')
        else:
            watch = (f'<p class="buywatch-add"><button type="button" class="btn primary" data-buywatch="add" '
                     f'data-name="{e(ko or q.get("keyword"))}">＋ 감시에 추가</button> '
                     f'<label class="small">꼭 들어갈 단어 <input name="must" value="{e(suggest_must(q))}" maxlength="160" '
                     'placeholder="예: SBTR|SBTQ041" title="상품명에 꼭 들어가야 할 단어, 여러 개는 | 로 구분 (5개까지). '
                     '비우면 시곗줄 같은 다른 상품이 최저가로 잡힐 수 있음" style="width:180px"></label> '
                     '<span class="small muted">이 검색 조건(검색어·카테고리·사이트·제외 단어)을 catalog/products.csv에 넣어 '
                     'buyagent 정기 감시(매시간)에 포함</span></p>')
    excl = s.get("excluded") or {}
    why = e(", ".join(f"{k}: {v}건" for k, v in excl.items()))
    note = (f'<p class="small muted">걸러낸 결과: {why}</p>' if excl and rows else "")
    if rows:
        body = table(["#", "판매처·상품명", "현지가", "개당 원가", *[f"{m} 권장가" for m in markets], "경고"], rows, "tight")
    elif not s.get("ok") and s.get("error"):         # 예: 알리·이베이 키 없음 → buyagent 문구 그대로
        body = f'<p class="bad">{e(s["error"])}</p>'
    else:                                             # 0건 → 어떤 규칙에 몇 건이 걸렸는지 (왜 안 나오는지)
        body = ('<p class="empty">조건에 맞는 재고 있는 판매처 없음'
                + (f'<br><span class="small">걸러낸 결과 — {why}</span>' if excl else "") + '</p>')
    return section("직접 검색", how + watch + body + note, sub)


def _products_section(data):
    from trade_hub.dashboard import clip, e, section, table
    from trade_hub.dashboard import SERVER
    rows = []
    for p in data["products"]:
        on = is_on(p)
        memo = p.get("memo") or ""
        tag = '<span class="pick">2번에서 가져옴</span> ' if FROM_SOURCING in memo else ""
        state = "활성" if on else '<span class="muted">꺼짐</span>'
        if SERVER["on"] and p.get("id"):            # buyagent web.py toggle 과 같은 동작 (active Y ↔ N)
            state += (f' <button type="button" class="btn" data-buywatch="toggle" data-id="{e(p.get("id"))}">'
                      f'{"끄기" if on else "켜기"}</button>')
        rows.append([e(p.get("id")), _product_link(p.get("name") or p.get("id"), _hub_id(p)), e(p.get("category")),
                     e(p.get("sources") or "기본"), state, tag + clip(memo, 50)])
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


def _watch_line(data):
    """buyagent 화면과 같은 한 줄 상태: ● 감시 중 / ⏸ 멈춤 · 자동 실행 · 마지막 감시 (+ 서버 모드면 버튼)"""
    from trade_hub.dashboard import SERVER, e
    st = watch_status(data["dir"]) if SERVER["on"] else None
    since = st["paused_since"] if st else data.get("paused_since")
    if since:
        state = (f'<span class="warn">⏸ 감시 전체 멈춤 중 ({e(since)}부터) — 정기 실행이 와도 조회·알림을 안 해요 '
                 '(그동안 최근 실행 결과가 갱신되지 않는 것은 정상)</span>')
    else:
        state = '<span class="ok">● 감시 중</span>'
    sched = ""
    if st:
        s = st.get("schedule") or {}
        sched = (f' · 자동 실행: <b>매시간</b>' + (f' (다음 {e(s.get("next"))})' if s.get("next") else "") if s.get("registered")
                 else ' · 자동 실행: <span class="warn">등록 안 됨</span> (정기 감시·디스코드 알림을 받으려면 켜 주세요)')
    latest = data.get("latest") or {}
    last = latest.get("run_at") or ""
    last_txt = (f' · 마지막 감시 {e(last[:16].replace("T", " "))}' + (" (예시 데이터)" if latest.get("demo") else "")
                if last else " · 감시 실행 기록 없음")
    btns = ""
    if SERVER["on"]:
        btns = (f' <button type="button" class="btn" data-buywatch="pause" data-paused="{"0" if since else "1"}">'
                f'{"▶ 감시 다시 시작" if since else "⏸ 감시 전체 멈춤"}</button>')
        if st:
            on = (st.get("schedule") or {}).get("registered")
            btns += (f' <button type="button" class="btn" data-buywatch="schedule" data-on="{"0" if on else "1"}">'
                     f'{"자동 실행 끄기 (등록 해제)" if on else "⏱ 매시간 자동 실행 켜기"}</button>')
    return f'<p class="small">{state}{sched}{last_txt}{btns}</p>'


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
    notes = _watch_line(data)
    if data.get("ship_warnings"):
        notes += ('<p class="small warn">⚠ ' + " / ".join(e(w) for w in data["ship_warnings"]) +
                  " → buyagent 화면의 '배대지 요금'에서 확인·저장 (대시보드는 읽기만)</p>")
    return intro + notes + _latest_section(data) + _search_section(data) + _products_section(data) + _prices_section(data) + _history_section(data)
