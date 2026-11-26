import json
import re
import shutil
import subprocess

import pytest

from trade_hub import charts, dashboard, db, naver


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """테스트에서는 네이버를 부르지 않는다 (참고 이미지는 테스트마다 직접 넣음)."""
    monkeypatch.setattr(naver, "keys", lambda: None)


def test_dashboard_renders_empty_and_filled_db(conn, tmp_path):
    out = dashboard.build(tmp_path / "empty.html", conn)
    html = out.read_text(encoding="utf-8")
    for no in range(1, 9):                                  # 파이프라인 1~8 → 에이전트 페이지
        assert f'href="#a{no}"' in html and f'id="p-a{no}"' in html
    assert 'id="p-home"' in html and "미연결" in html

    with db.agent_run(conn, "discover") as run:
        with conn:
            pid = db.upsert_product(conn, "<키캡>", run.id, origin="discover", reason="이유 " * 50)
            conn.execute("INSERT INTO product_scores (product_id, run_id, total, naver_json) VALUES (?, ?, 70, ?)",
                         (pid, run.id, db.dumps({"volume_idx": 2.0, "growth_3m": -10, "yoy": 31})))
    with conn:
        db.set_stage(conn, pid, "sourcing")
        sid = db.upsert_supplier(conn, "Keys & Co", "China", trust="중", trust_reason="60점 (B) — 좋음")
        db.link_supplier(conn, pid, sid, is_top_pick=1, moq="50 pcs")
        db.add_task(conn, "decision", "공급처 검토", product_id=pid, detail="없는파일.md")
    html = dashboard.build(tmp_path / "full.html", conn).read_text(encoding="utf-8")
    assert "&lt;키캡&gt;" in html and "<키캡>" not in html     # HTML 이스케이프
    assert "Keys &amp; Co" in html and "추천" in html and "공급처 검토" in html
    before = conn.total_changes
    dashboard.build(tmp_path / "again.html", conn)
    assert conn.total_changes == before                     # 읽기 전용


def test_photo_priority_confirmed_then_reference_then_none(conn, tmp_path, monkeypatch):
    with conn:
        a = db.upsert_product(conn, "확정 사진 품목")
        b = db.upsert_product(conn, "참고 이미지 품목")
        c = db.upsert_product(conn, "사진 없는 품목")
    img = tmp_path / "a.png"
    img.write_bytes(b"PNG fake")
    with conn:
        assert db.set_product_photo(conn, a, img) == f"images/{a}.png"
    monkeypatch.setattr(naver, "reference_images",
                        lambda q, log=None: {b: {"thumbnail": "https://t/b.jpg", "link": "https://l/b", "title": "B"}})
    before = conn.total_changes
    html = dashboard.build(tmp_path / "d.html", conn).read_text(encoding="utf-8")
    assert conn.total_changes == before                              # 참고 이미지는 DB에 저장 안 함
    assert (db.images_dir() / f"{a}.png").resolve().as_uri() in html  # 확정 사진
    assert "https://t/b.jpg" in html and "<em>참고</em>" in html       # 참고 이미지
    assert 'title="이미지 없음"' in html                                 # 둘 다 없음
    assert conn.execute("SELECT image_url FROM products WHERE id = ?", (b,)).fetchone()[0] is None


def test_set_and_clear_product_photo(conn, tmp_path):
    with conn:
        pid = db.upsert_product(conn, "사진")
    jpg, png = tmp_path / "x.jpg", tmp_path / "y.png"
    jpg.write_bytes(b"j"); png.write_bytes(b"p")
    with conn:
        db.set_product_photo(conn, pid, jpg)
        db.set_product_photo(conn, pid, png)                           # 교체 → 파일은 1장만
    assert [f.name for f in db.images_dir().glob(f"{pid}.*")] == [f"{pid}.png"]
    with pytest.raises(ValueError):
        db.set_product_photo(conn, pid, tmp_path / "없음.jpg")
    txt = tmp_path / "a.txt"; txt.write_text("x")
    with pytest.raises(ValueError):
        db.set_product_photo(conn, pid, txt)
    with conn:
        db.clear_product_photo(conn, pid)
    assert not list(db.images_dir().glob(f"{pid}.*"))
    assert conn.execute("SELECT image_url FROM products WHERE id = ?", (pid,)).fetchone()[0] is None


def test_supplier_photo_column(conn):
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        sid = db.upsert_supplier(conn, "Co", "China")
        db.link_supplier(conn, pid, sid, image_url="https://s/x.jpg")
    assert conn.execute("SELECT image_url FROM product_suppliers").fetchone()[0] == "https://s/x.jpg"


def test_pipeline_shows_connected_agent_and_its_page(conn, tmp_path):
    with db.agent_run(conn, "sourcing") as run:
        with conn:
            pid = db.upsert_product(conn, "키캡", run.id, stage="sourcing")
            sid = db.upsert_supplier(conn, "Co", "China")
            db.link_supplier(conn, pid, sid, run.id)
    html = dashboard.build(tmp_path / "d.html", conn).read_text(encoding="utf-8")
    nav = html[html.index('<nav class="pipe-nav">'):html.index("</nav>")]
    assert 'class="step ok" href="#a2"' in nav                 # 2번 연결됨
    assert 'class="step off" href="#a3"' in nav                # 3번(통관·규제) 미연결
    page2 = html[html.index('id="p-a2"'):html.index('id="p-a3"')]
    assert "품목별 공급처" in page2 and "키캡" in page2


