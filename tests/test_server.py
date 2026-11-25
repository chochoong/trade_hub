import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from trade_hub import db, naver, server

ORIGINAL_JOB_COMMAND = server.job_command   # srv 픽스처가 가짜로 바꾸기 전 원래 함수 (명령 조립 시험용)


@pytest.fixture
def srv(conn, monkeypatch, tmp_path):
    """테스트용 서버 (임시 DB, 네이버 호출 없음, 에이전트 대신 가짜 명령)."""
    monkeypatch.setattr(naver, "keys", lambda: None)
    monkeypatch.setattr(server, "JOBS", {})

    def fake_command(kind, arg):
        if kind == "research":   # 1번 흉내: 품목을 만들고 RESULT_PRODUCT_ID 출력
            code = ("from trade_hub import db; c = db.connect(); "
                    f"pid = db.upsert_product(c, {arg!r}, origin='manual'); c.commit(); print('조사 끝'); "
                    "print(f'RESULT_PRODUCT_ID={pid}')")
        else:                    # 2번 흉내: 실패 없이 끝
            code = "print('공급처 조사 끝')"
        return [sys.executable, "-c", code], tmp_path, 1

    monkeypatch.setattr(server, "job_command", fake_command)
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, sys.executable, 1))
    s = server.make_server(0, refs=False)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()
    s.server_close()


def call(url, body=None, headers=None):
    h = {"Content-Type": "application/json", "X-Trade-Hub": "1"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_job(base, job_id):
    for _ in range(100):
        jobs = {j["id"]: j for j in call(base + "/api/jobs")[1]}
        if jobs[job_id]["status"] != "running":
            return jobs[job_id]
        time.sleep(0.1)
    raise AssertionError("작업이 끝나지 않음")


def test_page_and_search(srv, conn):
    with conn:
        db.upsert_product(conn, "무선 기계식 키보드", keywords_ko_json=db.dumps(["무선키보드"]))
    code, html = call(srv + "/")
    assert code == 200 and "SERVER_MODE = true" in html and 'id="q"' in html
    code, hits = call(srv + "/api/search?q=" + urllib.request.quote("기계식 무선"))
    assert code == 200 and [h["name"] for h in hits] == ["무선 기계식 키보드"]


def test_post_needs_dashboard_header_and_host(srv):
    assert call(srv + "/api/research", {"query": "x"}, {"X-Trade-Hub": "0"})[0] == 403    # 다른 사이트에서 온 요청
    req = urllib.request.Request(srv + "/", headers={"Host": "evil.example:80"})          # DNS 리바인딩
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 403
    assert call(srv + "/api/research", {"query": "  "})[0] == 400
    assert call(srv + "/api/research", {"query": "가" * 101})[0] == 400


def test_research_runs_agent_and_reports_product(srv, conn):
    code, job = call(srv + "/api/research", {"query": "캠핑 랜턴"})
    assert code == 200 and job["status"] == "running"
    done = wait_job(srv, job["id"])
    assert done["status"] == "done" and done["product_id"]
    assert conn.execute("SELECT name FROM products WHERE id = ?", (done["product_id"],)).fetchone()[0] == "캠핑 랜턴"


def test_discover_runs_without_product_name(srv):
    code, job = call(srv + "/api/discover", {})                          # 품목명 없이 자동 발굴
    assert code == 200 and job["label"] == "1번 자동 발굴" and job["anchor"] == "a1"
    assert wait_job(srv, job["id"])["status"] == "done"
    code, job = call(srv + "/api/discover", {"hint": " 주방  수납 "})
    assert code == 200 and job["label"] == "1번 자동 발굴 (주방 수납)"
    wait_job(srv, job["id"])
    assert call(srv + "/api/discover", {"hint": "가" * 101})[0] == 400


def test_discover_command(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, "py", 30))
    cmd, _, _ = ORIGINAL_JOB_COMMAND("discover", "")
    assert cmd == ["py", "supplier_agent.py", "--count", "10", "--no-open"]
    cmd, _, _ = ORIGINAL_JOB_COMMAND("discover", "겨울 시즌")
    assert cmd[-2:] == ["--hint", "겨울 시즌"]


def test_select_moves_stage_then_runs_sourcing(srv, conn):
    with conn:
        pid = db.upsert_product(conn, "키캡")
        done_pid = db.upsert_product(conn, "발주한 품목", stage="ordered")
    code, job = call(srv + "/api/select", {"product_id": pid})
    assert code == 200 and job["product_id"] == pid
    assert conn.execute("SELECT stage FROM products WHERE id = ?", (pid,)).fetchone()[0] == "sourcing"
    assert wait_job(srv, job["id"])["status"] == "done"
    assert call(srv + "/api/select", {"product_id": done_pid})[0] == 400       # 이미 뒤 단계
    assert call(srv + "/api/select", {"product_id": 9999})[0] == 404


