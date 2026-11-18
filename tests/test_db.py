from datetime import datetime

import pytest

from trade_hub import db


def test_connect_is_idempotent(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    again = db.connect()   # 같은 DB에 스키마를 다시 적용해도 오류 없음
    assert again.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
    again.close()


# --- 품목 -------------------------------------------------------------------
def test_upsert_creates_then_updates_without_overwriting_with_none(conn):
    with conn:
        pid = db.upsert_product(conn, "요가매트", category="스포츠", origin="discover")
    with conn:
        same = db.upsert_product(conn, "요가매트", category=None, risk="낮음", origin="manual")
    assert same == pid
    row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    assert row["category"] == "스포츠"      # None은 덮어쓰지 않음
    assert row["risk"] == "낮음"
    assert row["origin"] == "discover"      # origin은 처음 값 유지
    events = conn.execute("SELECT to_stage, note FROM product_events WHERE product_id = ?", (pid,)).fetchall()
    assert [tuple(e) for e in events] == [("candidate", "등록")]


def test_upsert_rejects_unknown_field(conn):
    with pytest.raises(ValueError):
        db.upsert_product(conn, "x", price=1)


def test_advance_stage_moves_forward_only(conn):
    with conn:
        pid = db.upsert_product(conn, "선반")
        assert db.advance_stage(conn, pid, "negotiating") is True
        assert db.advance_stage(conn, pid, "sourcing") is False      # 뒤로는 안 감
        assert db.advance_stage(conn, pid, "negotiating") is False   # 같은 단계
    assert conn.execute("SELECT stage FROM products WHERE id = ?", (pid,)).fetchone()[0] == "negotiating"


def test_advance_stage_ignores_dropped_product(conn):
    with conn:
        pid = db.upsert_product(conn, "선반")
        db.set_stage(conn, pid, "dropped", note="중단")
        assert db.advance_stage(conn, pid, "sourcing") is False


def test_advance_stage_errors(conn):
    with pytest.raises(ValueError, match="없습니다"):
        db.advance_stage(conn, 999, "sourcing")
    with conn:
        pid = db.upsert_product(conn, "선반")
    with pytest.raises(ValueError, match="단계"):
        db.advance_stage(conn, pid, "dropped")


# --- 할 일 ------------------------------------------------------------------
def test_add_task_dedupes_pending(conn):
    with conn:
        a = db.add_task(conn, "decision", "진행 여부 결정")
        b = db.add_task(conn, "decision", "진행 여부 결정")
        c = db.add_task(conn, "review_quote", "견적 검토", ref_table="quotes", ref_id=5)
        d = db.add_task(conn, "review_quote", "견적 검토 (다른 제목)", ref_table="quotes", ref_id=5)
    assert a == b and c == d and a != c
    with conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
        assert db.add_task(conn, "decision", "진행 여부 결정") != a   # 처리된 뒤에는 새로 만듦


# --- 실행 기록 ----------------------------------------------------------------
def test_agent_run_success(conn):
    with db.agent_run(conn, "discover", args={"count": 3}) as run:
        run.report_dir = "reports/x"
    row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run.id,)).fetchone()
    assert (row["status"], row["report_dir"]) == ("succeeded", "reports/x")
    assert row["finished_at"]