def test_supplier_contacts_links_and_v4_score(conn, tmp_path):
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        a = db.upsert_supplier(conn, "Safe Co", "China", trust="하", trust_reason="58.0점 (C) — 예전 저장 문장",
                               url="https://safe.example.com/", platform_badge="Alibaba 평점 4.8/5",
                               contact_email="a@safe.example.com", contact_other="WhatsApp +86 123")
        b = db.upsert_supplier(conn, "Evil Co", "China", url="javascript:alert(1)")
        db.link_supplier(conn, pid, a, is_top_pick=1, score=54.0, grade="C")   # v4 칸이 문장보다 우선
        db.link_supplier(conn, pid, b)
    html = dashboard.build(tmp_path / "s.html", conn, refs=False).read_text(encoding="utf-8")
    assert 'href="https://safe.example.com/"' in html and "업체 페이지" in html
    assert 'href="javascript' not in html                     # http(s)가 아닌 주소는 링크로 걸지 않고
    assert 'title="링크로 열지 않음 (http/https 주소가 아님): javascript:alert(1)"' in html   # 마우스를 올리면 주소 표시
    assert "Alibaba 평점 4.8/5" in html and "WhatsApp +86 123" in html and "a@safe.example.com" in html
    assert "54점 (C)" in html and "예전 저장 문장" in html and "58.0점" not in html


def test_supplier_moq_badges(conn, tmp_path):
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        for company, kw in [("Small Co", {"moq": "1 piece"}),
                            ("Big Co", {"moq": "25 pcs (A형), 500 pcs", "purchase_mode": "retail"}),
                            ("Quiet Co", {}),
                            ("Oem Co", {"moq": "3000 pcs", "purchase_mode": "oem"})]:
            db.link_supplier(conn, pid, db.upsert_supplier(conn, company, "China"), **kw)
    html = dashboard.build(tmp_path / "m.html", conn, refs=False).read_text(encoding="utf-8")
    assert '<span class="moq ok" title="기준: 50개 이하 · 기본값 · MOQ 1개">소량 OK</span>' in html
    assert '<span class="moq over" title="기준: 10개 이하 (retail) · 기본값 · MOQ 25개">MOQ 초과</span>' in html
    assert ">MOQ 미확인</span>" in html and ">OEM · 제한 없음</span>" in html


def test_channel_ranking_above_supplier_table(conn, tmp_path):
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        db.save_channels(conn, pid, None, [
            {"rank": 1, "key": "aliexpress", "site": "AliExpress", "search_url": "https://ali.example.com/w/keycap",
             "total": 82.5, "typical_price": "USD 3.2-4.5", "min_qty": "1 piece", "summary": "1개부터", "assessed": True},
            {"rank": 2, "key": "evil", "site": "Evil <Site>", "search_url": "javascript:alert(1)", "total": 40,
             "assessed": False}], purchase_mode="small_batch")
        db.link_supplier(conn, pid, db.upsert_supplier(conn, "Keys Co", "China"), moq="1 piece")
    html = dashboard.build(tmp_path / "c.html", conn, refs=False).read_text(encoding="utf-8")
    card = html[html.index(f'id="prod-{pid}"'):]
    assert card.index("구매 사이트 순위") < card.index("Keys Co")          # 공급처 표 위
    assert "(small_batch 기준)" in card
    assert 'href="https://ali.example.com/w/keycap"' in card and "82.5점" in card and "USD 3.2-4.5" in card
    assert 'title="1개부터 · 최소 1 piece"' in card
    assert 'href="javascript' not in html and "단가 미확인" in card
    assert '<span class="nolink" title="링크로 열지 않음 (http/https 주소가 아님): javascript:alert(1)">Evil &lt;Site&gt;</span>' in card


def test_no_channel_strip_without_data(conn, tmp_path):
    with conn:
        db.upsert_product(conn, "키캡", stage="sourcing")
    html = dashboard.build(tmp_path / "n.html", conn, refs=False).read_text(encoding="utf-8")
    assert "구매 사이트 순위" not in html


def test_moved_on_products_listed_under_candidates(conn, tmp_path):
    with conn:
        stay = db.upsert_product(conn, "후보 그대로")
        moved = db.upsert_product(conn, "AI안경")
        db.set_stage(conn, moved, "sourcing", note="사람이 선정")
        gone = db.upsert_product(conn, "중단 품목")
        db.set_stage(conn, gone, "dropped")
    html = dashboard.build(tmp_path / "mv.html", conn, refs=False).read_text(encoding="utf-8")
    page = html[html.index('id="p-a1"'):html.index('id="p-a2"')]          # 시장조사·아이템 발굴 화면
    sec = page[page.index("다음 단계로 넘어간 품목"):]
    assert f'<a href="#p{moved}">AI안경</a>' in sec and "공급처" in sec    # 지금 단계와 카드로 가는 링크
    assert "후보 그대로" not in sec and "중단 품목" not in sec[:sec.index("</section>")]
    assert f'id="prod-{moved}"' in html                                    # 링크가 가리키는 카드가 있음


def test_moved_on_hidden_when_nothing_moved(conn, tmp_path):
    with conn:
        db.upsert_product(conn, "후보")
    html = dashboard.build(tmp_path / "mv0.html", conn, refs=False).read_text(encoding="utf-8")
    assert "다음 단계로 넘어간 품목" not in html