def test_busy_agent_is_not_started_twice(srv, conn):
    with conn:   # 슈퍼바이저 등 다른 곳에서 2번이 실행 중
        conn.execute("INSERT INTO agent_runs (agent, status) VALUES ('sourcing', 'running')")
        pid = db.upsert_product(conn, "키캡")
    code, out = call(srv + "/api/select", {"product_id": pid})
    assert code == 409 and "실행 중" in out["error"]
    assert conn.execute("SELECT stage FROM products WHERE id = ?", (pid,)).fetchone()[0] == "candidate"


def test_file_route_only_serves_allowed_folders(srv, tmp_path):
    img = db.images_dir()
    img.mkdir(parents=True, exist_ok=True)
    (img / "1.png").write_bytes(b"png")
    ok = call(srv + "/file?p=" + urllib.request.quote(str(img / "1.png")))
    assert ok[0] == 200
    assert call(srv + "/file?p=" + urllib.request.quote(str(db.default_path())))[0] == 403          # DB 파일
    assert call(srv + "/file?p=" + urllib.request.quote(str(img / ".." / "trade.db")))[0] == 403   # 경로 우회


def test_select_passes_purchase_mode(srv, conn, monkeypatch):
    seen = []
    real = server.job_command
    monkeypatch.setattr(server, "job_command", lambda kind, arg: (seen.append((kind, arg)), real("research", "x"))[1])
    with conn:
        a = db.upsert_product(conn, "AI안경")
        b = db.upsert_product(conn, "키캡")
    code, job = call(srv + "/api/select", {"product_id": a, "mode": "retail"})
    assert code == 200 and "구매대행용" in job["label"]
    wait_job(srv, job["id"])
    code, job = call(srv + "/api/select", {"product_id": b})                   # 방식이 없으면 소량 사입 (기존 동작)
    assert code == 200 and "소량 사입용" in job["label"]
    wait_job(srv, job["id"])
    code, job = call(srv + "/api/select", {"product_id": a, "mode": "retail", "country": "Japan"})
    assert code == 200 and "(일본)" in job["label"]
    wait_job(srv, job["id"])
    assert seen == [("sourcing", (a, "retail", "China")), ("sourcing", (b, "small_batch", "China")),
                    ("sourcing", (a, "retail", "Japan"))]
    assert call(srv + "/api/select", {"product_id": a, "mode": "oem; rm -rf"})[0] == 400   # 정해진 값만
    assert call(srv + "/api/select", {"product_id": a, "country": "Korea"})[0] == 400
    with conn:                                                                  # 국가를 안 보내면 1번 추천 국가
        c = db.upsert_product(conn, "빈티지 필름 카메라", source_country="USA")
    seen.clear()
    code, job = call(srv + "/api/select", {"product_id": c})
    assert code == 200 and "(미국)" in job["label"] and seen == [("sourcing", (c, "small_batch", "USA"))]
    wait_job(srv, job["id"])


def test_job_command_builds_type_flag(monkeypatch, tmp_path):
    real = ORIGINAL_JOB_COMMAND                                                 # 픽스처가 바꿔 끼우기 전 원래 함수
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, "py", 60))
    assert real("sourcing", (7, "retail"))[0] == ["py", "-m", "sourcing_agent", "from-db", "7", "--type", "retail"]
    assert real("sourcing", 7)[0][-2:] == ["--type", "small_batch"]
    assert real("sourcing", (7, "oem"))[0][-2:] == ["--type", "oem"]             # 대량 사입·OEM (2번 카드에서만)
    with pytest.raises(ValueError):
        real("sourcing", (7, "bulk"))
    assert real("sourcing", (7, "retail", "Japan"))[0][-4:] == ["--type", "retail", "--country", "Japan"]
    assert real("sourcing", (7, "retail", "China"))[0][-2:] == ["--country", "China"]   # 고른 나라는 항상 넘김
    assert "--country" not in real("sourcing", (7, "retail"))[0]                # 안 고르면 2번이 1번 추천 국가로
    assert real("sourcing", (7, "small_batch", "USA"))[0][-2:] == ["--country", "USA"]
    with pytest.raises(ValueError):
        real("sourcing", (7, "retail", "Korea"))


def test_buy_search_runs_buyagent(srv, monkeypatch, tmp_path):
    base = tmp_path / "ba"
    (base / "config").mkdir(parents=True)
    (base / "main.py").write_text("", encoding="utf-8")
    (base / "config" / "categories.toml").write_text(
        '[categories.watch]\nname = "시계"\nallowed = true\n[categories.food]\nname = "식품"\nallowed = false\n',
        encoding="utf-8")
    monkeypatch.setenv("BUYAGENT_DIR", str(base))
    assert call(srv + "/api/buysearch", {"query": " ", "category": "watch"})[0] == 400
    assert call(srv + "/api/buysearch", {"query": "x", "category": "food"})[0] == 400        # 등록 금지 카테고리
    assert call(srv + "/api/buysearch", {"query": "x", "category": "watch"}, {"X-Trade-Hub": "0"})[0] == 403
    code, job = call(srv + "/api/buysearch", {"query": "セイコー  SBTQ041", "category": "watch"})
    assert code == 200 and job["anchor"] == "buy" and "セイコー SBTQ041" in job["label"]
    assert wait_job(srv, job["id"])["status"] == "done"


