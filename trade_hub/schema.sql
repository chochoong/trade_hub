-- =============================================================================
-- 1인 무역 에이전트 공용 DB (SQLite)
--
-- 에이전트 (agent_runs.agent 값)
--  discover    1. 시장조사·아이템 발굴      → products(candidate), product_scores
--  sourcing    2. 공급처 발굴              → suppliers, product_suppliers
--  compliance  3. 통관·규제 체크            → compliance_checks(supplier_questions 포함), tasks(decision: 보류 판정)
--                                          (products.hs_code·duty_rate·fta_note는 관세사 확인 후 사람이 입력 — 에이전트는 후보만)
--  costing     4. 원가·수익 계산            → product_suppliers.est_landed_krw·est_margin_pct
--  negotiation 5. 커뮤니케이션·협상         → messages(초안), quotes, product_suppliers.supplier_specs_json
--  documents   6. 무역서류                 → (PI·CI·PL·계약서 초안: 파일 경로를 tasks/notes에)
--  sales       7. 판매·마케팅              → sales
--  finance     8. 재무·관리                → orders, shipments, ledger_entries
--  supervisor  슈퍼바이저 (스케줄러 + 검토관) → supervisor_dispatches, briefings, tasks(decision)
--
-- 원칙
--  * 각 에이전트는 실행할 때 agent_runs에 자기 이름으로 1행을 남긴다 (슈퍼바이저가 상태를 판단하는 근거).
--  * 단계 넘기기: candidate → sourcing 은 사람이 결정한다 (trade_hub select <번호>).
--  * 에이전트끼리 직접 호출하지 않는다. 각 에이전트는 자기 단계의 행을 읽고 결과를 기록한다.
--  * 품목(products) 하나가 단계(stage)를 따라 이동한다. 단계 변경은 product_events에 남긴다.
--  * 메일·결제처럼 되돌리기 어려운 일은 에이전트가 초안/할 일(tasks)만 만들고, 사람이 처리한다.
--  * 금액은 원화 정수(_krw) 또는 달러 실수(_usd), 날짜·시각은 ISO 8601 문자열.
--  * JSON은 TEXT 컬럼에 저장한다 (json_valid로 검사).
-- =============================================================================
PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 에이전트 실행 기록: 모든 에이전트 실행 1회 = 1행. 다른 테이블의 run_id가 출처를 가리킨다.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id            INTEGER PRIMARY KEY,
    agent         TEXT NOT NULL CHECK (agent IN ('discover', 'sourcing', 'negotiation', 'documents', 'compliance', 'costing', 'sales', 'finance', 'supervisor')),
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    provider      TEXT,                       -- anthropic / openrouter
    model         TEXT,
    args_json     TEXT CHECK (args_json IS NULL OR json_valid(args_json)),   -- 실행 옵션 (hint, count, usd_krw ...)
    report_dir    TEXT,                       -- reports/... 폴더 (HTML 리포트 위치)
    error         TEXT,
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    finished_at   TEXT
);

