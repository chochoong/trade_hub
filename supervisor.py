#!/usr/bin/env python3
"""
슈퍼바이저 — 1인 무역 에이전트 파이프라인의 관제탑.

  ① 스케줄러 (코드, LLM 없음): supervisor.json 규칙에 따라 각 에이전트를 실행할지 정하고 실행한다.
  ② 검토관 (LLM, 읽기 전용): 공용 DB 전체를 읽고 브리핑·경고·제안을 남긴다.
     DB에는 briefings와 tasks(kind='decision')만 쓴다. 단계 변경·메일 발송·다른 에이전트 데이터 수정은 하지 않는다.

사용:
  python supervisor.py                 # 상태 보기 (에이전트별 마지막 실행, 실행 예정 여부)
  python supervisor.py tick            # 스케줄 실행 + (주기가 되면) 검토. 작업 스케줄러에 1시간마다 등록용
  python supervisor.py tick --dry-run  # 무엇을 실행할지 보기만
  python supervisor.py schedule        # 스케줄러만
  python supervisor.py review          # 검토관만 (지금 바로)
  python supervisor.py review --no-llm # LLM 없이 규칙 기반 경고만
  python supervisor.py backup          # 공용 DB 백업 (tick에서 하루 1번 자동)

사람이 공용 DB를 보고 다루는 명령은 trade_hub (python -m trade_hub.cli) 참고.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import supervisor_llm as llm
from trade_hub import db as trade_db

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "supervisor.json"
LOG_DIR = ROOT / "data" / "logs"


def load_config(path=CONFIG_PATH):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg.setdefault("agents", {})
    cfg.setdefault("review", {})
    cfg.setdefault("alerts", {})
    cfg.setdefault("backup", {})
    return cfg


def parse_ts(s):
    return datetime.fromisoformat(s) if s else None


def hours_since(ts, now):
    return None if ts is None else (now - ts).total_seconds() / 3600


# ---------------------------------------------------------------------------
# ① 스케줄러
# ---------------------------------------------------------------------------
def last_activity(conn, agent):
    """에이전트의 마지막 실행 시각 (에이전트가 직접 남긴 agent_runs와 슈퍼바이저 실행 기록 중 최신)."""
    a = conn.execute("SELECT MAX(started_at) FROM agent_runs WHERE agent = ?", (agent,)).fetchone()[0]
    d = conn.execute("SELECT MAX(started_at) FROM supervisor_dispatches WHERE agent = ?", (agent,)).fetchone()[0]
    return max(filter(None, (parse_ts(a), parse_ts(d))), default=None)


def is_running(conn, agent, timeout_minutes, now):
    since = (now - timedelta(minutes=timeout_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    r = conn.execute("SELECT 1 FROM agent_runs WHERE agent = ? AND status = 'running' AND started_at > ?",
                     (agent, since)).fetchone()
    d = conn.execute("SELECT 1 FROM supervisor_dispatches WHERE agent = ? AND finished_at IS NULL AND started_at > ?",
                     (agent, since)).fetchone()
    return bool(r or d)


DEFAULT_RETRY_HOURS = 3
DEFAULT_MAX_RETRIES = 2


def failure_streak(conn, agent):
    """마지막 성공 이후 연속 실패 횟수 (0이면 마지막 시도가 성공했거나 기록 없음).
    슈퍼바이저가 실행해 실패한 횟수를 세고, 직접 실행(슈퍼바이저 밖)에서 실패한 것만 있으면 1로 본다."""
    ok_run = conn.execute("SELECT MAX(started_at) FROM agent_runs WHERE agent = ? AND status = 'succeeded'",
                          (agent,)).fetchone()[0]
    ok_disp = conn.execute("SELECT MAX(started_at) FROM supervisor_dispatches WHERE agent = ? AND exit_code = 0",
                           (agent,)).fetchone()[0]
    last_ok = max(filter(None, (ok_run, ok_disp)), default="")
    failed_disp = conn.execute(
        "SELECT COUNT(*) FROM supervisor_dispatches WHERE agent = ? AND exit_code <> 0 AND started_at > ?",
        (agent, last_ok)).fetchone()[0]
    if failed_disp:
        return failed_disp
    failed_run = conn.execute("SELECT 1 FROM agent_runs WHERE agent = ? AND status = 'failed' AND started_at > ?",
                              (agent, last_ok)).fetchone()
    return 1 if failed_run else 0


def decide(conn, name, spec, now):
    """(실행할지, 사유)를 돌려준다."""
    if not spec.get("enabled"):
        return False, "꺼짐"
    if not spec.get("command"):
        return False, "command 없음"
    if is_running(conn, name, spec.get("timeout_minutes", 60), now):
        return False, "실행 중"
    last = last_activity(conn, name)
    since = hours_since(last, now)
    n = None
    if spec.get("when_sql"):
        try:
            n = conn.execute(spec["when_sql"]).fetchone()[0] or 0
        except Exception as e:
            return False, f"when_sql 오류: {e}"

    # 실패 후 재시도: 주기를 기다리지 않고 retry_hours 뒤 다시. max_retries번 넘게 연속 실패하면 자동 실행을 멈춘다
    # (API 장애 등으로 비용만 반복되지 않게). 직접 실행해 한 번 성공하면 다시 자동 실행된다.
    streak = failure_streak(conn, name)
    if streak:
        max_retries = spec.get("max_retries", DEFAULT_MAX_RETRIES)
        retry_h = spec.get("retry_hours", DEFAULT_RETRY_HOURS)
        if streak > max_retries:
            return False, f"연속 실패 {streak}회 → 자동 실행 멈춤 (로그 확인 후 직접 실행해 성공하면 다시 시작)"
        if n is None or n > 0:
            if since is None or since >= retry_h:
                return True, f"실패 후 재시도 {streak}/{max_retries} (마지막 시도 {since or 0:.0f}시간 전)"
            return False, f"실패 → {retry_h - since:.1f}시간 뒤 재시도 ({streak}/{max_retries})"

    if n is not None:
        if n > 0:
            min_h = spec.get("min_interval_hours", 1)
            if since is not None and since < min_h:
                return False, f"처리할 항목 {n}건 · 최근 실행 {since:.1f}시간 전 (최소 간격 {min_h}시간)"
            return True, f"처리할 항목 {n}건"
    if spec.get("every_hours") is not None:
        if since is None:
            return True, "첫 실행"
        if since >= spec["every_hours"]:
            return True, f"주기 도래 (마지막 실행 {since:.0f}시간 전, 주기 {spec['every_hours']}시간)"
        return False, f"다음 실행까지 {spec['every_hours'] - since:.0f}시간"
    return False, "처리할 항목 없음" if spec.get("when_sql") else "실행 조건 없음"


def dispatch(conn, run_id, name, spec, reason, log=print):
    """에이전트 명령을 실행하고 결과를 supervisor_dispatches에 남긴다."""
    cmd = [sys.executable if part == "{python}" else part for part in spec["command"]]
    cwd = (ROOT / spec.get("cwd", ".")).resolve()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.log"
    started = trade_db.now()
    with conn:
        did = conn.execute(
            "INSERT INTO supervisor_dispatches (run_id, agent, reason, command, log_path, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, name, reason, subprocess.list2cmdline(cmd), str(log_path), started)).lastrowid
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TRADE_DB": str(trade_db.default_path().resolve())}
    timeout_min = spec.get("timeout_minutes", 60)
    log(f"  ▶ {name} 실행: {reason}")
    with open(log_path, "w", encoding="utf-8") as f:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, **_new_group())
        except OSError as e:   # 명령·폴더 없음 등
            code = -2
            f.write(f"\n[supervisor] 실행 실패: {e}\n")
            log(f"    ✗ 실행 실패: {e}")
        else:
            try:
                code = proc.wait(timeout=timeout_min * 60)
            except subprocess.TimeoutExpired:
                kill_tree(proc)        # 에이전트가 띄운 하위 프로세스까지 함께 종료
                code = -1
                f.write(f"\n[supervisor] 시간 초과 ({timeout_min}분) → 강제 종료\n")
                log(f"    ✗ 시간 초과 ({timeout_min}분) → 강제 종료")
    with conn:
        conn.execute("UPDATE supervisor_dispatches SET exit_code = ?, finished_at = ? WHERE id = ?",
                     (code, trade_db.now(), did))
        if code != 0:
            # 강제 종료·비정상 종료로 에이전트가 실패를 못 남기고 'running'으로 남은 실행을 정리
            n = conn.execute(
                "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ? "
                "WHERE agent = ? AND status = 'running' AND started_at >= ?",
                (f"슈퍼바이저: 종료 코드 {code}로 끝났는데 실행 기록이 'running'으로 남아 실패 처리",
                 trade_db.now(), name, started)).rowcount
            if n:
                log(f"    · 'running'으로 남은 {name} 실행 {n}건을 실패로 정리")
    log(f"    {'✓ 완료' if code == 0 else f'✗ 종료 코드 {code}'} · 로그: {log_path}")
    return code


def _new_group():
    """에이전트를 별도 프로세스 그룹으로 실행 (시간 초과 시 하위 프로세스까지 한꺼번에 종료하려고)."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc):
    """proc와 그 하위 프로세스를 모두 종료하고 끝날 때까지 기다린다."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def cleanup_stale_runs(conn, hours, now=None, log=print):
    """hours시간 넘게 'running'인 실행 기록을 실패로 정리한다 (슈퍼바이저 밖에서 실행됐다가 죽은 경우 등).
    정리한 건수를 돌려준다."""
    now = now or datetime.now()
    cutoff = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    with conn:
        n = conn.execute(
            "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ? "
            "WHERE status = 'running' AND started_at < ?",
            (f"슈퍼바이저: {hours}시간 넘게 'running' 상태라 비정상 종료로 보고 실패 처리",
             now.strftime("%Y-%m-%dT%H:%M:%S"), cutoff)).rowcount
    if n:
        log(f"  · {hours}시간 넘게 'running'으로 남은 실행 {n}건을 실패로 정리")
    return n


@contextmanager
def single_instance(lock_path):
    """슈퍼바이저가 동시에 두 개 돌지 않게 하는 잠금. 이미 잡혀 있으면 False를 내준다.
    OS 파일 잠금이라 프로세스가 죽으면 자동으로 풀린다."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def schedule(conn, cfg, run_id=None, dry_run=False, log=print):
    now = datetime.now()
    ran = 0
    for name, spec in cfg["agents"].items():
        due, reason = decide(conn, name, spec, now)
        if not due:
            continue
        if dry_run:
            log(f"  (dry-run) {name} 실행 예정: {reason}")
            continue
        dispatch(conn, run_id, name, spec, reason, log)
        ran += 1
    if not ran and not dry_run:
        log("  실행할 에이전트 없음")
    return ran