def test_buy_search_command(monkeypatch, tmp_path):
    monkeypatch.setenv("BUYAGENT_DIR", str(tmp_path))
    cmd, cwd, _ = ORIGINAL_JOB_COMMAND("buysearch", ("--evil x", "watch"))
    assert cmd[1:] == ["main.py", "--search=--evil x", "--category=watch", "--market=rakuten"] and cwd == tmp_path   # 옵션으로 오인 안 됨
    assert ORIGINAL_JOB_COMMAND("buysearch", ("x", "watch", "aliexpress"))[0][-1] == "--market=aliexpress"
    assert ORIGINAL_JOB_COMMAND("buysearch", ("x", "watch", "ebay"))[0][-1] == "--market=ebay"
    with pytest.raises(ValueError):
        ORIGINAL_JOB_COMMAND("buysearch", ("x", "watch", "taobao"))



def test_buy_search_passes_korean_and_api_key(srv, monkeypatch, tmp_path):
    from trade_hub import server
    base = tmp_path / "ba"
    (base / "config").mkdir(parents=True)
    (base / "main.py").write_text("", encoding="utf-8")
    (base / "config" / "categories.toml").write_text('[categories.watch]\nname = "시계"\nallowed = true\n', encoding="utf-8")
    monkeypatch.setenv("BUYAGENT_DIR", str(base))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(server, "dotenv_value", lambda path, name: "sk-test" if name == "ANTHROPIC_API_KEY" else None)
    seen = {}
    real_popen = server.subprocess.Popen
    def spy(cmd, **kw):
        seen["env"] = kw["env"]
        return real_popen(cmd, **kw)
    monkeypatch.setattr(server.subprocess, "Popen", spy)
    code, job = call(srv + "/api/buysearch", {"query": "세이코 시계", "category": "watch"})
    assert code == 200 and "세이코 시계" in job["label"]                     # 한글 그대로 buyagent로
    assert seen["env"]["ANTHROPIC_API_KEY"] == "sk-test"
    wait_job(srv, job["id"])


def test_dotenv_value(tmp_path):
    from trade_hub.server import dotenv_value
    f = tmp_path / ".env"
    f.write_text('# 주석\nOTHER=1\nexport ANTHROPIC_API_KEY="abc def"  # 메모\nNAVER=x # y\n', encoding="utf-8")
    assert dotenv_value(f, "ANTHROPIC_API_KEY") == "abc def"
    assert dotenv_value(f, "NAVER") == "x" and dotenv_value(f, "NONE") is None
    assert dotenv_value(tmp_path / "없음", "X") is None


def test_buy_search_site_picks_market(srv, monkeypatch, tmp_path):
    from trade_hub import server
    base = tmp_path / "ba"
    (base / "config").mkdir(parents=True)
    (base / "main.py").write_text("", encoding="utf-8")
    (base / "config" / "categories.toml").write_text('[categories.watch]\nname = "시계"\nallowed = true\n', encoding="utf-8")
    monkeypatch.setenv("BUYAGENT_DIR", str(base))
    seen = []
    monkeypatch.setattr(server, "job_command", lambda kind, arg: (seen.append(arg), ([sys.executable, "-c", "pass"], tmp_path, 1))[1])
    code, job = call(srv + "/api/buysearch", {"query": "Seiko watch", "category": "watch", "market": "aliexpress"})
    assert code == 200 and seen[-1] == ("Seiko watch", "watch", "aliexpress") and "알리익스프레스" in job["label"]
    wait_job(srv, job["id"])
    code, job = call(srv + "/api/buysearch", {"query": "Seiko watch", "category": "watch", "market": "ebay"})
    assert code == 200 and seen[-1][2] == "ebay" and "이베이" in job["label"]
    wait_job(srv, job["id"])
    code, out = call(srv + "/api/buysearch", {"query": "精工", "category": "watch", "market": "china"})
    assert code == 400 and "라쿠텐·야후·알리익스프레스·이베이" in out["error"]   # 타오바오·1688은 자동 검색 없음 (화면은 링크)
    assert call(srv + "/api/buysearch", {"query": "x", "category": ""})[0] == 400              # 카테고리 미선택


def test_translate_api(srv, monkeypatch):
    from trade_hub import translate
    monkeypatch.setattr(translate, "_call_llm", lambda q, lang: {"zh": "精工 手表", "ja": "セイコー"}[lang])
    code, out = call(srv + "/api/translate", {"text": "세이코 시계", "lang": "zh"})
    assert code == 200 and out == {"text": "精工 手表", "translated": True}
    assert call(srv + "/api/translate", {"text": "SBTQ041", "lang": "ja"})[1] == {"text": "SBTQ041", "translated": False}
    assert call(srv + "/api/translate", {"text": "x", "lang": "fr"})[0] == 400
    monkeypatch.setattr(translate, "_call_llm", lambda q, lang: (_ for _ in ()).throw(ConnectionError()))
    code, out = call(srv + "/api/translate", {"text": "세이코 가방", "lang": "en"})
    assert code == 502 and "영어로 직접" in out["error"]


