# ballast — 세션 인계 문서 (HANDOFF)

> 목적: 새 세션(새 ephemeral 컨테이너)에서 이 프로젝트를 **끊김 없이 이어받기** 위한 단일 진입 문서.
> 작성 기준일: 2026-06-26 · 브랜치: `claude/eager-johnson-7zk3nu`
> 이 문서가 최신 상태와 다르면 `git log`와 `.moai/specs/`가 우선이다.

---

## 0. 30초 요약 / 재개 방법

- **무엇**: 레버리지 ETF 전략 봇 (VR 밸류 리밸런싱 + MAB 무한매수법). moai-adk SPEC-First DDD로 개발.
- **어디까지**: **P0(전략 코어 + 백테스트) ✅ + P1(토스 read-only 어댑터) ✅**. 라이브 실행(P2/P3)은 아직.
- **다음 할 일**: **P2 — Order Manager + dry-run** (아래 §3). 또는 `/moai sync`로 문서화 먼저.
- **재개 한 줄**: 새 세션에서 이 브랜치를 받고 → §2로 환경 복구 → §3대로 P2 진행.

> ⚠️ 새 컨테이너는 ephemeral이다. `.moai/memory/`·`.venv/`는 **gitignore라 매번 사라진다(정상)**. 실제 작업물은 전부 git + PR #1에 있다. 손실 없음.

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
| **P2** | **Order Manager + dry-run** (의사결정→주문 변환·멱등·주문안 로깅)                     | ⬜ ← 다음 |
| P3     | 라이브 실행 (VR 예약지정가 / MAB LOC=`CLS`), 소액 소크                                | ⬜        |
| 보류   | BACKTEST-002(스윕)·데이터 fetcher·State Store·Scheduler·Notifier·CLI                  | ⬜        |

### 구현된 SPEC (5) — `.moai/specs/`

- **SPEC-CORE-001** — 공유 도메인 레이어 (순수, Decimal, IO 없음)
- **SPEC-VR-001** — VR 밸류 리밸런싱 순수함수
- **SPEC-MAB-001** — MAB 무한매수법 순수함수
- **SPEC-BACKTEST-001** — 백테스트 엔진/체결/비용/지표/베이스라인
- **SPEC-ADAPTER-001** — 토스 read-only 어댑터 (실 OpenAPI `docs/reference/toss-openapi.json` v1.1.5 근거)

### 코드/품질 현황 (마지막 검증 시점)

- 소스: `src/ballast/{core,backtest,adapters}/` · 테스트: `tests/unit/{core,backtest,adapters}/`
- **테스트 223 passed, 커버리지 100%**, `ruff` 0, `mypy --strict` 0 (24 source files)
- 마지막 커밋: `8f838f0 feat(adapters): implement SPEC-ADAPTER-001 ...`
- **Draft PR #1**: https://github.com/ccy5123/ballast/pull/1 (본문에 동일한 상태 요약 있음)

---

## 2. 환경 복구 (새 컨테이너에서 제일 먼저)

`.venv/`·`.moai/memory/`는 gitignore라 새 컨테이너엔 없다. `pyproject.toml`+`uv.lock`이 의존성을 전부 선언하므로 복원만 하면 된다.

```bash
cd /home/user/ballast            # 또는 클론 위치
# 의존성 설치 (코어+백테스트+어댑터 전부)
pip install -e ".[dev,backtest]"   # uv가 있으면 uv sync 도 가능
```

> 참고: 직전 세션에선 서브에이전트가 **system python**에 설치했다(.venv 아님). 어느 쪽이든 무방 — 게이트 실행 시 deps가 있는 인터프리터로 돌리면 된다.

### 품질 게이트 (P2 시작 전 베이스라인 그린 확인)

```bash
ruff check .
ruff format --check .
mypy --strict src
python3 -m pytest --cov=src/ballast --cov-report=term-missing
# 기대: All checks passed / Success / 223 passed, 100% (현 시점 기준)
```

---

## 3. 다음 작업 — P2: Order Manager + dry-run (SPEC-ORDER-001 초안)

**핵심 원칙**: P2는 **실제 주문 제출을 하지 않는다**(dry_run). 따라서 토스 write 어댑터(`POST /orders`)는 **불필요**하며 P3로 미룬다. Order Manager는 **브로커-중립**으로 만든다.

### 범위 (브로커-중립)