# ---------------------------------------------------------------------------
# ② 검토관
# ---------------------------------------------------------------------------
def rows(conn, sql, *params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


_SCORE_RE = re.compile(r"^\s*([\d.]+)\s*점\s*\(([A-Z])\)")


def top_suppliers(conn, limit=30):
    """진행 중 품목의 추천 공급처 요약 (검토관이 '어느 업체에 먼저 연락할지' 판단하는 재료).
    2번이 trust_reason을 "<점수>점 (<등급>) — 요약", red_flags를 ', '로 이은 문장들로 남긴다 (docs 7.1)."""
    out = []
    for r in rows(conn, """
            SELECT p.id AS product_id, p.name AS product, s.company, s.country, s.trust, s.trust_reason, s.red_flags,
                   ps.status, ps.moq, ps.unit_price_usd, ps.price_note
            FROM product_suppliers ps JOIN products p ON p.id = ps.product_id JOIN suppliers s ON s.id = ps.supplier_id
            WHERE ps.is_top_pick = 1 AND ps.status <> 'rejected' AND p.stage NOT IN ('candidate', 'closed', 'dropped')
            ORDER BY p.id, ps.id LIMIT ?""", limit):
        m = _SCORE_RE.match(r.pop("trust_reason") or "")
        flags = (r.pop("red_flags") or "").strip()
        r["score"], r["grade"] = (float(m[1]), m[2]) if m else (None, None)
        r["red_flag_count"] = len([x for x in re.split(r"(?<=[.다])\s*,\s+", flags) if x.strip()]) if flags else 0
        r["red_flags_brief"] = flags[:200] or None
        r["moq"] = (r["moq"] or "")[:80] or None
        r["price_note"] = (r["price_note"] or "")[:80] or None
        out.append(r)
    return out


def snapshot(conn):
    """검토관이 볼 DB 요약 (LLM에 그대로 전달)."""
    return {
        "now": trade_db.now(),
        "stage_counts": {r["stage"]: r["n"] for r in rows(
            conn, f"SELECT stage, COUNT(*) n FROM products GROUP BY stage ORDER BY {trade_db.stage_order_sql()}")},
        "active_products": rows(conn, """
            SELECT p.id, p.name, p.stage, p.updated_at,
                   (SELECT MAX(at) FROM product_events e WHERE e.product_id = p.id) AS last_event_at,
                   (SELECT COUNT(*) FROM product_suppliers ps WHERE ps.product_id = p.id) AS suppliers,
                   (SELECT COUNT(*) FROM quotes q JOIN product_suppliers ps ON ps.id = q.product_supplier_id
                     WHERE ps.product_id = p.id) AS quotes,
                   (SELECT COUNT(*) FROM tasks t WHERE t.product_id = p.id AND t.status = 'pending') AS pending_tasks
            FROM products p WHERE p.stage NOT IN ('candidate', 'closed', 'dropped')"""),
        "top_suppliers": top_suppliers(conn),
        "top_candidates": rows(conn, """
            SELECT p.id, p.name, p.category, p.created_at, s.total, s.demand, s.margin, s.competition, s.naver_json
            FROM products p
            JOIN product_scores s ON s.id = (SELECT MAX(id) FROM product_scores WHERE product_id = p.id)
            WHERE p.stage = 'candidate' ORDER BY s.total DESC LIMIT 10"""),
        "pending_tasks": rows(conn, """
            SELECT id, product_id, kind, title, priority, due_date, created_at FROM tasks
            WHERE status = 'pending' ORDER BY priority, created_at LIMIT 30"""),
        "low_margin_offers": rows(conn, """
            SELECT p.id AS product_id, p.name, s.company, ps.est_margin_pct, ps.status
            FROM product_suppliers ps JOIN products p ON p.id = ps.product_id JOIN suppliers s ON s.id = ps.supplier_id
            WHERE p.stage NOT IN ('closed', 'dropped') AND ps.status <> 'rejected' AND ps.est_margin_pct IS NOT NULL
            ORDER BY ps.est_margin_pct LIMIT 10"""),
        "recent_runs": rows(conn, """
            SELECT id, agent, status, started_at, finished_at, substr(error, 1, 200) AS error FROM agent_runs
            WHERE started_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime', '-7 days') ORDER BY id DESC LIMIT 30"""),
        "recent_dispatches": rows(conn, """
            SELECT agent, reason, exit_code, started_at, finished_at, log_path FROM supervisor_dispatches
            WHERE started_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime', '-7 days') ORDER BY id DESC LIMIT 20"""),
        "pnl": rows(conn, "SELECT * FROM v_product_pnl WHERE revenue_krw <> 0 OR cost_krw <> 0"),
    }


def rule_alerts(conn, cfg, snap, now=None):
    """규칙 기반 경고 (LLM 없이도 항상 계산). level: warn | info"""
    now = now or datetime.now()
    a = {"task_stale_days": 3, "product_stale_days": 14, "candidate_waiting_days": 7,
         "candidate_good_score": 65, "low_margin_pct": 15, "run_stuck_hours": 6, **cfg.get("alerts", {})}
    out = []

    def add(level, text, product_id=None):
        out.append({"level": level, "text": text, "product_id": product_id})

    for r in snap["recent_runs"]:
        if r["status"] == "failed":
            add("warn", f"{r['agent']} 실행 #{r['id']} 실패 ({r['started_at']}): {r['error'] or '오류 내용 없음'}")
        elif r["status"] == "running" and hours_since(parse_ts(r["started_at"]), now) > a["run_stuck_hours"]:
            add("warn", f"{r['agent']} 실행 #{r['id']}이 {a['run_stuck_hours']}시간 넘게 'running' 상태 (비정상 종료 의심)")
    for d in snap["recent_dispatches"]:
        if d["exit_code"] not in (0, None):
            add("warn", f"슈퍼바이저가 실행한 {d['agent']}가 종료 코드 {d['exit_code']}로 끝남 ({d['started_at']}) · 로그: {d['log_path']}")
    for t in snap["pending_tasks"]:
        age = hours_since(parse_ts(t["created_at"]), now) / 24
        overdue = t["due_date"] and t["due_date"] < now.strftime("%Y-%m-%d")
        if overdue or age >= a["task_stale_days"]:
            add("warn", f"할 일 #{t['id']} '{t['title']}' " + ("기한 지남" if overdue else f"{age:.0f}일째 대기"),
                t["product_id"])
    for p in snap["active_products"]:
        idle = hours_since(parse_ts(p["last_event_at"] or p["updated_at"]), now) / 24
        if idle >= a["product_stale_days"]:
            add("info", f"'{p['name']}'({p['stage']})가 {idle:.0f}일째 진행 없음", p["id"])
        # 2번이 '공급처를 찾지 못함' 할 일을 이미 만들었으면 (승인 대기함에 있음) 같은 내용을 또 경고하지 않음
        if p["stage"] == "sourcing" and p["suppliers"] == 0 and idle >= 2 and not p["pending_tasks"]:
            add("info", f"'{p['name']}'가 공급처 발굴 단계인데 {idle:.0f}일째 공급처 0곳", p["id"])
    for c in snap["top_candidates"]:
        wait = hours_since(parse_ts(c["created_at"]), now) / 24
        if (c["total"] or 0) >= a["candidate_good_score"] and wait >= a["candidate_waiting_days"]:
            add("info", f"고득점 후보 '{c['name']}'({c['total']}점)가 {wait:.0f}일째 선정 대기 → 진행/중단 결정 필요", c["id"])
    for o in snap["low_margin_offers"]:
        if o["est_margin_pct"] < a["low_margin_pct"]:
            add("warn", f"'{o['name']}' × {o['company']} 추정 마진 {o['est_margin_pct']}% (기준 {a['low_margin_pct']}% 미만)",
                o["product_id"])
    for name, spec in cfg["agents"].items():
        streak = failure_streak(conn, name) if spec.get("enabled") else 0
        if streak > spec.get("max_retries", DEFAULT_MAX_RETRIES):
            add("warn", f"{name} 에이전트가 연속 {streak}회 실패해 자동 실행을 멈췄습니다 → 로그 확인 후 직접 실행 필요")
        every = spec.get("every_hours")
        if spec.get("enabled") and every is not None:
            since = hours_since(last_activity(conn, name), now)
            if since is not None and since > every * 2:
                add("warn", f"{name} 에이전트가 {since:.0f}시간째 실행 안 됨 (주기 {every}시간)")
    return out


REVIEW_SYSTEM = """당신은 1인 수입 무역 에이전트 파이프라인의 '슈퍼바이저 검토관'입니다.
여러 에이전트(시장조사, 공급처 발굴, 협상, 서류, 통관, 원가, 판매, 재무)가 공용 DB에 남긴 결과를 읽고,
사용자(1인 사업자)가 오늘 무엇을 먼저 해야 하는지 브리핑합니다.

원칙:
- 주어진 DB 요약과 규칙 경고에 있는 사실만 사용합니다. 없는 수치나 사건을 만들지 않습니다.
- 당신은 읽기 전용입니다. 메일 발송·결제·단계 변경을 직접 하지 않고, 사람에게 '결정할 일'을 제안만 합니다.
- 단계를 넘나드는 판단을 우선합니다 (예: 견적 기준 마진이 처음 추정보다 크게 낮음, 고득점 후보가 방치됨, 같은 에이전트가 반복 실패).
- top_suppliers는 진행 중 품목의 추천 공급처입니다 (score·grade: 2번 평가, red_flag_count: 위험 요소 수). 공급처 검토가 대기 중이면 점수와 위험을 근거로 먼저 연락할 업체를 짚어 줍니다.
- pending_tasks에 이미 같은 취지의 할 일이 있으면 새로 제안하지 않습니다 (표현만 바꾼 중복 금지). 새로 결정할 것이 없으면 suggestions는 빈 목록입니다.
- 짧고 구체적으로. 품목은 이름과 번호로 지칭합니다.
- 결과는 반드시 submit_briefing 도구로 제출합니다."""

SUBMIT_BRIEFING_TOOL = {
    "name": "submit_briefing",
    "description": "오늘의 브리핑과 사람이 결정할 일 제안을 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "오늘의 브리핑 (한국어 마크다운, 15줄 이내): 전체 현황 → 주의할 점 → 오늘 할 일 순"},
            "suggestions": {
                "type": "array",
                "description": "사람이 결정·처리할 일 (최대 5개, 중요한 것부터)",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "할 일 제목 (한 줄)"},
                        "detail": {"type": "string", "description": "근거와 권장 행동"},
                        "priority": {"type": "number", "enum": [1, 2, 3], "description": "1 높음 · 2 보통 · 3 낮음"},
                        "product_id": {"type": ["number", "null"], "description": "관련 품목 번호 (없으면 null)"},
                    },
                    "required": ["title", "detail", "priority", "product_id"],
                },
            },
        },
        "required": ["summary", "suggestions"],
    },
}