def test_suppliers_grouped_by_purchase_mode(conn, tmp_path, monkeypatch):
    monkeypatch.setitem(dashboard.SERVER, "on", True)
    with conn:
        pid = db.upsert_product(conn, "AI안경", stage="sourcing")
        db.link_supplier(conn, pid, db.upsert_supplier(conn, "Retail Shop", "China"), purchase_mode="retail", moq="1 piece")
        db.link_supplier(conn, pid, db.upsert_supplier(conn, "Bulk Co", "China"), purchase_mode="small_batch", moq="50 pcs")
        db.link_supplier(conn, pid, db.upsert_supplier(conn, "Old Co", "China"))                   # 예전 저장분
        db.save_channels(conn, pid, None, [{"rank": 1, "key": "aliexpress", "site": "AliExpress", "total": 80}],
                         purchase_mode="retail")
    html = dashboard.render(conn, server=True)
    card = html[html.index(f'id="prod-{pid}"'):]
    card = card[:card.index('<div class="card"', 10)] if '<div class="card"' in card[10:] else card
    i_r, i_s, i_o = card.index(">구매대행 <"), card.index(">소량 사입 <"), card.index(">구매 방식 미기록 <")
    assert i_r < card.index("Retail Shop") < i_s < card.index("Bulk Co") < i_o < card.index("Old Co")
    assert card.index("AliExpress") < i_s                                    # 구매대행 사이트 순위는 구매대행 묶음에
    assert 'data-mode="retail"' in card and 'data-mode="small_batch"' in card
    assert "구매대행용 다시 찾기" in card and "소량 사입용 다시 찾기" in card
    assert 'data-mode="oem" data-mode-label="대량 사입·OEM">대량 사입·OEM용 찾기</button>' in card   # 아직 안 찾음 → 강조
    acts = card[card.index('<span class="acts">'):]
    assert acts.index('select class="country"') < acts.index('data-mode="retail"') < acts.index("</span>")   # 같은 묶음


def test_candidate_has_two_mode_buttons(conn, monkeypatch):
    monkeypatch.setitem(dashboard.SERVER, "on", True)
    with conn:
        db.upsert_product(conn, "후보")
    html = dashboard.render(conn, server=True)
    assert ">구매대행용 찾기</button>" in html and ">소량 사입용 찾기</button>" in html
    assert 'data-mode="oem"' not in html                                            # 후보 카드엔 대량 사입·OEM 없음
    assert ('<option value="China">중국</option><option value="Japan">일본</option>'
            '<option value="USA">미국</option>') in html                                   # 추천 없으면 중국(첫 칸)


def test_country_select_preselects_recommendation(conn, monkeypatch):
    monkeypatch.setitem(dashboard.SERVER, "on", True)
    with conn:
        db.upsert_product(conn, "빈티지 필름 카메라", source_country="USA", source_country_reason="eBay 중고 매물이 많음")
    html = dashboard.render(conn, server=True)
    assert '<option value="USA" selected>미국 (추천)</option>' in html
    assert "1번 추천: 미국 — eBay 중고 매물이 많음" in html


def test_sourcing_error_prefix_hidden(conn):
    assert dashboard.run_error("SourcingError: 검색 결과가 없습니다. 검색어를 바꿔 보세요") == "검색 결과가 없습니다. 검색어를 바꿔 보세요"
    assert dashboard.run_error("OSError: [Errno 2] 없음") == "OSError: [Errno 2] 없음"      # 다른 예외는 그대로
    assert dashboard.run_error(None) == ""
    with conn:
        conn.execute("INSERT INTO agent_runs (agent, status, error) VALUES ('sourcing', 'failed', 'SourcingError: 키가 없습니다')")
    html = dashboard.runs(conn, "sourcing")
    assert "키가 없습니다" in html and "SourcingError" not in html


def test_compliance_before_documents():
    # 통관·규제(KC 인증·표시) 3번 → 원가·수익 4번 → 협상 5번 → 무역서류 6번
    order = [a for a, *_ in dashboard.AGENT_PAGES]
    assert order[2:6] == ["compliance", "costing", "negotiation", "documents"]
    assert [n for _, n, _, _ in dashboard.AGENT_PAGES] == list(range(1, 9))
    assert dashboard.AGENT_LABEL["compliance"] == "3 통관·규제" and dashboard.AGENT_LABEL["costing"] == "4 원가"
    assert dashboard.AGENT_LABEL["negotiation"] == "5 협상" and dashboard.AGENT_LABEL["documents"] == "6 서류"


