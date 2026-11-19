"""
공용 DB → 대시보드 HTML 한 장 (읽기 전용).

  trade_hub dashboard              # data/dashboard.html을 만들고 브라우저로 연다
  trade_hub dashboard --no-open    # 파일만 만든다 (정기 실행용)
  trade_hub dashboard --out x.html

DB에는 아무것도 쓰지 않는다. 다시 실행하면 최신 상태로 다시 만든다.
맨 위 파이프라인(1~8번 에이전트)을 누르면 그 에이전트 페이지로 이동한다 (#a1 ~ #a8, 전체는 #home).
"""

import json
from datetime import datetime
from html import escape
from pathlib import Path

from trade_hub import buying, db

STAGE_LABEL = {
    "candidate": "후보", "sourcing": "공급처 발굴", "negotiating": "협상", "sampling": "샘플",
    "ordered": "발주", "shipping": "운송·통관", "selling": "판매", "closed": "종료", "dropped": "중단",
}
AGENT_LABEL = {
    "discover": "1 시장조사", "sourcing": "2 공급처", "negotiation": "3 협상", "documents": "4 서류",
    "compliance": "5 통관·규제", "costing": "6 원가", "sales": "7 판매", "finance": "8 재무", "supervisor": "슈퍼바이저",
}
TASK_KIND = {"send_message": "메일 발송", "review_quote": "견적 검토", "payment": "결제", "document": "서류",
             "decision": "결정", "other": "기타"}


# --- 작은 도우미 -------------------------------------------------------------
def e(v):
    return "" if v is None else escape(str(v))


def num(v, fmt="{:,}"):
    return "–" if v is None else fmt.format(v)


def pct(v):
    if v is None:
        return '<span class="muted">–</span>'
    cls = "up" if v > 0 else "down" if v < 0 else ""
    return f'<span class="{cls}">{v:+d}%</span>'


def search_links(p):
    """품목 이미지 검색 링크 (사진이 없어도 어떤 물건인지 바로 확인)."""
    from urllib.parse import quote
    try:
        ko = (json.loads(p["keywords_ko_json"]) or [None])[0] if p["keywords_ko_json"] else None
    except ValueError:
        ko = None
    ko = ko or p["name"]
    en = p["search_keyword_en"] or ko
    links = [("네이버쇼핑", f"https://search.shopping.naver.com/search/all?query={quote(ko)}"),
             ("구글 이미지", f"https://www.google.com/search?tbm=isch&q={quote(ko)}"),
             ("알리바바", f"https://www.alibaba.com/trade/search?SearchText={quote(en)}")]
    if p["ref_url"]:
        links.insert(0, ("참고 페이지", p["ref_url"]))
    return '<span class="links">' + " · ".join(
        f'<a href="{e(u)}" target="_blank" rel="noopener">{lbl}</a>' for lbl, u in links) + "</span>"


SERVER = {"on": False}   # trade_hub serve로 그릴 때 True → 로컬 파일은 /file?p=… 로, 조사·선정 버튼 표시


def file_href(path):
    """로컬 파일 링크. 파일로 열면 file:// URI, 서버로 열면 서버가 대신 보내 주는 /file 주소."""
    from urllib.parse import quote
    path = Path(path).resolve()
    return "/file?p=" + quote(str(path)) if SERVER["on"] else path.as_uri()


def img_url(value):
    """image_url 값 → <img src> (http(s)는 그대로, 상대경로는 DB 폴더 기준 로컬 파일). 없으면 None."""
    if not db.resolve_image(value):
        return None
    if str(value).startswith(("http://", "https://")):
        return str(value)
    path = Path(value)
    return file_href(path if path.is_absolute() else db.default_path().parent / path)


_REFS = {}   # 이번 화면에 쓸 참고 이미지 {품목번호: {thumbnail, link, title}} — render()가 채움, DB에는 저장 안 함
NO_IMAGE = '<span class="thumb none {size}" title="이미지 없음"></span>'


def photo(p, size=""):
    """확정 사진 → 참고 이미지('참고' 표시) → '이미지 없음' 아이콘 순서."""
    src = img_url(p["image_url"])
    if src:
        return (f'<a class="thumb {size}" href="{e(src)}" target="_blank" rel="noopener" title="확정 사진">'
                f'<img src="{e(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" '
                'onerror="this.parentNode.classList.add(&quot;none&quot;);this.remove()"></a>')
    ref = _REFS.get(p["id"])
    if ref:
        return (f'<a class="thumb ref {size}" href="{e(ref["link"])}" target="_blank" rel="noopener" '
                f'title="참고 이미지 (네이버 이미지 검색 결과 — 실제 상품 사진 아님): {e(ref["title"])}">'
                f'<img src="{e(ref["thumbnail"])}" alt="" loading="lazy" referrerpolicy="no-referrer" '
                'onerror="this.parentNode.classList.add(&quot;none&quot;);this.remove()"><em>참고</em></a>')
    return NO_IMAGE.format(size=size)


def supplier_photo(value):
    src = img_url(value)
    if not src:
        return ""
    return (f'<a class="thumb sm" href="{e(src)}" target="_blank" rel="noopener" title="공급사 제공 사진">'
            f'<img src="{e(src)}" alt="" loading="lazy" referrerpolicy="no-referrer"></a>')


def _search_word(p):
    try:
        ko = (json.loads(p["keywords_ko_json"]) or [None])[0] if p["keywords_ko_json"] else None
    except ValueError:
        ko = None
    return ko or p["name"]


def load_reference_images(conn, log=None):
    """확정 사진이 없는 품목(중단 제외)만 네이버 이미지 검색 → _REFS (화면 전용)."""
    from trade_hub import naver
    rows = conn.execute("SELECT id, name, keywords_ko_json, image_url FROM products WHERE stage != 'dropped'").fetchall()
    need = {r["id"]: _search_word(r) for r in rows if not db.resolve_image(r["image_url"])}
    _REFS.clear()
    _REFS.update({k: v for k, v in naver.reference_images(need, log).items() if v})
    return len(_REFS), len(need)


def link(path, label):
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return f'<span class="muted" title="{e(path)}">{e(label)} (파일 없음)</span>'
    return f'<a href="{e(file_href(p))}" target="_blank">{e(label)}</a>'


def web_link(url, label):
    """웹에서 수집한 URL은 http(s)만 링크로. 그 밖의 주소(javascript: 등)는 누르면 코드가 실행될 수 있어
    링크를 걸지 않고 글자로만 보여 주고, 마우스를 올리면 원래 주소가 보인다."""
    if not url:
        return ""
    if not str(url).strip().lower().startswith(("http://", "https://")):
        return (f'<span class="nolink" title="{e("링크로 열지 않음 (http/https 주소가 아님): " + str(url))}">'
                f'{e(label)}</span>')
    return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(label)}</a>'


def channel_strip(conn, product_id, purchase_mode=None):
    """공급처 표 위에 '어느 사이트에서 살까' 순위: 사이트명(검색 링크) + 점수 + 소량 단가. 마우스를 올리면 장단점.
    purchase_mode를 주면 그 구매 방식의 최신 평가만."""
    chans = db.latest_channels(conn, product_id, purchase_mode)
    if not chans:
        return ""
    items = []
    for c in chans:
        name = web_link(c["search_url"], c["site"]) or e(c["site"])
        price = (f'<span class="small">{e(c["typical_price"])}</span>' if c["typical_price"]
                 else '<span class="small muted">단가 미확인</span>')
        tip = " · ".join(x for x in (c["summary"], f'최소 {c["min_qty"]}' if c["min_qty"] else "",
                                      "웹 평가 못 함 (사이트 고유 점수만)" if c["assessed"] == 0 else "") if x)
        items.append(f'<li class="ch{" top" if c["rank"] == 1 else ""}{" unassessed" if c["assessed"] == 0 else ""}"'
                     f' title="{e(tip)}"><span class="ch-rank">{e(c["rank"])}</span>{name}'
                     f' <b class="ch-score">{num(c["total"], "{:g}점")}</b> {price}</li>')
    mode = chans[0]["purchase_mode"]
    return (f'<div class="channels"><span class="small muted">구매 사이트 순위'
            f'{" (" + e(mode) + " 기준)" if mode else ""}</span><ol>{"".join(items)}</ol></div>')


MOQ_BADGE = {"ok": ("소량 OK", "ok"), "over": ("MOQ 초과", "over"), "unknown": ("MOQ 미확인", "unk"),
             "no_limit": ("OEM · 제한 없음", "unk")}


def moq_badge(s):
    """MOQ 원문의 첫 숫자를 첫 구매 최대 수량(max_order_qty, 없으면 구매 방식 기본값·50)과 비교한 배지."""
    fit, qty, limit = db.moq_fit(s["moq"], s["ps_mode"], s["ps_max_qty"])
    label, cls = MOQ_BADGE[fit]
    basis = ("기준: 제한 없음 (OEM)" if limit is None
             else f"기준: {limit}개 이하" + (f" ({s['ps_mode']})" if s["ps_mode"] else "")
             + ("" if s["ps_max_qty"] is not None else " · 기본값"))
    title = basis + (f" · MOQ {qty}개" if qty is not None else "")
    return f'<span class="moq {cls}" title="{e(title)}">{label}</span>'


def supplier_score(s):
    """v4 score·grade 칸이 있으면 그것, 없으면 예전 저장분의 trust_reason "<점수>점 (<등급>) — 요약" 앞부분."""
    if s["ps_score"] is not None:
        return f'{s["ps_score"]:g}점' + (f' ({s["ps_grade"]})' if s["ps_grade"] else "")
    return (s["trust_reason"] or "").split(" — ")[0]


