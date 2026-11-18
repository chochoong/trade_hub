from datetime import date

import pytest

from trade_hub import calendar_sync as cs, db

TODAY = date(2026, 11, 18)


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeCalendar:
    """구글 캘린더 API 흉내 (calendars·events의 쓰는 부분만). 실제 구글에는 연결하지 않음."""

    def __init__(self):
        self.calendars_made, self.store, self.n = [], {}, 0

    def calendars(self):
        fake = self

        class C:
            def get(self, calendarId):
                return _Call(lambda: {"id": calendarId})

            def insert(self, body):
                fake.calendars_made.append(body)
                return _Call(lambda: {"id": f"cal{len(fake.calendars_made)}"})
        return C()

    def events(self):
        fake = self

        class E:
            def list(self, calendarId, **kw):
                return _Call(lambda: {"items": [dict(v, id=k) for k, v in fake.store.items()]})

            def insert(self, calendarId, body):
                fake.n += 1
                assert "attendees" not in body                     # 초대 메일이 나가지 않게
                fake.store[f"e{fake.n}"] = body
                return _Call(lambda: {})

            def update(self, calendarId, eventId, body):
                fake.store[eventId] = body
                return _Call(lambda: {})

            def delete(self, calendarId, eventId):
                fake.store.pop(eventId)
                return _Call(lambda: {})
        return E()

    def titles(self):
        return sorted(e["summary"] for e in self.store.values())


def _seed(conn):
    with conn:
        pid = db.upsert_product(conn, "AI안경", stage="negotiating")
        a = db.link_supplier(conn, pid, db.upsert_supplier(conn, "Quiet Co", "China"), purchase_mode="small_batch")
        b = db.link_supplier(conn, pid, db.upsert_supplier(conn, "Fast Co", "China"), purchase_mode="small_batch")
        ins = "INSERT INTO messages (product_supplier_id, direction, kind, subject, body, status, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        quiet = conn.execute(ins, (a, "outbound", "rfq", "RFQ 키캡", "본문", "sent", "2026-11-14T10:00:00")).lastrowid
        conn.execute(ins, (b, "outbound", "rfq", "RFQ", "본문", "sent", "2026-11-15T10:00:00"))
        conn.execute(ins, (b, "inbound", None, "Re: RFQ", "답장", "received", "2026-11-16T09:00:00"))
        conn.execute(ins, (a, "outbound", "rfq", "초안", "본문", "draft", None))                 # 아직 안 보낸 초안 → 없음
        conn.execute("INSERT INTO tasks (kind, title, product_id, due_date) VALUES ('payment', '선금 송금', ?, '2026-11-25')", (pid,))
        conn.execute("INSERT INTO tasks (kind, title, due_date, status) VALUES ('other', '끝난 일', '2026-11-20', 'done')")
        conn.execute("INSERT INTO quotes (product_supplier_id, unit_price_usd, min_qty, valid_until) VALUES (?, 2.8, 50, '2026-12-01')", (b,))
    return pid, a, b, quiet


def test_desired_events(conn):
    pid, a, b, quiet = _seed(conn)
    ev = cs.desired_events(conn, TODAY)
    kinds = sorted((e["kind"], e["day"].isoformat()) for e in ev.values())
    assert kinds == [("followup", "2026-11-17"),                 # Quiet Co: 11-14 보냄, 답 없음 → 3일 뒤
                     ("quote", "2026-12-01"), ("received", "2026-11-16"),
                     ("sent", "2026-11-14"), ("sent", "2026-11-15"), ("task", "2026-11-25")]
    f = ev[f"followup:{quiet}"]
    assert "Quiet Co" in f["title"] and "(1/2)" in f["title"] and f"#p{pid}" in f["description"]


