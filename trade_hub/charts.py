"""
1번 페이지 맨 아래 '후보 한눈에 보기' 차트 (인라인 SVG · 외부 라이브러리 없음 · 파일로 열어도 보임).

  1. 후보 지도 (산점도) — 1번 리포트(candidates.html) '후보 지도'와 같은 기준:
     가로 = 검색량지수 naver_json.volume_idx (로그 눈금, 0은 0.01), 세로 = 총점, 점 크기 = 마진 여지(1~5),
     점 색 = 쇼핑 추세 shopping_insight.trend (오름 초록 · 보합 주황 · 내림 빨강 · 없음 회색),
     검색량·총점 둘 다 중간값 이상 구역은 연하게. 점 → 표의 그 품목 줄(#p번호)
  2. 상위 15개 점수 구성 (수요 30 · 마진 25 · 경쟁 20 · 규제 15 · 물류 10 가중 점수 누적 막대)
  3. 상위 12개 쇼핑인사이트 12개월 클릭 추세 (작은 선 그래프 · 최고 달 점 · 계절 표시)
  4. 분류별 · 추천 공급국별 후보 수

대상: candidate 단계 품목의 최신 product_scores 한 줄 (진행 중 품목·[변형 혼합] 원래 품목 제외). 읽기 전용.
색은 대시보드 CSS 변수(--ok·--warn·--bad·--muted·--accent…)를 class로 써서 다크 모드에서도 보임.
"""
import json
import math
from html import escape


def e(v):
    return escape("" if v is None else str(v), quote=True)


WEIGHTS = (("demand", "수요", 30), ("margin", "마진", 25), ("competition", "경쟁", 20),
           ("regulation", "규제", 15), ("logistics", "물류", 10))
TREND_CLS = {"오름": "ch-up", "보합": "ch-flat", "내림": "ch-down"}

CSS = """
.charts{display:grid;gap:14px}.charts .chart{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--card)}
.charts h3{margin:0 0 6px;font-size:14px}.charts svg{display:block;max-width:100%;height:auto}
.charts svg text{fill:var(--fg);font-size:11px}.charts svg .ch-m{fill:var(--muted)}
.charts .ch-axis{stroke:var(--line)}.charts .ch-grid{stroke:var(--line);stroke-dasharray:2 3}
.charts .ch-zone{fill:var(--accent-soft);opacity:.6}
.charts .ch-up{fill:var(--ok)}.charts .ch-flat{fill:var(--warn)}.charts .ch-down{fill:var(--bad)}.charts .ch-none{fill:var(--muted)}
.charts circle.ch-pt{fill-opacity:.75;stroke:var(--card);stroke-width:1}.charts a:hover circle.ch-pt{fill-opacity:1;stroke:var(--fg)}
.charts .ch-s0{fill:var(--accent)}.charts .ch-s1{fill:var(--ok)}.charts .ch-s2{fill:var(--warn)}.charts .ch-s3{fill:var(--pick)}
.charts .ch-s4{fill:var(--muted)}.charts .ch-bar{fill:var(--accent);fill-opacity:.7}
.charts .ch-line{fill:none;stroke:var(--accent);stroke-width:1.6}.charts .ch-peak{fill:var(--bad)}
.charts .legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:4px 0}
.charts .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:-1px}
.charts .legend i.ch-up{background:var(--ok)}.charts .legend i.ch-flat{background:var(--warn)}.charts .legend i.ch-down{background:var(--bad)}
.charts .legend i.ch-none{background:var(--muted)}.charts .legend i.ch-s0{background:var(--accent)}.charts .legend i.ch-s1{background:var(--ok)}
.charts .legend i.ch-s2{background:var(--warn)}.charts .legend i.ch-s3{background:var(--pick)}.charts .legend i.ch-s4{background:var(--muted)}
.charts .smalls{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px}
.charts .smalls a{display:block;border:1px solid var(--line);border-radius:8px;padding:4px 6px;color:inherit;text-decoration:none}
.charts .smalls a:hover{border-color:var(--accent)}.charts .two{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
"""