def plain_summary(snap, alerts):
    """LLM 없이 만드는 간단한 브리핑."""
    stages = " · ".join(f"{k} {v}" for k, v in snap["stage_counts"].items()) or "품목 없음"
    lines = [f"**현황**: {stages} · 대기 할 일 {len(snap['pending_tasks'])}건"]
    if alerts:
        lines.append("**경고**")
        lines += [f"- {'⚠️' if x['level'] == 'warn' else 'ℹ️'} {x['text']}" for x in alerts[:15]]
    else:
        lines.append("특이사항 없음")
    return "\n".join(lines)


def review(conn, cfg, run_id, use_llm=True, log=print):
    snap = snapshot(conn)
    alerts = rule_alerts(conn, cfg, snap)
    suggestions = []
    if use_llm:
        backend = llm.make_backend(cfg["review"].get("provider"), cfg["review"].get("model"))
        log(f"  · 검토관: {backend[3]} · {backend[1]}")
        data = llm.submit(
            backend, REVIEW_SYSTEM,
            "공용 DB 요약(JSON)과 규칙 기반 경고입니다. 오늘의 브리핑과 결정할 일을 submit_briefing으로 제출하세요.\n\n"
            f"<db_snapshot>\n{json.dumps(snap, ensure_ascii=False, default=str)}\n</db_snapshot>\n\n"
            f"<rule_alerts>\n{json.dumps(alerts, ensure_ascii=False)}\n</rule_alerts>",
            SUBMIT_BRIEFING_TOOL)
        summary = data["summary"]
        suggestions = data["suggestions"][:5]
    else:
        summary = plain_summary(snap, alerts)

    valid_ids = {r[0] for r in conn.execute("SELECT id FROM products")}
    with conn:
        expired = expire_decisions(conn, cfg["review"].get("decision_expire_days", 7))
        if expired:
            log(f"  · 오래된 검토관 제안 {expired}건 자동 기각")
        bid = conn.execute(
            "INSERT INTO briefings (run_id, summary, alerts_json, source_runs_json) VALUES (?, ?, ?, ?)",
            (run_id, summary, trade_db.dumps(alerts), trade_db.dumps([r["id"] for r in snap["recent_runs"]]))
        ).lastrowid
        for s in suggestions:
            pid = int(s["product_id"]) if s.get("product_id") in valid_ids else None
            trade_db.add_task(conn, "decision", s["title"], product_id=pid, run_id=run_id,
                              detail=s["detail"], priority=int(s["priority"]))
    return bid, summary, alerts, suggestions