def test_followup_moves_after_followup_sent_and_stops_after_two(conn):
    pid, a, b, quiet = _seed(conn)
    ins = ("INSERT INTO messages (product_supplier_id, direction, kind, body, status, sent_at, reply_to_id) "
           "VALUES (?, 'outbound', 'follow_up', '후속', 'sent', ?, ?)")
    with conn:
        conn.execute(ins, (a, "2026-11-18T09:00:00", quiet))
    f = cs.desired_events(conn, TODAY)[f"followup:{quiet}"]
    assert f["day"] == date(2026, 11, 21) and "(2/2)" in f["title"]
    with conn:
        conn.execute(ins, (a, "2026-11-21T09:00:00", quiet))
    assert f"followup:{quiet}" not in cs.desired_events(conn, TODAY)   # 최대 2번


def test_sync_adds_then_keeps_then_updates(conn):
    pid, a, b, quiet = _seed(conn)
    cal = FakeCalendar()
    quiet_log = lambda *_: None
    assert cs.sync(conn, cal, log=quiet_log, today=TODAY) == {"add": 6, "update": 0, "delete": 0, "keep": 0}
    assert len(cal.calendars_made) == 1 and cal.calendars_made[0]["summary"] == cs.CALENDAR_NAME
    assert all(e["transparency"] == "transparent" and "date" in e["start"] for e in cal.store.values())
    assert cs.sync(conn, cal, log=quiet_log, today=TODAY) == {"add": 0, "update": 0, "delete": 0, "keep": 6}
    assert len(cal.calendars_made) == 1                          # 캘린더는 한 번만 만듦

    cal.store["mine"] = {"summary": "치과 예약", "start": {"date": "2026-11-20"}}   # 사람이 직접 넣은 일정
    with conn:                                                   # Quiet Co가 답장 → 후속 예정 사라지고 받은 메일 생김
        conn.execute("INSERT INTO messages (product_supplier_id, direction, body, status, sent_at) "
                     "VALUES (?, 'inbound', '답', 'received', '2026-11-18T08:00:00')", (a,))
        conn.execute("UPDATE tasks SET due_date = '2026-11-26' WHERE title = '선금 송금'")
    r = cs.sync(conn, cal, log=quiet_log, today=TODAY)
    assert r == {"add": 1, "update": 1, "delete": 1, "keep": 4}   # 6개 = 받은 답장 추가 1 + 기한 수정 1 + 그대로 4
    assert "치과 예약" in cal.titles()                            # trade_hub 키가 없는 일정은 건드리지 않음
    assert not any(t.startswith("⏰") for t in cal.titles())


def test_dry_run_changes_nothing(conn, monkeypatch):
    _seed(conn)
    cal = FakeCalendar()
    lines = []
    r = cs.sync(conn, cal, dry_run=True, log=lines.append, today=TODAY)
    assert r["add"] == 6 and cal.store == {} and cal.calendars_made == []
    assert any("(미리보기) 추가" in x for x in lines)


def test_old_mail_left_out_but_future_kept(conn):
    pid, a, b, quiet = _seed(conn)
    ev = cs.desired_events(conn, date(2027, 12, 1))              # 1년 넘게 지난 뒤
    assert not [e for e in ev.values() if e["kind"] in ("sent", "received")]
    assert any(e["kind"] == "task" for e in ev.values())          # 기한 일정은 남김


def test_not_logged_in(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN", str(tmp_path / "없음.json"))
    assert not cs.configured()
    with pytest.raises(RuntimeError, match="calendar-login"):
        cs.service()


def test_supervisor_skips_calendar_when_not_logged_in(conn, tmp_path, monkeypatch):
    import supervisor as sv
    monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN", str(tmp_path / "없음.json"))
    assert sv.sync_calendar(conn, {"calendar": {}}) is None
    monkeypatch.setattr(cs, "configured", lambda: True)
    monkeypatch.setattr(cs, "sync", lambda *a, **k: (_ for _ in ()).throw(OSError("네트워크")))
    msgs = []
    assert sv.sync_calendar(conn, {}, log=msgs.append) is None and "캘린더 동기화 실패" in msgs[0]   # 실패해도 tick은 계속
