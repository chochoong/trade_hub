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


def test_job_command_builds_type_flag(monkeypatch, tmp_path):
    real = ORIGINAL_JOB_COMMAND                                                 # 픽스처가 바꿔 끼우기 전 원래 함수
    monkeypatch.setattr(server, "agent_spec", lambda agent: (tmp_path, "py", 60))
    assert real("sourcing", (7, "retail"))[0] == ["py", "-m", "sourcing_agent", "from-db", "7", "--type", "retail"]
    assert real("sourcing", 7)[0][-2:] == ["--type", "small_batch"]
    with pytest.raises(ValueError):
        real("sourcing", (7, "oem"))                                             # 대시보드에서는 retail·small_batch만
    assert real("sourcing", (7, "retail", "Japan"))[0][-4:] == ["--type", "retail", "--country", "Japan"]
    assert "--country" not in real("sourcing", (7, "retail", "China"))[0]       # 중국은 2번 기본값
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


    assert ORIGINAL_JOB_COMMAND("buysearch", ("x", "watch", "ebay"))[0][-1] == "--market=ebay"
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
    assert code == 400 and "라쿠텐·알리익스프레스·이베이" in out["error"]   # 타오바오·1688은 자동 검색 없음 (화면은 링크)
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