-- -----------------------------------------------------------------------------
-- 품목 = 딜 단위. 같은 품목명을 다시 조사해도 한 행을 갱신한다.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    category           TEXT,
    stage              TEXT NOT NULL DEFAULT 'candidate' CHECK (stage IN (
                           'candidate',    -- 후보 (discover 점수표)
                           'sourcing',     -- 공급처 조사 중/완료
                           'negotiating',  -- RFQ·견적 협상
                           'sampling',     -- 샘플 주문·검수
                           'ordered',      -- 본 발주
                           'shipping',     -- 운송·통관
                           'selling',      -- 판매 중
                           'closed',      -- 종료 (재고 소진 등)
                           'dropped')),    -- 중단 (사유는 product_events.note)
    origin             TEXT CHECK (origin IN ('discover', 'auto', 'manual')),
    search_keyword_en  TEXT,
    keywords_ko_json   TEXT CHECK (keywords_ko_json IS NULL OR json_valid(keywords_ko_json)),  -- ["주방 벽선반", ...]
    reason             TEXT,                  -- 선정 근거
    regulations        TEXT,                  -- 필요 인증·규제
    risk               TEXT,
    hs_code            TEXT,
    duty_rate          REAL CHECK (duty_rate IS NULL OR duty_rate BETWEEN 0 AND 1),
    fta_note           TEXT,
    est_retail_krw     INTEGER,               -- 국내 판매가 추정
    est_shipping_usd   REAL,                  -- 개당 국제운송비 추정
    ref_url            TEXT,                  -- 참고 페이지 (1번이 조사 중 본 상품·기사 링크. 사진 아님)  [v2]
    image_url          TEXT,                  -- 확정 대표 사진: 직접 업로드했거나 공급사 사진 중 고른 것만  [v2]
                                              --   http(s) URL 또는 DB 폴더 기준 상대경로(images/16.jpg)
                                              --   검색으로 찾은 '참고 이미지'는 저장하지 않는다 (화면에만 표시)
    source_country     TEXT,                  -- 추천 공급 국가 (China/Japan = 2번 --country 값). 1번이 추천, 2번 기본값  [v10]
    source_country_reason TEXT,               -- 그 나라를 추천한 이유 (1번)  [v10]
    source_page_url    TEXT,                  -- 1번이 조사 중 본 해외 구매처 상품 상세 페이지 1개 (알리바바 product-detail,
                                              --   1688 offer, AliExpress item, 라쿠텐·eBay 상품 등). 2번이 그 판매자를 첫 후보로 검증  [v12]
    notes              TEXT,                  -- 사람이 적는 메모
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- 단계 이동·주요 사건 이력 (대시보드 타임라인)
CREATE TABLE IF NOT EXISTS product_events (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES agent_runs(id),       -- 사람이 바꾸면 NULL
    from_stage  TEXT,
    to_stage    TEXT,
    note        TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- discover 평가 결과 (실행마다 1행 → 점수 변화 추적 가능)
CREATE TABLE IF NOT EXISTS product_scores (
    id               INTEGER PRIMARY KEY,
    product_id       INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    run_id           INTEGER REFERENCES agent_runs(id),
    demand           REAL CHECK (demand BETWEEN 1 AND 5),
    margin           REAL CHECK (margin BETWEEN 1 AND 5),
    competition      REAL CHECK (competition BETWEEN 1 AND 5),
    regulation       REAL CHECK (regulation BETWEEN 1 AND 5),
    logistics        REAL CHECK (logistics BETWEEN 1 AND 5),
    total            INTEGER,                 -- 100점 만점 (스크립트 계산)
    score_notes      TEXT,
    est_fob_usd      REAL,
    est_margin_pct   REAL,
    naver_json       TEXT CHECK (naver_json IS NULL OR json_valid(naver_json)),  -- 검색량지수·성장률
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- -----------------------------------------------------------------------------
-- 공급처 (여러 품목에 재사용 가능)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppliers (
    id                 INTEGER PRIMARY KEY,
    company            TEXT NOT NULL,
    country            TEXT,
    type               TEXT,                  -- 제조사 / 무역상 / 미확인
    years_in_business  REAL,
    certifications     TEXT,
    platform_badge     TEXT,
    url                TEXT,
    contact_name       TEXT,
    contact_email      TEXT,
    contact_other      TEXT,                  -- WeChat, WhatsApp, Alibaba 채팅 등
    trust              TEXT CHECK (trust IN ('상', '중', '하')),
    trust_reason       TEXT,
    red_flags          TEXT,
    notes              TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (company, country)
);

-- 품목 × 공급처 후보 (소싱 에이전트가 만들고, 협상이 진행되며 status가 바뀜)
CREATE TABLE IF NOT EXISTS product_suppliers (
    id               INTEGER PRIMARY KEY,
    product_id       INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    supplier_id      INTEGER NOT NULL REFERENCES suppliers(id),
    run_id           INTEGER REFERENCES agent_runs(id),
    status           TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN (
                         'candidate', 'contacted', 'quoted', 'sampling', 'selected', 'rejected')),
    is_top_pick      INTEGER NOT NULL DEFAULT 0 CHECK (is_top_pick IN (0, 1)),
    moq              TEXT,                    -- 단위 포함 원문 (예: "200 pcs")
    unit_price_usd   REAL,                    -- 웹에서 확인한 FOB 단가 (견적 전)
    price_note       TEXT,
    est_landed_krw   INTEGER,                 -- 스크립트가 계산한 개당 원가
    est_margin_pct   REAL,
    image_url        TEXT,                    -- 이 공급사가 제공한 상품 사진 (URL 또는 상대경로)  [v3]
    -- 2번이 '이 품목 기준으로' 평가한 결과 (같은 업체라도 품목마다 다름)  [v4]
    score                 REAL,               -- 총점 (100점 만점, 위험 감점 반영)
    grade                 TEXT CHECK (grade IS NULL OR grade IN ('A', 'B', 'C', 'D')),
    score_detail_json     TEXT CHECK (score_detail_json IS NULL OR json_valid(score_detail_json)),   -- {"moq_fit":10, ..., "risk_penalty":-4}
    purchase_mode         TEXT,               -- 조사 때 구매 방식: retail / small_batch / oem
    max_order_qty         INTEGER,            -- 첫 구매 최대 수량 (MOQ 판단 기준) [v5]
                                              --   비어 있으면 구매 방식 기본값: retail 10 · small_batch 50 · oem 제한 없음 · 모름 50
    trade_terms_json      TEXT CHECK (trade_terms_json IS NULL OR json_valid(trade_terms_json)),     -- 웹에서 본 조건 {"lead_time","payment_terms","sample","export_markets"}
                                              --   업체가 답장으로 확정한 조건은 여기가 아니라 quotes에
    first_questions_json  TEXT CHECK (first_questions_json IS NULL OR json_valid(first_questions_json)),  -- 첫 문의 때 물어볼 질문 ["...", ...]
    unverified_json       TEXT CHECK (unverified_json IS NULL OR json_valid(unverified_json)),       -- 확인하지 못한 항목 ["...", ...]
    evidence_json         TEXT CHECK (evidence_json IS NULL OR json_valid(evidence_json)),           -- 근거 [{"claim":"...","url":"..."}]
    specs_json            TEXT CHECK (specs_json IS NULL OR json_valid(specs_json)),                 -- 제품 사양 (2번이 쓰고 3번이 읽음)  [v13]
                                              --   {"flags": {"power_source": "battery_lithium", "wireless": true, ...},
                                              --    "materials": ["ABS", ...], "evidence": [{"claim","url"}]}
                                              --   flags 키 = compliance_agent flags.py 사실 항목만: power_source, is_electronic,
                                              --   has_heating_or_motor, wireless, food_contact, chemical_product, hygiene_product,
                                              --   cosmetic, synthetic_resin_household, textile_wearable, origin_country
                                              --   (상표·어린이 대상·의료 효과는 3번 정책이 정하므로 넣지 않음)
                                              --   값: false = 페이지에서 '해당 없음'을 확인함, 모르면 null
    supplier_specs_json   TEXT CHECK (supplier_specs_json IS NULL OR json_valid(supplier_specs_json)),  -- 공급처가 답한 사양 (5번이 쓰고 3번이 읽음)  [v14]
                                              --   specs_json과 같은 형식, 근거는 URL 대신 메일 번호 {"claim","message_id"} + "updated_at"
                                              --   2번이 다시 조사하면 specs_json을 덮어쓰므로 칸을 따로 둠. 쓰기는 db.merge_supplier_specs로
    reject_reason    TEXT,
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
    -- 한 줄 = (품목, 공급처, 구매 방식) [v8]. 같은 업체가 retail·small_batch 양쪽에 나와도 따로 남는다.
    -- 고유 조건은 db.connect()가 만드는 인덱스 ux_ps_product_supplier_mode
    --   (product_id, supplier_id, COALESCE(purchase_mode, '')) — v7 이전 DB는 purchase_mode 칸이 없어 여기 못 둠
);

-- -----------------------------------------------------------------------------
-- 커뮤니케이션: 메일은 '초안'만 에이전트가 만든다. 발송은 사람이 직접 하고 sent로 표시.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                   INTEGER PRIMARY KEY,
    product_supplier_id  INTEGER NOT NULL REFERENCES product_suppliers(id) ON DELETE CASCADE,
    run_id               INTEGER REFERENCES agent_runs(id),
    direction            TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    kind                 TEXT CHECK (kind IN ('rfq', 'follow_up', 'negotiation', 'sample', 'order', 'other')),
    channel              TEXT NOT NULL DEFAULT 'email' CHECK (channel IN ('email', 'alibaba', 'wechat', 'whatsapp', 'other')),
    subject              TEXT,
    body                 TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN (
                             'draft',      -- 에이전트 초안 (outbound)
                             'approved',   -- 사람이 검토 완료, 아직 안 보냄
                             'sent',       -- 사람이 직접 발송함
                             'discarded',  -- 버림
                             'received')), -- 받은 답장 (inbound, 사람이 붙여넣음)
    reply_to_id          INTEGER REFERENCES messages(id),   -- 후속 메일 → 원래 보낸 메일
    language             TEXT,                -- en / zh ...  [v7]
    translation          TEXT,                -- 한국어 번역 (보낸 초안·받은 답장)  [v7]
    analysis_json        TEXT CHECK (analysis_json IS NULL OR json_valid(analysis_json)),   -- 5번 분석 결과  [v7]
                                              --   받은 답장: {"summary_ko","open_questions_ko",...}
                                              --     NULL = 아직 분석 안 함 (5번 auto가 처리) / {"error": ...} = 분석 실패
                                              --   보낸 초안: {"strategy_ko","tone"} — 사람 참고용, 발송 안 됨
    external_id          TEXT,                -- 메일 서비스의 메시지 id (Gmail: 보낸/받은 메일). 같은 메일 중복 저장 방지  [v9]
    external_thread_id   TEXT,                -- 메일 서비스의 대화(스레드) id — 답장을 어느 공급처 줄에 붙일지 판단  [v9]
    external_draft_id    TEXT,                -- 메일 서비스 임시보관함에 넣은 초안 id (발송은 사람이 직접)  [v9]
    followup_due_on      TEXT,                -- 보낸 메일(원래 메일): 사람이 캘린더에서 옮긴 다음 후속 메일 예정일 (YYYY-MM-DD)  [v15]
                                              --   마지막 후속 메일을 보낸 날보다 뒤일 때만 유효, 아니면 기본(보낸 지 3일)
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    sent_at              TEXT,                -- 발송/수신 시각
    CHECK ((direction = 'inbound') = (status = 'received'))
);