def test_buy_search_without_buyagent_folder(srv, monkeypatch, tmp_path):
    monkeypatch.setenv("BUYAGENT_DIR", str(tmp_path / "없음"))
    code, out = call(srv + "/api/buysearch", {"query": "x", "category": "watch"})
    assert code == 400 and "buyagent 폴더가 없습니다" in out["error"]
    (tmp_path / "없음").mkdir()
    (tmp_path / "없음" / "main.py").write_text("", encoding="utf-8")
    code, out = call(srv + "/api/buysearch", {"query": "x", "category": "watch"})
    assert code == 400 and "카테고리 설정" in out["error"]


def test_job_lines_arrive_while_running(srv, monkeypatch, tmp_path):
    # 에이전트가 print만 해도 진행 줄이 알림창에 바로 보여야 함 (PYTHONUNBUFFERED) — 끝날 때 한꺼번에 오면 내내 빈 칸
    from trade_hub import server
    script = "import time; print('검색 중…'); time.sleep(3)"
    monkeypatch.setattr(server, "job_command", lambda kind, arg: ([sys.executable, "-c", script], tmp_path, 1))
    job = server.start_job("research", "x", "테스트")
    deadline = time.time() + 2.5
    while time.time() < deadline and not job["lines"]:
        time.sleep(0.05)
    assert job["status"] == "running" and job["lines"] == ["검색 중…"]
    wait_job(srv, job["id"])