def expire_decisions(conn, days, now=None):
    """검토관이 만든 결정할 일 중 days일 넘게 대기 중인 것을 dismissed로 바꾼다 (승인 대기함이 쌓이지 않게).
    사람이나 다른 에이전트가 만든 할 일은 건드리지 않는다. 바꾼 건수를 돌려준다."""
    if not days:
        return 0
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    return conn.execute(
        "UPDATE tasks SET status = 'dismissed', done_at = ? WHERE kind = 'decision' AND status = 'pending'"
        " AND created_at < ? AND run_id IN (SELECT id FROM agent_runs WHERE agent = 'supervisor')",
        (now.strftime("%Y-%m-%dT%H:%M:%S"), cutoff)).rowcount


def run_backup(conn, cfg, log=print):
    """설정에 따라 하루 1번 DB를 백업한다."""
    b = cfg.get("backup", {})
    if not b.get("enabled", True):
        return None
    made = trade_db.backup(conn, b.get("dir") and (ROOT / b["dir"]), keep=b.get("keep", 14))
    if made:
        log(f"  · DB 백업: {made}")
    return made


def refresh_dashboard(conn, cfg, log=print):
    """tick 끝에 대시보드 HTML을 다시 만든다 (열 때마다 최신). 실패해도 tick은 실패로 치지 않는다.
    설정: "dashboard": {"enabled": true, "refs": true}  — refs=false면 네이버 참고 이미지 검색을 하지 않음."""
    d = cfg.get("dashboard", {})
    if not d.get("enabled", True):
        return None
    try:
        from trade_hub import dashboard
        path = dashboard.build(conn=conn, refs=d.get("refs", True), log=log)
    except Exception as e:   # 대시보드는 부가 기능 — 에이전트 실행·검토 결과에 영향 주지 않음
        log(f"  ✗ 대시보드 갱신 실패: {type(e).__name__}: {e}")
        return None
    log(f"  · 대시보드 갱신: {path}")
    return path


