from datetime import datetime, timedelta

import pytest

import supervisor as sv
from trade_hub import cli, db

NOW = datetime.now().replace(microsecond=0)   # snapshot()이 실제 시각 기준 '최근 7일'을 보므로 현재 시각 사용


def ts(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def add_run(conn, agent, hours_ago, status="succeeded"):
    with conn:
        return conn.execute("INSERT INTO agent_runs (agent, status, started_at) VALUES (?, ?, ?)",
                            (agent, status, ts(hours_ago))).lastrowid


def cfg(**agents):
    return {"agents": agents, "review": {}, "alerts": {}, "backup": {}}


# --- 스케줄 판단 --------------------------------------------------------------
SPEC = {"enabled": True, "command": ["x"], "every_hours": 24, "timeout_minutes": 30}


def test_decide_disabled_and_no_command(conn):
    assert sv.decide(conn, "discover", {**SPEC, "enabled": False}, NOW) == (False, "꺼짐")
    assert sv.decide(conn, "discover", {**SPEC, "command": []}, NOW)[0] is False


def test_decide_every_hours(conn):
    assert sv.decide(conn, "discover", SPEC, NOW) == (True, "첫 실행")
    add_run(conn, "discover", hours_ago=5)
    assert sv.decide(conn, "discover", SPEC, NOW)[0] is False
    add_run(conn, "discover", hours_ago=30)   # MAX(started_at)는 여전히 5시간 전
    assert sv.decide(conn, "discover", SPEC, NOW)[0] is False


def test_decide_every_hours_due(conn):
    add_run(conn, "discover", hours_ago=25)
    due, reason = sv.decide(conn, "discover", SPEC, NOW)
    assert due and "주기 도래" in reason


def test_decide_skips_while_running(conn):
    add_run(conn, "discover", hours_ago=0.1, status="running")
    assert sv.decide(conn, "discover", SPEC, NOW) == (False, "실행 중")


def test_decide_when_sql_with_min_interval(conn):
    spec = {"enabled": True, "command": ["x"], "timeout_minutes": 30, "min_interval_hours": 6,
            "when_sql": "SELECT COUNT(*) FROM products WHERE stage = 'sourcing'"}
    assert sv.decide(conn, "sourcing", spec, NOW) == (False, "처리할 항목 없음")
    with conn:
        db.set_stage(conn, db.upsert_product(conn, "선반"), "sourcing")
    assert sv.decide(conn, "sourcing", spec, NOW) == (True, "처리할 항목 1건")
    add_run(conn, "sourcing", hours_ago=2)
    assert sv.decide(conn, "sourcing", spec, NOW)[0] is False


def test_decide_when_sql_error(conn):
    spec = {"enabled": True, "command": ["x"], "when_sql": "SELECT nope FROM nothing"}
    due, reason = sv.decide(conn, "sourcing", spec, NOW)
    assert not due and "when_sql 오류" in reason


# --- 실행 ---------------------------------------------------------------------
def test_dispatch_records_exit_code(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "LOG_DIR", tmp_path / "logs")
    spec = {"command": ["{python}", "-c", "import os, sys; print(os.environ['TRADE_DB']); sys.exit(3)"],
            "cwd": ".", "timeout_minutes": 1}
    assert sv.dispatch(conn, None, "discover", spec, "테스트", log=lambda *_: None) == 3
    row = conn.execute("SELECT exit_code, finished_at, log_path FROM supervisor_dispatches").fetchone()
    assert row["exit_code"] == 3 and row["finished_at"]
    assert "trade.db" in open(row["log_path"], encoding="utf-8").read()   # TRADE_DB를 넘겨줌


def test_dispatch_missing_folder(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "LOG_DIR", tmp_path / "logs")
    spec = {"command": ["{python}", "-c", "pass"], "cwd": str(tmp_path / "없는폴더")}
    assert sv.dispatch(conn, None, "discover", spec, "테스트", log=lambda *_: None) == -2


# --- 검토관 -------------------------------------------------------------------
def test_rule_alerts(conn):
    add_run(conn, "discover", hours_ago=1, status="failed")
    with conn:
        conn.execute("INSERT INTO tasks (kind, title, created_at) VALUES ('other', '오래된 일', ?)", (ts(24 * 5),))
    snap = sv.snapshot(conn)
    texts = [a["text"] for a in sv.rule_alerts(conn, cfg(), snap, now=NOW)]
    assert any("실패" in t for t in texts)
    assert any("오래된 일" in t for t in texts)


def test_review_without_llm_writes_briefing(conn):
    with db.agent_run(conn, "supervisor") as run:
        bid, summary, alerts, suggestions = sv.review(conn, cfg(), run.id, use_llm=False, log=lambda *_: None)
    assert conn.execute("SELECT summary FROM briefings WHERE id = ?", (bid,)).fetchone()[0] == summary
    assert suggestions == []


def test_review_with_llm_adds_decisions(conn, monkeypatch):
    with conn:
        pid = db.upsert_product(conn, "선반")
    monkeypatch.setattr(sv.llm, "make_backend", lambda *a: (None, "fake-model", {}, "Fake"))
    monkeypatch.setattr(sv.llm, "submit", lambda *a: {"summary": "요약", "suggestions": [
        {"title": "선반 진행 결정", "detail": "근거", "priority": 1, "product_id": pid},
        {"title": "없는 품목", "detail": "근거", "priority": 2.0, "product_id": 999},
    ]})
    for _ in range(2):   # 같은 제안이 두 번 와도 중복으로 쌓이지 않음
        with db.agent_run(conn, "supervisor") as run:
            sv.review(conn, cfg(), run.id, log=lambda *_: None)
    rows = conn.execute("SELECT title, product_id, priority FROM tasks ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("선반 진행 결정", pid, 1), ("없는 품목", None, 2)]


def test_expire_decisions_only_touches_old_supervisor_suggestions(conn):
    sup = add_run(conn, "supervisor", hours_ago=24 * 10)
    other = add_run(conn, "negotiation", hours_ago=24 * 10)
    with conn:
        for title, run_id, kind, age_days in [("오래된 제안", sup, "decision", 10), ("새 제안", sup, "decision", 1),
                                              ("다른 에이전트", other, "decision", 10), ("메일", sup, "send_message", 10),
                                              ("사람이 만든 것", None, "decision", 10)]:
            conn.execute("INSERT INTO tasks (kind, title, run_id, created_at) VALUES (?, ?, ?, ?)",
                         (kind, title, run_id, ts(24 * age_days)))
        assert sv.expire_decisions(conn, 7, now=NOW) == 1
    status = dict(conn.execute("SELECT title, status FROM tasks").fetchall())
    assert status["오래된 제안"] == "dismissed"
    assert {k for k, v in status.items() if v == "pending"} == {"새 제안", "다른 에이전트", "메일", "사람이 만든 것"}


def test_llm_module_imports_without_calling_api():
    # anthropic이 없어도 import 자체는 성공해야 함 (status·--no-llm용)
    assert callable(sv.llm.make_backend)


# --- CLI ----------------------------------------------------------------------
def test_cli_done_and_dismiss(conn, capsys):
    with conn:
        a = db.add_task(conn, "decision", "A")
        b = db.add_task(conn, "decision", "B")
    cli.main(["done", str(a)])
    cli.main(["dismiss", str(b)])
    status = dict(conn.execute("SELECT title, status FROM tasks").fetchall())
    assert status == {"A": "done", "B": "dismissed"}
    with pytest.raises(SystemExit):
        cli.main(["dismiss"])


def test_cli_select_and_drop(conn, capsys):
    with conn:
        p1 = db.upsert_product(conn, "후보1")
        p2 = db.upsert_product(conn, "후보2")
    cli.main(["select", str(p1)])
    cli.main(["drop", str(p2), "검색량 부족"])
    stages = dict(conn.execute("SELECT name, stage FROM products").fetchall())
    assert stages == {"후보1": "sourcing", "후보2": "dropped"}
    note = conn.execute("SELECT note FROM product_events WHERE product_id = ? AND to_stage = 'dropped'", (p2,)).fetchone()
    assert note[0] == "검색량 부족"
