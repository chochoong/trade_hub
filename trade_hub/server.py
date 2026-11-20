"""
trade_hub serve — 내 PC에서만 열리는 대시보드 서버 (http://127.0.0.1:8765).

정적 대시보드(trade_hub dashboard)와 같은 화면에 다음을 더한다.
  · 검색창 → DB에 없는 품목은 [새로 조사] → 1번 에이전트 지정 품목 조사 (supplier_agent.py --product)
  · 후보 품목 [공급처 찾기] → 사람이 선정(candidate → sourcing) + 2번 에이전트 실행 (sourcing_agent from-db <번호>)
  · 구매대행 '직접 검색' → buyagent main.py --search (일본 쇼핑몰 최저가·원가·권장가, DB에 기록하지 않음)
  · 3번 통관·규제 페이지 [전체 점검]·품목별 [다시 점검] → 3번 에이전트 실행
    (python -m compliance_agent check-hub [번호] --official api|files|off). 판정은 3번이 compliance_checks에 기록
  · 5번 협상 페이지: [견적 요청 초안]·[답장 붙여넣기]·[역제안]·[후속 메일]·[검토 완료/발송 완료/버림]
    → 5번 에이전트 실행 (python -m negotiation_agent ...). 메일은 보내지 않는다 — 발송은 사람이 직접 하고 '발송 완료'만 누른다.
  · 실행 중인 작업 패널 (진행 로그), 끝나면 결과 품목으로 이동
  · 리포트·사진 같은 로컬 파일 보기 (/file — 허용된 폴더만)

원칙
  · 에이전트 일은 에이전트가 한다: 서버는 각 에이전트를 별도 프로세스로 실행만 한다 (슈퍼바이저와 같은 방식,
    실행 폴더·파이썬은 supervisor.json 설정 사용, TRADE_DB 전달). 결과는 각 에이전트가 공용 DB에 기록한다.
  · candidate → sourcing 은 사람이 버튼을 눌러 결정한다. 메일 발송·결제 같은 기능은 없다.
  · 같은 에이전트가 이미 실행 중이면(이 서버 또는 슈퍼바이저) 새로 실행하지 않는다.

보안: 127.0.0.1에만 열고, Host 헤더를 확인하며(DNS 리바인딩 방지), 실행 요청(POST)은
      커스텀 헤더 X-Trade-Hub: 1 이 있어야 받는다(다른 사이트에서 몰래 보내는 요청 차단).
"""

import itertools
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from trade_hub import buying, dashboard, db, translate

ROOT = Path(__file__).resolve().parent.parent          # trade_hub 저장소 폴더
CONFIG = ROOT / "supervisor.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_QUERY = 100
DISCOVER_COUNT = 10                                     # 자동 발굴 후보 수 (supervisor.json 주간 실행과 같게)
REF_TTL = 3600                                          # 참고 이미지 다시 검색하는 간격(초)

JOBS = {}                                               # 작업 id → dict (메모리, 서버를 끄면 사라짐)
LOCK = threading.Lock()
JOB_SEQ = itertools.count(1)                            # 작업 번호 (next()는 스레드에서 같이 불러도 안 겹침)
RENDER_LOCK = threading.Lock()                          # dashboard 모듈 상태(SERVER·_REFS)를 한 번에 하나만
_REF_STATE = {"at": 0.0, "need": None}


# ---------------------------------------------------------------------------
# 에이전트 실행 설정 (supervisor.json 공유)
# ---------------------------------------------------------------------------
def agent_spec(agent):
    """(실행 폴더, 파이썬, 제한 시간 분). 슈퍼바이저 설정의 command 첫 항목이 파이썬 경로면 그것
    (예: 2번 전용 venv), "{python}"이거나 없으면 이 서버를 돌리는 파이썬."""
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    spec = cfg.get("agents", {}).get(agent) or {}
    cwd = (CONFIG.parent / spec.get("cwd", ".")).resolve()
    python = sys.executable
    first = (spec.get("command") or ["{python}"])[0]
    if first != "{python}" and Path(first).name.lower().startswith("python"):
        cand = Path(first)
        cand = cand if cand.is_absolute() else cwd / cand
        if cand.exists():
            python = str(cand)
    return cwd, python, int(spec.get("timeout_minutes") or 60)


# 대시보드에서 고를 수 있는 2번 구매 방식 (슈퍼바이저 자동 실행은 supervisor.json대로 small_batch만)
PURCHASE_MODES = {"retail": "구매대행", "small_batch": "소량 사입", "oem": "대량 사입·OEM"}
COUNTRIES = db.SOURCE_COUNTRIES                   # 2번 sourcing_agent --country 값 (기본 China)


