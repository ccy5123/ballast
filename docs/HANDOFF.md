# ballast — 세션 인계 문서 (HANDOFF)

> 목적: 새 세션(새 ephemeral 컨테이너)에서 이 프로젝트를 **끊김 없이 이어받기** 위한 단일 진입 문서.
> 작성 기준일: 2026-06-26 · 브랜치: `claude/lucid-volta-a42se7`
> 이 문서가 최신 상태와 다르면 `git log`와 `.moai/specs/`가 우선이다.

---

## 0. 30초 요약 / 재개 방법

- **무엇**: 레버리지 ETF 전략 봇 (VR 밸류 리밸런싱 + MAB 무한매수법). moai-adk SPEC-First DDD로 개발.
- **어디까지**: **P0(전략 코어 + 백테스트) ✅ + P1(토스 read-only 어댑터) ✅ + P2(Order Manager + dry-run) ✅**. 라이브 실행(P3)은 아직.
- **다음 할 일**: **P3 — 라이브 실행(토스 write 어댑터 SPEC-ADAPTER-002)** (아래 §3). 또는 `/moai sync`로 문서화 먼저.
- **재개 한 줄**: 새 세션에서 이 브랜치를 받고 → §2로 환경 복구 → §3대로 P3 진행.

> ⚠️ 새 컨테이너는 ephemeral이다. `.moai/memory/`·`.venv/`는 **gitignore라 매번 사라진다(정상)**. 실제 작업물은 전부 git + PR #3에 있다. 손실 없음.

---

## 1. 현재 상태 (무엇이 끝났나)

### 로드맵

| 단계   | 내용                                                                                  | 상태      |
| ------ | ------------------------------------------------------------------------------------- | --------- |
| P0     | CORE (도메인 + instrument registry + 가드레일 + Config)                               | ✅        |
| P0     | VR 전략 코어 (`next_value`/`rebalance_decision`/`order_from_decision` + `VRStrategy`) | ✅        |
| P0     | MAB 전략 코어 (LOC 분할매수/익절/쿼터매도 + `MABStrategy`)                            | ✅        |
| P0     | 백테스트 엔진 + 비용모델(양도세 22%) + 지표 + 베이스라인                              | ✅        |
| P1     | 토스 어댑터 **read-only** (OAuth2 + 시세 + 계좌/자산 + 사전검증 + 체결조회)           | ✅        |
| P1+    | 토스 **env 크리덴셜 부트스트랩** (`from_env` + `.env.example` + smoke + `py.typed`)   | ✅        |
| P0+    | BACKTEST-002 (파라미터 스윕 + walk-forward + 레짐분할 + 고원 평가)                    | ✅        |
| **P2** | **Order Manager + dry-run** (의사결정→주문 변환·멱등·주문안 로깅)                     | ✅        |
| **P3** | **라이브 실행** (토스 write 어댑터 SPEC-ADAPTER-002, VR 예약지정가 / MAB LOC=`CLS`)   | ⬜ ← 다음 |
| 보류   | 데이터 fetcher·State Store·Scheduler·Notifier·CLI                                     | ⬜        |

### 구현된 SPEC (7) — `.moai/specs/`

- **SPEC-CORE-001** — 공유 도메인 레이어 (순수, Decimal, IO 없음)
- **SPEC-VR-001** — VR 밸류 리밸런싱 순수함수
- **SPEC-MAB-001** — MAB 무한매수법 순수함수
- **SPEC-BACKTEST-001** — 백테스트 엔진/체결/비용/지표/베이스라인
- **SPEC-BACKTEST-002** — 파라미터 스윕 + walk-forward + 레짐분할(상승/하락/횡보) + 강건한 고원 평가
- **SPEC-ADAPTER-001** — 토스 read-only 어댑터 (실 OpenAPI `docs/reference/toss-openapi.json` v1.1.5 근거). PR #2 머지로 **env 크리덴셜 부트스트랩**(`from_env` + `.env.example` + `scripts/toss_smoke.py` + `py.typed`)이 추가됨 — 실 토스 Open API로 라이브 검증 완료.
- **SPEC-ORDER-001** — 브로커-중립 Order Manager + dry-run (의사결정→`OrderIntent` 변환·결정적 멱등키·주문안 기록). 패키지 `src/ballast/orders/`(models/ports/mapping/guards/manager/recording).

### 코드/품질 현황 (마지막 검증 시점)

- 소스: `src/ballast/{core,backtest,adapters,orders}/` · 테스트: `tests/unit/{core,backtest,adapters,orders}/`
- **테스트 319 passed, 커버리지 100%**, `ruff` 0, `ruff format` clean, `mypy --strict` 0 (33 source files)
- 마지막 커밋: `f07deed feat(orders): implement SPEC-ORDER-001 Order Manager + dry-run (TDD)`
- **Draft PR #3**: https://github.com/ccy5123/ballast/pull/3 (PR #1·#2를 누적으로 대체하는 트렁크; 본문에 동일한 상태 요약 있음)