-- 견적: 받은 답장(inbound)에서 협상 에이전트가 추출하거나 사람이 입력
CREATE TABLE IF NOT EXISTS quotes (
    id                   INTEGER PRIMARY KEY,
    product_supplier_id  INTEGER NOT NULL REFERENCES product_suppliers(id) ON DELETE CASCADE,
    message_id           INTEGER REFERENCES messages(id),   -- 근거가 된 답장
    run_id               INTEGER REFERENCES agent_runs(id),
    unit_price_usd       REAL NOT NULL,
    min_qty              INTEGER,
    price_tiers_json     TEXT CHECK (price_tiers_json IS NULL OR json_valid(price_tiers_json)),  -- [{"qty":500,"usd":2.8}, ...]
    incoterm             TEXT CHECK (incoterm IS NULL OR incoterm IN ('EXW', 'FOB', 'CFR', 'CIF', 'DAP', 'DDP')),
    lead_time_days       INTEGER,
    payment_terms        TEXT,                -- 예: 30% 선금 / 70% 선적 전
    sample_cost_usd      REAL,
    sample_shipping_usd  REAL,
    valid_until          TEXT,
    notes                TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- -----------------------------------------------------------------------------
-- 발주·운송·통관
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                   INTEGER PRIMARY KEY,
    product_supplier_id  INTEGER NOT NULL REFERENCES product_suppliers(id),
    quote_id             INTEGER REFERENCES quotes(id),
    kind                 TEXT NOT NULL DEFAULT 'bulk' CHECK (kind IN ('sample', 'bulk')),
    qty                  INTEGER NOT NULL CHECK (qty > 0),
    unit_price_usd       REAL NOT NULL,
    total_usd            REAL NOT NULL,
    incoterm             TEXT,
    status               TEXT NOT NULL DEFAULT 'placed' CHECK (status IN (
                             'placed', 'paid_deposit', 'in_production', 'ready', 'shipped', 'received', 'cancelled')),
    ordered_at           TEXT,
    notes                TEXT
);

