# 슈퍼바이저 설계 넘김 문서

슈퍼바이저를 새로 만들거나 고치는 사람(또는 에이전트)을 위한 문서다. 현재 구현은 이 저장소(`trade_hub`)의
`supervisor.py`·`supervisor.json`·`supervisor_llm.py`에 있다(커밋 `27880c9`). 새로 만들더라도
**3장의 공용 DB 계약은 다른 에이전트들이 의존하므로 깨면 안 된다.**

---

## 1. 배경

1인 수입 무역을 8개 에이전트로 나눈다. 각 에이전트는 **별도 프로젝트**이고 서로 직접 호출하지 않는다.
일은 공용 DB(`trade_hub/data/trade.db`, SQLite)로 주고받는다.

| # | agent 이름 | 역할 | 프로젝트 | 공용 DB 연결 |
|---|---|---|---|---|
| 1 | `discover` | 시장조사·아이템 발굴 | `../supplier_agent` | ✅ 연결됨 |
| 2 | `sourcing` | 공급처 발굴 | `../trade_sourcing_agent` | ❌ 파일 출력(`output/`)만 |
| 3 | `negotiation` | 커뮤니케이션·협상 (메일은 초안만) | 미정 | - |
| 4 | `documents` | 무역서류 (PI·CI·PL·계약서) | 미정 | - |
| 5 | `compliance` | 통관·규제 체크 | 미정 | - |
| 6 | `costing` | 원가·수익 계산 | 미정 | - |
| 7 | `sales` | 판매·마케팅 | 미정 | - |
| 8 | `finance` | 재무·관리 | 미정 | - |
| - | `supervisor` | 관제탑 (이 문서) | `trade_hub` | ✅ |

참고: `../trade_agent`(자체 `data/market.db`, 키워드 → 수요·경쟁·원가·마진 판정 + Streamlit)와
`../buyagent`(자체 `history.db`)는 1번·6번과 역할이 겹치는 이전 버전 성격이라 **아직 연결하지 않기로** 했다.

---

## 2. 사용자가 정한 원칙 (바꾸지 말 것)

1. **각 에이전트는 자기 임무만 한다.** 슈퍼바이저도 다른 에이전트의 일을 대신하지 않는다.
2. **메일은 초안만.** 메일 발송·결제·계약처럼 되돌리기 어려운 일은 에이전트도 슈퍼바이저도 직접 하지 않고,
   승인 대기함(`tasks`)에 사람이 할 일로 남긴다.
3. **연결 방식은 공용 DB.** 슈퍼바이저가 각 에이전트의 파일·DB를 읽는 어댑터 방식은 쓰지 않는다
   (출력 형식이 바뀔 때 조용히 깨지고, 단계를 넘나드는 판단은 데이터가 한 스키마에 있어야 가능해서).
4. **슈퍼바이저 = 스케줄러 + 검토관.** "LLM이 다른 에이전트를 지휘하는" 구조는 쓰지 않는다
   (예측이 어렵고 비싸며, 실행 순서는 DB 상태로 이미 정해지기 때문).
5. **candidate → sourcing 단계 이동은 사람이 결정**한다 (`trade_hub select <번호>`).

---

## 3. 공용 DB 계약 (`trade_hub/schema.sql`, 헬퍼 `trade_hub/db.py`)

### 3.1 연결
- 위치: 환경변수 `TRADE_DB`, 없으면 `trade_hub/data/trade.db`. 슈퍼바이저가 에이전트를 실행할 때 `TRADE_DB`를 넘긴다.
- 각 에이전트는 `pip install -e ../trade_hub` 후 `from trade_hub import db` → `db.connect()`.
  WAL 모드, `foreign_keys=ON`, 연결할 때마다 스키마 적용(`IF NOT EXISTS`).
- **스키마 변경은 `trade_hub`에서만**, 그리고 **컬럼·테이블 추가만** 허용한다(다른 에이전트가 깨지지 않게).
  변경 시 `SCHEMA_VERSION`을 올리고 `connect()`에 `ALTER` 마이그레이션을 넣는다. 아직 마이그레이션은 없다(user_version=1).
- 시각은 로컬 시간 ISO 문자열(`YYYY-MM-DDTHH:MM:SS`), 금액은 `_krw` 정수 / `_usd` 실수, JSON은 TEXT(+`json_valid` 검사).

### 3.2 모든 에이전트가 지키는 것 (슈퍼바이저 판단의 근거)
- 실행할 때마다 `agent_runs`에 **자기 agent 이름으로 1행** (`with db.agent_run(conn, "<이름>") as run:`).
  status: `running` → `succeeded` / `failed`(오류 내용 포함). `db.agent_run`은 정해진 이름만 받는다.
- 자기 담당 테이블에만 쓴다(schema.sql 머리말 표).
- 단계는 `db.advance_stage`로 **앞으로만** 이동. 종료(`closed`)·중단(`dropped`) 품목은 건드리지 않는다.
- 사람이 할 일은 `db.add_task` (같은 대상·제목의 대기 중 할 일이 있으면 중복 생성 안 함).