@pytest.mark.skipif(not shutil.which("node"), reason="node 없음")
@pytest.mark.parametrize("server", [False, True])
def test_page_script_has_no_syntax_error(conn, tmp_path, server):
    # 스크립트 한 곳만 문법이 틀려도 화면 전환·버튼이 전부 멈춤 (예: 파이썬 문자열의 \n 이 JS 문자열 안에서 진짜 줄바꿈이 됨)
    html = dashboard.render(conn, server=server)
    js = tmp_path / "page.js"
    js.write_text("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


def test_compliance_page_shows_latest_check(conn, tmp_path):
    report = tmp_path / "hub_1_report.md"
    report.write_text("# 보고서", encoding="utf-8")
    with conn:
        pid = db.upsert_product(conn, "전기 주전자", stage="sourcing")
        other = db.upsert_product(conn, "판정 없는 품목", stage="sourcing")
        db.save_compliance_check(conn, pid, None, "go", "2026-11-18", verdict_reasons=["옛 판정"])
        db.save_compliance_check(conn, pid, None, "hold", "2026-11-19", used_llm=True, report_path=str(report),
                                 verdict_reasons=["KC 안전인증 필요 <확인>"], unknown_flags=["battery"],
                                 required_certs=[{"id": "kc_safety", "title": "KC 안전인증", "timing": "발주 전"}],
                                 hs_candidates=[{"code": "8516.79", "description": "전열기기", "reason": "-"}])
    html = dashboard.compliance_rows(conn)
    row = html[html.index("전기 주전자"):html.index("판정 없는 품목")]
    assert '<span class="bad"><b>보류</b></span>' in row and "옛 판정" not in row     # 최신 판정만
    assert "KC 안전인증 필요 &lt;확인&gt;" in row and "모르는 항목 1개: battery" in row
    assert "2026-11-19 · LLM 분류" in row and 'target="_blank" rel="noopener">보고서</a>' in row
    assert "KC 안전인증" in row and "8516.79" in row and "후보" in row
    assert "판정 없음" in html[html.index("판정 없는 품목"):]
    with conn:
        conn.execute("UPDATE products SET hs_code = '8516.71' WHERE id = ?", (pid,))
    row = dashboard.compliance_rows(conn)
    assert "8516.71" in row and "확정" in row and "8516.79" not in row               # 사람이 확정한 HS 코드가 우선


def test_sourcing_card_shows_discover_summary(conn):
    with conn:
        pid = db.upsert_product(conn, "빈티지 카메라", stage="sourcing", reason="수요 증가 <급등> · 도매 $30~40",
                                regulations="전파인증 필요", risk="중고 품질 편차", est_retail_krw=189000,
                                source_country="Japan", source_country_reason="일본 중고 시장이 큼",
                                ref_url="https://example.com/item?a=1&b=2")
        conn.execute("INSERT INTO product_scores (product_id, total, est_fob_usd) VALUES (?, 60, 20)", (pid,))
        conn.execute("INSERT INTO product_scores (product_id, total, est_fob_usd) VALUES (?, 72, 35.5)", (pid,))
        bad = db.upsert_product(conn, "위험 링크", stage="sourcing", ref_url="javascript:alert(1)")
        cand = db.upsert_product(conn, "후보 품목", reason="후보 이유")
    html, _ = dashboard.products_in_progress(conn)
    card = html[html.index(f'id="prod-{pid}"'):html.index(f'id="prod-{bad}"')]
    assert re.search(r"<summary>1번 시장조사 요약 · 72점 · \d{4}-\d{2}-\d{2}</summary>", card)   # 최신 점수·FOB·조사일 (날짜만)
    assert "수요 증가 &lt;급등&gt; · 도매 $30~40" in card and "$35.50" in card and "$20.00" not in card
    assert "189,000원" in card and "일본" in card and "일본 중고 시장이 큼" in card
    assert "전파인증 필요" in card and "중고 품질 편차" in card
    assert 'href="https://example.com/item?a=1&amp;b=2"' in card
    rest = html[html.index(f'id="prod-{bad}"'):]
    assert 'href="javascript' not in rest and "참고 상품" in rest                     # http(s) 아니면 링크 안 걸음
    assert "후보 이유" not in html                                                    # 후보는 2번 카드가 아님


def test_source_page_link_shown(conn):
    with conn:
        db.upsert_product(conn, "키캡 세트", ref_url="https://www.coupang.com/vp/products/1",
                          source_page_url="https://detail.1688.com/offer/123.html")
    html = dashboard.render(conn)
    assert 'href="https://detail.1688.com/offer/123.html" target="_blank" rel="noopener noreferrer">해외 판매 페이지</a>' in html


def test_compliance_page_broker_request_and_basis_supplier(conn, tmp_path):
    hub = tmp_path / "output" / "hub"
    hub.mkdir(parents=True)
    (hub / "hub_1_report.md").write_text("#", encoding="utf-8")
    with conn:
        a = db.upsert_product(conn, "벽선반", stage="sourcing")
        b = db.upsert_product(conn, "키캡", stage="negotiating")
        top = db.link_supplier(conn, a, db.upsert_supplier(conn, "Top Co", "China"), is_top_pick=1, score=60)
        sel = db.link_supplier(conn, a, db.upsert_supplier(conn, "Picked Co", "China"), score=50)
        db.link_supplier(conn, a, db.upsert_supplier(conn, "Gone Co", "China"), score=99)
        conn.execute("UPDATE product_suppliers SET status = 'rejected' WHERE score = 99")
        db.save_compliance_check(conn, a, None, "go", "2026-11-20", report_path=str(hub / "hub_1_report.md"),
                                 supplier_questions=[])
        db.save_compliance_check(conn, b, None, "hold", "2026-11-20",
                                 supplier_questions=[{"flag": "wireless", "question_ko": "무선?"}])
    html = dashboard.render(conn)
    assert "관세사 문의서" not in html                                              # 파일이 없으면 링크 없음
    assert "판정 기준 공급처: Top Co" in html and "Gone Co" not in html.split("판정 기준 공급처")[1][:40]
    (hub / "broker_request.html").write_text("<p>문의서</p>", encoding="utf-8")
    with conn:
        conn.execute("UPDATE product_suppliers SET status = 'selected' WHERE id = ?", (sel,))
        db.merge_supplier_specs(conn, sel, {"wireless": False}, [{"claim": "무선 없음", "message_id": 3}])
    html = dashboard.render(conn)
    assert "broker_request.html" in html and "보낼 수 있음 <b>1</b>개" in html        # 키캡은 공급처 질문이 남음
    # 판정 때 기록된 공급처(Top Co)와 지금 대표(Picked Co)가 다름 → 경고
    assert "판정 기준 공급처: Top Co</div>" in html
    assert "⚠ 공급처가 바뀜 — 지금 기준: Picked Co (선정) · 공급처 답 있음 · 다시 점검 필요" in html
    with conn:                                                                      # 다시 점검 → 경고 사라짐
        db.save_compliance_check(conn, a, None, "go", "2026-11-21", supplier_questions=[])
    html = dashboard.render(conn)
    assert "판정 기준 공급처: Picked Co (선정) · 공급처 답 있음" in html and "공급처가 바뀜" not in html
    with conn:                                                                      # 기록 없는 예전 판정: 지금 대표 표시
        conn.execute("UPDATE compliance_checks SET basis_ps_id = NULL")
    assert "판정 기준 공급처: Picked Co (선정)" in dashboard.render(conn)


def test_task_detail_report_path_becomes_link(tmp_path):
    rep = tmp_path / "hub_1_ps2_report.html"
    rep.write_text("x", encoding="utf-8")
    cell = dashboard.detail_cell(f"5번 협상 기준을 자동으로 넣었습니다. | 보고서: {rep}")
    assert "보고서 열기" in cell and "<a href=" in cell and str(rep) not in cell.split("<a")[0]
    assert dashboard.detail_cell("보고서 없는 글") == "보고서 없는 글"
    assert "파일 없음" in dashboard.detail_cell(f"글 | 보고서: {tmp_path / 'none.html'}")


def test_costing_page_links_supplier_report(conn, tmp_path):
    hub_dir = tmp_path / "output" / "hub"
    with db.agent_run(conn, "costing") as run:
        run.report_dir = str(hub_dir / "runs" / str(run.id))
    with conn:
        pid = db.upsert_product(conn, "EVA 케이스", stage="negotiating")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=3.0)
        conn.execute("UPDATE product_suppliers SET est_landed_krw = 5000, est_margin_pct = 30 WHERE id = ?", (ps,))
    hub_dir.mkdir(parents=True)
    (hub_dir / f"hub_{pid}_ps{ps}_report.html").write_text("x", encoding="utf-8")
    html = dashboard.costing_rows(conn)
    assert f"hub_{pid}_ps{ps}_report.html" in html and "<a href=" in html