CREATE TABLE IF NOT EXISTS shipments (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    mode             TEXT CHECK (mode IN ('express', 'air', 'sea_lcl', 'sea_fcl')),
    forwarder        TEXT,
    tracking_no      TEXT,
    cartons          INTEGER,
    gross_weight_kg  REAL,
    cbm              REAL,
    etd              TEXT,
    eta              TEXT,
    status           TEXT NOT NULL DEFAULT 'booked' CHECK (status IN (
                         'booked', 'in_transit', 'arrived', 'customs', 'cleared', 'delivered')),
    customs_decl_no  TEXT,                    -- 수입신고번호
    cleared_at       TEXT,
    notes            TEXT
);

-- -----------------------------------------------------------------------------
-- 판매·정산
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales (
    id            INTEGER PRIMARY KEY,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    channel       TEXT NOT NULL,              -- coupang / naver / 11st / own ...
    period_start  TEXT NOT NULL,              -- 일·주·월 단위 집계 시작일
    period_end    TEXT NOT NULL,
    qty           INTEGER NOT NULL DEFAULT 0,
    revenue_krw   INTEGER NOT NULL DEFAULT 0, -- 부가세 포함 판매액
    notes         TEXT,
    UNIQUE (product_id, channel, period_start, period_end)
);

-- 모든 돈의 흐름 (정산 에이전트가 손익 계산). 수입·지출 모두 여기 한 곳에.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id           INTEGER PRIMARY KEY,
    product_id   INTEGER REFERENCES products(id),        -- 공통비면 NULL
    order_id     INTEGER REFERENCES orders(id),
    shipment_id  INTEGER REFERENCES shipments(id),
    sale_id      INTEGER REFERENCES sales(id),
    entry_date   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN (
                     'revenue',        -- 판매 수입 (+)
                     'goods',          -- 물품 대금
                     'sample',         -- 샘플 비용
                     'freight',        -- 국제운송
                     'duty',           -- 관세
                     'import_vat',     -- 수입 부가세 (매입세액 공제 대상)
                     'customs_fee',    -- 통관 수수료
                     'domestic_ship',  -- 국내 택배
                     'platform_fee',   -- 판매 수수료
                     'ad',             -- 광고
                     'certification',  -- 인증·시험
                     'bank_fee',       -- 송금·환전 수수료
                     'other')),
    amount_krw   INTEGER NOT NULL,            -- 수입은 +, 지출은 -
    amount_usd   REAL,                        -- 외화 원금 (있으면)
    fx_rate      REAL,                        -- 적용 환율
    memo         TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- -----------------------------------------------------------------------------
