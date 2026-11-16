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