def test_buy_watch_api(srv, monkeypatch, tmp_path):
    from trade_hub import buying as b
    base = tmp_path / "ba"
    (base / "reports").mkdir(parents=True)
    (base / "catalog").mkdir()
    monkeypatch.setenv("BUYAGENT_DIR", str(base))
    assert call(srv + "/api/buywatch", {"action": "add"})[0] == 400                          # web.py 없음
    seen = []

    def fake_call(action, base_=None, **kw):
        if action == "add":
            seen.append(("add", kw["search"]["query"]["keyword"], kw["name"], kw["must"])); return 200, {"id": "x1"}
        seen.append(("toggle", kw["id"])); return 200, {"id": kw["id"], "active": "N"}
    (base / "web.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(b, "watch_call", fake_call)
    code, out = call(srv + "/api/buywatch", {"action": "add"})
    assert code == 400 and "먼저 직접 검색" in out["error"]
    (base / "reports" / "search.json").write_text(json.dumps({"ok": False, "error": "키 없음", "query": {"keyword": "a"}}),
                                                  encoding="utf-8")
    assert "실패한 검색" in call(srv + "/api/buywatch", {"action": "add"})[1]["error"]
    (base / "reports" / "search.json").write_text(json.dumps(
        {"ok": True, "query": {"keyword": "セイコー", "keyword_original": "세이코", "category": "watch"}}), encoding="utf-8")
    assert call(srv + "/api/buywatch", {"action": "add"}) == (200, {"id": "x1"})
    assert call(srv + "/api/buywatch", {"action": "add", "must": "SBTR|SBTQ041"})[0] == 200
    assert call(srv + "/api/buywatch", {"action": "add", "must": ["x"]})[0] == 400
    assert call(srv + "/api/buywatch", {"action": "toggle", "id": "x1"})[1]["active"] == "N"
    assert call(srv + "/api/buywatch", {"action": "toggle"})[0] == 400
    assert call(srv + "/api/buywatch", {"action": "rm"})[0] == 400
    assert seen == [("add", "セイコー", "세이코", None), ("add", "セイコー", "세이코", "SBTR|SBTQ041"), ("toggle", "x1")]


def test_buy_watch_pause_and_schedule(srv, monkeypatch, tmp_path):
    from trade_hub import buying as b
    base = tmp_path / "ba"
    (base / "catalog").mkdir(parents=True)
    (base / "web.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("BUYAGENT_DIR", str(base))
    seen = []

    def fake_call(action, base_=None, **kw):
        seen.append((action, kw))
        if action == "schedule":
            return 500, {"error": "작업 스케줄러 등록 실패: 액세스 거부"}
        return 200, {"paused": kw["paused"]}
    monkeypatch.setattr(b, "watch_call", fake_call)
    assert call(srv + "/api/buywatch", {"action": "pause", "paused": True}) == (200, {"paused": True})
    assert call(srv + "/api/buywatch", {"action": "pause", "paused": "yes"})[0] == 400
    code, out = call(srv + "/api/buywatch", {"action": "schedule", "on": True})
    assert code == 500 and out["error"] == "작업 스케줄러 등록 실패: 액세스 거부"               # buyagent 문구 그대로
    assert call(srv + "/api/buywatch", {"action": "schedule"})[0] == 400
    assert seen == [("pause", {"paused": True}), ("schedule", {"on": True})]


def test_select_oem_only_from_sourcing_stage(srv, conn, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "start_job", lambda kind, arg, label, **kw: (seen.append((arg, label)), {"id": "1", "label": label})[1])
    monkeypatch.setattr(server, "job_view", lambda j: j)
    with conn:
        cand = db.upsert_product(conn, "후보 품목")
        src = db.upsert_product(conn, "발굴 중 품목", stage="sourcing")
    code, out = call(srv + "/api/select", {"product_id": cand, "mode": "oem"})
    assert code == 400 and "공급처 발굴 단계" in out["error"]
    assert conn.execute("SELECT stage FROM products WHERE id = ?", (cand,)).fetchone()[0] == "candidate"   # 단계 그대로
    code, out = call(srv + "/api/select", {"product_id": src, "mode": "oem"})
    assert code == 200 and seen[-1][0] == (src, "oem", "China") and "대량 사입·OEM용 공급처 찾기" in seen[-1][1]


# --- 3번 통관·규제 [전체 점검]·[다시 점검] ------------------------------------------------
def test_compliance_job_command(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, "py", 30))
    cmd, cwd, timeout = ORIGINAL_JOB_COMMAND("compliance", (16, "api"))
    assert cmd == ["py", "-m", "compliance_agent", "check-hub", "16", "--official", "api"] and timeout == 30
    cmd, _, _ = ORIGINAL_JOB_COMMAND("compliance", (None, "files"))
    assert cmd == ["py", "-m", "compliance_agent", "check-hub", "--recheck", "--official", "files"]
    with pytest.raises(ValueError):
        ORIGINAL_JOB_COMMAND("compliance", (None, "maybe"))


def test_compliance_route_validates_and_runs(srv, conn, monkeypatch):
    seen = []
    real = server.job_command
    monkeypatch.setattr(server, "job_command", lambda kind, arg: (seen.append((kind, arg)), real(kind, arg))[1])
    with conn:
        pid = db.upsert_product(conn, "AI안경", stage="sourcing")
        cand = db.upsert_product(conn, "후보")
    code, job = call(srv + "/api/compliance", {"product_id": pid})
    assert code == 200 and f"#{pid} AI안경" in job["label"] and job["anchor"] == "a3"
    assert wait_job(srv, job["id"])["status"] == "done"
    code, job = call(srv + "/api/compliance", {"official": "files"})
    assert code == 200 and "진행 품목 전체" in job["label"]
    wait_job(srv, job["id"])
    assert seen == [("compliance", (pid, "api")), ("compliance", (None, "files"))]
    assert call(srv + "/api/compliance", {"product_id": cand})[0] == 400       # 후보 품목은 점검 안 함
    assert call(srv + "/api/compliance", {"product_id": 9999})[0] == 404
    assert call(srv + "/api/compliance", {"official": "maybe"})[0] == 400
    with conn:   # 슈퍼바이저 등 다른 곳에서 3번이 실행 중
        conn.execute("INSERT INTO agent_runs (agent, status) VALUES ('compliance', 'running')")
    assert call(srv + "/api/compliance", {})[0] == 409


def test_compliance_job_gets_keys_without_overriding_env(srv, conn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    monkeypatch.setattr(server, "dotenv_value", lambda path, name: "from-dotenv" if name == "ANTHROPIC_API_KEY" else None)
    monkeypatch.setattr(server, "windows_user_env", lambda name: "from-registry" if name == "DATA_GO_KR_KEY" else None)
    seen = {}
    real_popen = server.subprocess.Popen
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: (seen.update(env=kw["env"]), real_popen(cmd, **kw))[1])
    with conn:
        pid = db.upsert_product(conn, "키캡", stage="sourcing")
    code, job = call(srv + "/api/compliance", {"product_id": pid})
    assert code == 200
    assert seen["env"]["ANTHROPIC_API_KEY"] == "from-env"          # 서버 환경변수가 우선
    assert seen["env"]["DATA_GO_KR_KEY"] == "from-registry"        # 서버를 켠 뒤 넣은 키도 찾음
    wait_job(srv, job["id"])


def test_compliance_buttons_only_in_server_mode(conn, tmp_path):
    from trade_hub import dashboard
    with conn:
        db.upsert_product(conn, "AI안경", stage="sourcing")
    static = dashboard.build(tmp_path / "s.html", conn, refs=False).read_text(encoding="utf-8")
    assert 'data-act="compliance"' not in static
    dashboard.SERVER["on"] = True
    try:
        html = "".join(dashboard.agent_body(conn, "compliance"))
    finally:
        dashboard.SERVER["on"] = False
    assert html.count('data-act="compliance"') == 2 and 'id="compliance-official"' in html   # 전체 + 품목 1개


def test_sourcing_runs_in_parallel(srv, conn, monkeypatch, tmp_path):
    """품목·구매 방식이 다르면 2번을 같이 돌리고, 같은 품목·같은 방식과 상한 초과만 막는다."""
    monkeypatch.setattr(server, "job_command",
                        lambda kind, arg: ([sys.executable, "-c", "import time; time.sleep(8)"], tmp_path, 1))
    with conn:
        a, b, c, d = (db.upsert_product(conn, n) for n in ("AI안경", "키캡", "텀블러", "선반"))
    jobs = []
    try:
        code, job = call(srv + "/api/select", {"product_id": a, "mode": "retail"})
        assert code == 200
        jobs.append(job)
        code, out = call(srv + "/api/select", {"product_id": a, "mode": "retail"})
        assert code == 409 and "같은 구매 방식" in out["error"]                  # 같은 품목·같은 방식
        for pid, mode in ((a, "small_batch"), (b, "retail")):                  # 방식이 다르거나 품목이 다르면 같이
            code, job = call(srv + "/api/select", {"product_id": pid, "mode": mode})
            assert code == 200, job
            jobs.append(job)
        assert len({j["id"] for j in jobs}) == 3
        code, out = call(srv + "/api/select", {"product_id": c})
        assert code == 409 and "동시에 3개" in out["error"]                     # 상한
        assert conn.execute("SELECT stage FROM products WHERE id = ?", (c,)).fetchone()[0] == "candidate"
    finally:
        server.JOBS.clear()   # 가짜 작업(8초 대기)은 스스로 끝남
    with conn:   # 다른 곳(슈퍼바이저)에서 특정 품목만 조사 중이면 다른 품목은 돌림
        conn.execute("INSERT INTO agent_runs (agent, status, args_json) VALUES "
                     "('sourcing', 'running', ?)", (json.dumps({"ids": [d], "sourcing_type": "small_batch"}),))
    assert server.sourcing_busy_reason(conn, b, "small_batch") is None
    assert "같은 구매 방식" in server.sourcing_busy_reason(conn, d, "small_batch")
    assert server.sourcing_busy_reason(conn, d, "retail") is None


def test_stop_job(srv, conn, monkeypatch, tmp_path):
    """중지: 프로세스 종료 → '중지됨', 남은 agent_runs는 실패로, 후보에서 넘긴 품목은 후보로 되돌림."""
    monkeypatch.setattr(server, "job_command",
                        lambda kind, arg: ([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1))
    with conn:
        a = db.upsert_product(conn, "문풍지")
        b = db.upsert_product(conn, "키캡", stage="sourcing")
    code, job = call(srv + "/api/select", {"product_id": a, "mode": "small_batch"})
    assert code == 200
    with conn:   # 2번이 시작하며 남기는 실행 기록 흉내 (강제 종료라 running으로 남음)
        mine = conn.execute("INSERT INTO agent_runs (agent, status, args_json) VALUES ('sourcing', 'running', ?)",
                            (json.dumps({"ids": [a], "sourcing_type": "small_batch"}),)).lastrowid
        other = conn.execute("INSERT INTO agent_runs (agent, status, args_json) VALUES ('sourcing', 'running', ?)",
                             (json.dumps({"ids": [b], "sourcing_type": "retail"}),)).lastrowid
    code, out = call(srv + "/api/stop", {"id": job["id"]})
    assert code == 200 and out["status"] == "cancelled"
    assert wait_job(srv, job["id"])["status"] == "cancelled"
    status = dict(conn.execute("SELECT id, status FROM agent_runs").fetchall())
    assert status[mine] == "failed" and status[other] == "running"          # 같이 돌던 다른 품목 기록은 그대로
    assert conn.execute("SELECT stage FROM products WHERE id = ?", (a,)).fetchone()[0] == "candidate"
    assert call(srv + "/api/stop", {"id": job["id"]})[0] == 400              # 이미 끝남
    assert call(srv + "/api/stop", {"id": "없음"})[0] == 404
    code, job = call(srv + "/api/select", {"product_id": b, "mode": "small_batch"})  # 이미 공급처 발굴 단계면 그대로
    assert code == 200
    assert call(srv + "/api/stop", {"id": job["id"]})[0] == 200
    assert conn.execute("SELECT stage FROM products WHERE id = ?", (b,)).fetchone()[0] == "sourcing"


def test_job_panel_has_stop_button():
    from trade_hub import dashboard
    assert 'data-stop="' in dashboard.SERVER_JS and '"/api/stop"' in dashboard.SERVER_JS


# --- 4번 원가 → 5번 [넘기기] ---------------------------------------------------------
def test_costneg_job_command(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, "py", 15))
    cmd, cwd, timeout = ORIGINAL_JOB_COMMAND("cost_neg", 37)
    assert cmd == ["py", "-m", "cost_agent", "to-negotiation", "37"] and cwd == tmp_path and timeout == 2


def test_costneg_route_validates_and_runs(srv, conn, monkeypatch):
    seen = []
    real = server.job_command
    monkeypatch.setattr(server, "job_command", lambda kind, arg: (seen.append((kind, arg)), real(kind, arg))[1])
    with conn:
        pid = db.upsert_product(conn, "EVA 케이스", stage="negotiating")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=3.0)
    code, job = call(srv + "/api/costneg", {"ps_id": ps})
    assert code == 200 and f"#{pid} EVA 케이스 × Allwin" in job["label"] and job["anchor"] == f"ps-{ps}"
    wait_job(srv, job["id"])
    assert seen == [("cost_neg", ps)]
    assert call(srv + "/api/costneg", {"ps_id": "x"})[0] == 400
    assert call(srv + "/api/costneg", {"ps_id": 9999})[0] == 404


