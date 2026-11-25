"""스키마 v18: documents 표(6번 무역서류) · quotes.incoterm_place · 6번 실행 조건 · 6번 화면."""
import json
import sqlite3
from pathlib import Path

from trade_hub import dashboard, db

WHEN = json.loads((Path(__file__).resolve().parent.parent / "supervisor.json").read_text(encoding="utf-8"))["agents"]["documents"]


def seed(conn):
    with conn:
        pid = db.upsert_product(conn, "실리콘 찜기", stage="negotiating")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=2.0)
        conn.execute("UPDATE product_suppliers SET status = 'selected' WHERE id = ?", (ps,))
    return pid, ps


def test_when_sql_po_needed_then_rerun_after_new_quote(conn):
    assert WHEN["cwd"] == "../trade_docs_agent" and WHEN["command"][-1] == "from-hub"
    count = lambda: conn.execute(WHEN["when_sql"]).fetchone()[0]
    pid, ps = seed(conn)
    assert count() == 0                                                     # 견적 없음
    with conn:
        q = conn.execute("INSERT INTO quotes (product_supplier_id, unit_price_usd, min_qty, incoterm, incoterm_place, created_at)"
                         " VALUES (?, 2.1, 500, 'FOB', 'Ningbo', '2026-11-20T10:00:00')", (ps,)).lastrowid
    assert count() == 1                                                     # PO 없음
    with conn:
        tid = db.add_task(conn, "document", "PO 초안 검토: 실리콘 찜기", product_id=pid)
        d1 = db.add_document(conn, "po", "PO-2026-0001", pid, "C:/x/PO-2026-0001.xlsx", product_supplier_id=ps, quote_id=q,
                             task_id=tid, sha256="ab", qty=500, total_usd=1050, qty_source="quote_min_qty",
                             check_result="warning", checks=[{"level": "warning", "msg": "원산지 추정 — 확인 필요"}])
        conn.execute("UPDATE documents SET created_at = '2026-11-21T10:00:00' WHERE id = ?", (d1,))
    assert count() == 0
    with conn:
        conn.execute("INSERT INTO quotes (product_supplier_id, unit_price_usd, created_at) VALUES (?, 1.9, '2026-11-22T10:00:00')", (ps,))
    assert count() == 1                                                     # 새 견적 → 다시
    with conn:
        d2 = db.add_document(conn, "po", "PO-2026-0001", pid, "C:/x/PO-2026-0001-R1.xlsx", task_id=tid)
    revs = conn.execute("SELECT revision, status FROM documents ORDER BY id").fetchall()
    assert [tuple(r) for r in revs] == [(0, "superseded"), (1, "draft")]
    with conn:
        db.approve_document(conn, d2)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "done"
    assert conn.execute("SELECT status, approved_at IS NOT NULL FROM documents WHERE id = ?", (d2,)).fetchone()[:] == ("approved", 1)


def test_documents_page_lists_latest(conn):
    pid, ps = seed(conn)
    with conn:
        db.add_document(conn, "po", "PO-2026-0002", pid, "C:/x/a.xlsx", product_supplier_id=ps, qty=500, total_usd=1050,
                        qty_source="quote_min_qty", check_result="warning", checks=[{"level": "warning", "msg": "지정장소 미정"}])
    html = dashboard.render(conn)
    assert "서류 목록" in html and "PO-2026-0002" in html and "지정장소 미정" in html and "검토 대기" in html


def test_v17_db_gets_documents_and_incoterm_place(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setenv("TRADE_DB", str(path))
    db.connect().close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE documents")
    raw.execute("ALTER TABLE quotes DROP COLUMN incoterm_place")
    raw.execute("PRAGMA user_version = 17")
    raw.commit(); raw.close()
    c = db.connect()
    try:
        assert "incoterm_place" in [r[1] for r in c.execute("PRAGMA table_info(quotes)")]
        assert c.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        c.close()


def test_relative_doc_path_resolves_to_trade_docs_agent(conn, tmp_path, monkeypatch):
    pid, ps = seed(conn)
    monkeypatch.setattr(dashboard, "docs_dir", lambda: tmp_path)
    f = tmp_path / "output" / "HUB-P1-S1" / "PO-HUB-P1-S1.xlsx"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    with conn:
        db.add_document(conn, "po", "PO-HUB-P1-S1", pid, "output/HUB-P1-S1/PO-HUB-P1-S1.xlsx", product_supplier_id=ps)
    html = dashboard.document_rows(conn)
    assert "PO-HUB-P1-S1</a>" in html and "파일 없음" not in html
    assert dashboard.doc_path("C:/abs/x.xlsx") == Path("C:/abs/x.xlsx")


def test_docs_output_is_servable_root():
    from trade_hub import server
    assert server.agent_spec("documents")[0] / "output" in server.allowed_roots() or \
        (server.agent_spec("documents")[0] / "output").resolve() in server.allowed_roots()