-- 사람이 할 일 = 대시보드의 '승인 대기함'
-- 에이전트는 실행 대신 여기에 할 일을 만든다 (메일 발송, 견적 검토, 송금, 서류 확인 등).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER REFERENCES products(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES agent_runs(id),
    kind        TEXT NOT NULL CHECK (kind IN (
                    'send_message',   -- ref: messages.id (초안 검토 후 직접 발송)
                    'review_quote',   -- ref: quotes.id
                    'payment',        -- ref: orders.id
                    'document',       -- 인보이스·패킹리스트·C/O 등 확인
                    'decision',       -- 진행/중단 판단
                    'other')),
    ref_table   TEXT,
    ref_id      INTEGER,
    title       TEXT NOT NULL,
    detail      TEXT,
    priority    INTEGER NOT NULL DEFAULT 2 CHECK (priority IN (1, 2, 3)),   -- 1 높음 · 2 보통 · 3 낮음 (검토관이 조정)
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dismissed')),
    due_date    TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    done_at     TEXT
);

-- 구매 사이트 순위 (2번): "이 품목을 어느 사이트에서 사는 게 좋은가". 실행마다 사이트별 1행 → 최신 실행분을 보여준다.
-- 점수 = 항목별 평가 × 구매 방식별 가중치 (trade_sourcing_agent/config/channels.yaml), 100점 만점  [v6]
CREATE TABLE IF NOT EXISTS product_channels (
    id             INTEGER PRIMARY KEY,
    product_id     INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    run_id         INTEGER REFERENCES agent_runs(id),
    rank           INTEGER,                   -- 그 실행 안의 순위 (1 = 추천)
    site_key       TEXT NOT NULL,             -- aliexpress / alibaba / 1688 ...
    site           TEXT NOT NULL,             -- 표시 이름 (AliExpress, Alibaba (Ready to Ship) ...)
    search_url     TEXT,                      -- 그 사이트에서 이 품목 검색 결과 링크
    total          REAL,                      -- 총점 (100점 만점)
    scores_json    TEXT CHECK (scores_json IS NULL OR json_valid(scores_json)),       -- {"small_qty":20, "price":16, ...}
    typical_price  TEXT,                      -- 소량 구매 기준 단가 범위 원문 (예: "USD 3.2-4.5")
    min_qty        TEXT,                      -- 최소 구매 수량 원문 (예: "1 piece")
    summary        TEXT,                      -- 이 사이트에서 살 때의 장단점
    site_note      TEXT,                      -- 사이트 고유 설명 (품목 무관)
    evidence_json  TEXT CHECK (evidence_json IS NULL OR json_valid(evidence_json)),   -- [{"claim":"...","url":"..."}]
    assessed       INTEGER CHECK (assessed IS NULL OR assessed IN (0, 1)),            -- 0 = 웹 평가 못 함 (사이트 고유 점수만)
    purchase_mode  TEXT,                      -- 평가 기준 구매 방식: retail / small_batch / oem
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- 통관·규제 체크 결과 (3번): 실행마다 품목별 1행 → 최신 행이 현재 판정. 규칙 엔진 판정이며 HS코드는 '후보'만  [v11]
--  판정은 발주 전 점검용 초안. HS코드·세율 확정은 관세사 확인 후 사람이 products.hs_code·duty_rate에 입력한다.
CREATE TABLE IF NOT EXISTS compliance_checks (
    id                     INTEGER PRIMARY KEY,
    product_id             INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    run_id                 INTEGER REFERENCES agent_runs(id),
    verdict                TEXT NOT NULL CHECK (verdict IN ('go', 'conditional', 'hold')),   -- 진행 / 조건부 진행 / 보류
    verdict_reasons_json   TEXT CHECK (verdict_reasons_json IS NULL OR json_valid(verdict_reasons_json)),    -- ["...", ...]
    hs_candidates_json     TEXT CHECK (hs_candidates_json IS NULL OR json_valid(hs_candidates_json)),        -- [{"code","description","reason"}]
    required_certs_json    TEXT CHECK (required_certs_json IS NULL OR json_valid(required_certs_json)),      -- [{"id","title","timing"}] 인증·요건 (공통 요건 제외)
    possible_rules_json    TEXT CHECK (possible_rules_json IS NULL OR json_valid(possible_rules_json)),      -- 정보 부족으로 판단 못 한 요건 [{"id","title","missing_flags"}]
    factory_requests_json  TEXT CHECK (factory_requests_json IS NULL OR json_valid(factory_requests_json)),  -- 공장에 요청할 서류·시안
    labeling_json          TEXT CHECK (labeling_json IS NULL OR json_valid(labeling_json)),                  -- 제품·포장 표시사항
    contract_clauses_json  TEXT CHECK (contract_clauses_json IS NULL OR json_valid(contract_clauses_json)),  -- 계약서·PI 조건 (6번 무역서류가 읽음)
    broker_questions_json  TEXT CHECK (broker_questions_json IS NULL OR json_valid(broker_questions_json)),  -- 관세사에게 물을 것
    flags_json             TEXT CHECK (flags_json IS NULL OR json_valid(flags_json)),                        -- 판정에 쓴 제품 특성 플래그
    unknown_flags_json     TEXT CHECK (unknown_flags_json IS NULL OR json_valid(unknown_flags_json)),        -- 아직 모르는 플래그 이름
    warnings_json          TEXT CHECK (warnings_json IS NULL OR json_valid(warnings_json)),                  -- 규칙 표 경고 (미검증·재확인 필요)
    supplier_questions_json TEXT CHECK (supplier_questions_json IS NULL OR json_valid(supplier_questions_json)),  -- 5번이 공급처에게 물을 사양 질문 [v14]
                                                  --   [{"flag": "chemical_product", "question_ko": "..."}] — '모름'으로 남은 사실 항목만 (정책 항목 제외)
    used_llm               INTEGER NOT NULL DEFAULT 0 CHECK (used_llm IN (0, 1)),                             -- LLM 분류로 플래그·HS 후보를 채웠는가
    report_path            TEXT,                  -- 사람이 읽는 보고서(.md) 경로
    checked_on             TEXT NOT NULL,         -- 판정 기준일 (YYYY-MM-DD)
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- 슈퍼바이저 스케줄러가 다른 에이전트를 실행한 기록 (supervisor.json의 명령을 subprocess로 실행)
CREATE TABLE IF NOT EXISTS supervisor_dispatches (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER REFERENCES agent_runs(id),          -- 이 실행을 결정한 supervisor 실행
    agent        TEXT NOT NULL,                              -- supervisor.json의 에이전트 키
    reason       TEXT,                                       -- 실행 사유 (주기 도래 / 처리할 일 N건)
    command      TEXT NOT NULL,
    exit_code    INTEGER,                                    -- NULL = 실행 중 또는 비정상 종료
    log_path     TEXT,
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    finished_at  TEXT
);

-- 슈퍼바이저 검토관(LLM, 읽기 전용)의 브리핑. 대시보드 첫 화면 상단에 최신 1건을 보여준다.
CREATE TABLE IF NOT EXISTS briefings (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER REFERENCES agent_runs(id),
    summary           TEXT NOT NULL,          -- 오늘의 요약 (마크다운)
    alerts_json       TEXT CHECK (alerts_json IS NULL OR json_valid(alerts_json)),   -- [{"product_id":1,"level":"warn","text":"..."}]
    source_runs_json  TEXT CHECK (source_runs_json IS NULL OR json_valid(source_runs_json)),  -- 근거로 삼은 run_id 목록
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- -----------------------------------------------------------------------------
-- 인덱스
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_products_stage        ON products(stage);
CREATE INDEX IF NOT EXISTS idx_events_product        ON product_events(product_id, at);
CREATE INDEX IF NOT EXISTS idx_scores_product        ON product_scores(product_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ps_product            ON product_suppliers(product_id, status);
CREATE INDEX IF NOT EXISTS idx_channels_product      ON product_channels(product_id, run_id, rank);
CREATE INDEX IF NOT EXISTS idx_compliance_product    ON compliance_checks(product_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_ps           ON messages(product_supplier_id, status);
CREATE INDEX IF NOT EXISTS idx_quotes_ps             ON quotes(product_supplier_id);
CREATE INDEX IF NOT EXISTS idx_shipments_status      ON shipments(status);
CREATE INDEX IF NOT EXISTS idx_ledger_product_date   ON ledger_entries(product_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status, priority, due_date);
CREATE INDEX IF NOT EXISTS idx_briefings_created     ON briefings(created_at);
CREATE INDEX IF NOT EXISTS idx_dispatches_agent     ON supervisor_dispatches(agent, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_agent           ON agent_runs(agent, started_at);

-- -----------------------------------------------------------------------------
-- 대시보드용 뷰: 품목별 손익 (ledger 합계, 부가세 제외 기준)
--  revenue는 부가세 포함으로 기록하므로 /1.1, 수입 부가세는 매입세액 공제 대상이라 비용에서 제외.
--  비용 항목은 부가세가 따로 붙지 않는 금액으로 기록한다고 가정 (수수료 등 부가세는 별도 정산).
-- -----------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_product_pnl AS
SELECT product_id, name, stage, revenue_krw,
       CAST(ROUND(revenue_krw / 1.1) AS INTEGER)                AS revenue_ex_vat_krw,
       cost_krw, import_vat_krw,
       CAST(ROUND(revenue_krw / 1.1) AS INTEGER) + cost_krw      AS profit_krw
FROM (
    SELECT p.id AS product_id, p.name, p.stage,
           COALESCE(SUM(CASE WHEN l.kind = 'revenue' THEN l.amount_krw END), 0)                     AS revenue_krw,
           COALESCE(SUM(CASE WHEN l.kind NOT IN ('revenue', 'import_vat') THEN l.amount_krw END), 0) AS cost_krw,
           COALESCE(SUM(CASE WHEN l.kind = 'import_vat' THEN l.amount_krw END), 0)                  AS import_vat_krw
    FROM products p LEFT JOIN ledger_entries l ON l.product_id = p.id
    GROUP BY p.id
);
