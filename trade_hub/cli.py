"""
사람이 공용 DB를 보고 다루는 명령어.

  trade_hub summary                 # 단계별 품목, 대기 중인 할 일, 최근 실행
  trade_hub candidates              # 후보 품목 (최신 점수순)
  trade_hub select 3 7 12           # 후보를 공급처 발굴 단계(sourcing)로 넘기기
  trade_hub drop 5 "검색량 부족"     # 품목 중단 (사유 기록)
  trade_hub tasks                   # 대기 중인 할 일 (자세히)
  trade_hub done 4                  # 할 일 완료 처리
  trade_hub dismiss 6 7             # 할 일 기각 (안 하기로 함)
  trade_hub briefing                # 최신 슈퍼바이저 브리핑
  trade_hub dashboard [--no-open] [--no-ref]   # 대시보드 HTML(data/dashboard.html) 만들고 열기 (DB 읽기 전용)
                                    #   --no-ref: 네이버 참고 이미지 검색 안 함
  trade_hub photo 16 사진.jpg        # 품목 확정 사진 등록 (data/images/16.jpg로 복사)
  trade_hub photo 16 --clear        # 확정 사진 삭제

(설치 없이: python -m trade_hub.cli summary)
"""

import sys

from trade_hub import db


def summary(conn):
    print("▶ 단계별 품목")
    rows = conn.execute(f"SELECT stage, COUNT(*) n FROM products GROUP BY stage ORDER BY {db.stage_order_sql()}").fetchall()
    for r in rows:
        print(f"  {r['stage']:<12} {r['n']}")
    if not rows:
        print("  (없음)")
    print("▶ 대기 중인 할 일")
    rows = conn.execute("SELECT id, priority, title FROM tasks WHERE status = 'pending' ORDER BY priority, created_at").fetchall()
    for r in rows:
        print(f"  [{r['priority']}] #{r['id']} {r['title']}")
    if not rows:
        print("  (없음)")
    print("▶ 최근 실행")
    for r in conn.execute("SELECT id, agent, status, started_at FROM agent_runs ORDER BY id DESC LIMIT 5"):
        print(f"  #{r['id']} {r['agent']:<11} {r['status']:<10} {r['started_at']}")


def candidates(conn):
    rows = conn.execute("""
        SELECT p.id, p.name, p.category, s.total
        FROM products p
        LEFT JOIN product_scores s ON s.id = (SELECT MAX(id) FROM product_scores WHERE product_id = p.id)
        WHERE p.stage = 'candidate' ORDER BY s.total DESC""").fetchall()
    for r in rows:
        print(f"  [{r['id']:>3}] {r['total'] if r['total'] is not None else '-':>3}점  {r['name']} ({r['category']})")
    if not rows:
        print("  (후보 없음)")


def move(conn, cmd, args):
    nums = [int(a) for a in args if a.isdigit()]
    note = " ".join(a for a in args if not a.isdigit()) or None
    if not nums:
        sys.exit(f"사용법: trade_hub {cmd} <품목 번호...>" + (' "사유"' if cmd == "drop" else ""))
    for n in nums:
        with conn:
            row = conn.execute("SELECT stage, name FROM products WHERE id = ?", (n,)).fetchone()
            if row is None:
                print(f"  ✗ 품목 번호 {n}가 없습니다")
                continue
            if cmd == "select" and row["stage"] != "candidate":
                print(f"  · #{n} {row['name']}: 이미 {row['stage']} 단계 → 그대로 둠")
                continue
            to = "sourcing" if cmd == "select" else "dropped"
            db.set_stage(conn, n, to, note=note or ("사람이 선정 → 공급처 발굴로" if cmd == "select" else "사람이 중단"))
            print(f"  ✓ #{n} {row['name']} → {to}")


def tasks(conn):
    rows = conn.execute("""SELECT t.*, p.name AS product FROM tasks t LEFT JOIN products p ON p.id = t.product_id
                           WHERE t.status = 'pending' ORDER BY t.priority, t.created_at""").fetchall()
    for r in rows:
        print(f"[{r['priority']}] #{r['id']} {r['title']}" + (f"  (품목 #{r['product_id']} {r['product']})" if r['product'] else ""))
        if r["detail"]:
            print(f"     {r['detail']}")
    if not rows:
        print("  (없음)")


def done(conn, args, status="done"):
    nums = [int(a) for a in args if a.isdigit()]
    if not nums:
        sys.exit(f"사용법: trade_hub {'done' if status == 'done' else 'dismiss'} <할 일 번호...>")
    for n in nums:
        with conn:
            cur = conn.execute("UPDATE tasks SET status = ?, done_at = ? WHERE id = ? AND status = 'pending'",
                               (status, db.now(), n))
        print(f"  {'✓' if cur.rowcount else '·'} 할 일 #{n}" + ("" if cur.rowcount else " (없거나 이미 처리됨)"))


def briefing(conn):
    b = conn.execute("SELECT created_at, summary FROM briefings ORDER BY id DESC LIMIT 1").fetchone()
    print(f"({b['created_at']})\n{b['summary']}" if b else "브리핑 없음 — python supervisor.py review")


def photo(conn, args):
    if len(args) != 2 or not args[0].isdigit():
        sys.exit("사용법: trade_hub photo <품목 번호> <사진 파일>   |   trade_hub photo <품목 번호> --clear")
    pid = int(args[0])
    row = conn.execute("SELECT name FROM products WHERE id = ?", (pid,)).fetchone()
    if row is None:
        sys.exit(f"품목 번호 {pid}가 없습니다")
    with conn:
        if args[1] == "--clear":
            db.clear_product_photo(conn, pid)
            print(f"  ✓ #{pid} {row['name']}: 확정 사진 삭제")
            return
        try:
            rel = db.set_product_photo(conn, pid, args[1])
        except ValueError as err:
            sys.exit(f"  ✗ {err}")
    print(f"  ✓ #{pid} {row['name']}: 확정 사진 {rel}  (확인: trade_hub dashboard)")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cmd, rest = (argv[0], argv[1:]) if argv else ("summary", [])
    conn = db.connect()
    print(f"DB: {db.default_path()}")
    if cmd == "summary":
        summary(conn)
    elif cmd == "candidates":
        candidates(conn)
    elif cmd in ("select", "drop"):
        move(conn, cmd, rest)
    elif cmd == "tasks":
        tasks(conn)
    elif cmd == "done":
        done(conn, rest)
    elif cmd == "dismiss":
        done(conn, rest, status="dismissed")
    elif cmd == "briefing":
        briefing(conn)
    elif cmd == "photo":
        photo(conn, rest)
    elif cmd == "dashboard":
        from trade_hub import dashboard
        out = rest[rest.index("--out") + 1] if "--out" in rest and rest.index("--out") + 1 < len(rest) else None
        path = dashboard.build(out, conn, refs="--no-ref" not in rest, log=print)
        print(f"  ✓ 대시보드: {path}")
        if "--no-open" not in rest:
            import webbrowser
            webbrowser.open(path.as_uri())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