# --- 중복 실행 방지 -----------------------------------------------------------------------
def test_second_server_on_same_port_is_refused(capsys, monkeypatch):
    first = server.make_server(0, refs=False)
    port = first.server_address[1]
    try:
        with pytest.raises(OSError):
            server.make_server(port, refs=False)
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        assert server.serve(port, open_browser=True, refs=False) == 1
        assert "이미 켜져 있습니다" in capsys.readouterr().out
    finally:
        first.server_close()


def test_buy_watch_naver_low(srv, monkeypatch, tmp_path):
    from trade_hub import buying as b
    (tmp_path / "web.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("BUYAGENT_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(b, "watch_call", lambda action, base_=None, **kw: (seen.append((action, kw)), (200, {"id": kw["id"]}))[1])
    assert call(srv + "/api/buywatch", {"action": "naver_low", "id": "w1", "price": "165,000", "memo": " 스토어A "}) == (200, {"id": "w1"})
    assert call(srv + "/api/buywatch", {"action": "naver_low", "id": "w1", "price": ""})[0] == 200          # 빈 값 = 지우기
    assert call(srv + "/api/buywatch", {"action": "naver_low", "price": "1"})[0] == 400                     # ID 없음
    assert call(srv + "/api/buywatch", {"action": "naver_low", "id": "w1", "price": "1" * 21})[0] == 400
    assert seen == [("naver_low", {"id": "w1", "price": "165,000", "memo": "스토어A"}),
                    ("naver_low", {"id": "w1", "price": "", "memo": ""})]


# --- 3번 화면 [4번 원가 계산] -------------------------------------------------------
def test_costing_job_command(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, "py", 15))
    assert ORIGINAL_JOB_COMMAND("costing", 36) == (["py", "-m", "cost_agent", "from-hub", "36"], tmp_path, 15)
    assert server.KIND_AGENT["costing"] == "costing"