def rows(conn):
    """candidate 단계 품목 + 최신 점수 (변형 혼합 원래 품목 제외). naver_json은 dict로."""
    from trade_hub.dashboard import mixed_variant
    out = []
    for r in conn.execute("""
            SELECT p.id, p.name, p.category, p.source_country, p.notes, s.total, s.demand, s.margin, s.competition,
                   s.regulation, s.logistics, s.naver_json
            FROM products p JOIN product_scores s ON s.id = (SELECT MAX(id) FROM product_scores WHERE product_id = p.id)
            WHERE p.stage = 'candidate' ORDER BY s.total DESC, p.id DESC"""):
        if mixed_variant(r["notes"]):
            continue
        try:
            nv = json.loads(r["naver_json"]) if r["naver_json"] else {}
        except ValueError:
            nv = {}
        d = dict(r)
        d["nv"] = nv if isinstance(nv, dict) else {}
        si = d["nv"].get("shopping_insight")
        d["si"] = si if isinstance(si, dict) else None
        out.append(d)
    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pct(v):
    return f"{v:+.0f}%" if _num(v) is not None else "–"


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else None


def _short(name, n=14):
    name = name or ""
    return name if len(name) <= n else name[:n - 1] + "…"


def scatter(items):
    """차트 1. 후보 지도. 검색량(volume_idx)이나 쇼핑인사이트가 없는 품목은 빼고 아래에 작게 적음."""
    pts, skipped = [], []
    for r in items:
        v = _num(r["nv"].get("volume_idx"))
        if v is None or r["si"] is None or _num(r["total"]) is None:
            skipped.append(r["id"])
            continue
        pts.append((r, max(v, 0.01)))
    if not pts:
        return '<p class="empty">지도에 찍을 품목이 없습니다 (검색량·쇼핑 추세가 있는 후보 없음).</p>'
    W, H, L, R, T, B = 760, 420, 52, 18, 14, 42
    lx = [math.log10(v) for _, v in pts]
    x0, x1 = math.floor(min(lx)), math.ceil(max(lx))
    x1 = x1 if x1 > x0 else x0 + 1
    ys = [r["total"] for r, _ in pts]
    y0, y1 = max(0, math.floor(min(ys) / 10) * 10), min(100, math.ceil(max(ys) / 10) * 10)
    y1 = y1 if y1 > y0 else y0 + 10

    def X(v):
        return L + (math.log10(v) - x0) / (x1 - x0) * (W - L - R)

    def Y(t):
        return H - B - (t - y0) / (y1 - y0) * (H - T - B)

    mx, my = _median([v for _, v in pts]), _median(ys)
    g = [f'<rect class="ch-zone" x="{X(mx):.1f}" y="{T}" width="{W - R - X(mx):.1f}" height="{Y(my) - T:.1f}"/>']
    for k in range(x0, x1 + 1):                     # 가로 눈금: 10의 거듭제곱
        x = X(10 ** k)
        g.append(f'<line class="ch-grid" x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{H - B}"/>'
                 f'<text class="ch-m" x="{x:.1f}" y="{H - B + 14}" text-anchor="middle">{10 ** k:g}</text>')
    for t in range(y0, y1 + 1, 10):
        y = Y(t)
        g.append(f'<line class="ch-grid" x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}"/>'
                 f'<text class="ch-m" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{t}</text>')
    g.append(f'<line class="ch-axis" x1="{L}" y1="{H - B}" x2="{W - R}" y2="{H - B}"/>'
             f'<line class="ch-axis" x1="{L}" y1="{T}" x2="{L}" y2="{H - B}"/>'
             f'<text class="ch-m" x="{(L + W - R) / 2}" y="{H - 6}" text-anchor="middle">검색량지수 (로그 눈금 · 텀블러=100 · 0은 0.01)</text>'
             f'<text class="ch-m" x="12" y="{(T + H - B) / 2}" text-anchor="middle" transform="rotate(-90 12 {(T + H - B) / 2})">총점</text>')
    for r, v in sorted(pts, key=lambda p: -(_num(p[0]["margin"]) or 0)):   # 큰 점 먼저 (작은 점이 위에)
        si, nv = r["si"], r["nv"]
        trend = si.get("trend")
        rad = 3 + 1.6 * min(max(_num(r["margin"]) or 1, 1), 5)
        x, y = X(v), Y(r["total"])
        tip = (f'#{r["id"]} {r["name"]} · 총점 {r["total"]:g} · 검색량 {nv.get("volume_idx")} · 3개월 {_pct(nv.get("growth_3m"))}'
               f' · 전년比 {_pct(nv.get("yoy"))} · 쇼핑 추세 {trend or "없음"}'
               + (f' {si["change_3m"]:+.0f}%' if _num(si.get("change_3m")) is not None else "")
               + f' · 마진 여지 {r["margin"] if r["margin"] is not None else "?"}')
        g.append(f'<a href="#p{r["id"]}"><title>{e(tip)}</title>'
                 f'<circle class="ch-pt {TREND_CLS.get(trend, "ch-none")}" cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}"/>'
                 f'<text x="{x + rad + 2:.1f}" y="{y + 4:.1f}">{r["id"]}</text></a>')
    legend = ('<div class="legend"><span><i class="ch-up"></i>쇼핑 추세 오름</span><span><i class="ch-flat"></i>보합</span>'
              '<span><i class="ch-down"></i>내림</span><span><i class="ch-none"></i>추세 없음</span>'
              f'<span>점 크기 = 마진 여지(1~5) · 칠한 구역 = 검색량·총점 둘 다 중간값 이상 (검색량 {mx:g} · 총점 {my:g}) · 점을 누르면 표의 그 줄로</span></div>')
    skip = (f'<p class="small muted">검색량·쇼핑 추세가 없어 지도에서 뺀 품목 {len(skipped)}개: '
            + ", ".join(f'<a href="#p{i}">#{i}</a>' for i in skipped) + "</p>") if skipped else ""
    return legend + f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="후보 지도">{"".join(g)}</svg>' + skip


