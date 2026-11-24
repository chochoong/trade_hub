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
| 2 | `sourcing` | 공급처 발굴 | `../trade_sourcing_agent` | ✅ 연결됨 (`python -m sourcing_agent from-db`, 7장) |
| 3 | `compliance` | 통관·규제 체크 (발주 전 — KC 인증·표시는 생산 때 해결, 선적 뒤엔 통관 보류→폐기·반송) | `../compliance_agent` | ✅ 연결됨 (`python -m compliance_agent check-hub`, 슈퍼바이저 설정은 꺼짐) |
| 4 | `costing` | 원가·수익 계산 (목표가·포기 가격은 공용 DB에 쓰지 않음 — 로컬 보고서만) | `../cost_agent` | ✅ 연결됨 (`python -m cost_agent from-hub`) |
| 5 | `negotiation` | 커뮤니케이션·협상 (메일은 초안만) — 인증 요건·원가를 안 뒤에 | `../negotiation_agent` | ✅ 연결됨 (7.7) |
| 6 | `documents` | 무역서류 (PI·CI·PL·계약서) | 미정 | - |
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
  변경 시 `SCHEMA_VERSION`을 올리고 `db.MIGRATIONS`에 추가 컬럼을 적는다(`connect()`가 예전 DB에 `ALTER TABLE`). 현재 **v3** (7장).
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
    "sourcing": {"enabled": false, "command": ["{python}", "-m", "sourcing_agent", "from-db"], "cwd": "../trade_sourcing_agent",
                 "when_sql": "SELECT COUNT(*) FROM products p WHERE p.stage='sourcing' AND NOT EXISTS (SELECT 1 FROM product_suppliers ps WHERE ps.product_id=p.id) AND NOT EXISTS (SELECT 1 FROM product_events e JOIN agent_runs r ON r.id=e.run_id WHERE e.product_id=p.id AND r.agent='sourcing')",
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

- 연결된 에이전트는 1·2번. 2번은 DB 연동이 끝났지만 **슈퍼바이저 자동 실행은 아직 꺼져 있다**(7.2 — 실행 파이썬 문제).
- 다른 에이전트의 실행 명령이 확정되지 않아 `supervisor.json` 예시의 `command`·`cwd`는 가정값.
- 실행은 순차(병렬 없음). 한 tick에서 오래 걸리는 에이전트가 있으면 다음 에이전트가 늦어진다.
- 비정상 종료로 `agent_runs.status='running'`이 남으면 `timeout_minutes` 동안 그 에이전트를 건너뛰고, 이후 검토관이 경고한다(자동 정리는 없음).
- `when_sql`은 설정 파일의 SQL을 그대로 실행한다(신뢰된 로컬 설정 전제).
- 검토관 제안의 중복 판단은 "같은 제목의 대기 중 할 일" 기준이라, 제목이 조금 달라지면 비슷한 할 일이 쌓일 수 있다.
- 대시보드: `trade_hub dashboard`(정적 HTML, 7.4). 브라우저 업로드·버튼은 아직 없음(CLI로 처리).
- hub에 자동 테스트 없음(지금까지는 스크래치 DB로 수동 검증).

## 6. 검증 방법 (실제 DB를 건드리지 않고)

```bash
set TRADE_DB=C:\temp\test.db                  # (bash: export TRADE_DB=...) 스크래치 DB
python supervisor.py tick --dry-run           # 무엇을 실행할지
python supervisor.py tick --no-llm --config test.json   # 가짜 명령(python -c ...)으로 성공/실패/시간초과/폴더없음 확인
python supervisor.py review                   # LLM 브리핑 1회 (비용 소액)
trade_hub summary / tasks / briefing
```

---

## 7. 변경 사항 (2026-11-17) — 슈퍼바이저가 알아야 할 것

### 7.1 2번(sourcing) 공용 DB 연결
- 명령: `python -m sourcing_agent from-db [품목번호 ...] [--type small_batch|oem] [--max-qty 50] [--dry-run]`
  (cwd `../trade_sourcing_agent`). 번호를 생략하면 **아직 조사하지 않은 sourcing 품목 전부**.
- 읽는 것: `products`(stage='sourcing'), 최신 `product_scores.est_fob_usd`. 1번이 넘긴 이름·영어 검색어·시세·선정 이유·
  예상 규제·위험 요소로 소싱 조건을 만든다(YAML 파일 불필요).
- 쓰는 것 (`agent_runs`에 `sourcing`으로 1행):
  - `suppliers` / `product_suppliers` — 품목당 검증한 공급처(보통 5곳), 상위 3곳 `is_top_pick=1`.
    `suppliers.trust`는 2번 등급 A→상, B→중, C·D→하. `trust_reason`은 `"<점수>점 (<등급>) — 요약"` 형식.
  - `tasks` — kind=`decision`, `"[품목] 공급처 N곳 검토 — 연락할 곳 고르기"`(priority 2), 0곳이면 `"공급처를 찾지 못함 …"`(priority 1).
    `detail` = 리포트 경로(`output/<실행폴더>/report.html`).
  - `product_events` — `from_stage = to_stage = 'sourcing'`, note `"공급처 조사: N곳"` (**단계 변경이 아닌 조사 기록**).
- **단계는 바꾸지 않는다.** 공급처를 골라 협상으로 넘기는 건 사람/3번.
- **재실행 방지**: 이미 sourcing 실행 기록(`product_events` + `agent_runs.agent='sourcing'`)이 있는 품목은
  공급처 0곳이어도 자동으로 다시 돌리지 않는다(API 비용 반복 방지). 다시 하려면 번호를 지정해 수동 실행.
- 실패 처리: 품목을 순서대로 처리하며 각 품목 결과는 바로 커밋. 중간에 예외가 나면 그 실행은 `failed`, 이미 저장한 품목은 남는다.
- 참고: `trade_sourcing_agent`는 아직 git 저장소가 아니다.

### 7.2 `supervisor.json` 의 sourcing 항목 (변경됨, 아직 `enabled: false`)
- `command`: `["{python}", "-m", "sourcing_agent", "from-db"]`
- `when_sql`: 위 재실행 방지 조건을 추가 (`hub.PENDING_SQL`과 같은 조건 — 둘 중 하나를 바꾸면 같이 바꿀 것).
- **켜기 전에 결정 필요**: `{python}`(슈퍼바이저를 돌리는 파이썬)에는 2번 의존성(`pyyaml`, `python-dotenv`)이 없어 지금 켜면 실패한다.
  선택지: (a) 그 파이썬에 2번 의존성 설치, (b) 에이전트별 파이썬을 설정에 둘 수 있게
  (예: `"python": "venv/Scripts/python.exe"`, `cwd` 기준) `dispatch`를 확장. 에이전트가 늘어날 것을 생각하면 (b) 권장.

### 7.3 스키마 v1 → v3 (컬럼 추가만, 기존 코드 영향 없음)
| 버전 | 추가 | 의미 |
|---|---|---|
| v2 | `products.ref_url` | 참고 페이지 링크 (1번이 조사 중 본 대표 상품 페이지). **사진 아님** |
| v2 | `products.image_url` | **확정 대표 사진만**: 사람이 업로드(`trade_hub photo`)했거나 공급사 사진 중 고른 것 |
| v3 | `product_suppliers.image_url` | 공급사가 준 상품 사진 (2·3번이 기록 예정, 아직 채우는 에이전트 없음) |

- 값 형식: http(s) URL 또는 **DB 폴더 기준 상대경로**(`images/16.jpg`). 화면에 쓸 땐 `db.resolve_image(value)`.
- **검색으로 찾은 '참고 이미지'는 DB에 저장하지 않는다**(대시보드가 그릴 때만 네이버 이미지 검색). 검토관·다른 에이전트도
  참고 이미지를 우리 상품 사진으로 쓰거나 DB에 넣지 말 것.
- 마이그레이션: `db.MIGRATIONS = {버전: [(테이블, 컬럼, 타입)]}` → `connect()`가 `user_version`을 보고 없는 컬럼만 `ALTER`.
  실제 DB는 v3 적용 완료. 정리 전 백업: `data/backups/before_photo_v3_20261117_1018.db`.

### 7.4 `trade_hub/db.py` 새 헬퍼와 CLI
- 공급처: `upsert_supplier(conn, company, country, **fields)` — (업체명, 국가)로 찾아 갱신/생성, None은 덮어쓰지 않음.
  `link_supplier(conn, product_id, supplier_id, run_id, **fields)` — **`status`(연락·견적 진행)는 건드리지 않음**.
- 사진: `set_product_photo(conn, id, 파일)` → `data/images/<id>.<ext>`로 복사 후 연결, `clear_product_photo`, `resolve_image`, `images_dir`.
- CLI: `trade_hub photo <번호> <파일>` / `trade_hub photo <번호> --clear`,
  `trade_hub dashboard [--no-open] [--no-ref] [--out 파일]` → `data/dashboard.html` (DB 읽기 전용).
- 대시보드의 참고 이미지는 `trade_hub/naver.py`가 NAVER API HUB 이미지 검색(`GET /search/v1/image`)으로 가져온다.
  키는 `trade_hub/.env`의 `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET`(HUB 애플리케이션에 '검색' API 구독 필요, 하루 25,000회).
  키가 없거나 실패하면 '이미지 없음' 아이콘으로 표시하고 오류 없이 진행.

### 7.5 슈퍼바이저 쪽에서 검토하면 좋은 것 (제안)
1. **검토관 경고 중복**: 2번이 공급처 0곳일 때 이미 priority 1 `decision` 할 일을 만든다. `rule_alerts`의
   "sourcing인데 공급처 0곳(2일+)"이 같은 품목을 또 경고하므로, 그 할 일이 대기 중이면 경고를 생략하거나 문구를 맞출 것.
2. **`product_stale_days` 기준**: 2번의 조사 기록(`from_stage = to_stage`)도 `product_events`라서 "마지막 진행"으로 잡힌다.
   의도와 맞는지 확인(단계 변경만 볼 거라면 `from_stage IS NOT to_stage` 조건).
3. **tick 끝에 대시보드 갱신**: `trade_hub dashboard --no-open`을 tick 마지막에 돌리면 열 때마다 최신 상태.
   네이버 호출이 매번 일어나므로 원치 않으면 `--no-ref`.
4. **스냅샷에 공급처 요약 추가**: 진행 중 품목의 추천 공급처(`is_top_pick`)·신뢰도·위험 건수를 넣으면
   검토관이 "어느 업체에 먼저 연락할지" 같은 단계 간 판단을 할 수 있다.
5. `trade_hub/.env`에 네이버 키가 추가됐다(검토관 LLM 키와 같은 파일). 검토관은 이 키를 쓰지 않는다.


### 7.6 대시보드 서버 `trade_hub serve` (2026-11-17 추가)
- `trade_hub serve [--port 8765] [--no-open] [--no-ref]` → `http://127.0.0.1:8765` (이 PC에서만, `trade_hub/server.py`).
  정적 대시보드와 같은 화면 + **검색창 → [새로 조사]**, 후보의 **[공급처 찾기]·[다시 조사]**, 실행 중 작업 패널.
- 서버는 에이전트 일을 하지 않고 **에이전트를 별도 프로세스로 실행만** 한다 (슈퍼바이저 dispatch와 같은 방식):
  | 버튼 | 실행 | 실행 폴더·파이썬 |
  |---|---|---|
  | 새로 조사 / 다시 조사 | `supplier_agent.py --product "<검색어>" --no-open` | `supervisor.json`의 `discover` cwd, 현재 파이썬 |
  | 공급처 찾기 | (후보면 `set_stage → sourcing`, 사람 결정) 후 `-m sourcing_agent from-db <번호>` | `sourcing` cwd, `command[0]`의 venv 파이썬 |
  - `TRADE_DB`·`PYTHONIOENCODING=utf-8` 전달, 로그 `data/logs/<시각>_serve_<종류>.log`, 제한 시간은 설정의 `timeout_minutes`.
  - 1번 `--product` 모드: 지정 품목 하나만 평가해 `products`(origin=`manual`)·`product_scores`에 기록.
    `agent_runs.args_json = {"product": "..."}`, 리포트 폴더 `reports/<시각>_product`. 마지막에 `RESULT_PRODUCT_ID=<번호>` 출력.
- **슈퍼바이저와 겹치지 않게**: 같은 에이전트가 `agent_runs.status='running'`(제한 시간 이내)이면 서버는 실행을 거부(409).
  반대로 서버가 띄운 실행도 각 에이전트가 `agent_runs`에 남기므로 슈퍼바이저의 "실행 중이면 건너뜀"이 그대로 적용된다.
- 서버가 바꾸는 DB 값은 **[공급처 찾기]의 candidate → sourcing 단계 이동 하나뿐** (`product_events` note `"사람이 선정 → 공급처 발굴로 (대시보드)"`, run_id NULL = 사람).
- 보안: 127.0.0.1에만 바인딩, Host 헤더 확인, 실행(POST)은 `X-Trade-Hub: 1` 헤더 필수, `/file`은 `data/images`·1번 `reports`·2번 `output`만.
- 참고 이미지는 1시간마다 또는 사진 없는 품목 목록이 바뀔 때만 다시 검색.

### 7.7 3번(negotiation) 연결 — `../negotiation_agent` (2026-11-17)
- 스키마 **v7**: `messages.language`·`translation`(한국어 번역)·`analysis_json` 추가 (컬럼 추가만).
  받은 답장의 `analysis_json IS NULL` = 아직 분석 안 함, `{"error":...}` = 분석 실패(자동 재시도 안 함 → 대시보드 [다시 분석]).
- 명령 (`cwd ../negotiation_agent`, `{python}`): `python -m negotiation_agent rfq|reply|analyze|counter|followup|auto|mark ...`
- **슈퍼바이저**: `negotiation` 켜짐, `auto` = 분석 안 된 답장 분석 + 후속 메일 초안(보낸 뒤 3일 무응답, 메일당 최대 2회).
  `when_sql`은 `negotiation_agent/negotiation_agent/hub.py FOLLOWUP_DUE_SQL`과 같은 조건 — 바꾸면 같이 바꿀 것. RFQ·역제안은 자동으로 만들지 않음(사람이 버튼).
- **대시보드 서버** `POST /api/neg {action, ...}` → `server.neg_args`가 값을 검증해 `--옵션=값` 인자로 바꾸고 3번을 실행만 한다.
  답장 붙여넣기는 `data/inbox/*.txt`로 넘기고 3번이 저장 후 지움. 출력 `RESULT_ANCHOR=ps-<연결번호>` → 끝나면 그 공급처로 이동.
  같은 대상·같은 작업이 이 서버에서 실행 중일 때만 거절(다른 공급처 작업은 동시에 가능).
- 3번이 쓰는 것: `messages`, `quotes`, `tasks`(send_message·review_quote), `product_suppliers.status`(candidate→contacted→quoted, 앞으로만).
  사람이 첫 메일을 '발송 완료'로 표시하면 `products.stage` sourcing → negotiating (`product_events.run_id` NULL = 사람).
  '발송 완료'·'버림'·'검토 완료' 표시는 LLM 없이 `mark`로 처리하고 `agent_runs`에 남기지 않는다.
- 내부 상한가는 공용 DB에 없다 (`negotiation_agent/data/private.db`). 검토관 스냅샷에 넣지 말 것.
- 검토관이 참고할 만한 것: 대기 중인 `send_message` 할 일 = 사람이 아직 안 보낸 초안, `review_quote` = 받은 견적.