- CORE `Order`(side/ticker/qty/limit_price/order_type/account_seq) → 브로커-중립 `OrderIntent`로 변환
  - `reserved_limit` → 지정가, `tif=DAY`
  - `LOC` → 지정가, `tif=CLS` (Limit-On-Close)
  - `market` → 시장가 (두 전략 기본 미사용)
- **멱등키 `client_order_id`** 생성 — 결정적(전략 ns + 날짜/사이클 + 주문 시그니처). 토스 규약상 유효 ~10분.
- **`dry_run` 기본 ON** (config `execution.dry_run`): 제출 대신 `OrderIntent`/주문안을 기록·로깅하고 반환. 실제 호출 없음.
- 라이브 제출 경로(`dry_run=False`): `BrokerOrderPort.place_order(intent)` 호출, 멱등 dedup, 결과 추적.
- **안전 가드**: 전역 킬스위치, `max_position_pct` 클램프, dry_run 게이트.
- 테스트용 **In-Memory/Recording 포트** 구현 (실 네트워크 없이 검증).

### EARS 모듈 초안 (≤5)

- R1 `OrderIntent` 모델 + `BrokerOrderPort`(place/cancel) Protocol + CORE `Order`→`OrderIntent` 매핑(타입→tif, client_order_id 멱등)
- R2 dry-run: 기본 ON, 주문안 기록만, 제출 없음
- R3 라이브 제출: 포트 경유, 멱등 dedup, 결과/상태 추적
- R4 안전 가드: 킬스위치, `max_position_pct`, dry_run 게이트(위반/초과 시 차단·클램프)
- R5 Recording/In-Memory 포트 + (경량) 제출결과 추적

### 의존성 / 근거

- CORE-001 `Order`/`Config` 재사용. ADAPTER-001의 read 포트는 잔고·예수금·평단 조회에 사용.
- **토스 주문 생성 사실(이미 확인, P3 매핑용)**: LOC = `timeInForce=CLS` + `orderType=LIMIT` (미국주식 한정), 멱등키 `clientOrderId`(10분). 출처: `docs/reference/toss-openapi.json` `POST /api/v1/orders`.

### 진행 방식 (moai)

1. `manager-spec` 위임 → `.moai/specs/SPEC-ORDER-001/{spec,plan,acceptance}.md` (영어, EARS, 8필드 frontmatter)
2. 검토 후 `manager-ddd` 위임 → TDD(RED→GREEN→REFACTOR), mock 포트로 검증
3. 게이트 재검증 → 커밋 → 푸시 (PR #1에 반영)

---

## 4. 라이브 전 정리할 follow-up 2건

- 🔸 **VR `V_n` 다주기 미진화**: `Strategy.plan_orders`가 주문만 반환 → 백테스트 엔진이 재계산된 가치선 `V_n`을 관측 못 함 → **다주기 VR 백테스트가 충실하지 않음**. `Strategy` 계약 보강 필요(상태 델타 반환 / `advance_state` / 엔진이 `next_value` 직접 호출 중 택1). MAB는 무관(상태가 체결에서 파생).
- 🔸 **MAB 쿼터매도 LOC 가격**: `mab_on_seed_exhausted`가 `OrderType.LOC` + `limit_price=None`(사실상 MOC). 백테스트는 종가 체결로 OK지만 **라이브 경로는 쿼터매도 가격 정의 또는 MOC 채택 필요**.

---

## 5. 보류 백로그 (P2 이후 후보)

- **SPEC-ADAPTER-002 (P3)** — 토스 **주문 write** 어댑터: `POST /api/v1/orders`(생성, LOC=`CLS` 매핑)·`/modify`·`/cancel`. `BrokerOrderPort` 구현체.
- **SPEC-BACKTEST-002** — 파라미터 스윕 + walk-forward + 레짐분할(상승/하락/횡보) + 강건한 고원 평가.
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
- SPEC: `.moai/specs/SPEC-{CORE,VR,MAB,BACKTEST,ADAPTER}-001/`
- 소스: `src/ballast/core/` (순수), `src/ballast/backtest/` (pandas/numpy 허용), `src/ballast/adapters/` (IO 경계)
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

_이 문서는 인계용 스냅샷이다. 작업이 진행되면 갱신하거나, PR #1 본문/`.moai/specs/`를 최신 출처로 삼을 것._