def job_command(kind, arg):
    """(명령, 실행 폴더, 제한 시간 분). 테스트에서 바꿔 끼울 수 있게 함수로 둔다."""
    if kind == "research":
        cwd, python, timeout = agent_spec("discover")
        return [python, "supplier_agent.py", "--product", arg, "--no-open"], cwd, timeout
    if kind == "discover":                  # 품목명 없이 자동 발굴: arg = 방향 힌트(없으면 "") — discover가 검증한 값
        cwd, python, timeout = agent_spec("discover")
        return [python, "supplier_agent.py", "--count", str(DISCOVER_COUNT), "--no-open"] + (
            ["--hint", arg] if arg else []), cwd, timeout
    if kind == "sourcing":                  # arg = (품목 번호, 구매 방식[, 국가]) — select가 검증한 값
        pid, mode, country = (*arg, None)[:3] if isinstance(arg, tuple) else (arg, "small_batch", None)
        if mode not in PURCHASE_MODES:
            raise ValueError(f"구매 방식: {mode!r}")
        if country is not None and country not in COUNTRIES:
            raise ValueError(f"국가: {country!r}")
        cwd, python, timeout = agent_spec("sourcing")
        cmd = [python, "-m", "sourcing_agent", "from-db", str(int(pid)), "--type", mode]
        return cmd + (["--country", country] if country else []), cwd, timeout   # 없으면 2번이 1번 추천 국가로
    if kind == "buysearch":                 # 구매대행: arg = (검색어, 카테고리[, 사이트]) — buy_search가 검증한 값
        q, cat, market = (*arg, "rakuten")[:3]
        if market not in BUY_MARKETS:
            raise ValueError(f"사이트: {market!r}")
        return ([sys.executable, "main.py", f"--search={q}", f"--category={cat}", f"--market={market}"],
                buying.buyagent_dir(), BUYSEARCH_TIMEOUT)
    if kind == "compliance":                # 3번: arg = (품목 번호 또는 None=진행 품목 전체, 공식 대조 방식) — compliance가 검증한 값
        pid, official = arg
        if official not in OFFICIAL_MODES:
            raise ValueError(f"공식 대조: {official!r}")
        cwd, python, timeout = agent_spec("compliance")
        target = [str(int(pid))] if pid else ["--recheck"]       # 번호를 주면 판정이 있어도 다시 점검
        return [python, "-m", "compliance_agent", "check-hub", *target, "--official", official], cwd, timeout
    if kind == "neg":                       # 5번: arg = negotiation_agent 명령 인자 목록 (neg_args가 검증해서 만든 것)
        cwd, python, timeout = agent_spec("negotiation")
        return [python, "-m", "negotiation_agent", *arg], cwd, timeout
    raise ValueError(kind)


KIND_AGENT = {"research": "discover", "discover": "discover", "sourcing": "sourcing", "compliance": "compliance",
              "neg": "negotiation", "buysearch": "buyagent"}
# 3번 관세청 공식 데이터 대조: api = HS·관세율 파일 + 세관장확인 API(인증키 DATA_GO_KR_KEY) / files = 파일만 / off
OFFICIAL_MODES = {"api": "공식 대조(파일+API)", "files": "공식 대조(파일만)", "off": "공식 대조 안 함"}
# 3번 실행에 넘길 키 (서버를 켠 뒤에 넣은 키도 쓰도록): 이 서버 환경변수 > 3번 폴더 .env > trade_hub .env > Windows 사용자 환경변수
COMPLIANCE_KEYS = ("ANTHROPIC_API_KEY", "DATA_GO_KR_KEY")
BUYSEARCH_TIMEOUT = 5                                   # 분 (라쿠텐 검색 페이지 2장 + 크롬 실행)
# 직접 검색 사이트 = buyagent --market (buyagent main.py SEARCH_MARKETS와 같은 값). 타오바오·1688은 화면에서 링크만
BUY_MARKETS = buying.BUY_MARKETS


def allowed_roots():
    """/file 로 보여 줄 수 있는 폴더: 확정 사진(data/images), 1번 리포트, 2번 출력, 3번 보고서. (DB 파일·백업·.env는 제외)"""
    roots = [db.images_dir().resolve()]
    for agent, sub in (("discover", "reports"), ("sourcing", "output"), ("compliance", "output")):
        try:
            roots.append((agent_spec(agent)[0] / sub).resolve())
        except Exception:
            pass
    return roots


# ---------------------------------------------------------------------------
# 작업 실행
# ---------------------------------------------------------------------------
def busy_reason(conn, agent):
    """이미 실행 중이면 이유 문자열, 아니면 None."""
    with LOCK:
        if any(j["agent"] == agent and j["status"] == "running" for j in JOBS.values()):
            return "이 서버에서 같은 에이전트가 이미 실행 중입니다. 끝난 뒤 다시 눌러 주세요."
    _, _, timeout = agent_spec(agent)
    since = (datetime.now() - timedelta(minutes=timeout)).strftime("%Y-%m-%dT%H:%M:%S")
    row = conn.execute("SELECT id FROM agent_runs WHERE agent = ? AND status = 'running' AND started_at >= ?",
                       (agent, since)).fetchone()
    if row:
        return f"다른 곳(슈퍼바이저 등)에서 실행 중입니다 (실행 #{row['id']}). 끝난 뒤 다시 눌러 주세요."
    return None


MAX_PARALLEL_SOURCING = 3   # 2번 공급처 찾기 동시 실행 수 (품목·구매 방식이 다르면 같이 돌림. LLM 비용·메모리 때문에 상한)


