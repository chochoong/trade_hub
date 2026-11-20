"""
공용 DB의 날짜 → 구글 캘린더 '무역 (trade_hub)' 캘린더 (한 방향 동기화).

  trade_hub calendar-login            # 구글 로그인 (한 번, 브라우저가 열림)
  trade_hub calendar-sync [--dry-run] # DB 날짜를 캘린더에 반영 (--dry-run: 무엇을 바꿀지 보기만)

원칙
  · 권한은 calendar.app.created 하나 — 이 앱이 만든 '무역' 캘린더만 만들고 고칠 수 있다. 개인 일정은 읽지도 못한다.
  · 기본은 DB → 캘린더. 캘린더에서 고친 내용은 다음 동기화 때 DB 값으로 돌아간다.
  · 예외 (캘린더 → DB): 사람이 정하는 날짜 두 가지는 캘린더에서 날짜를 옮기면 DB에 반영한다.
      📌 할 일 기한 → tasks.due_date   ⏰ 후속 메일 예정일 → messages.followup_due_on (5번이 그 날부터 후속 초안)
    일정마다 마지막으로 쓴 날짜(private.day)를 달아 두고, 캘린더 날짜가 그와 다르면 '사람이 옮김'으로 본다.
    같은 사이에 DB 날짜도 바뀌었으면 DB가 이긴다. 메일·견적·선적 날짜(기록)는 옮겨도 되돌린다.
  · 참석자를 넣지 않는다 (구글이 초대 메일을 보내지 않게). 모두 종일 일정 + '한가함'(다른 일정과 겹쳐도 바쁨 표시 안 함).
  · 일정마다 DB 키(extendedProperties.private.trade_hub_key)를 달고, 그 키가 있는 일정만 고치거나 지운다.

넣는 일정
  📤 메일 보낸 날 · 📥 답장 받은 날 (messages)
  ⏰ 후속 메일 예정일 — 보낸 뒤 3일 답이 없으면 (5번 negotiation_agent의 FOLLOWUP_DUE_SQL과 같은 기준)
  📌 할 일 기한 (tasks.due_date, 대기 중) · ⌛ 견적 유효기간 만료 (quotes.valid_until)
  🚢 선적 예정 · 도착 예정 (shipments.etd / eta)

파일 (git 제외)
  OAuth 클라이언트: 환경변수 GOOGLE_CALENDAR_CREDENTIALS → secrets/google_credentials.json
                    → (없으면) ../negotiation_agent/secrets/gmail_credentials.json (같은 데스크톱 앱 클라이언트 재사용)
  로그인 토큰:      data/calendar_token.json  (Gmail 토큰과 따로 — 한쪽이 새어도 다른 쪽은 안전)
  캘린더 id:        data/calendar_state.json
"""

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from trade_hub import db

SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
CALENDAR_NAME = "무역 (trade_hub)"
TIMEZONE = "Asia/Seoul"
DASHBOARD_URL = "http://127.0.0.1:8765/"
PAST_DAYS = 180                 # 이보다 오래된 지난 일정(보낸·받은 메일)은 캘린더에 두지 않음
FOLLOWUP_DAYS, MAX_FOLLOWUPS = 3, 2   # negotiation_agent/hub.py 의 FOLLOWUP_DAYS·MAX_FOLLOWUPS와 같게 — 바꾸면 같이
KEY = "trade_hub_key"
COLORS = {"sent": "2", "received": "7", "followup": "6", "task": "11", "quote": "5", "shipment": "9"}
PULL_KINDS = ("task", "followup")   # 캘린더에서 날짜를 옮기면 DB에 반영하는 일정 종류
KIND_LABEL = {"rfq": "RFQ", "follow_up": "후속 메일", "negotiation": "협상", "sample": "샘플", "order": "발주", "other": "메일"}


# ---------------------------------------------------------------------------
# 파일 위치·로그인
# ---------------------------------------------------------------------------
def credentials_path():
    env = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS")
    if env:
        return Path(env)
    own = db.PKG_DIR.parent / "secrets" / "google_credentials.json"
    shared = db.PKG_DIR.parent.parent / "negotiation_agent" / "secrets" / "gmail_credentials.json"
    return own if own.exists() or not shared.exists() else shared


