"""
1인 무역 에이전트 공용 DB (SQLite) — 모든 에이전트가 함께 쓰는 헬퍼.

에이전트들은 서로 직접 호출하지 않고 이 DB로 일을 주고받는다.
스키마(에이전트 간 약속)와 에이전트별 담당 테이블: trade_hub/schema.sql

DB 파일 위치: 환경변수 TRADE_DB, 없으면 trade_hub/data/trade.db
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
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PKG_DIR / "schema.sql"
SCHEMA_VERSION = 18  # 스키마를 바꾸면 올리고, MIGRATIONS에 추가한 컬럼을 적을 것 (컬럼 추가만 허용)
# 버전별로 추가된 컬럼: 예전 DB에는 connect() 때 ALTER TABLE로 붙인다 (새 DB는 schema.sql에 이미 있음)


def _json_col(col):
    return f"TEXT CHECK ({col} IS NULL OR json_valid({col}))"


MIGRATIONS = {
    2: [("products", "ref_url", "TEXT"), ("products", "image_url", "TEXT")],
    3: [("product_suppliers", "image_url", "TEXT")],
    4: [("product_suppliers", "score", "REAL"),
        ("product_suppliers", "grade", "TEXT CHECK (grade IS NULL OR grade IN ('A', 'B', 'C', 'D'))"),
        ("product_suppliers", "score_detail_json", _json_col("score_detail_json")),
        ("product_suppliers", "purchase_mode", "TEXT"),
        ("product_suppliers", "trade_terms_json", _json_col("trade_terms_json")),
        ("product_suppliers", "first_questions_json", _json_col("first_questions_json")),
        ("product_suppliers", "unverified_json", _json_col("unverified_json")),
        ("product_suppliers", "evidence_json", _json_col("evidence_json"))],
    5: [("product_suppliers", "max_order_qty", "INTEGER")],
    6: [],   # product_channels 테이블 추가 (새 테이블은 schema.sql의 CREATE TABLE IF NOT EXISTS로 예전 DB에도 생김)
    7: [("messages", "language", "TEXT"), ("messages", "translation", "TEXT"),   # 5번 negotiation 연결
        ("messages", "analysis_json", _json_col("analysis_json"))],
    8: [],   # product_suppliers 한 줄 기준 (품목, 공급처) → (품목, 공급처, 구매 방식): _migrate_v8 (칸 추가가 아닌 유일한 예외)
    9: [("messages", "external_id", "TEXT"), ("messages", "external_thread_id", "TEXT"),   # 5번 Gmail 연동
        ("messages", "external_draft_id", "TEXT")],
    10: [("products", "source_country", "TEXT"), ("products", "source_country_reason", "TEXT")],   # 1번 추천 공급 국가
    11: [],  # compliance_checks 테이블 추가 (3번 통관·규제) — schema.sql의 CREATE TABLE IF NOT EXISTS로 예전 DB에도 생김
    12: [("products", "source_page_url", "TEXT")],   # 1번이 조사 중 본 해외 구매처 상품 상세 페이지 → 2번 첫 후보
    13: [("product_suppliers", "specs_json", "TEXT")],   # 2번이 판매 페이지에서 읽은 제품 사양 → 3번 통관·규제가 읽음
    14: [("compliance_checks", "supplier_questions_json", "TEXT"),   # 3번 → 5번: 공급처에게 물을 사양 질문
         ("product_suppliers", "supplier_specs_json", "TEXT")],      # 5번 → 3번: 공급처가 답한 사양 (merge_supplier_specs)
    15: [("messages", "followup_due_on", "TEXT")],   # 캘린더에서 옮긴 후속 메일 예정일 (calendar_sync가 씀, 5번 FOLLOWUP_DUE_SQL이 따름)
    16: [("compliance_checks", "basis_ps_id",        # 3번 판정에 쓴 대표 공급처 줄 (공급처 선정이 바뀌면 다시 점검)
          "INTEGER REFERENCES product_suppliers(id) ON DELETE SET NULL")],
    17: [("compliance_checks", "duty_estimate_json", _json_col("duty_estimate_json"))],   # 3번 HS 후보 관세율 참고값 (관세사 확인값 아님)
    18: [("quotes", "incoterm_place", "TEXT")],   # 인코텀즈 지정장소 (5번이 답장에서) — documents 표는 schema.sql CREATE IF NOT EXISTS로 생김
}
PS_UNIQUE_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS ux_ps_product_supplier_mode "
                   "ON product_suppliers(product_id, supplier_id, COALESCE(purchase_mode, ''))")
# v9 칸에 거는 인덱스: 예전 DB는 schema.sql 실행 시점에 칸이 없으므로 마이그레이션 뒤에 만든다
MSG_EXTERNAL_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_external ON messages(external_id) WHERE external_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(external_thread_id) WHERE external_thread_id IS NOT NULL",
)


def _ps_has_old_unique(conn):
    """예전 테이블 제약 UNIQUE(product_id, supplier_id)가 남아 있는가."""
    for idx in conn.execute("PRAGMA index_list(product_suppliers)").fetchall():
        if idx["unique"] and idx["origin"] == "u":
            cols = [r["name"] for r in conn.execute(f"PRAGMA index_info('{idx['name']}')")]
            if cols == ["product_id", "supplier_id"]:
                return True
    return False


def _migrate_v8(conn, path):
    """product_suppliers를 새 정의(schema.sql)로 다시 만들어 2칸 UNIQUE를 없애고, 3칸 고유 인덱스를 건다.
    SQLite는 제약을 바꿀 수 없어 '새 테이블 만들기 → 복사 → 교체' (id 그대로 → messages·quotes·orders 연결 유지).
    바꾸기 전에 data/backups/before_v8_<시각>.db로 백업한다."""
    if _ps_has_old_unique(conn):
        if conn.execute("SELECT COUNT(*) FROM product_suppliers").fetchone()[0]:
            bdir = Path(path).parent / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            dest = sqlite3.connect(bdir / f"before_v8_{datetime.now():%Y%m%d_%H%M%S}.db")
            try:
                conn.backup(dest)
            finally:
                dest.close()
        ddl = re.search(r"CREATE TABLE IF NOT EXISTS product_suppliers \((.*?)\n\);", SCHEMA_PATH.read_text(encoding="utf-8"),
                        re.S)[1]
        old_level = conn.isolation_level
        conn.isolation_level = None          # 트랜잭션을 직접 관리 (외래키 끄기는 트랜잭션 밖에서만 됨)
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")   # 다른 프로세스가 동시에 바꾸지 못하게
            try:
                if _ps_has_old_unique(conn):  # 잠금을 잡은 뒤 다시 확인 (그새 다른 프로세스가 끝냈을 수 있음)
                    conn.execute(f"CREATE TABLE product_suppliers_v8 ({ddl}\n)")
                    new_cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(product_suppliers_v8)")}
                    names = [r["name"] for r in conn.execute("PRAGMA table_info(product_suppliers)") if r["name"] in new_cols]
                    # 예전 줄에 NULL이 있어도 새 테이블의 NOT NULL·기본값 칸이면 기본값으로 채움 (예: updated_at)
                    picks = [f"COALESCE({n}, {new_cols[n]['dflt_value']})" if new_cols[n]["notnull"] and new_cols[n]["dflt_value"]
                             else n for n in names]
                    conn.execute(f"INSERT INTO product_suppliers_v8 ({', '.join(names)}) "
                                 f"SELECT {', '.join(picks)} FROM product_suppliers")
                    conn.execute("DROP TABLE product_suppliers")
                    conn.execute("ALTER TABLE product_suppliers_v8 RENAME TO product_suppliers")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_product ON product_suppliers(product_id, status)")
                    # 메일·견적·발주가 가리키는 공급처 줄이 모두 그대로 있는지 (id를 유지했으므로 0건이어야 함)
                    bad = [r for t in ("messages", "quotes", "orders")
                           for r in conn.execute(f"PRAGMA foreign_key_check({t})").fetchall() if r[2] == "product_suppliers"]
                    if bad:
                        raise RuntimeError(f"v8 마이그레이션 후 공급처 연결이 끊긴 줄 {len(bad)}건 → 되돌림")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.isolation_level = old_level
    with conn:
        conn.execute(PS_UNIQUE_INDEX)

STAGES = ["candidate", "sourcing", "negotiating", "sampling", "ordered", "shipping", "selling", "closed"]
TERMINAL_STAGES = {"closed", "dropped"}
AGENTS = ["discover", "sourcing", "compliance", "costing", "negotiation", "documents", "sales", "finance", "supervisor"]


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
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 8:
        _migrate_v8(conn, path)   # 테이블 교체는 칸 추가와 따로 (새 정의에 v4~v5 칸이 이미 들어 있음)
    if version < SCHEMA_VERSION:
        with conn:
            for v in sorted(MIGRATIONS):
                if v <= version:
                    continue
                for table, col, typ in MIGRATIONS[v]:
                    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if col not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    with conn:
        for sql in MSG_EXTERNAL_INDEXES:
            conn.execute(sql)
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
    """with agent_run(conn, "discover", ...) as run: ...  → 성공/실패를 agent_runs에 기록한다.

    블록 안의 쓰기는 `with conn:`으로 짧게 나눠 커밋할 것. 커밋하지 않은 채 LLM 호출 등을 기다리면
    그동안 DB 쓰기 잠금이 잡혀 다른 에이전트·CLI가 'database is locked'로 실패한다.
    실패(예외)로 끝나면 커밋하지 않은 쓰기는 버리고(rollback) 실패만 기록한다.
    """
    if agent not in AGENTS:
        raise ValueError(f"알 수 없는 에이전트 이름: {agent!r} (가능: {', '.join(AGENTS)})")
    with conn:
        cur = conn.execute("INSERT INTO agent_runs (agent, provider, model, args_json) VALUES (?, ?, ?, ?)",
                           (agent, provider, model, dumps(args)))
    run = Run(conn, cur.lastrowid)
    try:
        yield run
    except BaseException as e:   # SystemExit·Ctrl+C 포함 → 실패로 남기고 그대로 전파
        conn.rollback()          # 실패한 실행의 커밋 안 된 쓰기는 반영하지 않음
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
                  "hs_code", "duty_rate", "fta_note", "est_retail_krw", "est_shipping_usd", "notes",
                  "ref_url", "image_url", "source_country", "source_country_reason", "source_page_url"}
SOURCE_COUNTRIES = {"China": "중국", "Japan": "일본", "USA": "미국"}   # 추천 공급 국가 = 2번이 조사할 수 있는 나라 (2번 --country 값)


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
    if stage not in STAGES:
        raise ValueError(f"알 수 없는 단계: {stage!r} (가능: {', '.join(STAGES)})")
    row = conn.execute("SELECT stage FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise ValueError(f"품목 번호 {product_id}가 없습니다")
    cur = row["stage"]
    if cur in TERMINAL_STAGES or STAGES.index(cur) >= STAGES.index(stage):
        return False
    return set_stage(conn, product_id, stage, run_id, note)


def add_document(conn, kind, doc_no, product_id, path, *, product_supplier_id=None, quote_id=None, run_id=None,
                 task_id=None, sha256=None, qty=None, total_usd=None, qty_source=None, check_result=None, checks=None):
    """6번 서류 1건 기록. 같은 doc_no가 있으면 revision +1, 예전 초안(draft)은 superseded. 새 행 id를 돌려준다."""
    prev = conn.execute("SELECT MAX(revision) FROM documents WHERE doc_no = ?", (doc_no,)).fetchone()[0]
    rev = 0 if prev is None else prev + 1
    if prev is not None:
        conn.execute("UPDATE documents SET status = 'superseded' WHERE doc_no = ? AND status = 'draft'", (doc_no,))
    return conn.execute(
        "INSERT INTO documents (kind, doc_no, revision, product_id, product_supplier_id, quote_id, run_id, task_id, path, sha256,"
        " qty, total_usd, qty_source, check_result, check_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, doc_no, rev, product_id, product_supplier_id, quote_id, run_id, task_id, str(path), sha256,
         qty, total_usd, qty_source, check_result, dumps(checks))).lastrowid


def approve_document(conn, doc_id):
    """6번 approve(해시 확인)가 끝난 뒤 부름: 서류를 approved로, 연결된 할 일을 done으로. (trade_hub done 만으로는 승인 아님)"""
    row = conn.execute("SELECT task_id, status FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise ValueError(f"서류 {doc_id} 없음")
    if row["status"] != "draft":
        raise ValueError(f"서류 {doc_id}는 {row['status']} 상태라 승인할 수 없습니다")
    conn.execute("UPDATE documents SET status = 'approved', approved_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')"
                 " WHERE id = ?", (doc_id,))
    if row["task_id"]:
        conn.execute("UPDATE tasks SET status = 'done', done_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')"
                     " WHERE id = ? AND status = 'pending'", (row["task_id"],))


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


# ---------------------------------------------------------------------------
# 공급처 (2번 sourcing 에이전트가 쓴다)
# ---------------------------------------------------------------------------
SUPPLIER_FIELDS = {"type", "years_in_business", "certifications", "platform_badge", "url", "contact_name",
                   "contact_email", "contact_other", "trust", "trust_reason", "red_flags", "notes"}
LINK_FIELDS = {"is_top_pick", "moq", "unit_price_usd", "price_note", "est_landed_krw", "est_margin_pct", "image_url",
               # v4: 2번이 이 품목 기준으로 평가한 결과. *_json 필드는 list/dict를 그대로 넘기면 JSON으로 저장
               "score", "grade", "score_detail_json", "purchase_mode", "trade_terms_json", "first_questions_json",
               "unverified_json", "evidence_json", "max_order_qty",
               "specs_json"}   # v13: 제품 사양 {"flags": {...}, "materials": [...], "evidence": [...]}

def save_channels(conn, product_id, run_id, channels, purchase_mode=None):
    """2번 results.json의 channels(구매 사이트 순위)를 product_channels에 저장한다. 저장한 행 수를 돌려준다.
    같은 (품목, 실행)으로 다시 저장하면 그 실행분을 통째로 바꾼다 (재저장해도 중복 없음).
    channels 항목: {rank, key, site, search_url, total, scores, typical_price, min_qty, summary, site_note, evidence, assessed}"""
    conn.execute("DELETE FROM product_channels WHERE product_id = ? AND run_id IS ?", (product_id, run_id))
    n = 0
    for i, c in enumerate(channels or [], 1):
        if not isinstance(c, dict) or not (c.get("key") or c.get("site")):
            continue
        assessed = c.get("assessed")
        conn.execute(
            "INSERT INTO product_channels (product_id, run_id, rank, site_key, site, search_url, total, scores_json,"
            " typical_price, min_qty, summary, site_note, evidence_json, assessed, purchase_mode)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, run_id, c.get("rank") or i, str(c.get("key") or c.get("site")), str(c.get("site") or c.get("key")),
             c.get("search_url"), c.get("total"), dumps(c.get("scores") or None), c.get("typical_price"),
             c.get("min_qty"), c.get("summary") or None, c.get("site_note"), dumps(c.get("evidence") or None),
             None if assessed is None else int(bool(assessed)), purchase_mode))
        n += 1
    return n


def latest_channels(conn, product_id, purchase_mode=None):
    """품목의 가장 최근 실행분 사이트 순위 (순위순). purchase_mode를 주면 그 구매 방식으로 평가한 실행 중 최신."""
    last = conn.execute("SELECT run_id FROM product_channels WHERE product_id = ? AND (? IS NULL OR purchase_mode = ?)"
                        " ORDER BY id DESC LIMIT 1", (product_id, purchase_mode, purchase_mode)).fetchone()
    if last is None:
        return []
    return conn.execute("SELECT * FROM product_channels WHERE product_id = ? AND run_id IS ? ORDER BY rank, id",
                        (product_id, last["run_id"])).fetchall()


# ---------------------------------------------------------------------------
# 통관·규제 체크 (3번 compliance 에이전트가 쓴다)
# ---------------------------------------------------------------------------
COMPLIANCE_VERDICTS = {"go": "진행", "conditional": "조건부 진행", "hold": "보류"}
COMPLIANCE_LIST_FIELDS = ("verdict_reasons", "hs_candidates", "required_certs", "possible_rules", "factory_requests",
                          "labeling", "contract_clauses", "broker_questions", "unknown_flags", "warnings",
                          "supplier_questions",   # v14: [{"flag", "question_ko"}] — 5번이 공급처에게 물음
                          "duty_estimate")        # v17: {"general_pct", "fta_pct", ...} 관세율 참고값 (dict, 못 찾으면 None)


# 3번이 판정에 쓰는 대표 공급처 줄: 선정 > 추천(top pick) > 점수 > id, 제외(rejected) 빼고.
# compliance_agent/compliance_agent/hub.py의 대표 공급처 쿼리와 같은 순서 — 바꾸면 같이 바꿀 것.
# {pid}: 품목 id 식 (예: "?" 또는 "p.id"). 별칭 bp는 바깥 쿼리의 ps와 겹치지 않게.
BASIS_PS_SQL = ("(SELECT bp.id FROM product_suppliers bp WHERE bp.product_id = {pid} AND bp.status != 'rejected' "
                "ORDER BY bp.status = 'selected' DESC, bp.is_top_pick DESC, bp.score DESC, bp.id LIMIT 1)")
# 최신 판정의 대표 공급처가 지금 대표와 다른 품목 (p 별칭 기준) — 판정 다시 할 것.
# 3번 PENDING_SQL·supervisor.json compliance.when_sql이 이 조건을 포함 — 바꾸면 같이 바꿀 것.
BASIS_CHANGED_SQL = ("EXISTS (SELECT 1 FROM compliance_checks bc WHERE bc.id = (SELECT MAX(id) FROM compliance_checks "
                     "WHERE product_id = p.id) AND bc.basis_ps_id IS NOT NULL AND bc.basis_ps_id IS NOT "
                     + BASIS_PS_SQL.format(pid="p.id") + ")")


def basis_ps_id(conn, product_id):
    """품목의 지금 대표 공급처 줄 id (없으면 None)."""
    row = conn.execute("SELECT " + BASIS_PS_SQL.format(pid="?"), (product_id,)).fetchone()
    return row[0]


_AUTO = object()


def save_compliance_check(conn, product_id, run_id, verdict, checked_on, flags=None, used_llm=False,
                          report_path=None, basis_ps_id=_AUTO, **lists):
    """통관·규제 판정 1건을 남긴다 (실행마다 새 행 → 이력 유지). 새 행 id를 돌려준다.
    verdict: go / conditional / hold (한국어 '진행'·'조건부 진행'·'보류'도 받음)
    basis_ps_id: 판정에 쓴 대표 공급처 줄 [v16]. 안 주면 저장 시점의 대표(BASIS_PS_SQL)를 적는다.
    lists: COMPLIANCE_LIST_FIELDS의 이름 (verdict_reasons=[...], hs_candidates=[...] ...) → *_json 칸에 저장"""
    verdict = {v: k for k, v in COMPLIANCE_VERDICTS.items()}.get(verdict, verdict)
    if verdict not in COMPLIANCE_VERDICTS:
        raise ValueError(f"알 수 없는 판정: {verdict!r} (가능: {', '.join(COMPLIANCE_VERDICTS)})")
    unknown = set(lists) - set(COMPLIANCE_LIST_FIELDS)
    if unknown:
        raise ValueError(f"알 수 없는 통관·규제 필드: {unknown}")
    if conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone() is None:
        raise ValueError(f"품목 번호 {product_id}가 없습니다")
    if basis_ps_id is _AUTO:
        basis_ps_id = globals()["basis_ps_id"](conn, product_id)
    elif basis_ps_id is not None and conn.execute("SELECT 1 FROM product_suppliers WHERE id = ? AND product_id = ?",
                                                  (basis_ps_id, product_id)).fetchone() is None:
        raise ValueError(f"공급처 줄 {basis_ps_id}는 품목 {product_id}의 것이 아닙니다")
    cols = {"product_id": product_id, "run_id": run_id, "verdict": verdict, "checked_on": str(checked_on),
            "basis_ps_id": basis_ps_id,
            "flags_json": dumps(flags), "used_llm": int(bool(used_llm)), "report_path": report_path,
            **{f"{k}_json": dumps(v) for k, v in lists.items()}}
    return conn.execute(f"INSERT INTO compliance_checks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        tuple(cols.values())).lastrowid


def latest_compliance_check(conn, product_id):
    """품목의 가장 최근 통관·규제 판정 (없으면 None)."""
    return conn.execute("SELECT * FROM compliance_checks WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                        (product_id,)).fetchone()


# 제품 사양 사실 항목 (compliance_agent/compliance_agent/flags.py FLAGS 중 사실 항목만 — 바꾸면 같이 바꿀 것).
# 상표·라이선스·어린이 대상·캐릭터 디자인·의료 효과는 3번 정책이 정하므로 사양으로 받지 않는다.
SPEC_FLAG_KEYS = {
    "power_source": ("none", "ac", "usb", "battery_lithium", "battery_other"),
    "is_electronic": bool, "has_heating_or_motor": bool, "wireless": bool, "food_contact": bool,
    "chemical_product": bool, "hygiene_product": bool, "cosmetic": bool, "synthetic_resin_household": bool,
    "textile_wearable": bool, "origin_country": str,
}


def _spec_value_ok(key, value):
    rule = SPEC_FLAG_KEYS.get(key)
    if rule is None or value is None:
        return False
    if rule is bool:
        return isinstance(value, bool)
    if rule is str:
        return isinstance(value, str) and 0 < len(value.strip()) <= 10
    return value in rule


def merge_supplier_specs(conn, ps_id, flags, evidence, materials=None):
    """공급처가 답장으로 알려 준 사양을 product_suppliers.supplier_specs_json에 합친다 (5번이 쓰고 3번이 읽음).
    - flags: 사실 항목(SPEC_FLAG_KEYS)만, 값 형식이 맞는 것만. 새로 확인된 값만 덮어쓰고 null은 기존 값을 지우지 않음
    - evidence: [{"claim": "...", "message_id": 123}] 누적 (같은 항목은 한 번만)
    - materials: 누적 (중복 제거)
    - updated_at: 합친 시각 — 3번이 '최신 판정 이후 공급처 답이 바뀌었나'를 볼 때 씀
    합친 결과(dict)를 돌려준다. 받은 값 중 쓸 게 없으면 저장하지 않고 기존 값을 돌려준다."""
    row = conn.execute("SELECT supplier_specs_json FROM product_suppliers WHERE id = ?", (ps_id,)).fetchone()
    if row is None:
        raise ValueError(f"품목-공급처 연결 번호 {ps_id}가 없습니다")
    try:
        cur = json.loads(row["supplier_specs_json"]) if row["supplier_specs_json"] else {}
    except ValueError:
        cur = {}
    cur = cur if isinstance(cur, dict) else {}
    old_flags = cur.get("flags") if isinstance(cur.get("flags"), dict) else {}
    old_ev = cur.get("evidence") if isinstance(cur.get("evidence"), list) else []
    old_mat = cur.get("materials") if isinstance(cur.get("materials"), list) else []
    new_flags = {k: v for k, v in (flags or {}).items() if _spec_value_ok(k, v)}
    new_ev = [e for e in (evidence or []) if isinstance(e, dict) and str(e.get("claim") or "").strip() and e not in old_ev]
    new_mat = [str(m).strip() for m in (materials or []) if str(m).strip() and str(m).strip() not in old_mat]
    new_flags = {k: v for k, v in new_flags.items() if old_flags.get(k) != v}
    if not (new_flags or new_ev or new_mat):
        return cur
    merged = {"flags": {**old_flags, **new_flags}, "materials": old_mat + list(dict.fromkeys(new_mat)),
              "evidence": old_ev + new_ev, "updated_at": now()}
    conn.execute("UPDATE product_suppliers SET supplier_specs_json = ?, updated_at = ? WHERE id = ?",
                 (dumps(merged), now(), ps_id))
    return merged


# MOQ 판단 기준: max_order_qty가 비어 있으면 구매 방식 기본값 (oem = 제한 없음), 구매 방식도 모르면 50
DEFAULT_MAX_ORDER_QTY = {"retail": 10, "small_batch": 50, "oem": None}
FALLBACK_MAX_ORDER_QTY = 50


def first_qty(text):
    """MOQ 원문에서 첫 숫자 (예: "1 piece" → 1, "25 pcs (…), 50 pcs" → 25, "1,000 pcs" → 1000). 없으면 None."""
    m = re.search(r"\d[\d,]*", text or "")
    return int(m[0].replace(",", "")) if m else None


def order_limit(purchase_mode=None, max_order_qty=None):
    """첫 구매 최대 수량. None이면 제한 없음 (oem)."""
    if max_order_qty is not None:
        return int(max_order_qty)
    mode = (purchase_mode or "").strip().lower()
    return DEFAULT_MAX_ORDER_QTY[mode] if mode in DEFAULT_MAX_ORDER_QTY else FALLBACK_MAX_ORDER_QTY


def moq_fit(moq, purchase_mode=None, max_order_qty=None):
    """(판정, MOQ 첫 숫자, 기준 수량). 판정: ok(기준 이하) / over(기준 초과) / unknown(MOQ 없음) / no_limit(oem)."""
    qty, limit = first_qty(moq), order_limit(purchase_mode, max_order_qty)
    if qty is None:
        return "unknown", None, limit
    if limit is None:
        return "no_limit", qty, None
    return ("ok" if qty <= limit else "over"), qty, limit


def _check_fields(fields, allowed, what):
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"알 수 없는 {what} 필드: {unknown}")
    return {k: v for k, v in fields.items() if v is not None}


def upsert_supplier(conn, company, country=None, **fields):
    """(업체명, 국가)로 공급처를 찾아 갱신하거나 새로 만든다. None인 값은 기존 값을 덮어쓰지 않는다. id를 돌려준다."""
    fields = _check_fields(fields, SUPPLIER_FIELDS, "공급처")
    row = conn.execute("SELECT id FROM suppliers WHERE company = ? AND country IS ?", (company, country)).fetchone()
    if row is None:
        cols = ["company", "country", *fields]
        return conn.execute(f"INSERT INTO suppliers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            (company, country, *fields.values())).lastrowid
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE suppliers SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), now(), row["id"]))
    return row["id"]


def link_supplier(conn, product_id, supplier_id, run_id=None, **fields):
    """품목-공급처 연결을 만들거나 갱신한다. status(연락·견적 진행 상태)는 다른 단계가 바꾼 값을 유지. id를 돌려준다."""
    fields = _check_fields(fields, LINK_FIELDS, "품목-공급처")
    fields = {k: dumps(v) if k.endswith("_json") and not isinstance(v, str) else v for k, v in fields.items()}
    # 한 줄 = (품목, 공급처, 구매 방식) [v8]. 구매 방식을 주면 그 방식의 줄 → 없으면 방식이 비어 있는 예전 줄을 이어받음.
    # 구매 방식을 안 주면 줄이 하나일 때만 그 줄을 갱신 (여러 방식이 있으면 어느 줄인지 알 수 없어 오류)
    rows = conn.execute("SELECT id, purchase_mode FROM product_suppliers WHERE product_id = ? AND supplier_id = ?",
                        (product_id, supplier_id)).fetchall()
    mode = fields.get("purchase_mode")
    if mode is not None:
        row = (next((r for r in rows if r["purchase_mode"] == mode), None)
               or next((r for r in rows if r["purchase_mode"] is None), None))
    elif len(rows) > 1:
        raise ValueError(f"품목 {product_id}·공급처 {supplier_id}의 줄이 구매 방식별로 {len(rows)}개 → purchase_mode를 지정하세요")
    else:
        row = rows[0] if rows else None
    if row is None:
        cols = ["product_id", "supplier_id", "run_id", *fields]
        return conn.execute(f"INSERT INTO product_suppliers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            (product_id, supplier_id, run_id, *fields.values())).lastrowid
    sets = ", ".join(f"{k} = ?" for k in ["run_id", *fields])
    conn.execute(f"UPDATE product_suppliers SET {sets}, updated_at = ? WHERE id = ?",
                 (run_id, *fields.values(), now(), row["id"]))
    return row["id"]


# ---------------------------------------------------------------------------
# 사진 — products.image_url = 확정 대표 사진 (직접 업로드 또는 공급사 사진 중 선택)
#        product_suppliers.image_url = 공급사가 준 사진
#        검색으로 찾은 참고 이미지는 DB에 저장하지 않는다 (대시보드가 화면에만 표시)
# ---------------------------------------------------------------------------
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def images_dir():
    return default_path().parent / "images"


def resolve_image(value):
    """image_url 값 → 브라우저에서 열 수 있는 주소 (http(s)는 그대로, 상대경로는 DB 폴더 기준 file:// URI). 없으면 None."""
    if not value:
        return None
    if str(value).startswith(("http://", "https://")):
        return str(value)
    path = Path(value)
    path = path if path.is_absolute() else default_path().parent / path
    return path.resolve().as_uri() if path.exists() else None


def set_product_photo(conn, product_id, src):
    """사진 파일을 data/images/<품목번호><확장자>로 복사하고 products.image_url에 연결한다. 저장한 상대경로를 돌려준다."""
    import shutil
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"사진 파일이 없습니다: {src}")
    ext = src.suffix.lower()
    if ext not in PHOTO_EXTS:
        raise ValueError(f"사진 형식이 아닙니다 ({ext}). 가능: {', '.join(sorted(PHOTO_EXTS))}")
    if conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone() is None:
        raise ValueError(f"품목 번호 {product_id}가 없습니다")
    images_dir().mkdir(parents=True, exist_ok=True)
    for old in images_dir().glob(f"{product_id}.*"):     # 확장자가 바뀌어도 사진은 1장만
        old.unlink()
    shutil.copyfile(src, images_dir() / f"{product_id}{ext}")
    rel = f"images/{product_id}{ext}"
    conn.execute("UPDATE products SET image_url = ?, updated_at = ? WHERE id = ?", (rel, now(), product_id))
    return rel