def test_costing_page_shows_supplier_and_listing_links(conn):
    with conn:
        pid = db.upsert_product(conn, "키링", stage="sourcing")
        sid = db.upsert_supplier(conn, "Koi ZH Store", "China", url="https://koi.aliexpress.com/store/1")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=1.6, is_top_pick=1,
                              evidence_json=[{"claim": "x", "url": "https://koi.aliexpress.com/store/1"},
                                        {"claim": "판매가", "url": "https://www.aliexpress.com/item/1.html"}])
        conn.execute("UPDATE product_suppliers SET est_landed_krw = 3000, est_margin_pct = -80 WHERE id = ?", (ps,))
    html = dashboard.costing_rows(conn)
    assert 'href="https://koi.aliexpress.com/store/1"' in html and "업체 페이지 ↗" in html
    assert 'href="https://www.aliexpress.com/item/1.html"' in html and "판매 페이지 ↗" in html
    assert "추천" in html and "China" in html
    with conn:
        conn.execute("UPDATE product_suppliers SET trade_terms_json = ? WHERE id = ?",
                     ('{"price_basis": {"variant": "4구 LED형", "kind": "retail", "includes_shipping": true}}', ps))
    html = dashboard.costing_rows(conn)
    assert "4구 LED형 · 소매가 · 배송비 포함" in html
    assert "변형 혼합" not in html
    with conn:
        conn.execute("UPDATE products SET notes = '[변형 혼합] #66·#67로 나눔' WHERE id = ?", (pid,))
    assert "-80% 변형 혼합" in dashboard.costing_rows(conn)
    with conn:                                                   # 2번이 나뉜 품목으로 옮긴 줄 → 원가 표에서 빠짐
        conn.execute("UPDATE product_suppliers SET status = 'rejected', reject_reason = '→ #66으로 옮김' WHERE id = ?", (ps,))
    assert "Koi ZH Store" not in dashboard.costing_rows(conn)


def test_competitive_evidence_skipped_for_listing_link_and_shown_as_badge(conn):
    with conn:
        pid = db.upsert_product(conn, "마우스", stage="sourcing")
        sid = db.upsert_supplier(conn, "Mouse Co", "China", url="https://mouse.example.com")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=5, evidence_json=[
            {"claim": "경쟁 대비 요약 (우위 2 · 열위 1): 무게 가벼움", "url": "https://rival.example.com/m", "type": "competitive"},
            {"claim": "경쟁 비교 [열위] 폴링레이트: 1000Hz vs 8000Hz", "url": "https://rival.example.com/m", "type": "competitive"},
            {"claim": "판매가", "url": "https://mouse.example.com/item/9"}])
        conn.execute("UPDATE product_suppliers SET est_landed_krw = 3000 WHERE id = ?", (ps,))
    html = dashboard.costing_rows(conn)
    assert 'href="https://mouse.example.com/item/9"' in html and "rival.example.com" not in html
    badge = dashboard.competitive_badge(conn.execute("SELECT evidence_json FROM product_suppliers WHERE id = ?", (ps,)).fetchone()[0])
    assert "경쟁 대비 우위 2 · 열위 1" in badge and 'class="small ok"' in badge
    assert "폴링레이트" in badge and 'href="https://rival.example.com/m"' in badge
    assert "경쟁 대비 우위 2 · 열위 1" in dashboard.products_in_progress(conn)[0]
    assert dashboard.competitive_badge(None) == "" and dashboard.competitive_badge('[{"claim": "x"}]') == ""