def stacked(items, n=15):
    """차트 2. 상위 n개 점수 구성 (점수/5 × 가중치를 쌓음 → 합 = 총점)."""
    top = [r for r in items if _num(r["total"]) is not None][:n]
    if not top:
        return '<p class="empty">점수가 있는 후보가 없습니다.</p>'
    W, L, R, rowh = 760, 170, 40, 22
    H = len(top) * rowh + 26
    sc = (W - L - R) / 100
    g = []
    for k in (0, 25, 50, 75, 100):
        x = L + k * sc
        g.append(f'<line class="ch-grid" x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{H - 20}"/>'
                 f'<text class="ch-m" x="{x:.1f}" y="{H - 6}" text-anchor="middle">{k}</text>')
    for i, r in enumerate(top):
        y = i * rowh + 3
        x = L
        segs = []
        for j, (col, label, w) in enumerate(WEIGHTS):
            s = _num(r[col])
            if s is None:
                continue
            v = s / 5 * w
            segs.append(f'<rect class="ch-s{j}" x="{x:.1f}" y="{y}" width="{v * sc:.1f}" height="{rowh - 6}">'
                        f'<title>{e(f"#{r['id']} {label} {s:g}/5 × {w} = {v:.1f}점")}</title></rect>')
            x += v * sc
        g.append(f'<a href="#p{r["id"]}"><text x="{L - 6}" y="{y + 12}" text-anchor="end">#{r["id"]} {e(_short(r["name"]))}</text>'
                 + "".join(segs) + f'<text class="ch-m" x="{x + 4:.1f}" y="{y + 12}">{r["total"]:g}</text></a>')
    legend = '<div class="legend">' + "".join(f'<span><i class="ch-s{j}"></i>{label} {w}</span>'
                                              for j, (_, label, w) in enumerate(WEIGHTS)) + "<span>(점수/5 × 가중치 · 합 = 총점)</span></div>"
    return legend + f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="상위 {len(top)}개 점수 구성">{"".join(g)}</svg>'