def token_path():
    return Path(os.environ.get("GOOGLE_CALENDAR_TOKEN") or db.default_path().parent / "calendar_token.json")


def state_path():
    return db.default_path().parent / "calendar_state.json"


def configured():
    return token_path().exists()


def login():
    """브라우저로 구글 로그인 → data/calendar_token.json. 권한: calendar.app.created 하나."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    cred = credentials_path()
    if not cred.exists():
        raise RuntimeError(f"OAuth 클라이언트 파일이 없습니다: {cred}\n"
                           "  Google Cloud에서 '데스크톱 앱' OAuth 클라이언트 JSON을 받아 trade_hub/secrets/google_credentials.json으로 저장")
    creds = InstalledAppFlow.from_client_secrets_file(str(cred), SCOPES).run_local_server(
        port=0, access_type="offline", prompt="consent")
    _save_token(creds)
    return token_path()


def _save_token(creds):
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(creds.to_json(), encoding="utf-8")


def service():
    """로그인 토큰으로 캘린더 API 연결 (만료되면 자동 갱신)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not configured():
        raise RuntimeError("구글 캘린더 로그인이 안 돼 있습니다 → trade_hub calendar-login")
    creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            raise RuntimeError("구글 캘린더 로그인이 만료됐습니다 → trade_hub calendar-login")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# DB → 넣을 일정 목록
# ---------------------------------------------------------------------------
def _day(ts):
    try:
        return date.fromisoformat(str(ts)[:10]) if ts else None
    except ValueError:
        return None


def _who(r):
    return " · ".join(x for x in (r["company"], f'#{r["product_id"]} {r["product"]}' if r["product_id"] else None) if x)


def _ev(key, kind, day, title, lines, product_id=None):
    desc = "\n".join(x for x in lines if x)
    if product_id:
        desc += f"\n\n대시보드: {DASHBOARD_URL}#p{product_id}"
    return {"key": key, "kind": kind, "day": day, "title": title, "description": desc.strip()}