---

## 2. 환경 복구 (새 컨테이너에서 제일 먼저)

`.venv/`·`.moai/memory/`는 gitignore라 새 컨테이너엔 없다. `pyproject.toml`+`uv.lock`이 의존성을 전부 선언하므로 복원만 하면 된다.

```bash
cd /home/user/ballast            # 또는 클론 위치
# 의존성 설치 (코어+백테스트+어댑터 전부)
pip install -e ".[dev,backtest]"   # uv가 있으면 uv sync 도 가능
```

> 참고: 직전 세션에선 서브에이전트가 **system python**에 설치했다(.venv 아님). 어느 쪽이든 무방 — 게이트 실행 시 deps가 있는 인터프리터로 돌리면 된다.

### 품질 게이트 (P3 시작 전 베이스라인 그린 확인)

```bash
ruff check .
ruff format --check .
mypy --strict src
python3 -m pytest --cov=src/ballast --cov-report=term-missing
# 기대: All checks passed / Success / 319 passed, 100% (현 시점 기준)
```

---

## 3. 다음 작업 — P3: 라이브 실행 (토스 write 어댑터 SPEC-ADAPTER-002 초안)

**핵심 원칙**: P2까지는 **실제 주문 제출이 없다**(dry-run). P3에서 비로소 **토스 write 어댑터**가 `BrokerOrderPort`를 구현해 실주문을 낸다. Order Manager(P2)는 이미 브로커-중립이라, P3는 포트 구현체 하나를 추가하는 일이다.

### 범위 (토스 write 어댑터)

- **`BrokerOrderPort` 구현체** — ADAPTER-001의 read 어댑터 옆에 write 경로 추가. `place_order(intent)` / `cancel_order(...)` 구현.
- **엔드포인트**: `POST /api/v1/orders`(생성) · `/modify`(정정) · `/cancel`(취소). 출처: `docs/reference/toss-openapi.json` `POST /api/v1/orders`.
- **매핑**: P2의 `OrderIntent(kind=LIMIT, tif=CLS)` → 토스 **LOC**(`orderType=LIMIT` + `timeInForce=CLS`, 미국주식 한정). `OrderIntent(kind=LIMIT, tif=DAY)` → 토스 지정가.
- **멱등키 패스스루**: P2가 만든 결정적 `client_order_id`를 토스 `clientOrderId`로 **그대로 전달**(이미 `^[a-zA-Z0-9\-_]+$`, ≤36자 제약 충족, 토스 규약상 유효 ~10분).
- **라이브 경로**: `execution.dry_run=False`일 때만 활성. dry_run 게이트·킬스위치·`max_position_pct` 클램프(P2 가드)는 그대로 상위에서 적용.
- **소액 소크(soak)**: 1주 단위 실주문/취소로 라이브 검증 — **사용자 로컬에서만** (원격 컨테이너엔 키 없음).

### EARS 모듈 초안 (≤5)

- R1 `BrokerOrderPort` 구현 토스 write 어댑터 + `OrderIntent`→토스 페이로드 매핑(LOC=`LIMIT`+`CLS`, 지정가=`LIMIT`+`DAY`)
- R2 `place_order`: `POST /api/v1/orders`, `client_order_id`→`clientOrderId` 패스스루, 응답→제출결과 매핑
- R3 `cancel_order` / 정정: `/cancel`·`/modify` 경유, 멱등 dedup 유지
- R4 오류/재시도: 토스 에러코드→도메인 결과 매핑, 멱등 재제출 안전성
- R5 소액 소크 점검표 + (로컬) 실주문 검증 절차

### 의존성 / 근거

- P2(SPEC-ORDER-001)의 `OrderIntent`/`BrokerOrderPort`/`client_order_id`를 재사용 — write 어댑터는 그 계약의 구현체.
- CORE-001 `Order`/`Config`, ADAPTER-001의 OAuth2/HTTP 인프라 재사용. **토스 주문 생성 사실(이미 확인)**: LOC = `timeInForce=CLS` + `orderType=LIMIT`(미국주식 한정), 멱등키 `clientOrderId`(10분). 출처: `docs/reference/toss-openapi.json` `POST /api/v1/orders`.

### 진행 방식 (moai)

