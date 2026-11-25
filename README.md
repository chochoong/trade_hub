# trade_hub — 1인 무역 에이전트 관제탑

1인 무역 파이프라인의 에이전트들(시장조사 → 공급처 발굴 → 통관·규제 → 원가 → 협상 → 서류 → 판매 → 재무)이
함께 쓰는 **공용 DB**와, 모든 에이전트를 지켜보는 **슈퍼바이저**입니다. 각 에이전트는 별도 프로젝트로 두고,
서로 직접 호출하지 않고 이 DB로 일을 주고받습니다.

| 파일 | 역할 |
|---|---|
| `trade_hub/schema.sql` | 공용 DB 스키마 = 에이전트 간 약속. 머리말에 에이전트별 담당 테이블 정리. **스키마 변경은 여기서만** |
| `trade_hub/db.py` | 공용 DB 헬퍼 (연결·실행 기록·품목·단계 이동·할 일). 각 에이전트가 `from trade_hub import db`로 사용 |
| `trade_hub/cli.py` | 사람이 쓰는 명령어 `trade_hub` (현황, 후보 선정·중단, 할 일, 브리핑) |
| `supervisor.py` / `supervisor.json` | 슈퍼바이저: 스케줄러(규칙대로 에이전트 실행) + 검토관(LLM 브리핑·경고·제안) |
| `data/trade.db` | 공용 DB 파일 (git에 올라가지 않음) |

## 1. 설치

```bash
pip install -r requirements.txt     # anthropic + trade_hub(편집 모드)
```

`.env` (git에 올라가지 않음) — 검토관 LLM용:

```
ANTHROPIC_API_KEY=sk-ant-...
PROVIDER=anthropic                   # 또는 openrouter (OPENROUTER_API_KEY, OPENROUTER_MODEL)
```

각 에이전트 프로젝트에서는 `pip install -e ../trade_hub`로 설치해 씁니다 (편집 모드라 이 폴더를 고치면 바로 반영).

## 2. 사람이 쓰는 명령 (`trade_hub`)

```bash
trade_hub summary              # 단계별 품목, 대기 중인 할 일, 최근 실행
trade_hub candidates           # 후보 품목 (최신 점수순)
trade_hub select 3 7 12        # 후보를 공급처 발굴 단계(sourcing)로 넘기기
trade_hub drop 5 "검색량 부족"  # 중단 (사유 기록)
trade_hub tasks                # 승인 대기함 (자세히)
trade_hub done 4               # 할 일 완료
trade_hub dismiss 6 7          # 할 일 기각 (안 하기로 함)
trade_hub briefing             # 최신 슈퍼바이저 브리핑
```

## 3. 슈퍼바이저

```bash
python supervisor.py                 # 상태: 에이전트별 마지막 실행·실행 예정, 최근 브리핑
python supervisor.py tick            # 스케줄 실행 + (20시간마다) 검토 ← 작업 스케줄러에 1시간마다 등록
python supervisor.py tick --dry-run  # 무엇을 실행할지 보기만
python supervisor.py review          # 지금 바로 검토 (LLM)
python supervisor.py review --no-llm # 규칙 기반 경고만 (API 비용 없음)
python supervisor.py backup          # 공용 DB 백업 (tick에서 하루 1번 자동)
```

**① 스케줄러 (코드, LLM 없음)** — `supervisor.json`의 에이전트를 규칙대로 실행
- `every_hours`: 마지막 실행 후 N시간이 지나면 실행
- `when_sql`: 공용 DB에 처리할 일이 있으면 실행 (`min_interval_hours`로 과도한 재실행 방지)
- 실행 중이면 건너뛰고 `timeout_minutes`가 지나면 하위 프로세스까지 강제 종료하고, `running`으로 남은 실행 기록은 실패로 정리. 기록은 `supervisor_dispatches`, 출력은 `data/logs/`
- 실행할 때 환경변수 `TRADE_DB`로 공용 DB 경로를 넘김

