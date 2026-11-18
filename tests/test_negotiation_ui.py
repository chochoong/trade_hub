"""3번 협상 연결: 대시보드 화면 + 서버 /api/neg (에이전트 대신 가짜 명령 — negotiation_agent·API 호출 없음)."""
import json
import sys

from tests.test_server import call, srv, wait_job   # noqa: F401  (srv 픽스처 재사용)
from trade_hub import dashboard, db, server


def seed(conn):
    with conn:
        pid = db.upsert_product(conn, "요가매트", stage="negotiating", search_keyword_en="yoga mat")
        s1 = db.upsert_supplier(conn, "ABC Co", "China", contact_email="a@abc.cn", trust="상")
        s2 = db.upsert_supplier(conn, "Other Co", "China")
        ps = db.link_supplier(conn, pid, s1, is_top_pick=1, max_order_qty=50)
        other = db.link_supplier(conn, pid, s2)
        sent = conn.execute("INSERT INTO messages (product_supplier_id, direction, kind, subject, body, status, sent_at,"
                            " translation) VALUES (?, 'outbound', 'rfq', 'Inquiry <yoga>', 'Dear', 'sent',"
                            " '2026-11-01T10:00:00', '번역')", (ps,)).lastrowid
        reply = conn.execute("INSERT INTO messages (product_supplier_id, direction, body, status, sent_at, reply_to_id,"
                             " analysis_json) VALUES (?, 'inbound', 'Price $3.2', 'received', '2026-11-02T10:00:00', ?, ?)",
                             (ps, sent, db.dumps({"summary_ko": "단가 3.2달러", "open_questions_ko": ["로고 비용"]}))).lastrowid
        draft = conn.execute("INSERT INTO messages (product_supplier_id, direction, kind, subject, body, status, analysis_json)"
                             " VALUES (?, 'outbound', 'negotiation', 'Re: price', 'Could you do 2.8?', 'draft', ?)",
                             (ps, db.dumps({"strategy_ko": "목표가 제시", "tone": "firm", "counter_price_usd": 2.8}))).lastrowid
        q = conn.execute("INSERT INTO quotes (product_supplier_id, message_id, unit_price_usd, min_qty, price_tiers_json,"
                         " incoterm) VALUES (?, ?, 3.2, 300, ?, 'FOB')", (ps, reply, db.dumps([{"qty": 1000, "usd": 2.9}]))).lastrowid
    return {"pid": pid, "ps": ps, "other": other, "sent": sent, "reply": reply, "draft": draft, "quote": q}


def test_static_page_shows_threads_without_buttons(conn):
    ids = seed(conn)
    html = dashboard.render(conn)
    assert f'id="ps-{ids["ps"]}"' in html and "Inquiry &lt;yoga&gt;" in html          # 제목은 이스케이프
    assert "단가 3.2달러" in html and "로고 비용" in html and "목표가 제시" in html
    assert "$3.2" in html and "1000개 $2.9" in html
    assert "다른 후보 공급처 1곳" in html
    assert 'class="negf' not in html and f"python -m negotiation_agent mark {ids['draft']} sent" in html


def test_server_page_has_forms(conn):
    ids = seed(conn)
    html = dashboard.render(conn, server=True)
    assert f'name="quote_id" value="{ids["quote"]}"' in html                          # 역제안
    assert f'name="message_id" value="{ids["draft"]}"' in html and 'value="sent"' in html   # 발송 완료 표시
    assert 'name="action" value="reply"' in html                                        # 답장 붙여넣기
    assert f'name="ps_id" value="{ids["other"]}"' in html                               # 연락 전 공급처: 견적 요청
    assert f'data-copy="msg-{ids["draft"]}"' in html