def sourcing_busy_reason(conn, pid, mode):
    """2번 공급처 찾기를 지금 시작하면 안 되는 이유, 괜찮으면 None.
    품목이 다르거나 구매 방식이 다르면 같이 돌린다 (DB 기록은 품목·공급처·구매 방식별로 따로 쌓임).
    같은 품목·같은 방식이 실행 중이거나, 동시 실행 상한이거나, 다른 곳(슈퍼바이저)이 '남은 품목 전부'를
    조사 중이면 (같은 품목을 겹쳐 조사할 수 있어) 막는다."""
    with LOCK:
        running = [j for j in JOBS.values() if j["agent"] == "sourcing" and j["status"] == "running"]
    if any(j.get("key") == (pid, mode) for j in running):
        return "이 품목을 같은 구매 방식으로 이미 찾는 중입니다. 끝난 뒤 다시 눌러 주세요."
    if len(running) >= MAX_PARALLEL_SOURCING:
        return f"공급처 찾기는 동시에 {MAX_PARALLEL_SOURCING}개까지 돌립니다. 하나가 끝난 뒤 다시 눌러 주세요."
    _, _, timeout = agent_spec("sourcing")
    since = (datetime.now() - timedelta(minutes=timeout)).strftime("%Y-%m-%dT%H:%M:%S")
    for row in conn.execute("SELECT id, args_json FROM agent_runs WHERE agent = 'sourcing' AND status = 'running' "
                            "AND started_at >= ?", (since,)):
        try:
            args = json.loads(row["args_json"] or "{}") or {}
        except ValueError:
            args = {}
        ids = args.get("ids") or []
        if not ids:
            return f"다른 곳(슈퍼바이저 등)에서 남은 품목 전부를 조사하느라 실행 중입니다 (실행 #{row['id']}). 끝난 뒤 다시 눌러 주세요."
        if pid in ids and args.get("sourcing_type", "small_batch") == mode:
            return f"다른 곳에서 이 품목을 같은 구매 방식으로 찾느라 실행 중입니다 (실행 #{row['id']}). 끝난 뒤 다시 눌러 주세요."
    return None


def _kill(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":       # 자식 프로세스까지 종료
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def dotenv_value(path, name):
    """.env에서 값 하나만 읽는다 (서버 환경변수 전체에 .env를 풀지 않으려고). 없으면 None."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        k, sep, v = line.strip().removeprefix("export ").partition("=")
        if sep and k.strip() == name:
            v = v.strip()
            if v[:1] in ('"', "'") and v[0] in v[1:]:
                return v[1:v.index(v[0], 1)] or None
            return re.split(r"\s#", v, maxsplit=1)[0].strip() or None
    return None


def windows_user_env(name):
    """Windows 사용자 환경변수 (서버를 켠 뒤에 setx·SetEnvironmentVariable로 넣은 값). 없거나 Windows가 아니면 None."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v = winreg.QueryValueEx(k, name)[0]
        return str(v).strip() or None
    except OSError:
        return None


def start_job(kind, arg, label, product_id=None, anchor=None, key=None):
    cmd, cwd, timeout = job_command(kind, arg)
    if not Path(cwd).is_dir():
        raise RuntimeError(f"에이전트 폴더가 없습니다: {cwd} (supervisor.json의 cwd 확인)")
    job_id = f"{datetime.now():%H%M%S}-{next(JOB_SEQ)}"   # 동시에 여러 작업을 시작해도 번호·로그 파일이 겹치지 않게
    logs = db.default_path().parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    job = {"id": job_id, "kind": kind, "agent": KIND_AGENT[kind], "label": label, "status": "running",
           "product_id": product_id, "anchor": anchor, "lines": [], "started": time.time(), "ended": None, "exit_code": None,
           "key": key, "log": str(logs / f"{datetime.now():%Y%m%d_%H%M%S}_serve_{kind}_{job_id}.log")}
    env = {**os.environ, "TRADE_DB": str(db.default_path().resolve()), "PYTHONIOENCODING": "utf-8",
           "PYTHONUNBUFFERED": "1"}   # 진행 줄을 바로바로 넘김 (없으면 파이프라 끝날 때 한꺼번에 와서 알림창이 내내 빈 칸)
    if kind == "buysearch" and not env.get("ANTHROPIC_API_KEY"):   # buyagent 한글 검색어 번역용 — 이 키 하나만 넘김
        key = dotenv_value(ROOT / ".env", "ANTHROPIC_API_KEY")
        if key:
            env["ANTHROPIC_API_KEY"] = key
    if kind == "compliance":                # 3번: LLM 키·관세청 키만 찾아서 넘김 (값은 로그·화면에 남기지 않음)
        for name in COMPLIANCE_KEYS:
            if not env.get(name):
                key = (dotenv_value(Path(cwd) / ".env", name) or dotenv_value(ROOT / ".env", name)
                       or windows_user_env(name))
                if key:
                    env[name] = key
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    job["proc"] = proc
    with LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_watch, args=(job, proc, timeout), daemon=True).start()
    return job