def clip(text, n=90):
    """긴 글은 앞부분만 보이고 펼치면 전체."""
    if not text:
        return ""
    text = str(text)
    if len(text) <= n:
        return e(text)
    return f'<details><summary>{e(text[:n])}…</summary><div class="long">{e(text)}</div></details>'


def table(headers, rows, cls=""):
    if not rows:
        return '<p class="empty">없음</p>'
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def section(title, inner, sub=""):
    sub = f'<span class="sub">{sub}</span>' if sub else ""
    return f"<section><h2>{title}{sub}</h2>{inner}</section>"


# --- 섹션 -------------------------------------------------------------------
def pending_tasks(conn):
    rows = conn.execute("""SELECT t.*, p.name AS product FROM tasks t LEFT JOIN products p ON p.id = t.product_id
                           WHERE t.status = 'pending' ORDER BY t.priority, t.created_at""").fetchall()
    out = []
    for r in rows:
        detail = r["detail"] or ""
        if detail and Path(detail).suffix in (".md", ".html"):
            html = Path(detail).with_name("report.html")
            detail_html = link(html if html.exists() else detail, "리포트 열기")
        else:
            detail_html = clip(detail)
        out.append([f'<span class="pri p{r["priority"]}">{r["priority"]}</span>', f"#{r['id']}",
                    e(TASK_KIND.get(r["kind"], r["kind"])), e(r["title"]), detail_html, e(r["created_at"][:16])])
    return table(["우선", "번호", "종류", "할 일", "자료", "생성"], out), len(rows)


def candidate_rows(conn):
    rows = conn.execute("""
        SELECT p.*, s.total, s.demand, s.margin, s.competition, s.regulation, s.logistics, s.est_fob_usd,
               s.naver_json, s.score_notes, r.report_dir
        FROM products p
        LEFT JOIN product_scores s ON s.id = (SELECT MAX(id) FROM product_scores WHERE product_id = p.id)
        LEFT JOIN agent_runs r ON r.id = s.run_id
        WHERE p.stage = 'candidate' ORDER BY s.total DESC, p.id DESC""").fetchall()
    out = []
    for r in rows:
        nv = json.loads(r["naver_json"]) if r["naver_json"] else {}
        sub = " · ".join(f"{k} {num(r[c], '{:g}')}" for k, c in
                         (("수요", "demand"), ("마진", "margin"), ("경쟁", "competition"), ("규제", "regulation"),
                          ("물류", "logistics")))
        report = link(Path(r["report_dir"]) / "candidates.html", "점수표") if r["report_dir"] else ""
        out.append([f'<span class="anchor" id="prod-{r["id"]}"></span>#{r["id"]}'
                    + ('<div class="small muted">직접 조사</div>' if r["origin"] == "manual" else ""),
                    f'<b class="score">{num(r["total"])}</b>',
                    f'<div class="pcell">{photo(r)}<div>{e(r["name"])}<div class="muted small">{e(r["category"])} · {sub}</div>'
                    f'<div class="small">{search_links(r)}</div></div></div>',
                    num(nv.get("volume_idx"), "{:g}") if nv else '<span class="muted">–</span>',
                    pct(nv.get("growth_3m")) if nv else "", pct(nv.get("yoy")) if nv else "",
                    num(r["est_fob_usd"], "${:g}"), num(r["est_retail_krw"], "{:,}원"),
                    clip(r["reason"], 60), report]
                   + ([actions(r)] if SERVER["on"] else []))
    heads = ["번호", "점수", "품목", "검색량", "3개월", "전년比", "FOB", "판매가", "선정 이유", "리포트"]
    return table(heads + (["작업"] if SERVER["on"] else []), out), len(rows)


MODE_BUTTONS = (("retail", "구매대행"), ("small_batch", "소량 사입"))
COUNTRIES = (("China", "중국"), ("Japan", "일본"))          # 2번 sourcing_agent --country 값


def country_select():
    """[공급처 찾기] 버튼 옆 국가 선택 (같은 묶음의 버튼이 이 값을 함께 보냄)."""
    opts = "".join(f'<option value="{k}">{n}</option>' for k, n in COUNTRIES)
    return f'<select class="country" title="어느 나라 공급처를 찾을지">{opts}</select>'


def actions(p):
    """서버 모드 버튼: 후보 → [구매대행용 찾기]·[소량 사입용 찾기](2번 단계로 넘기고 그 방식으로 2번 실행), [다시 조사]."""
    name = e(p["name"])
    btns = "".join(f'<button type="button" class="btn primary" data-act="select" data-id="{p["id"]}" data-name="{name}" '
                   f'data-mode="{mode}" data-mode-label="{label}">{label}용 찾기</button>' for mode, label in MODE_BUTTONS)
    return (f'<div class="acts">{country_select()}{btns}'
            f'<button type="button" class="btn" data-act="research" data-q="{name}">다시 조사</button></div>')


def sourcing_button(p, modes_found):
    """공급처 발굴 카드: 구매 방식별 찾기. 이미 찾은 방식은 '다시 찾기'(보조 버튼), 아직이면 '찾기'(강조)."""
    if not SERVER["on"] or p["stage"] != "sourcing":
        return ""
    name = e(p["name"])
    return ' <span class="acts">' + country_select() + "".join(
        f'<button type="button" class="btn{"" if mode in modes_found else " primary"}" data-act="select" data-id="{p["id"]}" '
        f'data-name="{name}" data-mode="{mode}" data-mode-label="{label}">{label}용 {"다시 " if mode in modes_found else ""}찾기</button>'
        for mode, label in MODE_BUTTONS) + "</span>"


