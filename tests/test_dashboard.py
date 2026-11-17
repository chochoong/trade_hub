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
    assert 'class="step off" href="#a3"' in nav                # 3번 미연결
    page2 = html[html.index('id="p-a2"'):html.index('id="p-a3"')]
    assert "품목별 공급처" in page2 and "키캡" in page2