def _watch(job, proc, timeout):
    def on_timeout():
        job["timed_out"] = True
        _kill(proc)

    timer = threading.Timer(timeout * 60, on_timeout)
    timer.start()
    with open(job["log"], "w", encoding="utf-8") as f:
        f.write(f"$ {' '.join(map(str, proc.args))}\n")
        for line in proc.stdout:
            f.write(line)
            f.flush()                      # 실행 중에도 로그 파일로 진행 상황을 볼 수 있게
            line = line.rstrip()
            if line.startswith("RESULT_PRODUCT_ID="):
                try:
                    job["product_id"] = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                continue
            if line.startswith("RESULT_ANCHOR="):  # 5번: 끝나면 이동할 화면 위치 (예: ps-12)
                anchor = line.split("=", 1)[1].strip()
                if anchor.replace("-", "").isalnum():
                    job["anchor"] = anchor
                continue
            if line.strip():
                with LOCK:
                    job["lines"] = (job["lines"] + [line])[-6:]
    code = proc.wait()
    timer.cancel()
    with LOCK:
        job["exit_code"], job["ended"] = code, time.time()
        job["status"] = "cancelled" if job.get("cancelled") else "done" if code == 0 else "failed"
        if job.get("cancelled"):
            job["lines"] = (job["lines"] + ["사람이 중지했습니다"])[-6:]
        elif job.get("timed_out"):
            job["lines"] = (job["lines"] + [f"제한 시간 {timeout}분 초과로 중단했습니다"])[-6:]


def _stopped_runs(conn, job):
    """중지한 작업이 남긴 agent_runs(강제 종료라 'running'으로 남음)를 실패로 정리한다.
    안 하면 같은 품목·에이전트가 제한 시간 동안 '실행 중'으로 막힌다. 병렬 2번은 품목·구매 방식으로 구별."""
    since = datetime.fromtimestamp(job["started"] - 2).strftime("%Y-%m-%dT%H:%M:%S")
    ids = []
    for row in conn.execute("SELECT id, args_json FROM agent_runs WHERE agent = ? AND status = 'running' AND started_at >= ?",
                            (job["agent"], since)):
        if job.get("key"):
            try:
                args = json.loads(row["args_json"] or "{}") or {}
            except ValueError:
                args = {}
            pid, mode = job["key"]
            if pid not in (args.get("ids") or []) or args.get("sourcing_type", "small_batch") != mode:
                continue
        ids.append(row["id"])
    return ids