def product_index(conn):
    """검색창용 품목 목록 (페이지 안에 JSON으로 넣음)."""
    out = []
    for r in conn.execute("SELECT id, name, stage, category, search_keyword_en, keywords_ko_json FROM products ORDER BY id DESC"):
        try:
            kw = " ".join(json.loads(r["keywords_ko_json"]) or []) if r["keywords_ko_json"] else ""
        except ValueError:
            kw = ""
        out.append({"id": r["id"], "name": r["name"], "stage": STAGE_LABEL.get(r["stage"], r["stage"]),
                    "kw": " ".join(x for x in (kw, r["search_keyword_en"], r["category"]) if x)})
    # <script> 안에 넣으므로 <, >, & 를 유니코드 이스케이프 (JSON 값은 그대로, HTML로는 해석 안 됨)
    return (json.dumps(out, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


# 공급처 목록을 나눌 구매 방식 (product_suppliers.purchase_mode). 대시보드 버튼은 retail·small_batch만 실행
PURCHASE_GROUPS = [("retail", "구매대행"), ("small_batch", "소량 사입"), ("oem", "OEM"), (None, "구매 방식 미기록")]
SUPPLIER_HEADS = ["", "공급처", "신뢰도·점수", "상태", "MOQ", "단가", "연락처", "위험 요소"]


def sourcing_report(conn, product_id, mode):
    """이 구매 방식으로 가장 최근에 돈 2번 실행의 리포트와 리포트 안 순위 {연결 id: 순위}.
    mode가 None(예전 저장분)이면 구매 방식과 관계없이 가장 최근 실행."""
    report = conn.execute("""
        SELECT t.detail, t.run_id FROM tasks t JOIN agent_runs r ON r.id = t.run_id
        WHERE t.product_id = ? AND r.agent = 'sourcing'
          AND (? IS NULL OR json_extract(r.args_json, '$.sourcing_type') = ?)
        ORDER BY t.id DESC LIMIT 1""", (product_id, mode, mode)).fetchone()
    report_html = None
    if report and report["detail"]:
        path = Path(report["detail"])
        html = path.with_name("report.html")      # 예전 실행은 report.md를 가리킴 → 같은 폴더의 HTML 우선
        report_html = html if html.exists() else (path if path.exists() else None)
    # 리포트 안 순위 = 그 실행에서 저장된 순서 → 이름을 누르면 리포트의 해당 업체가 펼쳐진 채로 열림
    ranks = {}
    if report_html and report_html.suffix == ".html":
        ids = [r[0] for r in conn.execute("SELECT id FROM product_suppliers WHERE product_id = ? AND run_id = ? ORDER BY id",
                                          (product_id, report["run_id"]))]
        ranks = {lid: n for n, lid in enumerate(ids, 1)}
    return report_html, ranks


def supplier_row(s, report_html, ranks):
    rank = ranks.get(s["link_id"])
    name = (f'<a href="{e(file_href(report_html))}#s{rank}" target="_blank" title="리포트에서 상세 보기">{e(s["company"])}</a>'
            if rank else e(s["company"]))
    site = web_link(s["url"], "업체 페이지 ↗")
    badge = f'<div class="small">{clip(s["platform_badge"], 40)}</div>' if s["platform_badge"] else ""
    reason = (s["trust_reason"] or "").split(" — ", 1)
    reason = reason[1] if len(reason) == 2 else (s["trust_reason"] if s["ps_score"] is not None else "")
    contact = "".join(f'<div>{x}</div>' for x in (e(s["contact_email"]), clip(s["contact_other"], 40)) if x)
    return [
        '<span class="pick">추천</span>' if s["is_top_pick"] else "",
        f'<div class="pcell">{supplier_photo(s["sup_image"])}<div>{name}<div class="muted small">{e(s["country"])} · {e(s["type"])}'
        f'{" · " + num(s["years_in_business"], "{:g}년") if s["years_in_business"] else ""}'
        f'{" · " + site if site else ""}</div>{badge}</div></div>',
        f'<span class="trust t{e(s["trust"])}">{e(s["trust"] or "–")}</span> <span class="small">{e(supplier_score(s))}</span>'
        + (f'<div class="small">{clip(reason, 40)}</div>' if reason else ""),
        e(s["status"]), f'{moq_badge(s)}<div>{clip(s["moq"], 30)}</div>', clip(s["price_note"] or num(s["unit_price_usd"], "${:g}"), 30),
        contact or '<span class="muted">–</span>',
        clip(s["red_flags"], 50),
    ]


def products_in_progress(conn):
    prods = conn.execute(f"""SELECT * FROM products WHERE stage NOT IN ('candidate', 'dropped')
                             ORDER BY {db.stage_order_sql()} DESC, id""").fetchall()
    if not prods:
        return '<p class="empty">진행 중인 품목 없음 — <code>trade_hub select &lt;번호&gt;</code>로 후보를 넘기세요</p>', 0
    cards = []
    for p in prods:
        sups = conn.execute("""
            SELECT s.*, ps.id AS link_id, ps.image_url AS sup_image, ps.status, ps.is_top_pick, ps.moq, ps.price_note, ps.unit_price_usd, ps.run_id,
                   ps.score AS ps_score, ps.grade AS ps_grade, ps.purchase_mode AS ps_mode, ps.max_order_qty AS ps_max_qty
            FROM product_suppliers ps JOIN suppliers s ON s.id = ps.supplier_id
            WHERE ps.product_id = ? ORDER BY ps.is_top_pick DESC, ps.id""", (p["id"],)).fetchall()
        groups = {}                               # 구매 방식별로 나눠 보여 줌 (retail = 구매대행, small_batch = 소량 사입)
        for s in sups:
            groups.setdefault(s["ps_mode"], []).append(s)
        blocks = []
        for mode, label in PURCHASE_GROUPS:
            if mode not in groups:
                continue
            report_html, ranks = sourcing_report(conn, p["id"], mode)
            rep = link(report_html, "리포트") if report_html else ""
            rows = [supplier_row(s, report_html, ranks) for s in groups[mode]]
            blocks.append(f"""<div class="ps-group"><h3 class="ps-mode">{e(label)} <span class="muted small">{len(rows)}곳</span>
    <span class="small">{rep}</span></h3>
  {channel_strip(conn, p["id"], mode)}
  {table(SUPPLIER_HEADS, rows, "tight")}</div>""")
        if not blocks:                            # 공급처가 아직 없음 → 사이트 순위만 (있으면)
            blocks.append(channel_strip(conn, p["id"]) + table(SUPPLIER_HEADS, [], "tight"))
        last = conn.execute("SELECT note, at FROM product_events WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                            (p["id"],)).fetchone()
        cards.append(f"""<div class="card" id="prod-{p["id"]}">
  <div class="card-head"><span class="badge">{e(STAGE_LABEL.get(p["stage"], p["stage"]))}</span>
    {photo(p, "big")}<div><b>#{p["id"]} {e(p["name"])}</b> <span class="muted small">{e(last["note"] if last else "")} · {e(last["at"][:16] if last else "")}</span>
    <div class="small">{search_links(p)}</div></div>
    <span class="right">{sourcing_button(p, set(groups))}</span></div>
  {"".join(blocks)}
</div>""")
    return "".join(cards), len(prods)


FRIENDLY_ERRORS = ("SourcingError: ",)   # 에이전트가 한국어 안내 문구를 담아 올리는 예외 → 클래스 이름은 떼고 보여 줌


def run_error(text):
    """agent_runs.error ("예외이름: 메시지") 화면 표시용. 안내용 예외만 접두어를 떼고, 나머지는 그대로."""
    text = text or ""
    for prefix in FRIENDLY_ERRORS:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def runs(conn, agent=None):
    rows = (conn.execute("SELECT * FROM agent_runs WHERE agent = ? ORDER BY id DESC LIMIT 15", (agent,)) if agent
            else conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 15")).fetchall()
    out = []
    for r in rows:
        dur = ""
        if r["finished_at"]:
            sec = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
            dur = f"{sec / 60:.0f}분" if sec >= 60 else f"{sec:.0f}초"
        status = {"succeeded": '<span class="ok">성공</span>', "failed": '<span class="bad">실패</span>',
                  "running": '<span class="warn">실행 중</span>'}.get(r["status"], e(r["status"]))
        rep = r["report_dir"]
        rep_html = ""
        if rep:
            html = next((Path(rep) / n for n in ("candidates.html", "report.html", "report.md") if (Path(rep) / n).exists()), None)
            rep_html = link(html or rep, Path(rep).name)
        out.append([f"#{r['id']}", e(AGENT_LABEL.get(r["agent"], r["agent"])), status, e(r["started_at"][:16]), dur,
                    e(r["model"]), rep_html, clip(run_error(r["error"]), 60)])
    return table(["실행", "에이전트", "결과", "시작", "소요", "모델", "결과물", "오류"], out)


def briefing(conn):
    b = conn.execute("SELECT * FROM briefings ORDER BY id DESC LIMIT 1").fetchone()
    if not b:
        return '<p class="empty">브리핑 없음 — <code>python supervisor.py review</code></p>'
    alerts = json.loads(b["alerts_json"]) if b["alerts_json"] else []
    al = "".join(f'<li class="{e(a.get("level"))}">{e(a.get("text"))}</li>' for a in alerts if isinstance(a, dict))
    return (f'<p class="muted small">{e(b["created_at"])}</p>' + (f'<ul class="alerts">{al}</ul>' if al else "")
            + f'<div class="brief">{e(b["summary"])}</div>')


def dropped(conn):
    rows = conn.execute("""SELECT p.id, p.name, (SELECT note FROM product_events WHERE product_id = p.id
                           ORDER BY id DESC LIMIT 1) AS note FROM products p WHERE stage = 'dropped' ORDER BY id DESC""").fetchall()
    if not rows:
        return ""
    items = "".join(f"<li>#{r['id']} {e(r['name'])} <span class='muted'>— {e(r['note'])}</span></li>" for r in rows)
    return f"<section><details><summary><h2 class='inline'>중단한 품목 ({len(rows)})</h2></summary><ul>{items}</ul></details></section>"


# --- 페이지 -----------------------------------------------------------------
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--accent-soft:#e8efff;
--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--pick:#7c3aed}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--fg:#e5e7eb;--muted:#9aa3b2;--line:#2a303b;
--accent:#7aa2ff;--accent-soft:#1d2a4a;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--pick:#c4a5ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif}
main{max-width:1280px;margin:0 auto;padding:8px 16px 64px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 16px;margin-bottom:10px}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.top-in{max-width:1280px;margin:0 auto;padding:14px 16px 12px}
.pipe-nav{display:flex;align-items:stretch;gap:2px;overflow-x:auto;padding-bottom:2px}
.pipe-nav .arrow{align-self:center;color:var(--muted);padding:0 1px}
.pipe-nav .step{flex:1 0 108px;display:flex;flex-direction:column;gap:1px;text-decoration:none;color:var(--fg);
background:var(--card);border:1px solid var(--line);border-radius:10px;padding:7px 9px}
.pipe-nav .step:hover{border-color:var(--accent)}
.pipe-nav .step.cur{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent);background:var(--accent-soft)}
.pipe-nav .no{font-size:11px;font-weight:700;color:var(--muted)}.pipe-nav b{font-size:13px;line-height:1.25}
.pipe-nav .metric{font-size:18px;font-weight:700;color:var(--accent)}.pipe-nav .metric small{font-size:11px;font-weight:400;color:var(--muted)}
.pipe-nav .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px;background:var(--muted);vertical-align:middle}
.step.ok .dot{background:var(--ok)}.step.bad .dot{background:var(--bad)}.step.run .dot{background:var(--warn)}
.step.off{opacity:.62}.step.off.cur{opacity:1}.step.home{flex-basis:84px}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{border:1px solid var(--line);border-radius:999px;padding:3px 12px;color:var(--muted)}
.buylang.on{border-color:var(--accent);background:var(--accent-soft);font-weight:700}
.chip.on{border-color:var(--accent);color:var(--fg);background:var(--accent-soft)}.chip b{color:var(--accent)}
.search{position:relative;flex:1 1 320px;max-width:520px}
.sbox{display:flex}
.search input{flex:1;min-width:0;font:inherit;padding:7px 12px;border:1px solid var(--line);border-right:0;
border-radius:8px 0 0 8px;background:var(--card);color:var(--fg)}
.search input:focus{outline:2px solid var(--accent);outline-offset:-2px}
.sgo{flex:none;display:flex;align-items:center;justify-content:center;width:42px;border:1px solid var(--accent);
border-radius:0 8px 8px 0;background:var(--accent);color:#fff;cursor:pointer}
.sgo:hover{filter:brightness(1.1)}.sgo:focus-visible{outline:2px solid var(--fg);outline-offset:2px}
.results{position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:10;background:var(--card);border:1px solid var(--line);
border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.15);overflow:hidden}
.hit{display:block;width:100%;text-align:left;padding:8px 12px;border:0;border-bottom:1px solid var(--line);background:none;
color:var(--fg);font:inherit;text-decoration:none;cursor:pointer}.hit:last-child{border-bottom:0}
a.hit:hover,.hit.new:hover{background:var(--accent-soft)}.hit.new{color:var(--accent);font-weight:600}
.btn{font:inherit;font-size:12px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);
color:var(--fg);cursor:pointer;white-space:nowrap}.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.acts{display:flex;flex-direction:column;gap:4px}
@keyframes flash{from{background:var(--accent-soft)}to{background:transparent}}
.flash,.flash td{animation:flash 2.5s ease-out}
.jobs{position:fixed;right:16px;bottom:16px;z-index:20;width:min(460px,calc(100vw - 32px));max-height:60vh;overflow:auto;
background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.2);padding:8px 12px}
.job{position:relative;padding:6px 26px 6px 0;border-bottom:1px solid var(--line)}.job:last-child{border-bottom:0}
.jobs .x{border:0;background:none;color:var(--muted);cursor:pointer;font:inherit;font-size:12px;padding:2px 6px;border-radius:6px}
.jobs .x:hover{background:var(--accent-soft);color:var(--fg)}.job>.x{position:absolute;top:4px;right:0}
.jobs-head{display:flex;justify-content:space-between;align-items:center;padding-bottom:4px;border-bottom:1px solid var(--line)}
.job pre{margin:4px 0 0;font-size:11px;white-space:pre-wrap;color:var(--muted);max-height:96px;overflow:auto}
.job.failed b{color:var(--bad)}.job.done b{color:var(--ok)}
.intro{display:flex;gap:16px;align-items:flex-start}.intro h2{margin-bottom:6px}.intro ul{margin:0 0 6px;padding-left:18px}
.intro-no{flex:none;width:44px;height:44px;border-radius:12px;background:var(--accent-soft);color:var(--accent);
font-size:22px;font-weight:800;display:flex;align-items:center;justify-content:center}
h1{font-size:22px;margin:0}h2{font-size:16px;margin:0 0 12px}h2.inline{display:inline;margin:0}
.sub{font-weight:400;color:var(--muted);font-size:13px;margin-left:8px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0}
.muted{color:var(--muted)}.small{font-size:12px}.empty{color:var(--muted);margin:0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}tr:last-child td{border-bottom:0}
table.tight td{padding:6px 8px}
.score{font-size:16px;color:var(--accent)}.up{color:var(--ok)}.down{color:var(--bad)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.pri{display:inline-block;width:20px;height:20px;border-radius:50%;text-align:center;font-size:12px;line-height:20px;color:#fff}
.p1{background:var(--bad)}.p2{background:var(--warn)}.p3{background:var(--muted)}
.pick{color:var(--pick);font-weight:600;font-size:12px;white-space:nowrap}
.trust{font-weight:700}.t상{color:var(--ok)}.t중{color:var(--warn)}.t하{color:var(--bad)}
.moq{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;border:1px solid;white-space:nowrap}
.moq.ok{color:var(--ok)}.moq.over{color:var(--bad)}.moq.unk{color:var(--muted)}
.channels{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin:8px 0}
.channels ol{display:flex;flex-wrap:wrap;gap:6px;margin:0;padding:0;list-style:none}
.ch{display:flex;align-items:baseline;gap:6px;border:1px solid var(--line);border-radius:8px;padding:3px 10px;cursor:default}
.ch.top{border-color:var(--accent);background:var(--accent-soft)}.ch.unassessed{opacity:.6}
.ch-rank{font-size:11px;color:var(--muted)}.ch-score{color:var(--accent)}
.ps-group+.ps-group{margin-top:14px}.ps-mode{font-size:14px;margin:10px 0 4px;display:flex;gap:8px;align-items:baseline}
.nolink{text-decoration:underline dotted;cursor:help}
.card{border:1px solid var(--line);border-radius:10px;padding:12px;margin:10px 0}
.card-head{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin-bottom:8px}.right{margin-left:auto}
.pcell{display:flex;gap:10px;align-items:flex-start;min-width:260px}
.thumb{flex:none;display:block;width:56px;height:56px;border-radius:8px;overflow:hidden;background:var(--line)}
.thumb.big{width:72px;height:72px}.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb.none{background:var(--line) center/22px no-repeat url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%239aa3b2' stroke-width='1.8'%3E%3Crect x='3' y='5' width='18' height='14' rx='2'/%3E%3Ccircle cx='9' cy='10' r='2'/%3E%3Cpath d='m21 16-5-5-8 8'/%3E%3C/svg%3E")}
.links a{white-space:nowrap}
.thumb{position:relative}.thumb.sm{width:36px;height:36px;border-radius:6px}
.thumb.ref img{opacity:.85}.thumb.ref em{position:absolute;left:0;bottom:0;right:0;font:600 10px/16px sans-serif;
font-style:normal;text-align:center;color:#fff;background:rgba(0,0,0,.55)}
.badge{background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600}
details summary{cursor:pointer}.long{white-space:pre-wrap;margin-top:4px;max-width:520px}
.brief{white-space:pre-wrap}.alerts li.warn{color:var(--warn)}.alerts li.critical,.alerts li.error{color:var(--bad)}
a{color:var(--accent)}code{background:var(--accent-soft);padding:1px 5px;border-radius:4px}
.neg{border-top:1px solid var(--line);padding:10px 0 6px}.neg:first-of-type{border-top:0}
.neg-head{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;margin-bottom:6px}
.acts.row{flex-direction:row;flex-wrap:wrap;align-items:flex-start;gap:6px}
.msg{border-left:3px solid var(--line);padding:4px 10px;margin:6px 0 6px 4px}
.msg.out{border-left-color:var(--accent)}.msg.in{border-left-color:var(--ok)}
.msg.discarded{opacity:.5}.msg.draft{background:var(--accent-soft)}
.msg .acts{margin-top:4px}
.mst{font-weight:600}.mst.draft,.mst.approved{color:var(--warn)}.mst.sent{color:var(--accent)}.mst.received{color:var(--ok)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px;margin-top:4px}
.cols pre{margin:0;white-space:pre-wrap;font:inherit;font-size:13px;background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:8px;max-height:320px;overflow:auto}
form.negf.inline{display:inline-flex;flex-wrap:wrap;gap:6px}
form.negf.stack{display:flex;flex-direction:column;gap:6px;margin-top:6px;padding:10px;min-width:min(420px,80vw);
border:1px solid var(--line);border-radius:10px;background:var(--card)}
form.negf input:not([type=hidden]),form.negf select,form.negf textarea{font:inherit;font-size:13px;padding:5px 8px;
border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
form.negf textarea{min-height:120px;resize:vertical}
.frow{display:flex;flex-wrap:wrap;gap:8px}.frow label{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:4px}
summary.btn{list-style:none;display:inline-block}summary.btn::-webkit-details-marker{display:none}
"""


# --- 에이전트별 페이지 ---------------------------------------------------------
# (agent 이름, 번호, 이름, 역할) — 파이프라인 순서
AGENT_PAGES = [
    ("discover", 1, "시장조사·아이템 발굴", ["해외 트렌드, 경쟁 상품 가격, 검색량, 리뷰를 수집·분석",
                                       "\"수요는 있는데 공급이 적은 품목\"을 정기적으로 리포트"]),
    ("sourcing", 2, "공급처 발굴", ["알리바바·1688·글로벌소싱 등에서 후보 업체 리스트업",
                                 "업체 신뢰도(업력·인증·거래 후기) 1차 검증"]),
    ("negotiation", 3, "커뮤니케이션·협상", ["다국어 이메일·인콰이어리 작성, 번역",
                                         "견적 요청(RFQ)·샘플 요청·가격 협상 문안 초안 (발송은 본인이 검토 후)"]),
    ("documents", 4, "무역서류", ["PI·CI·Packing List·계약서 초안", "인코텀즈(FOB·CIF·DDP 등) 조건별 비용·책임 정리"]),
    ("compliance", 5, "통관·규제 체크", ["HS코드 후보, 관세율·FTA 원산지 혜택 확인",
                                     "KC·식약처·전파인증 등 수입 요건 체크리스트"]),
    ("costing", 6, "원가·수익 계산", ["제품가+운임+관세+부가세+수수료 랜딩 코스트", "환율 변동 반영 마진 시뮬레이션"]),
    ("sales", 7, "판매·마케팅", ["스마트스토어·쿠팡 상세페이지·상품명 SEO·광고 문구", "고객 문의 응대 초안"]),
    ("finance", 8, "재무·관리", ["주문·결제·선적 일정 트래킹", "매출·비용 장부, 부가세 신고 자료 준비"]),
]


def _scalar(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def last_run(conn, agent):
    return conn.execute("SELECT * FROM agent_runs WHERE agent = ? ORDER BY id DESC LIMIT 1", (agent,)).fetchone()


def agent_metric(conn, agent):
    """파이프라인 칸에 보일 핵심 숫자 (값, 단위 설명)."""
    q = {
        "discover": ("SELECT COUNT(*) FROM products WHERE stage = 'candidate'", "후보"),
        "sourcing": ("SELECT COUNT(DISTINCT product_id) FROM product_suppliers", "품목 조사"),
        "negotiation": ("SELECT COUNT(*) FROM messages WHERE direction = 'outbound' AND status = 'draft'", "초안"),
        "documents": ("SELECT COUNT(*) FROM tasks WHERE kind = 'document' AND status = 'pending'", "서류 할 일"),
        "compliance": ("SELECT COUNT(*) FROM products WHERE hs_code IS NOT NULL", "HS 확인"),
        "costing": ("SELECT COUNT(*) FROM product_suppliers WHERE est_landed_krw IS NOT NULL", "원가 계산"),
        "sales": ("SELECT COUNT(*) FROM sales", "판매 기록"),
        "finance": ("SELECT COUNT(*) FROM orders", "발주"),
    }[agent]
    try:
        return _scalar(conn, q[0]), q[1]
    except Exception:          # 테이블 컬럼이 다른 예전 DB 등 — 숫자 없이 표시
        return None, q[1]


def nav(conn):
    pending = _scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'pending'")
    items = [f'<a class="step home" href="#home"><span class="no">◎</span><b>전체</b>'
             f'<span class="small">할 일 {pending}건</span></a>']
    for agent, no, name, _ in AGENT_PAGES:
        r = last_run(conn, agent)
        val, unit = agent_metric(conn, agent)
        state = ("off", "미연결") if not r else {"failed": ("bad", "실패"), "running": ("run", "실행 중")}.get(
            r["status"], ("ok", "연결됨"))
        when = f' · {r["started_at"][5:10].replace("-", "/")}' if r else ""
        items.append(f'<a class="step {state[0]}" href="#a{no}" title="{e(name)}">'
                     f'<span class="no">{no}</span><b>{e(name)}</b>'
                     f'<span class="small"><i class="dot"></i>{state[1]}{when}</span>'
                     f'<span class="metric">{"–" if val is None else val}<small> {e(unit)}</small></span></a>')
        if agent == "sourcing":                 # 2번 옆 갈래: 구매대행 (buyagent 폴더를 읽기만)
            items.append(buying.nav_item())
    return '<nav class="pipe-nav">' + '<span class="arrow">›</span>'.join(items) + "</nav>"


def stage_overview(conn):
    counts = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, COUNT(*) n FROM products GROUP BY stage")}
    steps = "".join(f'<div class="chip{" on" if counts.get(s) else ""}"><b>{counts.get(s, 0)}</b> {STAGE_LABEL[s]}</div>'
                    for s in db.STAGES)
    return (f'<div class="chips">{steps}</div>'
            f'<p class="muted small">중단 {counts.get("dropped", 0)}개 · 전체 {sum(counts.values())}개</p>')


def generic_rows(conn, table_name, limit=50):
    """뒤 단계 테이블을 있는 그대로 보여 준다 (해당 에이전트가 생기면 전용 화면으로 바꿀 것)."""
    rows = conn.execute(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT {int(limit)}").fetchall()
    if not rows:
        return '<p class="empty">없음</p>'
    cols = [c for c in rows[0].keys() if not c.endswith("_json")]
    return table([e(c) for c in cols], [[clip(r[c], 60) for c in cols] for r in rows], "tight")


def tasks_of(conn, kinds):
    rows = conn.execute(f"""SELECT * FROM tasks WHERE status = 'pending' AND kind IN ({', '.join('?' * len(kinds))})
                            ORDER BY priority, created_at""", kinds).fetchall()
    return table(["우선", "번호", "할 일", "자료"],
                 [[f'<span class="pri p{r["priority"]}">{r["priority"]}</span>', f"#{r['id']}", e(r["title"]),
                   clip(r["detail"], 60)] for r in rows])


def compliance_rows(conn):
    rows = conn.execute(f"""SELECT * FROM products WHERE stage NOT IN ('candidate', 'dropped')
                            ORDER BY {db.stage_order_sql()} DESC, id""").fetchall()
    return table(["품목", "단계", "HS 코드", "관세율", "FTA", "인증·규제"],
                 [[f'#{r["id"]} {e(r["name"])}', e(STAGE_LABEL.get(r["stage"], r["stage"])),
                   e(r["hs_code"]) or '<span class="muted">미확인</span>',
                   "–" if r["duty_rate"] is None else f'{r["duty_rate"] * 100:g}%', clip(r["fta_note"], 40),
                   clip(r["regulations"], 70)] for r in rows])


def costing_rows(conn):
    rows = conn.execute("""SELECT p.id, p.name, s.company, ps.unit_price_usd, ps.est_landed_krw, ps.est_margin_pct, p.est_retail_krw
                           FROM product_suppliers ps JOIN products p ON p.id = ps.product_id JOIN suppliers s ON s.id = ps.supplier_id
                           WHERE ps.est_landed_krw IS NOT NULL ORDER BY ps.est_margin_pct DESC""").fetchall()
    return table(["품목", "공급처", "FOB", "개당 원가", "판매가", "마진"],
                 [[f'#{r["id"]} {e(r["name"])}', e(r["company"]), num(r["unit_price_usd"], "${:g}"),
                   num(r["est_landed_krw"], "{:,}원"), num(r["est_retail_krw"], "{:,}원"),
                   num(r["est_margin_pct"], "{:.0f}%")] for r in rows])


def pnl_rows(conn):
    rows = conn.execute("SELECT * FROM v_product_pnl WHERE revenue_krw > 0 OR cost_krw > 0").fetchall()
    return table(["품목", "단계", "매출(부가세 제외)", "비용", "이익"],
                 [[e(r["name"]), e(STAGE_LABEL.get(r["stage"], r["stage"])), num(r["revenue_ex_vat_krw"], "{:,.0f}원"),
                   num(r["cost_krw"], "{:,.0f}원"), num(r["profit_krw"], "{:,.0f}원")] for r in rows])


def moved_on(conn):
    """후보 목록에서 빠진(다음 단계로 넘어간) 품목 → 지금 있는 카드로 가는 링크. 최근에 넘어간 순."""
    rows = conn.execute(f"""
        SELECT p.id, p.name, p.stage,
               (SELECT MAX(at) FROM product_events e WHERE e.product_id = p.id AND e.from_stage = 'candidate') AS moved_at
        FROM products p WHERE p.stage NOT IN ('candidate', 'dropped')
        ORDER BY moved_at IS NULL, moved_at DESC, p.id DESC""").fetchall()
    if not rows:
        return ""
    out = [[f'#{r["id"]}', f'<a href="#p{r["id"]}">{e(r["name"])}</a>',
            f'<span class="badge">{e(STAGE_LABEL.get(r["stage"], r["stage"]))}</span>',
            e((r["moved_at"] or "")[:16].replace("T", " ")) or '<span class="muted">–</span>'] for r in rows]
    return section("다음 단계로 넘어간 품목", table(["번호", "품목", "지금 단계", "넘어간 시각"], out, "tight"),
                   f"{len(rows)}개 · 후보 목록에서 빠진 품목 · 이름을 누르면 지금 있는 화면으로 이동")


# --- 3번 협상 (negotiation_agent) -----------------------------------------------------
MSG_STATUS = {"draft": "초안", "approved": "검토 완료", "sent": "보냄", "discarded": "버림", "received": "받음"}
MSG_KIND = {"rfq": "견적 요청", "follow_up": "후속 메일", "negotiation": "역제안", "sample": "샘플 요청",
            "order": "발주", "other": "메일"}
PS_STATUS = {"candidate": "연락 전", "contacted": "연락함", "quoted": "견적 받음", "sampling": "샘플",
             "selected": "선정", "rejected": "제외"}
LANG_OPTS = '<option value="en">영어</option><option value="zh">중국어</option>'
SENT_Q = "이 메일을 직접 보냈나요? (에이전트는 보내지 않습니다)"
MARK_BUTTONS = {   # 현재 상태 → (바꿀 상태, 버튼, 확인 문구)
    "draft": [("approved", "검토 완료", ""), ("sent", "발송 완료로 표시", SENT_Q), ("discarded", "버림", "이 초안을 버릴까요?")],
    "approved": [("sent", "발송 완료로 표시", SENT_Q), ("draft", "초안으로", ""), ("discarded", "버림", "이 초안을 버릴까요?")],
}
COST = "3번 에이전트를 실행할까요? (수십 초 · API 비용)"


def neg_form(action, hidden, inner, confirm=COST, cls=""):
    """서버 모드 폼 → JS가 /api/neg 로 보낸다 (action·hidden 값 + 입력칸)."""
    fields = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in {"action": action, **hidden}.items())
    return f'<form class="negf {cls}" data-confirm="{e(confirm)}">{fields}{inner}</form>'


def mark_form(m):
    btns = "".join(f'<button class="btn{" primary" if s == "sent" else ""}" name="status" value="{s}" '
                   f'data-confirm="{e(c)}">{label}</button>' for s, label, c in MARK_BUTTONS.get(m["status"], []))
    return neg_form("mark", {"message_id": m["id"]}, btns, confirm="", cls="inline") if btns else ""


def _analysis(m):
    if not m["analysis_json"]:
        return {}
    try:
        v = json.loads(m["analysis_json"])
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def message_item(m, waiting=None, can_follow=False):
    out = m["direction"] == "outbound"
    a = _analysis(m)
    when = (m["sent_at"] or m["created_at"] or "")[:16].replace("T", " ")
    head = (f'{"📤" if out else "📥"} <b>#{m["id"]}</b> {e(MSG_KIND.get(m["kind"], m["kind"]) if out else "받은 답장")} · '
            f'<span class="mst {e(m["status"])}">{e(MSG_STATUS.get(m["status"], m["status"]))}</span> · '
            f'<span class="muted small">{e(when)}</span> {e(m["subject"])}')
    copy_text = f'{m["subject"]}\n\n{m["body"]}' if out and m["subject"] else m["body"]
    is_draft = out and m["status"] in ("draft", "approved")      # 검토할 초안은 펼쳐서 바로 보이게
    texts = (f'<details{" open" if is_draft else ""}><summary class="small">원문 · 한국어{" 번역" if m["translation"] else ""}</summary>'
             f'<div class="cols"><pre id="msg-{m["id"]}">{e(copy_text)}</pre>'
             f'<pre>{e(m["translation"] or "(번역 없음)")}</pre></div></details>')
    extra = []
    if out and a.get("strategy_ko"):
        meta = ""
        if a.get("tone"):
            meta = f' <span class="muted">({e(a["tone"])}' + (f' · ${a["counter_price_usd"]:g}' if a.get("counter_price_usd") else "") + ")</span>"
        extra.append(f'<div class="small">💡 {e(a["strategy_ko"])}{meta}</div>')
    if not out:
        if a.get("error"):
            extra.append(f'<div class="small bad">분석 실패: {clip(a["error"], 80)}</div>')
        elif not a:
            extra.append('<div class="small warn">분석 대기 (슈퍼바이저가 자동 처리)</div>')
        if a.get("summary_ko"):
            extra.append(f'<div class="small">{e(a["summary_ko"])}</div>')
        if a.get("open_questions_ko"):
            extra.append('<div class="small muted">확인할 점: ' + " · ".join(e(q) for q in a["open_questions_ko"]) + "</div>")
    if waiting is not None:
        extra.append(f'<div class="small warn">⏳ {waiting}일째 답 없음</div>')
    acts = []
    if SERVER["on"]:
        if out and m["status"] in ("draft", "approved"):
            acts.append(f'<button type="button" class="btn" data-copy="msg-{m["id"]}">원문 복사</button>' + mark_form(m))
            if m["external_draft_id"]:       # Gmail 임시보관함에 있음 → Gmail에서 보내면 동기화 때 '보냄'으로 바뀜
                acts.append('<span class="small ok">📨 Gmail 임시보관함에 있음 — Gmail에서 직접 보내세요</span>')
            else:
                acts.append(neg_form("gmail_draft", {"message_id": m["id"]},
                                     '<button class="btn">Gmail 임시보관함에 넣기</button>',
                                     confirm="이 초안을 Gmail 임시보관함에 넣을까요? (보내지는 않습니다)", cls="inline"))
        if can_follow:
            target = m["reply_to_id"] if m["kind"] == "follow_up" and m["reply_to_id"] else m["id"]
            acts.append(neg_form("followup", {"message_id": target}, '<button class="btn">후속 메일 초안</button>', cls="inline"))
        if not out and a.get("error"):
            acts.append(neg_form("analyze", {"message_id": m["id"]}, '<button class="btn">다시 분석</button>', cls="inline"))
    elif out and m["status"] in ("draft", "approved"):
        acts.append(f'<code class="small">python -m negotiation_agent mark {m["id"]} sent</code>')
    return (f'<div class="msg {"out" if out else "in"} {e(m["status"])}"><div>{head}</div>{texts}{"".join(extra)}'
            + (f'<div class="acts row">{"".join(acts)}</div>' if acts else "") + "</div>")


COUNTER_FIELDS = (
    '<div class="frow"><label>톤 <select name="tone"><option value="soft">부드럽게</option>'
    '<option value="normal" selected>보통</option><option value="firm">강하게</option></select></label>'
    '<label>단가 $ <input name="price" inputmode="decimal" size="6" placeholder="목표가"></label>'
    f'<label>언어 <select name="lang">{LANG_OPTS}</select></label></div>'
    '<input name="other" maxlength="200" placeholder="타 업체 비교 언급 (선택, 사실만)">'
    '<input name="note" maxlength="500" placeholder="추가 요청 (예: MOQ 200으로, 샘플비 환급)">'
    '<button class="btn primary">역제안 초안 만들기</button>'
    '<div class="small muted">단가를 비우면 3번의 내부 목표가를 씁니다. 상한가는 초안·공용 DB에 들어가지 않습니다.</div>')


def quote_rows(conn, ps_id):
    rows = conn.execute("SELECT * FROM quotes WHERE product_supplier_id = ? ORDER BY id DESC", (ps_id,)).fetchall()
    out = []
    for q in rows:
        try:
            tiers = json.loads(q["price_tiers_json"]) if q["price_tiers_json"] else []
        except ValueError:
            tiers = []
        tiers = tiers if isinstance(tiers, list) else []
        tier_txt = " · ".join(f'{t.get("qty")}개 ${t["usd"]:g}' for t in tiers
                              if isinstance(t, dict) and isinstance(t.get("usd"), (int, float)))
        row = [f'#{q["id"]}', f'<b>${q["unit_price_usd"]:g}</b>', num(q["min_qty"], "{:,}개"), e(tier_txt) or "–",
               e(q["incoterm"] or "–"), num(q["lead_time_days"], "{}일"), clip(q["payment_terms"], 30),
               num(q["sample_cost_usd"], "${:g}"), e((q["created_at"] or "")[:10])]
        if SERVER["on"]:
            row.append('<details><summary class="btn">역제안</summary>'
                       + neg_form("counter", {"quote_id": q["id"]}, COUNTER_FIELDS, cls="stack") + "</details>")
        out.append(row)
    if not out:
        return ""
    heads = ["견적", "단가", "MOQ", "가격 구간", "조건", "납기", "결제", "샘플", "받은 날"] + (["작업"] if SERVER["on"] else [])
    return table(heads, out, "tight")


def supplier_thread(conn, s):
    msgs = conn.execute("SELECT * FROM messages WHERE product_supplier_id = ? ORDER BY id", (s["link_id"],)).fetchall()
    # 마지막으로 보낸 메일 이후 답장이 없으면 'N일째 답 없음' + (대기 중인 후속 초안이 없으면) 후속 메일 버튼
    sent = [m for m in msgs if m["direction"] == "outbound" and m["status"] == "sent" and m["sent_at"]]
    last = sent[-1] if sent else None
    waiting = None
    if last and not any(m["direction"] == "inbound" and (m["sent_at"] or m["created_at"]) >= last["sent_at"] for m in msgs):
        waiting = (datetime.now() - datetime.fromisoformat(last["sent_at"])).days
    pending_fu = any(m["kind"] == "follow_up" and m["status"] in ("draft", "approved") for m in msgs)
    items = "".join(message_item(m, waiting if m is last else None,
                                 can_follow=(m is last and waiting is not None and not pending_fu)) for m in msgs)
    has_rfq = any(m["kind"] == "rfq" and m["status"] != "discarded" for m in msgs)
    forms = ""
    if SERVER["on"] and not has_rfq:
        qty = f'{s["ps_max_qty"]} pcs' if s["ps_max_qty"] else "예: 50 pcs"
        forms += ('<details><summary class="btn primary">견적 요청 초안</summary>' + neg_form("rfq", {"ps_id": s["link_id"]},
                  f'<div class="frow"><label>수량 <input name="qty" maxlength="60" size="12" placeholder="{e(qty)}"></label>'
                  '<label>조건 <select name="incoterm"><option value="">미지정</option><option>FOB</option>'
                  '<option>EXW</option><option>CIF</option><option>DDP</option></select></label>'
                  f'<label>언어 <select name="lang">{LANG_OPTS}</select></label></div>'
                  '<input name="note" maxlength="500" placeholder="추가 요청 (예: 로고 인쇄 가능 여부)">'
                  '<button class="btn primary">초안 만들기</button>'
                  '<div class="small muted">2번이 정리한 첫 문의 질문·확인 못 한 항목·필요 인증을 함께 묻습니다.</div>',
                  cls="stack") + "</details>")
    if SERVER["on"] and msgs:
        forms += ('<details><summary class="btn">받은 답장 붙여넣기</summary>' + neg_form("reply", {"ps_id": s["link_id"]},
                  '<textarea name="text" rows="7" maxlength="20000" required '
                  'placeholder="공급처가 보낸 메일·채팅 내용을 그대로 붙여넣기"></textarea>'
                  '<button class="btn primary">저장하고 분석</button>'
                  '<div class="small muted">요약·번역하고, 가격이 있으면 견적표에 넣습니다.</div>',
                  confirm="답장을 저장하고 3번 에이전트로 분석할까요? (수십 초 · API 비용)", cls="stack") + "</details>")
    contact = " · ".join(x for x in (e(s["contact_email"]), e(s["contact_other"])) if x)
    return f"""<div class="neg" id="ps-{s["link_id"]}">
  <div class="neg-head">{'<span class="pick">추천</span> ' if s["is_top_pick"] else ""}<b>{e(s["company"])}</b>
    <span class="muted small">{e(s["country"])} · 연결 #{s["link_id"]}</span>
    <span class="trust t{e(s["trust"])}">{e(s["trust"] or "")}</span>
    <span class="badge">{e(PS_STATUS.get(s["status"], s["status"]))}</span>
    <span class="small">{contact or '<span class="muted">연락처 없음</span>'}</span>
    <span class="right acts row">{forms}</span></div>
  {items}{quote_rows(conn, s["link_id"])}
</div>"""


def negotiation_view(conn):
    prods = conn.execute(f"""SELECT * FROM products WHERE stage NOT IN ('candidate', 'closed', 'dropped')
                             AND EXISTS (SELECT 1 FROM product_suppliers ps WHERE ps.product_id = products.id)
                             ORDER BY {db.stage_order_sql()} DESC, id""").fetchall()
    if not prods:
        return '<p class="empty">공급처가 정해진 진행 품목이 없습니다 — 2번이 공급처를 찾으면 여기서 견적 요청을 시작합니다.</p>', 0
    cards = []
    for p in prods:
        sups = conn.execute("""
            SELECT s.*, ps.id AS link_id, ps.status, ps.is_top_pick, ps.max_order_qty AS ps_max_qty,
                   (SELECT COUNT(*) FROM messages m WHERE m.product_supplier_id = ps.id) AS n_msgs
            FROM product_suppliers ps JOIN suppliers s ON s.id = ps.supplier_id WHERE ps.product_id = ?
            ORDER BY n_msgs > 0 DESC, ps.is_top_pick DESC, ps.id""", (p["id"],)).fetchall()
        main = [s for s in sups if s["n_msgs"] or s["is_top_pick"]]
        rest = [s for s in sups if not (s["n_msgs"] or s["is_top_pick"])]
        body = "".join(supplier_thread(conn, s) for s in main)
        if rest:
            body += (f'<details class="more"><summary class="small">다른 후보 공급처 {len(rest)}곳</summary>'
                     + "".join(supplier_thread(conn, s) for s in rest) + "</details>")
        cards.append(f'<div class="card"><div class="card-head"><span class="badge">{e(STAGE_LABEL.get(p["stage"], p["stage"]))}</span>'
                     f'{photo(p)}<div><b>#{p["id"]} {e(p["name"])}</b> <span class="muted small">{e(p["search_keyword_en"])}</span>'
                     f'</div></div>{body}</div>')
    if SERVER["on"]:       # Gmail: 사람이 보낸 메일을 '보냄'으로, 새 답장은 가져와 분석 (슈퍼바이저도 2시간마다 함)
        cards.insert(0, '<div class="row">' + neg_form(
            "gmail_sync", {}, '<button class="btn">Gmail 확인 (발송 확인 · 새 답장 가져오기)</button>',
            confirm="Gmail에서 보낸 메일·새 답장을 확인할까요? (새 답장은 분석 · API 비용)", cls="inline")
            + '<span class="small muted">처음 한 번 터미널에서 <code>python -m negotiation_agent gmail-login</code></span></div>')
    return "".join(cards), len(prods)


def agent_body(conn, agent):
    """에이전트별 페이지의 본문 섹션들."""
    if agent == "discover":
        html, n = candidate_rows(conn)
        return [section("후보 품목", html, f"{n}개 · 다음 단계로: trade_hub select &lt;번호&gt; · 검색량은 '텀블러'=100"),
                moved_on(conn), dropped(conn)]
    if agent == "sourcing":
        html, n = products_in_progress(conn)
        return [section("품목별 공급처", html, f"{n}개 품목 · 업체 이름을 누르면 리포트 상세"),
                section("공급처 검토 할 일", tasks_of(conn, ["decision"]))]
    if agent == "negotiation":
        html, n = negotiation_view(conn)
        hint = ("메일은 직접 보내고 '발송 완료로 표시' · 답 없으면 3일 뒤 후속 메일 초안 자동" if SERVER["on"]
                else "버튼은 <code>trade_hub serve</code>에서 · 터미널: <code>python -m negotiation_agent status</code>")
        return [section("공급처별 메일·견적", html, f"{n}개 품목 · {hint}"),
                section("발송·견적 검토 할 일", tasks_of(conn, ["send_message", "review_quote"]))]
    if agent == "documents":
        return [section("서류 할 일", tasks_of(conn, ["document"]))]
    if agent == "compliance":
        return [section("진행 품목의 HS 코드·관세·인증", compliance_rows(conn))]
    if agent == "costing":
        return [section("공급처별 원가·마진", costing_rows(conn))]
    if agent == "sales":
        return [section("판매 기록", generic_rows(conn, "sales"))]
    if agent == "finance":
        return [section("발주", generic_rows(conn, "orders")), section("운송·선적", generic_rows(conn, "shipments")),
                section("장부", generic_rows(conn, "ledger_entries")), section("품목별 손익", pnl_rows(conn)),
                section("결제 할 일", tasks_of(conn, ["payment"]))]
    return []


def agent_page(conn, agent, no, name, roles):
    r = last_run(conn, agent)
    if r:
        state = f'마지막 실행 #{r["id"]} · {e(r["started_at"][:16])} · ' + (
            {"succeeded": '<span class="ok">성공</span>', "failed": '<span class="bad">실패</span>',
             "running": '<span class="warn">실행 중</span>'}.get(r["status"], e(r["status"])))
    else:
        state = '<span class="muted">아직 공용 DB에 연결되지 않았습니다 — 이 에이전트가 기록을 남기면 여기에 표시됩니다.</span>'
    intro = (f'<section class="intro"><div class="intro-no">{no}</div><div><h2>{e(name)} 에이전트</h2>'
             f'<ul>{"".join(f"<li>{e(x)}</li>" for x in roles)}</ul><p class="small">{state}</p></div></section>')
    return intro + "".join(agent_body(conn, agent)) + section("실행 기록", runs(conn, agent), "최근 15건")


SEARCH_JS = """
var INDEX = JSON.parse(document.getElementById("prod-index").textContent);
var box = document.getElementById("q"), list = document.getElementById("q-results");
function norm(t) { return (t || "").toLowerCase().replace(/\\s+/g, ""); }
function esc(t) { var d = document.createElement("div"); d.textContent = t == null ? "" : String(t); return d.innerHTML; }
function renderResults() {
  var q = box.value.trim(), nq = norm(q);
  if (!nq) { list.hidden = true; return; }
  var hits = INDEX.filter(function (p) { return norm(p.name + " " + p.kw).indexOf(nq) >= 0; }).slice(0, 8);
  var html = hits.map(function (p) {
    return '<a class="hit" href="#p' + p.id + '"><b>#' + p.id + '</b> ' + esc(p.name) + ' <span class="muted small">' + esc(p.stage) + '</span></a>';
  }).join("");
  if (!hits.length) html = '<div class="hit muted">DB에 없는 품목입니다</div>';
  if (window.SERVER_MODE) {
    html += '<button type="button" class="hit new" id="q-new">🔎 「' + esc(q) + '」 새로 조사하기 <span class="muted small">(1번 에이전트 · 1~3분 · API 비용)</span></button>';
  } else {
    html += '<div class="hit muted small">새로 조사하려면 <code>trade_hub serve</code>로 열거나 터미널에서 <code>python supplier_agent.py --product "' + esc(q) + '"</code></div>';
  }
  list.innerHTML = html; list.hidden = false;
  var nb = document.getElementById("q-new");
  if (nb) nb.onclick = function () {
    if (confirm("「" + q + "」을(를) 1번 에이전트로 조사할까요? (1~3분 · API 비용)")) startJob("/api/research", {query: q});
  };
}
box.addEventListener("input", renderResults);
box.addEventListener("focus", renderResults);
box.addEventListener("keydown", function (ev) {
  if (ev.key === "Enter") { var first = list.querySelector("a.hit, #q-new"); if (first) first.click(); }
  if (ev.key === "Escape") { list.hidden = true; box.blur(); }
});
document.getElementById("q-go").addEventListener("click", function () {   // 돋보기: 검색 실행
  if (!box.value.trim()) { box.focus(); return; }
  renderResults();
  var hits = list.querySelectorAll("a.hit");
  if (hits.length === 1) { hits[0].click(); return; }                     // 딱 하나면 바로 그 품목으로
  box.focus();
});
document.addEventListener("click", function (ev) { if (!ev.target.closest(".search")) list.hidden = true; });
list.addEventListener("click", function (ev) { if (ev.target.closest("a.hit")) list.hidden = true; });

function show() {
  var id = (location.hash || "#home").slice(1), target = null;
  var m = id.match(/^p(\\d+)$/);
  if (m) {                                   // #p16 → 그 품목이 있는 페이지를 열고 그 줄로 이동
    target = document.getElementById("prod-" + m[1]);
    var page = target && target.closest(".page");
    id = page ? page.id.slice(2) : "home";
  } else if (/^ps-\\d+$/.test(id)) {          // #ps-12 → 3번 페이지의 그 공급처 (접힌 '다른 후보' 안이면 펼침)
    target = document.getElementById(id);
    var pg = target && target.closest(".page");
    var fold = target && target.closest("details.more");
    if (fold) fold.open = true;
    id = pg ? pg.id.slice(2) : "home";
  }
  var found = false;
  document.querySelectorAll(".page").forEach(function (p) { var on = p.id === "p-" + id; p.hidden = !on; found = found || on; });
  if (!found) { id = "home"; document.getElementById("p-home").hidden = false; }
  document.querySelectorAll(".pipe-nav a").forEach(function (a) { a.classList.toggle("cur", a.getAttribute("href") === "#" + id); });
  if (target) {
    var row = target.closest("tr") || target;
    row.scrollIntoView({block: "center"});
    row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
  } else { window.scrollTo(0, 0); }
}
window.addEventListener("hashchange", show);
show();
document.addEventListener("click", function (ev) {   // 초안 원문 복사 → 메일·알리바바 등에 붙여넣어 직접 발송
  var b = ev.target.closest("button[data-copy]");
  if (!b) return;
  var src = document.getElementById(b.dataset.copy);
  if (!src || !navigator.clipboard) return;
  navigator.clipboard.writeText(src.textContent).then(function () {
    var t = b.textContent; b.textContent = "복사됨 ✓"; setTimeout(function () { b.textContent = t; }, 1500);
  });
});
"""

SERVER_JS = """
window.SERVER_MODE = true;
var panel = document.getElementById("jobs"), mine = {}, lastJobs = [];
var closedJobs = {};                          // 닫은 알림 (작업 id) — 새로고침해도 유지
                                              // ※ 이름을 closed로 하면 브라우저 내장 window.closed(읽기 전용, 항상 false)와 겹쳐 저장이 안 됨
try { closedJobs = JSON.parse(localStorage.getItem("tradeHubClosedJobs") || "{}"); } catch (err) { closedJobs = {}; }
if (!closedJobs || typeof closedJobs !== "object" || Array.isArray(closedJobs)) closedJobs = {};   // 예전 버그로 저장된 "false" 등은 버림
function saveClosed() { try { localStorage.setItem("tradeHubClosedJobs", JSON.stringify(closedJobs)); } catch (err) {} }
function api(path, body) {
  return fetch(path, {method: body ? "POST" : "GET",
    headers: body ? {"Content-Type": "application/json", "X-Trade-Hub": "1"} : {},
    body: body ? JSON.stringify(body) : undefined}).then(function (r) {
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.status); return d; }); });
}
function startJob(path, body) {
  return api(path, body).then(function (job) { mine[job.id] = true; poll(); })
    .catch(function (err) { alert(err.message); });
}
document.addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-act]");
  if (!b) return;
  if (b.dataset.act === "select") {
    var mode = b.dataset.mode || "small_batch", ml = b.dataset.modeLabel || "소량 사입";
    var cs = b.parentNode.querySelector("select.country"), country = cs ? cs.value : "China";
    ml += " · " + (cs ? cs.options[cs.selectedIndex].text : "중국");
    if (!confirm("#" + b.dataset.id + " " + b.dataset.name + "\\n[" + ml + "] 방식으로 공급처를 찾을까요? 공급처 발굴 단계로 넘기고 2번 에이전트를 실행합니다. (5~10분 · API 비용)")) return;
    startJob("/api/select", {product_id: Number(b.dataset.id), mode: mode, country: country});
  } else if (b.dataset.act === "research") {
    if (!confirm("「" + b.dataset.q + "」을(를) 1번 에이전트로 다시 조사할까요? (1~3분 · API 비용)")) return;
    startJob("/api/research", {query: b.dataset.q});
  }
});
// 구매대행 직접 검색: [영어]·[일본어]·[중국어] 버튼 = 언어 고르기 + 한글이면 그 언어로 번역해 검색창에 넣기
var HANGUL = /[가-힣ㄱ-ㆎ]/;
function buyTranslate(f, lang) {
  var input = f.querySelector('input[name="query"]'), text = input.value.trim();
  if (!text || !HANGUL.test(text)) return Promise.resolve(text);
  input.disabled = true;
  return api("/api/translate", {text: text, lang: lang}).then(function (d) {
    input.value = d.text; input.title = "원문: " + text; return d.text;
  }).finally(function () { input.disabled = false; });
}
function buyLinks(f, q) {   // 중국 사이트는 자동 검색이 없음 → 검색 결과 페이지 링크 (사람이 직접 확인)
  var box = f.parentNode.querySelector(".buylinks"), u = encodeURIComponent(q);
  var sites = [["타오바오", "https://s.taobao.com/search?q=" + u],
               ["1688", "https://s.1688.com/selloffer/offer_search.htm?charset=utf8&keywords=" + u],
               ["알리익스프레스", "https://www.aliexpress.com/w/wholesale-" + u + ".html"]];
  box.innerHTML = "";
  box.appendChild(document.createTextNode("「" + q + "」 중국 사이트에서 보기: "));
  sites.forEach(function (s, i) {
    var a = document.createElement("a");
    a.href = s[1]; a.target = "_blank"; a.rel = "noopener"; a.textContent = s[0] + " ↗";
    if (i) box.appendChild(document.createTextNode(" · "));
    box.appendChild(a);
  });
}
document.addEventListener("click", function (ev) {   // 언어 버튼: 번역 + 사이트가 그 언어가 아니면 맞는 사이트로
  var b = ev.target.closest("button.buylang");
  if (!b) return;
  var f = b.closest("form.buysearch"), lang = b.dataset.lang, site = f.querySelector('select[name="market"]');
  f.querySelectorAll("button.buylang").forEach(function (x) { x.classList.toggle("on", x === b); });
  if (site.options[site.selectedIndex].dataset.lang !== lang) {
    var o = site.querySelector('option[data-lang="' + lang + '"]');
    if (o) site.value = o.value;
  }
  buyTranslate(f, lang).catch(function (err) { alert(err.message); });
});
document.addEventListener("submit", function (ev) {   // 검색 → 라쿠텐·알리·이베이: /api/buysearch (buyagent --market) · 타오바오·1688: 링크
  var f = ev.target.closest("form.buysearch");
  if (!f) return;
  ev.preventDefault();
  var body = {};
  new FormData(f).forEach(function (v, k) { body[k] = v; });
  if (!(body.query || "").trim()) return;
  var btn = f.querySelector('button[type="submit"]'), txt = btn.textContent;
  btn.disabled = true; btn.textContent = "검색 준비 중…";
  var done = function () { btn.disabled = false; btn.textContent = txt; };
  if (body.market === "china") {
    buyTranslate(f, "zh").then(function (q) { buyLinks(f, q); }).catch(function (err) { alert(err.message); }).finally(done);
    return;
  }
  f.parentNode.querySelector(".buylinks").innerHTML = "";
  startJob("/api/buysearch", body).then(done);
});
document.addEventListener("submit", function (ev) {   // 3번 협상 폼 → /api/neg (메일은 보내지 않음, 초안만)
  var f = ev.target.closest("form.negf");
  if (!f) return;
  ev.preventDefault();
  var body = {};
  new FormData(f).forEach(function (v, k) { body[k] = v; });
  var sub = ev.submitter;
  if (sub && sub.name) body[sub.name] = sub.value;
  var ask = (sub && sub.dataset.confirm) || f.dataset.confirm;
  if (ask && !confirm(ask)) return;
  startJob("/api/neg", body);
});
function jobTarget(j) { return j.anchor ? "#" + j.anchor : (j.product_id ? "#p" + j.product_id : ""); }
var timer = null;
function poll() {
  api("/api/jobs").then(function (jobs) {
    var running = jobs.some(function (j) { return j.status === "running"; });
    var shown = jobs.filter(function (j) { return !closedJobs[j.id]; });   // 닫은 알림은 숨김 (작업은 계속 진행)
    panel.hidden = !shown.length;
    panel.innerHTML = (shown.length > 1 ? '<div class="jobs-head"><span class="small muted">작업 ' + shown.length +
        '개</span><button type="button" class="x" data-close="all" title="모두 닫기" aria-label="모두 닫기">모두 닫기 ✕</button></div>' : "") +
      shown.map(function (j) {
      var st = {running: "⏳ 실행 중", done: "✅ 완료", failed: "❌ 실패"}[j.status] || j.status;
      var go = jobTarget(j) && j.status === "done" ? ' <a href="' + esc(jobTarget(j)) + '" class="go" data-reload="1">결과 보기</a>' : "";
      return '<div class="job ' + j.status + '"><button type="button" class="x" data-close="' + esc(j.id) +
        '" title="닫기" aria-label="닫기">✕</button><b>' + esc(j.label) + '</b> <span class="small">' + st + ' · ' + esc(j.elapsed) + go +
        '</span><pre>' + esc(j.lines.join("\\n")) + '</pre></div>';
    }).join("");
    lastJobs = jobs;
    jobs.forEach(function (j) {               // 이 화면에서 시작한 작업이 끝나면 새 데이터로 다시 그리고 결과로 이동
      if (mine[j.id] && j.status !== "running") {
        delete mine[j.id];
        if (j.status === "done") { location.hash = jobTarget(j) || location.hash; location.reload(); }
      }
    });
    clearTimeout(timer);
    if (running) timer = setTimeout(poll, 2000);
  }).catch(function () { clearTimeout(timer); timer = setTimeout(poll, 5000); });
}
panel.addEventListener("click", function (ev) {
  var x = ev.target.closest("button[data-close]");
  if (x) {                                    // 알림만 닫음 — 실행 중인 작업은 멈추지 않음
    if (x.dataset.close === "all") lastJobs.forEach(function (j) { closedJobs[j.id] = true; });
    else closedJobs[x.dataset.close] = true;
    saveClosed(); poll(); return;
  }
  var a = ev.target.closest("a.go"); if (!a) return;
  ev.preventDefault(); location.hash = a.getAttribute("href"); location.reload();
});
poll();
"""


def render(conn, server=False):
    """server=True: trade_hub serve용 (로컬 파일은 /file 주소, 조사·선정 버튼과 작업 패널 포함)."""
    SERVER["on"] = server
    try:
        return _render(conn, server)
    finally:
        SERVER["on"] = False


def _render(conn, server):
    tasks_html, n_tasks = pending_tasks(conn)
    home = "".join([
        section("승인 대기함", tasks_html, f"{n_tasks}건 · 처리: trade_hub done &lt;번호&gt;"),
        section("품목 단계 현황", stage_overview(conn)),
        section("슈퍼바이저 브리핑", briefing(conn)),
        section("전체 실행 기록", runs(conn), "최근 15건"),
    ])
    pages = [f'<div class="page" id="p-home">{home}</div>']
    for agent, no, name, roles in AGENT_PAGES:
        pages.append(f'<div class="page" id="p-a{no}" hidden>{agent_page(conn, agent, no, name, roles)}</div>')
        if agent == "sourcing":
            pages.append(f'<div class="page" id="p-buy" hidden>{buying.page()}</div>')
    now =datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>무역 대시보드</title><style>{CSS}</style></head>
<body>
<div class="top"><div class="top-in">
<header><h1>1인 수입 무역 대시보드</h1>
<div class="search"><div class="sbox"><input id="q" type="search" autocomplete="off" aria-label="품목 검색"
  placeholder="품목 검색{' · 없으면 새로 조사' if server else ''} (예: 무선 기계식 키보드)"><button type="button" id="q-go"
  class="sgo" title="검색" aria-label="검색"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
  stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></button></div>
<div id="q-results" class="results" hidden></div></div>
<span class="muted small">{now} 기준 · {'서버 모드 (새로고침하면 최신)' if server else '최신화: <code>trade_hub dashboard</code> · 조사 실행은 <code>trade_hub serve</code>'}</span></header>
{nav(conn)}
</div></div>
<main>{"".join(pages)}</main>
<div id="jobs" class="jobs" hidden></div>
<script type="application/json" id="prod-index">{product_index(conn)}</script>
<script>{SERVER_JS if server else "function startJob() {{}}"}{SEARCH_JS}</script>
</body></html>"""


def build(out=None, conn=None, refs=True, log=None):
    """refs=True면 확정 사진이 없는 품목에 네이버 이미지 검색 결과를 '참고 이미지'로 붙인다 (화면에만, DB 저장 없음)."""
    conn = conn or db.connect()
    out = Path(out) if out else db.default_path().parent / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    _REFS.clear()
    if refs:
        got, need = load_reference_images(conn, log)
        if log and need:
            log(f"  · 참고 이미지 {got}/{need}개 (확정 사진 없는 품목)" + ("" if got else " — .env의 NAVER_CLIENT_ID/SECRET·'검색' API 구독 확인"))
    out.write_text(render(conn), encoding="utf-8")
    return out.resolve()
