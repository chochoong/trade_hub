import pytest

from trade_hub import dashboard, db, naver


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
    acts = card[card.index('<span class="acts">'):]
    assert acts.index('select class="country"') < acts.index('data-mode="retail"') < acts.index("</span>")   # 같은 묶음


def test_candidate_has_two_mode_buttons(conn, monkeypatch):
    monkeypatch.setitem(dashboard.SERVER, "on", True)
    with conn:
        db.upsert_product(conn, "후보")
    html = dashboard.render(conn, server=True)
    assert ">구매대행용 찾기</button>" in html and ">소량 사입용 찾기</button>" in html
    assert '<option value="China">중국</option><option value="Japan">일본</option>' in html   # 기본 중국


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