def stop(conn, body):
    """실행 중인 작업을 중지한다 (자식 프로세스까지 종료). 2번 공급처 찾기로 후보 → 공급처 발굴로 넘긴 품목은
    공급처가 하나도 저장되지 않았으면 후보로 되돌린다 (안 그러면 슈퍼바이저가 다음 정시에 2번을 자동으로 다시 돌림)."""
    with LOCK:
        job = JOBS.get(str(body.get("id") or ""))
    if job is None:
        return 404, {"error": "없는 작업입니다."}
    if job["status"] != "running":
        return 400, {"error": "이미 끝난 작업입니다."}
    job["cancelled"] = True
    _kill(job["proc"])
    with conn:
        for run_id in _stopped_runs(conn, job):
            conn.execute("UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                         ("사람이 대시보드에서 중지", db.now(), run_id))
        pid = job.get("revert_stage")
        if pid:
            row = conn.execute("SELECT stage FROM products WHERE id = ?", (pid,)).fetchone()
            has = conn.execute("SELECT 1 FROM product_suppliers WHERE product_id = ? LIMIT 1", (pid,)).fetchone()
            if row and row["stage"] == "sourcing" and not has:
                db.set_stage(conn, pid, "candidate", note="공급처 찾기 중지 → 후보로 되돌림 (대시보드)")
    with LOCK:
        job["status"] = "cancelled"
        job["ended"] = job["ended"] or time.time()
    return 200, job_view(job)


def job_view(j):
    sec = int((j["ended"] or time.time()) - j["started"])
    return {k: j.get(k) for k in ("id", "kind", "label", "status", "product_id", "anchor", "lines", "exit_code")} | {
        "elapsed": f"{sec // 60}분 {sec % 60}초" if sec >= 60 else f"{sec}초"}


# ---------------------------------------------------------------------------
# 요청 처리
# ---------------------------------------------------------------------------
def search(conn, q, limit=10):
    words = [w for w in q.lower().split() if w]
    if not words:
        return []
    rows = conn.execute("SELECT id, name, stage, category, search_keyword_en, keywords_ko_json FROM products").fetchall()
    out = []
    for r in rows:
        hay = " ".join(str(r[k] or "") for k in ("name", "category", "search_keyword_en", "keywords_ko_json")).lower()
        if all(w in hay for w in words):
            out.append({"id": r["id"], "name": r["name"], "stage": r["stage"]})
    return out[:limit]


def research(conn, body):
    q = " ".join(str(body.get("query") or "").split())
    if not q:
        return 400, {"error": "품목명을 입력해 주세요."}
    if len(q) > MAX_QUERY:
        return 400, {"error": f"품목명은 {MAX_QUERY}자 이내로 입력해 주세요."}
    reason = busy_reason(conn, "discover")
    if reason:
        return 409, {"error": reason}
    return 200, job_view(start_job("research", q, f"1번 조사: {q}"))


def discover(conn, body):
    """품목명 없이 1번 자동 발굴 (웹 검색 + 네이버 검색량으로 후보 10개). hint는 선택."""
    hint = " ".join(str(body.get("hint") or "").split())
    if len(hint) > MAX_QUERY:
        return 400, {"error": f"방향은 {MAX_QUERY}자 이내로 입력해 주세요."}
    reason = busy_reason(conn, "discover")
    if reason:
        return 409, {"error": reason}
    label = f"1번 자동 발굴 ({hint})" if hint else "1번 자동 발굴"
    return 200, job_view(start_job("discover", hint, label, anchor="a1"))


def select(conn, body):
    try:
        pid = int(body.get("product_id"))
    except (TypeError, ValueError):
        return 400, {"error": "품목 번호가 필요합니다."}
    mode = body.get("mode") or "small_batch"
    if mode not in PURCHASE_MODES:
        return 400, {"error": f"구매 방식은 {', '.join(PURCHASE_MODES)} 중 하나여야 합니다."}
    row = conn.execute("SELECT name, stage, source_country FROM products WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return 404, {"error": f"품목 번호 {pid}가 없습니다."}
    country = body.get("country") or row["source_country"] or "China"     # 고른 나라 > 1번 추천 > 중국
    if country not in COUNTRIES:
        return 400, {"error": f"국가는 {', '.join(COUNTRIES)} 중 하나여야 합니다."}
    if row["stage"] not in ("candidate", "sourcing"):
        return 400, {"error": f"#{pid}는 이미 {dashboard.STAGE_LABEL.get(row['stage'], row['stage'])} 단계입니다."}
    if mode == "oem" and row["stage"] == "candidate":   # 대량은 구매대행·소량으로 검증한 뒤 (2번 공급처 발굴 카드에서만)
        return 400, {"error": "대량 사입·OEM은 공급처 발굴 단계 품목에서만 찾을 수 있습니다 (소량으로 먼저 검증)."}
    reason = sourcing_busy_reason(conn, pid, mode)
    if reason:
        return 409, {"error": reason}
    if row["stage"] == "candidate":           # 사람이 버튼으로 결정 → 단계 이동 (trade_hub select와 같음)
        with conn:
            db.set_stage(conn, pid, "sourcing", note="사람이 선정 → 공급처 발굴로 (대시보드)")
    job = start_job("sourcing", (pid, mode, country),
                    f"2번 {PURCHASE_MODES[mode]}용 공급처 찾기 ({COUNTRIES[country]}): #{pid} {row['name']}",
                    product_id=pid, key=(pid, mode))
    if row["stage"] == "candidate":
        job["revert_stage"] = pid             # 중지하면 후보로 되돌릴 품목
    return 200, job_view(job)


def compliance(conn, body):
    """3번 통관·규제 점검. product_id가 있으면 그 품목만 다시, 없으면 진행 중인 품목 전체(--recheck)."""
    official = body.get("official") or "api"
    if official not in OFFICIAL_MODES:
        return 400, {"error": f"공식 대조는 {', '.join(OFFICIAL_MODES)} 중 하나여야 합니다."}
    pid, row = body.get("product_id"), None
    if pid not in (None, ""):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return 400, {"error": "품목 번호가 올바르지 않습니다."}
        row = conn.execute("SELECT name, stage FROM products WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return 404, {"error": f"품목 번호 {pid}가 없습니다."}
        if row["stage"] in ("candidate", "dropped"):
            return 400, {"error": f"#{pid}는 {dashboard.STAGE_LABEL.get(row['stage'], row['stage'])} 품목이라 점검하지 않습니다."}
    else:
        pid = None
    reason = busy_reason(conn, "compliance")
    if reason:
        return 409, {"error": reason}
    target = f"#{pid} {row['name']}" if pid else "진행 품목 전체"
    return 200, job_view(start_job("compliance", (pid, official), f"3번 통관·규제 점검 ({OFFICIAL_MODES[official]}): {target}",
                                   product_id=pid, anchor="a3"))   # 끝나면 3번 페이지로


def buy_search(conn, body):
    """구매대행 직접 검색: buyagent 를 --search 로 한 번 실행 → reports/search.json → 구매대행 화면에 표시."""
    q = " ".join(str(body.get("query") or "").split())
    cat = str(body.get("category") or "")
    if not q:
        return 400, {"error": "검색어를 입력해 주세요. (예: 세이코 SBTQ041)"}
    if len(q) > MAX_QUERY:
        return 400, {"error": f"검색어는 {MAX_QUERY}자 이내로 입력해 주세요."}
    base = Path(buying.buyagent_dir())
    if not (base / "main.py").is_file():
        return 400, {"error": f"buyagent 폴더가 없습니다: {base} (환경변수 BUYAGENT_DIR 또는 ../buyagent/buyagent 확인)"}
    cats = dict(buying.categories())
    if not cats:
        return 400, {"error": f"buyagent 카테고리 설정을 읽지 못했습니다: {base / 'config' / 'categories.toml'}"}
    if cat not in cats:
        return 400, {"error": "카테고리를 골라 주세요."}
    market = str(body.get("market") or "rakuten")
    if market not in BUY_MARKETS:
        return 400, {"error": "자동 검색 사이트는 라쿠텐·알리익스프레스·이베이 중에서 골라 주세요."}
    with LOCK:
        if any(j["kind"] == "buysearch" and j["status"] == "running" for j in JOBS.values()):
            return 409, {"error": "직접 검색이 이미 실행 중입니다. 끝난 뒤 다시 눌러 주세요."}
    # 한글이 남아 있으면 buyagent가 사이트 언어로 바꿔서 찾는다 (실행 로그에 "번역: …", search.json에 keyword_original)
    return 200, job_view(start_job("buysearch", (q, cat, market),
                                   f"구매대행 검색: {q} ({cats[cat]} · {buying.SITE_SHORT[market]})", anchor="buy"))


def buy_translate(conn, body):
    """검색창 언어 버튼: 한글 검색어 → 영어·일본어·중국어 (화면의 검색창에 넣어 사람이 고칠 수 있게)."""
    text = " ".join(str(body.get("text") or "").split())
    lang = str(body.get("lang") or "")
    if lang not in translate.LANGS:
        return 400, {"error": "언어를 골라 주세요."}
    if not text or len(text) > MAX_QUERY:
        return 400, {"error": f"검색어를 {MAX_QUERY}자 이내로 입력해 주세요."}
    try:
        return 200, {"text": translate.to_lang(text, lang), "translated": translate.has_korean(text)}
    except RuntimeError as err:
        return 502, {"error": str(err)}


def buy_watch(conn, body):
    """구매대행 감시 목록(buyagent catalog/products.csv): add = 마지막 직접 검색 조건 추가 · toggle = 켜기/끄기.
    쓰기는 buyagent web.py 의 add_watch·toggle_watch 그대로 (열 순서·id 규칙·중복 거절이 buyagent와 같음)."""
    action = str(body.get("action") or "")
    base = buying.buyagent_dir()
    if action not in ("add", "toggle", "pause", "schedule"):
        return 400, {"error": "action은 add·toggle·pause·schedule 중 하나"}
    if not (Path(base) / "web.py").is_file():
        return 400, {"error": f"buyagent web.py가 없습니다: {Path(base) / 'web.py'} (buyagent를 최신으로)"}
    if action in ("pause", "schedule"):              # 감시 전체 멈춤·매시간 자동 실행 (buyagent pause_request·schedule_request)
        key = "paused" if action == "pause" else "on"
        if not isinstance(body.get(key), bool):
            return 400, {"error": f"{key} 값(true/false)이 필요합니다."}
        buying.forget_status()
        try:
            return buying.watch_call(action, base, **{key: body[key]})
        finally:
            buying.forget_status()
    with LOCK:
        if any(j["kind"] == "buysearch" and j["status"] == "running" for j in JOBS.values()):
            return 409, {"error": "직접 검색이 실행 중입니다. 끝난 뒤 다시 눌러 주세요."}
    if action == "toggle":
        pid = str(body.get("id") or "").strip()
        if not pid or len(pid) > 100:
            return 400, {"error": "감시 상품 ID가 없습니다."}
        return buying.watch_call("toggle", base, id=pid)
    data = buying.load()
    if data.get("search_error") or not data.get("search"):
        return 400, {"error": "먼저 직접 검색을 해 주세요."}
    s = data["search"]
    if not s.get("ok"):
        return 400, {"error": "실패한 검색은 감시에 추가할 수 없습니다."}
    q = s.get("query") or {}
    must = body.get("must")                           # 상품명에 꼭 들어갈 단어 ('|' 구분, 검사는 buyagent parse_must)
    if must is not None and (not isinstance(must, str) or len(must) > 200):
        return 400, {"error": "꼭 들어갈 단어 형식이 이상합니다."}
    return buying.watch_call("add", base, search=s, name=q.get("keyword_original") or "", must=must)


# ---------------------------------------------------------------------------
# 5번 협상 (negotiation_agent) — 서버는 입력을 검증해 명령 인자로 바꾸고 실행만 한다. DB 기록은 5번이 한다.
# ---------------------------------------------------------------------------
NEG_LANGS = {"en", "zh"}
NEG_TONES = {"soft": "부드럽게", "normal": "보통", "firm": "강하게"}
NEG_MARKS = {"approved": "검토 완료", "sent": "발송 완료", "discarded": "버림", "draft": "초안으로 되돌림"}
INCOTERMS = {"", "EXW", "FOB", "CFR", "CIF", "DAP", "DDP"}
MAX_REPLY = 20_000
NEG_SUPPLIER = {"select": "선정", "reject": "제외", "restore": "선정·제외 되돌리기"}


class BadRequest(ValueError):
    pass


def _id(body, key):
    try:
        v = int(body.get(key))
    except (TypeError, ValueError):
        raise BadRequest(f"{key}가 필요합니다.")
    if v <= 0:
        raise BadRequest(f"{key}가 올바르지 않습니다.")
    return v


def _text(body, key, limit=500):
    v = " ".join(str(body.get(key) or "").split())
    if len(v) > limit:
        raise BadRequest(f"{key}는 {limit}자 이내로 입력해 주세요.")
    return v


def _row(conn, sql, value, what):
    row = conn.execute(sql, (value,)).fetchone()
    if row is None:
        raise BadRequest(f"{what} #{value}가 없습니다.")
    return row


PS_NAME_SQL = """SELECT ps.id, s.company FROM product_suppliers ps JOIN suppliers s ON s.id = ps.supplier_id
                 WHERE ps.id = ?"""
MSG_NAME_SQL = """SELECT m.id, m.direction, s.company FROM messages m JOIN product_suppliers ps ON ps.id = m.product_supplier_id
                  JOIN suppliers s ON s.id = ps.supplier_id WHERE m.id = ?"""


def neg_args(conn, body):
    """대시보드 요청 → (negotiation_agent 인자 목록, 작업 이름). 값은 '--옵션=값' 형태로 넘겨 옵션으로 오인되지 않게 한다."""
    action = body.get("action")
    lang = body.get("lang") or "en"
    if lang not in NEG_LANGS:
        raise BadRequest("언어는 en 또는 zh")
    if action == "rfq":
        ps = _row(conn, PS_NAME_SQL, _id(body, "ps_id"), "공급처 연결")
        inco = str(body.get("incoterm") or "").upper()
        if inco not in INCOTERMS:
            raise BadRequest("인코텀즈 값이 올바르지 않습니다.")
        args = ["rfq", str(ps["id"]), f"--lang={lang}"]
        args += [f"--qty={v}" for v in [_text(body, "qty", 60)] if v]
        args += [f"--incoterm={inco}"] if inco else []
        args += [f"--note={v}" for v in [_text(body, "note")] if v]
        args += ["--force"] if body.get("force") else []
        return args, f"5번 견적 요청 초안: {ps['company']}"
    if action == "sample":
        ps = _row(conn, PS_NAME_SQL, _id(body, "ps_id"), "공급처 연결")
        args = ["sample", str(ps["id"])] + ([f"--lang={lang}"] if body.get("lang") else [])
        args += [f"--qty={v}" for v in [_text(body, "qty", 60)] if v]
        args += [f"--note={v}" for v in [_text(body, "note")] if v]
        return args, f"5번 샘플 요청 초안: {ps['company']}"
    if action == "supplier":
        ps = _row(conn, PS_NAME_SQL, _id(body, "ps_id"), "공급처 연결")
        what = body.get("what")
        if what not in NEG_SUPPLIER:
            raise BadRequest("공급처 처리 값이 올바르지 않습니다.")
        args = ["supplier", str(ps["id"]), what]
        args += [f"--reason={v}" for v in [_text(body, "reason", 300)] if v and what == "reject"]
        return args, f"5번 공급처 {NEG_SUPPLIER[what]}: {ps['company']}"
    if action == "reply":
        ps = _row(conn, PS_NAME_SQL, _id(body, "ps_id"), "공급처 연결")
        text = str(body.get("text") or "").strip()
        if not text:
            raise BadRequest("답장 내용을 붙여넣어 주세요.")
        if len(text) > MAX_REPLY:
            raise BadRequest(f"답장은 {MAX_REPLY:,}자 이내로 붙여넣어 주세요.")
        inbox = db.default_path().parent / "inbox"          # 긴 글은 명령줄 대신 파일로 넘기고 5번이 저장 후 지움
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{datetime.now():%Y%m%d_%H%M%S_%f}_ps{ps['id']}.txt"
        path.write_text(text, encoding="utf-8")
        return ["reply", str(ps["id"]), f"--file={path}", "--delete-file"], f"5번 답장 저장·분석: {ps['company']}"
    if action == "counter":
        q = _row(conn, "SELECT q.id, s.company FROM quotes q JOIN product_suppliers ps ON ps.id = q.product_supplier_id "
                       "JOIN suppliers s ON s.id = ps.supplier_id WHERE q.id = ?", _id(body, "quote_id"), "견적")
        tone = body.get("tone") or "normal"
        if tone not in NEG_TONES:
            raise BadRequest("톤 값이 올바르지 않습니다.")
        args = ["counter", str(q["id"]), f"--tone={tone}", f"--lang={lang}"]
        price = str(body.get("price") or "").strip()
        if price:
            try:
                p = float(price)
            except ValueError:
                raise BadRequest("역제안 단가는 숫자로 입력해 주세요.")
            if not 0 < p < 1_000_000:
                raise BadRequest("역제안 단가가 올바르지 않습니다.")
            args.append(f"--price={p:g}")
        args += [f"--note={v}" for v in [_text(body, "note")] if v]
        args += [f"--other={v}" for v in [_text(body, "other", 200)] if v]
        return args, f"5번 역제안 초안({NEG_TONES[tone]}): {q['company']}"
    if action == "gmail_sync":
        return ["gmail-sync"], "5번 Gmail 확인 (발송 확인·새 답장)"
    if action in ("mark", "followup", "analyze", "gmail_draft"):
        m = _row(conn, MSG_NAME_SQL, _id(body, "message_id"), "메일")
        if action == "gmail_draft":
            if m["direction"] != "outbound":
                raise BadRequest("보낼 메일 초안만 Gmail에 넣을 수 있습니다.")
            to = _text(body, "to", 400)        # 형식 검사는 5번(gmail.parse_recipients)이 함
            return (["gmail-draft", str(m["id"])] + ([f"--to={to}"] if to else []),
                    f"5번 Gmail 임시보관함에 넣기 #{m['id']}: {m['company']}")
        if action == "mark":
            status = body.get("status")
            if status not in NEG_MARKS or m["direction"] != "outbound":
                raise BadRequest("바꿀 수 없는 상태입니다.")
            return ["mark", str(m["id"]), status], f"5번 메일 #{m['id']} {NEG_MARKS[status]}: {m['company']}"
        if action == "followup":
            return ["followup", str(m["id"])], f"5번 후속 메일 초안: {m['company']}"
        return ["analyze", str(m["id"])], f"5번 답장 다시 분석: {m['company']}"
    raise BadRequest("알 수 없는 작업입니다.")


def negotiation(conn, body):
    try:
        args, label = neg_args(conn, body)
    except BadRequest as err:
        return 400, {"error": str(err)}
    with LOCK:     # 같은 대상에 같은 작업이 이미 돌고 있으면 거절 (다른 공급처 작업은 동시에 가능)
        if any(j["label"] == label and j["status"] == "running" for j in JOBS.values()):
            return 409, {"error": "같은 작업이 이미 실행 중입니다. 끝난 뒤 다시 눌러 주세요."}
    return 200, job_view(start_job("neg", args, label))


def refresh_reference_images(conn):
    """참고 이미지는 1시간마다 또는 사진 없는 품목이 바뀌었을 때만 다시 검색 (네이버 호출 절약)."""
    need = {r["id"] for r in conn.execute("SELECT id, image_url FROM products WHERE stage != 'dropped'")
            if not db.resolve_image(r["image_url"])}
    with LOCK:
        if need == _REF_STATE["need"] and time.time() - _REF_STATE["at"] < REF_TTL:
            return
        _REF_STATE.update(need=need, at=time.time())
    dashboard.load_reference_images(conn)


class Handler(BaseHTTPRequestHandler):
    server_version = "trade_hub"

    def log_message(self, fmt, *args):      # 콘솔 조용히 (작업 로그는 data/logs)
        pass

    # --- 응답 도우미 ---
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if ctype.startswith("application/json") else body.encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    # --- GET ---
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "허용되지 않은 Host"})
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        conn = db.connect()
        try:
            if url.path == "/":
                with RENDER_LOCK:
                    if getattr(self.server, "refs", True):
                        refresh_reference_images(conn)
                    page = dashboard.render(conn, server=True)
                return self._send(200, page, "text/html; charset=utf-8")
            if url.path == "/api/search":
                return self._send(200, search(conn, (qs.get("q") or [""])[0]))
            if url.path == "/api/jobs":
                with LOCK:
                    jobs = [job_view(j) for j in sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:8]]
                return self._send(200, jobs)
            if url.path == "/file":
                return self._file((qs.get("p") or [""])[0])
            return self._send(404, {"error": "없는 주소"})
        finally:
            conn.close()

    def _file(self, raw):
        try:
            path = Path(raw).resolve(strict=True)
        except (OSError, RuntimeError):
            return self._send(404, {"error": "파일 없음"})
        if not path.is_file() or not any(path.is_relative_to(r) for r in allowed_roots()):
            return self._send(403, {"error": "보여 줄 수 없는 파일"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in (".md", ".csv", ".json", ".txt", ".log"):
            ctype = "text/plain"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        return self._send(200, path.read_bytes(), ctype)

    # --- POST (실행) ---
    def do_POST(self):
        # 거절하더라도 본문은 먼저 읽는다 — 안 읽고 응답하면 Windows에서 연결이 끊겨(ConnectionAbortedError) 응답을 못 받는다
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 200_000)   # 답장 붙여넣기 (한글·중국어 UTF-8 포함)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not self._host_ok():
            return self._send(403, {"error": "허용되지 않은 Host"})
        if self.headers.get("X-Trade-Hub") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(403, {"error": "대시보드에서만 실행할 수 있습니다"})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "요청 형식 오류"})
        route = {"/api/research": research, "/api/discover": discover, "/api/select": select, "/api/compliance": compliance, "/api/stop": stop,
                 "/api/neg": negotiation, "/api/buysearch": buy_search,
                 "/api/translate": buy_translate, "/api/buywatch": buy_watch}.get(urlparse(self.path).path)
        if not route:
            return self._send(404, {"error": "없는 주소"})
        conn = db.connect()
        try:
            code, out = route(conn, body)
        except RuntimeError as err:
            code, out = 500, {"error": str(err)}
        finally:
            conn.close()
        return self._send(code, out)


def make_server(port=DEFAULT_PORT, refs=True):
    srv = ThreadingHTTPServer((HOST, port), Handler)
    srv.daemon_threads = True
    srv.refs = refs
    return srv


def serve(port=DEFAULT_PORT, open_browser=True, refs=True):
    srv = make_server(port, refs)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"▶ 대시보드 서버: {url}  (이 PC에서만 접속 가능 · 끄려면 Ctrl+C)")
    print(f"  DB: {db.default_path()}")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        running = [j for j in JOBS.values() if j["status"] == "running"]
        if running:
            print(f"  · 실행 중이던 작업 {len(running)}개는 계속 돌고, 결과는 공용 DB에 기록됩니다 (로그: data/logs).")
        print("서버를 껐습니다.")
