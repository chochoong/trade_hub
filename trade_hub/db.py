"""
1인 무역 에이전트 공용 DB (SQLite) — 모든 에이전트가 함께 쓰는 헬퍼.

에이전트들은 서로 직접 호출하지 않고 이 DB로 일을 주고받는다.
스키마(에이전트 간 약속)와 에이전트별 담당 테이블: trade_hub/schema.sql

DB 파일 위치: 환경변수 TRADE_DB, 없으면 trade-hub/data/trade.db
(슈퍼바이저가 에이전트를 실행할 때는 TRADE_DB를 넘겨준다)

사용 예 (각 에이전트에서):
    from trade_hub import db
    conn = db.connect()
    with db.agent_run(conn, "sourcing", provider="anthropic", model="...") as run:
        ...  # 작업
        pid = db.upsert_product(conn, "요가매트", run.id)
        db.advance_stage(conn, pid, "negotiating", run.id, "견적 요청 시작")
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PKG_DIR / "schema.sql"
SCHEMA_VERSION = 1   # 스키마를 바꾸면 올리고, connect()에 ALTER 마이그레이션을 추가할 것 (컬럼 추가만 허용)

STAGES = ["candidate", "sourcing", "negotiating", "sampling", "ordered", "shipping", "selling", "closed"]
TERMINAL_STAGES = {"closed", "dropped"}
AGENTS = ["discover", "sourcing", "negotiation", "documents", "compliance", "costing", "sales", "finance", "supervisor"]


def now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def default_path():
    return Path(os.environ.get("TRADE_DB") or PKG_DIR.parent / "data" / "trade.db")


def connect(path=None):
    """DB에 연결하고 스키마를 적용한다 (여러 번 호출해도 안전)."""
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")     # 여러 에이전트·슈퍼바이저가 동시에 읽고 써도 덜 막히게
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def dumps(v):
    return None if v is None else json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 실행 기록 — 모든 에이전트는 실행할 때 agent_runs에 자기 이름으로 1행을 남긴다
# ---------------------------------------------------------------------------
class Run:
    def __init__(self, conn, run_id):
        self.conn, self.id, self.report_dir = conn, run_id, None


@contextmanager
def agent_run(conn, agent, provider=None, model=None, args=None):
    """with agent_run(conn, "discover", ...) as run: ...  → 성공/실패를 agent_runs에 기록한다."""
    if agent not in AGENTS:
        raise ValueError(f"알 수 없는 에이전트 이름: {agent!r} (가능: {', '.join(AGENTS)})")
    with conn:
        cur = conn.execute("INSERT INTO agent_runs (agent, provider, model, args_json) VALUES (?, ?, ?, ?)",
                           (agent, provider, model, dumps(args)))
    run = Run(conn, cur.lastrowid)
    try:
        yield run
    except BaseException as e:   # SystemExit·Ctrl+C 포함 → 실패로 남기고 그대로 전파
        with conn:
            conn.execute("UPDATE agent_runs SET status='failed', error=?, report_dir=?, finished_at=? WHERE id=?",
                         (f"{type(e).__name__}: {e}"[:2000], run.report_dir, now(), run.id))
        raise
    with conn:
        conn.execute("UPDATE agent_runs SET status='succeeded', report_dir=?, finished_at=? WHERE id=?",
                     (run.report_dir, now(), run.id))


# ---------------------------------------------------------------------------
# 품목·단계
# ---------------------------------------------------------------------------
PRODUCT_FIELDS = {"category", "origin", "search_keyword_en", "keywords_ko_json", "reason", "regulations", "risk",
                  "hs_code", "duty_rate", "fta_note", "est_retail_krw", "est_shipping_usd", "notes"}


def upsert_product(conn, name, run_id=None, stage="candidate", **fields):
    """이름으로 품목을 찾아 갱신하거나 새로 만든다. None인 값은 기존 값을 덮어쓰지 않고, origin은 처음 값을 유지."""
    unknown = set(fields) - PRODUCT_FIELDS
    if unknown:
        raise ValueError(f"알 수 없는 품목 필드: {unknown}")
    fields = {k: v for k, v in fields.items() if v is not None}
    row = conn.execute("SELECT id, origin FROM products WHERE name = ?", (name,)).fetchone()
    if row is None:
        cols = ["name", "stage", *fields]
        cur = conn.execute(f"INSERT INTO products ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           (name, stage, *fields.values()))
        conn.execute("INSERT INTO product_events (product_id, run_id, from_stage, to_stage, note) VALUES (?, ?, NULL, ?, ?)",
                     (cur.lastrowid, run_id, stage, "등록"))
        return cur.lastrowid
    if row["origin"]:
        fields.pop("origin", None)
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE products SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), now(), row["id"]))
    return row["id"]


def set_stage(conn, product_id, stage, run_id=None, note=None):
    """단계를 바꾸고 이력을 남긴다. 바뀌었으면 True."""
    row = conn.execute("SELECT stage FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise ValueError(f"품목 번호 {product_id}가 없습니다")
    if row["stage"] == stage:
        return False
    conn.execute("UPDATE products SET stage = ?, updated_at = ? WHERE id = ?", (stage, now(), product_id))
    conn.execute("INSERT INTO product_events (product_id, run_id, from_stage, to_stage, note) VALUES (?, ?, ?, ?, ?)",
                 (product_id, run_id, row["stage"], stage, note))
    return True


def advance_stage(conn, product_id, stage, run_id=None, note=None):
    """앞 단계로만 이동한다 (이미 더 진행됐거나 종료·중단된 품목은 그대로). 에이전트용."""
    cur = conn.execute("SELECT stage FROM products WHERE id = ?", (product_id,)).fetchone()["stage"]
    if cur in TERMINAL_STAGES or STAGES.index(cur) >= STAGES.index(stage):
        return False
    return set_stage(conn, product_id, stage, run_id, note)


def add_task(conn, kind, title, product_id=None, run_id=None, ref_table=None, ref_id=None,
             detail=None, priority=2, due_date=None):
    """사람이 할 일(승인 대기함)을 만든다. 같은 대상(또는 같은 제목)의 대기 중인 할 일이 있으면 그 id를 돌려준다."""
    if ref_table and ref_id:
        row = conn.execute("SELECT id FROM tasks WHERE kind = ? AND ref_table = ? AND ref_id = ? AND status = 'pending'",
                           (kind, ref_table, ref_id)).fetchone()
    else:
        row = conn.execute("SELECT id FROM tasks WHERE kind = ? AND title = ? AND product_id IS ? AND status = 'pending'",
                           (kind, title, product_id)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO tasks (product_id, run_id, kind, ref_table, ref_id, title, detail, priority, due_date)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, run_id, kind, ref_table, ref_id, title, detail, priority, due_date)).lastrowid


def stage_order_sql(col="stage"):
    return "CASE " + col + " " + " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(STAGES)) + " ELSE 99 END"