### 3.3 품목 단계 (`products.stage`)
```
candidate → sourcing → negotiating → sampling → ordered → shipping → selling → closed
                                                          (어느 단계에서든) → dropped
```
| 넘어가는 지점 | 누가 |
|---|---|
| (신규) → candidate | 1번 discover |
| candidate → sourcing | **사람** (`trade_hub select`) |
| sourcing 이후 | 각 단계 에이전트가 `advance_stage` (아직 연결된 에이전트 없음) |
| → dropped | 사람 (`trade_hub drop`) |

이동할 때마다 `product_events`에 이력(from/to/run_id/note)이 남는다.

### 3.4 테이블과 주인
| 테이블 | 쓰는 쪽 | 비고 |
|---|---|---|
| `agent_runs` | 모든 에이전트 | 실행 기록 |
| `products`, `product_events` | discover(생성), 각 단계(갱신), 사람(단계) | 품목 = 딜 단위, 이름 UNIQUE |
| `product_scores` | discover | 실행마다 누적. 최신 = MAX(id) |
| `suppliers`, `product_suppliers` | sourcing (+ costing이 `est_landed_krw`·`est_margin_pct`) | `product_suppliers.status`: candidate→contacted→quoted→sampling→selected/rejected |
| `messages` | negotiation | outbound는 `draft→approved→sent`(발송은 사람), inbound는 `received`(사람이 붙여넣음) |
| `quotes` | negotiation | 근거 메일(`message_id`) 연결 |
| `orders`, `shipments` | finance (물류 포함) | |
| `sales` | sales | |
| `ledger_entries` | finance | 수입 +, 지출 −. 뷰 `v_product_pnl`(매출 부가세 제외, 수입 부가세 공제) |
| `tasks` | 모든 에이전트 + 슈퍼바이저 | 승인 대기함. kind: send_message/review_quote/payment/document/decision/other, priority 1~3 |
| `briefings` | 슈퍼바이저 검토관 | |
| `supervisor_dispatches` | 슈퍼바이저 스케줄러 | |

---

## 4. 현재 슈퍼바이저 설계

### 4.1 실행 방식
- 작업 스케줄러에 `python supervisor.py tick` **하나만** 1시간마다 등록 (시작 위치 = trade_hub 폴더).
- `tick` = ① 스케줄 → ② (마지막 브리핑 후 `review.every_hours`(20)시간 지났으면) 검토.
- 슈퍼바이저 자신도 `agent_runs`에 `supervisor`로 기록된다.
- CLI: `status`(기본) / `tick` / `schedule` / `review`, 옵션 `--dry-run`, `--no-llm`, `--no-review`, `--config`.

### 4.2 ① 스케줄러 (코드, LLM 없음)
`supervisor.json`의 `agents.<agent 이름>`마다 판단(`decide`):
1. `enabled` 아님 → 건너뜀
2. **실행 중이면 건너뜀**: `timeout_minutes` 안에 시작된 `agent_runs.status='running'` 또는 끝나지 않은 dispatch가 있으면
3. `when_sql` 결과가 1 이상이고, 마지막 실행 후 `min_interval_hours`가 지났으면 → 실행 ("처리할 항목 N건")
4. `every_hours`가 있고 마지막 실행 후 그만큼 지났으면(또는 첫 실행) → 실행 ("주기 도래")
   - 마지막 실행 = `agent_runs`(에이전트가 직접 남김)와 `supervisor_dispatches` 중 최신
   - `every_hours: 0`도 유효한 값으로 취급(`is not None` 비교 — 예전 버그)

실행(`dispatch`): `command`의 `{python}`을 현재 파이썬으로 바꾸고, `cwd`(설정 파일 기준 상대경로)에서
subprocess로 순차 실행. 환경변수 `PYTHONIOENCODING=utf-8`, `TRADE_DB` 전달. stdout+stderr → `data/logs/<시각>_<agent>.log`.
`supervisor_dispatches`에 시작 시 행 생성 → 끝나면 `exit_code`·`finished_at`.
종료 코드: 0 성공 / 그 외 에이전트 실패 / `-1` 시간 초과 / `-2` 실행 불가(폴더·명령 없음).

`supervisor.json` 형식:
```json
{
  "agents": {
    "discover": {"enabled": true, "command": ["{python}", "supplier_agent.py", "--count", "10", "--no-open"],
                 "cwd": "../supplier_agent", "every_hours": 168, "timeout_minutes": 30},
    "sourcing": {"enabled": false, "command": ["{python}", "..."], "cwd": "../trade_sourcing_agent",
                 "when_sql": "SELECT COUNT(*) FROM products p WHERE p.stage='sourcing' AND NOT EXISTS (SELECT 1 FROM product_suppliers ps WHERE ps.product_id=p.id)",
                 "min_interval_hours": 6, "timeout_minutes": 60}
  },
  "review": {"enabled": true, "every_hours": 20, "provider": null, "model": null},
  "alerts": {"task_stale_days": 3, "product_stale_days": 14, "candidate_waiting_days": 7,
             "candidate_good_score": 65, "low_margin_pct": 15, "run_stuck_hours": 6}
}
```
(negotiation·compliance·costing·finance 예시도 꺼진 채로 들어 있다 — 폴더·명령은 가정값.)