1. `manager-spec` 위임 → `.moai/specs/SPEC-ADAPTER-002/{spec,plan,acceptance}.md` (영어, EARS, frontmatter)
2. 검토 후 `manager-ddd` 위임 → TDD(RED→GREEN→REFACTOR), `respx` mock 토스 API로 검증(실 네트워크 없이)
3. 게이트 재검증 → 커밋 → 푸시 (PR #3에 반영) → 라이브 소액 소크는 사용자 로컬

---

## 4. 라이브 전 정리할 follow-up 2건

- 🔸 **VR `V_n` 다주기 미진화**: `Strategy.plan_orders`가 주문만 반환 → 백테스트 엔진이 재계산된 가치선 `V_n`을 관측 못 함 → **다주기 VR 백테스트가 충실하지 않음**. `Strategy` 계약 보강 필요(상태 델타 반환 / `advance_state` / 엔진이 `next_value` 직접 호출 중 택1). MAB는 무관(상태가 체결에서 파생).
- 🔸 **MAB 쿼터매도 LOC 가격**: `mab_on_seed_exhausted`가 `OrderType.LOC` + `limit_price=None`(사실상 MOC). 백테스트는 종가 체결로 OK지만 **라이브 경로는 쿼터매도 가격 정의 또는 MOC 채택 필요**.

---

## 5. 보류 백로그 (P3 이후 후보)

- **SPEC-ADAPTER-002 (P3, 다음 작업)** — 토스 **주문 write** 어댑터: `POST /api/v1/orders`(생성, LOC=`CLS` 매핑)·`/modify`·`/cancel`. `BrokerOrderPort` 구현체. (상세는 §3)
- **데이터 fetcher** — yfinance/CSV 어댑터로 실데이터 OHLC+FX 주입 → 실제 비교표(CAGR/MDD/세금드래그) 산출.
- **State Store** — 전략별 namespace(vr/mab) 상태 영속(V_n·평단·시드·qty).
- **Scheduler** — MAB 매일 / VR 사이클 트리거, DST-aware.
- **Reconciliation** — 체결 대조 → 상태 갱신.
- **Notifier** — ntfy/Discord 알림.
- **CLI** — `backtest`/`dry-run`/`live` 진입점.

---

## 6. moai 워크플로우로 재개하는 법

- 이 브랜치를 클론하면 `.claude/`(스킬·에이전트)·`CLAUDE.md`가 자동 로드된다.
- 권한: `.claude/settings.local.json`에 `defaultMode: acceptEdits` 설정됨(편집 자동승인; 위험 bash는 계속 확인).
- 모드: moai personal / git `manual`(현 브랜치에 직접 커밋, 강제 PR 없음).
- 흐름: `/moai plan "..."`(manager-spec) → 검토 → `/moai run SPEC-XXX`(manager-ddd, TDD) → `/moai sync`(manager-docs).
- 게이트: 모든 구현은 `ruff` 0 · `mypy --strict` 0 · 커버리지 ≥85%(코어는 사실상 100%).

---

## 7. 핵심 파일/레퍼런스 위치

- 프로젝트 DNA: `.moai/project/{product,structure,tech}.md` (tech.md에 Constitution)
- SPEC: `.moai/specs/SPEC-{CORE,VR,MAB}-001/`, `SPEC-BACKTEST-{001,002}/`, `SPEC-ADAPTER-001/`, `SPEC-ORDER-001/`
- 소스: `src/ballast/core/` (순수), `src/ballast/backtest/` (pandas/numpy 허용), `src/ballast/adapters/` (IO 경계), `src/ballast/orders/` (Order Manager, dry-run-first)
- **토스 API 진실의 원천**: `docs/reference/toss-openapi.json` (OpenAPI 3.1.0 v1.1.5)
- 설정: `.moai/config/sections/*.yaml`, `pyproject.toml`(deps·ruff·mypy·pytest)

---

## 8. 결정·관례 메모 (Constitution 요약)

- 코어(`src/ballast/core/`)는 **순수**: IO·네트워크·시계(`datetime.now`) 금지 → 시각·가격은 인자로 주입.
- 금액·수량은 **`Decimal`만**(절대 `float` 금지), 반올림 소수점 2자리. 백테스트의 곡선/비율 계산만 numpy/float 허용(머니 장부와 분리).
- 종목·레버리지 하드코딩 금지 → instrument registry 경유. 비레버리지/1x는 가드레일이 경고/차단.
- 전략 격리 = **별도 계좌**(`account_seq`)가 v1 기본.
- 시크릿(`TOSS_CLIENT_ID/SECRET`)은 **env var만**, 로그·repr 마스킹. 토큰은 메모리만.
- **실계좌 검증(1주 조회/주문)은 사용자 로컬에서** 수행 — 원격 컨테이너엔 키 없음, 샌드박스 없음.

---

_이 문서는 인계용 스냅샷이다. 작업이 진행되면 갱신하거나, PR #3 본문/`.moai/specs/`를 최신 출처로 삼을 것._