def test_costing_route_validates_and_runs(srv, conn, monkeypatch):
    seen = []
    real = server.job_command
    monkeypatch.setattr(server, "job_command", lambda kind, arg: (seen.append((kind, arg)), real(kind, arg))[1])
    with conn:
        pid = db.upsert_product(conn, "EVA 케이스", stage="sourcing")
        bare = db.upsert_product(conn, "가격 없음", stage="sourcing")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        db.link_supplier(conn, pid, sid, unit_price_usd=3.0)
        db.link_supplier(conn, bare, sid)
    code, job = call(srv + "/api/costing", {"product_id": pid})
    assert code == 200 and f"#{pid} EVA 케이스" in job["label"] and job["anchor"] == f"cost-{pid}"
    wait_job(srv, job["id"])
    assert seen == [("costing", pid)]
    assert call(srv + "/api/costing", {"product_id": bare})[0] == 400
    assert call(srv + "/api/costing", {"product_id": "x"})[0] == 400
    assert call(srv + "/api/costing", {"product_id": 9999})[0] == 404


# --- "새 결과 있음" 띠 ------------------------------------------------------------------
def test_changed_endpoint_moves_when_db_changes_outside_screen(srv, conn):
    code, a = call(srv + "/api/changed")
    assert code == 200 and set(a) == {"runs", "data", "buy"}
    with db.agent_run(conn, "costing"):            # 명령줄·슈퍼바이저 실행 (화면 작업 아님)
        pass
    b = call(srv + "/api/changed")[1]
    assert b["runs"].get("costing") and b["runs"] != a["runs"]
    with db.agent_run(conn, "supervisor"):
        pass
    assert call(srv + "/api/changed")[1]["runs"] == b["runs"]          # 슈퍼바이저 tick만으로는 안 바뀜
    with conn:
        db.upsert_product(conn, "사람이 추가", stage="candidate")
    c = call(srv + "/api/changed")[1]
    assert c["data"] != b["data"] and c["runs"] == b["runs"]
    page = urllib.request.urlopen(srv + "/").read().decode("utf-8")
    assert 'id="hub-sig"' in page and 'id="changed"' in page and '"costing": "4번 원가"' in page


def test_changed_bar_js_rules(conn):
    from trade_hub import dashboard
    js = dashboard.SERVER_JS
    assert "setInterval(checkChanged, 30000)" in js and "location.reload()" in js
    check = js[js.index("function checkChanged"):js.index("setInterval(checkChanged")]
    assert "document.hidden" in check and "Object.keys(mine).length" in check and '"running"' in check
    assert "visibilitychange" in js
    assert 'id="hub-sig"' not in dashboard.render(conn)                          # 파일 대시보드엔 없음