def test_pending_task_text_ending_with_path_not_treated_as_path(conn, tmp_path):
    rep = tmp_path / "broker_request.html"
    rep.write_text("x", encoding="utf-8")
    with conn:
        db.add_task(conn, "other", "관세사 문의", detail=f"물을 것 5개 | 문의서: {rep}")
    html, n = dashboard.pending_tasks(conn)
    assert n == 1 and "문의서 열기" in html and "파일 없음" not in html and "물을 것 5개" in html


def test_costing_page_send_button_only_in_server_mode(conn, tmp_path, monkeypatch):
    import json as _json
    hub_dir = tmp_path / "output" / "hub"
    with db.agent_run(conn, "costing") as run:
        run.report_dir = str(hub_dir / "runs" / str(run.id))
    with conn:
        pid = db.upsert_product(conn, "EVA 케이스", stage="negotiating")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=3.0)
        conn.execute("UPDATE product_suppliers SET est_landed_krw = 5000, est_margin_pct = 30 WHERE id = ?", (ps,))
    hub_dir.mkdir(parents=True)
    (hub_dir / f"hub_{pid}_ps{ps}_handoff.json").write_text("{}", encoding="utf-8")
    assert 'data-act="costneg"' not in dashboard.costing_rows(conn)          # 파일로 연 대시보드엔 버튼 없음
    monkeypatch.setitem(dashboard.SERVER, "on", True)
    html = dashboard.costing_rows(conn)
    assert f'data-act="costneg" data-id="{ps}"' in html and "5번 기준" not in html
    (hub_dir / "negotiation_applied.json").write_text(
        _json.dumps({str(pid): {"target": 2.5, "max": 2.9, "ps_id": ps}}), encoding="utf-8")
    html = dashboard.costing_rows(conn)
    assert "✓ 5번 기준" in html and "2.9" not in html                         # 가격은 화면에 안 나옴


def test_result_link_scroll_not_undone_after_reload():
    js = dashboard.SEARCH_JS
    assert js.index('history.scrollRestoration = "manual"') < js.index("show();")   # 새로 고침 뒤 예전 위치로 안 되돌림
    assert 'window.addEventListener("load", function () { if (location.hash) show(); });' in js


def test_long_card_scrolls_to_title_below_sticky_bar():
    js = dashboard.SEARCH_JS
    assert 'row.scrollIntoView({block: row.offsetHeight > innerHeight * 0.8 ? "start" : "center"});' in js
    assert 'row.style.scrollMarginTop' in js and 'document.querySelector(".top")' in js


def test_compliance_row_costing_button_and_costing_grouped(conn, monkeypatch):
    with conn:
        a = db.upsert_product(conn, "벽선반", stage="sourcing")
        b = db.upsert_product(conn, "키캡", stage="sourcing")
        n = db.upsert_product(conn, "판정 없음", stage="sourcing")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        sid2 = db.upsert_supplier(conn, "Bolt", "China")
        pa = db.link_supplier(conn, a, sid, unit_price_usd=3.0)
        pa2 = db.link_supplier(conn, a, sid2, unit_price_usd=2.0)
        db.link_supplier(conn, b, sid)                                         # 가격 없음
        db.link_supplier(conn, n, sid, unit_price_usd=1.0)
        db.save_compliance_check(conn, a, None, "go", "2026-11-24")
        db.save_compliance_check(conn, b, None, "go", "2026-11-24")
        conn.execute("UPDATE product_suppliers SET est_landed_krw = 5000, est_margin_pct = 10 WHERE id = ?", (pa,))
        conn.execute("UPDATE product_suppliers SET est_landed_krw = 4000, est_margin_pct = NULL WHERE id = ?", (pa2,))
        conn.execute("UPDATE products SET est_retail_krw = NULL WHERE id = ?", (a,))
    monkeypatch.setitem(dashboard.SERVER, "on", True)
    html = dashboard.compliance_rows(conn)
    assert f'data-act="costing" data-id="{a}"' in html
    assert f'data-act="costing" data-id="{b}"' not in html and 'disabled title="가격(웹 단가·견적) 있는 공급처 줄 없음"' in html
    assert f'data-act="costing" data-id="{n}"' not in html                    # 판정 없는 품목엔 버튼 없음
    cost = dashboard.costing_rows(conn)
    assert cost.count(f'id="cost-{a}"') == 1 and "판매가 없음" in cost
    assert cost.index("Allwin") < cost.index("Bolt")                           # 같은 품목끼리 묶고 마진 있는 줄 먼저
    assert 'b.dataset.act === "costing"' in dashboard.SERVER_JS and "(ps|cost)-" in dashboard.SEARCH_JS