def test_agent_run_failure_rolls_back_uncommitted_writes(conn):
    with pytest.raises(RuntimeError):
        with db.agent_run(conn, "discover") as run:
            with conn:
                db.upsert_product(conn, "커밋됨")         # 커밋된 쓰기는 남음
            db.upsert_product(conn, "커밋 안 됨")         # 커밋 전에 실패 → 버려짐
            raise RuntimeError("API 오류")
    names = {r[0] for r in conn.execute("SELECT name FROM products")}
    assert names == {"커밋됨"}
    row = conn.execute("SELECT status, error FROM agent_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["status"] == "failed" and "API 오류" in row["error"]


def test_agent_run_rejects_unknown_agent(conn):
    with pytest.raises(ValueError):
        with db.agent_run(conn, "nobody"):
            pass


# --- 백업 -------------------------------------------------------------------
def test_backup_once_per_day_and_prunes(conn, tmp_path):
    with conn:
        db.upsert_product(conn, "백업될 품목")
    bdir = tmp_path / "backups"
    first = db.backup(conn, bdir, keep=2, today=datetime(2026, 1, 1))
    assert first.name == "trade_20260101.db"
    assert db.backup(conn, bdir, keep=2, today=datetime(2026, 1, 1)) is None   # 같은 날 두 번은 안 함
    db.backup(conn, bdir, keep=2, today=datetime(2026, 1, 2))
    db.backup(conn, bdir, keep=2, today=datetime(2026, 1, 3))
    assert sorted(p.name for p in bdir.glob("*.db")) == ["trade_20260102.db", "trade_20260103.db"]

    import sqlite3
    copy = sqlite3.connect(bdir / "trade_20260103.db")
    assert copy.execute("SELECT name FROM products").fetchone()[0] == "백업될 품목"
    copy.close()


# --- 공급처 -----------------------------------------------------------------
def test_upsert_supplier_matches_company_and_country_and_keeps_values(conn):
    with conn:
        a = db.upsert_supplier(conn, "Shenzhen Keys", "China", type="제조사", contact_email="a@x.com")
        b = db.upsert_supplier(conn, "Shenzhen Keys", "China", type=None, trust="중")
        c = db.upsert_supplier(conn, "Shenzhen Keys", None)         # 국가 모름 → 다른 업체로 취급
        d = db.upsert_supplier(conn, "Shenzhen Keys", None)
    assert a == b and c == d and a != c
    row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (a,)).fetchone()
    assert (row["type"], row["contact_email"], row["trust"]) == ("제조사", "a@x.com", "중")
    with pytest.raises(ValueError):
        db.upsert_supplier(conn, "X", "China", phone="1")


def test_link_supplier_updates_without_touching_status(conn):
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        sid = db.upsert_supplier(conn, "Keycap Co", "China")
        lid = db.link_supplier(conn, pid, sid, moq="50 pcs", is_top_pick=1)
        conn.execute("UPDATE product_suppliers SET status = 'contacted' WHERE id = ?", (lid,))
        again = db.link_supplier(conn, pid, sid, moq="30 pcs", is_top_pick=0)
    assert again == lid
    row = conn.execute("SELECT * FROM product_suppliers WHERE id = ?", (lid,)).fetchone()
    assert (row["moq"], row["is_top_pick"], row["status"]) == ("30 pcs", 0, "contacted")


def test_migrates_v1_db_by_adding_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)          # v1 시절 products (ref_url·image_url 없음)
    old.executescript("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT,"
                      " stage TEXT NOT NULL DEFAULT 'candidate', origin TEXT, search_keyword_en TEXT,"
                      " keywords_ko_json TEXT, reason TEXT, regulations TEXT, risk TEXT, hs_code TEXT, duty_rate REAL,"
                      " fta_note TEXT, est_retail_krw INTEGER, est_shipping_usd REAL, notes TEXT,"
                      " created_at TEXT, updated_at TEXT); INSERT INTO products (name) VALUES ('옛 품목');"
                      " PRAGMA user_version = 1;")
    old.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    with conn:
        pid = db.upsert_product(conn, "옛 품목", image_url="https://x/a.jpg", ref_url="https://x/p")
    assert conn.execute("SELECT image_url FROM products WHERE id = ?", (pid,)).fetchone()[0] == "https://x/a.jpg"
    conn.close()
    db.connect(path).close()             # 두 번 연결해도 오류 없음