def desired_events(conn, today=None):
    """DB 날짜로 캘린더에 있어야 할 일정 목록 {키: 일정}."""
    today = today or date.today()
    oldest = today - timedelta(days=PAST_DAYS)
    out = {}

    def add(ev):
        if ev["day"] and (ev["kind"] not in ("sent", "received") or ev["day"] >= oldest):
            out[ev["key"]] = ev

    msg_sql = """
        SELECT m.*, s.company, p.id AS product_id, p.name AS product
        FROM messages m JOIN product_suppliers ps ON ps.id = m.product_supplier_id
        JOIN suppliers s ON s.id = ps.supplier_id JOIN products p ON p.id = ps.product_id"""
    for r in conn.execute(msg_sql + " WHERE m.direction = 'outbound' AND m.status = 'sent'"):
        add(_ev(f"msg:{r['id']}", "sent", _day(r["sent_at"] or r["created_at"]),
                f"📤 {KIND_LABEL.get(r['kind'], '메일')} 보냄 · {r['company']}",
                [_who(r), f"제목: {r['subject']}" if r["subject"] else "", f"메일 #{r['id']}"], r["product_id"]))
    for r in conn.execute(msg_sql + " WHERE m.direction = 'inbound'"):
        add(_ev(f"msg:{r['id']}", "received", _day(r["sent_at"] or r["created_at"]),
                f"📥 답장 받음 · {r['company']}",
                [_who(r), f"제목: {r['subject']}" if r["subject"] else "", f"메일 #{r['id']}"], r["product_id"]))

    # 후속 메일 예정일: negotiation_agent FOLLOWUP_DUE_SQL에서 '3일 지남' 조건만 빼고, 예정일을 계산
    for r in conn.execute("""
        SELECT m.id, m.subject, m.sent_at, m.followup_due_on, s.company, p.id AS product_id, p.name AS product,
               (SELECT MAX(f.sent_at) FROM messages f WHERE f.reply_to_id = m.id AND f.kind = 'follow_up'
                  AND f.status = 'sent') AS last_followup,
               (SELECT COUNT(*) FROM messages f WHERE f.reply_to_id = m.id AND f.kind = 'follow_up'
                  AND f.status = 'sent') AS followups
        FROM messages m JOIN product_suppliers ps ON ps.id = m.product_supplier_id
        JOIN suppliers s ON s.id = ps.supplier_id JOIN products p ON p.id = ps.product_id
        WHERE m.direction = 'outbound' AND m.status = 'sent' AND m.kind != 'follow_up' AND m.sent_at IS NOT NULL
          AND ps.status != 'rejected' AND p.stage NOT IN ('closed', 'dropped')
          AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.product_supplier_id = m.product_supplier_id
                          AND r.direction = 'inbound' AND COALESCE(r.sent_at, r.created_at) >= m.sent_at)
          AND NOT EXISTS (SELECT 1 FROM messages n WHERE n.product_supplier_id = m.product_supplier_id
                          AND n.direction = 'outbound' AND n.kind != 'follow_up' AND n.status = 'sent' AND n.id > m.id)
          AND NOT EXISTS (SELECT 1 FROM messages f WHERE f.reply_to_id = m.id AND f.kind = 'follow_up'
                          AND f.status IN ('draft', 'approved', 'discarded'))"""):
        if r["followups"] >= MAX_FOLLOWUPS:
            continue
        base = _day(r["last_followup"] or r["sent_at"])
        n = r["followups"] + 1
        moved = _day(r["followup_due_on"])
        if moved and (not r["last_followup"] or moved > _day(r["last_followup"])):
            due, why = moved, "캘린더에서 옮긴 날짜 — 이 날부터 5번이 후속 메일 초안을 만듭니다 (보내는 건 직접)."
        else:
            due = base + timedelta(days=FOLLOWUP_DAYS) if base else None
            why = f"{FOLLOWUP_DAYS}일째 답이 없으면 5번이 후속 메일 초안을 만듭니다 (보내는 건 직접)."
        add(_ev(f"followup:{r['id']}", "followup", due,
                f"⏰ 후속 메일 예정 ({n}/{MAX_FOLLOWUPS}) · {r['company']}",
                [_who(r), why, "날짜를 옮기면 다음 동기화 때 DB에 반영됩니다.",
                 f"원래 메일 #{r['id']}" + (f" — {r['subject']}" if r["subject"] else "")], r["product_id"]))

    for r in conn.execute("""SELECT t.*, p.name AS product FROM tasks t LEFT JOIN products p ON p.id = t.product_id
                             WHERE t.status = 'pending' AND t.due_date IS NOT NULL"""):
        add(_ev(f"task:{r['id']}", "task", _day(r["due_date"]), f"📌 할 일 기한 · {r['title']}",
                [f"#{r['product_id']} {r['product']}" if r["product_id"] else "", r["detail"] or "",
                 f"할 일 #{r['id']} — 처리: trade_hub done {r['id']}", "날짜를 옮기면 다음 동기화 때 DB 기한에 반영됩니다."],
                r["product_id"]))

    for r in conn.execute("""
        SELECT q.*, s.company, p.id AS product_id, p.name AS product
        FROM quotes q JOIN product_suppliers ps ON ps.id = q.product_supplier_id
        JOIN suppliers s ON s.id = ps.supplier_id JOIN products p ON p.id = ps.product_id
        WHERE q.valid_until IS NOT NULL AND p.stage NOT IN ('closed', 'dropped')"""):
        add(_ev(f"quote:{r['id']}", "quote", _day(r["valid_until"]), f"⌛ 견적 만료 · {r['company']} ${r['unit_price_usd']:g}",
                [_who(r), f"견적 #{r['id']} · 최소 {r['min_qty']}개" if r["min_qty"] else f"견적 #{r['id']}"], r["product_id"]))

    for r in conn.execute("""
        SELECT sh.*, o.id AS order_id, s.company, p.id AS product_id, p.name AS product
        FROM shipments sh JOIN orders o ON o.id = sh.order_id JOIN product_suppliers ps ON ps.id = o.product_supplier_id
        JOIN suppliers s ON s.id = ps.supplier_id JOIN products p ON p.id = ps.product_id
        WHERE sh.status NOT IN ('cleared', 'delivered')"""):
        info = [_who(r), f"발주 #{r['order_id']} · 운송 #{r['id']}", f"추적번호: {r['tracking_no']}" if r["tracking_no"] else ""]
        if r["status"] == "booked":
            add(_ev(f"etd:{r['id']}", "shipment", _day(r["etd"]), f"🚢 선적 예정 · {r['company']}", info, r["product_id"]))
        add(_ev(f"eta:{r['id']}", "shipment", _day(r["eta"]), f"🚢 도착 예정 · {r['company']}", info, r["product_id"]))
    return out