def clear_product_photo(conn, product_id):
    """확정 사진 연결을 끊고, 업로드했던 파일이면 지운다."""
    for old in images_dir().glob(f"{product_id}.*"):
        old.unlink()
    conn.execute("UPDATE products SET image_url = NULL, updated_at = ? WHERE id = ?", (now(), product_id))


def backup(conn, backup_dir=None, keep=14, today=None):
    """하루 1개씩 DB 백업 파일(trade_YYYYMMDD.db)을 만들고 오래된 것은 keep개만 남긴다.
    오늘 백업이 이미 있으면 아무것도 하지 않는다. 만든 파일 경로(없으면 None)를 돌려준다."""
    backup_dir = Path(backup_dir or default_path().parent / "backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"trade_{(today or datetime.now()):%Y%m%d}.db"
    made = None
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        dest = sqlite3.connect(tmp)
        try:
            conn.backup(dest)      # 다른 에이전트가 쓰는 중이어도 일관된 스냅샷
        finally:
            dest.close()
        tmp.replace(target)
        made = target
    for old in sorted(backup_dir.glob("trade_*.db"))[:-keep or None]:
        old.unlink()
    return made


def stage_order_sql(col="stage"):
    return "CASE " + col + " " + " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(STAGES)) + " ELSE 99 END"