# --- 스키마 v4: 품목 기준 공급처 평가 ------------------------------------------------
def test_link_supplier_v4_fields_store_json(conn):
    import json
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        sid = db.upsert_supplier(conn, "Keycap Co", "China")
        lid = db.link_supplier(conn, pid, sid, score=54.0, grade="C", purchase_mode="small_batch",
                               score_detail_json={"moq_fit": 10.0, "risk_penalty": -4.0},
                               trade_terms_json={"lead_time": None, "sample": "1개부터"},
                               first_questions_json=["단가는?", "배송비는?"], unverified_json=["법인 등기"],
                               evidence_json=[{"claim": "MOQ 1개", "url": "https://example.com"}])
    row = conn.execute("SELECT * FROM product_suppliers WHERE id = ?", (lid,)).fetchone()
    assert (row["score"], row["grade"], row["purchase_mode"]) == (54.0, "C", "small_batch")
    assert json.loads(row["first_questions_json"]) == ["단가는?", "배송비는?"]
    assert json.loads(row["evidence_json"])[0]["url"] == "https://example.com"
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):              # 등급은 A~D만
        with conn:
            db.link_supplier(conn, pid, sid, grade="E")


def test_migration_adds_v4_columns_to_old_db(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)          # v3 시절 product_suppliers (v4 칸 없음)
    old.executescript("""
        CREATE TABLE product_suppliers (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL,
            run_id INTEGER, status TEXT NOT NULL DEFAULT 'candidate', is_top_pick INTEGER NOT NULL DEFAULT 0, moq TEXT,
            unit_price_usd REAL, price_note TEXT, est_landed_krw INTEGER, est_margin_pct REAL, image_url TEXT,
            reject_reason TEXT, updated_at TEXT, UNIQUE (product_id, supplier_id));
        INSERT INTO product_suppliers (product_id, supplier_id, moq) VALUES (1, 1, '50 pcs');
        PRAGMA user_version = 3;""")
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(product_suppliers)")}
    assert {"score", "grade", "first_questions_json", "evidence_json", "purchase_mode"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT moq, score FROM product_suppliers").fetchone()[:] == ("50 pcs", None)   # 기존 행 보존
    conn.close()


# --- MOQ 기준 (v5) -------------------------------------------------------------
@pytest.mark.parametrize("text, qty", [
    ("1 piece", 1), ("50 pcs", 50), ("25 pcs (LED 없는 2/3/4구 스트립형), 50 pcs (LED형)", 25),
    ("1,000 pcs", 1000), ("MOQ: 20 pieces", 20), ("", None), (None, None), ("문의 필요", None)])
def test_first_qty(text, qty):
    assert db.first_qty(text) == qty


@pytest.mark.parametrize("moq, mode, max_qty, expected", [
    ("50 pcs", None, None, ("ok", 50, 50)),              # 칸이 비어 있으면 50 기준
    ("51 pcs", None, None, ("over", 51, 50)),
    ("20 pcs", "retail", None, ("over", 20, 10)),        # retail 기본 10
    ("10 pcs", "retail", None, ("ok", 10, 10)),
    ("50 pcs", "small_batch", None, ("ok", 50, 50)),
    ("5000 pcs", "oem", None, ("no_limit", 5000, None)),  # oem 제한 없음
    ("30 pcs", "oem", 20, ("over", 30, 20)),             # max_order_qty가 있으면 그것이 우선
    ("100 pcs", "small_batch", 200, ("ok", 100, 200)),
    (None, "small_batch", None, ("unknown", None, 50)),
    ("1 set", "알수없음", None, ("ok", 1, 50)),
])
def test_moq_fit(moq, mode, max_qty, expected):
    assert db.moq_fit(moq, mode, max_qty) == expected


def test_link_supplier_stores_max_order_qty(conn):
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        sid = db.upsert_supplier(conn, "Keycap Co", "China")
        lid = db.link_supplier(conn, pid, sid, purchase_mode="small_batch", max_order_qty=50)
    assert conn.execute("SELECT purchase_mode, max_order_qty FROM product_suppliers WHERE id = ?",
                        (lid,)).fetchone()[:] == ("small_batch", 50)


# --- 구매 사이트 순위 (v6) --------------------------------------------------------
CHANNELS = [
    {"rank": 1, "key": "aliexpress", "site": "AliExpress", "search_url": "https://www.aliexpress.com/w/wholesale-keycap.html",
     "total": 82.5, "scores": {"small_qty": 20, "price": 16}, "typical_price": "USD 3.2-4.5", "min_qty": "1 piece",
     "summary": "1개부터 구매 가능", "site_note": "구매자 보호", "evidence": [{"claim": "1개", "url": "https://x"}],
     "assessed": True},
    {"rank": 2, "key": "1688", "site": "1688", "search_url": "https://s.1688.com/x", "total": 61, "scores": {},
     "typical_price": None, "min_qty": None, "summary": "", "assessed": False},
]


def test_save_channels_and_latest(conn):
    import json
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        r1 = conn.execute("INSERT INTO agent_runs (agent) VALUES ('sourcing')").lastrowid
        assert db.save_channels(conn, pid, r1, CHANNELS, purchase_mode="small_batch") == 2
        assert db.save_channels(conn, pid, r1, CHANNELS) == 2            # 같은 실행 재저장 → 중복 없음
    assert conn.execute("SELECT COUNT(*) FROM product_channels").fetchone()[0] == 2
    top = db.latest_channels(conn, pid)[0]
    assert (top["site"], top["total"], top["typical_price"], top["assessed"]) == ("AliExpress", 82.5, "USD 3.2-4.5", 1)
    assert json.loads(top["scores_json"]) == {"small_qty": 20, "price": 16}
    with conn:                                                           # 새 실행 → 최신 실행분만 보임
        r2 = conn.execute("INSERT INTO agent_runs (agent) VALUES ('sourcing')").lastrowid
        db.save_channels(conn, pid, r2, [{"rank": 1, "key": "alibaba", "site": "Alibaba", "total": 70}, "잘못된 항목"])
    assert [c["site"] for c in db.latest_channels(conn, pid)] == ["Alibaba"]
    assert db.latest_channels(conn, 999) == []


def test_channels_table_created_on_old_db(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("PRAGMA user_version = 5")
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'product_channels'").fetchone()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


# --- v8: 한 줄 = (품목, 공급처, 구매 방식) ---------------------------------------------
def test_same_supplier_kept_separately_per_purchase_mode(conn):
    with conn:
        pid = db.upsert_product(conn, "AI안경", stage="sourcing")
        sid = db.upsert_supplier(conn, "NAMOFOTO", "China")
        small = db.link_supplier(conn, pid, sid, purchase_mode="small_batch", score=30.0, moq="50 pcs")
        retail = db.link_supplier(conn, pid, sid, purchase_mode="retail", score=72.0, moq="1 piece")
        again = db.link_supplier(conn, pid, sid, purchase_mode="small_batch", score=35.0)   # 같은 방식 → 그 줄 갱신
    assert small != retail and again == small
    got = dict(conn.execute("SELECT purchase_mode, score FROM product_suppliers").fetchall())
    assert got == {"small_batch": 35.0, "retail": 72.0}                                     # 서로 덮어쓰지 않음
    with pytest.raises(ValueError, match="purchase_mode"):                                  # 어느 줄인지 모르면 오류
        db.link_supplier(conn, pid, sid, moq="10 pcs")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):                                             # 같은 방식 두 줄은 막힘
        with conn:
            conn.execute("INSERT INTO product_suppliers (product_id, supplier_id, purchase_mode) VALUES (?, ?, 'retail')",
                         (pid, sid))


def test_legacy_row_without_mode_is_adopted(conn):
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
        sid = db.upsert_supplier(conn, "Keycap Co", "China")
        old = db.link_supplier(conn, pid, sid, moq="50 pcs")                  # 구매 방식 기록 전 저장분
        assert db.link_supplier(conn, pid, sid, moq="40 pcs") == old          # 줄이 하나면 방식 없이도 갱신
        assert db.link_supplier(conn, pid, sid, purchase_mode="small_batch") == old   # 첫 방식 지정 → 그 줄을 이어받음
    assert conn.execute("SELECT COUNT(*), MAX(purchase_mode) FROM product_suppliers").fetchone()[:] == (1, "small_batch")


def test_v8_migration_rebuilds_old_table_and_keeps_links(tmp_path):
    import sqlite3
    path = tmp_path / "v7.db"
    db.connect(path).close()                       # 최신 스키마로 만든 뒤 product_suppliers만 v7 모양(2칸 UNIQUE)으로 되돌림
    old = sqlite3.connect(path)
    old.executescript("""
        PRAGMA foreign_keys = OFF;
        DROP INDEX IF EXISTS ux_ps_product_supplier_mode;
        DROP TABLE product_suppliers;   -- (RENAME을 쓰면 다른 테이블의 외래키 이름까지 바뀌어 시험이 엉뚱해짐)
        CREATE TABLE product_suppliers (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL,
            run_id INTEGER, status TEXT NOT NULL DEFAULT 'candidate', is_top_pick INTEGER NOT NULL DEFAULT 0, moq TEXT,
            unit_price_usd REAL, price_note TEXT, est_landed_krw INTEGER, est_margin_pct REAL, image_url TEXT,
            score REAL, grade TEXT, purchase_mode TEXT, reject_reason TEXT, updated_at TEXT,
            UNIQUE (product_id, supplier_id));
        INSERT INTO products (id, name) VALUES (1, '키캡');
        INSERT INTO suppliers (id, company) VALUES (1, 'A'), (2, 'B');
        INSERT INTO product_suppliers (id, product_id, supplier_id, status, moq, score, purchase_mode)
            VALUES (7, 1, 1, 'contacted', '50 pcs', 54, 'small_batch'), (9, 1, 2, 'candidate', '1 piece', 30, 'small_batch');
        INSERT INTO messages (product_supplier_id, direction, body, status) VALUES (7, 'outbound', 'RFQ', 'sent');
        INSERT INTO quotes (product_supplier_id, unit_price_usd) VALUES (7, 2.8);
        PRAGMA user_version = 7;""")
    old.close()

    conn = db.connect(path)                        # ← 여기서 v8 마이그레이션
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert not db._ps_has_old_unique(conn)
    rows = [tuple(r) for r in conn.execute("SELECT id, status, moq, score, purchase_mode FROM product_suppliers ORDER BY id")]
    assert rows == [(7, "contacted", "50 pcs", 54.0, "small_batch"), (9, "candidate", "1 piece", 30.0, "small_batch")]
    assert conn.execute("SELECT product_supplier_id FROM messages").fetchone()[0] == 7      # 연결 유지
    assert conn.execute("SELECT product_supplier_id FROM quotes").fetchone()[0] == 7
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1                            # 외래키 다시 켜짐
    with conn:                                     # 이제 같은 업체를 retail로 따로 저장 가능
        db.link_supplier(conn, 1, 1, purchase_mode="retail", moq="1 piece")
    assert conn.execute("SELECT COUNT(*) FROM product_suppliers WHERE supplier_id = 1").fetchone()[0] == 2
    assert list((tmp_path / "backups").glob("before_v8_*.db"))                               # 바꾸기 전 백업
    conn.close()
    db.connect(path).close()                       # 다시 연결해도 아무 일 없음 (두 번 하지 않음)


def test_v9_adds_gmail_columns_to_old_db(tmp_path):
    """v8 DB → v9: messages에 external_* 칸 + 같은 Gmail 메일 중복 저장 방지 인덱스."""
    import sqlite3
    path = tmp_path / "old.db"
    db.connect(path).close()
    old = sqlite3.connect(path)
    old.executescript("DROP INDEX ux_messages_external; DROP INDEX idx_messages_thread;"
                      "ALTER TABLE messages DROP COLUMN external_id; ALTER TABLE messages DROP COLUMN external_thread_id;"
                      "ALTER TABLE messages DROP COLUMN external_draft_id; PRAGMA user_version = 8;")
    old.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    assert {"external_id", "external_thread_id", "external_draft_id"} <= cols
    with conn:
        pid = db.upsert_product(conn, "키캡")
        sid = db.upsert_supplier(conn, "A")
        ps = db.link_supplier(conn, pid, sid)
        conn.execute("INSERT INTO messages (product_supplier_id, direction, body, status, external_id)"
                     " VALUES (?, 'inbound', 'hi', 'received', 'g1')", (ps,))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO messages (product_supplier_id, direction, body, status, external_id)"
                         " VALUES (?, 'inbound', 'hi', 'received', 'g1')", (ps,))