### 4.3 ② 검토관 (LLM, 읽기 전용)
1. `snapshot()`: 단계별 개수, 진행 중 품목(마지막 이벤트·공급처 수·견적 수), 상위 후보 10개(최신 점수·네이버 데이터),
   대기 할 일 30개, 마진 낮은 공급처 10개, 최근 7일 실행·dispatch, 손익 뷰.
2. `rule_alerts()` (LLM 없이 항상 계산, level warn/info): 실패한 실행·dispatch / `run_stuck_hours` 넘게 running /
   `task_stale_days` 넘거나 기한 지난 할 일 / `product_stale_days` 넘게 진행 없는 품목 / sourcing인데 공급처 0곳(2일+) /
   `candidate_good_score` 이상인데 `candidate_waiting_days` 넘게 대기 / `low_margin_pct` 미만 마진 / 주기의 2배 넘게 안 돈 에이전트.
3. LLM 1회 호출(웹 검색 없음, `supervisor_llm.submit`): 스냅샷+경고를 주고 `submit_briefing` 도구로
   `summary`(마크다운 15줄 이내) + `suggestions`(최대 5개: title/detail/priority/product_id)를 받음. 스키마 검증 후 실패하면 재제출 요청.
   시스템 프롬프트 요지: 주어진 사실만, 읽기 전용, 단계를 넘나드는 판단 우선, 품목은 이름+번호로.
4. 저장: `briefings` 1행(summary, alerts_json, source_runs_json) + suggestions → `tasks`(kind=`decision`, 같은 제목 대기 중이면 중복 안 함).
   `product_id`는 실제 존재하는 번호만 연결.
5. `--no-llm`: 규칙 경고만으로 간단한 브리핑을 만든다(API 비용 없음).

**검토관이 하지 않는 것**: 단계 변경, 메일 발송, 다른 에이전트 테이블 수정, 다른 에이전트 실행(그건 스케줄러).

### 4.4 LLM 설정 (`supervisor_llm.py`, `.env`)
- `PROVIDER`(anthropic|openrouter), `ANTHROPIC_API_KEY`(+선택 `ANTHROPIC_EFFORT`), `OPENROUTER_API_KEY`·`OPENROUTER_MODEL`.
- 기본 모델 `claude-sonnet-5-5`. 키는 각 접속처 것만 명시적으로 넘긴다(다른 서비스 키가 섞여 전송되지 않게).
- `anthropic>=2.8,<3`. `.env` 줄 끝 `# 주석` 무시.

---

## 5. 알려진 한계·남은 일

- **연결된 에이전트가 1번뿐**이라 스케줄러의 `when_sql`·검토관의 단계 간 판단은 아직 실제로 쓰이지 않는다.
  2번(`trade_sourcing_agent`)에 DB 입출력 추가가 다음 순서: `stage='sourcing'`이고 공급처 없는 품목을 읽어 처리하고,
  결과를 `suppliers`·`product_suppliers`에 저장(기존 파일 출력은 유지). 공급처 저장 헬퍼(`upsert_supplier` 등)는
  `trade_hub/db.py`에 공용으로 추가할 것(현재 없음).
- 다른 에이전트의 실행 명령이 확정되지 않아 `supervisor.json` 예시의 `command`·`cwd`는 가정값.
- 실행은 순차(병렬 없음). 한 tick에서 오래 걸리는 에이전트가 있으면 다음 에이전트가 늦어진다.
- 비정상 종료로 `agent_runs.status='running'`이 남으면 `timeout_minutes` 동안 그 에이전트를 건너뛰고, 이후 검토관이 경고한다(자동 정리는 없음).
- `when_sql`은 설정 파일의 SQL을 그대로 실행한다(신뢰된 로컬 설정 전제).
- 검토관 제안의 중복 판단은 "같은 제목의 대기 중 할 일" 기준이라, 제목이 조금 달라지면 비슷한 할 일이 쌓일 수 있다.
- 대시보드 없음: 현재는 `trade_hub` CLI(`summary`/`tasks`/`done`/`briefing`)로만 본다.
- hub에 자동 테스트 없음(지금까지는 스크래치 DB로 수동 검증).

## 6. 검증 방법 (실제 DB를 건드리지 않고)

```bash
set TRADE_DB=C:\temp\test.db                  # (bash: export TRADE_DB=...) 스크래치 DB
python supervisor.py tick --dry-run           # 무엇을 실행할지
python supervisor.py tick --no-llm --config test.json   # 가짜 명령(python -c ...)으로 성공/실패/시간초과/폴더없음 확인
python supervisor.py review                   # LLM 브리핑 1회 (비용 소액)
trade_hub summary / tasks / briefing
```