# --- 6번 [PO 초안 만들기] ---------------------------------------------------------------
def test_documents_job_command(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, "py", 15))
    assert ORIGINAL_JOB_COMMAND("documents", 7) == (["py", "scripts/tradedocs.py", "from-hub", "--product", "7"], tmp_path, 15)
    assert ORIGINAL_JOB_COMMAND("documents", None)[0] == ["py", "scripts/tradedocs.py", "from-hub"]
    assert server.KIND_AGENT["documents"] == "documents"


def test_documents_route_validates_and_runs(srv, conn, monkeypatch):
    seen = []
    real = server.job_command
    monkeypatch.setattr(server, "job_command", lambda kind, arg: (seen.append((kind, arg)), real(kind, arg))[1])
    with conn:
        pid = db.upsert_product(conn, "실리콘 찜기", stage="negotiating")
        bare = db.upsert_product(conn, "견적 없음", stage="negotiating")
        sid = db.upsert_supplier(conn, "Allwin", "China")
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=2.0)
        ps2 = db.link_supplier(conn, bare, sid, unit_price_usd=2.0)
        conn.execute("UPDATE product_suppliers SET status = 'selected' WHERE id IN (?, ?)", (ps, ps2))
        conn.execute("INSERT INTO quotes (product_supplier_id, unit_price_usd, min_qty) VALUES (?, 2.1, 500)", (ps,))
    code, job = call(srv + "/api/documents", {"product_id": pid})
    assert code == 200 and f"#{pid} 실리콘 찜기" in job["label"] and job["anchor"] == "a6"
    wait_job(srv, job["id"])
    code, job = call(srv + "/api/documents", {})
    assert code == 200 and "조건에 맞는 품목 전부" in job["label"]
    wait_job(srv, job["id"])
    assert seen == [("documents", pid), ("documents", None)]
    assert call(srv + "/api/documents", {"product_id": bare})[0] == 400
    assert call(srv + "/api/documents", {"product_id": "x"})[0] == 400
    assert call(srv + "/api/documents", {"product_id": 9999})[0] == 404


def test_supplier_page_shows_full_red_flags(srv, conn):
    with conn:
        pid = db.upsert_product(conn, "선반", stage="sourcing")
        sid = db.upsert_supplier(conn, "Shelf Co", "China", red_flags="결제 조건 불명확, " + "가" * 120 + ", 인증 없음",
                                 trust_reason="이유 " + "나" * 100)
        ps = db.link_supplier(conn, pid, sid, unit_price_usd=3, unverified_json=["샘플 가능 여부"],
                              evidence_json=[{"claim": "경쟁 대비 요약 (우위 1 · 열위 0)", "url": "https://r.example.com", "type": "competitive"}])
    st, html = call(f"{srv}/supplier?id={ps}")
    assert st == 200 and "위험 요소 (3)" in html and "<li>인증 없음</li>" in html and "가" * 120 in html
    assert "나" * 100 in html and "샘플 가능 여부" in html and "경쟁 비교" in html
    assert f'href="/supplier?id={ps}"' in call(srv + "/")[1]
    assert call(f"{srv}/supplier?id=99999")[0] == 404 and call(f"{srv}/supplier?id=x")[0] == 404


def test_sourcing_options_validated_and_passed_as_args(srv, conn):
    real = ORIGINAL_JOB_COMMAND
    cmd, _, _ = real("sourcing", (21, "oem", None))
    assert cmd[-4:] == ["from-db", "21", "--type", "oem"]                     # 옵션 없으면 지금과 같은 명령
    opts = server.sourcing_opts({"cheaper": True, "new_only": True, "max_price": "30,000", "note": '무브랜드; rm -rf "x"\nOEM'})
    cmd, _, _ = real("sourcing", (21, "oem", "China", opts))
    assert cmd[cmd.index("--country"):] == ["--country", "China", "--cheaper", "--max-price", "30000", "--new-only",
                                            "--note", '무브랜드; rm -rf "x" OEM']   # 리스트 인자 · 한 줄
    for bad in ({"max_price": "-1"}, {"max_price": "abc"}, {"max_price": "0"}, {"max_price": "3.5"}, {"max_price": "nan"}, {"max_price": "inf"}, {"note": "가" * 501}, {"note": 5}):
        with pytest.raises(ValueError):
            server.sourcing_opts(bad)
    assert server.sourcing_opt_args(server.sourcing_opts({"cheaper": "yes"})) == []   # true 만 켬
    with conn:
        pid = db.upsert_product(conn, "옵션 품목", stage="sourcing")
    assert call(srv + "/api/select", {"product_id": pid, "options": {"max_price": "x"}})[0] == 400
    code, job = call(srv + "/api/select", {"product_id": pid, "options": {"cheaper": True, "max_price": 30000}})
    assert code == 200 and "더 싼 곳만 · 단가 ≤ 30,000원" in job["label"]