def review_due(conn, cfg, now):
    if not cfg["review"].get("enabled", True):
        return False
    last = conn.execute("SELECT MAX(created_at) FROM briefings").fetchone()[0]
    since = hours_since(parse_ts(last), now)
    return since is None or since >= cfg["review"].get("every_hours", 20)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def show_status(conn, cfg):
    now = datetime.now()
    print("▶ 에이전트")
    for name, spec in cfg["agents"].items():
        last = last_activity(conn, name)
        due, reason = decide(conn, name, spec, now)
        mark = "▶ 실행 예정" if due else "·"
        print(f"  {name:<12} {'켜짐' if spec.get('enabled') else '꺼짐':<3} 마지막 {last:%m-%d %H:%M} " if last
              else f"  {name:<12} {'켜짐' if spec.get('enabled') else '꺼짐':<3} 마지막 -          ", end="")
        print(f"{mark} {reason}")
    b = conn.execute("SELECT created_at, summary FROM briefings ORDER BY id DESC LIMIT 1").fetchone()
    print("▶ 최근 브리핑" + (f" ({b['created_at']})\n{b['summary']}" if b else ": 없음"))
    print(f"▶ 검토 예정: {'예' if review_due(conn, cfg, now) else '아니오'}")


def print_review(summary, alerts, suggestions):
    print("\n" + summary)
    if suggestions:
        print("\n▶ 결정할 일 (승인 대기함에 추가)")
        for s in suggestions:
            print(f"  [{int(s['priority'])}] {s['title']}")
    print(f"\n(규칙 경고 {len(alerts)}건)")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="슈퍼바이저 (스케줄러 + 검토관)")
    ap.add_argument("cmd", nargs="?", default="status", choices=["status", "tick", "schedule", "review", "backup"])
    ap.add_argument("--dry-run", action="store_true", help="에이전트를 실행하지 않고 실행 예정만 표시")
    ap.add_argument("--no-llm", action="store_true", help="검토관이 LLM 없이 규칙 기반 경고만 남김")
    ap.add_argument("--no-review", action="store_true", help="tick에서 검토를 건너뜀")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    llm.load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    conn = trade_db.connect()
    print(f"DB: {trade_db.default_path()}")

    if args.cmd == "status":
        show_status(conn, cfg)
        return
    if args.cmd == "backup":
        made = trade_db.backup(conn, cfg["backup"].get("dir") and (ROOT / cfg["backup"]["dir"]),
                               keep=cfg["backup"].get("keep", 14))
        print(f"✓ 백업: {made}" if made else "· 오늘 백업이 이미 있습니다")
        return
    if args.dry_run:
        print("▶ 스케줄 (dry-run)")
        schedule(conn, cfg, dry_run=True)
        if args.cmd == "tick":
            print(f"▶ 검토: {'예정' if review_due(conn, cfg, datetime.now()) and not args.no_review else '없음'}")
        return

    # 작업 스케줄러가 1시간마다 띄우므로, 이전 tick이 아직 돌고 있으면 겹쳐 실행하지 않는다
    with single_instance(trade_db.default_path().parent / "supervisor.lock") as acquired:
        if not acquired:
            print("· 다른 슈퍼바이저가 실행 중이라 건너뜀")
            return
        cleanup_stale_runs(conn, cfg["alerts"].get("run_stuck_hours", 6))
        with trade_db.agent_run(conn, "supervisor", args={"cmd": args.cmd, "no_llm": args.no_llm}) as run:
            if args.cmd == "tick":
                run_backup(conn, cfg)     # 에이전트 실행 전에 하루 1번
            if args.cmd in ("tick", "schedule"):
                print("▶ 스케줄")
                schedule(conn, cfg, run.id)
            if args.cmd == "review" or (args.cmd == "tick" and not args.no_review
                                         and review_due(conn, cfg, datetime.now())):
                print("▶ 검토")
                bid, summary, alerts, suggestions = review(conn, cfg, run.id, use_llm=not args.no_llm)
                print_review(summary, alerts, suggestions)
            if args.cmd == "tick":
                print("▶ 대시보드")
                refresh_dashboard(conn, cfg)   # 에이전트 결과·브리핑까지 반영된 뒤에


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"\n✗ 중단: {e}")