def test_demand_trend_short_text():
    si = {"cid": "50000008", "category": "생활/건강", "keyword": "실리콘 찜기", "months": ["2025-11", "2025-12"],
          "series": [40, 100], "change_3m": 24, "trend": "오름",
          "seasonal": {"is_seasonal": True, "peak_month": "2025-12", "peak_to_mean": 4.8}}
    t = dashboard.demand_trend({"shop_category": "생활/건강", "shopping_insight": si})
    assert '<span class="up">오름 +24%</span> · 계절 12월' in t and "쇼핑인사이트" in t and "25-12:100" in t
    t = dashboard.demand_trend({"shopping_insight": {**si, "trend": None, "change_3m": None,
                                                     "seasonal": {"is_seasonal": True, "peak_month": "2026-01"}}})
    assert "최근 클릭 적음 · 계절 1월" in t
    assert "데이터 적음" in dashboard.demand_trend({"shopping_insight": {**si, "trend": None, "seasonal": None}})
    assert "분야 미정" in dashboard.demand_trend({"volume_idx": 3, "shop_category": None})
    assert dashboard.demand_trend({"volume_idx": 3}) == '<span class="muted">–</span>'        # 기존 품목 (칸 없음)
    assert dashboard.demand_trend({}) == '<span class="muted">–</span>'


def test_progress_cards_show_demand_trend(conn):
    import json
    si = {"category": "생활/건강", "keyword": "찜기", "months": [], "series": [], "change_3m": 24, "trend": "오름",
          "seasonal": None}
    with conn:
        a = db.upsert_product(conn, "실리콘 찜기", stage="sourcing")
        b = db.upsert_product(conn, "예전 품목", stage="negotiating")
        conn.execute("INSERT INTO product_scores (product_id, naver_json) VALUES (?, ?)",
                     (a, json.dumps({"volume_idx": 3, "shop_category": "생활/건강", "shopping_insight": si})))
    assert "수요 추세" in dashboard.demand_tag(conn, a) and "오름 +24%" in dashboard.demand_tag(conn, a)
    assert dashboard.demand_tag(conn, b) == ""                                       # 값 없으면 표시 안 함
    html, _ = dashboard.products_in_progress(conn)
    assert html.count("trend-tag") == 1


def test_supplier_table_wider_score_and_price(conn):
    with conn:
        pid = db.upsert_product(conn, "LED 조명", stage="sourcing")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        db.link_supplier(conn, pid, sid, price_note="USD 0.53-0.79 — 4구 LED형 (변형 이름이 길어도 보임)")
    html, _ = dashboard.products_in_progress(conn)
    assert '<table class="tight sup">' in html and "4구 LED형 (변형 이름이" in html
    assert dashboard.SUPPLIER_HEADS[2] == "신뢰도·점수" and dashboard.SUPPLIER_HEADS[5] == "단가"   # nth-child(3)·(6)
    assert "table.sup td:nth-child(3)" in dashboard.CSS and "table.sup td:nth-child(6)" in dashboard.CSS


def test_research_dates_candidate_card_and_suppliers(conn):
    import json
    with conn:
        a = db.upsert_product(conn, "찜기", stage="candidate")
        conn.execute("UPDATE products SET est_retail_krw = 13980, notes = ? WHERE id = ?",
                     ("[변형 혼합] [1번 시세 2026-11-24] 판매가 확인\n사람 메모", a))
        conn.execute("INSERT INTO product_scores (product_id, total, naver_json, created_at) VALUES (?, 70, ?, '2026-11-19T10:00:00')",
                     (a, json.dumps({"volume_idx": 3, "checked_at": "2026-11-24T09:00:00",
                                     "shopping_insight": {"trend": "오름", "change_3m": 5, "checked_at": "2026-11-23"}})))
    html, _ = dashboard.candidate_rows(conn)
    assert '>2026-11-19</div>' in html and 'title="1번 조사·평가일 2026-11-19"' in html and " 조사</div>" not in html
    assert '시세 확인 2026-11-24">11-24</div>' in html and 'title="2026-11-24"' in html and "2026-11-23" in html
    assert "네이버 조회" not in html and ">시세 " not in html
    assert dashboard.price_checked_on("메모만") is None and dashboard.price_date_tag(None) == ""

    with conn:
        conn.execute("UPDATE products SET stage = 'sourcing' WHERE id = ?", (a,))
        old = conn.execute("INSERT INTO agent_runs (agent, status, started_at, finished_at) VALUES "
                           "('sourcing', 'succeeded', '2026-11-20T09:00:00', '2026-11-20T09:10:00')").lastrowid
        new = conn.execute("INSERT INTO agent_runs (agent, status, started_at) VALUES "
                           "('sourcing', 'succeeded', '2026-11-24T09:00:00')").lastrowid   # finished_at 없음 → started_at
        s1 = db.upsert_supplier(conn, "Old Co", "China")
        s2 = db.upsert_supplier(conn, "New Co", "China")
        p1 = db.link_supplier(conn, a, s1, unit_price_usd=1.0)
        p2 = db.link_supplier(conn, a, s2, unit_price_usd=1.0)
        conn.execute("UPDATE product_suppliers SET run_id = ?, purchase_mode = 'small_batch' WHERE id = ?", (old, p1))
        conn.execute("UPDATE product_suppliers SET run_id = ?, purchase_mode = 'small_batch' WHERE id = ?", (new, p2))
    card, _ = dashboard.products_in_progress(conn)
    assert "· 최근 조사 11/24</span>" in card and "(11-24)</span>" in card and "· 2026-11-19</summary>" in card
    assert f'<div class="small muted" title="2번 조사 2026-11-20T09:10 · 실행 #{old} (예전 조사에서 나온 업체)">조사 11/20 (#{old})</div>' in card
    assert f'<div class="small" title="2번 조사 2026-11-24T09:00 · 실행 #{new}">조사 11/24 (#{new})</div>' in card