- tick이 겹치지 않도록 잠금(`data/supervisor.lock`)을 쓰고, `run_stuck_hours`(기본 6시간) 넘게 `running`인 실행 기록은 실패로 정리

**② 검토관 (LLM, 읽기 전용)** — 공용 DB 전체를 읽고 브리핑
- 규칙 경고: 실패한 실행, 오래 대기 중인 할 일, 진행 없는 품목, 방치된 고득점 후보, 낮은 마진, 오래 안 돈 에이전트
- LLM이 오늘의 브리핑과 **결정할 일**(최대 5개)을 작성 → `briefings`와 승인 대기함(`tasks`, kind=decision)
- 단계 변경·메일 발송·다른 에이전트 데이터 수정은 하지 않음
- 이미 대기 중인 할 일과 같은 취지는 다시 제안하지 않고, 자기가 만든 제안이 `decision_expire_days`(기본 7일) 넘게 대기하면 자동 기각

**③ 백업** — tick 때 하루 1번 공용 DB를 `data/backups/trade_YYYYMMDD.db`로 복사, 최근 `keep`(기본 14)개만 유지

정기 실행: 작업 스케줄러에서 프로그램 `python`, 인수 `supervisor.py tick`, **시작 위치를 이 폴더로** 지정.

## 4. 에이전트 연결 규칙

1. `pip install -e ../trade_hub` 후 `from trade_hub import db`
2. 실행할 때 `with db.agent_run(conn, "<에이전트 이름>") as run:`으로 감싸 `agent_runs`에 기록
   - 쓰기는 `with conn:`으로 **짧게 나눠 커밋**할 것. 커밋 안 한 채 LLM 응답을 기다리면 그동안 DB 쓰기 잠금이 잡혀
     다른 에이전트·CLI가 `database is locked`로 실패함. 실행이 예외로 끝나면 커밋 안 된 쓰기는 버려짐(rollback)
   (이름: discover · sourcing · compliance · costing · negotiation · documents · sales · finance)
3. 자기 담당 테이블에만 쓰기 (`trade_hub/schema.sql` 머리말). 단계는 `db.advance_stage`로 앞으로만 이동
4. 되돌리기 어려운 일(메일 발송·결제 등)은 실행하지 말고 `db.add_task`로 승인 대기함에 남기기
5. `supervisor.json`에 `command`·`cwd`·실행 조건을 등록하고 `enabled: true`

| 에이전트 | 프로젝트 | 상태 |
|---|---|---|
| 1. discover | `../supplier_agent` | 연결됨 (주 1회) |
| 2. sourcing | `../trade_sourcing_agent` | 설정 예시만 (꺼짐) — 공용 DB 연동 필요 |
| 3. compliance | `../compliance_agent` | 공용 DB 연동됨 (`python -m compliance_agent check-hub` → `compliance_checks`) — 슈퍼바이저 설정은 꺼짐 |
| 4. costing | `../cost_agent` | 연결됨 — `python -m cost_agent from-hub` (가격이 생기거나 바뀐 공급처 줄의 개당 원가·마진 → `product_suppliers.est_landed_krw·est_margin_pct`) |
| 5. negotiation | `../negotiation_agent` | 연결됨 — 대시보드 5번 페이지 버튼 + 슈퍼바이저 `auto`(답장 분석·후속 메일) |
| 6. documents | `../trade_docs_agent` | 설정 등록됨 (꺼짐) — `scripts/tradedocs.py from-hub` (선정 공급처 견적 → PO 초안 xlsx → `documents`·`tasks`, 승인은 6번 approve) · openpyxl 필요 |
| 7·8 | 미정 | 설정 예시만 (꺼짐) |

## 5. 테스트

```bash
pip install pytest
python -m pytest -q      # 임시 DB로 실행 (data/trade.db는 건드리지 않음)
```