def event_body(ev):
    fp = hashlib.sha1(json.dumps([ev["day"].isoformat(), ev["title"], ev["description"]],
                                 ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    return {"summary": ev["title"], "description": ev["description"],
            "start": {"date": ev["day"].isoformat()}, "end": {"date": (ev["day"] + timedelta(days=1)).isoformat()},
            "transparency": "transparent", "colorId": COLORS.get(ev["kind"]),
            "extendedProperties": {"private": {KEY: ev["key"], "fp": fp, "day": ev["day"].isoformat()}}}


# ---------------------------------------------------------------------------
# 캘린더에 반영
# ---------------------------------------------------------------------------
def _load_state():
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ensure_calendar(svc, create=True):
    """'무역' 캘린더 id. 없거나 지워졌으면 새로 만든다 (create=False면 None)."""
    from googleapiclient.errors import HttpError
    cal_id = _load_state().get("calendar_id")
    if cal_id:
        try:
            svc.calendars().get(calendarId=cal_id).execute()
            return cal_id
        except HttpError as e:
            if getattr(e, "status_code", None) not in (404, 410) and getattr(e.resp, "status", None) not in (404, 410):
                raise
    if not create:
        return None
    cal = svc.calendars().insert(body={"summary": CALENDAR_NAME, "timeZone": TIMEZONE,
                                       "description": "trade_hub 공용 DB의 날짜를 옮겨 온 캘린더 (자동 동기화). "
                                                      "📌 할 일 기한·⏰ 후속 메일 예정일은 날짜를 옮기면 DB에 반영, 나머지는 고쳐도 DB 값으로 돌아감"}
                                 ).execute()
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"calendar_id": cal["id"], "created_at": datetime.now().isoformat(timespec="seconds")},
                            ensure_ascii=False), encoding="utf-8")
    return cal["id"]


def _existing(svc, cal_id):
    """캘린더에 있는 trade_hub 일정 {키: (일정 id, fp, 마지막으로 쓴 날짜, 지금 캘린더 날짜)}
    — 키가 없는(사람이 직접 만든) 일정은 건드리지 않음."""
    out, token = {}, None
    while True:
        page = svc.events().list(calendarId=cal_id, maxResults=2500, pageToken=token, showDeleted=False,
                                 singleEvents=True).execute()
        for ev in page.get("items") or []:
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            if priv.get(KEY):
                out[priv[KEY]] = (ev["id"], priv.get("fp"), priv.get("day"), (ev.get("start") or {}).get("date"))
        token = page.get("nextPageToken")
        if not token:
            return out