def test_clip_shows_full_view_link_extra_html_and_split_items():
    assert dashboard.clip("짧은 글", 10, '<a>리포트 ↗</a>') == "짧은 글"
    html = dashboard.clip("가" * 20, 10, '<a>리포트 ↗</a>')
    assert 'class="clip"' in html and "전체 보기 ▸" in html and "접기 ▾" in html and html.endswith(" <a>리포트 ↗</a>")
    flags = dashboard.clip("결제 조건 불명확, 샘플 미확인, 인증 없음", 10, sep=", ")
    assert "<div>· 결제 조건 불명확</div><div>· 샘플 미확인</div><div>· 인증 없음</div>" in flags


def test_link_fragment_for_candidate_report_row(tmp_path):
    f = tmp_path / "candidates.html"
    f.write_text("x", encoding="utf-8")
    assert dashboard.link(f, "점수표", "c81").endswith('#c81" target="_blank">점수표</a>')
    assert "#" not in dashboard.link(f, "점수표")


def test_candidate_charts_map_bars_sparks_and_counts(conn):
    def score(pid, total, margin, nv):
        conn.execute("INSERT INTO product_scores (product_id, total, demand, margin, competition, regulation, logistics, naver_json)"
                     " VALUES (?, ?, 4, ?, 3, 3, 4, ?)", (pid, total, margin, json.dumps(nv)))
    si = {"trend": "오름", "change_3m": 35, "months": [f"2025-{m:02d}" for m in range(1, 13)], "series": list(range(1, 13)),
          "seasonal": {"is_seasonal": True, "peak_month": "2025-12"}, "keyword": "선반"}
    with conn:
        a = db.upsert_product(conn, "벽선반", category="주방", source_country="China")
        b = db.upsert_product(conn, "우산꽂이", category="주방")
        c = db.upsert_product(conn, "섞인 품목", notes="[변형 혼합] #1·#2로 나눔")
        d = db.upsert_product(conn, "진행 품목", stage="sourcing")
        score(a, 84, 5, {"volume_idx": 0, "growth_3m": 12, "yoy": -3, "shopping_insight": si})
        score(b, 60, 2, {"volume_idx": None})
        score(c, 90, 5, {"volume_idx": 5, "shopping_insight": si})
        score(d, 95, 5, {"volume_idx": 5, "shopping_insight": si})
    html = charts.candidate_charts(conn)
    assert html.count('<circle class="ch-pt') == 1 and f'href="#p{a}"' in html and "ch-up" in html
    assert "총점 84 · 검색량 0 · 3개월 +12% · 전년比 -3% · 쇼핑 추세 오름" in html
    assert f'지도에서 뺀 품목 1개: <a href="#p{b}">#{b}</a>' in html
    assert "섞인 품목" not in html and "진행 품목" not in html
    assert f"#{a} 수요 4/5 × 30 = 24.0점" in html and "ch-peak" in html and "계절 12월" in html
    assert "분류별 후보 수" in html and "중국" in html
    page = dashboard.render(conn)
    a1 = page[page.index('id="p-a1"'):]
    assert a1.index("후보 품목") < a1.index("후보 한눈에 보기") < a1.index("실행 기록")


def test_price_krw_cell_range_currency_and_tiers(monkeypatch):
    from trade_hub import fx
    monkeypatch.setattr(fx, "rates", lambda: {"KRW": 1400, "CNY": 7.0})
    usd = {"unit_price_usd": 11.2, "trade_terms_json": json.dumps({"price_basis": {"currency": "USD", "low": 4.42, "high": 11.2}})}
    assert "<b>약 6,200~15,700원</b>" in dashboard.price_krw_cell(usd)
    cny = {"unit_price_usd": 10, "trade_terms_json": json.dumps({"price_basis": {"currency": "CNY", "low": 35, "high": 35,
                                                                                    "fx_assumed": 7.0, "tiers": [{}, {}]}})}
    out = dashboard.price_krw_cell(cny)
    assert "<b>약 7,000원</b>" in out and "수량 구간 2개" in out and "1 USD = 7 CNY" in out
    assert "<b>약 14,000원</b>" in dashboard.price_krw_cell({"unit_price_usd": 10, "trade_terms_json": None})
    assert dashboard.price_krw_cell({"unit_price_usd": None, "trade_terms_json": "{}"}) == ""
    monkeypatch.setattr(fx, "rates", lambda: None)     # 환율 모르는 통화 → unit_price_usd
    jpy = {"unit_price_usd": 20, "trade_terms_json": json.dumps({"price_basis": {"currency": "JPY", "low": 3000}})}
    assert "<b>약 28,000원</b>" in dashboard.price_krw_cell(jpy)


def test_margin_lines_under_recommended_price():
    m = {"target_krw": 109400, "margin_pct": 25.0, "profit_krw": 24870, "ad_pct": 10, "ad_krw": 9946,
         "fee_krw": 4980, "ship_krw": 3000, "return_krw": 180}
    out = dashboard.margin_lines(m, {})
    assert "마진 25% (개당 +24,900원) · 광고비 10% (9,900원)" in out and "수수료 5,000원 · 택배 3,000원 · 반품 200원" in out
    old = dashboard.margin_lines({"target_krw": 38100}, {"target_margin_pct": 25.0})
    assert "마진 25%</div>" in old and "광고비" not in old and "수수료" not in old
    assert dashboard.margin_lines({"target_krw": 1}, {}) == ""