def test_neg_args_validation(conn):
    ids = seed(conn)
    args, label = server.neg_args(conn, {"action": "rfq", "ps_id": ids["other"], "qty": "--force", "lang": "zh",
                                         "incoterm": "fob", "note": "로고  인쇄"})
    assert args == ["rfq", str(ids["other"]), "--lang=zh", "--qty=--force", "--incoterm=FOB", "--note=로고 인쇄"]
    assert "Other Co" in label
    args, _ = server.neg_args(conn, {"action": "counter", "quote_id": ids["quote"], "tone": "firm", "price": "2.80"})
    assert args == ["counter", str(ids["quote"]), "--tone=firm", "--lang=en", "--price=2.8"]
    assert server.neg_args(conn, {"action": "mark", "message_id": ids["draft"], "status": "sent"})[0] == \
        ["mark", str(ids["draft"]), "sent"]
    bad = [{"action": "rfq", "ps_id": 999}, {"action": "counter", "quote_id": ids["quote"], "tone": "rude"},
           {"action": "counter", "quote_id": ids["quote"], "price": "abc"},
           {"action": "mark", "message_id": ids["reply"], "status": "sent"},          # 받은 메일은 표시 대상 아님
           {"action": "mark", "message_id": ids["draft"], "status": "deleted"},
           {"action": "reply", "ps_id": ids["ps"], "text": "  "}, {"action": "rfq", "ps_id": ids["ps"], "lang": "fr"},
           {"action": "drop_table"}]
    for body in bad:
        try:
            server.neg_args(conn, body)
        except server.BadRequest:
            continue
        raise AssertionError(f"검증 실패: {body}")


def test_reply_goes_through_file_and_job_reports_anchor(srv, conn, monkeypatch):
    ids = seed(conn)
    seen = {}

    def fake_command(kind, arg):
        seen["kind"], seen["arg"] = kind, arg
        path = next(a.split("=", 1)[1] for a in arg if a.startswith("--file="))
        seen["text"] = open(path, encoding="utf-8").read()
        return [sys.executable, "-c", f"print('저장'); print('RESULT_ANCHOR=ps-{ids['ps']}')"], ".", 1

    monkeypatch.setattr(server, "job_command", fake_command)
    code, job = call(srv + "/api/neg", {"action": "reply", "ps_id": ids["ps"], "text": "价格 3.2 美元\n谢谢"})
    assert code == 200, job
    done = wait_job(srv, job["id"])
    assert done["status"] == "done" and done["anchor"] == f"ps-{ids['ps']}"
    assert seen["kind"] == "neg" and seen["arg"][:2] == ["reply", str(ids["ps"])] and "--delete-file" in seen["arg"]
    assert seen["text"] == "价格 3.2 美元\n谢谢"
    assert call(srv + "/api/neg", {"action": "rfq", "ps_id": 9999})[0] == 400
    assert call(srv + "/api/neg", {"action": "mark", "message_id": ids["draft"], "status": "sent"},
                {"X-Trade-Hub": "0"})[0] == 403


def test_job_command_uses_negotiation_config():
    cmd, cwd, timeout = server.job_command("neg", ["auto"])
    cfg = json.loads(server.CONFIG.read_text(encoding="utf-8"))["agents"]["negotiation"]
    assert cmd[1:] == ["-m", "negotiation_agent", "auto"] and cwd.name == "negotiation_agent"
    assert timeout == cfg["timeout_minutes"]


def test_gmail_buttons_and_args(conn):
    ids = seed(conn)
    html = dashboard.render(conn, server=True)
    assert 'name="action" value="gmail_sync"' in html                                   # Gmail 확인
    assert 'name="action" value="gmail_draft"' in html and "Gmail 임시보관함에 넣기" in html
    with conn:
        conn.execute("UPDATE messages SET external_draft_id = 'd1' WHERE id = ?", (ids["draft"],))
    html = dashboard.render(conn, server=True)
    assert "Gmail 임시보관함에 있음" in html and "Gmail 임시보관함에 넣기" not in html
    assert server.neg_args(conn, {"action": "gmail_draft", "message_id": ids["draft"]})[0] == ["gmail-draft", str(ids["draft"])]
    assert server.neg_args(conn, {"action": "gmail_sync"})[0] == ["gmail-sync"]
    try:
        server.neg_args(conn, {"action": "gmail_draft", "message_id": ids["reply"]})       # 받은 메일은 안 됨
    except server.BadRequest:
        return
    raise AssertionError("받은 메일을 Gmail 초안으로 넣으면 안 됨")