def pull_moved(conn, have, want, dry_run=False, log=print):
    """캘린더에서 사람이 옮긴 📌 할 일 기한·⏰ 후속 메일 예정일 → DB. 반영한 키 목록을 돌려준다.
    조건: 캘린더 날짜 ≠ 마지막으로 쓴 날짜(사람이 옮김) 이고, DB 날짜 = 마지막으로 쓴 날짜(DB는 그대로).
    DB도 바뀌었으면 DB가 이김(반영 안 함). 완료된 할 일·끝난 후속 메일(want에 없음)은 무시."""
    moved = []
    for k, (_, _, wrote, now_day) in have.items():
        kind, _, ref = k.partition(":")
        if kind not in PULL_KINDS or k not in want or not wrote or not now_day or now_day == wrote:
            continue
        if want[k][0]["day"].isoformat() != wrote:
            continue                                   # DB 날짜도 바뀜 → DB가 이김
        try:
            new_day, ref_id = date.fromisoformat(now_day), int(ref)
        except ValueError:
            continue
        label = f"할 일 #{ref_id} 기한" if kind == "task" else f"메일 #{ref_id} 후속 예정일"
        log(f"  {'(미리보기) ' if dry_run else ''}캘린더에서 옮김 → DB: {label} {wrote} → {new_day.isoformat()}")
        moved.append(k)
        if dry_run:
            continue
        with conn:
            if kind == "task":
                conn.execute("UPDATE tasks SET due_date = ? WHERE id = ? AND status = 'pending'",
                             (new_day.isoformat(), ref_id))
            else:
                conn.execute("UPDATE messages SET followup_due_on = ? WHERE id = ?", (new_day.isoformat(), ref_id))
    return moved


def sync(conn, svc=None, dry_run=False, log=print, today=None):
    """캘린더에서 옮긴 할 일 기한·후속 예정일을 DB에 먼저 반영하고, DB 날짜를 캘린더에 반영.
    반환: {'add': n, 'update': n, 'delete': n, 'keep': n, 'pulled': n}."""
    want = {k: (ev, event_body(ev)) for k, ev in desired_events(conn, today).items()}
    moved = []
    if svc is None and dry_run and not configured():   # 로그인 전 미리보기: DB에서 만들 일정만 보여 줌
        log("  (구글 로그인 전 — DB에서 만들 일정만 표시합니다. 로그인: trade_hub calendar-login)")
        cal_id, have = None, {}
    else:
        svc = svc or service()
        cal_id = ensure_calendar(svc, create=not dry_run)
        have = _existing(svc, cal_id) if cal_id else {}
        moved = pull_moved(conn, have, want, dry_run, log)
        if moved and not dry_run:                      # DB가 바뀌었으니 다시 계산 (옮긴 날짜가 새 기준)
            want = {k: (ev, event_body(ev)) for k, ev in desired_events(conn, today).items()}
    skip = set(moved) if dry_run else set()           # 미리보기: 옮긴 일정을 되돌리는 '수정'으로 보이지 않게
    plan = {"add": [k for k in want if k not in have],
            "update": [k for k in want if k in have and k not in skip
                       and (have[k][1] != want[k][1]["extendedProperties"]["private"]["fp"]
                            or have[k][2] != want[k][1]["extendedProperties"]["private"]["day"]
                            or have[k][3] != want[k][1]["start"]["date"])],
            "delete": [k for k in have if k not in want]}
    keep = len(want) - len(plan["add"]) - len(plan["update"])
    for what, keys in plan.items():
        for k in keys:
            title = want[k][0]["title"] if k in want else k
            day = want[k][0]["day"].isoformat() if k in want else ""
            log(f"  {'(미리보기) ' if dry_run else ''}{ {'add': '추가', 'update': '수정', 'delete': '삭제'}[what]} {day} {title}")
            if dry_run:
                continue
            if what == "add":
                svc.events().insert(calendarId=cal_id, body=want[k][1]).execute()
            elif what == "update":
                svc.events().update(calendarId=cal_id, eventId=have[k][0], body=want[k][1]).execute()
            else:
                svc.events().delete(calendarId=cal_id, eventId=have[k][0]).execute()
    result = {"add": len(plan["add"]), "update": len(plan["update"]), "delete": len(plan["delete"]), "keep": keep,
              "pulled": len(moved)}
    log(f"  캘린더 '{CALENDAR_NAME}': 추가 {result['add']} · 수정 {result['update']} · 삭제 {result['delete']} · 그대로 {keep}"
        + (f" · 캘린더→DB {len(moved)}" if moved else "")
        + (" (미리보기 — 바뀐 것 없음)" if dry_run else ""))
    return result