def sparks(items, n=12):
    """차트 3. 상위 n개(쇼핑인사이트 12개월 값이 있는 품목) 클릭 추세 작은 선 그래프."""
    top = []
    for r in items:
        si = r["si"] or {}
        s = [v for v in si.get("series") or [] if _num(v) is not None]
        if len(s) >= 2:
            top.append(r)
        if len(top) >= n:
            break
    if not top:
        return '<p class="empty">쇼핑인사이트 추세가 있는 후보가 없습니다.</p>'
    W, H, P = 170, 54, 4
    cells = []
    for r in top:
        si = r["si"]
        months = [str(m) for m in si.get("months") or []]
        s = [v if _num(v) is not None else 0 for v in si.get("series") or []]
        hi = max(s) or 1
        pts = [(P + i * (W - 2 * P) / (len(s) - 1), H - P - v / hi * (H - 2 * P)) for i, v in enumerate(s)]
        sea = si.get("seasonal") if isinstance(si.get("seasonal"), dict) else {}
        peak = str(sea.get("peak_month") or "")
        dot = ""
        if peak in months and months.index(peak) < len(pts):
            px, py = pts[months.index(peak)]
            dot = f'<circle class="ch-peak" cx="{px:.1f}" cy="{py:.1f}" r="3"><title>{e("최고 달 " + peak)}</title></circle>'
        trend = si.get("trend")
        tag = (f' <span class="{ {"오름": "ok", "내림": "bad"}.get(trend, "muted") }">{e(trend or "데이터 적음")}'
               + (f' {si["change_3m"]:+.0f}%' if _num(si.get("change_3m")) is not None else "") + "</span>"
               + (f' <span class="moq unk">계절 {int(peak[5:7])}월</span>' if sea.get("is_seasonal") and peak[5:7].isdigit() else ""))
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        span = f"{months[0][2:]}~{months[-1][2:]}" if months else ""
        cells.append(f'<a href="#p{r["id"]}" title="{e(si.get("keyword") or "")} · {e(span)} · 키워드 자기 최대=100">'
                     f'<div class="small"><b>#{r["id"]}</b> {e(_short(r["name"], 12))}{tag}</div>'
                     f'<svg viewBox="0 0 {W} {H}" aria-label="#{r["id"]} 클릭 추세"><polyline class="ch-line" points="{line}"/>{dot}</svg></a>')
    return ('<div class="legend"><span>네이버 쇼핑인사이트 12개월 클릭 추세 · 품목마다 자기 최대=100 (품목끼리 크기 비교는 검색량)'
            ' · 빨간 점 = 최고 달</span></div><div class="smalls">' + "".join(cells) + "</div>")


def counts(title, pairs):
    if not pairs:
        return f'<div><b class="small">{e(title)}</b><p class="empty">없음</p></div>'
    W, L, R, rowh = 360, 120, 30, 18
    H = len(pairs) * rowh + 4
    hi = max(c for _, c in pairs)
    g = []
    for i, (k, c) in enumerate(pairs):
        y = i * rowh + 2
        w = c / hi * (W - L - R)
        g.append(f'<text x="{L - 6}" y="{y + 11}" text-anchor="end">{e(_short(k, 16))}</text>'
                 f'<rect class="ch-bar" x="{L}" y="{y}" width="{w:.1f}" height="{rowh - 5}"><title>{e(k)} {c}개</title></rect>'
                 f'<text class="ch-m" x="{L + w + 4:.1f}" y="{y + 11}">{c}</text>')
    return f'<div><b class="small">{e(title)}</b><svg viewBox="0 0 {W} {H}" aria-label="{e(title)}">{"".join(g)}</svg></div>'


def distribution(items):
    """차트 4. 분류별 · 추천 공급국별 후보 수."""
    from trade_hub import db
    cat, cty = {}, {}
    for r in items:
        k = r["category"] or "(분류 없음)"
        cat[k] = cat.get(k, 0) + 1
        c = db.SOURCE_COUNTRIES.get(r["source_country"], r["source_country"]) if r["source_country"] else "(추천 없음)"
        cty[c] = cty.get(c, 0) + 1
    order = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))   # noqa: E731
    return f'<div class="two">{counts("분류별 후보 수", order(cat))}{counts("추천 공급국별 후보 수", order(cty))}</div>'


def candidate_charts(conn):
    """1번 페이지 '후보 한눈에 보기' 섹션 본문."""
    items = rows(conn)
    if not items:
        return '<p class="empty">후보 품목이 없습니다.</p>'
    parts = [("후보 지도 — 검색량 × 총점", scatter(items)), (f"상위 {min(15, len(items))}개 점수 구성", stacked(items)),
             ("수요 추세 (상위 12개)", sparks(items)), ("분포", distribution(items))]
    return '<div class="charts">' + "".join(f'<div class="chart"><h3>{e(t)}</h3>{b}</div>' for t, b in parts) + "</div>"
