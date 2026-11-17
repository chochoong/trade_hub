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

from trade_hub import db

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


def actions(p):
    """서버 모드 버튼: 후보 → [공급처 찾기](2번 단계로 넘기고 2번 실행), [다시 조사](1번 지정 품목 조사)."""
    name = e(p["name"])
    return (f'<div class="acts"><button type="button" class="btn primary" data-act="select" data-id="{p["id"]}" '
            f'data-name="{name}">공급처 찾기</button>'
            f'<button type="button" class="btn" data-act="research" data-q="{name}">다시 조사</button></div>')


def sourcing_button(p, has_suppliers):
    if not SERVER["on"] or p["stage"] != "sourcing":
        return ""
    label = "공급처 다시 찾기" if has_suppliers else "공급처 찾기"
    return (f' <button type="button" class="btn{"" if has_suppliers else " primary"}" data-act="select" '
            f'data-id="{p["id"]}" data-name="{e(p["name"])}">{label}</button>')


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


def products_in_progress(conn):
    prods = conn.execute(f"""SELECT * FROM products WHERE stage NOT IN ('candidate', 'dropped')
                             ORDER BY {db.stage_order_sql()} DESC, id""").fetchall()
    if not prods:
        return '<p class="empty">진행 중인 품목 없음 — <code>trade_hub select &lt;번호&gt;</code>로 후보를 넘기세요</p>', 0
    cards = []
    for p in prods:
        sups = conn.execute("""
            SELECT s.*, ps.id AS link_id, ps.image_url AS sup_image, ps.status, ps.is_top_pick, ps.moq, ps.price_note, ps.unit_price_usd, ps.run_id
            FROM product_suppliers ps JOIN suppliers s ON s.id = ps.supplier_id
            WHERE ps.product_id = ? ORDER BY ps.is_top_pick DESC, ps.id""", (p["id"],)).fetchall()
        report = conn.execute("""SELECT detail, run_id FROM tasks WHERE product_id = ? AND run_id IN
                                 (SELECT id FROM agent_runs WHERE agent = 'sourcing') ORDER BY id DESC LIMIT 1""",
                              (p["id"],)).fetchone()
        report_html = None
        if report and report["detail"]:
            path = Path(report["detail"])
            html = path.with_name("report.html")      # 예전 실행은 report.md를 가리킴 → 같은 폴더의 HTML 우선
            report_html = html if html.exists() else (path if path.exists() else None)
        # 리포트 안 순위 = 그 실행에서 저장된 순서 → 이름을 누르면 리포트의 해당 업체가 펼쳐진 채로 열림
        ranks = {}
        if report_html and report_html.suffix == ".html":
            ids = [r[0] for r in conn.execute("SELECT id FROM product_suppliers WHERE product_id = ? AND run_id = ? ORDER BY id",
                                              (p["id"], report["run_id"]))]
            ranks = {lid: n for n, lid in enumerate(ids, 1)}
        rows = []
        for s in sups:
            score = (s["trust_reason"] or "").split(" — ")[0]
            rank = ranks.get(s["link_id"])
            name = (f'<a href="{e(file_href(report_html))}#s{rank}" target="_blank" title="리포트에서 상세 보기">{e(s["company"])}</a>'
                    if rank else e(s["company"]))
            rows.append([
                '<span class="pick">추천</span>' if s["is_top_pick"] else "",
                f'<div class="pcell">{supplier_photo(s["sup_image"])}<div>{name}<div class="muted small">{e(s["country"])} · {e(s["type"])}'
                f'{" · " + num(s["years_in_business"], "{:g}년") if s["years_in_business"] else ""}</div></div></div>',
                f'<span class="trust t{e(s["trust"])}">{e(s["trust"] or "–")}</span> <span class="small">{e(score)}</span>',
                e(s["status"]), clip(s["moq"], 30), clip(s["price_note"] or num(s["unit_price_usd"], "${:g}"), 30),
                e(s["contact_email"]) or '<span class="muted">–</span>',
                clip(s["red_flags"], 50),
            ])
        last = conn.execute("SELECT note, at FROM product_events WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                            (p["id"],)).fetchone()
        rep = link(report_html, "공급처 리포트") if report_html else ""
        cards.append(f"""<div class="card" id="prod-{p["id"]}">
  <div class="card-head"><span class="badge">{e(STAGE_LABEL.get(p["stage"], p["stage"]))}</span>
    {photo(p, "big")}<div><b>#{p["id"]} {e(p["name"])}</b> <span class="muted small">{e(last["note"] if last else "")} · {e(last["at"][:16] if last else "")}</span>
    <div class="small">{search_links(p)}</div></div>
    <span class="right">{rep}{sourcing_button(p, bool(sups))}</span></div>
  {table(["", "공급처", "신뢰도·점수", "상태", "MOQ", "단가", "이메일", "위험 요소"], rows, "tight")}
</div>""")
    return "".join(cards), len(prods)


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
                    e(r["model"]), rep_html, clip(r["error"], 60)])
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
.chip.on{border-color:var(--accent);color:var(--fg);background:var(--accent-soft)}.chip b{color:var(--accent)}
.search{position:relative;flex:1 1 320px;max-width:520px}
.search input{width:100%;font:inherit;padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
.search input:focus{outline:2px solid var(--accent);border-color:transparent}
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
.job{padding:6px 0;border-bottom:1px solid var(--line)}.job:last-child{border-bottom:0}
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


def agent_body(conn, agent):
    """에이전트별 페이지의 본문 섹션들."""
    if agent == "discover":
        html, n = candidate_rows(conn)
        return [section("후보 품목", html, f"{n}개 · 다음 단계로: trade_hub select &lt;번호&gt; · 검색량은 '텀블러'=100"),
                dropped(conn)]
    if agent == "sourcing":
        html, n = products_in_progress(conn)
        return [section("품목별 공급처", html, f"{n}개 품목 · 업체 이름을 누르면 리포트 상세"),
                section("공급처 검토 할 일", tasks_of(conn, ["decision"]))]
    if agent == "negotiation":
        return [section("메일·메시지 (초안·수신)", generic_rows(conn, "messages")),
                section("견적", generic_rows(conn, "quotes")),
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
document.addEventListener("click", function (ev) { if (!ev.target.closest(".search")) list.hidden = true; });
list.addEventListener("click", function (ev) { if (ev.target.closest("a.hit")) list.hidden = true; });

function show() {
  var id = (location.hash || "#home").slice(1), target = null;
  var m = id.match(/^p(\\d+)$/);
  if (m) {                                   // #p16 → 그 품목이 있는 페이지를 열고 그 줄로 이동
    target = document.getElementById("prod-" + m[1]);
    var page = target && target.closest(".page");
    id = page ? page.id.slice(2) : "home";
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
"""

SERVER_JS = """
window.SERVER_MODE = true;
var panel = document.getElementById("jobs"), mine = {};
function api(path, body) {
  return fetch(path, {method: body ? "POST" : "GET",
    headers: body ? {"Content-Type": "application/json", "X-Trade-Hub": "1"} : {},
    body: body ? JSON.stringify(body) : undefined}).then(function (r) {
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.status); return d; }); });
}
function startJob(path, body) {
  api(path, body).then(function (job) { mine[job.id] = true; poll(); })
    .catch(function (err) { alert(err.message); });
}
document.addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-act]");
  if (!b) return;
  if (b.dataset.act === "select") {
    if (!confirm("#" + b.dataset.id + " " + b.dataset.name + "\\n공급처 발굴 단계로 넘기고 2번 에이전트를 실행할까요? (5~10분 · API 비용)")) return;
    startJob("/api/select", {product_id: Number(b.dataset.id)});
  } else if (b.dataset.act === "research") {
    if (!confirm("「" + b.dataset.q + "」을(를) 1번 에이전트로 다시 조사할까요? (1~3분 · API 비용)")) return;
    startJob("/api/research", {query: b.dataset.q});
  }
});
var timer = null;
function poll() {
  api("/api/jobs").then(function (jobs) {
    var running = jobs.some(function (j) { return j.status === "running"; });
    panel.hidden = !jobs.length;
    panel.innerHTML = jobs.map(function (j) {
      var st = {running: "⏳ 실행 중", done: "✅ 완료", failed: "❌ 실패"}[j.status] || j.status;
      var go = j.product_id && j.status === "done" ? ' <a href="#p' + j.product_id + '" class="go" data-reload="1">결과 보기</a>' : "";
      return '<div class="job ' + j.status + '"><b>' + esc(j.label) + '</b> <span class="small">' + st + ' · ' + esc(j.elapsed) + go +
        '</span><pre>' + esc(j.lines.join("\\n")) + '</pre></div>';
    }).join("");
    jobs.forEach(function (j) {               // 이 화면에서 시작한 작업이 끝나면 새 데이터로 다시 그리고 결과로 이동
      if (mine[j.id] && j.status !== "running") {
        delete mine[j.id];
        if (j.status === "done") { location.hash = j.product_id ? "#p" + j.product_id : location.hash; location.reload(); }
      }
    });
    clearTimeout(timer);
    if (running) timer = setTimeout(poll, 2000);
  }).catch(function () { clearTimeout(timer); timer = setTimeout(poll, 5000); });
}
panel.addEventListener("click", function (ev) {
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
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>무역 대시보드</title><style>{CSS}</style></head>
<body>
<div class="top"><div class="top-in">
<header><h1>1인 수입 무역 대시보드</h1>
<div class="search"><input id="q" type="search" autocomplete="off"
  placeholder="품목 검색{' · 없으면 새로 조사' if server else ''} (예: 무선 기계식 키보드)">
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
